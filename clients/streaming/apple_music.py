"""Authenticated Apple Music API client.

Replaces the unauthenticated iTunes Search endpoint (`itunes.apple.com/search`)
after the 2026-05-28 Railway egress 403 (LML#443). See
`docs/adr/0001-authenticated-apple-music-api.md` for the migration rationale.

Auth: ES256 developer token (JWT), signed per request with a ~20 minute
lifetime, header `kid=Key ID`, claims `iss=Team ID`, `iat`, `exp`. Apple
validates the signature against the public half registered to the MusicKit
identifier `media.org.wxyc.lml`.

Public surface:

- `find_album_match(artist, title)` — BaseStreamingClient contract; powers
  `/streaming-check` via `streaming/router.handle_streaming_check`.
- `find_track_url(artist, song, album=None)` — used by the lookup hot path
  in `lookup/orchestrator.enrich_artwork_results`. 3-way fuzz floor on
  `attributes.{artistName,name,albumName}`.
"""

from __future__ import annotations

import asyncio
import logging
import time

import jwt as pyjwt
import sentry_sdk
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison

from clients.streaming.base import BaseStreamingClient
from clients.streaming.matching import find_best_source_match
from streaming.models import SourceMatch

logger = logging.getLogger(__name__)

# Minimum fuzzy score (0-100) for accepting an Apple Music search result as a
# genuine match. Mirrors the 80/80 floor every other provider uses inside
# `clients/streaming/matching.is_acceptable_match`. Stops the wrong link from
# freezing onto a flowsheet row when Apple's search ranking is unstable for
# obscure artists (LML#389) or returns a same-titled track from the wrong
# album (LML#396).
_APPLE_MUSIC_MATCH_FLOOR = 80.0

# Apple Music developer-token lifetime. Apple permits `exp` up to ~6 months;
# we sign per-request with a short window so a leaked token's blast radius
# stays small. 20 minutes is well above any single-request round-trip.
_JWT_LIFETIME_SECONDS = 1200

_SEARCH_URL = "https://api.music.apple.com/v1/catalog/us/search"

# Apple's published catalog quota and our concurrency ceiling. (60, 60) =
# 1 req/sec sustained = 3600 req/h; sized below Apple's per-team daily cap
# with comfort margin for request-o-matic bursts.
_RATE_LIMIT = (60.0, 60.0)
_SEMAPHORE_LIMIT = 5
# Latency budget: ``find_track_url`` is on the lookup hot path. With
# ``_MAX_RETRIES=4`` and a 60s ``Retry-After`` cap, a single sustained 429
# storm pinned one of 5 Semaphore slots for up to 240s — bulk burst then
# starved real-time ``/lookup`` (LML#449 + LML#450). Trim retries to 2
# (degrade-fast on transient upstream) and rely on the orchestrator's
# ``asyncio.wait_for`` ceiling for the request-level latency cap.
_MAX_RETRIES = 2

# Status codes the retry loop sleeps + retries on. Anything else is terminal.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Hard cap for a single ``Retry-After`` honor; before LML#450 this was 60s
# which let one slot stall for a minute on a single pathological response.
_RETRY_AFTER_CAP_SECONDS = 5.0


