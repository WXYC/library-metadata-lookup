"""Unit test fixtures."""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

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


def make_mock_conn():
    """Build a mock asyncpg.Connection that supports ``conn.transaction()``.

    Shared across fixtures so the transaction-mock plumbing stays in one place.
    Exposes ``_mock_tx_ctx`` for assertions on transaction entry/exit.
    asyncpg's ``Connection.transaction()`` returns a Transaction object
    supporting ``async with``; ``__aexit__`` with a non-None exc triggers
    ROLLBACK on a real connection.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.executemany = AsyncMock()

    tx_ctx = MagicMock()
    tx_ctx.__aenter__ = AsyncMock(return_value=tx_ctx)
    tx_ctx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx_ctx)
    conn._mock_tx_ctx = tx_ctx
    return conn


@pytest.fixture
def mock_pg_tx():
    """Mock PgSource exposing pool-level methods plus ``acquire()`` for transactions.

    Used by tests of store methods that wrap multi-statement work in
    ``async with pg.acquire() as conn, conn.transaction()`` — including
    ``EntityDeduplicator.merge_group`` and the LML#377 orphan-pass primitives
    (`delete_identity_by_library_name`, `merge_identity_by_library_name`).
    Tests inspect ``mock_pg_tx._mock_conn`` for assertions about which
    queries ran inside the transaction.
    """
    pg = AsyncMock()
    pg.fetchall = AsyncMock(return_value=[])
    pg.fetchone = AsyncMock(return_value=None)
    pg.execute = AsyncMock(return_value="OK")

    conn = make_mock_conn()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="OK")

    acq_ctx = MagicMock()
    acq_ctx.__aenter__ = AsyncMock(return_value=conn)
    acq_ctx.__aexit__ = AsyncMock(return_value=False)
    pg.acquire = MagicMock(return_value=acq_ctx)
    pg._mock_conn = conn
    return pg


@pytest.fixture
def mock_asyncpg_pool():
    """AsyncMock mimicking asyncpg.Pool."""
    pool = AsyncMock()
    pool.fetch = AsyncMock(return_value=[])
    pool.fetchrow = AsyncMock(return_value=None)
    pool.fetchval = AsyncMock(return_value=1)

    conn = make_mock_conn()

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


@pytest.fixture(scope="session")
def es256_keypair() -> tuple[str, str]:
    """Generate an ephemeral ES256 (P-256) keypair for Apple Music client tests.

    Returns ``(private_pem, public_pem)`` as PKCS#8 / SubjectPublicKeyInfo PEM
    strings. Tests sign real JWTs with the private half and verify with the
    public half — the same shape Apple validates, just against a key we own
    instead of one Apple registered. Session-scoped so generation cost (~1 ms)
    isn't paid per test.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


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
    from wxyc_fastapi.observability.cache_stats import _cache_stats_var

    cache_stats_token = _cache_stats_var.set(None)
    set_skip_cache(False)
    yield
    clear_all_caches()
    reset_rate_limiting()
    # Clear library caches (separate from Discogs caches)
    from library.db import clear_library_caches

    clear_library_caches()
    # Restore the ContextVar to its state before the test
    _cache_stats_var.reset(cache_stats_token)
