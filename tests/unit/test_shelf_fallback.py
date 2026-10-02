"""Unit + perform_lookup-level tests for the unbound shelf fallback (LML#1391/#1393).

Re-scope of the original LML#1391 fix (PR #1396 closed after five review
rounds, each finding a new way that attempt changed what Backend-Service
binds). The invariant under test throughout this file: **a response that is
non-empty on ``main`` stays byte-for-byte unchanged. Only a response whose
result list is empty today may change.**

``TestOrderShelfRows`` and ``TestApplyShelfFallback`` test
``lookup/shelf_fallback.py`` directly, in isolation. The
``TestPerformLookup*`` classes drive the real pipeline end to end, mocking
only ``LibraryDB``/``DiscogsService`` -- the same pattern ``test_orchestrator.py``
and ``test_library_miss_discogs.py`` already use.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.search import SEARCH_TYPE_FALLBACK, SearchState, SearchStrategyType
from discogs.models import DiscogsSearchResponse, TrackReleasesResponse
from lookup.matching import MAX_SEARCH_RESULTS
from lookup.miss_kind import MISS_CLEAN, derive_miss_kind
from lookup.models import LookupRequest, LookupResultItem
from lookup.orchestrator import perform_lookup
from lookup.shelf_fallback import _order_shelf_rows, _rows_by_artist, apply_shelf_fallback
from services.parser import MessageType, ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_item, shelve

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry():
    return make_lml_telemetry()


def _parsed(**overrides):
    defaults = {
        "artist": "Jessica Pratt",
        "album": "Zzyzx Road",
        "song": None,
        "message_type": MessageType.REQUEST,
        "is_request": True,
    }
    defaults.update(overrides)
    return ParsedRequest(**defaults)


# ---------------------------------------------------------------------------
# _order_shelf_rows -- pure ordering helper
# ---------------------------------------------------------------------------


class TestOrderShelfRows:
    def test_song_in_title_leads(self):
        unrelated = make_library_item(id=1, artist="Jessica Pratt", title="Unrelated Album")
        matching = make_library_item(id=2, artist="Jessica Pratt", title="Zzyzx Road Sessions")

        ordered = _order_shelf_rows([unrelated, matching], "Zzyzx Road")

        assert [row.id for row in ordered] == [2, 1]

    def test_no_song_keeps_query_order(self):
        first = make_library_item(id=1, artist="Jessica Pratt", title="A")
        second = make_library_item(id=2, artist="Jessica Pratt", title="B")

        assert _order_shelf_rows([first, second], None) == [first, second]

    def test_no_title_match_keeps_query_order(self):
        first = make_library_item(id=1, artist="Jessica Pratt", title="A")
        second = make_library_item(id=2, artist="Jessica Pratt", title="B")

        ordered = _order_shelf_rows([first, second], "Nothing Like Either Title")

        assert [row.id for row in ordered] == [1, 2]


# ---------------------------------------------------------------------------
# _rows_by_artist -- the normalized-equality guard
# ---------------------------------------------------------------------------


class TestRowsByArtist:
    @pytest.mark.parametrize("artist", ["", "   ", "\t\n"], ids=["empty", "spaces", "tab-newline"])
    def test_artist_normalizing_to_empty_returns_no_rows(self, artist):
        """A query artist that normalizes to the empty string (blank,
        whitespace-only) must not match rows whose artist *also* normalizes
        to empty -- two empty keys are not "equal" here, they are both
        absent."""
        rows = [make_library_item(id=1, artist=""), make_library_item(id=2, artist="   ")]

        assert _rows_by_artist(rows, artist) == []


# ---------------------------------------------------------------------------
# apply_shelf_fallback -- the step function, called directly
# ---------------------------------------------------------------------------


class TestApplyShelfFallback:
    @pytest.mark.asyncio
    async def test_passthrough_when_results_already_present(self):
        db = AsyncMock()
        existing = [
            LookupResultItem(library_item=make_library_item(id=1).to_catalog_item()),
        ]

        out = await apply_shelf_fallback(_parsed(), db, False, existing, "direct", None, "library")

        assert out == (existing, "direct", None, "library", 0)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_timed_out(self):
        db = AsyncMock()
        shelve(db, [make_library_item(artist="Jessica Pratt")])

        out = await apply_shelf_fallback(_parsed(), db, True, [], "fallback", None, None)

        assert out == ([], "fallback", None, None, 0)
        db.artist_names_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_no_album(self):
        db = AsyncMock()

        out = await apply_shelf_fallback(_parsed(album=None), db, False, [], "none", None, None)

        assert out == ([], "none", None, None, 0)
        db.artist_names_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_album_is_whitespace(self):
        db = AsyncMock()

        out = await apply_shelf_fallback(_parsed(album="   "), db, False, [], "none", None, None)

        assert out == ([], "none", None, None, 0)
        db.artist_names_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_no_library_artist(self):
        db = AsyncMock()

        out = await apply_shelf_fallback(_parsed(artist=None), db, False, [], "none", None, None)

        assert out == ([], "none", None, None, 0)
        db.artist_names_matching.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_artist_not_shelved(self):
        db = AsyncMock()
        shelve(db, [])

        out = await apply_shelf_fallback(_parsed(), db, False, [], "none", None, None)

        assert out == ([], "none", None, None, 0)

    @pytest.mark.asyncio
    async def test_fires_and_builds_display_only_rows(self):
        db = AsyncMock()
        shelf = [
            make_library_item(id=201, artist="Jessica Pratt", title="On Your Own Love Again"),
            make_library_item(id=202, artist="Jessica Pratt", title="Quiet Signs"),
        ]
        shelve(db, shelf)

        result_items, search_type, context, external, rows = await apply_shelf_fallback(
            _parsed(), db, False, [], "none", None, None
        )

        assert [item.library_item.id for item in result_items] == [201, 202]
        assert all(item.artwork is None for item in result_items)
        assert all(item.matched_via is None for item in result_items)
        assert search_type == SEARCH_TYPE_FALLBACK
        assert context == (
            '"Zzyzx Road" not found in the library, but here are other albums by Jessica Pratt:'
        )
        assert external == "library"
        assert rows == 2
        db.artist_names_matching.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_orders_song_matching_title_first(self):
        db = AsyncMock()
        shelf = [
            make_library_item(id=1, artist="Jessica Pratt", title="Unrelated"),
            make_library_item(id=2, artist="Jessica Pratt", title="Zzyzx Road Live"),
        ]
        shelve(db, shelf)

        result_items, *_ = await apply_shelf_fallback(
            _parsed(song="Zzyzx Road"), db, False, [], "none", None, None
        )

        assert [item.library_item.id for item in result_items] == [2, 1]

    @pytest.mark.asyncio
    async def test_caps_at_max_search_results(self):
        db = AsyncMock()
        shelf = [
            make_library_item(id=i, artist="Jessica Pratt", title=f"Album {i}") for i in range(1, 8)
        ]
        shelve(db, shelf)

        result_items, _search_type, _context, _external, rows = await apply_shelf_fallback(
            _parsed(), db, False, [], "none", None, None
        )

        assert len(result_items) == MAX_SEARCH_RESULTS
        assert rows == MAX_SEARCH_RESULTS

    @pytest.mark.asyncio
    async def test_no_artwork_even_when_row_has_curated_streaming_links(self):
        """``on_streaming`` stands in for "the library row itself has curated
        streaming presence" -- the only such field ``LibraryItem`` carries.
        Even then, the built row must never carry ``artwork`` (hence no
        release id, artwork URL, year, Discogs URL, or streaming links --
        all of those ride only on ``artwork``)."""
        db = AsyncMock()
        row = make_library_item(
            id=5, artist="Jessica Pratt", title="Streaming Hit", on_streaming=True
        )
        shelve(db, [row])

        result_items, *_ = await apply_shelf_fallback(_parsed(), db, False, [], "none", None, None)

        assert result_items[0].artwork is None
        assert result_items[0].library_item.on_streaming is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("artist", "found", "expected_ids"),
        [
            pytest.param(
                "Can",
                [("Canibus", "Can-I-Bus"), ("Can", "Ege Bamyasi"), ("Can", "Future Days")],
                [2, 3],
                id="prefix-collision-dropped-own-rows-kept",
            ),
            pytest.param(
                "Low",
                [("Low Profile", "We're in This Together"), ("The Low Numbers", "Twist Again")],
                [],
                id="only-prefix-collisions-does-not-fire",
            ),
            pytest.param(
                "Sun Ra",
                [("Sun Ra", "Lanquidity"), ("Sun Ra Arkestra", "Swirling")],
                [1],
                id="longer-credit-is-a-different-artist",
            ),
            pytest.param(
                "nilufer yanya",
                [("Nilüfer Yanya", "PAINLESS"), ("Nilüfer Yanya", "My Method Actor")],
                [1, 2],
                id="case-and-diacritics-are-normalized",
            ),
        ],
    )
    async def test_keeps_only_rows_by_the_library_artist(self, artist, found, expected_ids):
        """``db.search`` (and ``filter_results_by_artist``'s prefix rung) admit
        any artist that merely *starts with* the query. This lane names the
        artist in its context line, so it keeps a row only on normalized
        artist equality -- and does not fire at all when none survives."""
        db = AsyncMock()
        shelve(
            db,
            [
                make_library_item(id=i, artist=row_artist, title=title)
                for i, (row_artist, title) in enumerate(found, start=1)
            ],
        )

        out = await apply_shelf_fallback(_parsed(artist=artist), db, False, [], "none", None, None)

        if not expected_ids:
            assert out == ([], "none", None, None, 0)
            return
        result_items, search_type, _context, _external, rows = out
        assert [item.library_item.id for item in result_items] == expected_ids
        assert search_type == SEARCH_TYPE_FALLBACK
        assert rows == len(expected_ids)

    @pytest.mark.asyncio
    async def test_fuzzy_corrected_artist_is_searched_and_named(self):
        """LML#626 two-channel seam: the shelf is searched under the corrected
        ``library_artist``, so that is the artist the sentence names. The typed
        album stays verbatim."""
        db = AsyncMock()
        shelve(db, [make_library_item(id=7, artist="Jessica Pratt", title="Quiet Signs")])
        parsed = _parsed(artist="Jesica Prat", library_artist="Jessica Pratt", album="zzyzx rd")

        result_items, _search_type, context, _external, rows = await apply_shelf_fallback(
            parsed, db, False, [], "none", None, None
        )

        db.artist_names_matching.assert_awaited_once()
        assert [item.library_item.id for item in result_items] == [7]
        assert rows == 1
        assert context == (
            '"zzyzx rd" not found in the library, but here are other albums by Jessica Pratt:'
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("typed", "stored"),
        [
            pytest.param("jessica pratt", "Jessica Pratt", id="casing"),
            pytest.param("Nilufer Yanya", "Nilüfer Yanya", id="diacritics"),
            pytest.param("Clientele", "The Clientele", id="leading-article"),
            pytest.param("Chuquimamani Condori", "Chuquimamani-Condori", id="punctuation"),
        ],
    )
    async def test_sentence_names_the_artist_as_the_rows_spell_it(self, typed, stored):
        """LML#1406: the sentence introduces the rows listed under it, so it
        uses their stored spelling, not the listener's."""
        db = shelve(AsyncMock(), [make_library_item(id=9, artist=stored, title="An Album")])

        result_items, _search_type, context, _external, _rows = await apply_shelf_fallback(
            _parsed(artist=typed), db, False, [], "none", None, None
        )

        assert [item.library_item.id for item in result_items] == [9]
        assert context == (
            f'"Zzyzx Road" not found in the library, but here are other albums by {stored}:'
        )

    @pytest.mark.asyncio
    async def test_search_failure_leaves_the_empty_response_unchanged(self, caplog):
        """On main this path returned an empty 200 with no further I/O, so a
        failing ``db.search`` must not turn it into a 500."""
        db = AsyncMock()
        db.artist_names_matching = AsyncMock(side_effect=RuntimeError("Database not connected"))

        with caplog.at_level(logging.WARNING, logger="lookup.shelf_fallback"):
            out = await apply_shelf_fallback(_parsed(), db, False, [], "direct", None, None)

        assert out == ([], "direct", None, None, 0)
        assert any(
            record.levelno == logging.WARNING and "Database not connected" in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_cancellation_propagates_instead_of_being_swallowed(self):
        """``except Exception`` -- never ``except BaseException`` -- guards the
        search call. ``asyncio.CancelledError`` is a ``BaseException``, not an
        ``Exception``, so a wider guard would swallow a cancellation (e.g. from
        the spine deadline) and return the empty-response passthrough instead
        of letting the task actually cancel."""
        db = AsyncMock()
        db.artist_names_matching = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await apply_shelf_fallback(_parsed(), db, False, [], "direct", None, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("shelf_size", "expected_calls"),
        [
            pytest.param(2, [("lookup.shelf_fallback_rows", 2)], id="fires"),
            pytest.param(0, [], id="does-not-fire"),
        ],
    )
    async def test_sentry_attr_is_set_only_when_the_lane_fires(self, shelf_size, expected_calls):
        db = AsyncMock()
        shelve(
            db,
            [
                make_library_item(id=i, artist="Jessica Pratt", title=f"Album {i}")
                for i in range(1, shelf_size + 1)
            ],
        )
        scope = Mock()

        with patch("lookup.shelf_fallback.sentry_sdk.get_current_scope", return_value=scope):
            await apply_shelf_fallback(_parsed(), db, False, [], "none", None, None)

        assert [call.args for call in scope.transaction.set_data.call_args_list] == expected_calls


# ---------------------------------------------------------------------------
# perform_lookup -- song-bearing album miss (LML#1391)
# ---------------------------------------------------------------------------


class TestPerformLookupShelfFallbackSongBearing:
    @pytest.mark.asyncio
    async def test_shelved_artist_unmatched_album_returns_shelf(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Artist shelved, typed album matches nothing, no strategy confirms
        the song -> the response carries the artist's shelf as fallback rows."""
        mock_library_db.find_similar_artist.return_value = None
        on_your_own = make_library_item(
            id=201, artist="Jessica Pratt", title="On Your Own Love Again"
        )
        quiet_signs = make_library_item(id=202, artist="Jessica Pratt", title="Quiet Signs")

        async def fake_search(query=None, **kwargs):
            if query == "Jessica Pratt":
                return [on_your_own, quiet_signs]
            return []

        mock_library_db.search.side_effect = fake_search
        shelve(mock_library_db, [on_your_own, quiet_signs])
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Back, Baby", artist="Jessica Pratt", releases=[], total=0, cached=False
            )
        )

        request = LookupRequest(
            artist="Jessica Pratt",
            song="Back, Baby",
            album="Zzyzx Road",
            raw_message="Play Back, Baby by Jessica Pratt",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert [item.library_item.id for item in response.results] == [201, 202]
        assert all(item.artwork is None for item in response.results)
        assert response.search_type == SEARCH_TYPE_FALLBACK
        assert response.song_not_found is True
        assert response.context_message == (
            '"Zzyzx Road" not found in the library, but here are other albums by Jessica Pratt:'
        )
        # LML#1233 telemetry invisibility: the internal signal reports the
        # true row count, but the response still classifies as a clean miss.
        assert response._shelf_fallback_rows == 2
        assert (
            derive_miss_kind(response, shelf_fallback_rows=response._shelf_fallback_rows)
            == MISS_CLEAN
        )

    @pytest.mark.asyncio
    async def test_shelf_capped_at_max_search_results(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        mock_library_db.find_similar_artist.return_value = None
        shelf = [
            make_library_item(id=i, artist="Jessica Pratt", title=f"Album {i}") for i in range(1, 8)
        ]

        async def fake_search(query=None, **kwargs):
            if query == "Jessica Pratt":
                return shelf
            return []

        mock_library_db.search.side_effect = fake_search
        shelve(mock_library_db, shelf)
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Back, Baby", artist="Jessica Pratt", releases=[], total=0, cached=False
            )
        )

        request = LookupRequest(
            artist="Jessica Pratt",
            song="Back, Baby",
            album="Zzyzx Road",
            raw_message="Play Back, Baby by Jessica Pratt",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) == MAX_SEARCH_RESULTS
        assert response._shelf_fallback_rows == MAX_SEARCH_RESULTS


# ---------------------------------------------------------------------------
# perform_lookup -- album-only miss (LML#1393)
# ---------------------------------------------------------------------------


class TestPerformLookupShelfFallbackAlbumOnly:
    @pytest.mark.asyncio
    async def test_shelved_artist_unmatched_album_no_song_returns_shelf(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """No song typed -- main would report ``search_type: direct`` with
        zero rows (ARTIST_PLUS_ALBUM is the last strategy tried and
        ``song_not_found`` stays False). The fallback must force ``fallback``,
        never leave ``direct`` standing over a non-empty shelf."""
        mock_library_db.find_similar_artist.return_value = None
        moon_pix = make_library_item(id=301, artist="Cat Power", title="Moon Pix")

        async def fake_search(query=None, **kwargs):
            if query == "Cat Power":
                return [moon_pix]
            return []

        mock_library_db.search.side_effect = fake_search
        shelve(mock_library_db, [moon_pix])
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Cat Power",
            album="Zzyzx Road",
            raw_message="Play Zzyzx Road by Cat Power",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert [item.library_item.id for item in response.results] == [301]
        assert response.results[0].artwork is None
        assert response.search_type == SEARCH_TYPE_FALLBACK
        assert response.search_type != "direct"
        assert response.context_message == (
            '"Zzyzx Road" not found in the library, but here are other albums by Cat Power:'
        )
        assert response._shelf_fallback_rows == 1
        assert (
            derive_miss_kind(response, shelf_fallback_rows=response._shelf_fallback_rows)
            == MISS_CLEAN
        )


# ---------------------------------------------------------------------------
# Invariance: every shape that is non-empty (or excluded) on main must stay
# byte-for-byte unchanged.
# ---------------------------------------------------------------------------


class TestPerformLookupShelfFallbackInvariance:
    @pytest.mark.asyncio
    async def test_direct_album_match_is_unchanged(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        item = make_library_item(
            id=10,
            artist="Duke Ellington & John Coltrane",
            title="Duke Ellington & John Coltrane",
        )
        mock_library_db.search.return_value = [item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Duke Ellington & John Coltrane",
            album="Duke Ellington & John Coltrane",
            raw_message="Play Duke Ellington & John Coltrane by Duke Ellington & John Coltrane",
        )

        with patch("lookup.orchestrator.apply_shelf_fallback", new_callable=AsyncMock) as spy:
            spy.side_effect = _passthrough_side_effect
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) == 1
        assert response.results[0].library_item.id == 10
        assert response.search_type == "direct"
        assert response._shelf_fallback_rows == 0
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_compilation_hit_is_unchanged(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """A confirmed compilation match (``found_on_compilation=True``,
        non-empty rows) must pass through untouched. ``execute_search_pipeline``
        is patched directly to isolate this call site's wiring from
        TRACK_ON_COMPILATION's own internals, which ``TestPerformLookupCompilations``
        already covers."""
        comp_item = make_library_item(
            id=50,
            artist="Various Artists - Electronic",
            title="A Station Compilation",
        )
        comp_state = SearchState(
            results=[comp_item],
            found_on_compilation=True,
            song_not_found=False,
            strategies_tried=[SearchStrategyType.TRACK_ON_COMPILATION],
        )
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="Chuquimamani-Condori",
            song="Call Your Name",
            raw_message="Play Call Your Name by Chuquimamani-Condori",
        )

        with (
            patch(
                "lookup.orchestrator.execute_search_pipeline",
                AsyncMock(return_value=comp_state),
            ),
            patch("lookup.orchestrator.apply_shelf_fallback", new_callable=AsyncMock) as spy,
        ):
            spy.side_effect = _passthrough_side_effect
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) == 1
        assert response.results[0].library_item.id == 50
        assert response.found_on_compilation is True
        assert response.search_type == "compilation"
        assert response._shelf_fallback_rows == 0
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_row_less_library_miss_probe_hit_is_unchanged(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """The step-3a row-less carry-through (LML#583/#628): library misses,
        Discogs has a confident match. Reuses the known-good recipe from
        ``test_library_miss_discogs.py``'s
        ``test_library_miss_with_discogs_hit_returns_result``."""
        from tests.factories import make_discogs_result

        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=37008771,
                    artist="Chuquimamani-Condori",
                    album="Chuquimamani-Condori",
                )
            ]
        )
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="Chuquimamani-Condori",
            album="Chuquimamani-Condori",
            raw_message="Chuquimamani-Condori - Chuquimamani-Condori",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.results[0].library_item.id == 0
        assert response.song_not_found is False
        assert response._shelf_fallback_rows == 0

    @pytest.mark.asyncio
    async def test_external_cache_fallback_rows_are_unchanged(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Step 7 (``include_external_caches``) already populated
        ``result_items`` -- reuses the known-good recipe from
        ``test_falls_back_to_discogs_when_library_empty``."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(
            return_value=[{"id": 99, "name": "Astrid Øster Mortensen", "score": 0.71}]
        )
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Astrid ster Mortenson",
            raw_message="Astrid ster Mortenson",
            include_external_caches=True,
        )

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert response.external_source == "discogs"
        assert len(response.results) == 1
        assert response.results[0].artwork is None
        assert response._shelf_fallback_rows == 0

    @pytest.mark.asyncio
    async def test_artist_not_shelved_stays_empty(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="A Totally Unshelved Artist",
            album="Some Album",
            raw_message="Play Some Album by A Totally Unshelved Artist",
        )
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert response.results == []
        assert response.degraded is False
        assert response._shelf_fallback_rows == 0

    @pytest.mark.asyncio
    async def test_no_typed_album_stays_as_main(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Call Your Name",
                artist="Chuquimamani-Condori",
                releases=[],
                total=0,
                cached=False,
            )
        )

        request = LookupRequest(
            artist="Chuquimamani-Condori",
            song="Call Your Name",
            raw_message="Play Call Your Name by Chuquimamani-Condori",
        )

        with (
            patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("lookup.orchestrator.apply_shelf_fallback", new_callable=AsyncMock) as spy,
        ):
            spy.side_effect = _passthrough_side_effect
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.results == []
        assert response._shelf_fallback_rows == 0
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state_flags", "low_priority", "deadline_spent", "expected"),
        [
            pytest.param({"timed_out": True}, False, False, {"timeout": True}, id="timed-out"),
            pytest.param(
                {"upstream_shed": True}, False, False, {"degraded": True}, id="search-leg-shed"
            ),
            pytest.param({}, True, False, {}, id="low-priority-caller"),
            pytest.param({}, False, True, {}, id="deadline-spent"),
        ],
    )
    async def test_each_skip_term_keeps_the_response_empty(
        self,
        mock_library_db,
        mock_discogs_service,
        telemetry,
        state_flags,
        low_priority,
        deadline_spent,
        expected,
    ):
        """Every ``skip`` term in ``perform_lookup``, pinned one at a time
        against the REAL ``apply_shelf_fallback``. None of them returns early:
        a mid-pipeline hard-cap trip (``timed_out``), LML#1126's search-leg
        breaker shed (``upstream_shed``), a low-priority caller and a spent
        spine deadline all reach the call site with an empty result list, an
        album and a shelved artist -- every other trigger condition holds, so
        removing a term makes that case return the shelf. The control case
        proves the same fixture does fire with no term set."""
        response = await self._lookup_shelved_artist_album_miss(
            mock_library_db, mock_discogs_service, telemetry, {}, False, False
        )
        assert [item.library_item.id for item in response.results] == [201]

        response = await self._lookup_shelved_artist_album_miss(
            mock_library_db,
            mock_discogs_service,
            telemetry,
            state_flags,
            low_priority,
            deadline_spent,
        )

        assert response.results == []
        assert response._shelf_fallback_rows == 0
        assert response.context_message is None
        assert response.external_source is None
        assert response.timeout is expected.get("timeout", False)
        assert response.degraded is expected.get("degraded", False)
        mock_library_db.artist_names_matching.assert_not_awaited()

    @staticmethod
    async def _lookup_shelved_artist_album_miss(
        mock_library_db, mock_discogs_service, telemetry, state_flags, low_priority, deadline_spent
    ):
        """Drive ``perform_lookup`` to step 8 with an empty search state for a
        shelved artist. ``execute_search_pipeline`` is patched so the state
        flags are set directly; ``should_shed_tail`` is neutralized so a spent
        deadline reaches step 8 instead of shedding the tail earlier."""
        mock_library_db.find_similar_artist.return_value = None
        shelve(
            mock_library_db,
            [make_library_item(id=201, artist="Jessica Pratt", title="Quiet Signs")],
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(
            artist="Jessica Pratt",
            album="Zzyzx Road",
            raw_message="Play Zzyzx Road by Jessica Pratt",
        )
        spent = (deadline_spent, 0.0 if deadline_spent else 25_000.0, "hard_cap")
        with (
            patch(
                "lookup.orchestrator.execute_search_pipeline",
                AsyncMock(return_value=SearchState(results=[], **state_flags)),
            ),
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=low_priority),
            patch("lookup.orchestrator.should_shed_tail", return_value=None),
            patch("lookup.spine_deadline.SpineDeadline.tail_exhausted", return_value=spent),
        ):
            return await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

    @pytest.mark.asyncio
    async def test_existing_trace_attrs_report_the_pre_fallback_response(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Telemetry invisibility, Sentry half: ``lookup.results_count`` and
        ``lookup.match_type`` are projected BEFORE step 8, so a shelf-only
        response still reads as the empty ``direct`` response main reports.
        The last write to each key wins, so a re-projection after step 8
        fails here. ``lookup.shelf_fallback_rows`` is the lane's one new key."""
        mock_library_db.find_similar_artist.return_value = None
        moon_pix = make_library_item(id=301, artist="Cat Power", title="Moon Pix")

        async def fake_search(query=None, **kwargs):
            return [moon_pix] if query == "Cat Power" else []

        mock_library_db.search.side_effect = fake_search
        shelve(mock_library_db, [moon_pix])
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        request = LookupRequest(
            artist="Cat Power", album="Zzyzx Road", raw_message="Play Zzyzx Road by Cat Power"
        )
        scope = Mock()

        with (
            patch("lookup.orchestrator.sentry_sdk.get_current_scope", return_value=scope),
            patch("lookup.shelf_fallback.sentry_sdk.get_current_scope", return_value=scope),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert [item.library_item.id for item in response.results] == [301]
        assert response.search_type == SEARCH_TYPE_FALLBACK
        attrs = {call.args[0]: call.args[1] for call in scope.transaction.set_data.call_args_list}
        assert attrs["lookup.results_count"] == 0
        assert attrs["lookup.match_type"] == "direct"
        assert attrs["lookup.shelf_fallback_rows"] == 1

    @pytest.mark.asyncio
    async def test_admission_shed_degraded_path_never_calls_shelf_fallback(
        self, mock_library_db, mock_discogs_service, telemetry, monkeypatch
    ):
        """Enforce-mode admission shed (LML#930 PR2) returns early via
        ``_build_degraded_response`` before the tail even starts -- reuses
        ``TestAdmissionShedGate``'s recipe from ``test_orchestrator.py``.
        The shelf fallback call site is never reached at all."""
        from lookup.admission import ADMISSION_SHED_ENFORCE_ENV_VAR

        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, "true")
        item = make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes")
        mock_library_db.search.return_value = [item]

        request = LookupRequest(
            artist="Stereolab",
            album="Aluminum Tunes",
            raw_message="Play Aluminum Tunes by Stereolab",
        )

        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=True),
            patch("lookup.orchestrator.get_event_loop_lag_ms", return_value=900.0),
            patch("lookup.orchestrator.apply_shelf_fallback", new_callable=AsyncMock) as spy,
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.degraded is True
        assert response._shelf_fallback_rows == 0
        spy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_low_priority_caller_excludes_shelf_fallback(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#1391/#1393's own gate: a low-priority caller (bulk/enrichment/
        backfill, never DJ-facing) on the SINGLE endpoint must not get the
        shelf decoration either, even though every other trigger condition
        holds. Mirrors the location-union gate's "D4" exclusion
        (``lookup/orchestrator.py``) and keeps `/lookup/bulk`'s
        ``match``/``no_match`` contract from self-contradicting."""
        mock_library_db.find_similar_artist.return_value = None
        shelf_item = make_library_item(id=201, artist="Jessica Pratt", title="Quiet Signs")

        async def fake_search(query=None, **kwargs):
            if query == "Jessica Pratt":
                return [shelf_item]
            return []

        mock_library_db.search.side_effect = fake_search
        shelve(mock_library_db, [shelf_item])
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Jessica Pratt",
            album="Zzyzx Road",
            raw_message="Play Zzyzx Road by Jessica Pratt",
        )

        with patch("lookup.orchestrator.is_discogs_low_priority", return_value=True):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.results == []
        assert response._shelf_fallback_rows == 0


async def _passthrough_side_effect(
    parsed, db, skip, result_items, search_type, context, external_source
):
    """Real implementation, called through the spy -- ``wraps`` for an
    ``AsyncMock``."""
    return await apply_shelf_fallback(
        parsed, db, skip, result_items, search_type, context, external_source
    )
