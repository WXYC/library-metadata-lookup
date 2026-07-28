"""Unit tests for library/router.py."""

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

import library.router
import lookup.endpoint_family
from library.db import LibraryDB
from lookup.endpoint_family import ENDPOINT_FAMILY_LIBRARY_SEARCH
from tests.factories import make_library_item
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_db():
    db = AsyncMock(spec=LibraryDB)
    db.search = AsyncMock(return_value=[])
    return db


@pytest.fixture
def app_client(mock_db, mock_settings):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    with override_deps(
        app,
        {
            get_library_db: mock_db,
            get_discogs_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
        },
    ):
        yield app


class TestSearchLibrary:
    @pytest.mark.asyncio
    async def test_query_search(self, app_client, mock_db):
        item = make_library_item(id=1, artist="Queen", title="The Game", call_letters="Q")
        mock_db.search = AsyncMock(return_value=[item])

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"q": "Queen"})

        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["results"][0]["artist"] == "Queen"

    @pytest.mark.asyncio
    async def test_artist_filter(self, app_client, mock_db):
        item = make_library_item(id=2, artist="Radiohead", title="Album", call_letters="R")
        mock_db.search = AsyncMock(return_value=[item])

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"artist": "Radiohead"})

        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    @pytest.mark.asyncio
    async def test_title_filter(self, app_client, mock_db):
        item = make_library_item(id=3, artist="Radiohead", title="OK Computer", call_letters="R")
        mock_db.search = AsyncMock(return_value=[item])

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"title": "OK Computer"})

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_params_returns_400(self, app_client):
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search")

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_search_error_returns_500(self, app_client, mock_db):
        mock_db.search = AsyncMock(side_effect=Exception("db error"))

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as client:
            resp = await client.get("/api/v1/library/search", params={"q": "test"})

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# LML#929 — protect the local-catalog search path (rung-a, #926 ADR)
# ---------------------------------------------------------------------------

#: The only modules `library/router.py` is permitted to import. The
#: `/library/search` path must stay a pure LibraryDB (aiosqlite) path with NO
#: Discogs / Apple / streaming / enrichment dependency, so its degradation is
#: only shared event-loop starvation — never upstream-fan-out latency. The
#: allowlist is deliberately tight so that ANY new import trips the guard and
#: forces a review of whether it belongs on the protected hot path.
_ALLOWED_ROUTER_IMPORT_ROOTS = {
    "logging",
    "fastapi",
    "core.dependencies",
    "library.db",
    "library.models",
    # Telemetry label only (LML#929/#931). `lookup/__init__.py` is empty and
    # `lookup/endpoint_family.py` imports only `logging` + `sentry_sdk`, so this
    # pulls in zero enrichment code — verified by test_endpoint_family_import_is_enrichment_free.
    "lookup.endpoint_family",
}


def _imported_modules(path: Path) -> set[str]:
    """Every module `path` imports, as dotted names.

    Relative imports (``level > 0``) are surfaced in dotted form (``.enrichment``,
    ``..foo``) rather than skipped, so a *package-relative* enrichment import can
    never silently match an absolute-name allowlist entry — it trips the guard
    like an absolute one would. ``__future__`` (a compiler directive, not a real
    dependency) is excluded.
    """
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." * node.level + (node.module or ""))
    imported.discard("__future__")
    return imported


class TestSearchPathStaysEnrichmentFree:
    """Characterization guards that fail if enrichment is ever wired into the
    `/library/search` handler (LML#929 unconditional deliverable)."""

    def test_router_imports_stay_within_allowlist(self):
        """Structural guard: `library/router.py`'s imports must be a subset of
        the enrichment-free allowlist. A new Discogs/Apple/streaming/enrichment
        import — absolute (`from discogs.service import …`) or package-relative
        (`from .enrichment import …`) — is the realistic "wired enrichment into
        search" change, and both forms trip this guard."""
        imported = _imported_modules(Path(library.router.__file__))
        unexpected = imported - _ALLOWED_ROUTER_IMPORT_ROOTS
        assert not unexpected, (
            f"library/router.py imports outside the LML#929 allowlist: {sorted(unexpected)}. "
            "The /library/search path must stay a pure LibraryDB (aiosqlite) path with no "
            "Discogs/Apple/streaming/enrichment dependency (rung-a of the #926 ADR). If this "
            "is a legitimate non-enrichment dependency, add it to _ALLOWED_ROUTER_IMPORT_ROOTS; "
            "if it is enrichment/upstream, do NOT wire it into the search handler — route it off "
            "the protected hot path."
        )

    def test_endpoint_family_import_is_enrichment_free(self):
        """The one cross-module import the allowlist permits (`lookup.endpoint_family`,
        the traffic-class label) must itself stay enrichment-free, or it would be a
        back-door onto the search path's import surface."""
        imported = _imported_modules(Path(lookup.endpoint_family.__file__))
        assert imported == {"logging", "sentry_sdk"}, (
            f"lookup/endpoint_family.py grew imports beyond {{logging, sentry_sdk}}: "
            f"{sorted(imported)}. It is on the /library/search import allowlist (LML#929); "
            "keep it a pure Sentry-tag helper so it cannot pull enrichment onto the search path."
        )

    @pytest.mark.asyncio
    async def test_search_invokes_no_enrichment_function(self, app_client, mock_db):
        """Runtime complement to the structural allowlist: a search request never
        calls enrichment via a call-time-qualified
        ``lookup.enrichment.enrich_artwork_results(...)``. Deliberately narrower
        than the allowlist guard above — a top-level ``from lookup.enrichment
        import enrich_artwork_results`` binding resolves before this ``patch``
        and would slip past it — so ``test_router_imports_stay_within_allowlist``
        is the authoritative "enrichment wired into search" detector; this arm is
        just a runtime tripwire for the qualified-call pattern."""
        item = make_library_item(id=1, artist="Juana Molina", title="Halo", call_letters="J")
        mock_db.search = AsyncMock(return_value=[item])

        with patch("lookup.enrichment.enrich_artwork_results") as enrich:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/library/search", params={"q": "Juana Molina"})

        assert resp.status_code == 200
        enrich.assert_not_called()


class TestSearchTrafficClassLabel:
    """The `library_search` endpoint-family label (LML#929 unconditional deliverable)."""

    @pytest.mark.asyncio
    async def test_search_emits_library_search_label(self, app_client, mock_db):
        item = make_library_item(
            id=2, artist="Jessica Pratt", title="Quiet Signs", call_letters="P"
        )
        mock_db.search = AsyncMock(return_value=[item])

        with patch("library.router.record_endpoint_family_tag") as tag:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/library/search", params={"q": "Jessica Pratt"})

        assert resp.status_code == 200
        tag.assert_called_once_with(ENDPOINT_FAMILY_LIBRARY_SEARCH)
        assert ENDPOINT_FAMILY_LIBRARY_SEARCH == "library_search"

    @pytest.mark.asyncio
    async def test_label_emitted_before_validation_on_400(self, app_client):
        """The label rides at handler entry, so even a no-params 400 carries the
        traffic class — the flood measurement counts rejected search hits too."""
        with patch("library.router.record_endpoint_family_tag") as tag:
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/library/search")

        assert resp.status_code == 400
        tag.assert_called_once_with(ENDPOINT_FAMILY_LIBRARY_SEARCH)
