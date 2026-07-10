"""Unit tests for `POST /api/v1/identity/bulk-resolve-libraries`.

Exercises the FastAPI endpoint with mocked dependencies. Composition
internals are covered separately in `test_bulk_resolve_composer.py`.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
import sentry_sdk
from asyncpg.exceptions import PostgresError
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


class TestBulkResolveGlobalBound:
    @pytest.mark.asyncio
    async def test_concurrent_requests_share_the_global_bound(
        self, app_client, mock_entity_store, monkeypatch
    ):
        """Two concurrent bulk-resolve requests never exceed LML_BULK_GLOBAL_MAX_CONCURRENT (LML#716).

        The per-request semaphore multiplies across concurrent requests; the
        process-global permit (shared with /lookup/bulk and cache refresh) is
        the cross-request bound. Per-request knob wide (10), global knob 2,
        two 4-input requests → peak in-flight store lookups <= 2.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "10")
        monkeypatch.setenv("LML_BULK_GLOBAL_MAX_CONCURRENT", "2")

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def fake_resolve(artist_name: str):
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            return None

        mock_entity_store.resolve_library_name.side_effect = fake_resolve

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            body = {
                "inputs": [
                    {"library_id": i, "artist_name": f"Artist {i}", "album_title": f"Album {i}"}
                    for i in range(4)
                ]
            }
            resp_a, resp_b = await asyncio.gather(
                ac.post("/api/v1/identity/bulk-resolve-libraries", json=body),
                ac.post("/api/v1/identity/bulk-resolve-libraries", json=body),
            )

        assert resp_a.status_code == 200
        assert resp_b.status_code == 200
        # Queue-don't-shed: every input in both requests still resolves.
        assert len(resp_a.json()["results"]) == 4
        assert len(resp_b.json()["results"]) == 4
        assert peak <= 2, (
            f"Peak cross-request concurrency was {peak}; global permit did not bound it"
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
    1. An ``INFO`` log at handler entry carrying `inputs=<N>` — fires before
       the per-input PG loop, so a handler that hangs in the loop still
       produces this signal.
    2. An ``INFO`` log at handler exit carrying the per-kind verdict counts.
    3. An explicit Sentry ``http.server`` span tied to the bulk-resolve route.
    4. A ``http.status_code=499`` pin on the span when the client aborts
       mid-loop (CancelledError caught and re-raised).

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
    async def test_499_set_on_span_when_client_aborts_mid_loop(
        self, app_client, mock_entity_store, caplog
    ):
        """Mid-loop CancelledError → http.status_code=499 on span + warn log.

        The sequential per-input loop has no gather/sentinel structure (unlike
        PR #417's `/lookup/bulk` shape), so the only signal of a client abort
        is the `asyncio.CancelledError` raised at the next `await` point.
        Without an explicit catch the span closes with no ``http.status_code``
        set, and the audit-style query
        ``op:http.server http.status_code:499`` returns nothing.

        The handler must catch ``CancelledError``, pin 499 on the span, emit
        a warn log so Railway carries a record, and re-raise so the asyncio
        cancellation contract is honored.
        """
        import asyncio
        import logging
        from unittest.mock import MagicMock

        async def cancel_mid_lookup(name: str):
            raise asyncio.CancelledError()

        mock_entity_store.resolve_library_name.side_effect = cancel_mid_lookup

        # Capture the span so we can assert set_data calls after the request
        # raises out of the test client.
        span_mock = MagicMock()
        span_cm = MagicMock()
        span_cm.__enter__ = MagicMock(return_value=span_mock)
        span_cm.__exit__ = MagicMock(return_value=False)

        with patch("identity.router.sentry_sdk.start_span", return_value=span_cm):
            with caplog.at_level(logging.WARNING, logger="identity.router"):
                async with AsyncClient(
                    transport=ASGITransport(app=app_client), base_url="http://test"
                ) as ac:
                    # CancelledError propagating out of the handler surfaces to
                    # the test client as a connection-level exception. We don't
                    # care which exact class — only that the span was tagged
                    # 499 and the warn log fired before the re-raise. Use a
                    # bare try/except (rather than pytest.raises(BaseException),
                    # which trips B017) so any exception class — including
                    # CancelledError, which subclasses BaseException not
                    # Exception — is captured.
                    raised: BaseException | None = None
                    try:
                        await ac.post(
                            "/api/v1/identity/bulk-resolve-libraries",
                            json={
                                "inputs": [
                                    {
                                        "library_id": 1,
                                        "artist_name": "Juana Molina",
                                        "album_title": "DOGA",
                                    }
                                ]
                            },
                        )
                    except BaseException as e:
                        raised = e
                    assert raised is not None, (
                        "Expected CancelledError (or wrapped client-abort exception) "
                        "to propagate out of the test client; nothing raised."
                    )

        status_data_calls = [
            c for c in span_mock.set_data.call_args_list if c.args[0] == "http.status_code"
        ]
        assert any(c.args[1] == 499 for c in status_data_calls), (
            f"Expected http.status_code=499 on span; saw: {status_data_calls}"
        )

        abort_logs = [
            r
            for r in caplog.records
            if "abort" in r.message.lower() or "client" in r.message.lower()
        ]
        assert abort_logs, (
            f"Expected a warn log mentioning the client abort; got: "
            f"{[r.message for r in caplog.records]}"
        )

    @pytest.mark.asyncio
    async def test_503_pinned_on_span_when_per_input_lookup_fails(
        self, app_client, mock_entity_store
    ):
        """A per-input PG failure pins ``http.status_code=503`` on the span.

        The #278 gather refactor moved the span-status pin: the old serial loop
        set 503 on the span inside each per-input ``except`` block, whereas the
        gather path raises ``HTTPException`` out of the child coroutine (which
        runs *before* the span is opened, so it can't touch the span) and pins
        the status in the handler's outer ``except HTTPException`` instead. This
        characterizes that relocation so a future edit that drops the outer
        catch can't silently close the span with no status on the 503 path —
        the audit-style ``op:http.server http.status_code:503`` query depends
        on it.
        """
        from unittest.mock import MagicMock

        async def maybe_fail(name: str):
            if name == "Boom":
                raise PostgresError("simulated mid-batch PG failure")
            return None

        mock_entity_store.resolve_library_name.side_effect = maybe_fail

        span_mock = MagicMock()
        span_cm = MagicMock()
        span_cm.__enter__ = MagicMock(return_value=span_mock)
        span_cm.__exit__ = MagicMock(return_value=False)

        with patch("identity.router.sentry_sdk.start_span", return_value=span_cm):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await ac.post(
                    "/api/v1/identity/bulk-resolve-libraries",
                    json={
                        "inputs": [
                            {"library_id": 1, "artist_name": "Fine One", "album_title": "x"},
                            {"library_id": 2, "artist_name": "Boom", "album_title": "x"},
                        ]
                    },
                )

        assert resp.status_code == 503
        status_calls = [
            c for c in span_mock.set_data.call_args_list if c.args[0] == "http.status_code"
        ]
        assert any(c.args[1] == 503 for c in status_calls), (
            f"Expected http.status_code=503 pinned on the span; saw: {status_calls}"
        )

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


class TestBulkResolveConcurrency:
    """Per-input lookups fan out concurrently under a pool-bound semaphore (#278).

    Before #278 the handler iterated inputs serially — worst-case latency for a
    1,000-row miss-heavy batch was ~3,000 sequential PG round-trips. #278
    dispatches the per-input work via ``asyncio.gather`` capped by a semaphore
    sized to the discogs-cache pool's ``max_size`` so we parallelize without
    exhausting the asyncpg pool. These tests pin:

    1. Concurrency actually happens (total elapsed ≈ one slow lookup, not N).
    2. The semaphore caps in-flight lookups at the configured bound.
    3. ``asyncio.gather`` preserves input order even when completion order
       differs (slowest input first).
    4. A ``PostgresError`` raised by any one input still fails the whole batch
       closed with 503 — never a 200 carrying partial results.
    """

    @pytest.mark.asyncio
    async def test_per_input_lookups_run_concurrently(self, app_client, mock_entity_store):
        """N lookups each sleeping DELAY finish in ~DELAY, not N×DELAY.

        With the default cap (the pool ``max_size`` of 5) and N=5 inputs, all
        five ``resolve_library_name`` calls overlap, so wall-clock is dominated
        by a single sleep. The serial implementation would take ~5×DELAY; this
        asserts a 3× margin below that to stay robust on slow CI.
        """
        delay = 0.05
        n = 5

        async def slow_lookup(name: str):
            await asyncio.sleep(delay)
            return None

        mock_entity_store.resolve_library_name.side_effect = slow_lookup

        inputs = [
            {"library_id": i, "artist_name": f"Artist {i}", "album_title": "x"} for i in range(n)
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            start = time.perf_counter()
            resp = await ac.post("/api/v1/identity/bulk-resolve-libraries", json={"inputs": inputs})
            elapsed = time.perf_counter() - start

        assert resp.status_code == 200
        assert len(resp.json()["results"]) == n
        # Serial would be n * delay = 0.25s. Concurrent is ~delay. A 3×-delay
        # ceiling cleanly separates the two without being CI-flaky.
        assert elapsed < (n * delay) / 2, (
            f"Expected concurrent dispatch (~{delay:.2f}s) but took {elapsed:.3f}s; "
            "per-input lookups appear to still be running serially."
        )

    @pytest.mark.asyncio
    async def test_semaphore_bounds_in_flight_lookups(
        self, app_client, mock_entity_store, monkeypatch
    ):
        """The semaphore caps concurrent in-flight lookups at the configured bound.

        Sets ``LML_BULK_MAX_CONCURRENT=2`` and posts 6 inputs. A counter tracks
        how many ``resolve_library_name`` calls overlap; the observed peak must
        equal the bound (2) — proving the work both parallelizes *and* is capped
        so the asyncpg pool can't be exhausted.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "2")

        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def tracked_lookup(name: str):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1
            return None

        mock_entity_store.resolve_library_name.side_effect = tracked_lookup

        inputs = [
            {"library_id": i, "artist_name": f"Artist {i}", "album_title": "x"} for i in range(6)
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/identity/bulk-resolve-libraries", json={"inputs": inputs})

        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 6
        assert max_in_flight == 2, (
            f"Expected the semaphore to cap in-flight lookups at 2; observed peak "
            f"{max_in_flight}. <2 means no parallelism; >2 means the bound leaked."
        )

    @pytest.mark.asyncio
    async def test_semaphore_bound_tracks_discogs_pool_size(
        self, app_client, mock_entity_store, monkeypatch
    ):
        """End-to-end: with ``LML_BULK_MAX_CONCURRENT`` unset, the endpoint's
        in-flight bound tracks ``LML_DISCOGS_POOL_MAX_SIZE`` (LML#706).

        The unit tests for ``_bulk_resolve_default_concurrency`` prove the helper
        reads the pool knob; this proves the *endpoint* actually wires that
        default into its semaphore. Guards against a refactor that keeps the
        helper correct but stops feeding it to ``asyncio.Semaphore`` — which
        would leave the unit tests green while silently losing the pool-tracking.
        """
        monkeypatch.delenv("LML_BULK_MAX_CONCURRENT", raising=False)
        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "2")

        in_flight = 0
        max_in_flight = 0
        lock = asyncio.Lock()

        async def tracked_lookup(name: str):
            nonlocal in_flight, max_in_flight
            async with lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            await asyncio.sleep(0.02)
            async with lock:
                in_flight -= 1
            return None

        mock_entity_store.resolve_library_name.side_effect = tracked_lookup

        inputs = [
            {"library_id": i, "artist_name": f"Artist {i}", "album_title": "x"} for i in range(6)
        ]

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post("/api/v1/identity/bulk-resolve-libraries", json={"inputs": inputs})

        assert resp.status_code == 200
        assert max_in_flight == 2, (
            f"Expected LML_DISCOGS_POOL_MAX_SIZE=2 to cap in-flight lookups at 2; "
            f"observed peak {max_in_flight}. The pool-derived default is not reaching "
            f"the endpoint's semaphore."
        )

    @pytest.mark.asyncio
    async def test_input_order_preserved_under_staggered_completion(
        self, app_client, mock_entity_store
    ):
        """gather preserves input order even when the first input finishes last.

        The earliest input sleeps the longest so completion order is the reverse
        of input order. ``results[i]`` must still correspond to ``inputs[i]``.
        """
        identities = {
            "First": _identity("First", id=1, discogs_artist_id=11),
            "Second": _identity("Second", id=2, discogs_artist_id=22),
            "Third": _identity("Third", id=3, discogs_artist_id=33),
        }
        # Slowest first, fastest last → completion order is reversed.
        delays = {"First": 0.06, "Second": 0.03, "Third": 0.0}

        async def staggered_lookup(name: str):
            await asyncio.sleep(delays[name])
            return identities[name]

        mock_entity_store.resolve_library_name.side_effect = staggered_lookup
        mock_entity_store.get_latest_provenance_by_source.return_value = {}

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {"library_id": 1, "artist_name": "First", "album_title": "x"},
                        {"library_id": 2, "artist_name": "Second", "album_title": "x"},
                        {"library_id": 3, "artist_name": "Third", "album_title": "x"},
                    ]
                },
            )

        assert resp.status_code == 200
        results = resp.json()["results"]
        assert [r["library_id"] for r in results] == [1, 2, 3]
        assert [r["main"]["discogs_artist_id"] for r in results] == [11, 22, 33]

    @pytest.mark.asyncio
    async def test_client_disconnect_cancels_in_flight_lookups(
        self, app_client, mock_entity_store, monkeypatch
    ):
        """Mid-batch client disconnect cancels in-flight lookups; queued ones never start.

        Adopts the ``/lookup/bulk`` disconnect-cancellation pattern (LML#700):
        without it, a caller that hangs up mid-request leaves the in-flight
        per-input tasks — and the discogs-cache pool permits they hold — running
        to completion against a client that is already gone. The sentinel race
        cancels the outstanding gather promptly instead.

        Patches ``identity.router.watch_disconnect`` with a sentinel that
        "detects" the disconnect after a short delay; the real receive-channel
        mechanism is exercised in production by uvicorn. The slow lookup outlasts
        the sentinel so the abort fires while work is still in flight.

        The load-bearing assertion is ``cancelled == started``: every lookup that
        actually entered (the in-flight items, bounded by the pinned concurrency)
        must receive ``CancelledError``. A regression that returns 499 but leaves
        the gather draining in the background (the LML#700 bug) leaves the
        in-flight items sleeping — ``cancelled == 0 != started`` — so this fails.
        ``await_count``-style counts alone can't catch that: the semaphore already
        bounds in-flight lookups below the batch size whether or not the cancel
        lands. Concurrency is pinned (not read from the production default) so a
        future pool-size change can't silently make the batch fit under one wave.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "3")
        concurrency = 3
        batch_size = 9

        started = 0
        cancelled = 0

        async def slow_lookup(name: str):
            nonlocal started, cancelled
            started += 1
            try:
                await asyncio.sleep(5)
                return None
            except asyncio.CancelledError:
                cancelled += 1
                raise

        mock_entity_store.resolve_library_name.side_effect = slow_lookup

        async def fake_sentinel(_request):
            await asyncio.sleep(0.05)

        inputs = [
            {"library_id": i, "artist_name": f"Artist {i}", "album_title": "x"}
            for i in range(batch_size)
        ]

        with patch("identity.router.watch_disconnect", fake_sentinel):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post("/api/v1/identity/bulk-resolve-libraries", json={"inputs": inputs}),
                    timeout=3.0,
                )

        assert resp.status_code == 499, f"Expected 499 on client disconnect, got {resp.status_code}"
        # Queued lookups never start: the semaphore caps in-flight at `concurrency`
        # and each in-flight lookup is still sleeping when the sentinel fires.
        assert 0 < started <= concurrency, (
            f"Expected 1..{concurrency} in-flight lookups, got {started}; "
            f"{batch_size - started} should have stayed queued behind the semaphore"
        )
        # Every in-flight lookup received the cancel and unwound — the actual proof
        # that the disconnect cancelled the outstanding gather rather than letting
        # it drain in the background against a departed client.
        assert cancelled == started, (
            f"{started - cancelled} of {started} in-flight lookup(s) were never "
            "cancelled — the disconnect did not cancel the outstanding gather"
        )

    @pytest.mark.asyncio
    async def test_client_disconnect_unwinds_per_item_tasks_cleanly(
        self, app_client, mock_entity_store, monkeypatch
    ):
        """Cancellation reaches each per-input coroutine, which unwinds via `finally`.

        Permit release is the downstream consequence: the per-input semaphore in
        ``bulk_resolve_libraries`` releases via ``async with semaphore`` __aexit__
        only if the cancel actually reaches the per-input coroutine. Proxy
        assertion: count ``started`` (each lookup entered) and ``cleaned_up``
        (each that exited via ``finally``). On clean propagation
        ``started == cleaned_up``; if the cancel never reaches the per-item tasks
        they keep running and ``started > cleaned_up`` — the LML#700 bug.
        """
        monkeypatch.setenv("LML_BULK_MAX_CONCURRENT", "2")

        started = 0
        cleaned_up = 0

        async def slow_lookup(name: str):
            nonlocal started, cleaned_up
            started += 1
            try:
                await asyncio.sleep(5)
                return None
            finally:
                cleaned_up += 1

        mock_entity_store.resolve_library_name.side_effect = slow_lookup

        async def fake_sentinel(_request):
            await asyncio.sleep(0.05)

        inputs = [
            {"library_id": i, "artist_name": f"Artist {i}", "album_title": "x"} for i in range(6)
        ]

        with patch("identity.router.watch_disconnect", fake_sentinel):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post("/api/v1/identity/bulk-resolve-libraries", json={"inputs": inputs}),
                    timeout=3.0,
                )

        assert resp.status_code == 499
        assert started > 0, "no lookups started — test mis-configured"
        assert started == cleaned_up, (
            f"{started - cleaned_up} lookup(s) started but never cleaned up — "
            "cancellation did not propagate into the per-input tasks"
        )

    @pytest.mark.asyncio
    async def test_client_disconnect_sets_sentry_tag(self, app_client, mock_entity_store):
        """`lml.client_aborted=true` lands on the active Sentry scope on abort.

        Filterable in the trace explorer (`lml.client_aborted:true`) to surface
        aborted batches for triage — same global-scope tag the `/lookup/bulk`
        abort branch sets, so a single filter spans both bulk routes.
        """
        captured_tags: dict[str, str] = {}
        original_set_tag = sentry_sdk.set_tag

        def capture_tag(key, value):
            captured_tags[key] = value
            return original_set_tag(key, value)

        async def slow_lookup(name: str):
            await asyncio.sleep(5)
            return None

        mock_entity_store.resolve_library_name.side_effect = slow_lookup

        async def fake_sentinel(_request):
            await asyncio.sleep(0.05)

        inputs = [
            {"library_id": i, "artist_name": f"Artist {i}", "album_title": "x"} for i in range(4)
        ]

        with (
            patch("identity.router.watch_disconnect", fake_sentinel),
            patch("identity.router.sentry_sdk.set_tag", side_effect=capture_tag),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app_client), base_url="http://test"
            ) as ac:
                resp = await asyncio.wait_for(
                    ac.post("/api/v1/identity/bulk-resolve-libraries", json={"inputs": inputs}),
                    timeout=3.0,
                )

        assert resp.status_code == 499
        assert captured_tags.get("lml.client_aborted") == "true", (
            f"Expected lml.client_aborted=true tag; got {captured_tags!r}"
        )

    @pytest.mark.asyncio
    async def test_postgres_error_mid_batch_returns_503_not_partial(
        self, app_client, mock_entity_store
    ):
        """A PostgresError on any input fails the whole batch closed with 503.

        Fail-closed posture must survive the gather refactor: the caller cannot
        distinguish "row had no identity" from "PG died before this row was
        tried", so a partial 200 would silently cache wrong no-match verdicts.
        """

        async def maybe_fail(name: str):
            if name == "Boom":
                raise PostgresError("simulated mid-batch PG failure")
            return None

        mock_entity_store.resolve_library_name.side_effect = maybe_fail

        async with AsyncClient(
            transport=ASGITransport(app=app_client), base_url="http://test"
        ) as ac:
            resp = await ac.post(
                "/api/v1/identity/bulk-resolve-libraries",
                json={
                    "inputs": [
                        {"library_id": 1, "artist_name": "Fine One", "album_title": "x"},
                        {"library_id": 2, "artist_name": "Boom", "album_title": "x"},
                        {"library_id": 3, "artist_name": "Fine Two", "album_title": "x"},
                    ]
                },
            )

        assert resp.status_code == 503
        # No partial results leak: the body is the 503 error envelope, not a
        # 200 `results` payload.
        assert "results" not in resp.json()


class TestBulkResolveDefaultConcurrencyTracksPool:
    """LML#706: the bulk-resolve semaphore default mirrors the discogs-cache
    pool's ``max_size``.

    ``identity/router`` documents that the default "IS the pool max" so the
    semaphore saturates the pool without coroutines queueing on
    ``pool.acquire()``. Once the pool became env-tunable
    (``LML_DISCOGS_POOL_MAX_SIZE``), a hardcoded ``5`` would break that coupling:
    raise the pool and bulk-resolve stays under-parallelized; lower the pool and
    the semaphore admits more coroutines than the pool has connections. The
    default must track the same env var.
    """

    def test_default_is_pool_default_when_unset(self, monkeypatch):
        from identity.router import _bulk_resolve_default_concurrency

        monkeypatch.delenv("LML_DISCOGS_POOL_MAX_SIZE", raising=False)
        assert _bulk_resolve_default_concurrency() == 5

    def test_default_tracks_pool_env(self, monkeypatch):
        from identity.router import _bulk_resolve_default_concurrency

        monkeypatch.setenv("LML_DISCOGS_POOL_MAX_SIZE", "8")
        assert _bulk_resolve_default_concurrency() == 8
