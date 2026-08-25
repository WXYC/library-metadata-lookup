"""Base HTTP client for streaming service API integrations."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Protocol, runtime_checkable

import httpx
from aiolimiter import AsyncLimiter
from wxyc_fastapi.http import async_singleton

from streaming.models import SourceMatch

# LML#1108: an absolute ``time.monotonic()`` deadline for the CURRENT
# background streaming-URL warm probe, set by
# ``lookup.streaming_warm._warm_streaming_url_cache`` right before
# its ``asyncio.wait_for`` call and reset in a ``finally`` immediately after.
# Any streaming client's own retry/backoff loop MAY read it (via
# :func:`get_probe_deadline`) to bound an inter-attempt sleep against the
# caller's actual remaining budget instead of a fixed attempt-count ceiling
# blind to it -- mirroring ``discogs.service._retry_budget_deadline_var`` /
# ``discogs.admission.retry_429``'s ``budget_deadline`` parameter, LML#758.
#
# ``SpotifyClient`` is the first (and, as of #1108, only) reader: its 429
# handler's ``Retry-After`` sleep has a floor around 5s while the warm
# probe's wall-clock ceiling defaults to 4s, so every warm-path 429 sleep was
# guaranteed to be cancelled mid-flight by the enclosing ``wait_for``
# (LIBRARY-METADATA-LOOKUP-1B: 33k+ occurrences) -- wasting the full ceiling
# on a sleep that could never complete, and holding the warm-concurrency
# semaphore permit for that whole span. Reading this deadline lets the sleep
# give up immediately instead. ``None`` outside a warm probe (interactive
# `/lookup`, offline scripts, any client that never opts in) -- retry loops
# must treat ``None`` as "no deadline, fall back to the pre-existing
# attempt-count-only bound," the same contract ``retry_429`` documents.
#
# Living here (not in ``clients/streaming/spotify.py``) rather than a
# Spotify-only module lets any streaming client opt in the same way, without
# ``lookup/streaming_url_postprocess.py`` needing to import a client-specific
# setter for a module that otherwise treats every service uniformly through
# the shared ``BaseStreamingClient`` contract.
_probe_deadline_var: ContextVar[float | None] = ContextVar(
    "lml_streaming_probe_deadline", default=None
)


def set_probe_deadline(deadline: float | None) -> Token:
    """Set the active background-warm-probe deadline; returns a reset token.

    ``deadline`` is an absolute ``time.monotonic()`` value, not a duration.
    Callers must ``reset_probe_deadline(token)`` in a ``finally`` so the
    deadline doesn't leak into whatever runs next in the same task, mirroring
    ``discogs.service.set_retry_budget_deadline``.
    """
    return _probe_deadline_var.set(deadline)


def reset_probe_deadline(token: Token) -> None:
    """Undo :func:`set_probe_deadline`; see its docstring."""
    _probe_deadline_var.reset(token)


def get_probe_deadline() -> float | None:
    """The active background-warm-probe deadline (an absolute
    ``time.monotonic()`` value), or ``None`` when no probe deadline is active."""
    return _probe_deadline_var.get()


@runtime_checkable
class AlbumMatchClient(Protocol):
    """The single method the streaming-URL cache + warm machinery needs.

    ``entity.streaming_url_cache.resolve_streaming_url_with_cache``,
    ``lookup.streaming_url_postprocess``, and ``lookup.streaming_warm`` call
    exactly one method on a client — ``find_album_match`` — but annotated it as
    ``BaseStreamingClient``, which over-constrains: it demands an httpx
    lifecycle, an ``AsyncLimiter``, and an ``asyncio.Semaphore`` that none of
    those call sites touch.

    LML#1103 made that over-constraint load-bearing. ``YouTubeMusicClient``
    honors the ``find_album_match`` contract but is deliberately NOT a
    ``BaseStreamingClient`` subclass: ``ytmusicapi`` is a *synchronous*,
    ``requests``-based library, so the httpx lifecycle the base class manages
    does not apply and it runs its own ``AsyncLimiter`` + thread offload
    instead. Widening these annotations to this Protocol was the alternative to
    forcing an inheritance that would have carried an unused httpx client into
    every process — structural typing describing what the cache layer actually
    requires, rather than nominal typing describing where a class came from.

    ``BaseStreamingClient`` satisfies this structurally, so every existing
    call site type-checks unchanged with no edit to the base class itself.
    """

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search this service for an album match; see the base class docstring."""
        ...


class BaseStreamingClient:
    """Base class for streaming service HTTP clients.

    Provides lazy httpx.AsyncClient lifecycle management, rate limiting
    via aiolimiter, concurrency control via asyncio.Semaphore, and the
    ``find_album_match`` interface that lets the streaming-check
    orchestrator gather over any client uniformly.

    Args:
        rate_limit: Tuple of (max_rate, time_period) for AsyncLimiter.
        semaphore_limit: Maximum concurrent requests.
    """

    def __init__(self, rate_limit: tuple[float, float], semaphore_limit: int):
        self._rate_limiter = AsyncLimiter(*rate_limit)
        self._semaphore = asyncio.Semaphore(semaphore_limit)
        # Test seam: tests assign ``client._http = mock`` before any call;
        # the singleton getter respects that pre-set value (see below).
        self._http: httpx.AsyncClient | None = None
        # One (getter, closer) pair per instance — subclass clients
        # (Bandcamp, Deezer, Spotify, Apple Music) must stay isolated.
        # See LML#241/#242 for the FD-leak race this lock prevents and
        # WXYC/wxyc-fastapi#5 for the helper.
        self._singleton_get, self._singleton_close = async_singleton(self._make_client)

    async def _make_client(self) -> httpx.AsyncClient:
        """Construct the underlying httpx client. Override to customize."""
        return httpx.AsyncClient(timeout=10.0)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared HTTP client.

        Honors an externally-set ``self._http`` (test injection) so unit
        tests that pre-assign a mock client can keep doing so. Otherwise
        delegates to the lock-guarded singleton.
        """
        if self._http is not None:
            return self._http
        client = await self._singleton_get()
        # Cache on the instance so future calls (and test introspection)
        # see the same value the singleton holds.
        self._http = client
        return client

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        await self._singleton_close()
        self._http = None

    async def find_album_match(self, artist: str, title: str) -> SourceMatch | None:
        """Search this service for an album match and return a normalized verdict.

        The deep contract the streaming-check orchestrator (LML#392) gathers
        over: each provider absorbs its own response-shape adaptation so the
        orchestrator never branches on service identity. Subclasses MUST
        override; the base raises ``NotImplementedError`` so a missing
        override fails loudly the first time the orchestrator dispatches.
        """
        raise NotImplementedError(f"{type(self).__name__} must override find_album_match")
