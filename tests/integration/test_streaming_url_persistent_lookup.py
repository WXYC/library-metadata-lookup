"""Integration tests for the persistent streaming-URL cache (LML#573).

LML#573 generalized the Apple-only ``entity.album_apple_music_lookup_cache``
(LML#571) into the polymorphic ``lml_cache.album_streaming_url_cache`` table,
the cache module ``entity/streaming_url_cache.py``, and the read-through
resolver ``resolve_streaming_url_with_cache``. All unit coverage mocks
``PgSource``; this file is the matching ``@pytest.mark.pg`` layer that drives
the real schema, the real UPSERT, and the real TTL math against an actual
PostgreSQL connection.

Concrete production risks the unit tests cannot catch:

* The named CHECK constraint actually rejects an unknown ``service`` value.
* TIMESTAMPTZ codec drift — asyncpg returns aware ``datetime``; a naive value
  would break the SQL ``last_checked_at > $4`` comparison.
* UPSERT semantics — the composite-PK ``ON CONFLICT`` updates in place.
* ``to_match_form`` normalization parity between SELECT and UPSERT.

Run with: pytest -m pg -v tests/integration/test_streaming_url_persistent_lookup.py
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import asyncpg
import pytest
import pytest_asyncio

import entity.ddl as ddl
from clients.streaming.base import BaseStreamingClient
from entity.streaming_url_cache import (
    get_cached_streaming_url,
    peek_cached_streaming_url,
    resolve_streaming_url_with_cache,
    set_cached_streaming_url,
    set_up_streaming_url_cache_schema,
)
from streaming.models import SourceMatch
from streaming.service import StreamingService
from tests.integration.conftest import skip_if_named_tables_populated

_SERVICE_CASES = [
    ("apple_music_album", "https://music.apple.com/us/album/aluminum-tunes/1234567890"),
    ("spotify_album", "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf"),
    ("bandcamp", "https://juanamolina.bandcamp.com/album/doga"),
]

_SERVICE_CHECK_OID = (
    "SELECT oid FROM pg_constraint "
    "WHERE conname = 'album_streaming_url_cache_service_valid' "
    "AND conrelid = 'lml_cache.album_streaming_url_cache'::regclass"
)


@pytest_asyncio.fixture
async def pg_pool(pg_pool_large):
    """Alias to the conftest ``max_size=4`` pool.

    This module's downstream fixtures and tests request ``pg_pool`` by name and
    historically got a ``max_size=4`` pool; aliasing keeps that cap without
    re-typing every request.
    """
    yield pg_pool_large


@pytest_asyncio.fixture(autouse=True)
async def set_up_cache_schema(pg_pool, pg_source):
    """Reset just ``lml_cache.album_streaming_url_cache``, then apply the DDL.

    Surgical: drops only the one table this suite owns — not the whole
    ``lml_cache`` schema, which hosts every other LML cache (release-
    resolution, rate bucket, override, streaming catalog) — and refuses to run
    at all if that table already holds rows (a mispointed ``DATABASE_URL_TEST``
    at the shared discogs-cache PG would otherwise drop real cached URLs).
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "album_streaming_url_cache"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")
    await set_up_streaming_url_cache_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT count(*)::int AS n FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'album_streaming_url_cache'"
            )
        assert row["n"] == 1

    @pytest.mark.asyncio
    async def test_named_check_constraint_rejects_unknown_service(self, pg_pool):
        # The CHECK must actually fire for a service outside the shipped set.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.album_streaming_url_cache "
                    "(service, artist_normalized, album_normalized, url) "
                    "VALUES ('deezer_album', 'x', 'y', 'https://example.test')"
                )

    @pytest.mark.asyncio
    async def test_alter_widens_check_on_preexisting_table(self, pg_pool, pg_source):
        # The production migration path: a prod table frozen at the pre-bandcamp
        # service set must pick up 'bandcamp' on the next boot. CREATE TABLE IF
        # NOT EXISTS is a no-op on the existing table; the idempotent ALTER is
        # what widens the named CHECK in place.
        async with pg_pool.acquire() as conn:
            # The autouse fixture already veto-checked and dropped this table;
            # drop again here only to replace it with the frozen pre-bandcamp
            # shape (never the whole schema — sibling caches live there too).
            await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS lml_cache")
            await conn.execute(
                "CREATE TABLE lml_cache.album_streaming_url_cache ("
                "  service TEXT NOT NULL,"
                "  artist_normalized TEXT NOT NULL,"
                "  album_normalized TEXT NOT NULL,"
                "  url TEXT,"
                "  last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "  PRIMARY KEY (service, artist_normalized, album_normalized),"
                "  CONSTRAINT album_streaming_url_cache_service_valid CHECK ("
                "    service IN ('apple_music_album', 'spotify_album')"
                "  )"
                ")"
            )

        # Pre-ALTER: bandcamp violates the frozen constraint.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.album_streaming_url_cache "
                    "(service, artist_normalized, album_normalized, url) "
                    "VALUES ('bandcamp', 'x', 'y', 'https://x.bandcamp.com/album/y')"
                )

        # Re-boot: the ALTER widens the CHECK so bandcamp now inserts.
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.album_streaming_url_cache "
                "(service, artist_normalized, album_normalized, url) "
                "VALUES ('bandcamp', 'x', 'y', 'https://x.bandcamp.com/album/y')"
            )
            row = await conn.fetchrow(
                "SELECT url FROM lml_cache.album_streaming_url_cache WHERE service = 'bandcamp'"
            )
        assert row["url"] == "https://x.bandcamp.com/album/y"

    @pytest.mark.asyncio
    async def test_alter_adds_error_column_on_preexisting_table(self, pg_pool, pg_source):
        # LML#1121: the production migration path for a table created before
        # `is_error` existed. CREATE TABLE IF NOT EXISTS is a no-op on the
        # existing table; the idempotent ALTER is what adds the column.
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS lml_cache")
            await conn.execute(
                "CREATE TABLE lml_cache.album_streaming_url_cache ("
                "  service TEXT NOT NULL,"
                "  artist_normalized TEXT NOT NULL,"
                "  album_normalized TEXT NOT NULL,"
                "  url TEXT,"
                "  last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "  PRIMARY KEY (service, artist_normalized, album_normalized),"
                "  CONSTRAINT album_streaming_url_cache_service_valid CHECK ("
                "    service IN ('apple_music_album', 'spotify_album', 'bandcamp')"
                "  )"
                ")"
            )
            await conn.execute(
                "INSERT INTO lml_cache.album_streaming_url_cache "
                "(service, artist_normalized, album_normalized, url) "
                "VALUES ('bandcamp', 'x', 'y', NULL)"
            )

        # Pre-ALTER: the column doesn't exist yet.
        with pytest.raises(asyncpg.PostgresError):
            async with pg_pool.acquire() as conn:
                await conn.fetchval(
                    "SELECT is_error FROM lml_cache.album_streaming_url_cache "
                    "WHERE service = 'bandcamp'"
                )

        # Re-boot: the ALTER adds the column, backfilling existing rows false.
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            is_error = await conn.fetchval(
                "SELECT is_error FROM lml_cache.album_streaming_url_cache "
                "WHERE service = 'bandcamp'"
            )
        assert is_error is False

    @pytest.mark.asyncio
    async def test_alter_lock_timeout_does_not_roll_back_the_table_and_is_retryable(
        self, pg_pool, pg_source, monkeypatch, caplog
    ):
        # LML#1121 FIX 5: PG16 confirmed ``ALTER TABLE ... ADD COLUMN IF NOT
        # EXISTS`` acquires an AccessExclusiveLock even when the column
        # ALREADY exists -- which conflicts with even the weakest read lock
        # (AccessShareLock), i.e. live ``/lookup`` traffic just SELECTing
        # from the table. Pre-fix, that ALTER shared ONE transaction with
        # CREATE TABLE via ``bootstrap_lml_cache_table`` -- a lock_timeout
        # there rolled back the WHOLE boot transaction, and on a table that
        # predates ``is_error`` the column was never created. Every
        # subsequent SELECT/UPSERT would then raise ``UndefinedColumnError``
        # and be swallowed, silently disabling the cache for the process
        # lifetime. Reproduce the exact failure: hold a competing ACCESS
        # SHARE lock (an ordinary read, exactly the ticket's "waits behind
        # live /lookup traffic" scenario) on the table from a second
        # connection while booting, so the ALTER's lock_timeout trips -- then
        # prove (a) the schema/table step still succeeded, (b) the failure
        # is logged loud (ERROR, naming the column), and (c) a retry on the
        # next boot (lock released) actually adds the column. An ACCESS
        # SHARE blocker (rather than ACCESS EXCLUSIVE) is deliberate: it
        # conflicts with the ALTER's AccessExclusiveLock but NOT with the
        # widen step's own read-only regclass lookup, so this isolates the
        # ALTER's failure without also tripping the (out-of-scope, unrelated
        # to ``is_error``) widen step.
        async with pg_pool.acquire() as conn:
            await conn.execute("DROP TABLE IF EXISTS lml_cache.album_streaming_url_cache")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS lml_cache")
            await conn.execute(
                "CREATE TABLE lml_cache.album_streaming_url_cache ("
                "  service TEXT NOT NULL,"
                "  artist_normalized TEXT NOT NULL,"
                "  album_normalized TEXT NOT NULL,"
                "  url TEXT,"
                "  last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "  PRIMARY KEY (service, artist_normalized, album_normalized),"
                "  CONSTRAINT album_streaming_url_cache_service_valid CHECK ("
                "    service IN ('apple_music_album', 'spotify_album', 'bandcamp')"
                "  )"
                ")"
            )

        # A short lock_timeout keeps this test fast; the production value
        # (10s) would make the deliberate trip below take 10 real seconds.
        monkeypatch.setattr(ddl, "_BOOTSTRAP_LOCK_TIMEOUT", "SET LOCAL lock_timeout = '200ms'")

        blocker = await pg_pool.acquire()
        blocker_tx = blocker.transaction()
        await blocker_tx.start()
        await blocker.execute("LOCK TABLE lml_cache.album_streaming_url_cache IN ACCESS SHARE MODE")
        try:
            with caplog.at_level(logging.ERROR, logger="entity.streaming_url_cache"):
                await set_up_streaming_url_cache_schema(pg_source)
        finally:
            await blocker_tx.rollback()
            await pg_pool.release(blocker)

        # The column truly was not added -- the ALTER lock-timed-out rather
        # than silently succeeding some other way.
        async with pg_pool.acquire() as conn:
            missing = await conn.fetchval(
                "SELECT count(*)::int FROM information_schema.columns "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'album_streaming_url_cache' "
                "AND column_name = 'is_error'"
            )
        assert missing == 0
        assert any(
            "is_error" in record.message and record.levelno == logging.ERROR
            for record in caplog.records
        ), "expected an ERROR-level log naming the missing is_error column"

        # Retryable: a second boot, with the competing lock released, adds
        # the column -- the failed ALTER didn't roll back the schema/table
        # step or otherwise poison the table for a future boot.
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            present = await conn.fetchval(
                "SELECT count(*)::int FROM information_schema.columns "
                "WHERE table_schema = 'lml_cache' "
                "AND table_name = 'album_streaming_url_cache' "
                "AND column_name = 'is_error'"
            )
        assert present == 1

    @pytest.mark.asyncio
    async def test_second_boot_leaves_the_check_constraint_untouched(self, pg_pool, pg_source):
        # Re-booting over an up-to-date table must not DROP+ADD the CHECK:
        # the constraint OID is the tell (a rewrite allocates a new one), and
        # a rewrite costs an ACCESS EXCLUSIVE lock plus a full-table
        # re-validation scan on every production boot.
        async with pg_pool.acquire() as conn:
            oid_before = await conn.fetchval(_SERVICE_CHECK_OID)

        await set_up_streaming_url_cache_schema(pg_source)

        async with pg_pool.acquire() as conn:
            oid_after = await conn.fetchval(_SERVICE_CHECK_OID)
        assert oid_before is not None
        assert oid_after == oid_before

    @pytest.mark.asyncio
    async def test_boot_preserves_a_preexisting_superset_constraint(self, pg_pool, pg_source):
        # A deployed table may admit services this code version doesn't ship
        # (a newer deploy widened it, then this build rolled back). Boot must
        # merge, never narrow: narrowing re-validates and would abort on any
        # row using the extra service, taking the whole bootstrap transaction
        # down with it. Simulate by hand-widening the constraint to a strict
        # superset and seeding a row using the extra service.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE lml_cache.album_streaming_url_cache "
                "DROP CONSTRAINT album_streaming_url_cache_service_valid, "
                "ADD CONSTRAINT album_streaming_url_cache_service_valid CHECK ("
                "  service IN ('apple_music_album', 'spotify_album', 'bandcamp', 'deezer_album')"
                ")"
            )
            await conn.execute(
                "INSERT INTO lml_cache.album_streaming_url_cache "
                "(service, artist_normalized, album_normalized, url) "
                "VALUES ('deezer_album', 'x', 'y', 'https://example.test/deezer')"
            )
            oid_before = await conn.fetchval(_SERVICE_CHECK_OID)

        await set_up_streaming_url_cache_schema(pg_source)

        async with pg_pool.acquire() as conn:
            # Nothing to add (existing set is a superset), so the constraint
            # must be untouched — and the collected deezer row intact.
            oid_after = await conn.fetchval(_SERVICE_CHECK_OID)
            preserved = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.album_streaming_url_cache "
                "WHERE service = 'deezer_album'"
            )
        assert oid_after == oid_before
        assert preserved == 1


