"""LML#1420: a top-1 enrichment breaker shed surfaces as ``degraded`` on the wire.

``lookup/enrichment/top1.py`` swallows ``DiscogsBreakerOpenError`` on two steps
(the release fetch and the artist-details fetch). Before LML#1420 neither branch
told the response build, so a shed went out as ``artist_bio: null`` with
``degraded: false`` -- byte-identical to "this artist has no Discogs profile".
The search leg's LML#1126 precedent (``SearchState.upstream_shed`` ->
``LookupState.upstream_shed`` -> ``degraded: true, degraded_reason:
upstream_unavailable``) is the model these tests pin for the enrichment leg.

These drive the REAL ``perform_lookup`` (only the search pipeline, library DB
and Discogs service are mocked) so the whole chain -- top1 branch -> coordinator
-> ``LookupState`` -> response build -- is exercised, not just one hop.
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
from discogs.models import (
    ArtistDetails,
    DiscogsSearchResponse,
    ReleaseMetadataResponse,
)
from generated.api_models import DegradedReason
from identity.dependencies import get_entity_store
from library.models import LibraryItem
from lookup.enrichment.wikipedia_bio import BioSource, ServedBioResolution
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from main import app
from streaming.dependencies import get_apple_music_client, get_spotify_client
from tests.conftest import make_lml_telemetry
from tests.factories import make_discogs_result
from tests.unit.conftest import override_deps

RELEASE_ID = 7001
ARTIST_ID = 4242


@pytest.fixture
def juana_item() -> LibraryItem:
    return LibraryItem(
        id=31,
        artist="Juana Molina",
        title="DOGA",
        call_letters="M",
        artist_call_number=1,
        release_call_number=1,
        genre="Rock",
        format="CD",
    )


def _release() -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=RELEASE_ID,
        title="DOGA",
        artist="Juana Molina",
        year=2024,
        artist_id=ARTIST_ID,
        release_url=f"https://www.discogs.com/release/{RELEASE_ID}",
        artwork_url="https://img.discogs.com/doga-cover.jpg",
    )


def _artist_details() -> ArtistDetails:
    return ArtistDetails(
        artist_id=ARTIST_ID,
        name="Juana Molina",
        profile="Argentine singer-songwriter and former comedy actress.",
        urls=[],
    )


def _arm_discogs(mock_discogs_service, step: str) -> None:
    """Search returns DOGA; ``step`` picks which top-1 call fails and how.

    ``step`` is ``"release"``/``"artist"`` (a breaker shed on that call),
    ``"release_error"`` (a non-shed exception on the release call), or
    ``"none"`` (both succeed -- the healthy control).
    """
    mock_discogs_service.search.return_value = DiscogsSearchResponse(
        results=[
            make_discogs_result(
                release_id=RELEASE_ID,
                album="DOGA",
                artist="Juana Molina",
                artwork_url="https://img.discogs.com/doga-cover.jpg",
            )
        ]
    )
    mock_discogs_service.get_artist_image = AsyncMock(return_value=None)
    mock_discogs_service.get_release = AsyncMock(return_value=_release())
    mock_discogs_service.get_artist_details = AsyncMock(return_value=_artist_details())
    if step == "release":
        mock_discogs_service.get_release.side_effect = DiscogsBreakerOpenError("shed")
    elif step == "artist":
        mock_discogs_service.get_artist_details.side_effect = DiscogsBreakerOpenError("shed")
    elif step == "release_error":
        mock_discogs_service.get_release.side_effect = RuntimeError("unexpected")


async def _lookup(mock_library_db, mock_discogs_service, juana_item):
    request = LookupRequest(artist="Juana Molina", album="DOGA", raw_message="Juana Molina - DOGA")
    with patch(
        "lookup.orchestrator.execute_search_pipeline",
        AsyncMock(return_value=SearchState(results=[juana_item])),
    ):
        return await perform_lookup(
            request, mock_library_db, mock_discogs_service, make_lml_telemetry()
        )


class TestTop1ShedMarksResponseDegraded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("step", "expected_year"),
        [
            # Nothing fetched yet when the release call sheds: every field drops.
            pytest.param("release", None, id="release-step-shed"),
            # LML#1049: the artist-step shed keeps the release-level year.
            pytest.param("artist", 2024, id="artist-step-shed"),
        ],
    )
    async def test_shed_marks_degraded_and_keeps_content(
        self, mock_library_db, mock_discogs_service, juana_item, step, expected_year
    ):
        _arm_discogs(mock_discogs_service, step)

        response = await _lookup(mock_library_db, mock_discogs_service, juana_item)

        assert response.degraded is True
        assert response.degraded_reason == DegradedReason.upstream_unavailable
        # Content unchanged: still a match, the bio is what the shed cost.
        assert len(response.results) == 1
        artwork = response.results[0].artwork
        assert artwork is not None
        assert artwork.release_id == RELEASE_ID
        assert artwork.artist_bio is None
        assert artwork.release_year == expected_year
        # A shed is not a timeout.
        assert response.timeout is False

    @pytest.mark.asyncio
    async def test_healthy_top1_is_not_degraded(
        self, mock_library_db, mock_discogs_service, juana_item
    ):
        """Control: both top-1 calls succeed -> the pre-#1420 healthy shape."""
        _arm_discogs(mock_discogs_service, "none")

        response = await _lookup(mock_library_db, mock_discogs_service, juana_item)

        assert response.results[0].artwork.artist_bio is not None
        assert response.degraded is False
        assert response.degraded_reason is None

    @pytest.mark.asyncio
    async def test_generic_exception_is_not_degraded(
        self, mock_library_db, mock_discogs_service, juana_item
    ):
        """Decision (LML#1420): the generic ``except Exception`` in top1.py is
        NOT marked degraded. ``DiscogsService`` already converts Discogs HTTP
        failures into ``None`` before they reach top1, so what lands in that
        branch is an unexpected error (a bug, a malformed payload) -- naming it
        ``upstream_unavailable`` would mislabel it, and a deterministic failure
        flagged degraded would make every re-asking caller retry it forever.
        It stays a ``logger.exception`` Sentry event instead."""
        _arm_discogs(mock_discogs_service, "release_error")

        with patch("lookup.enrichment.top1.logger") as mock_logger:
            response = await _lookup(mock_library_db, mock_discogs_service, juana_item)

        mock_logger.exception.assert_called_once()
        assert response.results[0].artwork.artist_bio is None
        assert response.degraded is False
        assert response.degraded_reason is None

    @pytest.mark.asyncio
    async def test_shed_never_reaches_logger_exception(
        self, mock_library_db, mock_discogs_service, juana_item
    ):
        """LML#1192 rule kept: reporting the shed must not route it through
        ``logger.exception`` (the LML#755 Sentry flood)."""
        _arm_discogs(mock_discogs_service, "release")

        with patch("lookup.enrichment.top1.logger") as mock_logger:
            response = await _lookup(mock_library_db, mock_discogs_service, juana_item)

        mock_logger.exception.assert_not_called()
        assert response.degraded is True

    @pytest.mark.asyncio
    async def test_artist_shed_with_served_bio_is_not_degraded(
        self, mock_library_db, mock_discogs_service, juana_item
    ):
        """Decision (LML#1420): only mark degraded when the shed actually cost
        the response its bio. If a bio still reaches the wire (the
        ``LML_BIO_PREFER_WIKIPEDIA`` path serving a cached Wikipedia extract),
        the response is not degraded. Unreachable through today's cache key
        (the Wikipedia row is keyed on the shed step's own ``artist_id``), so
        the served pair is injected directly to pin the rule."""
        _arm_discogs(mock_discogs_service, "artist")
        served = ServedBioResolution(
            bio="Juana Molina is an Argentine musician.",
            wiki_url="https://en.wikipedia.org/wiki/Juana_Molina",
            source=BioSource.WIKIPEDIA,
            miss_details=None,
            discogs_artist_id=None,
        )

        with patch(
            "lookup.enrichment.wikipedia_bio.resolve_served_bio",
            AsyncMock(return_value=served),
        ):
            response = await _lookup(mock_library_db, mock_discogs_service, juana_item)

        assert response.results[0].artwork.artist_bio == served.bio
        assert response.degraded is False


class TestBulkTop1Shed:
    """The bulk route reports the flag on the item's ``lookup`` (LML#1420)."""

    @pytest.fixture
    def app_client_live_orchestrator(self, mock_library_db, mock_discogs_service, mock_settings):
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
    async def test_bulk_item_carries_degraded(
        self, app_client_live_orchestrator, mock_library_db, mock_discogs_service, juana_item
    ):
        _arm_discogs(mock_discogs_service, "artist")

        with patch(
            "lookup.orchestrator.execute_search_pipeline",
            AsyncMock(return_value=SearchState(results=[juana_item])),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client_live_orchestrator), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/lookup/bulk",
                    json={"items": [{"artist": "Juana Molina", "album": "DOGA"}]},
                )

        assert resp.status_code == 200
        item = resp.json()["results"][0]
        # Still a match -- the shed cost the bio, not the release.
        assert item["status"] == "match"
        assert item["lookup"]["degraded"] is True
        assert item["lookup"]["degraded_reason"] == "upstream_unavailable"
        assert item["lookup"]["results"][0]["artwork"]["artist_bio"] is None
