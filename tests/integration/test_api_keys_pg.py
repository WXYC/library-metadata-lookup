"""Integration (``@pytest.mark.pg``) tests for lml_cache.api_keys.

The unit tests mock ``PgSource``; this is the matching ``@pytest.mark.pg``
layer that drives the real schema, the real UNIQUE(key_hash) constraint, the
``revoked_at IS NULL`` filter, the ``last_used_at`` throttle window, and a
seeded row's round-trip through ``ApiKeyCache.refresh()`` -- against an
actual PostgreSQL connection.

Run with: pytest -m pg -v tests/integration/test_api_keys_pg.py
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from core.api_key_cache import ApiKeyCache, hash_token
from entity.api_keys import (
    fetch_active_keys,
    insert_api_key,
    list_api_keys,
    mark_key_used,
    revoke_api_key,
    set_up_api_keys_schema,
)
from tests.integration.conftest import skip_if_named_tables_populated

_CALLER = "wxyc-canary"
_TOKEN = "lml_test-token-do-not-use"


@pytest_asyncio.fixture(autouse=True)
async def set_up_api_keys_table(pg_pool, pg_source):
    """Reset just ``lml_cache.api_keys``, then apply the DDL.

    Surgical: drops only the one table this suite owns -- not the whole
    ``lml_cache`` schema -- and refuses to run if that table already holds
    rows (a mispointed ``DATABASE_URL_TEST`` would otherwise drop real keys).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "api_keys"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.api_keys")
    await set_up_api_keys_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.api_keys")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_table_created_with_expected_columns(self, pg_pool):
        async with pg_pool.acquire() as conn:
            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'lml_cache' AND table_name = 'api_keys'"
            )
        names = {r["column_name"] for r in cols}
        assert {
            "id",
            "caller_name",
            "key_hash",
            "created_at",
            "last_used_at",
            "revoked_at",
            "note",
        } <= names

    @pytest.mark.asyncio
    async def test_bootstrap_is_idempotent(self, pg_source):
        await set_up_api_keys_schema(pg_source)  # second call must not raise

    @pytest.mark.asyncio
    async def test_key_hash_uniqueness_is_enforced(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.api_keys (caller_name, key_hash) VALUES ($1, $2)",
                "wxyc-canary",
                "dup-hash",
            )
            with pytest.raises(asyncpg.PostgresError):
                await conn.execute(
                    "INSERT INTO lml_cache.api_keys (caller_name, key_hash) VALUES ($1, $2)",
                    "backend-service",
                    "dup-hash",
                )

    @pytest.mark.asyncio
    async def test_two_rows_for_the_same_caller_are_allowed(self, pg_pool):
        # Deliberate: a rotation in progress means an old (not-yet-revoked)
        # and a new (not-yet-confirmed) row for the same caller coexist.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.api_keys (caller_name, key_hash) VALUES ($1, $2)",
                "backend-service",
                "hash-old",
            )
            await conn.execute(
                "INSERT INTO lml_cache.api_keys (caller_name, key_hash) VALUES ($1, $2)",
                "backend-service",
                "hash-new",
            )
            count = await conn.fetchval(
                "SELECT count(*)::int FROM lml_cache.api_keys WHERE caller_name = 'backend-service'"
            )
        assert count == 2


@pytest.mark.pg
class TestInsertFetchRevoke:
    @pytest.mark.asyncio
    async def test_insert_then_fetch_active_returns_the_row(self, pg_source):
        await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))

        rows = await fetch_active_keys(pg_source)

        assert {"key_hash": hash_token(_TOKEN), "caller_name": _CALLER} in [
            {"key_hash": r["key_hash"], "caller_name": r["caller_name"]} for r in rows
        ]

    @pytest.mark.asyncio
    async def test_revoked_row_is_excluded_from_active_fetch(self, pg_source):
        inserted = await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))

        caller = await revoke_api_key(pg_source, key_id=inserted["id"])

        assert caller == _CALLER
        rows = await fetch_active_keys(pg_source)
        assert hash_token(_TOKEN) not in {r["key_hash"] for r in rows}

    @pytest.mark.asyncio
    async def test_revoke_is_idempotent(self, pg_source):
        inserted = await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))

        first = await revoke_api_key(pg_source, key_id=inserted["id"])
        second = await revoke_api_key(pg_source, key_id=inserted["id"])

        assert first == _CALLER
        assert second is None  # already revoked -- a no-op, not an error

    @pytest.mark.asyncio
    async def test_revoke_of_unknown_id_returns_none(self, pg_source):
        assert await revoke_api_key(pg_source, key_id=999999) is None

    @pytest.mark.asyncio
    async def test_list_includes_revoked_rows_but_never_key_hash(self, pg_source):
        inserted = await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))
        await revoke_api_key(pg_source, key_id=inserted["id"])

        rows = await list_api_keys(pg_source)

        assert any(r["id"] == inserted["id"] and r["revoked_at"] is not None for r in rows)
        assert all("key_hash" not in r for r in rows)


@pytest.mark.pg
class TestTouchLastUsed:
    @pytest.mark.asyncio
    async def test_first_touch_sets_last_used_at(self, pg_source, pg_pool):
        await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))

        await mark_key_used(pg_source, hash_token(_TOKEN))

        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_used_at FROM lml_cache.api_keys WHERE key_hash = $1",
                hash_token(_TOKEN),
            )
        assert row["last_used_at"] is not None

    @pytest.mark.asyncio
    async def test_second_touch_within_the_hour_is_a_noop(self, pg_source, pg_pool):
        await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))
        await mark_key_used(pg_source, hash_token(_TOKEN))

        async with pg_pool.acquire() as conn:
            first = await conn.fetchval(
                "SELECT last_used_at FROM lml_cache.api_keys WHERE key_hash = $1",
                hash_token(_TOKEN),
            )

        await mark_key_used(pg_source, hash_token(_TOKEN))

        async with pg_pool.acquire() as conn:
            second = await conn.fetchval(
                "SELECT last_used_at FROM lml_cache.api_keys WHERE key_hash = $1",
                hash_token(_TOKEN),
            )
        assert first == second  # the throttle WHERE clause made the second UPDATE a no-op


@pytest.mark.pg
class TestApiKeyCacheRoundTrip:
    @pytest.mark.asyncio
    async def test_seeded_row_round_trips_through_refresh_and_resolve(self, pg_source):
        await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))
        cache = ApiKeyCache(pg_source)

        await cache.refresh()

        assert cache.resolve(_TOKEN) == _CALLER

    @pytest.mark.asyncio
    async def test_revoked_row_disappears_from_cache_on_next_refresh(self, pg_source):
        inserted = await insert_api_key(pg_source, caller_name=_CALLER, key_hash=hash_token(_TOKEN))
        cache = ApiKeyCache(pg_source)
        await cache.refresh()
        assert cache.resolve(_TOKEN) == _CALLER

        await revoke_api_key(pg_source, key_id=inserted["id"])
        await cache.refresh()

        assert cache.resolve(_TOKEN) is None
