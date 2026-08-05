"""Unit tests for lookup/orchestrator.py - the core search pipeline.

These tests verify perform_lookup() orchestrates the full pipeline:
1. Artist spelling correction
2. Album resolution from Discogs
3. Search strategy pipeline execution
4. Fallback track validation
5. Artwork fetch
6. Context message generation

All external dependencies (LibraryDB, DiscogsService) are mocked.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.exceptions import BreakerOpenError
from core.search import SearchState
from discogs.breaker import DiscogsBreakerOpenError
from discogs.models import DiscogsSearchResponse
from entity.compilation_track_location import CompilationTrackLocationRow
from generated.api_models import (
    DegradedReason,
    DiscogsReleaseInfo,
    DiscogsTrackReleasesResponse,
    TrackMatchSource,
)
from library.models import LibraryItem
from lookup.location_union import ResolvedLocation
from lookup.models import LookupRequest, LookupResponse
from lookup.orchestrator import LookupState, perform_lookup
from lookup.spine_deadline import LIMIT_CALLER_BUDGET, SpineDeadline
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result, make_library_item


class _FutureStreamingBreakerOpenError(BreakerOpenError):
    """Stand-in for a breaker that does not exist yet (LML#1118 regression pin)."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry():
    """Create a telemetry tracker for tests."""
    return make_lml_telemetry()


@pytest.fixture
def queen_item():
    return LibraryItem(
        id=1,
        artist="Queen",
        title="A Night at the Opera",
        call_letters="Q",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def queen_game_item():
    return LibraryItem(
        id=2,
        artist="Queen",
        title="The Game",
        call_letters="Q",
        artist_call_number=1,
        release_call_number=2,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def stereolab_item():
    return LibraryItem(
        id=10,
        artist="Stereolab",
        title="Emperor Tomato Ketchup",
        call_letters="S",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def compilation_item():
    return LibraryItem(
        id=20,
        artist="Various Artists - Rock - D",
        title="Disco Not Disco",
        call_letters="V",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


@pytest.fixture
def joni_self_titled():
    return LibraryItem(
        id=19,
        artist="Joni Mitchell",
        title="Joni Mitchell",
        call_letters="MI",
        artist_call_number=8,
        release_call_number=1,
        genre="Rock",
        format="Vinyl",
    )


@pytest.fixture
def joni_court_and_spark():
    return LibraryItem(
        id=20,
        artist="Joni Mitchell",
        title="Court and Spark",
        call_letters="MI",
        artist_call_number=8,
        release_call_number=6,
        genre="Rock",
        format="Vinyl",
    )


# ---------------------------------------------------------------------------
# Tests: perform_lookup - basic cases
# ---------------------------------------------------------------------------


class TestPerformLookupBasic:
    """Test the full perform_lookup pipeline for basic cases."""

    @pytest.mark.asyncio
    async def test_artist_and_album_direct_match(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Direct match: artist + album finds results immediately."""
        mock_library_db.search.return_value = [queen_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=12345,
                    album="A Night at the Opera",
                    artist="Queen",
                    artwork_url="https://example.com/cover.jpg",
                )
            ]
        )

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Queen"
        assert response.results[0].library_item.title == "A Night at the Opera"
        assert response.search_type == "direct"
        assert response.song_not_found is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "breaker_cls",
        [DiscogsBreakerOpenError, _FutureStreamingBreakerOpenError],
        ids=["discogs-breaker", "future-breaker-subclass"],
    )
    async def test_breaker_shed_in_tail_degrades_to_cache_only_not_500(
        self, mock_library_db, mock_discogs_service, telemetry, stereolab_item, breaker_cls
    ):
        """R2-2 backstop: a breaker-open shed raised from a post-search step
        (here artwork fetch) must be caught in ``perform_lookup`` and degraded
        to a cache-only/partial response — the library results already found
        are returned, artwork/enrichment pending — NOT propagated to a 500.
        Defense in depth beyond the runner catch.

        Parametrized over ``DiscogsBreakerOpenError`` and a throwaway
        ``BreakerOpenError`` subclass standing in for a breaker that does not
        exist yet (LML#1118 regression pin): the tail's catch is typed on the
        shared base, not the concrete Discogs type, so a not-yet-existing
        breaker (Bandcamp/YTM/Spotify) degrades through this same
        defense-in-depth leg with no further change -- instead of falling into
        the generic ``except Exception`` -> ``logger.exception`` -> ERROR path
        and reproducing the #755 flood shape.
        """
        mock_library_db.search.return_value = [stereolab_item]

        request = LookupRequest(
            artist="Stereolab",
            album="Emperor Tomato Ketchup",
            raw_message="Play Emperor Tomato Ketchup by Stereolab",
        )

        async def _shed(*_a, **_k):
            raise breaker_cls("shed mid-enrichment")

        with patch("lookup.orchestrator._step_fetch_artwork", side_effect=_shed):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        # 200-shaped degrade: the library match survives, no exception.
        assert isinstance(response, LookupResponse)
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Stereolab"
        # LML#930: a saturation-breaker shed is now marked degraded on the wire
        # with the upstream_unavailable reason (distinct from a deadline shed).
        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.upstream_unavailable

    @pytest.mark.asyncio
    async def test_tail_shed_on_caller_deadline_degrades_deadline_exceeded(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """LML#930: when the spine deadline is exhausted by the time the tail
        would run, the enrichment tail is shed and the response is marked
        degraded(deadline_exceeded) — the library match survives, the
        live-Discogs/Apple tail steps never run, and it is NOT a 500 or an empty
        timeout. ``SpineDeadline.tail_exhausted`` is forced True so step 2 (which
        uses the real, un-exhausted ``step_timeout``) still completes and we
        isolate the between-tail-step gate."""
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        miss_probe = AsyncMock()
        enrich = AsyncMock()
        with (
            patch.object(
                SpineDeadline,
                "tail_exhausted",
                return_value=(True, -50.0, LIMIT_CALLER_BUDGET),
            ),
            patch("lookup.orchestrator._step_library_miss_probe", miss_probe),
            patch("lookup.orchestrator._step_enrich_metadata", enrich),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert isinstance(response, LookupResponse)
        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.deadline_exceeded
        # Search results survive the shed.
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Queen"
        # The gate fired before the first tail step, so no tail work ran.
        miss_probe.assert_not_awaited()
        enrich.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_success_path_not_degraded(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """LML#930: a normal lookup (deadline not exhausted, no shed) reports
        degraded=False / degraded_reason=None — byte-identical to pre-#930 for
        existing consumers."""
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert response.degraded is False
        assert response.degraded_reason is None

    @pytest.mark.asyncio
    async def test_no_results_returns_empty(self, mock_library_db, mock_discogs_service, telemetry):
        """When nothing matches, return empty results."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None

        request = LookupRequest(
            artist="Nonexistent Band",
            song="Unknown Song",
            raw_message="Play Unknown Song by Nonexistent Band",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 0
        # LML#1126 regression pin: a genuine no-match (no breaker shed anywhere
        # in the pipeline) must stay byte-identical to the pre-#1126 contract —
        # this is the exact shape a search-leg shed used to falsify.
        assert response.degraded is False
        assert response.degraded_reason is None
        assert response.timeout is False

    @pytest.mark.asyncio
    async def test_search_leg_shed_marks_response_degraded(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#1126: a Discogs saturation-breaker shed caught inside the search
        pipeline (core/search.py's one catch boundary, R2-2) degrades to an
        empty/cache-only ``SearchState`` with ``upstream_shed=True`` set. That
        flag must ride onto ``LookupState`` and surface on the wire as
        ``degraded: true, degraded_reason: upstream_unavailable`` — before this
        fix the response was byte-identical to a genuine no-match.

        ``execute_search_pipeline`` is patched directly (rather than driving a
        real strategy through ``DiscogsBreakerOpenError``, which is already
        pinned at the runner level in ``tests/unit/test_search.py``) to isolate
        the orchestrator-side copy-through + response-build wiring this ticket
        adds. No album on the request so the library-miss probe (3a) — gated on
        empty ``library_results`` plus a present ``album`` — does not fire and
        need its own Discogs mock.
        """
        request = LookupRequest(
            artist="Jessica Pratt",
            song="Back, Baby",
            raw_message="Jessica Pratt - Back, Baby",
        )

        shed_state = SearchState(results=[], upstream_shed=True)

        with patch(
            "lookup.orchestrator.execute_search_pipeline",
            AsyncMock(return_value=shed_state),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert isinstance(response, LookupResponse)
        assert response.results == []
        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.upstream_unavailable
        # Control flow unchanged: no 500, timeout stays independent of the shed.
        assert response.timeout is False

    @pytest.mark.asyncio
    async def test_search_leg_shed_with_results_still_marks_degraded(
        self, mock_library_db, mock_discogs_service, telemetry, stereolab_item
    ):
        """Decision (LML#1126): a shed that still produced library results is
        ALSO marked degraded=True. ``degraded`` means "trustworthy but
        incomplete" — a lookup that found library rows but could not reach
        Discogs to confirm/enrich them is exactly that, matching the tail-shed
        arms (``_build_degraded_response``), which keep whatever the library +
        cached legs produced while still returning ``degraded=True``."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=555,
                    album="Emperor Tomato Ketchup",
                    artist="Stereolab",
                )
            ]
        )

        request = LookupRequest(
            artist="Stereolab",
            album="Emperor Tomato Ketchup",
            raw_message="Stereolab - Emperor Tomato Ketchup",
        )

        shed_state = SearchState(results=[stereolab_item], upstream_shed=True)

        with patch(
            "lookup.orchestrator.execute_search_pipeline",
            AsyncMock(return_value=shed_state),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Stereolab"
        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.upstream_unavailable

    @pytest.mark.asyncio
    async def test_no_discogs_service_still_works(self, mock_library_db, telemetry, queen_item):
        """Pipeline works without Discogs (artwork will be None)."""
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, None, telemetry)

        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Queen"
        assert response.results[0].artwork is None

    @pytest.mark.asyncio
    async def test_structured_fields_without_raw_message_resolves(self, mock_library_db, telemetry):
        """Regression (WXYC/library-metadata-lookup#783): a structured-only request
        (artist/album/song set, ``raw_message`` omitted → None on the public
        ``LookupRequest``) must resolve normally, not blow up.

        ``ParsedRequest.raw_message`` is a non-optional ``str``; ``_step_prepare``
        copied the request value straight across, so a ``None`` raw_message raised a
        Pydantic ValidationError that ``handle_lookup`` surfaced as HTTP 500. The
        structured path drives the pipeline from artist/album/song regardless of
        raw_message, so this must return the library hit."""
        stereolab_aluminum = make_library_item(
            id=42,
            artist="Stereolab",
            title="Aluminum Tunes",
            call_letters="RO",
            genre="Rock",
        )
        mock_library_db.search.return_value = [stereolab_aluminum]

        request = LookupRequest(
            artist="Stereolab",
            album="Aluminum Tunes",
            song="Pop Quiz",
            # raw_message intentionally omitted → None
        )
        assert request.raw_message is None

        response = await perform_lookup(request, mock_library_db, None, telemetry)

        assert isinstance(response, LookupResponse)
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Stereolab"
        assert response.results[0].library_item.title == "Aluminum Tunes"

    @pytest.mark.asyncio
    async def test_song_as_track_emits_matched_via_on_result_item(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Song-only query 'vi scose poise' surfaces Confield with matched_via populated.

        Verifies the full wiring: SONG_AS_TRACK strategy runs after SONG_AS_ARTIST
        returns empty, the result row's matched_via dict is plumbed through
        SearchState into LookupResultItem.matched_via, and the TrackMatchHint
        records source=discogs_release with the track title.
        """
        confield = make_library_item(id=60359, artist="Autechre", title="Confield")

        # SONG_AS_ARTIST does: db.search(query=song) → []
        # SONG_AS_TRACK does: db.search(query=album) → [confield]
        async def search_side_effect(query, **kwargs):
            return [confield] if query and "confield" in query.lower() else []

        mock_library_db.search.side_effect = search_side_effect
        mock_discogs_service.search_releases_by_track.return_value = DiscogsTrackReleasesResponse(
            track="vi scose poise",
            releases=[
                DiscogsReleaseInfo(
                    album="Confield",
                    artist="Autechre",
                    release_id=8434,
                    release_url="https://discogs.com/release/8434",
                    is_compilation=False,
                )
            ],
            total=1,
        )
        # SONG_AS_ARTIST queries Discogs by artist; return empty so it falls through.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(song="vi scose poise", raw_message="vi scose poise")

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        item = response.results[0]
        assert item.library_item.artist == "Autechre"
        assert item.library_item.title == "Confield"
        assert item.matched_via is not None
        assert len(item.matched_via) == 1
        hint = item.matched_via[0]
        assert hint.title == "vi scose poise"
        assert hint.source == TrackMatchSource.discogs_release

    @pytest.mark.asyncio
    async def test_matched_via_absent_on_non_track_match(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """When the result came via ARTIST_PLUS_ALBUM, matched_via is None.

        Guards against accidental population of matched_via on non-track-driven
        results — the field is reserved for track-search provenance per plan §5.1.
        """
        mock_library_db.search.return_value = [queen_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.results[0].matched_via is None


# ---------------------------------------------------------------------------
# Tests: perform_lookup - LML#930 PR2 admission shed gate
# ---------------------------------------------------------------------------


class TestAdmissionShedGate:
    """The admission-shed gate sits right after the search pipeline and before
    the PR1 tail_steps loop: ``evaluate_admission_shed(low_priority, lag_ms)``.
    Interactive traffic and shadow mode never observably change the response;
    enforce mode short-circuits to ``degraded(cache_only)`` before any tail
    step is awaited.
    """

    @pytest.mark.asyncio
    async def test_low_priority_high_lag_enforce_degrades_cache_only(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, monkeypatch
    ):
        """Enforce mode: a low-priority request under sustained loop lag sheds
        to degraded(cache_only) before the tail runs — the library match
        survives, no tail step is awaited."""
        from lookup.admission import ADMISSION_SHED_ENFORCE_ENV_VAR

        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, "true")
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        miss_probe = AsyncMock()
        enrich = AsyncMock()
        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=True),
            patch("lookup.orchestrator.get_event_loop_lag_ms", return_value=900.0),
            patch("lookup.orchestrator._step_library_miss_probe", miss_probe),
            patch("lookup.orchestrator._step_enrich_metadata", enrich),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.cache_only
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Queen"
        miss_probe.assert_not_awaited()
        enrich.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_low_priority_high_lag_shadow_tail_still_runs(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, monkeypatch
    ):
        """Shadow mode (enforce unset): the predicate holds and would-shed
        telemetry is emitted, but the response falls through to the normal
        tail — byte-identical to pre-PR2 for the wire contract."""
        from lookup.admission import ADMISSION_SHED_ENFORCE_ENV_VAR

        monkeypatch.delenv(ADMISSION_SHED_ENFORCE_ENV_VAR, raising=False)
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=True),
            patch("lookup.orchestrator.get_event_loop_lag_ms", return_value=900.0),
            patch("lookup.admission.project_admission_shed_telemetry") as project,
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.degraded is False
        assert response.degraded_reason is None
        assert len(response.results) == 1
        project.assert_called_once_with(enforced=False, lag_ms=900.0)

    @pytest.mark.asyncio
    async def test_interactive_high_lag_never_sheds(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, monkeypatch
    ):
        """Interactive traffic (low_priority=False) never sheds, no matter how
        high the loop lag reads — even with enforce on."""
        from lookup.admission import ADMISSION_SHED_ENFORCE_ENV_VAR

        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, "true")
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=False),
            patch("lookup.orchestrator.get_event_loop_lag_ms", return_value=9000.0),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.degraded is False
        assert response.degraded_reason is None
        assert len(response.results) == 1

    @pytest.mark.asyncio
    async def test_lag_none_or_below_threshold_tail_runs(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, monkeypatch
    ):
        """A low-priority request with no gauge sample (``None``) or lag under
        the threshold runs the tail exactly as today, even with enforce on."""
        from lookup.admission import ADMISSION_SHED_ENFORCE_ENV_VAR

        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, "true")
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        for lag_ms in (None, 10.0):
            with (
                patch("lookup.orchestrator.is_discogs_low_priority", return_value=True),
                patch("lookup.orchestrator.get_event_loop_lag_ms", return_value=lag_ms),
            ):
                response = await perform_lookup(
                    request, mock_library_db, mock_discogs_service, telemetry
                )

            assert response.degraded is False
            assert response.degraded_reason is None
            assert len(response.results) == 1

    @pytest.mark.asyncio
    async def test_admission_control_low_priority_degrades_before_protected_interactive(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, monkeypatch
    ):
        """The AC test (#930): under one injected high gauge value, a
        low-priority request sheds to cache_only while an interactive request
        completes the full tail — proving low-priority degrades first and
        interactive traffic is protected."""
        from lookup.admission import ADMISSION_SHED_ENFORCE_ENV_VAR

        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, "true")
        mock_library_db.search.return_value = [queen_item]

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=True),
            patch("lookup.orchestrator.get_event_loop_lag_ms", return_value=900.0),
        ):
            low_priority_response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=False),
            patch("lookup.orchestrator.get_event_loop_lag_ms", return_value=900.0),
        ):
            interactive_response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert low_priority_response.degraded is True
        assert low_priority_response.degraded_reason == DegradedReason.cache_only
        assert interactive_response.degraded is False
        assert interactive_response.degraded_reason is None


# ---------------------------------------------------------------------------
# Tests: perform_lookup - intra-spine telemetry spans
# ---------------------------------------------------------------------------


class TestPerformLookupTelemetrySpans:
    """Cover pipeline regions that run awaited work outside every existing
    ``track_step`` span (the "intra-spine" gap between spans)."""

    @pytest.mark.asyncio
    async def test_streaming_status_step_is_tracked(
        self, mock_library_db, mock_discogs_service, telemetry, stereolab_item
    ):
        """Step 3c's ``get_streaming_status`` DB call is awaited work with no
        span of its own — it must be tracked so the Server-Timing sum stops
        losing that duration."""
        mock_library_db.search.return_value = [stereolab_item]
        mock_library_db._has_streaming_links = True
        mock_library_db.get_streaming_status = AsyncMock(return_value={stereolab_item.id: True})
        # Also exercised by enrichment's librarian-override lookup (item.py) once
        # ``_has_streaming_links`` is True; None keeps it a no-op so this test
        # stays focused on step 3c's own span.
        mock_library_db.get_streaming_links = AsyncMock(return_value=None)
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Stereolab",
            album="Emperor Tomato Ketchup",
            raw_message="Emperor Tomato Ketchup by Stereolab",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        mock_library_db.get_streaming_status.assert_awaited_once_with([stereolab_item.id])
        assert response.results[0].library_item.id == stereolab_item.id
        assert telemetry.steps.get("streaming_status") is not None
        assert telemetry.steps["streaming_status"].success is True
        # Narrowing the span to just the await must not drop the sync fan-out
        # that sets on_streaming — lock the observable effect.
        assert response.results[0].library_item.on_streaming is True

    @pytest.mark.asyncio
    async def test_streaming_status_step_not_tracked_when_no_streaming_links(
        self, mock_library_db, mock_discogs_service, telemetry, stereolab_item
    ):
        """Sanity check on the negative: when the library has no streaming_links
        table, step 3c does no awaited work, so no span should be recorded
        (mirrors the existing gate at ``_step_populate_streaming_status``)."""
        mock_library_db.search.return_value = [stereolab_item]
        mock_library_db._has_streaming_links = False
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Stereolab",
            album="Emperor Tomato Ketchup",
            raw_message="Emperor Tomato Ketchup by Stereolab",
        )

        await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        mock_library_db.get_streaming_status.assert_not_awaited()
        assert "streaming_status" not in telemetry.steps


# ---------------------------------------------------------------------------
# Tests: perform_lookup - artist correction
# ---------------------------------------------------------------------------


class TestPerformLookupArtistCorrection:
    """Test that artist spelling is corrected before searching."""

    @pytest.mark.asyncio
    async def test_corrects_artist_spelling(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Misspelled artist gets corrected via fuzzy match."""
        mock_library_db.find_similar_artist.return_value = "Living Colour"
        mock_library_db.search.return_value = [
            make_library_item(id=5, artist="Living Colour", title="Vivid", call_letters="L")
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Living Color",
            raw_message="Play something by Living Color",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        # Artist should be corrected
        assert response.corrected_artist == "Living Colour"
        mock_library_db.find_similar_artist.assert_called_once_with("Living Color")

    @pytest.mark.asyncio
    async def test_artist_correction_and_album_resolution_both_execute(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Artist correction and album resolution should both run, with the
        corrected artist applied before search pipeline executes."""
        mock_library_db.find_similar_artist.return_value = "Stereolab"
        mock_library_db.search.return_value = [
            make_library_item(artist="Stereolab", title="Emperor Tomato Ketchup")
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Stereolba",
            song="Percolator",
            raw_message="Stereolba - Percolator",
        )

        with (
            patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new_callable=AsyncMock,
                return_value=[("Stereolab", "Emperor Tomato Ketchup")],
            ),
            patch(
                "lookup.orchestrator.fetch_artwork_for_items",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        mock_library_db.find_similar_artist.assert_called_once_with("Stereolba")
        assert response.corrected_artist == "Stereolab"
        assert telemetry.steps.get("album_lookup") is not None

    @pytest.mark.asyncio
    async def test_typed_artist_reaches_discogs_probe_corrected_reaches_library(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Two-channel seam (WXYC/library-metadata-lookup#626) end-to-end.

        Typed "Plug" is a *distinct* non-library artist one edit from library
        "Plugz". The correction must NOT snap "Plug" into Discogs resolution:
        ``search_releases_by_track`` (the Discogs probe) sees the typed "Plug",
        while the primary library leg's ``db.search`` queries carry the corrected
        "Plugz".

        ``raw_message`` is deliberately non-ambiguous (no "X - Y" two-part shape)
        so SWAPPED_INTERPRETATION — which re-parses the raw message into typed
        tokens and is out of this seam's scope — never fires and the only library
        queries come from ``search_library_with_fallback``.
        """
        from discogs.models import TrackReleasesResponse

        mock_library_db.find_similar_artist.return_value = "Plugz"
        mock_library_db.search.return_value = []
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(track="Drift", artist="Plug", releases=[], total=0)
        )

        request = LookupRequest(
            artist="Plug",
            song="Drift",
            raw_message="dj played Drift by Plug",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        # The correction is reported, but parsed.artist was NOT overwritten:
        # every Discogs probe saw the typed "Plug".
        assert response.corrected_artist == "Plugz"
        assert mock_discogs_service.search_releases_by_track.await_count >= 1
        for call in mock_discogs_service.search_releases_by_track.await_args_list:
            artist_arg = call.kwargs.get("artist")
            if artist_arg is None and len(call.args) >= 2:
                artist_arg = call.args[1]
            assert artist_arg == "Plug"

        # The library leg used the corrected "Plugz" — every artist-keyed
        # library query carries it, and none falls back to the typed "Plug".
        library_queries = [
            (c.kwargs.get("query") or (c.args[0] if c.args else ""))
            for c in mock_library_db.search.await_args_list
        ]
        assert any("Plugz" in q for q in library_queries)
        assert not any("Plug" in q and "Plugz" not in q for q in library_queries)

    @pytest.mark.asyncio
    async def test_misspelled_library_artist_still_binds_to_library_row(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """The accepted-trade-off direction: a misspelled *library* artist still
        corrects and binds to its library row via the corrected library leg.

        Typed "Stereolb" corrects to library "Stereolab"; the artist+album
        library search surfaces the Stereolab row, which is returned.
        """
        stereolab_row = make_library_item(id=10, artist="Stereolab", title="Aluminum Tunes")
        mock_library_db.find_similar_artist.return_value = "Stereolab"
        mock_library_db.search.return_value = [stereolab_row]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Stereolb",
            album="Aluminum Tunes",
            raw_message="Stereolb - Aluminum Tunes",
        )

        with patch(
            "lookup.orchestrator.fetch_artwork_for_items",
            new_callable=AsyncMock,
            return_value=[(stereolab_row, None)],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.corrected_artist == "Stereolab"
        assert any(item.library_item.id == 10 for item in response.results)
        # The library search ran under the corrected name.
        library_queries = [
            (c.kwargs.get("query") or (c.args[0] if c.args else ""))
            for c in mock_library_db.search.await_args_list
        ]
        assert any("Stereolab" in q for q in library_queries)


# ---------------------------------------------------------------------------
# Tests: perform_lookup - album resolution from Discogs
# ---------------------------------------------------------------------------


class TestPerformLookupAlbumResolution:
    """Test album resolution when song is provided without album."""

    @pytest.mark.asyncio
    async def test_resolves_album_from_discogs_when_song_only(
        self, mock_library_db, mock_discogs_service, telemetry, stereolab_item
    ):
        """When song + artist given but no album, Discogs resolves album names."""
        mock_library_db.find_similar_artist.return_value = None
        mock_library_db.search.return_value = [stereolab_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Stereolab",
            song="Percolator",
            raw_message="Play Percolator by Stereolab",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Stereolab", "Emperor Tomato Ketchup")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) >= 1
        assert response.song_not_found is False

    @pytest.mark.asyncio
    async def test_track_validation_filters_album_resolved_results(
        self,
        mock_library_db,
        mock_discogs_service,
        telemetry,
        joni_self_titled,
        joni_court_and_spark,
    ):
        """Album resolution may return false positives; track validation should filter them.

        Scenario: "Help Me by Joni Mitchell"
        - Discogs resolves albums: ["Court and Spark", "Joni Mitchell"]
        - Library matches both (self-titled + Court and Spark)
        - Track validation confirms "Help Me" is on Court and Spark but NOT self-titled
        - Only Court and Spark should remain in results
        """
        mock_library_db.find_similar_artist.return_value = None
        # search_library_with_fallback searches each album:
        # 1. "Joni Mitchell Court and Spark" -> Court and Spark
        # 2. "Joni Mitchell Joni Mitchell" -> self-titled
        mock_library_db.search.side_effect = [
            [joni_court_and_spark],
            [joni_self_titled],
        ]

        # Discogs search for artwork (called per result in filter_results_by_track_validation)
        court_and_spark_discogs = make_discogs_result(
            release_id=1001,
            album="Court and Spark",
            artist="Joni Mitchell",
            artwork_url="https://example.com/court-and-spark.jpg",
        )
        self_titled_discogs = make_discogs_result(
            release_id=1002,
            album="Joni Mitchell",
            artist="Joni Mitchell",
            artwork_url="https://example.com/joni-mitchell.jpg",
        )
        # search() is called by filter_results_by_track_validation for each item,
        # then again by fetch_artwork_for_items
        mock_discogs_service.search.side_effect = [
            # track validation: search for Court and Spark
            DiscogsSearchResponse(results=[court_and_spark_discogs]),
            # track validation: search for Joni Mitchell (self-titled)
            DiscogsSearchResponse(results=[self_titled_discogs]),
            # artwork fetch for the one remaining result
            DiscogsSearchResponse(results=[court_and_spark_discogs]),
        ]

        # validate_track_on_release: "Help Me" IS on Court and Spark, NOT on self-titled
        mock_discogs_service.validate_track_on_release.side_effect = [True, False]

        request = LookupRequest(
            artist="Joni Mitchell",
            song="Help Me",
            raw_message="Play Help Me by Joni Mitchell",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Joni Mitchell", "Court and Spark"), ("Joni Mitchell", "Joni Mitchell")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) == 1
        assert response.results[0].library_item.title == "Court and Spark"
        assert response.song_not_found is False
        assert response.search_type == "direct"


# ---------------------------------------------------------------------------
# Tests: perform_lookup - fallback and context messages
# ---------------------------------------------------------------------------


class TestPerformLookupFallback:
    """Test fallback behavior when exact match isn't found."""

    @pytest.mark.asyncio
    async def test_song_not_found_sets_context_message(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, queen_game_item
    ):
        """When song isn't found, fall back to artist albums with context message."""
        # First search (artist+album) returns empty, fallback to artist-only
        mock_library_db.find_similar_artist.return_value = None
        mock_library_db.search.side_effect = [
            [],  # artist + song
            [queen_item, queen_game_item],  # artist only
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False

        request = LookupRequest(
            artist="Queen",
            song="Unknown Track",
            raw_message="Play Unknown Track by Queen",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.song_not_found is True
        assert response.context_message is not None
        assert "Queen" in response.context_message

    @pytest.mark.asyncio
    async def test_track_validation_filters_fallback_results(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, queen_game_item
    ):
        """When fallback returns all artist albums, track validation filters to correct one."""
        mock_library_db.find_similar_artist.return_value = None
        # Fallback: artist-only returns both albums
        mock_library_db.search.side_effect = [
            [],  # artist + song
            [queen_item, queen_game_item],  # artist only
        ]

        # Discogs validates: "Bohemian Rhapsody" is on "A Night at the Opera" but not "The Game"
        search_result = make_discogs_result(
            release_id=12345,
            album="A Night at the Opera",
            artist="Queen",
            artwork_url="https://example.com/opera.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[search_result])
        # validate_track_on_release: True for queen_item, False for queen_game_item
        mock_discogs_service.validate_track_on_release.side_effect = [True, False]

        request = LookupRequest(
            artist="Queen",
            song="Bohemian Rhapsody",
            raw_message="Play Bohemian Rhapsody by Queen",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        # Track validation should narrow it down
        assert response.song_not_found is False
        assert len(response.results) == 1
        assert response.results[0].library_item.title == "A Night at the Opera"

    @pytest.mark.asyncio
    async def test_promotes_album_known_to_contain_track_from_local_cache(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """When per-result Discogs validation can't confirm any artist-fallback
        candidate, the orchestrator should consult the local Discogs PG cache
        for "releases by this artist containing this track" and promote the
        matching library album.

        Reproduces "bucky skank by lee scratch perry": the artist-only fallback
        returned 5 unrelated Lee Perry albums, all failed validation, and the
        library album that actually contains the song ("Live at Maritime Hall")
        was never surfaced — even though the local cache had the answer.
        """
        # Five fallback albums that don't contain the track
        fallback_items = [
            LibraryItem(
                id=12663,
                artist="Lee 'Scratch' Perry",
                title="Chicken Scratch",
                call_letters="Pe",
                artist_call_number=1,
                release_call_number=1,
                genre="Reggae",
                format="vinyl",
            ),
            LibraryItem(
                id=12664,
                artist="Lee 'Scratch' Perry",
                title='Ooh! Wah! 12"',
                call_letters="Pe",
                artist_call_number=1,
                release_call_number=2,
                genre="Reggae",
                format="vinyl",
            ),
        ]
        # The actual answer (in the library, but not in the fallback's top-N FTS slice)
        maritime = LibraryItem(
            id=12682,
            artist="Lee 'Scratch' Perry",
            title="Live at Maritime Hall",
            call_letters="Pe",
            artist_call_number=1,
            release_call_number=20,
            genre="Reggae",
            format="cd",
        )

        mock_library_db.find_similar_artist.return_value = None

        # Mirror the production FTS5 behavior: queries containing the song words
        # ("bucky"/"skank") match nothing because no album *title* contains
        # those tokens. Only the artist-only and album-title-based queries hit.
        async def fake_search(query=None, **kwargs):
            q = (query or "").lower()
            if "bucky" in q or "skank" in q:
                return []
            if "maritime" in q:
                return [maritime]
            if "scratch" in q or "perry" in q:
                return fallback_items
            return []

        mock_library_db.search.side_effect = fake_search

        # Per-result validation fails for all fallback items (none contain the track)
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False

        from discogs.models import ReleaseInfo, TrackReleasesResponse

        # Compilation strategy's two API calls return nothing (mirrors prod where
        # the artist-only fallback isn't replaced by a compilation hit)
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Bucky Skank",
                artist="Lee 'Scratch' Perry",
                releases=[],
                total=0,
                cached=False,
            )
        )

        # The local PG cache holds the answer — Maritime Hall has Bucky Skank
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(
            return_value=[
                ReleaseInfo(
                    album="Lee Scratch Perry Live At Maritime Hall",
                    artist="Lee 'Scratch' Perry",
                    release_id=2865555,
                    release_url="https://discogs.com/release/2865555",
                    is_compilation=False,
                ),
            ]
        )

        request = LookupRequest(
            artist="Lee 'Scratch' Perry",
            song="Bucky Skank",
            raw_message="bucky skank by lee scratch perry",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        titles = [r.library_item.title for r in response.results]
        assert "Live at Maritime Hall" in titles, (
            f"Cache-known album should be promoted; got: {titles}"
        )
        assert response.song_not_found is False, (
            "Promoting a confirmed track-bearing album should clear song_not_found"
        )

    @pytest.mark.asyncio
    async def test_song_matching_album_title_clears_song_not_found(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """When the requested "song" is the title of an album by the artist,
        treat it as a direct album match — not a song-not-found fallback.

        Reproduces "on patrol, sun araw": request-o-matic routed it as
        ``song="On Patrol"`` / ``artist="Sun Araw"``. The Sun Araw album
        "On Patrol" is in the library and surfaced by the artist+song FTS
        branch, but Discogs has no track called "On Patrol" on any Sun Araw
        release, so per-result validation + cached-track promotion both
        fail and ``song_not_found`` stays True. The bot then says
        '"On Patrol" is not on any album in the library' — about the album
        sitting in its own result list.
        """
        on_patrol = LibraryItem(
            id=42,
            artist="Sun Araw",
            title="On Patrol",
            call_letters="SU",
            artist_call_number=138,
            release_call_number=1,
            genre="Rock",
            format="cd",
        )

        mock_library_db.find_similar_artist.return_value = None

        # search_library_with_fallback call order for (artist + song, no album):
        #   1. artist + song FTS -> finds "On Patrol" (title matches the song term)
        async def fake_search(query=None, **kwargs):
            q = (query or "").lower()
            if "sun araw" in q:
                return [on_patrol]
            return []

        mock_library_db.search.side_effect = fake_search

        # Per-result validation fails: no track named "On Patrol" on this release.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        mock_discogs_service.validate_track_on_release.return_value = False

        # Cache-known-track promotion also turns up empty (no Sun Araw release
        # in the cache contains a track titled "On Patrol").
        mock_discogs_service.cache_service = AsyncMock()
        mock_discogs_service.cache_service.search_releases_by_track = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Sun Araw",
            song="On Patrol",
            raw_message="on patrol, sun araw",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert len(response.results) == 1
        assert response.results[0].library_item.title == "On Patrol"
        assert response.song_not_found is False, (
            "The album titled 'On Patrol' is in results — the user wanted "
            "that album, not a track. song_not_found should be cleared."
        )
        assert response.context_message is None, (
            "Should not emit the misleading 'not on any album' message when "
            "results contain an album titled the same as the requested song."
        )


# ---------------------------------------------------------------------------
# Tests: perform_lookup - compilation search
# ---------------------------------------------------------------------------


class TestPerformLookupCompilations:
    """Test compilation search when direct search fails."""

    @pytest.mark.asyncio
    async def test_finds_song_on_compilation(
        self, mock_library_db, mock_discogs_service, telemetry, compilation_item
    ):
        """When song not on any artist album, find it on a compilation."""
        mock_library_db.find_similar_artist.return_value = None

        # A fallback item that would be returned by artist-only search
        fallback_item = make_library_item(
            id=99,
            artist="Some Artist",
            title="Some Album",
            call_letters="S",
        )

        # search_library_with_fallback call order:
        # 1. artist + song -> empty (no album match)
        # 2. artist only -> returns fallback (triggers song_not_found=True)
        # Then search_compilations_for_track is triggered:
        # 3. keyword search -> returns compilation_item
        # Then search_album_fuzzy is called for the Discogs album title:
        # 4. exact search for "Disco Not Disco" -> returns compilation_item
        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + song
            [fallback_item],  # search_library_with_fallback: artist only (song_not_found=True)
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact search for Discogs album
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Some Artist",
            song="Disco Song",
            raw_message="Play Disco Song by Some Artist",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Various Artists", "Disco Not Disco")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.context_message is not None
        assert "Found" in response.context_message

    @pytest.mark.asyncio
    async def test_finds_song_on_compilation_when_artist_not_in_library(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Compilation search runs when the track artist has zero library entries.

        Scenario: "No Way Back by Adonis"
        - resolve_albums_for_track finds only VA releases -> song_not_found=True
        - search_library_with_fallback finds nothing for "Adonis" -> ([], False)
        - song_not_found from album resolution must propagate so
          TRACK_ON_COMPILATION still triggers
        """
        compilation_item = make_library_item(
            id=46602,
            artist="Various Artists - Electronic - T",
            title="Trax Records 20th Anniversary Collection",
            call_letters="V",
        )

        mock_library_db.find_similar_artist.return_value = None

        # search_library_with_fallback: artist+song -> empty, artist-only -> empty
        # search_compilations_for_track: keyword search -> compilation,
        #   search_album_fuzzy -> compilation
        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + song
            [],  # search_library_with_fallback: artist only (Adonis not in library)
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact search for Discogs album
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[
                ("Various Artists", "Trax Records 20th Anniversary Collection"),
            ],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.results[0].library_item.title == "Trax Records 20th Anniversary Collection"
        assert response.context_message is not None
        assert "Found" in response.context_message

    @pytest.mark.asyncio
    async def test_finds_song_on_compilation_when_discogs_resolves_single(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Compilation search runs when Discogs resolves an artist single not in library.

        Scenario: "No Way Back by Adonis"
        - resolve_albums_for_track finds Adonis single -> (["No Way Back"], False)
        - search_library_with_fallback finds nothing for "Adonis" at all -> ([], True)
        - ARTIST_PLUS_ALBUM sets song_not_found=True from the fallback flag
        - TRACK_ON_COMPILATION triggers and finds the VA compilation
        """
        compilation_item = make_library_item(
            id=46602,
            artist="Various Artists - Electronic - T",
            title="Trax Records 20th Anniversary Collection",
            call_letters="V",
        )

        mock_library_db.find_similar_artist.return_value = None

        # search_library_with_fallback call order:
        # 1. artist + album "Adonis No Way Back" -> empty
        # 2. artist + song "Adonis No Way Back" -> empty
        # 3. artist only "Adonis" -> empty
        # Then search_compilations_for_track:
        # 4. keyword search "adonis back" -> compilation
        # 5. search_album_fuzzy("No Way Back") exact -> rejected by album_title_acceptable
        # 6. search_album_fuzzy("No Way Back") fuzzy "back" -> rejected by similarity
        # 7. search_album_fuzzy("Trax Records...") exact -> compilation (VA match)
        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + album
            [],  # search_library_with_fallback: artist + song
            [],  # search_library_with_fallback: artist only
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact "No Way Back" (filtered)
            [compilation_item],  # search_album_fuzzy: fuzzy "back" (rejected)
            [compilation_item],  # search_album_fuzzy: exact "Trax Records..." (match)
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        # Discogs API with artist field filter returns only the single;
        # with artist as keyword, also returns VA compilations
        async def mock_track_lookup(track, artist=None, artist_as_keyword=False, **kwargs):
            if artist and not artist_as_keyword:
                return [("Adonis", "No Way Back")]
            if artist and artist_as_keyword:
                return [
                    ("Adonis", "No Way Back"),
                    ("Various Artists", "Trax Records 20th Anniversary Collection"),
                ]
            return []

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            side_effect=mock_track_lookup,
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.results[0].library_item.title == "Trax Records 20th Anniversary Collection"
        assert response.context_message is not None
        assert "Found" in response.context_message

    @pytest.mark.asyncio
    async def test_compilation_search_uses_artist_as_keyword(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Compilation search uses artist as keyword (q param) to find VA compilations.

        The Discogs API `artist` param filters by release-level artist, which
        excludes VA compilations. Using `artist_as_keyword=True` puts the artist
        in the `q` param instead, matching track-level credits on compilations.
        """
        from discogs.models import ReleaseInfo, TrackReleasesResponse

        compilation_item = make_library_item(
            id=46602,
            artist="Various Artists - Hiphop",
            title="Trax Records 20th Anniversary Collection",
            call_letters="V",
        )

        mock_library_db.find_similar_artist.return_value = None

        # db.search call order:
        # 1. search_library_with_fallback: "Adonis No Way Back" (artist+album) -> []
        # 2. search_library_with_fallback: "Adonis No Way Back" (artist+song) -> []
        # 3. search_library_with_fallback: "Adonis" (artist only) -> []
        # 4. search_compilations_for_track: keyword "adonis back" -> []
        # 5. search_album_fuzzy("No Way Back"): exact -> []
        # 6. search_album_fuzzy("Trax Records..."): exact -> [compilation_item]
        mock_library_db.search.side_effect = [
            [],  # 1: artist + album
            [],  # 2: artist + song
            [],  # 3: artist only
            [],  # 4: keyword search
            [],  # 5: search_album_fuzzy("No Way Back") exact
            [compilation_item],  # 6: search_album_fuzzy("Trax Records...") exact
        ]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        # search_compilations_for_track calls discogs_service.search_releases_by_track
        # directly (library-first approach), not lookup_releases_by_track.
        adonis_release = ReleaseInfo(
            album="No Way Back",
            artist="Adonis",
            release_id=1001,
            release_url="https://www.discogs.com/release/1001",
            is_compilation=False,
        )
        trax_release = ReleaseInfo(
            album="Trax Records 20th Anniversary Collection",
            artist="Various Artists",
            release_id=1002,
            release_url="https://www.discogs.com/release/1002",
            is_compilation=True,
        )

        async def mock_discogs_search(track, artist=None, artist_as_keyword=False, **kwargs):
            if not artist_as_keyword:
                return TrackReleasesResponse(
                    track=track, artist=artist, releases=[adonis_release], total=1
                )
            return TrackReleasesResponse(
                track=track,
                artist=artist,
                releases=[adonis_release, trax_release],
                total=2,
            )

        mock_discogs_service.search_releases_by_track = AsyncMock(side_effect=mock_discogs_search)
        mock_discogs_service.validate_track_on_release = AsyncMock(return_value=True)

        request = LookupRequest(
            artist="Adonis",
            song="No Way Back",
            raw_message="No Way Back by Adonis",
        )

        # resolve_albums_for_track still uses lookup_releases_by_track
        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Adonis", "No Way Back")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        assert len(response.results) >= 1
        assert response.results[0].library_item.title == "Trax Records 20th Anniversary Collection"

    @pytest.mark.asyncio
    async def test_compilation_results_skip_track_validation(
        self, mock_library_db, mock_discogs_service, telemetry, compilation_item
    ):
        """When results come from compilation search, step 3b (filter_results_by_track_validation)
        should be skipped because the track was already validated in search_compilations_for_track.

        Re-running validation would make unnecessary Discogs API calls.
        """
        mock_library_db.find_similar_artist.return_value = None

        fallback_item = make_library_item(
            id=99,
            artist="Some Artist",
            title="Some Album",
            call_letters="S",
        )

        mock_library_db.search.side_effect = [
            [],  # search_library_with_fallback: artist + song
            [fallback_item],  # search_library_with_fallback: artist only
            [compilation_item],  # search_compilations_for_track: keyword search
            [compilation_item],  # search_album_fuzzy: exact search
        ]

        # If filter_results_by_track_validation runs, it calls discogs_service.search
        # for each result. We track whether this happens.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Some Artist",
            song="Disco Song",
            raw_message="Play Disco Song by Some Artist",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[("Various Artists", "Disco Not Disco")],
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        assert response.found_on_compilation is True
        # Compilation results should NOT be re-validated (already validated in
        # search_compilations_for_track). search calls come from:
        # 1. Artist fallback validation: 1 call (for fallback_item → no Discogs match)
        # 2. Artwork fetch: 1 call (for compilation_item)
        # Without the artist fallback validation this would be 1 call.
        # If compilation results were ALSO re-validated, count would be 3+.
        assert mock_discogs_service.search.call_count <= 2


# ---------------------------------------------------------------------------
# Tests: perform_lookup - comprehensive multi-location union (LML#1022)
# ---------------------------------------------------------------------------


def _location_row(
    *,
    library_id: int = 60654,
    track_position: str = "A3",
    track_artist: str = "brian reitzell",
    track_title: str = "ikebana",
    credit_role: str = "primary",
    discogs_release_id: int = 12345,
    artwork_url: str | None = "https://example.com/lit.jpg",
) -> CompilationTrackLocationRow:
    return CompilationTrackLocationRow(
        library_id=library_id,
        track_position=track_position,
        track_artist=track_artist,
        track_title=track_title,
        credit_role=credit_role,
        discogs_release_id=discogs_release_id,
        artwork_url=artwork_url,
    )


class TestPerformLookupLocationUnion:
    """The location union folded transparently into ``results`` -- gating, the
    kill-switch-off/low-priority no-op contracts, the Case A / Case B
    live-repro shapes from LML#271/#1022, dedup, the missing-shelf-item skip,
    non-suppression of the external-cache fallback, degraded-path carry
    (bounded await), and post-fold telemetry agreement. ``resolve_track_shelf_locations``
    is patched at the orchestrator's import site -- its own ranking/shelf-join
    logic is covered by ``tests/unit/test_location_union.py``; these tests
    verify orchestration only."""

    @pytest.mark.asyncio
    async def test_kill_switch_disabled_no_extra_result_rows(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, monkeypatch
    ):
        """Server-side kill switch off: no location rows are ever appended,
        for any caller -- an incident can be mitigated with a Railway var
        flip, no client deploy. Asserted on the result-row count (D3 removed
        the separate response field itself, so there is no removed-field
        attribute left to assert `is None` on)."""
        from config.settings import get_settings

        monkeypatch.setenv("LML_LOCATION_UNION_ENABLED", "false")
        get_settings.cache_clear()
        try:
            mock_library_db.search.return_value = [queen_item]
            mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

            request = LookupRequest(
                artist="Queen",
                song="Bohemian Rhapsody",
                album="A Night at the Opera",
                raw_message="Play Bohemian Rhapsody by Queen",
            )

            with patch(
                "lookup.orchestrator.resolve_track_shelf_locations", new_callable=AsyncMock
            ) as resolve_mock:
                response = await perform_lookup(
                    request,
                    mock_library_db,
                    mock_discogs_service,
                    telemetry,
                    discogs_cache_pg=AsyncMock(),
                )

            assert len(response.results) == 1
            resolve_mock.assert_not_awaited()
        finally:
            monkeypatch.delenv("LML_LOCATION_UNION_ENABLED", raising=False)
            get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_low_priority_caller_gets_no_extra_result_rows(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """D4: a low-priority caller (BS enrichment / the CDC firehose /
        backfills, signaled via ``is_discogs_low_priority()``) never even
        launches the probe -- class 5 gets no extra result rows, regardless
        of what the recall index would have returned."""
        mock_library_db.search.return_value = [queen_item]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Queen",
            song="Bohemian Rhapsody",
            album="A Night at the Opera",
            raw_message="Play Bohemian Rhapsody by Queen",
        )

        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=True),
            patch(
                "lookup.orchestrator.resolve_track_shelf_locations", new_callable=AsyncMock
            ) as resolve_mock,
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        assert len(response.results) == 1
        resolve_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_case_b_no_primary_match_surfaces_other_location_as_a_normal_result(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """'ikebana by brian reitzell' (LML#271 live-repro): the artist has no
        own release in the library (Case B) -- the LiT soundtrack location
        surfaces as an ordinary result, tagged via matched_via, and the
        response reads found-on-compilation, not not-found. LML#1026: the
        recall index is the single source on this path -- the index hit must
        also suppress TRACK_ON_COMPILATION's live-Discogs comp-discovery
        probes entirely (the Case-B primary is resolved from the index, and
        no live discovery call is spent rediscovering it)."""
        mock_library_db.search.return_value = []
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        lit_location = ResolvedLocation(row=_location_row(), shelf_item=lit_item)

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="Ikebana by Brian Reitzell",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            return_value=[lit_location],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        assert len(response.results) == 1
        item = response.results[0]
        assert item.library_item.id == 60654
        assert item.library_item.artist == "Soundtracks - L"
        assert item.library_item.title == "Lost in Translation"
        assert item.matched_via is not None
        assert item.matched_via[0].source == TrackMatchSource.discogs_release
        # Reads as found -- not the "not found in library" miss message.
        assert response.song_not_found is False
        assert response.found_on_compilation is True
        assert response.search_type == "compilation"
        assert response.context_message == 'Found "Ikebana" by Brian Reitzell on:'
        assert response.external_source == "library"
        # LML#1026: no live comp discovery on the union-active path. Step 2's
        # album-resolution probe (`resolve_albums_for_track`) legitimately
        # remains -- exactly one call, and never the strategy's Wave B
        # fingerprint (`artist_as_keyword=True`).
        assert mock_discogs_service.search_releases_by_track.await_count == 1
        assert not any(
            call.kwargs.get("artist_as_keyword")
            for call in mock_discogs_service.search_releases_by_track.await_args_list
        ), "the strategy's live comp-discovery pass must not fire on an index hit"

    @pytest.mark.asyncio
    async def test_low_priority_caller_keeps_the_legacy_live_comp_pass(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#1026 tri-state, the None branch: a class-5 caller never gets a
        union task, so TRACK_ON_COMPILATION runs its legacy live-Discogs
        comp-discovery pass byte-identically -- backfill/enrichment recall is
        untouched by the index-authoritative rework (and so is every caller
        when LML_LOCATION_UNION_ENABLED is off, the rollback lever)."""
        mock_library_db.search.return_value = []
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        with (
            patch("lookup.orchestrator.is_discogs_low_priority", return_value=True),
            patch(
                "lookup.orchestrator.resolve_track_shelf_locations", new_callable=AsyncMock
            ) as resolve_mock,
        ):
            await perform_lookup(
                LookupRequest(
                    artist="Brian Reitzell",
                    song="Ikebana",
                    raw_message="Ikebana by Brian Reitzell",
                ),
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        resolve_mock.assert_not_awaited()
        assert any(
            call.kwargs.get("artist_as_keyword")
            for call in mock_discogs_service.search_releases_by_track.await_args_list
        ), (
            "class-5 lookups must keep the legacy live comp-discovery pass "
            "(Wave B's artist_as_keyword=True call is its fingerprint)"
        )

    @pytest.mark.asyncio
    async def test_index_hit_skips_the_library_miss_probe(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#1026: with an album typed and no library results, step 3a used
        to live-search Discogs and synthesize a rowless id-0 'not in your
        library' item -- which _build_result_items ranks FIRST, preempting
        the folded shelf location as primary. An index hit must skip 3a: the
        fold supplies in-library rows, so the id-0 synthesis would be both a
        wasted live call and a wrong primary."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        # A confident Discogs match 3a WOULD synthesize if it ran.
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=12345,
                    album="Lost in Translation",
                    artist="Brian Reitzell",
                    artwork_url="https://example.com/lit.jpg",
                )
            ]
        )

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        lit_location = ResolvedLocation(row=_location_row(), shelf_item=lit_item)

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            album="Lost in Translation",
            raw_message="Ikebana by Brian Reitzell from Lost in Translation",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            return_value=[lit_location],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        ids = [r.library_item.id for r in response.results]
        assert 0 not in ids, f"step 3a's rowless id-0 row must not surface on an index hit: {ids}"
        assert response.results[0].library_item.id == 60654

    @pytest.mark.asyncio
    async def test_a_prime_prepends_the_location_ahead_of_trackless_artist_albums(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """A-prime: the artist's own album matched but does NOT carry the
        track (pre-fold song_not_found=True), and the track lives on a comp
        the index knows. The response reads found-on-compilation with a
        'Found ... on:' context -- so the row that actually carries the track
        must LEAD it, not trail an album that doesn't (the legacy live pass
        got this ordering by replacing prior results with the comp hit)."""
        other_album = make_library_item(
            id=41, artist="Brian Reitzell", title="Auto Music", call_letters="R"
        )
        mock_library_db.search.return_value = [other_album]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        lit_location = ResolvedLocation(row=_location_row(), shelf_item=lit_item)

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="Ikebana by Brian Reitzell",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            return_value=[lit_location],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        ids = [r.library_item.id for r in response.results]
        assert ids[0] == 60654, f"the track-bearing location must lead the response, got {ids}"
        assert 41 in ids, "the artist's own album stays in the response, demoted"
        assert response.found_on_compilation is True
        assert response.song_not_found is False

    @pytest.mark.asyncio
    async def test_overlap_with_existing_result_still_reconciles_signals(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#717 shape: the comp row already surfaced via the artist
        search's album-title leg, so the fold dedups the sole resolved
        location away (nothing appended). The index still proved the track
        is on the returned row -- the response must read found-on-compilation
        with the found context, not leak the pre-fold song_not_found=True."""
        lit_as_library_row = LibraryItem(
            id=60654, title="Lost in Translation", artist="Soundtracks - L"
        )
        mock_library_db.search.return_value = [lit_as_library_row]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        lit_location = ResolvedLocation(row=_location_row(), shelf_item=lit_as_library_row)

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="Ikebana by Brian Reitzell",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            return_value=[lit_location],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        ids = [r.library_item.id for r in response.results]
        assert ids.count(60654) == 1, f"the comp row must appear exactly once, got {ids}"
        assert response.song_not_found is False
        assert response.found_on_compilation is True
        assert response.search_type == "compilation"
        assert response.context_message == 'Found "Ikebana" by Brian Reitzell on:'

    @pytest.mark.asyncio
    async def test_degraded_probe_restores_the_legacy_live_pass_end_to_end(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """A probe failure means the index could not be consulted: the
        strategy must fall back to the full legacy live comp pass (Wave B's
        artist_as_keyword fingerprint observable) and the fold degrades to
        no locations -- never an exception, never a silent zeroing of comp
        recall."""
        mock_library_db.search.return_value = []
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="Ikebana by Brian Reitzell",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            side_effect=RuntimeError("probe exploded"),
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        assert any(
            call.kwargs.get("artist_as_keyword")
            for call in mock_discogs_service.search_releases_by_track.await_args_list
        ), "a degraded probe must restore the legacy live comp-discovery pass"
        assert response.degraded is False

    @pytest.mark.asyncio
    async def test_end_to_end_real_probe_rank_join_and_fold(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """LML#1026 nit 4: drive the REAL resolve_track_shelf_locations ->
        rank -> shelf-join -> build_location_result_items chain (the other
        union tests patch the probe at the orchestrator's import site),
        mocking only the recall-index read and the library-DB join."""
        mock_library_db.search.return_value = []
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        mock_library_db.get_items_by_ids = AsyncMock(return_value={60654: lit_item})

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="Ikebana by Brian Reitzell",
        )

        with patch(
            "lookup.location_union.get_compilation_track_locations",
            new_callable=AsyncMock,
            return_value=[_location_row()],
        ) as index_read:
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        index_read.assert_awaited_once()
        mock_library_db.get_items_by_ids.assert_awaited_once_with([60654])
        assert len(response.results) == 1
        item = response.results[0]
        assert item.library_item.id == 60654
        assert item.library_item.title == "Lost in Translation"
        assert item.artwork is not None
        assert item.artwork.release_id == 12345
        assert item.artwork.artwork_url == "https://example.com/lit.jpg"
        assert item.matched_via is not None
        assert item.matched_via[0].source == TrackMatchSource.discogs_release
        assert item.matched_via[0].position == "A3"
        assert response.found_on_compilation is True

    @pytest.mark.asyncio
    async def test_case_a_own_release_matched_plus_other_location_primary_first(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """'tommib by squarepusher' (LML#271 live-repro): the artist's own
        release (Go Plastic) already matches (Case A) -- the union still
        fires and appends the LiT location after it, but excludes Go
        Plastic's own library_id from the appended set (wxyc-shared's
        original 'every other ... location' contract, now enforced against
        the whole response). Primary always precedes appended locations."""
        go_plastic = make_library_item(
            id=5, artist="Squarepusher", title="Go Plastic", call_letters="S"
        )
        mock_library_db.search.return_value = [go_plastic]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_discogs_result(
                    release_id=999,
                    album="Go Plastic",
                    artist="Squarepusher",
                    artwork_url="https://example.com/go-plastic.jpg",
                )
            ]
        )

        own_item = make_library_item(id=5, artist="Squarepusher", title="Go Plastic")
        own_location = ResolvedLocation(
            row=_location_row(
                library_id=5,
                credit_role="primary",
                track_artist="squarepusher",
                track_title="tommib",
            ),
            shelf_item=own_item,
        )
        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        lit_location = ResolvedLocation(
            row=_location_row(
                library_id=60654,
                credit_role="extra",
                track_artist="squarepusher",
                track_title="tommib",
            ),
            shelf_item=lit_item,
        )

        request = LookupRequest(
            artist="Squarepusher",
            song="Tommib",
            raw_message="Tommib by Squarepusher",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            return_value=[own_location, lit_location],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        assert len(response.results) == 2
        # Primary always precedes appended locations.
        assert response.results[0].library_item.title == "Go Plastic"
        assert response.results[0].library_item.id == 5
        assert response.results[1].library_item.id == 60654
        assert response.results[1].library_item.title == "Lost in Translation"
        # A response the union contributed to reads as found-on-compilation,
        # whether or not the primary also matched directly.
        assert response.found_on_compilation is True
        assert response.song_not_found is False

    @pytest.mark.asyncio
    async def test_missing_shelf_item_location_is_skipped_not_crashed_on(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """A location row whose shelf join returned None (an individual miss,
        or the whole join degraded on a PG exception) is skipped, not crashed
        on -- the response still carries whichever other locations DID join
        successfully."""
        mock_library_db.search.return_value = []
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        joined = ResolvedLocation(row=_location_row(library_id=60654), shelf_item=lit_item)
        unjoined = ResolvedLocation(row=_location_row(library_id=999), shelf_item=None)

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="Ikebana by Brian Reitzell",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            return_value=[unjoined, joined],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        assert len(response.results) == 1
        assert response.results[0].library_item.id == 60654

    @pytest.mark.asyncio
    async def test_external_fallback_still_runs_and_dedups_against_synthetic_id_zero(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Appending locations happens AFTER step 7's external-cache fallback
        (not before it), so a primary miss with a resolvable location does
        not silently suppress the fallback -- and the fallback's synthetic
        id-0 row does not swallow a real (nonzero) location id in the dedup."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(
            return_value=[{"id": 99, "name": "Brian Reitzell", "score": 0.9}]
        )
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        lit_location = ResolvedLocation(row=_location_row(), shelf_item=lit_item)

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            include_external_caches=True,
            raw_message="Ikebana by Brian Reitzell",
        )

        with patch(
            "lookup.orchestrator.resolve_track_shelf_locations",
            new_callable=AsyncMock,
            return_value=[lit_location],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
                discogs_cache_pg=AsyncMock(),
            )

        # The fallback ran (not suppressed by the union having a location).
        assert response.external_source == "discogs"
        ids = [r.library_item.id for r in response.results]
        assert ids.count(0) == 1, f"expected exactly one synthetic external row, got {ids}"
        assert 60654 in ids, f"folded location must survive the id-0 dedup, got {ids}"
        assert len(response.results) == 2

    @pytest.mark.asyncio
    async def test_degraded_shed_path_still_carries_the_locations(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """A Discogs-breaker shed mid-tail is exactly when a DJ most needs the
        physical-shelf backups -- the degraded response must still carry the
        folded locations (already resolved, PG-only, no live enrichment
        needed), with found_on_compilation/song_not_found recomputed the same
        way the happy path does. context_message/external_source stay None,
        matching the pre-existing degraded contract."""
        from discogs.breaker import DiscogsBreakerOpenError

        mock_library_db.search.return_value = [queen_item]

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        lit_location = ResolvedLocation(
            row=_location_row(track_artist="queen", track_title="bohemian rhapsody"),
            shelf_item=lit_item,
        )

        request = LookupRequest(
            artist="Queen",
            song="Bohemian Rhapsody",
            album="A Night at the Opera",
            raw_message="Play Bohemian Rhapsody by Queen",
        )

        async def _shed(*_a, **_k):
            raise DiscogsBreakerOpenError("shed mid-enrichment")

        with (
            patch(
                "lookup.orchestrator.resolve_track_shelf_locations",
                new_callable=AsyncMock,
                return_value=[lit_location],
            ),
            patch("lookup.orchestrator._step_fetch_artwork", side_effect=_shed),
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.upstream_unavailable
        ids = [r.library_item.id for r in response.results]
        assert queen_item.id in ids
        assert 60654 in ids
        assert len(response.results) == 2
        assert response.found_on_compilation is True
        assert response.song_not_found is False
        # A location folded into a shed response must reconcile search_type to
        # "compilation" too -- not leak the pre-fold state.search_type, which
        # would contradict found_on_compilation=True (degraded-path M1).
        assert response.search_type == "compilation"
        assert response.context_message is None
        assert response.external_source is None

    @pytest.mark.asyncio
    async def test_stalled_probe_does_not_block_a_degraded_return(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """A degraded/shed early return bounds its await of the location-union
        task (~250ms) -- a probe that never resolves must not hang the
        response; its locations degrade to absent instead."""
        from discogs.breaker import DiscogsBreakerOpenError

        mock_library_db.search.return_value = [queen_item]

        async def _never_resolves(*_a, **_k):
            await asyncio.Event().wait()

        request = LookupRequest(
            artist="Queen",
            song="Bohemian Rhapsody",
            album="A Night at the Opera",
            raw_message="Play Bohemian Rhapsody by Queen",
        )

        async def _shed(*_a, **_k):
            raise DiscogsBreakerOpenError("shed mid-enrichment")

        with (
            patch("lookup.orchestrator.resolve_track_shelf_locations", new=_never_resolves),
            patch("lookup.orchestrator._step_fetch_artwork", side_effect=_shed),
            patch("lookup.orchestrator._DEGRADED_LOCATION_UNION_AWAIT_TIMEOUT_S", 0.05),
        ):
            response = await asyncio.wait_for(
                perform_lookup(
                    request,
                    mock_library_db,
                    mock_discogs_service,
                    telemetry,
                    discogs_cache_pg=AsyncMock(),
                ),
                timeout=5.0,
            )

        assert response.degraded is True
        assert len(response.results) == 1
        assert response.results[0].library_item.id == queen_item.id

    @pytest.mark.asyncio
    async def test_sentry_results_count_recomputed_post_fold(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """The Sentry `lookup.results_count`/`lookup.match_type` trace attrs
        must be recomputed from the FINAL post-fold result list, agreeing
        with the router's PostHog `results_count` (already `len(results)`).
        `_step_project_trace_attrs` runs before the fold and would otherwise
        leave a stale, smaller count -- this proves the later overwrite wins."""
        mock_library_db.search.return_value = []
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        lit_item = LibraryItem(id=60654, title="Lost in Translation", artist="Soundtracks - L")
        lit_location = ResolvedLocation(row=_location_row(), shelf_item=lit_item)

        request = LookupRequest(
            artist="Brian Reitzell",
            song="Ikebana",
            raw_message="Ikebana by Brian Reitzell",
        )

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with (
            patch(
                "lookup.orchestrator.resolve_track_shelf_locations",
                new_callable=AsyncMock,
                return_value=[lit_location],
            ),
            patch("lookup.orchestrator.sentry_sdk.get_current_scope", return_value=mock_scope),
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache_pg=AsyncMock(),
            )

        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert len(response.results) == 1
        assert calls["lookup.results_count"] == len(response.results) == 1
        assert calls["lookup.match_type"] == response.search_type == "compilation"


# ---------------------------------------------------------------------------
# Tests: perform_lookup - artwork
# ---------------------------------------------------------------------------


class TestPerformLookupArtwork:
    """Test artwork fetching for results."""

    @pytest.mark.asyncio
    async def test_fetches_artwork_for_results(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Results include artwork from Discogs."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None

        artwork = make_discogs_result(
            release_id=12345,
            album="A Night at the Opera",
            artist="Queen",
            artwork_url="https://example.com/cover.jpg",
        )
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[artwork])

        request = LookupRequest(
            artist="Queen",
            album="A Night at the Opera",
            raw_message="Play A Night at the Opera by Queen",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.results[0].artwork is not None
        assert response.results[0].artwork.artwork_url == "https://example.com/cover.jpg"


# ---------------------------------------------------------------------------
# Tests: perform_lookup - reconciled identity
# ---------------------------------------------------------------------------


class TestPerformLookupReconciledIdentity:
    """Test that perform_lookup populates reconciled_identity for results."""

    @pytest.mark.asyncio
    async def test_populates_reconciled_identity_when_entity_store_has_artist(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Each result gets ReconciledIdentity from EntityStore.get_identity."""
        from entity.store import Identity

        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(
            return_value=Identity(
                id=42,
                library_name="Queen",
                discogs_artist_id=7894,
                wikidata_qid="Q15862",
                spotify_artist_id="1dfeR4HaWDbWqFHLkxsg1d",
            )
        )

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 1
        identity = response.results[0].reconciled_identity
        assert identity is not None
        assert identity.discogs_artist_id == 7894
        assert identity.wikidata_qid == "Q15862"
        assert identity.spotify_artist_id == "1dfeR4HaWDbWqFHLkxsg1d"
        # Unset fields are None on the schema
        assert identity.musicbrainz_artist_id is None

    @pytest.mark.asyncio
    async def test_omits_reconciled_identity_when_entity_store_is_none(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """When EntityStore isn't configured, reconciled_identity is left None."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        # No entity_store kwarg — defaults to None
        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None

    @pytest.mark.asyncio
    async def test_omits_reconciled_identity_when_artist_unknown_to_entity_store(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """Artists that don't appear in entity.identity get None — not an error."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(return_value=None)

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None

    @pytest.mark.asyncio
    async def test_dedupes_lookup_per_artist_across_results(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item, queen_game_item
    ):
        """Two results for the same artist trigger only one EntityStore.get_identity call."""
        from entity.store import Identity

        mock_library_db.search.return_value = [queen_item, queen_game_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(
            return_value=Identity(id=1, library_name="Queen", discogs_artist_id=7894)
        )

        request = LookupRequest(artist="Queen", raw_message="Queen")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 2
        # Both results share the same reconciled identity
        assert response.results[0].reconciled_identity.discogs_artist_id == 7894
        assert response.results[1].reconciled_identity.discogs_artist_id == 7894
        # Only one DB lookup despite two results
        assert entity_store.get_identity.call_count == 1

    @pytest.mark.asyncio
    async def test_entity_store_exception_does_not_fail_lookup(
        self, mock_library_db, mock_discogs_service, telemetry, queen_item
    ):
        """A transient entity-store failure leaves reconciled_identity None
        rather than turning the whole /lookup response into a 500."""
        mock_library_db.search.return_value = [queen_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(side_effect=ConnectionError("entity DB down"))

        request = LookupRequest(artist="Queen", album="A Night at the Opera", raw_message="...")

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        # Lookup still succeeds; the field is just absent for the failed artist.
        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None

    @pytest.mark.asyncio
    async def test_compilation_entries_skip_identity_lookup(
        self, mock_library_db, mock_discogs_service, telemetry, compilation_item
    ):
        """Compilation entries (artist='Various Artists - ...') aren't keyed in
        entity.identity, so they get reconciled_identity=None — not an error."""
        mock_library_db.search.return_value = [compilation_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        entity_store = AsyncMock()
        entity_store.get_identity = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="Various Artists - Rock - D",
            album="Disco Not Disco",
            raw_message="...",
        )

        response = await perform_lookup(
            request, mock_library_db, mock_discogs_service, telemetry, entity_store=entity_store
        )

        assert len(response.results) == 1
        assert response.results[0].reconciled_identity is None


# ---------------------------------------------------------------------------
# Tests: perform_lookup - ambiguous format
# ---------------------------------------------------------------------------


class TestPerformLookupAmbiguousFormat:
    """Test handling of ambiguous 'X - Y' format messages."""

    @pytest.mark.asyncio
    async def test_tries_both_interpretations(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """For 'Artist - Title' format, tries both orderings."""
        amps_item = make_library_item(
            id=61692,
            artist="Amps for Christ",
            title="Circuits",
        )

        mock_library_db.find_similar_artist.return_value = None
        # Alternative search: first interpretation finds results
        # search_with_alternative_interpretation does 2 db.search calls:
        # 1. query="Amps for Christ Edward" -> filtered by "Amps for Christ" (part1)
        # 2. query="Edward Amps for Christ" -> filtered by "Edward" (part2)
        mock_library_db.search.side_effect = [
            [amps_item],  # interpretation 1: "Amps for Christ" as artist -> matches
            [],  # interpretation 2: "Edward" as artist -> no matches
        ]
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        request = LookupRequest(
            artist=None,
            song=None,
            raw_message="Amps for Christ - Edward",
        )

        response = await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        assert len(response.results) >= 1
        assert response.search_type == "alternative"


# ---------------------------------------------------------------------------
# Tests: perform_lookup - track on artist album + compilation
# ---------------------------------------------------------------------------


class TestArtistAlbumPlusCompilation:
    """Test that tracks found on both an artist album and a compilation return both results.

    Bug: "Poison Dart" by "The Bug" is on London Zoo (artist album, library ID 54324)
    AND The Sound of Dub (VA compilation, library ID 47808). The pipeline should
    return both, but currently only returns the compilation because TRACK_ON_COMPILATION
    replaces artist fallback results and track validation is skipped.
    """

    @pytest.mark.asyncio
    async def test_artist_album_included_when_compilation_also_found(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Both London Zoo (artist album) and The Sound of Dub (compilation) should be returned.

        Scenario:
        1. resolve_albums_for_track returns nothing (Discogs track lookup fails/empty)
        2. ARTIST_PLUS_ALBUM: artist+song empty, falls back to artist-only → Bug albums
        3. TRACK_ON_COMPILATION: finds The Sound of Dub (compilation)
        4. Track validation should confirm London Zoo contains "Poison Dart"
        5. Both London Zoo and The Sound of Dub should appear in results
        """
        from discogs.models import ReleaseInfo, TrackReleasesResponse

        london_zoo = make_library_item(
            id=54324,
            artist="The Bug",
            title="London Zoo",
            call_letters="B",
            genre="Electronic",
        )
        pressure = make_library_item(
            id=54325,
            artist="The Bug",
            title="Pressure",
            call_letters="B",
            genre="Electronic",
            release_call_number=2,
        )
        sound_of_dub = make_library_item(
            id=47808,
            artist="various",
            title="The Sound of Dub",
            call_letters="V",
            genre="Reggae",
        )

        mock_library_db.find_similar_artist.return_value = None

        # db.search call order:
        # 1. search_library_with_fallback: artist+song "The Bug Poison Dart" → []
        # 2. search_library_with_fallback: artist only "The Bug" → [london_zoo, pressure]
        # 3. search_compilations_for_track: keyword "poison dart" → []
        # 4. search_album_fuzzy: "The Sound of Dub" → [sound_of_dub]
        mock_library_db.search.side_effect = [
            [],  # artist + song
            [london_zoo, pressure],  # artist only fallback
            [],  # keyword search (no library match for "poison dart")
            [sound_of_dub],  # search_album_fuzzy for Discogs album title
        ]

        # Discogs: search_releases_by_track finds The Sound of Dub (compilation)
        mock_discogs_service.search_releases_by_track = AsyncMock(
            return_value=TrackReleasesResponse(
                track="Poison Dart",
                artist="The Bug",
                releases=[
                    ReleaseInfo(
                        album="The Sound of Dub",
                        artist="Various Artists",
                        release_id=2308471,
                        release_url="https://www.discogs.com/release/2308471",
                        is_compilation=True,
                    ),
                ],
                total=1,
                cached=False,
            )
        )

        # Track validation: "Poison Dart" IS on London Zoo, NOT on Pressure
        london_zoo_discogs = make_discogs_result(
            release_id=1395903, album="London Zoo", artist="The Bug"
        )
        pressure_discogs = make_discogs_result(release_id=9999, album="Pressure", artist="The Bug")

        async def mock_discogs_search(request):
            album = request.album if hasattr(request, "album") else ""
            if album and "london" in album.lower():
                return DiscogsSearchResponse(results=[london_zoo_discogs])
            if album and "pressure" in album.lower():
                return DiscogsSearchResponse(results=[pressure_discogs])
            return DiscogsSearchResponse(results=[])

        mock_discogs_service.search = AsyncMock(side_effect=mock_discogs_search)

        async def mock_validate(release_id, track, artist):
            # Poison Dart is on London Zoo (1395903) and The Sound of Dub (2308471)
            return release_id in (1395903, 2308471)

        mock_discogs_service.validate_track_on_release = AsyncMock(side_effect=mock_validate)
        mock_discogs_service.get_release = AsyncMock(return_value=None)

        request = LookupRequest(
            artist="The Bug",
            song="Poison Dart",
            raw_message="poison dart, the bug",
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],  # Discogs track lookup returns nothing
        ):
            response = await perform_lookup(
                request, mock_library_db, mock_discogs_service, telemetry
            )

        titles = [r.library_item.title for r in response.results]
        assert "London Zoo" in titles, (
            f"Artist album 'London Zoo' should be in results, got: {titles}"
        )
        assert "The Sound of Dub" in titles, (
            f"Compilation 'The Sound of Dub' should be in results, got: {titles}"
        )
        assert response.found_on_compilation is True
        # Artist's own album should come first
        assert titles.index("London Zoo") < titles.index("The Sound of Dub")


# ---------------------------------------------------------------------------
# Tests: perform_lookup - external cache fallback (include_external_caches)
# ---------------------------------------------------------------------------


class TestPerformLookupExternalCacheFallback:
    """include_external_caches=True falls back to discogs/MB when library is empty.

    Used by the lossy-mojibake matcher in tubafrenzy to recover canonical artist
    names that aren't in the WXYC library catalog (~99% of the lossy bucket).
    """

    @pytest.mark.asyncio
    async def test_default_off_does_not_query_external(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Existing callers (no flag) see no behavior change and no external query."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(artist="Whoever", raw_message="Whoever")

        response = await perform_lookup(
            request,
            mock_library_db,
            mock_discogs_service,
            telemetry,
            discogs_cache=discogs_cache,
            mb_pg=mb_pg,
        )

        assert response.results == []
        assert response.external_source is None
        discogs_cache.search_artists_by_name.assert_not_called()
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_library_hit_marks_source_library_and_skips_external(
        self,
        mock_library_db,
        mock_discogs_service,
        telemetry,
        stereolab_item,
    ):
        """When the library returns results, external_source='library' and external caches aren't queried."""
        mock_library_db.search.return_value = [stereolab_item]
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Stereolab",
            album="Emperor Tomato Ketchup",
            raw_message="Stereolab",
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

        assert len(response.results) == 1
        assert response.external_source == "library"
        discogs_cache.search_artists_by_name.assert_not_called()
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_discogs_when_library_empty(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """No library hit + flag set + discogs cache hit -> synthetic LookupResultItem."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(
            return_value=[
                {"id": 99, "name": "Astrid Øster Mortensen", "score": 0.71},
            ]
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
        assert response.results[0].library_item.artist == "Astrid Øster Mortensen"
        # No artwork enrichment for external candidates
        assert response.results[0].artwork is None
        # MB never queried because discogs hit
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_musicbrainz_when_discogs_empty(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(
            return_value=[
                {"id": "mb-uuid", "name": "Csillagrablók", "score": 0.83},
            ]
        )

        request = LookupRequest(
            artist="Csillagrablok",
            raw_message="Csillagrablok",
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

        assert response.external_source == "musicbrainz"
        assert len(response.results) == 1
        assert response.results[0].library_item.artist == "Csillagrablók"

    @pytest.mark.asyncio
    async def test_external_source_none_when_nothing_matches(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="ZZZ Nothing",
            raw_message="ZZZ Nothing",
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

        assert response.results == []
        assert response.external_source is None

    @pytest.mark.asyncio
    async def test_no_external_query_when_no_typed_field(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Phase 1.7: a bare raw_message with no typed field (LABEL_NAME case) skips fallback."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        discogs_cache.search_releases_by_title = AsyncMock(return_value=[])
        discogs_cache.search_tracks_by_title = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            raw_message="ESP Disk'",
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

        assert response.external_source is None
        discogs_cache.search_artists_by_name.assert_not_called()
        discogs_cache.search_releases_by_title.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_not_called()
        mb_pg.fetchall.assert_not_called()

    @pytest.mark.asyncio
    async def test_album_skeleton_falls_back_to_discogs_releases(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Phase 1.7: RELEASE_TITLE skeleton -> discogs release fuzzy hit -> synthetic result."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        discogs_cache.search_releases_by_title = AsyncMock(
            return_value=[
                {"id": 12345, "title": "DOGA", "artist": "Juana Molina", "score": 0.81},
            ]
        )
        discogs_cache.search_tracks_by_title = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            album="DOG",
            raw_message="DOG",
            include_external_caches=True,
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
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
        item = response.results[0].library_item
        assert item.artist == "Juana Molina"
        assert item.title == "DOGA"
        # Artist branch must NOT have fired; only release branch.
        discogs_cache.search_artists_by_name.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_not_called()
        discogs_cache.search_releases_by_title.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_song_skeleton_falls_back_to_discogs_tracks(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """Phase 1.7: SONG_TITLE skeleton -> discogs track fuzzy hit -> synthetic result."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(return_value=[])
        discogs_cache.search_releases_by_title = AsyncMock(return_value=[])
        discogs_cache.search_tracks_by_title = AsyncMock(
            return_value=[
                {
                    "id": 555,
                    "title": "Back, Baby",
                    "artist": "Jessica Pratt",
                    "score": 0.92,
                },
            ]
        )
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            song="Back Baby",
            raw_message="Back Baby",
            include_external_caches=True,
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
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
        item = response.results[0].library_item
        assert item.artist == "Jessica Pratt"
        assert item.title == "Back, Baby"
        discogs_cache.search_artists_by_name.assert_not_called()
        discogs_cache.search_releases_by_title.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_artist_takes_precedence_over_album_when_both_present(
        self, mock_library_db, mock_discogs_service, telemetry
    ):
        """When the request supplies both artist AND album, artist branch runs first."""
        mock_library_db.search.return_value = []
        mock_library_db.find_similar_artist.return_value = None
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])

        discogs_cache = AsyncMock()
        discogs_cache.search_artists_by_name = AsyncMock(
            return_value=[{"id": 1, "name": "Stereolab", "score": 0.9}]
        )
        discogs_cache.search_releases_by_title = AsyncMock(return_value=[])
        discogs_cache.search_tracks_by_title = AsyncMock(return_value=[])
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        request = LookupRequest(
            artist="Stereolab",
            album="Aluminum Tunes",
            raw_message="Stereolab Aluminum Tunes",
            include_external_caches=True,
        )

        with patch(
            "lookup.orchestrator.lookup_releases_by_track",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await perform_lookup(
                request,
                mock_library_db,
                mock_discogs_service,
                telemetry,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
            )

        assert response.external_source == "discogs"
        discogs_cache.search_artists_by_name.assert_awaited_once()
        discogs_cache.search_releases_by_title.assert_not_called()
        discogs_cache.search_tracks_by_title.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: LookupState result precedence properties
# ---------------------------------------------------------------------------


class TestLookupStateResultPrecedence:
    """LookupState.result_count / has_results encode the response precedence rule.

    ``items_with_artwork`` takes precedence over ``library_results`` when
    non-empty — the same rule ``_build_result_items`` dispatches on.
    """

    @pytest.mark.parametrize(
        ("n_artwork_pairs", "n_library_rows", "expected_count"),
        [
            pytest.param(2, 0, 2, id="items_with_artwork_only"),
            pytest.param(0, 3, 3, id="library_results_only"),
            pytest.param(1, 3, 1, id="both_populated_artwork_wins"),
            pytest.param(0, 0, 0, id="neither"),
        ],
    )
    def test_result_count_and_has_results(self, n_artwork_pairs, n_library_rows, expected_count):
        state = LookupState()
        state.items_with_artwork = [
            (make_library_item(id=100 + i), make_discogs_result(release_id=200 + i))
            for i in range(n_artwork_pairs)
        ]
        state.library_results = [make_library_item(id=i + 1) for i in range(n_library_rows)]

        assert state.result_count == expected_count
        assert state.has_results is (expected_count > 0)


class TestLookupStatePromotedSearchStateFields:
    """LML#750: artist_fallback_results / matched_via_by_id / timed_out ride on
    ``LookupState`` instead of escaping ``_step_search_pipeline`` on the raw
    ``SearchState``."""

    def test_defaults(self):
        state = LookupState()
        assert state.artist_fallback_results == []
        assert state.matched_via_by_id == {}
        assert state.timed_out is False


class TestUnionProbeExceptionRetrieval:
    """The done-callback that keeps a failed probe task from logging 'Task
    exception was never retrieved' when an unexpected error aborts
    perform_lookup before any await site consumes the task (LML#1026 sweep
    finding)."""

    @pytest.mark.asyncio
    async def test_failed_task_exception_is_marked_retrieved(self):
        from lookup.orchestrator import _retrieve_union_probe_exception

        async def _boom():
            raise RuntimeError("probe exploded")

        task = asyncio.create_task(_boom())
        task.add_done_callback(_retrieve_union_probe_exception)
        with pytest.raises(RuntimeError):
            await task
        # Callbacks run via call_soon; give the loop one tick, then verify
        # the exception reads as already-retrieved (no GC warning pending).
        await asyncio.sleep(0)
        assert isinstance(task.exception(), RuntimeError)

    @pytest.mark.asyncio
    async def test_cancelled_task_is_left_alone(self):
        from lookup.orchestrator import _retrieve_union_probe_exception

        async def _pending():
            await asyncio.Event().wait()

        task = asyncio.create_task(_pending())
        task.add_done_callback(_retrieve_union_probe_exception)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert task.cancelled()
