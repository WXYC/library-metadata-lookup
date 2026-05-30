"""Unit tests for `POST /api/v1/identity/bulk-resolve-libraries`.

Exercises the FastAPI endpoint with mocked dependencies. Composition
internals are covered separately in `test_bulk_resolve_composer.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sentry_sdk
from httpx import ASGITransport, AsyncClient

from entity.store import Identity
from tests.unit.conftest import override_deps


@pytest.fixture
def mock_entity_store():
    store = AsyncMock()
    return store


@pytest.fixture
def app_client(mock_settings, mock_entity_store):
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from identity.dependencies import get_entity_store
    from main import app

    with override_deps(
        app,
        {
            get_library_db: AsyncMock(),
            get_discogs_service: None,
            get_posthog_client: None,
            get_settings: mock_settings,
            get_entity_store: mock_entity_store,
        },
    ):
        yield app


def _identity(
    library_name: str,
    *,
    id: int = 1,
    discogs_artist_id: int | None = None,
    wikidata_qid: str | None = None,
) -> Identity:
    return Identity(
        id=id,
        library_name=library_name,
        discogs_artist_id=discogs_artist_id,
        wikidata_qid=wikidata_qid,
        musicbrainz_artist_id=None,
        spotify_artist_id=None,
        apple_music_artist_id=None,
        bandcamp_id=None,
        reconciliation_status="reconciled",
    )


class TestBulkResolveLibrariesEndpoint:
    @pytest.mark.asyncio
    async def test_single_artist_returns_main_and_provenance(self, app_client, mock_entity_store):
        """A populated identity → kind=single_artist with ReconciledIdentity main."""
        mock_entity_store.resolve_library_name.return_value = _identity(
            "Stereolab", id=1, discogs_artist_id=2154, wikidata_qid="Q484464"
        )
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 1234,
                            "artist_name": "Stereolab",
                            "album_title": "Aluminum Tunes",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["results"]) == 1
        result = data["results"][0]
        assert result["kind"] == "single_artist"
        assert result["library_id"] == 1234
        assert result["main"]["discogs_artist_id"] == 2154
        assert result["main"]["wikidata_qid"] == "Q484464"
        assert len(result["provenance"]) == 2

    @pytest.mark.asyncio
    async def test_compilation_kind_for_va_artist_name(self, app_client, mock_entity_store):
        """`Various Artists` artist_name → kind=compilation, no entity lookup."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 5678,
                            "artist_name": "Various Artists",
                            "album_title": "Edits",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "compilation"
        assert result["library_id"] == 5678
        assert result["main"] is None
        assert result["provenance"] == []
        assert result["tracks"] == []
        # V/A short-circuits the entity lookup — should never call get_identity.
        mock_entity_store.resolve_library_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unresolved_when_identity_missing(self, app_client, mock_entity_store):
        """No entity row for the artist → kind=unresolved."""
        mock_entity_store.resolve_library_name.return_value = None

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 9999,
                            "artist_name": "Some Obscure Artist",
                            "album_title": "An Album",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "unresolved"
        assert result["library_id"] == 9999
        assert result["main"] is None
        assert result["method"] is None
        assert result["confidence"] is None
        assert result["provenance"] == []

    @pytest.mark.asyncio
    async def test_response_order_matches_request_order(self, app_client, mock_entity_store):
        """Mixed inputs → results array preserves input order."""

        async def get_identity(name: str):
            mapping = {"Stereolab": _identity("Stereolab", id=1, discogs_artist_id=1)}
            return mapping.get(name)

        mock_entity_store.resolve_library_name.side_effect = get_identity
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {"library_id": 1, "artist_name": "Various Artists", "album_title": "VA"},
                        {"library_id": 2, "artist_name": "Stereolab", "album_title": "AT"},
                        {"library_id": 3, "artist_name": "Nobody", "album_title": "x"},
                    ]
                },
            )

        assert resp.status_code == 200
        data = resp.json()
        kinds = [r["kind"] for r in data["results"]]
        ids = [r["library_id"] for r in data["results"]]
        assert kinds == ["compilation", "single_artist", "unresolved"]
        assert ids == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_413_when_over_input_cap(self, app_client, mock_entity_store):
        """1001+ inputs → 413, not 422 (per api.yaml)."""
        oversized = [
            {"library_id": i, "artist_name": f"Artist_{i}", "album_title": "x"} for i in range(1001)
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={"inputs": oversized},
            )

        assert resp.status_code == 413
        # Cap-check fires before any DB work.
        mock_entity_store.resolve_library_name.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_503_when_entity_store_unavailable(self, mock_settings):
        """Endpoint mirrors `/identity/*` 503 posture when the store isn't ready."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from identity.dependencies import get_entity_store
        from main import app

        with override_deps(
            app,
            {
                get_library_db: AsyncMock(),
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
                get_entity_store: None,
            },
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                resp = await ac.post(
                    "/api/v1/identity/bulk-resolve-libraries",
                    json={
                        "inputs": [
                            {
                                "library_id": 1,
                                "artist_name": "Stereolab",
                                "album_title": "x",
                            }
                        ]
                    },
                )

        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_uses_fall_through_lookup_not_legacy_exact_match(
        self, app_client, mock_entity_store
    ):
        """Per #274/#276: handler must use the three-leg fall-through method.

        The handler must call ``resolve_library_name`` (exact → LOWER →
        canonical fall-through), NOT the legacy exact-match ``get_identity``.
        The legacy method would miss any input that differs from storage even
        by case — the exact regression the #276 production audit surfaced.
        """
        mock_entity_store.resolve_library_name.return_value = _identity(
            "nilufer yanya", id=1, discogs_artist_id=5499521, wikidata_qid="Q21470020"
        )
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {
                            "library_id": 42,
                            "artist_name": "Nilüfer Yanya",
                            "album_title": "Painless",
                        }
                    ]
                },
            )

        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["kind"] == "single_artist"
        assert result["main"]["discogs_artist_id"] == 5499521
        mock_entity_store.resolve_library_name.assert_awaited_once_with("Nilüfer Yanya")
        # The legacy exact-match path must not be exercised — divergence
        # vectors would otherwise leak past as silent unresolved verdicts.
        mock_entity_store.get_identity.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_422_for_validation_error(self, app_client, mock_entity_store):
        """Missing required field per-input → 422 (Pydantic validation)."""
        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={"inputs": [{"library_id": 1, "artist_name": "Stereolab"}]},
            )

        assert resp.status_code == 422
        mock_entity_store.resolve_library_name.assert_not_awaited()


class TestBulkResolveObservability:
    """Entry/exit instrumentation pins (LML#430, sibling of #371 / PR #417).

    The bulk-resolve-libraries route emits no `http.server` Sentry spans and no
    log lines visible to Sentry — the [#355](https://github.com/WXYC/library-metadata-lookup/issues/355)
    audit's prescribed Sentry pivot couldn't run because of this gap. The same
    root cause as #371 applies: FastAPI integration's automatic transaction
    commits on response completion, so a handler that hangs past the caller's
    AbortController leaves zero server-side signal.

    These tests pin the defensive instrumentation that closes the gap on this
    sibling endpoint, mirroring the shape PR #417 landed for `/lookup/bulk`:
    1. An ``INFO`` log at handler entry carrying `inputs=<N>` — fires BEFORE
       any awaits, so a totally-hung handler still produces this signal.
    2. An ``INFO`` log at handler exit carrying the per-kind verdict counts.
    3. An explicit Sentry ``http.server`` span tied to the bulk-resolve route.

    The matching production change is in
    ``identity/router.py:bulk_resolve_libraries``.
    """

    @pytest.mark.asyncio
    async def test_entry_log_includes_inputs_count(self, app_client, mock_entity_store, caplog):
        """An INFO log fires at handler entry with inputs=N.

        Load-bearing observability signal — synchronous fire before any awaits
        means a hung handler still produces this line. Pins the entry-log
        shape so refactors don't accidentally drop it.
        """
        import logging

        mock_entity_store.resolve_library_name.return_value = None

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            with caplog.at_level(logging.INFO, logger="identity.router"):
                resp = await ac.post(
                    "/api/v1/identity/bulk-resolve-libraries",
                    json={
                        "inputs": [
                            {"library_id": 1, "artist_name": "Juana Molina", "album_title": "DOGA"},
                            {"library_id": 2, "artist_name": "Stereolab", "album_title": "AT"},
                            {"library_id": 3, "artist_name": "Cat Power", "album_title": "Moon"},
                        ]
                    },
                )

        assert resp.status_code == 200
        entry_records = [
            r for r in caplog.records if "bulk resolve" in r.message and "start" in r.message
        ]
        assert entry_records, (
            "Expected an INFO log at bulk-resolve handler entry; got logs: "
            f"{[r.message for r in caplog.records]}"
        )
        entry_msg = entry_records[0].message
        assert "3" in entry_msg, f"Entry log missing inputs count: {entry_msg!r}"

    @pytest.mark.asyncio
    async def test_exit_log_includes_verdict_counts(self, app_client, mock_entity_store, caplog):
        """An INFO log fires at handler exit carrying per-kind verdict counts.

        Pairs with the entry log — operators can read off the
        single_artist / compilation / unresolved breakdown directly from
        Railway without correlating to a Sentry trace.
        """
        import logging

        async def resolve(name: str):
            if name == "Stereolab":
                return _identity("Stereolab", id=1, discogs_artist_id=2154)
            return None

        mock_entity_store.resolve_library_name.side_effect = resolve
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            with caplog.at_level(logging.INFO, logger="identity.router"):
                resp = await ac.post(
                    "/api/v1/identity/bulk-resolve-libraries",
                    json={
                        "inputs": [
                            {
                                "library_id": 1,
                                "artist_name": "Various Artists",
                                "album_title": "VA",
                            },
                            {"library_id": 2, "artist_name": "Stereolab", "album_title": "AT"},
                            {"library_id": 3, "artist_name": "Unknown", "album_title": "X"},
                        ]
                    },
                )

        assert resp.status_code == 200
        exit_records = [
            r for r in caplog.records if "bulk resolve" in r.message and "complete" in r.message
        ]
        assert exit_records, (
            "Expected an INFO log at bulk-resolve handler exit; got logs: "
            f"{[r.message for r in caplog.records]}"
        )
        exit_msg = exit_records[0].message
        # Pin the shape: inputs + the three kinds present. The format string
        # may use `=` or `:` — assert on keyword presence + count values, not
        # the exact format token.
        assert "inputs" in exit_msg, exit_msg
        assert "single_artist" in exit_msg, exit_msg
        assert "compilation" in exit_msg, exit_msg
        assert "unresolved" in exit_msg, exit_msg

    @pytest.mark.asyncio
    async def test_http_server_span_emitted(self, app_client, mock_entity_store):
        """An explicit Sentry `http.server` span wraps the handler.

        Defensive against the FastApiIntegration's automatic transaction not
        landing for this endpoint (the gap LML#355's audit hit). With the
        wrap, a query for `op:http.server span.description:*bulk-resolve-libraries*`
        in the trace explorer always surfaces traffic.
        """
        captured_spans: list[dict] = []
        original_start_span = sentry_sdk.start_span

        def capture_start_span(*args, **kwargs):
            captured_spans.append({"args": args, "kwargs": kwargs})
            return original_start_span(*args, **kwargs)

        mock_entity_store.resolve_library_name.return_value = None

        with patch("identity.router.sentry_sdk.start_span", side_effect=capture_start_span):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/identity/bulk-resolve-libraries",
                    json={
                        "inputs": [
                            {"library_id": 1, "artist_name": "Juana Molina", "album_title": "DOGA"}
                        ]
                    },
                )

        assert resp.status_code == 200
        http_server_spans = [s for s in captured_spans if s["kwargs"].get("op") == "http.server"]
        assert http_server_spans, (
            "Expected at least one Sentry span with op='http.server'; got ops: "
            f"{[s['kwargs'].get('op') for s in captured_spans]}"
        )
        span_name = http_server_spans[0]["kwargs"].get("name", "")
        assert "bulk-resolve-libraries" in span_name, (
            f"Expected http.server span name to include 'bulk-resolve-libraries'; got {span_name!r}"
        )