@pytest.mark.pg
class TestStreamingServiceCheckParity:
    """LML#1037 hard constraint: the LIVE deployed CHECK constraint's value
    set must equal the ``StreamingService`` enum's album-cache-key storage
    keys, byte-for-byte -- not just the code-side ``_SERVICES`` tuple the
    unit-level twin (``tests/unit/test_streaming_url_cache_schema.py::
    TestStreamingServiceParity``) pins. This is the proof that the refactor
    introduced zero data migration: what PostgreSQL actually enforces after a
    fresh boot is exactly what the enum says it should."""

    @pytest.mark.asyncio
    async def test_deployed_check_matches_enum_album_cache_keys(self, pg_pool, pg_source):
        await set_up_streaming_url_cache_schema(pg_source)
        async with pg_pool.acquire() as conn:
            constraint_def = await conn.fetchval(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'album_streaming_url_cache_service_valid' "
                "AND conrelid = 'lml_cache.album_streaming_url_cache'::regclass"
            )
        assert constraint_def is not None
        deployed = set(re.findall(r"'([^']+)'", constraint_def))
        expected = {s.album_cache_key for s in StreamingService if s.album_cache_key is not None}
        assert deployed == expected
        assert deployed == {"apple_music_album", "spotify_album", "bandcamp"}


