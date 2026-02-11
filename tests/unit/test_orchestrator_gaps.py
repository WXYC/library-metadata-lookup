"""Tests for uncovered lines in lookup/orchestrator.py."""

from unittest.mock import AsyncMock, patch

import pytest

from library.models import LibraryItem
from lookup.orchestrator import (
    fetch_artwork_for_items,
    filter_results_by_track_validation,
    resolve_albums_for_track,
    search_album_fuzzy,
    search_compilations_for_track,
    search_library_with_fallback,
    search_song_as_artist,
    search_with_alternative_interpretation,
)
from services.parser import ParsedRequest


def _item(id=1, artist="Artist", title="Album", **kwargs):
    defaults = dict(
        call_letters="A", artist_call_number=1, release_call_number=1,
        genre="Rock", format="CD",
    )
    defaults.update(kwargs)
    return LibraryItem(id=id, artist=artist, title=title, **defaults)


# ---------------------------------------------------------------------------
# resolve_albums -- exception path (lines 77-79)
# ---------------------------------------------------------------------------


class TestResolveAlbumsException:
    @pytest.mark.asyncio
    async def test_track_lookup_exception_returns_empty(self):
        """When lookup_releases_by_track raises, return empty list + song_not_found."""
        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody", raw_message="Queen - Bohemian Rhapsody"
        )
        discogs = AsyncMock()

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            side_effect=Exception("network error"),
        ):
            albums, song_not_found = await resolve_albums_for_track(parsed, discogs)

        assert albums == []
        assert song_not_found is True


# ---------------------------------------------------------------------------
# search_with_alternative_interpretation -- both results (lines 128-138)
# ---------------------------------------------------------------------------


class TestAlternativeInterpretationBothResults:
    @pytest.mark.asyncio
    async def test_combines_and_deduplicates(self):
        """When both interpretations match, results are combined and deduplicated."""
        db = AsyncMock()
        item1 = _item(id=1, artist="Foo", title="Bar")
        item2 = _item(id=2, artist="Bar", title="Foo")
        shared = _item(id=3, artist="Foo", title="Shared")

        # First interpretation: "Foo Bar" -> items 1, 3
        # Second interpretation: "Bar Foo" -> items 2, 3
        db.search = AsyncMock(side_effect=[[item1, shared], [item2, shared]])

        results, _ = await search_with_alternative_interpretation(db, "Foo", "Bar")

        ids = [r.id for r in results]
        assert 1 in ids
        assert 2 in ids
        assert 3 in ids
        # No duplicates
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# search_song_as_artist (lines 149-197)
# ---------------------------------------------------------------------------


class TestSearchSongAsArtist:
    @pytest.mark.asyncio
    async def test_direct_artist_match(self):
        """Direct library search with song-as-artist returns results."""
        db = AsyncMock()
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")
        db.search = AsyncMock(return_value=[item])

        results, _ = await search_song_as_artist(db, "Stereolab")
        assert len(results) == 1
        assert results[0].artist == "Stereolab"

    @pytest.mark.asyncio
    async def test_discogs_fallback(self):
        """When direct search fails, looks up Discogs for releases by that artist."""
        db = AsyncMock()
        item = _item(id=2, artist="Stereolab", title="Emperor Tomato Ketchup")

        # Direct search returns nothing; album search finds it
        db.search = AsyncMock(side_effect=[[], [item]])

        discogs = AsyncMock()

        with patch(
            "lookup.orchestrator.lookup_releases_by_artist",
            new_callable=AsyncMock,
            return_value=[("Stereolab", "Emperor Tomato Ketchup")],
        ):
            results, _ = await search_song_as_artist(db, "Stereolab", discogs)

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_discogs_returns_no_releases(self):
        """When Discogs also finds nothing, returns empty."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with patch(
            "lookup.orchestrator.lookup_releases_by_artist",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _ = await search_song_as_artist(db, "UnknownArtist123")

        assert results == []

    @pytest.mark.asyncio
    async def test_compilation_match_via_discogs(self):
        """Discogs cross-reference matches compilation albums."""
        db = AsyncMock()
        comp_item = _item(id=3, artist="Various Artists", title="Indie Comp 2020")

        # Direct search returns nothing; album search finds compilation
        db.search = AsyncMock(side_effect=[[], [comp_item]])

        with patch(
            "lookup.orchestrator.lookup_releases_by_artist",
            new_callable=AsyncMock,
            return_value=[("SomeArtist", "Indie Comp 2020")],
        ):
            results, _ = await search_song_as_artist(db, "SomeArtist")

        assert len(results) == 1
        assert results[0].artist == "Various Artists"

    @pytest.mark.asyncio
    async def test_skips_empty_album_title(self):
        """Discogs releases with empty album titles are skipped."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        with patch(
            "lookup.orchestrator.lookup_releases_by_artist",
            new_callable=AsyncMock,
            return_value=[("Artist", ""), ("Artist", None)],
        ):
            results, _ = await search_song_as_artist(db, "Artist")

        assert results == []


