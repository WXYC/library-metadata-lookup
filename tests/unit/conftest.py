"""Unit test fixtures."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from config.settings import Settings
from discogs.memory_cache import clear_all_caches, set_skip_cache
from discogs.ratelimit import reset_rate_limiting


@contextmanager
def override_deps(app, overrides):
    """Set FastAPI dependency overrides and clear them on exit.

    Args:
        app: The FastAPI application.
        overrides: A dict mapping dependency functions to their replacement values.
    """

    def _make_override(val):
        return lambda: val

    for dep_fn, provider in overrides.items():
        app.dependency_overrides[dep_fn] = _make_override(provider)
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def mock_settings(monkeypatch):
    """Settings with safe test defaults (no real tokens/DSNs)."""
    monkeypatch.setenv("DISCOGS_TOKEN", "")
    monkeypatch.setenv("DATABASE_URL_DISCOGS", "")
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("POSTHOG_API_KEY", "")
    monkeypatch.setenv("ENABLE_TELEMETRY", "false")
    return Settings(
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest.fixture
def mock_asyncpg_pool():
    """AsyncMock mimicking asyncpg.Pool."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=1)

    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()

    # acquire() must return an async context manager (not a coroutine).
    # asyncpg's pool.acquire() returns a PoolAcquireContext that supports
    # `async with pool.acquire() as conn:`.  Use MagicMock so the call
    # is synchronous and the result carries __aenter__/__aexit__.
    acq_ctx = MagicMock()
    acq_ctx.__aenter__ = AsyncMock(return_value=conn)
    acq_ctx.__aexit__ = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=acq_ctx)

    pool._mock_conn = conn  # expose for assertions
    return pool


@pytest.fixture
def mock_posthog_client():
    """Mock PostHog client."""
    client = Mock()
    client.capture = Mock()
    client.flush = Mock()
    client.shutdown = Mock()
    return client


@pytest.fixture(autouse=True)
def reset_caches():
    """Clear all in-memory caches, rate limiting state, and ContextVars between tests."""
    from core.telemetry import _cache_stats_var

    # Capture tokens so we can reset after test
    cache_stats_token = _cache_stats_var.set(None)
    set_skip_cache(False)
    yield
    clear_all_caches()
    reset_rate_limiting()
    # Restore the ContextVar to its state before the test
    _cache_stats_var.reset(cache_stats_token)
