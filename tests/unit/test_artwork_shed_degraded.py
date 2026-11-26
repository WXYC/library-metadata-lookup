"""LML#1398: an artwork breaker shed surfaces as ``degraded``, not "no artwork".

``lookup/artwork.py``'s ``fetch_one`` used to wrap its whole body in
``except Exception`` and return ``None``. That includes ``BreakerOpenError``
(and so ``DiscogsBreakerOpenError``), which ``core/exceptions.py`` documents as
**"couldn't ask, try later -- never a confirmed miss."** A transient outage
therefore went out carrying the byte-identical shape of a genuine "LML matched
the album and Discogs has no cover" answer: ``artwork: null, degraded: false``.
Callers negative-cache that shape -- WXYC/Backend-Service#2522's artwork marker
and #2743's 7-day rotation-picker stamp.

The fix follows LML#1420's model for the top-1 enrichment leg: ``fetch_one``
keeps the per-item ``None`` (there is no artwork to show) but records the shed
on an ``ArtworkFetchOutcome``; ``LookupState.artwork_shed`` carries it to the
response build, which emits ``degraded: true, degraded_reason:
upstream_unavailable``. The other items in the same ``gather`` keep theirs.

Two transient shapes are pinned, because only one of them raises:
``DiscogsService.search`` *catches* the breaker and returns ``None`` (LML#918's
"degraded, so ``@async_cached`` must not memoize it" contract), while the lazy
release-resolution probe (``resolve_release_for_track``) re-raises it. The
``None`` shape is the one a real OPEN breaker takes on the primary search path,
so both must mark the request. The raise is exercised by giving the mocked
``search`` a ``DiscogsBreakerOpenError`` side effect: it is a stand-in for any
``BreakerOpenError`` raised anywhere in ``fetch_one``'s try body, which is the
contract the ``except`` arm owns.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import get_settings
from core.dependencies import (
    get_discogs_cache_pg,
    get_discogs_cache_service,
    get_discogs_service,
    get_library_db,
    get_musicbrainz_pg,
    get_posthog_client,
)
from core.search import SearchState
from discogs.breaker import DiscogsBreakerOpenError
from discogs.models import DiscogsSearchResponse
from generated.api_models import DegradedReason
from identity.dependencies import get_entity_store
from library.models import LibraryItem
from lookup.artwork import fetch_artwork_for_items
from lookup.artwork_shed import ArtworkFetchOutcome
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from main import app
from streaming.dependencies import get_apple_music_client, get_spotify_client
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result, make_library_item
from tests.unit.conftest import override_deps

SHED_ARTIST = "Juana Molina"
SHED_TITLE = "DOGA"
SHED_ARTWORK = "https://img.discogs.com/shed-cover.jpg"
HELD_ARTIST = "Juana Molina"
# Scored against the request album by the LML#477 title gate in
# ``enrich_one``; token_set_ratio("DOGA", "DOGA (Remastered)") == 100, so the
# held row keeps its fetched artwork instead of collapsing to the sentinel.
HELD_TITLE = "DOGA (Remastered)"
HELD_ARTWORK = "https://img.discogs.com/held-cover.jpg"


@pytest.fixture
def two_items() -> list[LibraryItem]:
    return [
        make_library_item(id=41, artist=SHED_ARTIST, title=SHED_TITLE),
        make_library_item(id=42, artist=HELD_ARTIST, title=HELD_TITLE),
    ]


def _held_result() -> DiscogsSearchResponse:
    return DiscogsSearchResponse(
        results=[
            make_discogs_result(
                release_id=9002,
                album=HELD_TITLE,
                artist=HELD_ARTIST,
                artwork_url=HELD_ARTWORK,
            )
        ]
    )


def _shed_result() -> DiscogsSearchResponse:
    return DiscogsSearchResponse(
        results=[
            make_discogs_result(
                release_id=9001,
                album=SHED_TITLE,
                artist=SHED_ARTIST,
                artwork_url=SHED_ARTWORK,
            )
        ]
    )


def _arm_search(mock_discogs_service, shed_shape: str | None) -> None:
    """Answer each item's own query; make the shed item fail in ``shed_shape``.

    ``shed_shape`` is ``"breaker"`` (a raised ``DiscogsBreakerOpenError``, the
    trust-bind/fallback shape), ``"degraded_none"`` (the ``None``
    ``DiscogsService.search`` returns for a shed / rate-limit / 5xx, LML#918),
    ``"generic_error"`` (a non-shed exception -- must keep the old behaviour),
    or ``None`` (healthy control).
    """

    async def search(request):
        if request.album == SHED_TITLE:
            if shed_shape == "breaker":
                raise DiscogsBreakerOpenError("Discogs saturation breaker open")
            if shed_shape == "degraded_none":
                return None
            if shed_shape == "generic_error":
                raise RuntimeError("unexpected")
            return _shed_result()
        return _held_result()

    mock_discogs_service.search.side_effect = search


class TestFetchArtworkOutcome:
    """``fetch_artwork_for_items`` exposes the shed via its out-parameter."""

    @pytest.mark.asyncio
    async def test_breaker_shed_marks_outcome_and_keeps_other_items(
        self, mock_discogs_service, two_items
    ):
        """The raised shape: one item sheds, the gather still returns both."""
        _arm_search(mock_discogs_service, "breaker")
        outcome = ArtworkFetchOutcome()

        results = await fetch_artwork_for_items(two_items, mock_discogs_service, outcome=outcome)

        assert [item.id for item, _ in results] == [41, 42]
        assert results[0][1] is None
        assert results[1][1] is not None
        assert results[1][1].artwork_url == HELD_ARTWORK
        assert outcome.upstream_shed is True

    @pytest.mark.asyncio
    async def test_degraded_search_none_marks_outcome(self, mock_discogs_service, two_items):
        """LML#918: ``search`` returning ``None`` is "couldn't ask", not a miss.

        This is the shape a real OPEN breaker takes on the search path --
        ``DiscogsService.search`` catches the shed itself -- so a fix that only
        handles the raised exception would leave the primary path laundering.
        """
        _arm_search(mock_discogs_service, "degraded_none")
        outcome = ArtworkFetchOutcome()

        results = await fetch_artwork_for_items(two_items, mock_discogs_service, outcome=outcome)

        assert results[0][1] is None
        assert results[1][1] is not None
        assert results[1][1].artwork_url == HELD_ARTWORK
        assert outcome.upstream_shed is True

    @pytest.mark.asyncio
    async def test_genuine_empty_search_is_not_a_shed(self, mock_discogs_service):
        """A real 200-with-no-results is a confirmed miss: ``None`` is distinct."""
        mock_discogs_service.search.return_value = DiscogsSearchResponse(results=[])
        outcome = ArtworkFetchOutcome()

        results = await fetch_artwork_for_items(
            [make_library_item(id=41, artist=SHED_ARTIST, title=SHED_TITLE)],
            mock_discogs_service,
            outcome=outcome,
        )

        assert results[0][1] is None
        assert outcome.upstream_shed is False

    @pytest.mark.asyncio
    async def test_generic_exception_keeps_current_behaviour(self, mock_discogs_service):
        """A non-shed failure still logs and returns ``None``, and is not degraded."""
        _arm_search(mock_discogs_service, "generic_error")
        outcome = ArtworkFetchOutcome()

        results = await fetch_artwork_for_items(
            [make_library_item(id=41, artist=SHED_ARTIST, title=SHED_TITLE)],
            mock_discogs_service,
            outcome=outcome,
        )

        assert results[0][1] is None
        assert outcome.upstream_shed is False

    @pytest.mark.asyncio
    async def test_healthy_fetch_is_not_a_shed(self, mock_discogs_service, two_items):
        _arm_search(mock_discogs_service, None)
        outcome = ArtworkFetchOutcome()

        results = await fetch_artwork_for_items(two_items, mock_discogs_service, outcome=outcome)

        assert [art.artwork_url if art is not None else None for _, art in results] == [
            SHED_ARTWORK,
            HELD_ARTWORK,
        ]
        assert outcome.upstream_shed is False

    @pytest.mark.asyncio
    async def test_breaker_shed_never_reaches_logger_exception(self, mock_discogs_service):
        """LML#1192 rule kept: a shed is expected degrade, not a Sentry event."""
        _arm_search(mock_discogs_service, "breaker")

        with patch("lookup.artwork.logger") as mock_logger:
            await fetch_artwork_for_items(
                [make_library_item(id=41, artist=SHED_ARTIST, title=SHED_TITLE)],
                mock_discogs_service,
                outcome=ArtworkFetchOutcome(),
            )

        mock_logger.exception.assert_not_called()

    @pytest.mark.asyncio
    async def test_outcome_is_optional(self, mock_discogs_service):
        """Existing callers that pass no outcome keep working (and don't crash)."""
        _arm_search(mock_discogs_service, "breaker")

        results = await fetch_artwork_for_items(
            [make_library_item(id=41, artist=SHED_ARTIST, title=SHED_TITLE)],
            mock_discogs_service,
        )

        assert results[0][1] is None


class TestArtworkShedMarksResponseDegraded:
    """The real ``perform_lookup`` turns a shed into ``degraded: true``."""

    async def _lookup(self, mock_library_db, mock_discogs_service, two_items):
        request = LookupRequest(
            artist=SHED_ARTIST,
            album=SHED_TITLE,
            raw_message=f"{SHED_ARTIST} - {SHED_TITLE}",
        )
        mock_discogs_service.get_artist_image = AsyncMock(return_value=None)
        with patch(
            "lookup.orchestrator.execute_search_pipeline",
            AsyncMock(return_value=SearchState(results=two_items)),
        ):
            return await perform_lookup(
                request, mock_library_db, mock_discogs_service, make_lml_telemetry()
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shed_shape", ["breaker", "degraded_none"])
    async def test_shed_marks_degraded_and_keeps_other_artwork(
        self, mock_library_db, mock_discogs_service, two_items, shed_shape
    ):
        _arm_search(mock_discogs_service, shed_shape)

        response = await self._lookup(mock_library_db, mock_discogs_service, two_items)

        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.upstream_unavailable
        # A shed is not a timeout.
        assert response.timeout is False
        # Still a match for both rows -- the shed cost one row's artwork only.
        assert len(response.results) == 2
        by_id = {row.library_item.id: row for row in response.results}
        shed = by_id[41].artwork
        # Per-row shape unchanged (issue constraint): enrichment synthesizes a
        # ``None`` artwork into the BS#1185 ``release_id=0`` sentinel, which is
        # exactly the "no Discogs identity" shape callers negative-cache. The
        # shed is distinguishable only by the response-level ``degraded`` flag
        # -- the difference this fix adds.
        assert shed is not None
        assert shed.release_id == 0
        assert shed.artwork_url is None
        held = by_id[42].artwork
        assert held is not None
        assert held.artwork_url == HELD_ARTWORK
        assert held.release_id == 9002

    @pytest.mark.asyncio
    async def test_healthy_fetch_is_not_degraded(
        self, mock_library_db, mock_discogs_service, two_items
    ):
        """Control: both fetches answer -> the pre-#1398 healthy shape."""
        _arm_search(mock_discogs_service, None)

        response = await self._lookup(mock_library_db, mock_discogs_service, two_items)

        by_id = {row.library_item.id: row for row in response.results}
        assert by_id[41].artwork.artwork_url == SHED_ARTWORK
        assert by_id[42].artwork.artwork_url == HELD_ARTWORK
        assert response.degraded is False
        assert response.degraded_reason is None

    @pytest.mark.asyncio
    async def test_generic_exception_is_not_degraded(
        self, mock_library_db, mock_discogs_service, two_items
    ):
        """Decision (LML#1398): the generic ``except Exception`` arm is NOT
        marked degraded. ``DiscogsService`` already converts Discogs HTTP
        failures into the ``None``/empty-result shapes before they reach
        ``fetch_one``, so what lands there is an unexpected error (a bug, a
        malformed payload) -- naming it ``upstream_unavailable`` would mislabel
        it, and a deterministic failure flagged degraded would make every
        re-asking caller retry it forever. It keeps the log-and-``None`` shape
        (and the same reasoning LML#1420 applied to top1's generic arm)."""
        _arm_search(mock_discogs_service, "generic_error")

        response = await self._lookup(mock_library_db, mock_discogs_service, two_items)

        by_id = {row.library_item.id: row for row in response.results}
        shed = by_id[41].artwork
        assert shed is not None
        assert shed.release_id == 0
        assert shed.artwork_url is None
        assert by_id[42].artwork is not None
        assert response.degraded is False
        assert response.degraded_reason is None


class TestLookupEndpointReportsArtworkShed:
    """The wire contract: ``/api/v1/lookup`` says ``degraded`` on a shed."""

    @pytest.fixture
    def app_client(self, mock_library_db, mock_discogs_service, mock_settings):
        with override_deps(
            app,
            {
                get_library_db: mock_library_db,
                get_discogs_service: mock_discogs_service,
                get_discogs_cache_service: None,
                get_musicbrainz_pg: None,
                get_entity_store: None,
                get_discogs_cache_pg: None,
                get_posthog_client: None,
                get_apple_music_client: None,
                get_spotify_client: None,
                get_settings: mock_settings,
            },
        ):
            yield app

    @pytest.mark.asyncio
    async def test_lookup_json_is_degraded_and_keeps_sibling_artwork(
        self, app_client, mock_library_db, mock_discogs_service, two_items
    ):
        _arm_search(mock_discogs_service, "degraded_none")
        mock_discogs_service.get_artist_image = AsyncMock(return_value=None)

        with patch(
            "lookup.orchestrator.execute_search_pipeline",
            AsyncMock(return_value=SearchState(results=two_items)),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup",
                    json={"artist": SHED_ARTIST, "album": SHED_TITLE},
                )

        assert resp.status_code == 200
        body = resp.json()
        # The distinction this fix adds: the response is explicitly degraded
        # (``upstream_unavailable``) rather than a clean ``direct`` match whose
        # null artwork is byte-identical to a confirmed miss.
        assert body["degraded"] is True
        assert body["degraded_reason"] == "upstream_unavailable"
        assert body["timeout"] is False
        artwork_by_id = {row["library_item"]["id"]: row["artwork"] for row in body["results"]}
        assert artwork_by_id[42]["artwork_url"] == HELD_ARTWORK
        assert artwork_by_id[41]["artwork_url"] is None
