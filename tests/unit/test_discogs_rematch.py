"""Tests for Discogs re-matching logic."""

from unittest.mock import AsyncMock

import pytest

from scripts.discogs_rematch import (
    pass1_title_search,
    pass2_fuzzy_artist_search,
)


def _make_row(
    album_id=1,
    display_artist="Sharon Jones and the Dap-Kings",
    display_title="Got to Be the Way It Is",
    discogs_artist="Sharon Jones & The Dap-Kings",
    discogs_title=None,
    discogs_release_id=None,
):
    return {
        "id": album_id,
        "display_artist": display_artist,
        "display_title": display_title,
        "discogs_artist": discogs_artist,
        "discogs_title": discogs_title,
        "discogs_release_id": discogs_release_id,
    }


class TestPass1TitleSearch:
    """Pass 1: title-only search for enriched-but-unmatched albums."""

    @pytest.mark.asyncio
    async def test_exact_title_match(self):
        pool = AsyncMock()
        pool.fetch.return_value = [
            {
                "id": 99999,
                "title": "Got To Be The Way It Is",
                "artist_name": "Sharon Jones & The Dap-Kings",
            }
        ]
        row = _make_row()
        result = await pass1_title_search(pool, row)
        assert result is not None
        assert result["release_id"] == 99999

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self):
        pool = AsyncMock()
        pool.fetch.return_value = []
        row = _make_row()
        result = await pass1_title_search(pool, row)
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_wrong_artist(self):
        """Title matches but artist is completely different → reject."""
        pool = AsyncMock()
        pool.fetch.return_value = [
            {
                "id": 11111,
                "title": "Got To Be The Way It Is",
                "artist_name": "Autechre",
            }
        ]
        row = _make_row()
        result = await pass1_title_search(pool, row)
        assert result is None

    @pytest.mark.asyncio
    async def test_relaxed_artist_threshold(self):
        """Artist credit variation should pass with relaxed threshold (70)."""
        pool = AsyncMock()
        pool.fetch.return_value = [
            {
                "id": 22222,
                "title": "Got To Be The Way It Is",
                "artist_name": "Sharon Jones And The Dap Kings",
            }
        ]
        row = _make_row()
        result = await pass1_title_search(pool, row)
        assert result is not None

    @pytest.mark.asyncio
    async def test_strips_format_suffix_from_title(self):
        """Format suffix like '12"' should be stripped before PG query."""
        pool = AsyncMock()
        pool.fetch.return_value = [
            {
                "id": 33333,
                "title": "Catch A Bad One",
                "artist_name": "Busta Rhymes",
            }
        ]
        row = _make_row(
            display_artist="Busta Rhymes",
            display_title='Catch a Bad One 12"',
            discogs_artist="Busta Rhymes",
        )
        result = await pass1_title_search(pool, row)
        assert result is not None

    @pytest.mark.asyncio
    async def test_completely_different_artist_rejected(self):
        """'Del' vs 'Del Tha Funkee Homosapien' — too different, no fuzzy match."""
        pool = AsyncMock()
        pool.fetch.return_value = [
            {
                "id": 33333,
                "title": "Catch A Bad One",
                "artist_name": "Del Tha Funkee Homosapien",
            }
        ]
        row = _make_row(
            display_artist="Del",
            display_title="Catch a Bad One",
            discogs_artist="Deltron 3030",
        )
        result = await pass1_title_search(pool, row)
        assert result is None


class TestPass2FuzzyArtistSearch:
    """Pass 2: fuzzy artist + title search for not-enriched albums."""

    @pytest.mark.asyncio
    async def test_fuzzy_artist_match(self):
        pool = AsyncMock()
        # First query: trigram artist search
        pool.fetch.side_effect = [
            # Artist search results
            [{"artist_name": "Sharon Jones & The Dap-Kings"}],
            # Release search for that artist
            [
                {
                    "id": 44444,
                    "title": "Got To Be The Way It Is",
                    "artist_name": "Sharon Jones & The Dap-Kings",
                }
            ],
        ]
        row = _make_row(discogs_artist=None)
        result = await pass2_fuzzy_artist_search(pool, row)
        assert result is not None
        assert result["release_id"] == 44444

    @pytest.mark.asyncio
    async def test_no_artist_match_returns_none(self):
        pool = AsyncMock()
        pool.fetch.return_value = []
        row = _make_row(display_artist="Completely Unknown Artist", discogs_artist=None)
        result = await pass2_fuzzy_artist_search(pool, row)
        assert result is None
