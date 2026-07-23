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
- `find_track_url(artist, song, album=None)` — thin wrapper around
  `find_track_metadata` (LML#500). Used by the lookup hot path in
  `lookup.enrichment.enrich_artwork_results` when the WXYC library
  row clears the LML#477 title gate (just need the URL for the
  ``apple_music_url`` slot). Inherits `find_track_metadata`'s 3-way
  fuzz floor + artwork-preference tie-break so the two methods cannot
  drift on a multi-record response.
- `find_track_metadata(artist, song, album=None)` — used by the same hot
  path when the library row fails the gate (LML#487) or the catalog has
  no Discogs match (LML#401). 3-way fuzz floor on
  `attributes.{artistName,name,albumName}`; returns `AppleMusicTrackMatch`
  with the URL plus `attributes.artwork.url` (artwork) and
  `attributes.releaseDate` (year) so the synthesized
  `DiscogsSearchResult` surfaces the correct album's cover art instead
  of leaking a sibling-album library row's Discogs image. When every
  candidate fails only the album axis, re-scores the same response
  without it (LML#782 album-title divergence) and returns a URL-only,
  `album_verified=False` match.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import jwt as pyjwt
import sentry_sdk
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison

from clients.streaming.base import BaseStreamingClient
from clients.streaming.matching import (
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    find_best_source_match,
    record_match_telemetry,
    title_subset_is_degenerate,
)
from core.search import resolve_positive_int_env
from streaming.models import SourceMatch


@dataclass(frozen=True)
class AppleMusicTrackMatch:
    """Apple Music song-search verdict carrying the fields LML's lookup hot
    path needs to populate a synthesized ``DiscogsSearchResult`` when the
    WXYC catalog has no acceptable library row for the requested album
    (LML#487 / BS#1184).

    Extends ``find_track_url``'s plain ``str | None`` return with
    ``artwork_url`` and ``release_year``, extracted from the SAME
    ``search_song`` response payload — no extra Apple Music API rounds
    over the existing ``find_track_url`` quota. ``url`` is the Apple Music
    song deep-link (same value ``find_track_url`` returns).

    ``album_verified`` is True only when the winner cleared the
    ``album_score >= 80`` floor against a supplied album — the evidence
    the LML#505 override invalidation in ``lookup/enrichment/item.py``
    needs before clearing a library row's curated streaming URLs. Album-
    less winners and LML#782 album-fallback winners carry False: their
    album axis never passed, so they prove nothing about the requested
    album. Deliberately no default: a silent True would fail open
    (authorizing the destructive #505 clear), a silent False would fail
    open the other way (re-admitting the sibling-override leak #505
    fixed) — every construction site must state its evidence.
    """

    url: str
    artwork_url: str | None
    release_year: int | None
    album_verified: bool


logger = logging.getLogger(__name__)

# Minimum fuzzy score (0-100) for accepting an Apple Music search result as a
# genuine match. Bound to the shared `SCORE_MATCH_ACCEPTANCE_FLOOR` every other
# provider uses inside `clients/streaming/matching.is_acceptable_match` — not a
# re-declared literal, so the two can't drift (LML#592). Stops the wrong link
# from freezing onto a flowsheet row when Apple's search ranking is unstable for
# obscure artists (LML#389). On the album axis the floor now gates metadata
# rather than the link: a same-titled track from the wrong album (LML#396)
# loses its album-derived fields and `album_verified` status via the LML#782
# album-less fallback instead of being dropped outright.
_APPLE_MUSIC_MATCH_FLOOR = SCORE_MATCH_ACCEPTANCE_FLOOR

# Apple Music developer-token lifetime. Apple permits `exp` up to ~6 months;
# we sign per-request with a short window so a leaked token's blast radius
# stays small. 20 minutes is well above any single-request round-trip.
_JWT_LIFETIME_SECONDS = 1200

_SEARCH_URL = "https://api.music.apple.com/v1/catalog/us/search"

# Apple Music AsyncLimiter rate. The (max_rate, time_period) window is a fixed
# 60 s; only the per-minute ceiling (max_rate) is tunable, via
# LML_APPLE_MUSIC_RATE_PER_MIN. The default 60/min = 1 req/sec sustained is a
# SELF-imposed throttle, NOT an Apple-published limit — Apple publishes no
# official Apple Music API rate limit, and at 1 req/s the 4 s probe wait_for
# ceiling nulls a majority of live find_track_url probes under load. The knob
# lets the ceiling be widened from Railway with no redeploy + instant rollback
# (mirrors LML_STREAMING_WARM_CONCURRENCY). Raise it in steps
# (60 -> 300 -> 600/min = 1 -> 5 -> 10 req/s, still under the ~20 req/s
# community estimate) watching the 429-count + null-rate; the token is SHARED
# with staging + the docs/scripts.md resolver scripts, so the safe ceiling is
# the aggregate across all consumers, not prod /lookup alone.
_APPLE_MUSIC_RATE_PER_MIN_DEFAULT = 60
_APPLE_MUSIC_RATE_PERIOD_S = 60.0
APPLE_MUSIC_RATE_PER_MIN_ENV_VAR = "LML_APPLE_MUSIC_RATE_PER_MIN"
_SEMAPHORE_LIMIT = 5


def resolve_apple_music_rate_limit() -> tuple[float, float]:
    """Resolve the Apple Music ``AsyncLimiter`` ``(max_rate, time_period)``.

    ``max_rate`` (requests per fixed 60 s window) is env-tunable via
    :data:`APPLE_MUSIC_RATE_PER_MIN_ENV_VAR`; the window is fixed at 60 s so
    only the per-minute ceiling moves. The default preserves the historical
    1 req/s self-throttle when the var is unset (zero-behavior-change on
    merge). Read per-call (not at import) via :func:`resolve_positive_int_env`
    so the knob honors a runtime override and tests can monkeypatch it — see
    that helper for the unparseable/zero/negative -> default-with-WARN contract
    (a typo must not throttle to 0, which would serialize every probe).
    """
    max_rate = resolve_positive_int_env(
        APPLE_MUSIC_RATE_PER_MIN_ENV_VAR, _APPLE_MUSIC_RATE_PER_MIN_DEFAULT
    )
    return (float(max_rate), _APPLE_MUSIC_RATE_PERIOD_S)


# Latency budget: ``find_track_url`` is on the lookup hot path. With
# ``_MAX_RETRIES=4`` and a 60s ``Retry-After`` cap, a single sustained 429
# storm pinned one of 5 Semaphore slots for up to 240s — bulk burst then
# starved real-time ``/lookup`` (LML#449 + LML#450). Trim retries to 2
# (degrade-fast on transient upstream) and rely on the enrichment probe's
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
        super().__init__(
            rate_limit=resolve_apple_music_rate_limit(), semaphore_limit=_SEMAPHORE_LIMIT
        )
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
                raw_retry_after = resp.headers.get("Retry-After")
                retry_after = _parse_retry_after(raw_retry_after)
                # Log the raw header so operators can spot when Apple is
                # quota-pushing (Retry-After: 30+) vs returning routine 1-5s
                # 429s — the clamp at ``_RETRY_AFTER_CAP_SECONDS`` (LML#450)
                # is otherwise invisible. See LML#464.
                logger.warning(
                    "Apple Music 429 for %s - %s; Retry-After=%s sleeping %.1fs (attempt %d/%d)",
                    artist,
                    term,
                    raw_retry_after,
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
        """Search for `(artist, song[, album])` and return the Apple Music track
        URL of the best floor-clearing match, else `None`. The album axis is
        soft (LML#782): an album-constrained miss re-scores without it, so a
        supplied album narrows the choice but no longer vetoes the URL.

        Thin wrapper around ``find_track_metadata`` (LML#500): both methods
        score against the same ``search_song`` response with the same
        80/80(/80) ``token_set_ratio`` floor — keeping two scorers meant
        their match selection could drift silently (the artwork-preference
        tie-break iter-1 review added to ``find_track_metadata`` did not
        propagate here, so a multi-record response could surface two
        different URLs). Collapsing onto ``find_track_metadata`` puts the
        two methods in lockstep by construction.

        Returns only the ``url`` slot of the match — the happy path
        (``library_row_acceptable=True`` in ``enrich_one``,
        ``lookup/enrichment/item.py``)
        does not consume ``artwork_url`` or ``release_year`` (the
        Discogs-derived library row supplies those), so dropping them
        here keeps the existing call-site shape.
        """
        match = await self.find_track_metadata(artist, song, album=album)
        return match.url if match is not None else None

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
            service="apple_music",
        )

    async def find_track_metadata(
        self, artist: str, song: str, album: str | None = None
    ) -> AppleMusicTrackMatch | None:
        """Search for `(artist, song[, album])` and return URL + artwork + year.

        Surfaces the richer ``AppleMusicTrackMatch`` shape (LML#487 /
        BS#1184) — URL + artwork + year from the same ``search_song``
        response. Single Apple Music API call per invocation; the
        URL-only ``find_track_url`` wrapper delegates here so both
        methods scoring/selecting from the same record set is enforced
        by construction (LML#500).

        PREFERS floor-clearing matches that carry ``attributes.artwork``
        over higher-scoring matches that lack it. Returning
        artwork_url=None on the synthesis path defeats the whole point
        of LML#487 — for sparse Apple records (region-restricted
        singles, promo entries) we'd rather surface a lower-scoring-but-
        floor-clearing match's cover than nothing. The same preference
        carries over to ``find_track_url`` (post-collapse): the happy
        path doesn't consume artwork, but the URL it returns now comes
        from a likely-more-canonical record.

        When the album-constrained pass clears nobody, RE-SCORES the same
        response with the album axis dropped and returns the winner's URL
        only (LML#782 album-title divergence — see the fallback comment
        inline). Still a single Apple Music API call: the album constraint
        was never part of the ``search_song`` query, only of the scoring.
        """
        results = await self.search_song(artist, song)
        if not results:
            return None

        norm_artist = normalize_for_comparison(artist)
        norm_song = normalize_for_comparison(song)
        # ``or None``: a whitespace-only album normalizes to "" — no
        # scoreable constraint, so treat it as album-less rather than
        # running a guaranteed-miss constrained pass whose winner would
        # be mislabeled ``track_album_fallback`` in LML#592 telemetry.
        norm_album = (normalize_for_comparison(album) or None) if album else None

        best, (artist_axis, track_axis) = _select_best_track_candidate(
            results,
            norm_artist=norm_artist,
            norm_song=norm_song,
            norm_album=norm_album,
            raw_song=song,
        )

        # LML#782: album-title divergence. The WXYC catalog album and
        # Apple's album for the same track can legitimately differ (Friko's
        # "Get Numb To It!" sits on an Apple album titled "Get Numb to
        # It!", not the catalog's "RED XEROX"), and the album-keyed
        # streaming-URL post-process can never rescue such rows — it
        # searches Apple by the request album, which doesn't exist there.
        # Returning None here therefore froze these tracks as permanent
        # nulls once Backend-Service persisted the miss (BS#1192). Re-score
        # the SAME response with the album axis dropped: the artist/track
        # 80/80 floors (LML#389) and the LML#719 degenerate-subset guard
        # still apply, so only the album constraint is relaxed. Zero extra
        # API calls — the album was only ever a scoring-time filter. The
        # winner is surfaced URL-only (below): its album-derived fields
        # describe an album that just FAILED the floor against the request,
        # so carrying them would be the LML#396/#487 wrong-album leak.
        # Known recall trade: the fallback URL fills the Apple slot, which
        # preempts the album-keyed post-process leg (it fires only on a
        # null slot). For genuine divergence rows that leg could never
        # succeed anyway; when the requested album DOES exist on Apple but
        # its track version fell outside this search's top results, the
        # track deep-link wins over the eventually-warmed album-canonical
        # URL — the issue's "URL beats null" call, accepted knowingly.
        album_fallback = False
        if best is None and norm_album is not None:
            best, (artist_axis, track_axis) = _select_best_track_candidate(
                results,
                norm_artist=norm_artist,
                norm_song=norm_song,
                norm_album=None,
                raw_song=song,
            )
            album_fallback = True

        if best is None:
            return None

        record_match_telemetry(
            artist_score=artist_axis,
            title_score=track_axis,
            service="apple_music",
            # The fallback winner gets its own surface so LML#592 dashboards
            # can watch the fallback rate separately from ordinary track wins.
            surface="track_album_fallback" if album_fallback else "track",
            # LML#592 labeling: request values vs the CHOSEN winner's strings
            # (same record the axes were stashed from), for the marginal sample.
            query_artist=artist,
            matched_artist=_extract_artist_name(best),
            query_title=song,
            matched_title=_extract_name(best),
        )

        # When no album constrained the scoring (``norm_album is None``
        # covers both ``album=None`` and a falsy/empty album string) the
        # 80/80(/80) floor collapses to 80/80 — any artist+song match
        # clears regardless of album, and Apple typically returns the most
        # popular album containing this song title. The match's URL is
        # per-track so it stays, but the album-derived fields
        # (``artwork_url``, ``release_year``) describe whatever album Apple
        # ranked highest; surfacing them on the synthesized result would be
        # a wrong-album leak. ~40% of request-o-matic traffic is
        # artist+song-only. The LML#782 fallback winner is the same trade
        # with sharper evidence — its album just FAILED the floor against
        # the requested one — so it is URL-only too. Either way the album
        # axis never passed, so the match is ``album_verified=False`` and
        # the LML#505 invalidation downstream must not treat it as proof
        # the requested album exists on Apple.
        if norm_album is None or album_fallback:
            return AppleMusicTrackMatch(
                url=_extract_url(best),
                artwork_url=None,
                release_year=None,
                album_verified=False,
            )

        return AppleMusicTrackMatch(
            url=_extract_url(best),
            artwork_url=_extract_artwork_url(best),
            release_year=_extract_release_year(best),
            album_verified=True,
        )


def _select_best_track_candidate(
    results: list[dict],
    *,
    norm_artist: str,
    norm_song: str,
    norm_album: str | None,
    raw_song: str,
) -> tuple[dict | None, tuple[float, float]]:
    """One scoring pass over a ``search_song`` response.

    Applies the 80/80(/80) ``token_set_ratio`` floor per axis plus the
    LML#719 degenerate-subset guard on the title axis, and picks the
    highest-combined-score candidate — PREFERRING floor-clearers that
    carry ``attributes.artwork`` over higher-scoring ones that lack it
    (see ``find_track_metadata``'s docstring for why).

    Extracted from ``find_track_metadata`` so the LML#782 album-less
    fallback can re-score the same in-memory response with
    ``norm_album=None`` instead of paying a second Apple Music call.
    ``raw_song`` is the un-normalized request title — the LML#719 guard
    does its own normalization internally.

    Returns ``(best, (artist_score, track_score))`` — the chosen record
    (or ``None`` when nothing clears) plus its per-axis scores, stashed
    in lockstep with the candidate so they describe whichever record
    ``best_with_artwork or best_overall`` actually selected, not the
    higher-fuzz record that artwork preference may have passed over
    (LML#592).
    """
    best_overall: dict | None = None
    best_overall_score = 0.0
    best_overall_axes: tuple[float, float] = (0.0, 0.0)
    best_with_artwork: dict | None = None
    best_with_artwork_score = 0.0
    best_with_artwork_axes: tuple[float, float] = (0.0, 0.0)

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
        # LML#719: token_set_ratio returns 100 for any token-subset, so a
        # short generic query title buried in a long unrelated candidate
        # clears the floor with no real signal — fatal on Various-Artists
        # requests where the artist axis can't disambiguate (a `Various
        # Artists` request shares the `various artists` token cluster with
        # any VA-credited catalog spam). Reject the title axis's degenerate
        # subsets while keeping the legitimate variant subsets the
        # token_set path exists to admit. Title axis only — the artist axis
        # keeps token_set for leader->ensemble subsets (Yves -> Yves Tumor).
        #
        # Recall cost (accepted): the guard is unconditional, so a genuine
        # partial/truncated query title that is a NON-suffix token-subset
        # (`Fade` -> `Fade Into You`, `Motion` -> `Motion Sickness`) now
        # drops even when the artist axis is an exact match — these scored
        # token_set 100 pre-LML#719. This is the fail-safe trade: dropping
        # Apple enrichment (URL/artwork/year) beats caching a wrong one, and
        # the loss only bites when the track is ALSO absent from the WXYC
        # library (the synthesis path is the sole find_track_metadata
        # caller; in-catalog rows source artwork from the library row, not
        # this probe). Never alters the DJ's typed artist/song text.
        if title_subset_is_degenerate(raw_song, attrs.get("name") or ""):
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
        if combined > best_overall_score:
            best_overall_score = combined
            best_overall = item
            best_overall_axes = (artist_score, track_score)
        if (attrs.get("artwork") or {}).get("url") and combined > best_with_artwork_score:
            best_with_artwork_score = combined
            best_with_artwork = item
            best_with_artwork_axes = (artist_score, track_score)

    best = best_with_artwork or best_overall
    axes = best_with_artwork_axes if best is best_with_artwork else best_overall_axes
    return best, axes


def _extract_artist_name(item: dict) -> str:
    return (item.get("attributes") or {}).get("artistName") or ""


def _extract_name(item: dict) -> str:
    return (item.get("attributes") or {}).get("name") or ""


def _extract_url(item: dict) -> str:
    return (item.get("attributes") or {}).get("url") or ""


# Apple Music's artwork URLs are documented as `{w}x{h}` templates that the
# client substitutes with desired pixel dimensions before display. iOS/dj-site
# render at ~600px in flowsheet card sizing; 600x600 keeps payload tight while
# staying above retina display thresholds.
_ARTWORK_WIDTH = 600
_ARTWORK_HEIGHT = 600


def _extract_artwork_url(item: dict) -> str | None:
    """Pull `attributes.artwork.url` and substitute the `{w}x{h}` template.

    Apple Music returns artwork URLs as templates (documented at
    developer.apple.com/documentation/applemusicapi/artwork). Clients
    (iOS, dj-site) cannot render the template — substitute concrete
    dimensions before surfacing. Returns ``None`` when ``artwork`` is
    absent (region-restricted records, malformed items) so the caller
    can synthesize a record with ``artwork_url=None``.
    """
    attrs = item.get("attributes") or {}
    artwork = attrs.get("artwork") or {}
    template = artwork.get("url")
    if not template:
        return None
    return template.replace("{w}", str(_ARTWORK_WIDTH)).replace("{h}", str(_ARTWORK_HEIGHT))


_MIN_PLAUSIBLE_RELEASE_YEAR = 1900
_MAX_PLAUSIBLE_RELEASE_YEAR = 2100


def _extract_release_year(item: dict) -> int | None:
    """Pull the leading 4-digit ASCII year from `attributes.releaseDate`.

    Apple Music's catalog albums carry ISO dates (YYYY-MM-DD), but rare
    records ship just the year, sentinel placeholders ("0000-00-00"), or
    partial strings. Reject anything that isn't a 4-ASCII-digit year in a
    plausible range so 0/9999/202 don't reach downstream as a release year.
    """
    attrs = item.get("attributes") or {}
    raw = attrs.get("releaseDate")
    if not raw or not isinstance(raw, str):
        return None
    head = raw[:4]
    if len(head) != 4 or not head.isascii() or not head.isdigit():
        return None
    year = int(head)
    if year < _MIN_PLAUSIBLE_RELEASE_YEAR or year > _MAX_PLAUSIBLE_RELEASE_YEAR:
        return None
    return year


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