class AppleMusicClient(BaseStreamingClient):
    """Authenticated Apple Music API client.

    Inherits the FD-leak-safe httpx singleton + AsyncLimiter rate limiting +
    asyncio.Semaphore concurrency control from `BaseStreamingClient` (the
    LML#241 invariant: no per-request `httpx.AsyncClient` construction on the
    hot path).

    Per-call JWT signing uses a pre-parsed private key (cached on the
    instance) so request hot-path cost is just an ECDSA signature, not a PEM
    parse.

    Args:
        team_id: Apple Developer Team ID (10-char), used as JWT `iss` claim.
        key_id: MusicKit Key ID (10-char), used as JWT `kid` header.
        private_key: PEM contents of the ES256 private key downloaded from
            the Apple Developer console at key creation. Apple ships the
            `.p8` exactly once — losing it requires revoke + re-issue.

    Raises:
        ValueError: if `private_key` is not a parseable PEM-encoded ES256
            private key. Surfaced at startup so a Railway env-var that
            mangled newlines into literal `\\n` fails fast rather than
            silently degrading every request to `[]`.
    """

    def __init__(self, team_id: str, key_id: str, private_key: str):
        super().__init__(rate_limit=_RATE_LIMIT, semaphore_limit=_SEMAPHORE_LIMIT)
        self._team_id = team_id
        self._key_id = key_id
        # Parse once at startup so per-request cost is just the ECDSA
        # signature, and so a malformed PEM (Railway sometimes renders
        # newlines as the literal two-char `\n` sequence) raises here
        # instead of silently breaking every search.
        loaded = serialization.load_pem_private_key(
            private_key.encode() if isinstance(private_key, str) else private_key,
            password=None,
        )
        if not isinstance(loaded, EllipticCurvePrivateKey):
            raise ValueError(
                f"Apple Music private_key must be ES256/P-256, got {type(loaded).__name__}"
            )
        self._private_key: EllipticCurvePrivateKey = loaded

    def _sign_jwt(self) -> str:
        """Sign a fresh ES256 developer token. ~20-minute lifetime."""
        now = int(time.time())
        return pyjwt.encode(
            {"iss": self._team_id, "iat": now, "exp": now + _JWT_LIFETIME_SECONDS},
            self._private_key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )

    async def search_song(self, artist: str, song: str) -> list[dict]:
        """Search Apple Music for songs matching artist + song.

        Returns the raw `results.songs.data` list, or `[]` on error. Each item
        carries `attributes.{artistName, name, albumName, url}`.
        """
        return await self._search(artist, song, types="songs", result_key="songs")

    async def search_album(self, artist: str, title: str) -> list[dict]:
        """Search Apple Music for albums matching artist + title.

        Returns the raw `results.albums.data` list, or `[]` on error. Each item
        carries `attributes.{artistName, name, url}`.
        """
        return await self._search(artist, title, types="albums", result_key="albums")

    async def _search(self, artist: str, term: str, *, types: str, result_key: str) -> list[dict]:
        """Issue a signed search request and return the typed `data` list.

        Wraps the call in a dedicated `apple_music.search` child span so
        concurrent searches don't overwrite each other's status/result
        attributes on the root transaction (LML#213 wrap-at-chokepoint
        pattern). Non-200s + exceptions log + `capture_exception`/`capture_message`
        so the silent-failure mode the old iTunes path exhibited (LML#444)
        cannot recur.

        Retries 429 (honoring `Retry-After`) and transient 5xx with
        exponential backoff. All other non-200s return `[]` after one attempt.
        """
        with sentry_sdk.start_span(op="apple_music.search", name=types) as span:
            try:
                async with self._semaphore:
                    data, last_status = await self._search_with_retry(
                        artist, term, types=types, result_key=result_key, span=span
                    )
            except Exception:
                logger.exception("Apple Music search raised for %s - %s", artist, term)
                span.set_data("apple_music.search.result", "error")
                sentry_sdk.capture_exception()
                return []

            # `result` is the chokepoint signal Sentry dashboards filter on.
            # Hit/miss only make sense for the 200 path; non-200 statuses
            # carry the failure mode directly so a `result:403` query
            # surfaces auth outages distinctly from genuine catalog misses.
            if last_status == 200:
                span.set_data("apple_music.search.result", "hit" if data else "miss")
            else:
                span.set_data(
                    "apple_music.search.result",
                    f"retries_exhausted_{last_status}"
                    if last_status in _RETRYABLE_STATUSES
                    else str(last_status),
                )
            return data

    async def _search_with_retry(
        self,
        artist: str,
        term: str,
        *,
        types: str,
        result_key: str,
        span: sentry_sdk.tracing.Span,
    ) -> tuple[list[dict], int | None]:
        """Single-attempt fetch with 429/5xx retry.

        Returns ``(data, last_status)``. ``data`` is the typed results array
        on 200, ``[]`` on any non-200 outcome. ``last_status`` is the last
        HTTP status code observed (None only if a transport exception was
        raised inside the outer try/except). The caller decides what
        observability marker to project — this layer only sets ``status``
        on the span.
        """
        http = await self._get_client()
        last_status: int | None = None

        for attempt in range(_MAX_RETRIES):
            await self._rate_limiter.acquire()
            token = self._sign_jwt()
            resp = await http.get(
                _SEARCH_URL,
                params={"term": f"{artist} {term}", "types": types, "limit": 25},
                headers={"Authorization": f"Bearer {token}"},
            )
            last_status = resp.status_code
            span.set_data("apple_music.search.status", resp.status_code)

            if resp.status_code == 200:
                # `resp.json()` can raise on a 200-with-non-JSON body (CDN
                # edge-cache HTML errors are real); the outer except catches
                # it as `result=error` + capture_exception.
                payload = resp.json()
                return payload.get("results", {}).get(result_key, {}).get("data", []), 200

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                logger.warning(
                    "Apple Music 429 for %s - %s; sleeping %.1fs (attempt %d/%d)",
                    artist,
                    term,
                    retry_after,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code in _RETRYABLE_STATUSES:
                backoff = min(2**attempt, 30)
                logger.warning(
                    "Apple Music %d for %s - %s; backing off %ds (attempt %d/%d)",
                    resp.status_code,
                    artist,
                    term,
                    backoff,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                await asyncio.sleep(backoff)
                continue

            # Terminal non-200 (4xx other than 429): no recovery, log + capture.
            logger.warning(
                "Apple Music search returned %d for %s - %s",
                resp.status_code,
                artist,
                term,
            )
            sentry_sdk.capture_message(
                f"apple_music.search.status={resp.status_code}",
                level="warning",
            )
            return [], resp.status_code

        # Exhausted retries on 429/5xx.
        logger.error(
            "Apple Music max retries exhausted for %s - %s (last status %s)",
            artist,
            term,
            last_status,
        )
        sentry_sdk.capture_message(
            f"apple_music.search.retries_exhausted last_status={last_status}",
            level="warning",
        )
        return [], last_status

    async def find_track_url(self, artist: str, song: str, album: str | None = None) -> str | None:
        """Search for `(artist, song[, album])` and return the highest-scoring
        Apple Music track URL clearing the 80/80(/80) fuzz floor, else `None`.

        Uses `fuzz.token_set_ratio` (not the batch matcher's `token_sort_ratio`)
        so extra tokens — "The", "feat. X", "(Remastered)" — don't sink an
        otherwise-correct match. Iterates the full result list and picks the
        best-scoring URL so an early sub-optimal match in Apple's ranking
        order doesn't freeze the wrong link onto a flowsheet row.
        """
        results = await self.search_song(artist, song)
        if not results:
            return None

        norm_artist = normalize_for_comparison(artist)
        norm_song = normalize_for_comparison(song)
        norm_album = normalize_for_comparison(album) if album else None

        best_url: str | None = None
        best_score = 0.0

        for item in results:
            attrs = item.get("attributes") or {}
            url = attrs.get("url")
            if not url:
                continue
            artist_score = fuzz.token_set_ratio(
                norm_artist, normalize_for_comparison(attrs.get("artistName") or "")
            )
            track_score = fuzz.token_set_ratio(
                norm_song, normalize_for_comparison(attrs.get("name") or "")
            )
            if artist_score < _APPLE_MUSIC_MATCH_FLOOR or track_score < _APPLE_MUSIC_MATCH_FLOOR:
                continue
            if norm_album is not None:
                album_score = fuzz.token_set_ratio(
                    norm_album, normalize_for_comparison(attrs.get("albumName") or "")
                )
                if album_score < _APPLE_MUSIC_MATCH_FLOOR:
                    continue
                combined = (artist_score + track_score + album_score) / 3
            else:
                combined = (artist_score + track_score) / 2
            if combined > best_score:
                best_score = combined
                best_url = url

        return best_url

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search Apple Music for `(artist, title)` and return the best match.

        See `BaseStreamingClient.find_album_match`. The Apple Music response
        shape (`attributes.{artistName, name, url}`) is encapsulated here;
        the LML#389 wrong-artist guard lives in the shared
        `is_acceptable_match` floor inside `find_best_match`. Extractors use
        `.get()` chains so a malformed item in Apple's `data` list (sparse
        record, region-restricted, etc.) is skipped via score=0 rather than
        raising mid-iteration and erasing every legitimate match.
        """
        return find_best_source_match(
            await self.search_album(artist, title),
            artist,
            title,
            artist_fn=_extract_artist_name,
            title_fn=_extract_name,
            url_fn=_extract_url,
        )


def _extract_artist_name(item: dict) -> str:
    return (item.get("attributes") or {}).get("artistName") or ""


def _extract_name(item: dict) -> str:
    return (item.get("attributes") or {}).get("name") or ""


def _extract_url(item: dict) -> str:
    return (item.get("attributes") or {}).get("url") or ""


def _parse_retry_after(value: str | None) -> float:
    """Apple Music `Retry-After` is documented as integer seconds. Honor it
    when present; fall back to ``_RETRY_AFTER_CAP_SECONDS`` (5s) if absent or
    malformed. Capped at the same value so a pathological upstream response
    can't stall a Semaphore slot for the worst case (LML#450)."""
    if not value:
        return _RETRY_AFTER_CAP_SECONDS
    try:
        return min(float(value), _RETRY_AFTER_CAP_SECONDS)
    except ValueError:
        return _RETRY_AFTER_CAP_SECONDS
