"""Pure-ASGI middleware surfacing the ``lml_wall`` Server-Timing leg (LML#946).

``RequestTelemetry`` (constructed inside the ``/lookup`` in-flight-cap permit —
see ``lookup/router.py:handle_lookup``) only times the window between its own
construction and ``as_server_timing()``: the pipeline itself. FastAPI's
dependency injection, the Pydantic request deserialization, and the
``LookupResponse`` JSON serialization all happen OUTSIDE that window, inside
the ASGI ``call_next`` a middleware wraps — no ``track_step`` span nor the
``queue_wait``/``discogs`` ``extra`` legs can see that time. A caller measuring
its own wall-clock time for the whole HTTP round trip (request-o-matic's
``lookup_service`` metric) then sees a residual the LML-side header cannot
explain.

This middleware times request-received -> response-ready around the wrapped
ASGI app and APPENDS an ``lml_wall;dur=<ms>`` entry to whatever
``Server-Timing`` header the handler already produced — never overwrites it.
``lml_wall - total - queue_wait`` is then the honest LML-side estimate of DI +
deserialization + serialization overhead outside the tracked pipeline.

It is written as a plain ASGI callable — ``__init__(self, app)`` /
``async def __call__(self, scope, receive, send)`` — rather than a
``starlette.middleware.base.BaseHTTPMiddleware`` subclass. ``BaseHTTPMiddleware``
wraps EVERY request in an anyio task group plus a streaming-response shim
before any of its own dispatch code runs, and that machinery is not free; the
original implementation paid it on every single request (including the hot
``/health`` Railway poll) just to reach a path-string compare that immediately
returned. The ASGI form lets the scope-type check, the ``/api/v1/lookup``
path check, and the ``lml_emit_server_timing`` gate check all short-circuit
straight to ``await self.app(scope, receive, send)`` — the bare wrapped call,
no task group, no response interception — for every request this middleware
doesn't care about. Only a request that is HTTP, on the timed path, with the
gate on pays for a ``send`` wrapper.

Scoped to the single-lookup path only (``/api/v1/lookup`` — see
``_TIMED_PATH``): the endpoint the residual was measured against. Every other
route (``/health``, ``/api/v1/lookup/bulk``, admin, etc.) is a pure passthrough.
Gated on the same ``LML_EMIT_SERVER_TIMING`` kill switch ``lookup/router.py``'s
``_emit_server_timing_header`` reads, so the two either both fire or both stay
silent. Wrapped so a failure here can never break the response: observability
must not break the request path, matching the posture of every other
Server-Timing helper in this repo.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config.settings import get_settings

logger = logging.getLogger(__name__)

# The one route this middleware instruments today. A prefix/set could widen
# later (e.g. to also cover ``/api/v1/lookup/bulk``), but the residual this
# leg exists to explain was measured specifically against single ``/lookup``;
# scoping narrowly keeps every other route, including the hot ``/health``
# check uvicorn/Railway poll, on the zero-wrapper passthrough path below.
_TIMED_PATH = "/api/v1/lookup"

_SERVER_TIMING_HEADER = b"server-timing"


def _format_ms(value: float) -> str:
    """Format a millisecond duration matching ``RequestTelemetry``'s own wire
    format (two decimals, trailing zeros stripped, always fixed-point —
    ``wxyc_fastapi.observability.telemetry._fmt_dur``). Duplicated rather than
    imported: that helper is a private module function, and what matters here
    is an identical *format*, not sharing the implementation.
    """
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _with_appended_server_timing(
    headers: list[tuple[bytes, bytes]], entry: bytes
) -> list[tuple[bytes, bytes]]:
    """Return a NEW headers list with ``entry`` appended to an existing
    ``Server-Timing`` header, or added as a fresh one if absent.

    ASGI header names are ``bytes`` and (per spec, and per Starlette's own
    ``MutableHeaders.__setitem__``) always lowercased by a compliant sender —
    but the match here is still done case-insensitively to stay correct
    against any upstream app that doesn't normalize casing. The original
    name's casing is preserved in the merged tuple; only the comparison is
    case-folded.
    """
    merged: list[tuple[bytes, bytes]] = []
    found = False
    for name, value in headers:
        if name.lower() == _SERVER_TIMING_HEADER:
            merged.append((name, value + b", " + entry))
            found = True
        else:
            merged.append((name, value))
    if not found:
        merged.append((_SERVER_TIMING_HEADER, entry))
    return merged


class LmlWallTimingMiddleware:
    """ASGI middleware appending the ``lml_wall`` Server-Timing leg.

    Registered via ``app.add_middleware(LmlWallTimingMiddleware)`` in
    ``main.py``, positioned so it wraps only the routing/handler/serialization
    layer (see the registration site's comment for the ordering rationale — it
    deliberately excludes the sibling ``posthog_flush_middleware``'s
    synchronous flush from the timed window, so ``lml_wall`` isn't inflated by
    unrelated middleware cost).

    The wrapped app (``self.app``) is never called inside a try/except: a
    genuine failure inside routing must still propagate to Starlette's
    ``ServerErrorMiddleware`` for normal 500 handling. Only this middleware's
    OWN instrumentation — the settings read and the ``send`` message rewrite —
    is guarded, so it can neither turn a good response into a bad one nor mask
    a real error behind a swallowed one.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != _TIMED_PATH:
            await self.app(scope, receive, send)
            return

        try:
            emit = get_settings().lml_emit_server_timing
        except Exception as e:
            logger.warning(
                "Failed to read lml_emit_server_timing setting; skipping lml_wall leg: %s", e
            )
            emit = False

        if not emit:
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                try:
                    dur_ms = (time.perf_counter() - start) * 1000.0
                    entry = f"lml_wall;dur={_format_ms(dur_ms)}".encode("latin-1")
                    headers = _with_appended_server_timing(list(message.get("headers", [])), entry)
                    message = {**message, "headers": headers}
                except Exception as e:
                    logger.warning("Failed to append lml_wall Server-Timing leg: %s", e)
            await send(message)

        await self.app(scope, receive, send_wrapper)