@pytest.mark.pg
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestRoundTrip:
    @pytest.mark.asyncio
    async def test_set_then_get_returns_cached_url(self, pg_source, service, sample_url):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Stereolab", album="Aluminum Tunes", url=sample_url
        )
        result = await get_cached_streaming_url(
            pg_source, service=service, artist="Stereolab", album="Aluminum Tunes"
        )
        assert result == sample_url

    @pytest.mark.asyncio
    async def test_two_services_coexist_under_same_album_key(self, pg_source, service, sample_url):
        # The service column is part of the PK — the same (artist, album) can
        # carry independent rows per service without colliding.
        other_service = "spotify_album" if service == "apple_music_album" else "apple_music_album"
        await set_cached_streaming_url(
            pg_source, service=service, artist="Juana Molina", album="DOGA", url=sample_url
        )
        await set_cached_streaming_url(
            pg_source,
            service=other_service,
            artist="Juana Molina",
            album="DOGA",
            url="https://other.test/x",
        )
        assert (
            await get_cached_streaming_url(
                pg_source, service=service, artist="Juana Molina", album="DOGA"
            )
            == sample_url
        )
        assert (
            await get_cached_streaming_url(
                pg_source, service=other_service, artist="Juana Molina", album="DOGA"
            )
            == "https://other.test/x"
        )

    @pytest.mark.asyncio
    async def test_known_miss_past_ttl_filtered_by_sql(
        self, pg_source, pg_pool, service, sample_url
    ):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=None
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past = now - timedelta(days=8)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.album_streaming_url_cache SET last_checked_at = $1 "
                "WHERE service = $2 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                past,
                service,
            )
        result = await get_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", now=now
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_live_resolved_writes_row_to_pg(self, pg_source, pg_pool, service, sample_url):
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(
            return_value=SourceMatch(url=sample_url, confidence=95.0)
        )

        outcome = await resolve_streaming_url_with_cache(
            pg_source, client, service=service, artist="Chuquimamani-Condori", album="Edits"
        )

        assert outcome.url == sample_url
        assert outcome.source == "live_resolved"
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, last_checked_at FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'chuquimamani-condori' "
                "AND album_normalized = 'edits'",
                service,
            )
        assert row is not None
        assert row["url"] == sample_url
        assert row["last_checked_at"].tzinfo is not None

    @pytest.mark.asyncio
    async def test_upsert_updates_in_place(self, pg_source, pg_pool, service, sample_url):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Cat Power", album="Moon Pix", url="https://a.test/1"
        )
        await set_cached_streaming_url(
            pg_source, service=service, artist="Cat Power", album="Moon Pix", url=sample_url
        )
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT url FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'cat power' "
                "AND album_normalized = 'moon pix'",
                service,
            )
        assert len(rows) == 1, "ON CONFLICT must update in place, not insert"
        assert rows[0]["url"] == sample_url

    @pytest.mark.asyncio
    async def test_normalization_symmetry(self, pg_source, pg_pool, service, sample_url):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Nilüfer Yanya", album="PAINLESS", url=sample_url
        )
        result = await get_cached_streaming_url(
            pg_source, service=service, artist="Nilufer Yanya", album="painless"
        )
        assert result == sample_url


