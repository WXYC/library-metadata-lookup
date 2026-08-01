"""Unit tests for clients/streaming/youtube_music.py (LML#1056).

The client is backed by the unofficial, synchronous ``ytmusicapi``, which is
never imported here: every test injects a fake ``search_fn``, so these run
without the ytmusicapi dependency and touch no network. This mirrors the
``client._http = mock`` seam the httpx-based clients use, adapted for a
sync library.
"""

from __future__ import annotations

import pytest

from clients.streaming.youtube_music import YouTubeMusicClient, build_browse_url
from streaming.models import SourceMatch


def _album(
    title: str = "Aluminum Tunes",
    artist: str = "Stereolab",
    browse_id: str | None = "MPREb_stereolab",
    year: str = "1998",
) -> dict:
    """A ytmusicapi ``search(filter='albums')`` result row, per the #833 spike."""
    return {
        "title": title,
        "artists": [{"name": artist, "id": "UCxxx"}],
        "browseId": browse_id,
        "year": year,
        "resultType": "album",
    }


def _fake_search(results: list[dict]):
    """Fake for ``YTMusic().search(query, filter, limit)`` — ignores the query,
    returns a fixed candidate list truncated to ``limit``."""

    def search(query: str, filter: str, limit: int) -> list[dict]:  # noqa: A002 (ytm arg name)
        return list(results)[:limit]

    return search


class TestBuildBrowseUrl:
    def test_builds_direct_music_youtube_browse_url(self):
        assert build_browse_url("MPREb_abc") == "https://music.youtube.com/browse/MPREb_abc"


class TestFindAlbumMatch:
    @pytest.mark.asyncio
    async def test_returns_source_match_for_clearing_candidate(self):
        client = YouTubeMusicClient(search_fn=_fake_search([_album()]))
        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert isinstance(match, SourceMatch)
        assert match.url == "https://music.youtube.com/browse/MPREb_stereolab"
        assert match.confidence >= 80.0

    @pytest.mark.asyncio
    async def test_returns_none_when_no_results(self):
        client = YouTubeMusicClient(search_fn=_fake_search([]))
        assert await client.find_album_match("Juana Molina", "DOGA") is None

    @pytest.mark.asyncio
    async def test_rejects_wrong_album_below_floor(self):
        # Correct artist, unrelated album -> the title axis fails the 80 floor.
        client = YouTubeMusicClient(
            search_fn=_fake_search(
                [_album(title="Some Completely Unrelated Record", artist="Stereolab")]
            )
        )
        assert await client.find_album_match("Stereolab", "Aluminum Tunes") is None

    @pytest.mark.asyncio
    async def test_rejects_subset_inflation_false_positive(self):
        # Goya-class artifact (spike LML#833): a short query title/artist buried
        # inside an unrelated candidate must not clear. The production matcher
        # scores both axes with score_match (token_sort), not token_set, so the
        # subset inflation that produced the raw-pass FP is rejected here.
        client = YouTubeMusicClient(
            search_fn=_fake_search([_album(artist="Chantal Goya", title="Club Grenadine Sessions")])
        )
        assert await client.find_album_match("Grenadine", "Goya") is None

    @pytest.mark.asyncio
    async def test_skips_candidate_without_browse_id(self):
        # A candidate missing browseId must never win and yield ".../browse/None".
        client = YouTubeMusicClient(
            search_fn=_fake_search([_album(browse_id=None), _album(browse_id="MPREb_good")])
        )
        match = await client.find_album_match("Stereolab", "Aluminum Tunes")
        assert match is not None
        assert match.url.endswith("MPREb_good")


class TestSearchAlbum:
    @pytest.mark.asyncio
    async def test_drops_rows_without_browse_id(self):
        client = YouTubeMusicClient(
            search_fn=_fake_search([_album(browse_id=None), _album(browse_id="MPREb_ok")])
        )
        rows = await client.search_album("Stereolab", "Aluminum Tunes")
        assert [r["browseId"] for r in rows] == ["MPREb_ok"]

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_search_raises(self):
        def boom(query: str, filter: str, limit: int) -> list[dict]:  # noqa: A002
            raise RuntimeError("ytmusicapi transport error")

        client = YouTubeMusicClient(search_fn=boom)
        assert await client.search_album("Stereolab", "Aluminum Tunes") == []
