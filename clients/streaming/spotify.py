"""Spotify Web API client with client credentials auth and rate limiting."""

from __future__ import annotations

import asyncio
import base64
import logging
import time

from clients.streaming.base import BaseStreamingClient, get_probe_deadline
from clients.streaming.matching import find_best_source_match
from streaming.models import SourceMatch

logger = logging.getLogger(__name__)


class SpotifyRetryBudgetExceededError(Exception):
    """Raised by ``_search_with_retry_type`` when a 429 or 5xx retry's
    computed delay would sleep past the active background-warm-probe
    deadline (``clients.streaming.base.get_probe_deadline``, LML#1108).

    Distinct from the plain ``[]`` the loop still returns after genuinely
    exhausting ``self._max_retries`` with no deadline active: a budget
    give-up is an inconclusive "couldn't finish asking," never a confirmed
    no-match. ``search_album``/``search_track`` re-raise this past their own
    broad ``except Exception`` so it propagates out of ``find_album_match``
    to ``entity.streaming_url_cache.resolve_streaming_url_with_cache``'s
    ``except Exception`` -- which degrades to ``live_error`` and, critically,
    writes NO cache row (see that module's "don't poison the cache on
    timeout/exception" contract). Returning ``[]`` here instead would read as
    a confirmed "not on Spotify" and mint a 7-day negative-cache entry for an
    album that was merely rate-limited or hit a transient 5xx -- the exact
    poisoning bug this exception exists to prevent. Mirrors
    ``clients.bandcamp.BandcampRateLimitedError``'s identical rationale for
    its own fail-fast 429 path.
    """


