"""YouTube Music album-match client backed by the unofficial ytmusicapi.

Standalone (not a ``BaseStreamingClient`` subclass) because ytmusicapi is a
*synchronous*, ``requests``-based library — the httpx lifecycle the base class
manages does not apply. It still honors the
``find_album_match(artist, title) -> SourceMatch | None`` contract the
streaming-check orchestrator (LML#392) gathers over, so it can later be dropped
into the interactive ``/lookup`` per-service gather (LML#1056 follow-up).

Unauthenticated: ``YTMusic()`` with no OAuth/cookie returns album results
carrying a ``browseId`` (``MPREb_…``) that maps to a direct
``music.youtube.com/browse/<browseId>`` album page — the targeted-link upgrade
the coverage epic (LML#830) is after, established by the go/no-go spike
(LML#833).

Matching routes through the shared production matcher
(``find_best_source_match`` → 80/80 floor via ``is_acceptable_match``), which
scores both axes with ``score_match`` (token_sort). That is inherently immune
to the ``token_set_ratio`` subset inflation that produced the spike's
"Goya"-class false positive, so no bespoke short-title guard is needed here.

Test seam: pass ``search_fn`` to inject a fake searcher; the default lazily
constructs ``YTMusic().search`` so unit tests never import ytmusicapi and never
touch the network.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from aiolimiter import AsyncLimiter

from clients.streaming.matching import find_best_source_match
from streaming.models import SourceMatch

logger = logging.getLogger(__name__)

# ytmusicapi's ``search(query, filter, limit)`` — kept as a narrow injectable
# type so tests can pass any callable of this shape.
SearchFn = Callable[[str, str, int], list[dict[str, Any]]]

_BROWSE_URL_TEMPLATE = "https://music.youtube.com/browse/{browse_id}"
_ALBUMS_FILTER = "albums"


def build_browse_url(browse_id: str) -> str:
    """Map a YouTube Music album ``browseId`` to its direct browse URL.

    >>> build_browse_url("MPREb_abc")
    'https://music.youtube.com/browse/MPREb_abc'
    """
    return _BROWSE_URL_TEMPLATE.format(browse_id=browse_id)


class YouTubeMusicClient:
    """Resolve verified YouTube Music album URLs for ``(artist, title)`` pairs.

    Args:
        search_fn: Optional injected searcher with the ytmusicapi
            ``search(query, filter, limit)`` signature. Defaults to a lazily
            constructed unauthenticated ``YTMusic().search``.
        rate_limit: ``(max_rate, time_period)`` for the internal
            ``AsyncLimiter``. Defaults to 2 requests/second — polite pacing for
            an offline drain, well under any observed rate-limit ceiling
            (the spike saw 0/206 429s at ~1.6 req/s).
        limit: Number of album candidates to request per search.
    """

    DEFAULT_LIMIT = 5

    def __init__(
        self,
        *,
        search_fn: SearchFn | None = None,
        rate_limit: tuple[float, float] = (2, 1),
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self._search_fn = search_fn
        self._rate_limiter = AsyncLimiter(*rate_limit)
        self._limit = limit

    def _get_search_fn(self) -> SearchFn:
        """Return the searcher, lazily constructing the real ytmusicapi client.

        The import is deferred so that unit tests injecting ``search_fn`` never
        need ytmusicapi installed, and the service image never pays the import
        unless the client is actually used.
        """
        if self._search_fn is None:
            from ytmusicapi import YTMusic  # lazy: keep the dep off the test/import path

            yt = YTMusic()

            def search(query: str, filter: str, limit: int) -> list[dict[str, Any]]:  # noqa: A002
                # ytmusicapi types `filter` as a Literal of allowed strings; our
                # injectable SearchFn seam widens it to `str`. The only caller
                # (`search_album`) always passes `_ALBUMS_FILTER` ("albums"), a
                # valid member, so this narrowing is safe. The ignore is inert in
                # CI (ytmusicapi is a `drain` extra, absent under `.[dev]`, so the
                # module is Any) and only bites when the extra is installed.
                return yt.search(query, filter=filter, limit=limit)  # type: ignore[arg-type]

            self._search_fn = search
        return self._search_fn

    async def search_album(self, artist: str, title: str) -> list[dict[str, Any]]:
        """Search YouTube Music for ``(artist, title)`` album candidates.

        Runs the synchronous ytmusicapi call in a worker thread so the coroutine
        never blocks the event loop. Returns candidate rows filtered to those
        carrying both a ``browseId`` and a ``title`` — dropping browse-less rows
        here guarantees a winning candidate can never yield a
        ``.../browse/None`` URL. Returns ``[]`` on any search error (the drain
        treats a raise as a skip, not a fatal).
        """
        query = f"{artist} {title}".strip()
        search = self._get_search_fn()
        await self._rate_limiter.acquire()
        try:
            results = await asyncio.to_thread(search, query, _ALBUMS_FILTER, self._limit)
        except Exception:
            logger.exception("YouTube Music search failed for %s - %s", artist, title)
            return []
        return [r for r in results if r.get("browseId") and r.get("title")]

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Return the best 80/80-clearing album match as a ``SourceMatch``.

        See ``BaseStreamingClient.find_album_match`` for the contract. The
        ytmusicapi response shape (``artists[0].name`` / ``title`` /
        ``browseId``) is encapsulated in the extractor lambdas here.
        """
        return find_best_source_match(
            await self.search_album(artist, title),
            artist,
            title,
            artist_fn=lambda x: x["artists"][0]["name"],
            title_fn=lambda x: x["title"],
            url_fn=lambda x: build_browse_url(x["browseId"]),
            service="youtube_music",
        )