# ---------------------------------------------------------------------------
# search_library_with_fallback -- artist+song path (lines 260-265)
# ---------------------------------------------------------------------------


class TestSearchLibraryWithFallbackSongPath:
    @pytest.mark.asyncio
    async def test_artist_plus_song_fallback(self):
        """When albums produce no results, tries artist+song and sorts by song match."""
        db = AsyncMock()
        item1 = _item(id=1, artist="Queen", title="Greatest Hits")
        item2 = _item(id=2, artist="Queen", title="Bohemian Rhapsody Single")

        # No album results (first call), artist+song results (second call)
        db.search = AsyncMock(side_effect=[[], [item1, item2]])

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody", raw_message="Queen - Bohemian Rhapsody"
        )

        results, song_not_found = await search_library_with_fallback(
            db, parsed, albums=["Nonexistent Album"]
        )

        assert len(results) >= 1
        assert song_not_found is True
        # Item with song in title should be sorted first
        assert "Bohemian Rhapsody" in results[0].title


# ---------------------------------------------------------------------------
# search_compilations_for_track (lines 284-392)
# ---------------------------------------------------------------------------


class TestSearchCompilationsForTrack:
    @pytest.mark.asyncio
    async def test_no_song_returns_empty(self):
        db = AsyncMock()
        parsed = ParsedRequest(artist="Queen", raw_message="Queen")
        results, titles = await search_compilations_for_track(db, parsed)
        assert results == []
        assert titles == {}

    @pytest.mark.asyncio
    async def test_no_artist_returns_empty(self):
        db = AsyncMock()
        parsed = ParsedRequest(song="Bohemian Rhapsody", raw_message="Bohemian Rhapsody")
        results, titles = await search_compilations_for_track(db, parsed)
        assert results == []

    @pytest.mark.asyncio
    async def test_keyword_search_with_compilation_filter(self):
        """Keyword search returns results filtered by artist or compilation."""
        db = AsyncMock()
        comp = _item(id=1, artist="Various Artists", title="Rock Hits")
        match = _item(id=2, artist="Queen", title="Best of Queen")

        # keyword search returns both; discogs returns empty
        db.search = AsyncMock(return_value=[comp, match])

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody",
            raw_message="Queen - Bohemian Rhapsody",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        # Should use keyword matches as fallback
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_discogs_cross_reference(self):
        """Finds track on a compilation via Discogs cross-reference."""
        db = AsyncMock()
        comp = _item(id=1, artist="Various Artists", title="Rock Classics")

        # First call: keyword search (no results)
        # Second call: search for "Rock Classics" album
        db.search = AsyncMock(side_effect=[[], [comp]])

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody",
            raw_message="Queen - Bohemian Rhapsody",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Various Artists", "Rock Classics")],
        ):
            results, discogs_titles = await search_compilations_for_track(db, parsed)

        assert len(results) == 1
        assert results[0].id == 1
        assert 1 in discogs_titles

    @pytest.mark.asyncio
    async def test_remix_detection(self):
        """Detects remix info in raw message and uses it for search."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        parsed = ParsedRequest(
            artist="Depeche Mode", song="Enjoy the Silence",
            raw_message="Depeche Mode - Enjoy the Silence (Timo Maas Remix)",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_track_lookup:
            await search_compilations_for_track(db, parsed)

        # Should have searched with remix info
        call_args = mock_track_lookup.call_args
        assert "remix" in call_args[0][0].lower() or "Remix" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_skips_artist_named_albums(self):
        """Skips Discogs releases where album name matches artist name."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody",
            raw_message="Queen - Bohemian Rhapsody",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Queen", "Queen")],  # album name == artist name
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        assert results == []

    @pytest.mark.asyncio
    async def test_skips_short_album_names(self):
        """Skips Discogs releases with very short album names."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody",
            raw_message="Queen - Bohemian Rhapsody",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Queen", "XY")],  # too short
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        assert results == []

    @pytest.mark.asyncio
    async def test_compilation_artist_filter(self):
        """Discogs compilation artist + library compilation artist both pass filter."""
        db = AsyncMock()
        comp = _item(id=1, artist="Various Artists", title="Rock Comp")

        # keyword: no results; album search: compilation item
        db.search = AsyncMock(side_effect=[[], [comp]])

        parsed = ParsedRequest(
            artist="Queen", song="We Will Rock You",
            raw_message="Queen - We Will Rock You",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Various Artists", "Rock Comp")],
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_max_results_break(self):
        """Stops collecting once MAX_SEARCH_RESULTS reached."""
        db = AsyncMock()
        items = [_item(id=i, artist="Various Artists", title=f"Comp {i}") for i in range(30)]

        # keyword: no results; each album search returns items
        db.search = AsyncMock(side_effect=[[]] + [[item] for item in items])

        releases = [("Various Artists", f"Comp {i}") for i in range(30)]

        parsed = ParsedRequest(
            artist="Queen", song="Song",
            raw_message="Queen - Song",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=releases,
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        # Should be capped
        assert len(results) <= 10

    @pytest.mark.asyncio
    async def test_discogs_exception_falls_back_to_keyword(self):
        """When Discogs search raises, falls back to keyword matches."""
        db = AsyncMock()
        keyword_item = _item(id=1, artist="Queen", title="Best Hits")

        # keyword search succeeds
        db.search = AsyncMock(return_value=[keyword_item])

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody",
            raw_message="Queen - Bohemian Rhapsody",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            side_effect=Exception("Discogs down"),
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        # Should fall back to keyword matches
        assert len(results) >= 1


# ---------------------------------------------------------------------------
# search_album_fuzzy (lines 411-444)
# ---------------------------------------------------------------------------


class TestSearchAlbumFuzzy:
    @pytest.mark.asyncio
    async def test_exact_match(self):
        """Exact match returns results directly."""
        db = AsyncMock()
        item = _item(id=1, title="OK Computer")
        db.search = AsyncMock(return_value=[item])

        results = await search_album_fuzzy(db, "OK Computer")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_fuzzy_fallback(self):
        """When exact match fails, tries fuzzy keyword search."""
        db = AsyncMock()
        item = _item(id=1, title="The Very Best Greatest Hits Collection")

        # First search: no results. Second search: fuzzy match found.
        db.search = AsyncMock(side_effect=[[], [item]])

        results = await search_album_fuzzy(db, "Greatest Hits Collection Volume")
        # Should find it via fuzzy matching
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_fuzzy_threshold_filters(self):
        """Fuzzy results below threshold are filtered out."""
        db = AsyncMock()
        item = _item(id=1, title="Completely Different Title")

        # Exact: empty, fuzzy: returns unrelated item
        db.search = AsyncMock(side_effect=[[], [item]])

        results = await search_album_fuzzy(db, "Greatest Hits Collection Volume")
        # Should be filtered out due to low similarity
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_no_significant_words(self):
        """Short album title with no significant words skips fuzzy search."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        results = await search_album_fuzzy(db, "The")
        assert results == []


