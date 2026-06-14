"""Shared concurrency primitives for bulk FastAPI handlers.

Hoisted out of ``lookup/router.py`` so both ``/api/v1/lookup/bulk`` and the
``/api/v1/cache/refresh-for-identities`` dispatcher (LML#525) can share the
same shape — bounded outer semaphore, client-disconnect sentinel race, and
``CancelledError``-safe future drain.

The three pieces:

- ``max_concurrency_from_env(default)`` — resolves ``LML_BULK_MAX_CONCURRENT``,
  floored at 1 so a misconfigured ``0`` can't hang the gather.
- ``watch_disconnect(request)`` — sentinel coroutine that returns when uvicorn
  delivers an ``http.disconnect`` ASGI message. Used in an ``asyncio.wait``
  race against the actual work so a client abort releases any per-replica
  rate-limit / semaphore permits the gather is still holding.
- ``cancel_and_drain(future)`` — cancel a future and swallow the resulting
  ``CancelledError``. Required because cancellation is asynchronous; merely
  calling ``.cancel()`` doesn't free the permits until the task observes the
  cancel and unwinds.

The env knob (default 10) is shared between both endpoints intentionally —
they have the same outer/inner gate shape and similar per-item work profile.
If observed behavior diverges, splitting into per-endpoint knobs (with the
shared one as fallback) is mechanical.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from fastapi import Request

logger = logging.getLogger(__name__)

_BULK_MAX_CONCURRENT_ENV = "LML_BULK_MAX_CONCURRENT"


def max_concurrency_from_env(default: int) -> int:
    """Resolve ``LML_BULK_MAX_CONCURRENT``, floored at 1.

    Read at request time (not via ``Settings``) to mirror
    ``LML_SEARCH_BUDGET_MS`` in ``core/search.py:resolve_search_budget_ms`` —
    both are runtime knobs.

    Args:
        default: Fallback used when the env var is unset or unparseable.

    Returns:
        The resolved concurrency cap, clamped to ``>= 1``.
    """
    raw = os.getenv(_BULK_MAX_CONCURRENT_ENV)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "Invalid %s=%r, falling back to %d",
            _BULK_MAX_CONCURRENT_ENV,
            raw,
            default,
        )
        return default


async def watch_disconnect(request: Request) -> None:
    """Sentinel task: returns when the client closes its socket.

    uvicorn does not propagate client-side socket close into the handler — the
    handler keeps running and the in-flight ``gather`` keeps draining queued
    Discogs work, holding semaphore permits past the point any caller cares
    about the result. We race this sentinel against the gather and cancel the
    gather when the sentinel wins.

    Awaits ``request.receive()`` directly rather than polling
    ``request.is_disconnected()`` — the polling helper hangs under httpx's
    ASGITransport in tests (anyio CancelScope/asyncio interaction), and the
    direct ``receive()`` loop is the canonical Starlette pattern anyway.
    """
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


async def cancel_and_drain(future: asyncio.Future[Any]) -> None:
    """Cancel a future and swallow the resulting ``CancelledError``.

    Used by every bulk handler that races a gather against a disconnect
    sentinel — twice on each path (gather cleanup on abort + sentinel cleanup
    on happy path) so the two branches stay symmetric and permits actually
    free on cancellation (the bare ``.cancel()`` call is asynchronous — the
    underlying tasks have to observe it and unwind before the permits return).
    """
    future.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await future


__all__ = [
    "cancel_and_drain",
    "max_concurrency_from_env",
    "watch_disconnect",
]
