"""Tests for uncovered lines in lookup/orchestrator.py."""

from unittest.mock import AsyncMock, patch

import pytest

from lookup.orchestrator import (
    album_title_acceptable,
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
from tests.factories import make_discogs_result
from tests.factories import make_library_item as _item

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
    async def test_artist_plus_song_fallback_when_no_discogs_albums(self):
        """When Discogs found no albums (empty list), falls back to artist+song."""
        db = AsyncMock()
        item1 = _item(id=1, artist="Queen", title="Greatest Hits")
        item2 = _item(id=2, artist="Queen", title="Bohemian Rhapsody Single")

        # artist+song results (first call)
        db.search = AsyncMock(side_effect=[[item1, item2]])

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody", raw_message="Queen - Bohemian Rhapsody"
        )

        results, song_not_found = await search_library_with_fallback(db, parsed, albums=[])

        assert len(results) >= 1
        assert song_not_found is True
        # Item with song in title should be sorted first
        assert "Bohemian Rhapsody" in results[0].title

    @pytest.mark.asyncio
    async def test_falls_through_to_artist_search_when_discogs_albums_not_in_library(self):
        """When Discogs found specific albums but none matched, fall through to artist search.

        filter_results_by_track_validation() (called downstream by perform_lookup)
        handles rejecting false positives from the fallback results.
        """
        db = AsyncMock()
        item = _item(id=1, artist="Queen", title="Greatest Hits")

        db.search = AsyncMock(
            side_effect=[
                [],  # album search: no match
                [item],  # artist+song fallback
            ]
        )

        parsed = ParsedRequest(
            artist="Queen", song="Bohemian Rhapsody", raw_message="Queen - Bohemian Rhapsody"
        )

        results, song_not_found = await search_library_with_fallback(
            db, parsed, albums=["Nonexistent Album"]
        )

        assert len(results) == 1
        assert results[0].title == "Greatest Hits"
        assert song_not_found is True


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
            artist="Queen",
            song="Bohemian Rhapsody",
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
            artist="Queen",
            song="Bohemian Rhapsody",
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
            artist="Depeche Mode",
            song="Enjoy the Silence",
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
            artist="Queen",
            song="Bohemian Rhapsody",
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
            artist="Queen",
            song="Bohemian Rhapsody",
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
            artist="Queen",
            song="We Will Rock You",
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
    async def test_rejects_similar_but_different_compilation(self):
        """Discogs compilation with similar name but different series is rejected.

        "Get On The Dance Floor Volume 5" (Discogs, contains the track) should
        NOT match library item "Explorations on the Dancefloor vol. 3" (different
        compilation series). fuzz.ratio = 73.5, below the 80 threshold.
        """
        db = AsyncMock()
        wrong_item = _item(
            id=3288,
            artist="Various Artists - Hiphop",
            title="Explorations on the Dancefloor vol. 3",
        )

        # keyword: no results; album search: returns wrong item
        db.search = AsyncMock(side_effect=[[], [wrong_item]])

        parsed = ParsedRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Various Artists", "Get On The Dance Floor Volume 5")],
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_max_results_break(self):
        """Stops collecting once MAX_SEARCH_RESULTS reached."""
        db = AsyncMock()
        items_by_title = {
            f"Comp {i}": _item(id=i, artist="Various Artists", title=f"Comp {i}") for i in range(30)
        }

        # Route by query content: return the matching item for each "Comp N" query.
        # With asyncio.gather, album searches run concurrently so we can't
        # rely on sequential side_effect ordering.
        _keyword_search_done = False

        async def route_search(query, **kwargs):
            nonlocal _keyword_search_done
            if not _keyword_search_done:
                _keyword_search_done = True
                return []
            for title, item in items_by_title.items():
                if title.lower() in query.lower():
                    return [item]
            return []

        db.search = AsyncMock(side_effect=route_search)

        releases = [("Various Artists", f"Comp {i}") for i in range(30)]

        parsed = ParsedRequest(
            artist="Queen",
            song="Song",
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
            artist="Queen",
            song="Bohemian Rhapsody",
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

    @pytest.mark.asyncio
    async def test_no_keyword_fallback_when_discogs_found_releases(self):
        """When Discogs finds releases but none match in library, don't use keyword fallback.

        Bug: "flow coma by 808 state" — keyword search finds "808 State" via
        fuzzy matching, Discogs finds "The Best Of 808 State: Blueprint", but
        search_album_fuzzy correctly rejects "808 State" for that album.
        The keyword fallback then returns "808 State" anyway — a false positive.
        """
        db = AsyncMock()
        false_positive = _item(id=958, artist="808 State", title="808 State")

        # First call (keyword search): returns the false positive
        # Second call (search_album_fuzzy for "The Best Of 808 State: Blueprint"): empty
        db.search = AsyncMock(side_effect=[[false_positive], []])

        parsed = ParsedRequest(
            artist="808 State",
            song="Flow Coma",
            raw_message="flow coma by 808 state",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("808 State", "The Best Of 808 State: Blueprint")],
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        # Should NOT fall back to keyword matches — Discogs found releases
        assert results == [], (
            "Should not return '808 State' as keyword fallback when Discogs found specific "
            "releases that didn't match in library"
        )

    @pytest.mark.asyncio
    async def test_rejects_wrong_album_in_numbered_series(self):
        """search_compilations_for_track should not match Chicago V for Chicago 16.

        Bug: "Hard to Say I'm Sorry - Chicago" — Discogs correctly identifies
        the track on "Chicago 16", but the library doesn't have Chicago 16.
        FTS5 returns "Chicago V" (shares the word "Chicago"), and fuzzy matching
        incorrectly accepts it because the fuzz.ratio is high (~84%).
        """
        db = AsyncMock()
        chicago_v = _item(id=100, artist="Chicago", title="Chicago V")

        # Keyword search returns Chicago V; search_album_fuzzy also returns it
        db.search = AsyncMock(return_value=[chicago_v])

        parsed = ParsedRequest(
            artist="Chicago",
            song="Hard to Say I'm Sorry",
            raw_message="Hard to Say Im Sorry - Chicago",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Chicago", "Chicago 16")],
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        assert results == [], (
            "Should not match 'Chicago V' when Discogs album is 'Chicago 16' — "
            "these are different albums in a numbered series"
        )

    @pytest.mark.asyncio
    async def test_quoted_artist_prevents_keyword_fallback(self):
        """Quoted Discogs artist names should not cause keyword fallback.

        Bug: 'Bob by Weird Al Yankovic' returned the self-titled album because
        Discogs formats the artist as '"Weird Al" Yankovic' (with quotes), which
        broke validation, causing all validated releases to be rejected. With no
        Discogs results, the keyword fallback fired and returned the self-titled
        album without track validation.

        Fix: validate_track_on_release now strips quotes. Validation passes for
        legitimate releases, discogs_found_releases is True, and the keyword
        fallback does not fire.
        """
        db = AsyncMock()
        self_titled = _item(id=1, artist="Weird Al Yankovic", title="Weird Al Yankovic")
        db.search = AsyncMock(return_value=[self_titled])

        parsed = ParsedRequest(
            artist="Weird Al Yankovic",
            song="Bob",
            raw_message='Bob by Weird "Al" Yankovich',
        )

        # Discogs found Poodle Hat (which contains "Bob"), but it's not in the library
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[('"Weird Al" Yankovic', "Poodle Hat")],
        ):
            results, _ = await search_compilations_for_track(db, parsed)

        assert results == [], (
            "Should not return self-titled album via keyword fallback when Discogs "
            "found valid releases (Poodle Hat) that simply aren't in the library"
        )

    @pytest.mark.asyncio
    async def test_validates_only_library_matching_releases(self):
        """Only calls validate_track_on_release for releases found in the library.

        When Discogs returns 10 releases but only 1 matches the library, we should
        make 1 validation API call (not 10). This tests the library-first approach
        where we search the library before validating each Discogs release.
        """
        db = AsyncMock()
        comp = _item(
            id=46602,
            artist="Various Artists",
            title="Trax Records 20th Anniversary Collection",
        )

        # Route by query content: only "Trax Records" matches the library.
        # With asyncio.gather, album searches run concurrently so we can't
        # rely on sequential side_effect ordering.
        _keyword_search_done = False

        async def route_search(query, **kwargs):
            nonlocal _keyword_search_done
            if not _keyword_search_done:
                _keyword_search_done = True
                return []
            if "trax" in query.lower():
                return [comp]
            return []

        db.search = AsyncMock(side_effect=route_search)

        mock_discogs = AsyncMock()
        from discogs.models import ReleaseInfo, TrackReleasesResponse

        releases = [
            ReleaseInfo(
                album="Trax Records: The 20th Anniversary Edition",
                artist="Various Artists",
                release_id=1001,
                release_url="https://www.discogs.com/release/1001",
                is_compilation=True,
            ),
        ] + [
            ReleaseInfo(
                album=f"Album {i}",
                artist="Various Artists",
                release_id=1000 + i,
                release_url=f"https://www.discogs.com/release/{1000 + i}",
                is_compilation=True,
            )
            for i in range(2, 6)
        ]

        mock_discogs.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="No Way Back",
                artist="Adonis",
                releases=releases,
                total=len(releases),
            )
        )
        mock_discogs.validate_track_on_release = AsyncMock(return_value=True)

        parsed = ParsedRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        results, discogs_titles = await search_compilations_for_track(db, parsed, mock_discogs)

        assert len(results) == 1
        assert results[0].id == 46602
        # Key assertion: validate was called only for the library-matching release
        assert mock_discogs.validate_track_on_release.call_count == 1
        assert mock_discogs.validate_track_on_release.call_args[0] == (
            1001,
            "No Way Back",
            "Adonis",
        )


