"""Telemetry module for tracking request performance with PostHog."""

import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from posthog import Posthog

logger = logging.getLogger(__name__)

DISTINCT_ID = "library-metadata-lookup-service"


@dataclass
class StepResult:
    """Result of a tracked step."""

    duration_ms: float
    success: bool = True
    error_type: str | None = None


@dataclass
class RequestTelemetry:
    """Tracks performance metrics for a single request."""

    steps: dict[str, StepResult] = field(default_factory=dict)
    api_calls: dict[str, int] = field(default_factory=lambda: {"discogs": 0})
    start_time: float = field(default_factory=time.perf_counter)
    _current_step: str | None = field(default=None, repr=False)
    _step_start: float = field(default=0.0, repr=False)

    @contextmanager
    def track_step(self, step_name: str):
        """Context manager to time a step.

        Args:
            step_name: Name of the step being tracked

        Yields:
            None
        """
        self._current_step = step_name
        self._step_start = time.perf_counter()
        error_type = None

        try:
            yield
        except Exception as e:
            error_type = type(e).__name__
            raise
        finally:
            duration_ms = (time.perf_counter() - self._step_start) * 1000
            self.steps[step_name] = StepResult(
                duration_ms=duration_ms,
                success=error_type is None,
                error_type=error_type,
            )
            self._current_step = None

    def record_api_call(self, service: str) -> None:
        """Increment API call counter for a service.

        Args:
            service: Name of the service ("discogs")
        """
        if service in self.api_calls:
            self.api_calls[service] += 1
        else:
            logger.warning(f"Unknown service for API call tracking: {service}")

    def get_total_duration_ms(self) -> float:
        """Get total elapsed time since telemetry was created."""
        return (time.perf_counter() - self.start_time) * 1000

    def get_step_timings(self) -> dict[str, float]:
        """Get timing for each step in milliseconds."""
        return {f"{name}_ms": step.duration_ms for name, step in self.steps.items()}

    def send_to_posthog(
        self,
        posthog_client: Posthog,
        extra_properties: dict[str, Any] | None = None,
    ) -> None:
        """Send all telemetry events to PostHog.

        Args:
            posthog_client: PostHog client instance
            extra_properties: Additional properties to include in the completed event
        """
        extra_properties = extra_properties or {}

        # Send individual step events
        for step_name, step_result in self.steps.items():
            posthog_client.capture(
                distinct_id=DISTINCT_ID,
                event=f"lookup_{step_name}",
                properties={
                    "step": step_name,
                    "duration_ms": round(step_result.duration_ms, 2),
                    "success": step_result.success,
                    "error_type": step_result.error_type,
                },
            )

        # Read cache stats from ContextVar (populated during request lifecycle)
        cache_data = get_cache_stats()
        if cache_data:
            cache_props = cache_data.copy()
        else:
            cache_props = {
                "memory_hits": 0,
                "pg_hits": 0,
                "pg_misses": 0,
                "api_calls": 0,
                "pg_time_ms": 0.0,
                "api_time_ms": 0.0,
            }

        # Send summary event
        posthog_client.capture(
            distinct_id=DISTINCT_ID,
            event="lookup_completed",
            properties={
                "total_duration_ms": round(self.get_total_duration_ms(), 2),
                "steps": self.get_step_timings(),
                "api_calls": self.api_calls.copy(),
                "cache": cache_props,
                **extra_properties,
            },
        )

        logger.debug(
            f"Sent telemetry: {len(self.steps)} steps, total {self.get_total_duration_ms():.1f}ms"
        )


# ---------------------------------------------------------------------------
# Per-request cache stats via ContextVar
# ---------------------------------------------------------------------------

_cache_stats_var: ContextVar[dict] = ContextVar("cache_stats")


def init_cache_stats() -> None:
    """Initialize cache stats for the current request context."""
    _cache_stats_var.set(
        {
            "memory_hits": 0,
            "pg_hits": 0,
            "pg_misses": 0,
            "api_calls": 0,
            "pg_time_ms": 0.0,
            "api_time_ms": 0.0,
        }
    )


def record_memory_cache_hit() -> None:
    """Record an in-memory TTL cache hit in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["memory_hits"] += 1


def record_pg_cache_hit() -> None:
    """Record a PostgreSQL cache hit in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["pg_hits"] += 1


def record_pg_cache_miss() -> None:
    """Record a PostgreSQL cache miss in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["pg_misses"] += 1


def record_discogs_api_call() -> None:
    """Record a Discogs API call in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["api_calls"] += 1


def record_pg_time(ms: float) -> None:
    """Accumulate PostgreSQL cache query time in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["pg_time_ms"] += ms


def record_api_time(ms: float) -> None:
    """Accumulate Discogs API call time in the current request context."""
    stats = _cache_stats_var.get(None)
    if stats is not None:
        stats["api_time_ms"] += ms


def get_cache_stats() -> dict | None:
    """Get cache stats for the current request context, or None if not initialized."""
    return _cache_stats_var.get(None)