@pytest.mark.pg
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestErrorTTL:
    """LML#1121: a transport failure writes a short-TTL ``is_error=True`` row
    instead of either the pre-#1115 7-day known-miss or the #1115-#1121
    interim no-write. Real SQL evaluation of the ``is_error``-gated WHERE
    clause is the point of this class -- the unit-level mocks can only pin
    the bind shape, not that PostgreSQL actually treats a fresh error row and
    a fresh genuine miss on independent clocks."""

    @pytest.mark.asyncio
    async def test_live_error_writes_error_row_distinct_from_genuine_miss(
        self, pg_source, pg_pool, service, sample_url
    ):
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(side_effect=RuntimeError("upstream flake"))

        outcome = await resolve_streaming_url_with_cache(
            pg_source, client, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert outcome.url is None
        assert outcome.source == "live_error"
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, is_error FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                service,
            )
        assert row is not None
        assert row["url"] is None
        assert row["is_error"] is True

    @pytest.mark.asyncio
    async def test_fresh_error_row_short_circuits_without_a_second_live_call(
        self, pg_source, pg_pool, service, sample_url
    ):
        # Pins ``resolve_streaming_url_with_cache``'s own cache-first
        # short-circuit: once an error row is written, a caller that does NOT
        # bypass it (the LML#1098 fail-fast probe path) must not call the
        # client again for the same key inside error_ttl. The default
        # ``/lookup`` warm path deliberately does NOT get this benefit -- it
        # passes ``bypass_error_row=True`` precisely so its own retry reaches
        # the network (see the canonical rationale in
        # ``lookup/streaming_url_postprocess.py``); this is NOT a backpressure
        # guarantee on that path (LML#1141 tracks giving it one). LML#1115:
        # the reported source must also say WHICH miss flavor short-circuited
        # it -- a fresh error row is ``cache_error_recent``, distinct from a
        # genuine ``cache_miss_recent``.
        client = AsyncMock(spec=BaseStreamingClient)
        client.find_album_match = AsyncMock(side_effect=RuntimeError("upstream flake"))

        first = await resolve_streaming_url_with_cache(
            pg_source, client, service=service, artist="Sessa", album="Estrela Acesa"
        )
        assert first.source == "live_error"
        client.find_album_match.reset_mock()

        second = await resolve_streaming_url_with_cache(
            pg_source, client, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert second.source == "cache_error_recent"
        client.find_album_match.assert_not_called()

    @pytest.mark.asyncio
    async def test_peek_reports_is_error_true_for_a_fresh_error_row(
        self, pg_source, pg_pool, service, sample_url
    ):
        # LML#1115: the /lookup post-process reads via ``peek_cached_streaming_url``,
        # not ``resolve_streaming_url_with_cache`` -- it must see the same
        # distinction against a REAL row, not just the mocked unit-level SQL bind.
        await set_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            url=None,
            is_error=True,
        )

        url, has_fresh_decision, is_error = await peek_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert url is None
        assert has_fresh_decision is True
        assert is_error is True

    @pytest.mark.asyncio
    async def test_error_row_past_error_ttl_falls_through_to_live_probe(
        self, pg_source, pg_pool, service, sample_url
    ):
        # An error row is fresh only within the much-shorter error_ttl, not
        # the 7-day miss_ttl -- it must self-heal fast once the outage clears.
        await set_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            url=None,
            is_error=True,
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past_error_ttl = now - timedelta(minutes=6)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.album_streaming_url_cache SET last_checked_at = $1 "
                "WHERE service = $2 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                past_error_ttl,
                service,
            )

        result = await get_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            now=now,
            error_ttl=timedelta(minutes=5),
        )

        assert result is None
        # Distinguish "stale, fell through" from "still a fresh decision":
        _, has_fresh_decision, _ = await peek_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            now=now,
            error_ttl=timedelta(minutes=5),
        )
        assert has_fresh_decision is False

    @pytest.mark.asyncio
    async def test_error_and_miss_ttls_are_independent_clocks(
        self, pg_source, pg_pool, service, sample_url
    ):
        # A GENUINE miss (is_error=False) aged past the SHORT error_ttl but
        # still inside the 7-day miss_ttl must stay a fresh known miss -- the
        # two TTL branches must never leak into each other regardless of
        # which happens to be looser.
        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=None
        )
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        past_error_ttl_only = now - timedelta(minutes=6)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.album_streaming_url_cache SET last_checked_at = $1 "
                "WHERE service = $2 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                past_error_ttl_only,
                service,
            )

        _, has_fresh_decision, is_error = await peek_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            now=now,
            error_ttl=timedelta(minutes=5),
        )

        assert has_fresh_decision is True
        # A GENUINE miss, not an error row -- LML#1115's third tuple element
        # must not misreport it.
        assert is_error is False

    @pytest.mark.asyncio
    async def test_genuine_resolve_after_error_clears_is_error(
        self, pg_source, pg_pool, service, sample_url
    ):
        # A resolved URL must never be stuck reading as an error row.
        await set_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            url=None,
            is_error=True,
        )

        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=sample_url
        )

        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, is_error FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                service,
            )
        assert row["url"] == sample_url
        assert row["is_error"] is False

    @pytest.mark.asyncio
    async def test_genuine_miss_after_error_clears_is_error(
        self, pg_source, pg_pool, service, sample_url
    ):
        # A later CONFIRMED absence (live_miss) must also clear a stale error
        # marker -- the row should age on the 7-day miss_ttl from here, not
        # the short error_ttl.
        await set_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            url=None,
            is_error=True,
        )

        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=None
        )

        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, is_error FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                service,
            )
        assert row["url"] is None
        assert row["is_error"] is False


