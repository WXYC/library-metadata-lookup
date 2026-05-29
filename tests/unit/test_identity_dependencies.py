"""Unit tests for identity/dependencies.py — get_entity_store probe behaviour."""

from __future__ import annotations

import asyncpg
import pytest

import identity.dependencies as deps
from config.settings import Settings


@pytest.fixture(autouse=True)
def _reset_dep_state():
    """Ensure each test sees a fresh dependency cache."""
    deps._entity_store = None
    deps._entity_pg = None
    deps._entity_probe_failed = False
    yield
    deps._entity_store = None
    deps._entity_pg = None
    deps._entity_probe_failed = False


def _settings(dsn: str | None) -> Settings:
    return Settings(
        discogs_token=None,
        database_url_discogs=dsn,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest.mark.asyncio
async def test_returns_none_when_dsn_unset():
    assert await deps.get_entity_store(_settings(None)) is None


@pytest.mark.asyncio
async def test_returns_none_when_probe_raises_undefined_table(monkeypatch):
    """Probe failure (entity schema not applied) → store disabled."""

    async def boom(self, query, *args):
        raise asyncpg.UndefinedTableError('relation "entity.identity" does not exist')

    monkeypatch.setattr("entity.sources.PgSource.fetchone", boom)
    monkeypatch.setattr(
        "entity.sources.PgSource.close",
        _noop_close,
    )

    result = await deps.get_entity_store(_settings("postgresql://x:y@127.0.0.1:1/z"))
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_probe_raises_oserror(monkeypatch):
    """Probe failure (PG host unreachable) → store disabled."""

    async def boom(self, query, *args):
        raise OSError("connection refused")

    monkeypatch.setattr("entity.sources.PgSource.fetchone", boom)
    monkeypatch.setattr("entity.sources.PgSource.close", _noop_close)

    result = await deps.get_entity_store(_settings("postgresql://x:y@127.0.0.1:1/z"))
    assert result is None


@pytest.mark.asyncio
async def test_probe_failure_is_cached(monkeypatch):
    """A failed probe is not retried on subsequent calls (process-lifetime cache)."""
    call_count = 0

    async def boom(self, query, *args):
        nonlocal call_count
        call_count += 1
        raise asyncpg.UndefinedTableError("nope")

    monkeypatch.setattr("entity.sources.PgSource.fetchone", boom)
    monkeypatch.setattr("entity.sources.PgSource.close", _noop_close)

    settings = _settings("postgresql://x:y@127.0.0.1:1/z")
    assert await deps.get_entity_store(settings) is None
    assert await deps.get_entity_store(settings) is None
    assert await deps.get_entity_store(settings) is None
    assert call_count == 1


@pytest.mark.asyncio
async def test_returns_store_when_probe_succeeds(monkeypatch):
    async def ok(self, query, *args):
        return None  # SELECT ... LIMIT 0 returns no rows

    monkeypatch.setattr("entity.sources.PgSource.fetchone", ok)

    result = await deps.get_entity_store(_settings("postgresql://x:y@127.0.0.1:1/z"))
    assert result is not None


async def _noop_close(self):
    return None
