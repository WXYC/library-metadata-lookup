"""ASGI middleware surfacing the ``lml_wall`` Server-Timing leg (LML#907 follow-up).

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

This middleware times request-received -> response-ready around ``call_next``
and APPENDS an ``lml_wall;dur=<ms>`` entry to whatever ``Server-Timing`` header
the handler already produced — never overwrites it. ``lml_wall - total -
queue_wait`` is then the honest LML-side estimate of DI + deserialization +
serialization overhead outside the tracked pipeline.

Scoped to the single-lookup path only (``/api/v1/lookup`` — see
``_TIMED_PATH``): the endpoint the residual was measured against. Every other
route (``/health``, ``/api/v1/lookup/bulk``, admin, etc.) passes through
untouched at the cost of one path-string compare. Gated on the same
``LML_EMIT_SERVER_TIMING`` kill switch ``lookup/router.py``'s
``_emit_server_timing_header`` reads, so the two either both fire or both stay
silent. Wrapped so a failure here can never break the response: observability
must not break the request path, matching the posture of every other
Server-Timing helper in this repo.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response

from config.settings import get_settings

logger = logging.getLogger(__name__)

# The one route this middleware instruments today. A prefix/set could widen
# later (e.g. to also cover ``/api/v1/lookup/bulk``), but the residual this
# leg exists to explain was measured specifically against single ``/lookup``;
# scoping narrowly keeps the per-request cost (one string compare) off every
# other route, including the hot ``/health`` check uvicorn/Railway poll.
_TIMED_PATH = "/api/v1/lookup"


def _format_ms(value: float) -> str:
    """Format a millisecond duration matching ``RequestTelemetry``'s own wire
    format (two decimals, trailing zeros stripped, always fixed-point —
    ``wxyc_fastapi.observability.telemetry._fmt_dur``). Duplicated rather than
    imported: that helper is a private module function, and what matters here
    is an identical *format*, not sharing the implementation.
    """
    return f"{value:.2f}".rstrip("0").rstrip(".")


async def lml_wall_timing_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Time request-received -> response-ready and append the ``lml_wall`` leg.

    Registered via ``app.middleware("http")`` in ``main.py``, positioned so it
    wraps only the routing/handler/serialization layer (see the registration
    site's comment for the ordering rationale — it deliberately excludes the
    sibling ``posthog_flush_middleware``'s synchronous flush from the timed
    window, so ``lml_wall`` isn't inflated by unrelated middleware cost).

    ``call_next`` itself is never wrapped in try/except: a genuine failure
    inside routing must still propagate to Starlette's ``ServerErrorMiddleware``
    for normal 500 handling. Only the instrumentation AFTER ``call_next``
    returns (the settings read, the header append) is guarded, so this
    middleware can neither turn a good response into a bad one nor mask a real
    error behind a swallowed one.
    """
    if request.url.path != _TIMED_PATH:
        return await call_next(request)

    start = time.perf_counter()
    response = await call_next(request)

    try:
        if not get_settings().lml_emit_server_timing:
            return response
        dur_ms = (time.perf_counter() - start) * 1000.0
        entry = f"lml_wall;dur={_format_ms(dur_ms)}"
        existing = response.headers.get("Server-Timing")
        response.headers["Server-Timing"] = f"{existing}, {entry}" if existing else entry
    except Exception as e:
        logger.warning("Failed to append lml_wall Server-Timing leg: %s", e)

    return response