class SpotifyClient(BaseStreamingClient):
    """Spotify Web API client using client credentials flow for album searches."""

    ACCOUNTS_URL = "https://accounts.spotify.com/api/token"
    API_BASE = "https://api.spotify.com/v1"

    def __init__(self, client_id: str, client_secret: str):
        super().__init__(rate_limit=(30, 30), semaphore_limit=5)
        self._client_id = client_id
        self._client_secret = client_secret
        self._access_token: str | None = None
        self._token_expires_at: float = 0
        self._max_retries = 5

    async def _ensure_token(self) -> str:
        """Get or refresh the access token via client credentials flow."""
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token

        http = await self._get_client()
        credentials = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()

        resp = await http.post(
            self.ACCOUNTS_URL,
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        )
        resp.raise_for_status()
        data = resp.json()

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data["expires_in"] - 60
        logger.debug("Acquired Spotify access token (expires in %ds)", data["expires_in"])
        return self._access_token

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search Spotify for ``(artist, title)`` and return the best match.

        See ``BaseStreamingClient.find_album_match`` for the contract. The
        Spotify response shape (``artists[0].name`` / ``name`` /
        ``external_urls.spotify``) is encapsulated here.
        """
        return find_best_source_match(
            await self.search_album(artist, title),
            artist,
            title,
            artist_fn=lambda x: x["artists"][0]["name"],
            title_fn=lambda x: x["name"],
            url_fn=lambda x: x["external_urls"]["spotify"],
            service="spotify",
        )

    async def search_album(self, artist: str, title: str, market: str = "US") -> list[dict]:
        """Search Spotify for albums matching artist + title.

        Tries quoted field search first, falls back to unquoted if no results.
        Returns a list of album dicts from the Spotify API, or an empty list on
        error. LML#1108: a ``SpotifyRetryBudgetExceededError`` is deliberately
        NOT caught here -- it re-raises past this method (see that
        exception's docstring for why an empty list would be the wrong
        shape). Because it propagates as a raise rather than an empty
        ``results``, the unquoted fallback below never fires for a budget
        give-up either -- a second live request to a service that just
        signaled backoff would be exactly the wrong response.
        """
        try:
            async with self._semaphore:
                # Quoted field search (strict)
                results = await self._search_with_retry(artist, title, market)
                if results:
                    return results

                # Unquoted fallback (fuzzier)
                return await self._search_with_retry_type(
                    artist, title, "album", market, quoted=False
                )
        except SpotifyRetryBudgetExceededError:
            raise
        except Exception:
            logger.exception("Spotify search failed for %s - %s", artist, title)
            return []

    async def search_track(self, artist: str, track: str, market: str = "US") -> list[dict]:
        """Search Spotify for tracks matching artist + track name.

        Returns a list of track dicts from the Spotify API, or an empty list
        on error. LML#1108: ``SpotifyRetryBudgetExceededError`` re-raises
        past this method -- see ``search_album``'s docstring.
        """
        try:
            async with self._semaphore:
                return await self._search_with_retry_type(artist, track, "track", market)
        except SpotifyRetryBudgetExceededError:
            raise
        except Exception:
            logger.exception("Spotify track search failed for %s - %s", artist, track)
            return []

    async def _search_with_retry(self, artist: str, title: str, market: str) -> list[dict]:
        return await self._search_with_retry_type(artist, title, "album", market)

    @staticmethod
    def _delay_exceeds_probe_budget(delay: float) -> tuple[bool, float | None]:
        """Whether sleeping ``delay`` seconds would run past the active
        background-warm-probe deadline (LML#1108).

        A background streaming-URL warm probe
        (``lookup.streaming_warm._warm_streaming_url_cache``) arms
        an absolute deadline via ``clients.streaming.base.set_probe_deadline``
        before calling into this client -- its own ``asyncio.wait_for`` will
        cancel this coroutine at, or just before (it re-reads the clock a
        moment later), that same deadline regardless of what the retry loop
        does. A sleep that can't fit in what's left is therefore a
        GUARANTEED mid-sleep cancellation (LIBRARY-METADATA-LOOKUP-1B: 33k+
        occurrences), whether the triggering response was a 429 or a 5xx.

        Returns ``(exceeds, remaining_budget)``. ``remaining_budget`` is
        ``None`` when no deadline is active (interactive `/lookup`, offline
        scripts) -- callers keep the pre-#1108 attempt-count-only bound in
        that case, matching every OTHER caller of this client.
        """
        deadline = get_probe_deadline()
        if deadline is None:
            return False, None
        remaining_budget = deadline - time.monotonic()
        return delay > remaining_budget, remaining_budget

    async def _search_with_retry_type(
        self, artist: str, term: str, search_type: str, market: str, quoted: bool = True
    ) -> list[dict]:
        # LML#1108: this loop shares one attempt budget (``self._max_retries``)
        # across two DIFFERENT retry policies -- 429 (Retry-After, potentially
        # hours long) and 5xx (a bounded exponential backoff, capped at 30s).
        # ``discogs.admission.retry_429`` (the shared helper ``clients/bandcamp.py``
        # delegates to under LML#1040) owns only the 429 half: it returns
        # immediately on ANY non-429 status, including 5xx. Fully delegating here
        # would mean either dropping Spotify's existing 5xx resilience or nesting
        # an independent 5xx sub-loop inside `retry_429`'s per-attempt callback --
        # which would let a request that alternates between 5xx and 429 rack up
        # `max_retries` 429-attempts PER 5xx cycle instead of `max_retries` TOTAL,
        # a real (if unlikely) amplification this deadline-inversion fix has no
        # reason to introduce. The loop stays bespoke; both retry-status branches
        # below now gain the LML#758-style budget-deadline check `retry_429`
        # also applies -- a give-up RAISES (SpotifyRetryBudgetExceededError),
        # never returns `[]`, since `[]` reads as a confirmed no-match to every
        # caller and would poison the durable streaming-URL cache with a false
        # negative for a request we never actually completed.
        http = await self._get_client()
        result_key = search_type + "s"  # "albums" or "tracks"

        for attempt in range(self._max_retries):
            token = await self._ensure_token()
            await self._rate_limiter.acquire()

            if quoted:
                query = f'artist:"{artist}" {search_type}:"{term}"'
            else:
                query = f"{artist} {term}"
            resp = await http.get(
                f"{self.API_BASE}/search",
                params={"q": query, "type": search_type, "market": market, "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
            )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "5"))
                exceeds, remaining_budget = self._delay_exceeds_probe_budget(retry_after)
                if exceeds:
                    logger.warning(
                        "Spotify 429, remaining probe budget (%.2fs) can't "
                        "absorb the %ds Retry-After; giving up early instead "
                        "of sleeping past the deadline (attempt %d/%d)",
                        remaining_budget,
                        retry_after,
                        attempt + 1,
                        self._max_retries,
                    )
                    raise SpotifyRetryBudgetExceededError(
                        f"429 Retry-After={retry_after}s exceeds remaining "
                        f"probe budget ({remaining_budget:.2f}s) for {artist} - {term}"
                    )

                hours = retry_after / 3600
                logger.warning(
                    "Spotify 429, waiting %ds (%.1fh) (attempt %d/%d)",
                    retry_after,
                    hours,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(retry_after)
                continue

            if resp.status_code in (500, 502, 503, 504):
                wait = min(2**attempt, 30)
                exceeds, remaining_budget = self._delay_exceeds_probe_budget(wait)
                if exceeds:
                    logger.warning(
                        "Spotify %d, remaining probe budget (%.2fs) can't "
                        "absorb the %ds backoff; giving up early instead of "
                        "sleeping past the deadline (attempt %d/%d)",
                        resp.status_code,
                        remaining_budget,
                        wait,
                        attempt + 1,
                        self._max_retries,
                    )
                    raise SpotifyRetryBudgetExceededError(
                        f"{resp.status_code} backoff={wait}s exceeds remaining "
                        f"probe budget ({remaining_budget:.2f}s) for {artist} - {term}"
                    )

                logger.warning(
                    "Spotify %d, retrying in %ds (attempt %d/%d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(wait)
                continue

            if resp.status_code != 200:
                logger.warning(
                    "Spotify search returned %d for %s - %s",
                    resp.status_code,
                    artist,
                    term,
                )
                return []

            data = resp.json()
            return data.get(result_key, {}).get("items", [])

        logger.error("Spotify max retries exhausted for %s - %s", artist, term)
        return []
