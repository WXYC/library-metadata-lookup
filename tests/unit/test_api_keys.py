"""Unit tests for entity/api_keys.py (query helpers).

PG is mocked with ``AsyncMock(spec=PgSource)``; ``tests/integration/test_api_keys_pg.py``
covers the real SQL end-to-end (including the ``revoked_at IS NULL`` filter and
the ``last_used_at`` throttle window).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from entity.api_keys import (
    fetch_active_keys,
    insert_api_key,
    list_api_keys,
    mark_key_used,
    revoke_api_key,
)
from entity.sources import PgSource


@pytest.mark.asyncio
class TestFetchActiveKeys:
    async def test_returns_rows_from_pg(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[{"key_hash": "abc123", "caller_name": "wxyc-canary"}])

        rows = await fetch_active_keys(pg)

        assert rows == [{"key_hash": "abc123", "caller_name": "wxyc-canary"}]
        pg.fetchall.assert_awaited_once()
        sql = pg.fetchall.await_args.args[0]
        assert "WHERE revoked_at IS NULL" in sql

    async def test_returns_empty_list_when_no_active_keys(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])

        assert await fetch_active_keys(pg) == []


@pytest.mark.asyncio
class TestMarkKeyUsed:
    async def test_executes_touch_sql_with_key_hash(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="UPDATE 1")

        await mark_key_used(pg, "abc123")

        pg.execute.assert_awaited_once()
        args = pg.execute.await_args.args
        assert "UPDATE lml_cache.api_keys SET last_used_at = now()" in args[0]
        assert args[1] == "abc123"

    async def test_swallows_pg_errors(self, caplog):
        # Fire-and-forget: a write failure must never propagate to the
        # scheduling background task.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("pg blip"))

        with caplog.at_level(logging.ERROR, logger="entity.api_keys"):
            await mark_key_used(pg, "abc123")  # must not raise

        assert any(r.name == "entity.api_keys" for r in caplog.records)


@pytest.mark.asyncio
class TestInsertApiKey:
    async def test_inserts_and_returns_id_and_created_at(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"id": 4, "created_at": "2026-07-23T00:00:00Z"})

        result = await insert_api_key(
            pg, caller_name="wxyc-canary", key_hash="abc123", note="initial seed"
        )

        assert result == {"id": 4, "created_at": "2026-07-23T00:00:00Z"}
        pg.fetchone.assert_awaited_once()
        args = pg.fetchone.await_args.args
        assert "INSERT INTO lml_cache.api_keys" in args[0]
        assert args[1:] == ("wxyc-canary", "abc123", "initial seed")

    async def test_note_defaults_to_none(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"id": 1, "created_at": "2026-07-23T00:00:00Z"})

        await insert_api_key(pg, caller_name="wxyc-canary", key_hash="abc123")

        args = pg.fetchone.await_args.args
        assert args[3] is None


@pytest.mark.asyncio
class TestRevokeApiKey:
    async def test_returns_caller_name_on_success(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"caller_name": "wxyc-canary"})

        result = await revoke_api_key(pg, key_id=4)

        assert result == "wxyc-canary"
        pg.fetchone.assert_awaited_once()
        args = pg.fetchone.await_args.args
        assert "UPDATE lml_cache.api_keys SET revoked_at = now()" in args[0]
        assert args[1] == 4

    async def test_returns_none_when_already_revoked_or_missing(self):
        # Idempotent: revoking a nonexistent or already-revoked id is a no-op,
        # not an error -- the caller (the CLI) distinguishes on the None.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        assert await revoke_api_key(pg, key_id=999) is None


@pytest.mark.asyncio
class TestListApiKeys:
    async def test_returns_rows_without_key_hash_column(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "id": 1,
                    "caller_name": "wxyc-canary",
                    "created_at": "2026-07-01T00:00:00Z",
                    "last_used_at": None,
                    "revoked_at": None,
                }
            ]
        )

        rows = await list_api_keys(pg)

        assert rows[0]["caller_name"] == "wxyc-canary"
        assert "key_hash" not in rows[0]
        sql = pg.fetchall.await_args.args[0]
        assert "key_hash" not in sql
        assert "ORDER BY id" in sql
