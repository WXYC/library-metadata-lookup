"""Authenticated Apple Music API client.

Replaces the unauthenticated iTunes Search endpoint (`itunes.apple.com/search`)
after the 2026-05-28 Railway egress 403 (LML#443). See
`docs/adr/0001-authenticated-apple-music-api.md` for the migration rationale.

Auth: ES256 developer token (JWT), signed per request with a ~20 minute
lifetime, header `kid=Key ID`, claims `iss=Team ID`, `iat`, `exp`. Apple
validates the signature against the public half registered to the MusicKit
identifier `media.org.wxyc.lml`.

Surface area mirrors the previous iTunes client so the migration of the two
call sites (`lookup/orchestrator._fetch_apple_music_url` and
`streaming/router.handle_streaming_check`) is mechanical:

- `find_album_match(artist, title)` — BaseStreamingClient contract; powers
  /streaming-check.
- `find_track_url(artist, song, album=None)` — replaces the inline
  `_fetch_apple_music_url` in the lookup hot path. 3-way fuzz floor on
  `attributes.{artistName,name,albumName}`.
"""

from __future__ import annotations

import logging
import time

import httpx
import jwt as pyjwt
import sentry_sdk
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


class AppleMusicClient(BaseStreamingClient):
    """Authenticated Apple Music API client.

    Inherits the FD-leak-safe httpx singleton + AsyncLimiter rate limiting +
    asyncio.Semaphore concurrency control from `BaseStreamingClient` (the
    LML#241 invariant: no per-request `httpx.AsyncClient` construction on the
    hot path).

    Rate limit `(60, 60)` and semaphore `5` are Apple's published catalog
    quota (~3000 req/h with comfort margin), sized to peak request-o-matic
    burst behavior. Per-call JWT signing keeps the lifetime short without
    needing in-process token caching.

    Args:
        team_id: Apple Developer Team ID (10-char), used as JWT `iss` claim.
        key_id: MusicKit Key ID (10-char), used as JWT `kid` header.
        private_key: PEM contents of the ES256 private key downloaded from
            the Apple Developer console at key creation. Apple ships the
            `.p8` exactly once — losing it requires revoke + re-issue.
    """

    def __init__(self, team_id: str, key_id: str, private_key: str):
        super().__init__(rate_limit=(60, 60), semaphore_limit=5)
        self._team_id = team_id
        self._key_id = key_id
        self._private_key = private_key

    def _sign_jwt(self) -> str:
        """Sign a fresh ES256 developer token. ~20-minute lifetime."""
        now = int(time.time())
        return pyjwt.encode(
            {"iss": self._team_id, "iat": now, "exp": now + _JWT_LIFETIME_SECONDS},
            self._private_key,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )

    async def search_songs(self, artist: str, song: str) -> list[dict]:
        """Search Apple Music for songs matching artist + song.

        Returns the raw `results.songs.data` list, or `[]` on error. Each item
        carries `attributes.{artistName, name, albumName, url}`.
        """
        return await self._search(artist, song, types="songs", result_key="songs")

    async def search_albums(self, artist: str, title: str) -> list[dict]:
        """Search Apple Music for albums matching artist + title.

        Returns the raw `results.albums.data` list, or `[]` on error. Each item
        carries `attributes.{artistName, name, url}`.
        """
        return await self._search(artist, title, types="albums", result_key="albums")

    async def _search(self, artist: str, term: str, *, types: str, result_key: str) -> list[dict]:
        """Issue a signed search request and return the typed `data` list.

        Observability (O3): every call sets `apple_music.search.{status,result}`
        on the active Sentry span; non-2xx additionally `capture_message`s so
        the silent-failure mode the old iTunes path exhibited (LML#444) can't
        recur. The previous client absorbed errors into `[]` with only a log;
        the new contract surfaces them upstream.
        """
        transaction = sentry_sdk.get_current_scope().transaction
        try:
            async with self._semaphore:
                await self._rate_limiter.acquire()
                http = await self._get_client()
                token = self._sign_jwt()
                resp = await http.get(
                    _SEARCH_URL,
                    params={"term": f"{artist} {term}", "types": types, "limit": 25},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except Exception:
            logger.exception("Apple Music search raised for %s - %s", artist, term)
            if transaction is not None:
                transaction.set_data("apple_music.search.result", "error")
            sentry_sdk.capture_message("apple_music.search.exception", level="warning")
            return []

        if transaction is not None:
            transaction.set_data("apple_music.search.status", resp.status_code)

        if resp.status_code != 200:
            logger.warning(
                "Apple Music search returned %d for %s - %s",
                resp.status_code,
                artist,
                term,
            )
            if transaction is not None:
                transaction.set_data("apple_music.search.result", str(resp.status_code))
            sentry_sdk.capture_message(
                f"apple_music.search.status={resp.status_code}",
                level="warning",
            )
            return []

        data = resp.json().get("results", {}).get(result_key, {}).get("data", [])
        if transaction is not None:
            transaction.set_data("apple_music.search.result", "hit" if data else "miss")
        return data

    async def find_track_url(self, artist: str, song: str, album: str | None = None) -> str | None:
        """Search for `(artist, song[, album])` and return the best Apple Music
        track URL clearing the 80/80(/80) fuzz floor, else `None`.

        Replaces `lookup.orchestrator._fetch_apple_music_url`. Uses
        `fuzz.token_set_ratio` (not the batch matcher's `token_sort_ratio`)
        so extra tokens — "The", "feat. X", "(Remastered)" — don't sink an
        otherwise-correct match.
        """
        results = await self.search_songs(artist, song)
        if not results:
            return None

        norm_artist = normalize_for_comparison(artist)
        norm_song = normalize_for_comparison(song)
        norm_album = normalize_for_comparison(album) if album else None

        for item in results:
            attrs = item.get("attributes", {})
            url = attrs.get("url")
            if not url:
                continue
            artist_score = fuzz.token_set_ratio(
                norm_artist, normalize_for_comparison(attrs.get("artistName", ""))
            )
            track_score = fuzz.token_set_ratio(
                norm_song, normalize_for_comparison(attrs.get("name", ""))
            )
            if artist_score < _APPLE_MUSIC_MATCH_FLOOR or track_score < _APPLE_MUSIC_MATCH_FLOOR:
                continue
            if norm_album is not None:
                album_score = fuzz.token_set_ratio(
                    norm_album, normalize_for_comparison(attrs.get("albumName", ""))
                )
                if album_score < _APPLE_MUSIC_MATCH_FLOOR:
                    continue
            return url
        return None

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search Apple Music for `(artist, title)` and return the best match.

        See `BaseStreamingClient.find_album_match`. The Apple Music response
        shape (`attributes.{artistName, name, url}`) is encapsulated here;
        the LML#389 wrong-artist guard lives in the shared
        `is_acceptable_match` floor inside `find_best_match`.
        """
        return find_best_source_match(
            await self.search_albums(artist, title),
            artist,
            title,
            artist_fn=lambda x: x["attributes"]["artistName"],
            title_fn=lambda x: x["attributes"]["name"],
            url_fn=lambda x: x["attributes"]["url"],
        )

    async def _make_client(self) -> httpx.AsyncClient:
        """Override the base 10s timeout — Apple Music search is fast (<1s p99)
        and the orchestrator's outer search-budget cap is the real backstop."""
        return httpx.AsyncClient(timeout=10.0)
