"""Unit tests for core/server_timing_middleware.py (LML#907 fine-grained-timing
leg; LML#946 rewrote the middleware itself as pure ASGI).

Exercises the middleware in isolation against a minimal FastAPI app rather than
the full `main.app` — the router-level integration is covered by
`test_lookup_router.py`'s `TestHandleLookup.test_server_timing_wire_format_is_
strict_parser_safe` and `test_server_timing_failure_does_not_break_request`,
which drive the real app end-to-end and assert `lml_wall` shows up alongside
the router's own legs.

`TestLmlWallTimingMiddleware` drives the middleware through a real FastAPI/
httpx round trip (installed via `app.add_middleware`, same as `main.py`).
`TestLmlWallTimingMiddlewareRawASGI` drives `LmlWallTimingMiddleware.__call__`
directly against bare `(scope, receive, send)` — the only way to prove the
passthrough paths build zero `send` wrapper, which is the entire point of the
pure-ASGI rewrite (see the module docstring on `core/server_timing_middleware.py`).
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Response
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Receive, Scope, Send

from config.settings import Settings
from core.server_timing_middleware import LmlWallTimingMiddleware, _with_appended_server_timing

_DUR_GRAMMAR = re.compile(r"^lml_wall;dur=\d+(?:\.\d+)?$")


def _build_app(*, existing_header: str | None) -> FastAPI:
    """A minimal app with the middleware installed and two routes:
    ``/api/v1/lookup`` (the scoped path) and ``/health`` (an out-of-scope
    control). Each route optionally pre-sets ``Server-Timing`` on the
    ``Response`` it's handed, mimicking a handler that already emitted its own
    header (or one that didn't, e.g. because ``LML_EMIT_SERVER_TIMING`` was off
    or the build failed).
    """
    app = FastAPI()
    app.add_middleware(LmlWallTimingMiddleware)

    @app.get("/api/v1/lookup")
    async def timed_route(response: Response):
        if existing_header is not None:
            response.headers["Server-Timing"] = existing_header
        return {"ok": True}

    @app.get("/health")
    async def health_route(response: Response):
        if existing_header is not None:
            response.headers["Server-Timing"] = existing_header
        return {"ok": True}

    return app


@pytest.fixture
def settings_on() -> Settings:
    return Settings(
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
        lml_emit_server_timing=True,
    )


@pytest.fixture
def settings_off(settings_on: Settings) -> Settings:
    return settings_on.model_copy(update={"lml_emit_server_timing": False})


class TestLmlWallTimingMiddleware:
    @pytest.mark.asyncio
    async def test_appends_lml_wall_without_clobbering_existing_legs(self, settings_on):
        """The core contract (item 3): APPEND, never overwrite. The handler's
        three legs must survive byte-for-byte, with ``lml_wall`` added after.
        """
        app = _build_app(existing_header="library_search;dur=12.34, discogs;dur=5, total;dur=20")

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/lookup")

        assert resp.status_code == 200
        entries = resp.headers["Server-Timing"].split(", ")
        names = [e.split(";")[0] for e in entries]
        assert names == ["library_search", "discogs", "total", "lml_wall"]
        # The pre-existing legs are untouched, not just present-by-name.
        assert entries[:3] == ["library_search;dur=12.34", "discogs;dur=5", "total;dur=20"]
        assert _DUR_GRAMMAR.match(entries[3])

    @pytest.mark.asyncio
    async def test_creates_header_when_handler_emitted_none(self, settings_on):
        """No existing header (flag was off for the handler, or its build
        failed) still gets an ``lml_wall``-only header when the middleware's
        own gate is on — the two writers are independently gated by design.
        """
        app = _build_app(existing_header=None)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/lookup")

        assert resp.status_code == 200
        assert resp.headers["Server-Timing"].split(";")[0] == "lml_wall"
        assert _DUR_GRAMMAR.match(resp.headers["Server-Timing"])

    @pytest.mark.asyncio
    async def test_gated_off_leaves_existing_header_untouched(self, settings_off):
        app = _build_app(existing_header="total;dur=20")

        with patch("core.server_timing_middleware.get_settings", return_value=settings_off):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/lookup")

        assert resp.status_code == 200
        assert resp.headers["Server-Timing"] == "total;dur=20"

    @pytest.mark.asyncio
    async def test_gated_off_with_no_existing_header_stays_absent(self, settings_off):
        app = _build_app(existing_header=None)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_off):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/lookup")

        assert resp.status_code == 200
        assert "Server-Timing" not in resp.headers

    @pytest.mark.asyncio
    async def test_non_lookup_path_is_untouched(self, settings_on):
        """Scoped to `/api/v1/lookup` only — `/health` and everything else
        passes through with zero interference, even with the flag on.
        """
        app = _build_app(existing_header="total;dur=1")

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.headers["Server-Timing"] == "total;dur=1"

    @pytest.mark.asyncio
    async def test_path_not_instrumented_when_handler_emits_nothing(self, settings_on):
        """An out-of-scope path that never sets Server-Timing stays headerless
        — the middleware must not manufacture a header outside its scope.
        """
        app = _build_app(existing_header=None)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        assert "Server-Timing" not in resp.headers

    @pytest.mark.asyncio
    async def test_settings_read_failure_does_not_break_response(self, settings_on):
        """Observability must not break the request path: a raising
        ``get_settings()`` degrades to 'no leg appended', not a 500, and the
        handler's own header (out of this middleware's control) survives.
        """
        app = _build_app(existing_header="total;dur=1")

        with patch("core.server_timing_middleware.get_settings", side_effect=RuntimeError("boom")):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/lookup")

        assert resp.status_code == 200
        assert resp.headers["Server-Timing"] == "total;dur=1"

    @pytest.mark.asyncio
    async def test_dur_formatting_matches_request_telemetry_grammar(self, settings_on):
        """Two-decimal precision with trailing zeros stripped, always
        fixed-point — the same wire format ``RequestTelemetry.as_server_timing``
        uses (`wxyc_fastapi.observability.telemetry._fmt_dur`), so a strict
        downstream parser sees one consistent grammar across every leg.
        """
        app = _build_app(existing_header=None)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/v1/lookup")

        entry = resp.headers["Server-Timing"]
        assert _DUR_GRAMMAR.match(entry)
        dur_str = entry.split("dur=")[1]
        assert "e" not in dur_str.lower()  # never scientific notation
        if "." in dur_str:
            assert len(dur_str.split(".")[1]) <= 2

    @pytest.mark.asyncio
    async def test_call_next_exception_still_propagates(self, settings_on):
        """A genuine failure inside routing must still surface — the
        middleware only guards its OWN instrumentation (the settings read +
        header append AFTER ``call_next`` returns), never the routing call
        itself, so it must never swallow a real handler error into a silent
        success. An unhandled (non-``HTTPException``) exception is Starlette's
        own contract to re-raise past ``ServerErrorMiddleware`` after sending a
        500 — asserting the raise (rather than inspecting a response object)
        proves this middleware doesn't intercept and suppress it.
        """
        app = FastAPI()
        app.add_middleware(LmlWallTimingMiddleware)

        @app.get("/api/v1/lookup")
        async def boom():
            raise RuntimeError("handler exploded")

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with pytest.raises(RuntimeError, match="handler exploded"):
                    await client.get("/api/v1/lookup")


class _RecordingApp:
    """A bare ASGI app standing in for ``self.app`` inside the middleware.

    Records the exact ``send`` callable it was handed (so tests can assert
    object identity — proof the middleware did NOT build a wrapper) and, for
    HTTP scopes, replays a canned ``http.response.start``/``http.response.body``
    pair through whatever ``send`` it was given.
    """

    def __init__(self, *, response_headers: list[tuple[bytes, bytes]] | None = None) -> None:
        self.received_scope: Scope | None = None
        self.received_send: Send | None = None
        self._response_headers = response_headers or []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.received_scope = scope
        self.received_send = send
        if scope["type"] != "http":
            return
        await send(
            {"type": "http.response.start", "status": 200, "headers": self._response_headers}
        )
        await send({"type": "http.response.body", "body": b"{}"})


def _noop_receive() -> Receive:
    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


class TestLmlWallTimingMiddlewareRawASGI:
    """Exercises ``LmlWallTimingMiddleware.__call__`` directly against the raw
    ASGI protocol (bypassing FastAPI/httpx) to prove the passthrough paths do
    NOT build a ``send`` wrapper at all, and to pin the byte-level header
    format the ``send`` wrapper produces when it does engage.
    """

    @pytest.mark.asyncio
    async def test_non_http_scope_is_passthrough_and_never_reads_settings(self):
        """A non-``http`` scope (e.g. ``lifespan``, ``websocket``) must
        short-circuit before the settings gate is even consulted — proven by
        making ``get_settings`` raise if it's called at all — and the inner
        app must receive the ORIGINAL ``send`` (identity, not a wrapper).
        """
        inner = _RecordingApp()
        middleware = LmlWallTimingMiddleware(inner)
        scope: Scope = {"type": "lifespan"}
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with patch(
            "core.server_timing_middleware.get_settings",
            side_effect=AssertionError("get_settings must not be called for a non-http scope"),
        ) as mock_settings:
            await middleware(scope, _noop_receive(), send)

        mock_settings.assert_not_called()
        assert inner.received_send is send

    @pytest.mark.asyncio
    async def test_non_lookup_path_is_passthrough_with_no_send_wrapping(self, settings_on):
        """An HTTP request outside ``/api/v1/lookup`` never gets a ``send``
        wrapper — the inner app's ``send`` is the exact object passed in.
        """
        inner = _RecordingApp(response_headers=[(b"server-timing", b"total;dur=1")])
        middleware = LmlWallTimingMiddleware(inner)
        scope: Scope = {"type": "http", "path": "/health"}
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            await middleware(scope, _noop_receive(), send)

        assert inner.received_send is send
        start = sent[0]
        assert start["headers"] == [(b"server-timing", b"total;dur=1")]

    @pytest.mark.asyncio
    async def test_gate_off_is_passthrough_with_no_send_wrapping(self, settings_off):
        """Scoped path + gate off must also be a bare passthrough: no wrapper
        object is created merely because the path matched.
        """
        inner = _RecordingApp()
        middleware = LmlWallTimingMiddleware(inner)
        scope: Scope = {"type": "http", "path": "/api/v1/lookup"}
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_off):
            await middleware(scope, _noop_receive(), send)

        assert inner.received_send is send

    @pytest.mark.asyncio
    async def test_send_wrapper_appends_when_server_timing_present(self, settings_on):
        inner = _RecordingApp(response_headers=[(b"server-timing", b"total;dur=5")])
        middleware = LmlWallTimingMiddleware(inner)
        scope: Scope = {"type": "http", "path": "/api/v1/lookup"}
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            await middleware(scope, _noop_receive(), send)

        # A wrapper WAS built for the matching, gated-on path.
        assert inner.received_send is not send
        start = sent[0]
        assert start["type"] == "http.response.start"
        headers = dict(start["headers"])
        assert headers[b"server-timing"].startswith(b"total;dur=5, lml_wall;dur=")
        # Everything else on the message (status, other keys) is untouched.
        assert start["status"] == 200
        # The body message passes straight through, unmodified.
        assert sent[1] == {"type": "http.response.body", "body": b"{}"}

    @pytest.mark.asyncio
    async def test_send_wrapper_sets_header_when_absent(self, settings_on):
        inner = _RecordingApp(response_headers=[])
        middleware = LmlWallTimingMiddleware(inner)
        scope: Scope = {"type": "http", "path": "/api/v1/lookup"}
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            await middleware(scope, _noop_receive(), send)

        headers = dict(sent[0]["headers"])
        assert _DUR_GRAMMAR.match(headers[b"server-timing"].decode("latin-1"))

    @pytest.mark.asyncio
    async def test_send_wrapper_matches_server_timing_case_insensitively(self, settings_on):
        """Defensive against an upstream app that didn't lowercase the header
        name (ASGI/Starlette always do, but the merge must not assume it).
        Original name casing is preserved; only the match is case-folded.
        """
        inner = _RecordingApp(response_headers=[(b"Server-Timing", b"total;dur=5")])
        middleware = LmlWallTimingMiddleware(inner)
        scope: Scope = {"type": "http", "path": "/api/v1/lookup"}
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with patch("core.server_timing_middleware.get_settings", return_value=settings_on):
            await middleware(scope, _noop_receive(), send)

        headers = sent[0]["headers"]
        assert len(headers) == 1
        name, value = headers[0]
        assert name == b"Server-Timing"  # casing preserved, not forced to lowercase
        assert value.startswith(b"total;dur=5, lml_wall;dur=")

    @pytest.mark.asyncio
    async def test_send_wrapper_injection_failure_forwards_original_message(self, settings_on):
        """If the header-merge step itself raises, the ORIGINAL message must
        still reach ``send`` unmodified — never a broken/partial response.
        """
        inner = _RecordingApp(response_headers=[(b"server-timing", b"total;dur=5")])
        middleware = LmlWallTimingMiddleware(inner)
        scope: Scope = {"type": "http", "path": "/api/v1/lookup"}
        sent: list[Message] = []

        async def send(message: Message) -> None:
            sent.append(message)

        with (
            patch("core.server_timing_middleware.get_settings", return_value=settings_on),
            patch(
                "core.server_timing_middleware._with_appended_server_timing",
                side_effect=RuntimeError("boom"),
            ),
        ):
            await middleware(scope, _noop_receive(), send)

        assert sent[0] == {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"server-timing", b"total;dur=5")],
        }

    def test_with_appended_server_timing_helper_appends_and_sets(self):
        """Unit-level pin on the pure header-merge helper, independent of the
        ASGI plumbing around it.
        """
        assert _with_appended_server_timing([], b"lml_wall;dur=1") == [
            (b"server-timing", b"lml_wall;dur=1")
        ]
        assert _with_appended_server_timing(
            [(b"server-timing", b"total;dur=5")], b"lml_wall;dur=1"
        ) == [(b"server-timing", b"total;dur=5, lml_wall;dur=1")]
