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

from unittest.mock import AsyncMock, patch

import pytest

from core.search import SEARCH_TYPE_FALLBACK, SearchState, SearchStrategyType
from discogs.models import DiscogsSearchResponse, TrackReleasesResponse
from lookup.matching import _FETCH_LIMIT, MAX_SEARCH_RESULTS
from lookup.models import LookupRequest, LookupResultItem
from lookup.orchestrator import perform_lookup
from lookup.shelf_fallback import _order_shelf_rows, apply_shelf_fallback
from services.parser import MessageType, ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_item

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

        assert out == (existing, "direct", None, "library")
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_timed_out(self):
        db = AsyncMock()
        db.search = AsyncMock(return_value=[make_library_item(artist="Jessica Pratt")])

        out = await apply_shelf_fallback(_parsed(), db, True, [], "fallback", None, None)

        assert out == ([], "fallback", None, None)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_no_album(self):
        db = AsyncMock()

        out = await apply_shelf_fallback(_parsed(album=None), db, False, [], "none", None, None)

        assert out == ([], "none", None, None)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_album_is_whitespace(self):
        db = AsyncMock()

        out = await apply_shelf_fallback(_parsed(album="   "), db, False, [], "none", None, None)

        assert out == ([], "none", None, None)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_no_library_artist(self):
        db = AsyncMock()

        out = await apply_shelf_fallback(_parsed(artist=None), db, False, [], "none", None, None)

        assert out == ([], "none", None, None)
        db.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_passthrough_when_artist_not_shelved(self):
        db = AsyncMock()
        db.search = AsyncMock(return_value=[])

        out = await apply_shelf_fallback(_parsed(), db, False, [], "none", None, None)

        assert out == ([], "none", None, None)

    @pytest.mark.asyncio
    async def test_fires_and_builds_display_only_rows(self):
        db = AsyncMock()
        shelf = [
            make_library_item(id=201, artist="Jessica Pratt", title="On Your Own Love Again"),
            make_library_item(id=202, artist="Jessica Pratt", title="Quiet Signs"),
        ]
        db.search = AsyncMock(return_value=shelf)

        result_items, search_type, context, external = await apply_shelf_fallback(
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
        db.search.assert_awaited_once_with(query="Jessica Pratt", limit=_FETCH_LIMIT)

    @pytest.mark.asyncio
    async def test_orders_song_matching_title_first(self):
        db = AsyncMock()
        shelf = [
            make_library_item(id=1, artist="Jessica Pratt", title="Unrelated"),
            make_library_item(id=2, artist="Jessica Pratt", title="Zzyzx Road Live"),
        ]
        db.search = AsyncMock(return_value=shelf)

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
        db.search = AsyncMock(return_value=shelf)

        result_items, *_ = await apply_shelf_fallback(_parsed(), db, False, [], "none", None, None)

        assert len(result_items) == MAX_SEARCH_RESULTS

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
        db.search = AsyncMock(return_value=[row])

        result_items, *_ = await apply_shelf_fallback(_parsed(), db, False, [], "none", None, None)

        assert result_items[0].artwork is None
        assert result_items[0].library_item.on_streaming is True


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
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_timed_out_path_is_unchanged(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """A mid-pipeline hard-cap trip (``core/search.py``) sets
        ``timed_out`` without an early return -- the honest empty response
        reaches the final builder, and the fallback must still no-op."""
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Chuquimamani-Condori",
            album="Zzyzx Road",
            raw_message="Play Zzyzx Road by Chuquimamani-Condori",
        )
        timed_out_state = SearchState(results=[], timed_out=True)

        with (
            patch(
                "lookup.orchestrator.execute_search_pipeline",
                AsyncMock(return_value=timed_out_state),
            ),
            patch("lookup.orchestrator.apply_shelf_fallback", new_callable=AsyncMock) as spy,
        ):
            spy.side_effect = _passthrough_side_effect
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.results == []
        assert response.timeout is True
        assert response.degraded is False
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_leg_shed_degraded_path_is_unchanged(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#1126's search-leg Discogs saturation-breaker shed sets
        ``state.upstream_shed`` and rides the *normal* return path (unlike
        the tail-shed/admission-shed flavor below) -- the shelf fallback
        call site IS reached, and must no-op via the explicit ``skip``
        check rather than relying on an early return."""
        request = LookupRequest(
            artist="Jessica Pratt",
            album="On Your Own Love Again",
            raw_message="Play On Your Own Love Again by Jessica Pratt",
        )
        shed_state = SearchState(results=[], upstream_shed=True)

        with (
            patch(
                "lookup.orchestrator.execute_search_pipeline",
                AsyncMock(return_value=shed_state),
            ),
            patch("lookup.orchestrator.apply_shelf_fallback", new_callable=AsyncMock) as spy,
        ):
            spy.side_effect = _passthrough_side_effect
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.results == []
        assert response.degraded is True
        spy.assert_awaited_once()

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
        spy.assert_not_awaited()


async def _passthrough_side_effect(
    parsed, db, skip, result_items, search_type, context, external_source
):
    """Real implementation, called through the spy -- ``wraps`` for an
    ``AsyncMock``."""
    return await apply_shelf_fallback(
        parsed, db, skip, result_items, search_type, context, external_source
    )