# ---------------------------------------------------------------------------
# search_album_fuzzy (lines 411-444)
# ---------------------------------------------------------------------------


class TestAlbumTitleAcceptable:
    """Tests for album_title_acceptable: rejects numbered series albums that share only a prefix."""

    def test_rejects_chicago_v_for_chicago_16(self):
        """Chicago V and Chicago 16 are different albums in a numbered series."""
        assert album_title_acceptable("chicago 16", "chicago v") is False

    def test_rejects_chicago_ix_for_chicago_16(self):
        """Chicago IX and Chicago 16 are different albums in a numbered series."""
        assert album_title_acceptable("chicago 16", "chicago ix") is False

    def test_rejects_led_zeppelin_ii_for_led_zeppelin_iv(self):
        """Led Zeppelin II and Led Zeppelin IV are different albums."""
        assert album_title_acceptable("led zeppelin iv", "led zeppelin ii") is False

    def test_accepts_exact_match(self):
        assert album_title_acceptable("chicago 16", "chicago 16") is True

    def test_accepts_prefix_match(self):
        assert album_title_acceptable("abbey road", "abbey road remastered") is True

    def test_accepts_spelling_variation(self):
        """Rumours vs Rumors should match (spelling variation, not series)."""
        assert album_title_acceptable("rumours", "rumors") is True

    def test_accepts_high_similarity(self):
        """Dark Side of the Moon with/without 'The' should match."""
        assert album_title_acceptable("dark side of the moon", "the dark side of the moon") is True


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

        # Exact: empty; all fuzzy attempts return unrelated item (filtered each time)
        db.search = AsyncMock(side_effect=[[], [item], [item], [item]])

        results = await search_album_fuzzy(db, "Greatest Hits Collection Volume")
        # Should be filtered out due to low keyword match count
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_no_significant_words(self):
        """Short album title with no significant words skips fuzzy search."""
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        results = await search_album_fuzzy(db, "The")
        assert results == []

    @pytest.mark.asyncio
    async def test_drops_mismatched_word_and_retries(self):
        """When all significant words fail, progressively shorter queries are tried.

        Discogs may list an album as "Trax Records: The 20th Anniversary Edition"
        while the library has "Trax Records 20th Anniversary Collection". The word
        "edition" doesn't appear in the library title, so the 4-word query fails.
        Dropping to 3 words ("trax 20th anniversary") should find it.
        """
        db = AsyncMock()
        item = _item(id=46602, title="Trax Records 20th Anniversary Collection")

        # First call: exact match (full title) -> empty
        # Second call: 4-word fuzzy "trax 20th anniversary edition" -> empty
        # Third call: 3-word fuzzy "trax 20th anniversary" -> finds item
        db.search = AsyncMock(side_effect=[[], [], [item]])

        results = await search_album_fuzzy(db, "Trax Records: The 20th Anniversary Edition")
        assert len(results) == 1
        assert results[0].id == 46602

    @pytest.mark.asyncio
    async def test_matches_abbreviated_compilation_title(self):
        """Library may abbreviate titles differently from Discogs.

        Discogs: "Não Wave - Brazilian Post Punk 1982 - 1988"
        Library: "Nao Wave- Brazilian Punk 82-88"
        Only 3/6 significant words match ("wave", "brazilian", "punk") but
        token_set_ratio is 77% — the keyword threshold should be lenient
        enough to accept this.
        """
        db = AsyncMock()
        item = _item(id=57500, title="Nao Wave- Brazilian Punk 82-88")

        # Call 1: exact FTS5 search -> empty (diacritics mismatch)
        # Call 2: 4-word fuzzy "wave brazilian post punk" -> empty ("post" not in title)
        # Call 3: 3-word fuzzy "wave brazilian post" -> empty
        # Call 4: 2-word fuzzy "wave brazilian" -> finds item
        db.search = AsyncMock(side_effect=[[], [], [], [item]])

        results = await search_album_fuzzy(db, "Não Wave - Brazilian Post Punk 1982 - 1988")
        assert len(results) == 1
        assert results[0].id == 57500

    @pytest.mark.asyncio
    async def test_matches_va_series_with_volume_against_long_subtitle(self):
        """V/A library row catalogued as ``<base>, vol. N`` surfaces against a
        Discogs release with a long parenthetical subtitle (WXYC#531).

        Library row 58610 catalogues ``Disco Not Disco, vol. 1`` under artist
        ``Various Artists - Rock - D``. Discogs returns the canonical release as
        ``Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics
        1974-1986)``. The base ``Disco Not Disco`` matches as a prefix on the
        Discogs side, and the library suffix is the recognised ``, vol. N``
        series identifier, gated on ``is_compilation_artist`` so non-V/A albums
        are not grandfathered through this looser path.
        """
        db = AsyncMock()
        item = _item(
            id=58610,
            artist="Various Artists - Rock - D",
            title="Disco Not Disco, vol. 1",
            call_letters="V",
        )

        # FTS5 returns the V/A row on shared tokens ("disco", "not"). The new
        # vol.-N gate must accept it instead of falling through to the
        # length-sensitive fuzz.ratio reject in album_title_acceptable.
        db.search = AsyncMock(return_value=[item])

        results = await search_album_fuzzy(
            db,
            "Disco Not Disco (Post Punk, Electro & Leftfield Disco Classics 1974-1986)",
        )
        assert len(results) == 1
        assert results[0].id == 58610

    @pytest.mark.asyncio
    async def test_va_series_gate_does_not_grandfather_non_compilation(self):
        """The ``, vol. N`` special-case is gated on ``is_compilation_artist``.

        A non-V/A library row with the same shape (``<base>, vol. N``) must NOT
        be accepted against an unrelated Discogs release whose title merely
        starts with the same ``<base>``. This is the regression guard for
        directions (2) and (3) in WXYC#531 — option (1) is deliberately
        narrow.
        """
        db = AsyncMock()
        item = _item(
            id=99999,
            artist="Some Band",
            title="Live Sessions, vol. 2",
        )

        db.search = AsyncMock(return_value=[item])

        # Long descriptive Discogs title that shares the "Live Sessions" prefix
        # but is otherwise unrelated. Without the V/A gate this would slip
        # through the new special-case; with the gate it must be rejected by
        # the existing length-sensitive fuzz.ratio path.
        results = await search_album_fuzzy(
            db,
            "Live Sessions (Acoustic Recordings From The Greek Theatre 1998-2002)",
        )
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
        from discogs.models import DiscogsSearchResponse

        discogs.search = AsyncMock(
            side_effect=[
                Exception("timeout"),
                DiscogsSearchResponse(
                    results=[
                        make_discogs_result(
                            release_id=2,
                            album="Album2",
                            artist="Artist",
                        )
                    ],
                    total=1,
                ),
            ]
        )
        discogs.validate_track_on_release = AsyncMock(return_value=True)

        result = await filter_results_by_track_validation([item1, item2], "Song", "Artist", discogs)
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
        from discogs.models import DiscogsSearchResponse

        discogs.search = AsyncMock(
            side_effect=[
                Exception("timeout"),
                DiscogsSearchResponse(
                    results=[
                        make_discogs_result(
                            release_id=2,
                            album="Album2",
                            artist="Stereolab",
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