@pytest.mark.pg
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestErrorWriteDoesNotClobberAUrl:
    """LML#1121 FIX 3: two interleaved resolves for the same key -- A resolves
    a real URL and UPSERTs it, B's leg then fails and UPSERTs
    ``url=NULL, is_error=true`` -- must not let B destroy A's URL. Pre-#1121
    the exception path wrote nothing at all, so this interleaving was
    impossible; the ``is_error`` column is what creates it. Real ``ON
    CONFLICT`` evaluation is the point here -- the unit-level mock
    (``tests/unit/test_streaming_url_cache.py``) can only pin the SQL TEXT,
    not that PostgreSQL actually honors the CASE guard."""

    @pytest.mark.asyncio
    async def test_error_write_keeps_the_existing_url(
        self, pg_source, pg_pool, service, sample_url
    ):
        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=sample_url
        )

        await set_cached_streaming_url(
            pg_source,
            service=service,
            artist="Sessa",
            album="Estrela Acesa",
            url=None,
            is_error=True,
        )

        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, is_error FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                service,
            )
        assert row["url"] == sample_url
        assert row["is_error"] is False

    @pytest.mark.asyncio
    async def test_genuine_miss_write_still_overwrites_an_existing_url(
        self, pg_source, pg_pool, service, sample_url
    ):
        # Companion pin: the guard is scoped to an ERROR write only -- a
        # GENUINE miss (is_error=false, e.g. a librarian-curated URL going
        # dead and a fresh probe confirming absence) must still be free to
        # overwrite an existing URL. Out of FIX 3's scope, unchanged.
        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=sample_url
        )

        await set_cached_streaming_url(
            pg_source, service=service, artist="Sessa", album="Estrela Acesa", url=None
        )

        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT url, is_error FROM lml_cache.album_streaming_url_cache "
                "WHERE service = $1 AND artist_normalized = 'sessa' "
                "AND album_normalized = 'estrela acesa'",
                service,
            )
        assert row["url"] is None
        assert row["is_error"] is False