# ---------------------------------------------------------------------------
# filter_results_by_track_validation -- exception (lines 482-483)
# ---------------------------------------------------------------------------


class TestTrackValidationException:
    @pytest.mark.asyncio
    async def test_validation_exception_skips_item(self):
        """When validation raises for one item, that item is skipped."""
        item1 = _item(id=1, title="Album1")
        item2 = _item(id=2, title="Album2")

        discogs = AsyncMock()
        from discogs.models import DiscogsSearchResponse, DiscogsSearchResult
        discogs.search = AsyncMock(
            side_effect=[
                Exception("timeout"),
                DiscogsSearchResponse(
                    results=[
                        DiscogsSearchResult(
                            album="Album2", artist="Artist",
                            release_id=2, release_url="https://discogs.com/release/2",
                        )
                    ],
                    total=1,
                ),
            ]
        )
        discogs.validate_track_on_release = AsyncMock(return_value=True)

        result = await filter_results_by_track_validation(
            [item1, item2], "Song", "Artist", discogs
        )
        assert result is not None
        assert len(result) == 1
        assert result[0].id == 2


# ---------------------------------------------------------------------------
# fetch_artwork_for_items -- exception (lines 525-527)
# ---------------------------------------------------------------------------


class TestFetchArtworkException:
    @pytest.mark.asyncio
    async def test_artwork_exception_returns_none(self):
        """When artwork fetch raises for one item, returns None for that item."""
        item1 = _item(id=1, title="Album1")
        item2 = _item(id=2, title="Album2")

        discogs = AsyncMock()
        from discogs.models import DiscogsSearchResponse, DiscogsSearchResult
        discogs.search = AsyncMock(
            side_effect=[
                Exception("timeout"),
                DiscogsSearchResponse(
                    results=[
                        DiscogsSearchResult(
                            album="Album2", artist="Artist",
                            release_id=2, release_url="https://discogs.com/release/2",
                        )
                    ],
                    total=1,
                ),
            ]
        )

        results = await fetch_artwork_for_items([item1, item2], discogs)
        assert len(results) == 2
        # First one should have None artwork
        assert results[0][1] is None
        # Second one should have artwork
        assert results[1][1] is not None
