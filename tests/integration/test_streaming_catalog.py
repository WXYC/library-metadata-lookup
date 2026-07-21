"""Integration (``@pytest.mark.pg``) tests for the streaming-catalog schema (LML#842 PR A).

``entity/streaming_catalog.py`` owns the DDL for the row-level PG canonical of
the offline streaming-availability catalog: ``lml_cache.streaming_album`` (one
row per deduplicated library album), ``lml_cache.streaming_album_service``
(one row per album x service probe), ``lml_cache.streaming_track_result``
(compilation-track resolution), and ``lml_cache.streaming_coverage_baseline``
(write-side floor metrics). This file drives the real DDL against an actual
PostgreSQL: bootstrap idempotency, the service CHECK, the uniqueness/FK
contract, and — the load-bearing part — the no-regress trigger matrix that
structurally replaces the #672 whole-file coverage guard: collected streaming
URLs cannot be discarded (``url`` nulled or blanked, a resolved status
demoted, a row deleted, or a table truncated) unless the transaction opts in
via ``set_config('lml_cache.allow_url_removal', 'on', true)``.

Run with: pytest -m pg -v tests/integration/test_streaming_catalog.py
"""

from __future__ import annotations

import asyncpg
import pytest
import pytest_asyncio

from entity.sources import PgSource
from entity.streaming_catalog import set_up_streaming_catalog_schema

_TABLES = (
    "streaming_album",
    "streaming_album_service",
    "streaming_track_result",
    "streaming_coverage_baseline",
)

# Children before parents so plain DROP TABLE succeeds against the FKs.
_DROP_ORDER = (
    "streaming_track_result",
    "streaming_album_service",
    "streaming_album",
    "streaming_coverage_baseline",
)

_INSERT_ALBUM = (
    "INSERT INTO lml_cache.streaming_album "
    "(normalized_artist, normalized_title, display_artist, display_title) "
    "VALUES ($1, $2, $3, $4) RETURNING id"
)

_INSERT_SERVICE = (
    "INSERT INTO lml_cache.streaming_album_service "
    "(album_id, service, status, url) VALUES ($1, $2, $3, $4)"
)

_INSERT_TRACK = (
    "INSERT INTO lml_cache.streaming_track_result "
    "(album_id, artist, title, source, source_type, spotify_url, deezer_url, resolution_status) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id"
)

# Provenance defaults matching the dominant real rows (66,401 of 72,341 prod
# tracks are discogs_tracklist/compilation).
_TRACK_SOURCE = "discogs_tracklist"
_TRACK_SOURCE_TYPE = "compilation"

_OPT_IN = "SELECT set_config('lml_cache.allow_url_removal', 'on', true)"

_SPOTIFY_URL = "https://open.spotify.com/album/2u30gztZTylY4RG7IvfXs8"
_APPLE_URL = "https://music.apple.com/us/album/aluminum-tunes/1443179207"


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


# Well above anything this suite inserts (a handful of rows per test), well
# below any real dataset (the prod catalog holds ~29K albums / 72K tracks).
_POPULATED_GUARD_THRESHOLD = 500


async def _skip_if_catalog_tables_populated(pg_pool) -> None:
    """Refuse to drop tables that already hold real collected data.

    ``DATABASE_URL_TEST`` chooses the target database; if it is ever pointed
    at a populated streaming catalog (e.g. the shared discogs-cache PG after
    the PR C seed), the autouse fixture's ``DROP TABLE`` would destroy
    rate-limited API results. Skip instead — the suite only runs against a
    scratch database.
    """
    async with pg_pool.acquire() as conn:
        for table in _DROP_ORDER:
            regclass = await conn.fetchval("SELECT to_regclass($1)", f"lml_cache.{table}")
            if regclass is None:
                continue
            count = await conn.fetchval(f"SELECT count(*) FROM lml_cache.{table}")
            if count > _POPULATED_GUARD_THRESHOLD:
                pytest.skip(
                    f"lml_cache.{table} holds {count} rows — refusing to drop what looks "
                    "like real collected data; point DATABASE_URL_TEST at a scratch database"
                )


@pytest_asyncio.fixture(autouse=True)
async def set_up_catalog_schema(pg_pool, pg_source):
    """Reset just the four streaming-catalog tables, then apply the DDL.

    Surgical: drops only the tables this suite owns (not the whole
    ``lml_cache`` schema), so sibling LML caches present in the shared test PG
    stay intact, and refuses to run at all if those tables already hold what
    looks like real collected data (see ``_skip_if_catalog_tables_populated``).
    Guard functions survive the table drops; the bootstrap's
    ``CREATE OR REPLACE`` re-binds them, which is itself part of the
    idempotency contract under test.
    """
    await _skip_if_catalog_tables_populated(pg_pool)
    async with pg_pool.acquire() as conn:
        for table in _DROP_ORDER:
            await conn.execute(f"DROP TABLE IF EXISTS lml_cache.{table}")
    await set_up_streaming_catalog_schema(pg_source)
    yield
    async with pg_pool.acquire() as conn:
        for table in _DROP_ORDER:
            await conn.execute(f"DROP TABLE IF EXISTS lml_cache.{table}")


async def _make_album(pg_pool, artist: str = "stereolab", title: str = "aluminum tunes") -> int:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(_INSERT_ALBUM, artist, title, "Stereolab", "Aluminum Tunes")


async def _make_found_service_row(
    pg_pool, service: str = "spotify", url: str = _SPOTIFY_URL
) -> int:
    album_id = await _make_album(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(_INSERT_SERVICE, album_id, service, "found", url)
    return album_id


async def _make_track(
    pg_pool,
    album_id: int,
    *,
    artist: str = "Juana Molina",
    title: str = "la paradoja",
    spotify_url: str | None = None,
    deezer_url: str | None = None,
    resolution_status: str = "pending",
) -> int:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            _INSERT_TRACK,
            album_id,
            artist,
            title,
            _TRACK_SOURCE,
            _TRACK_SOURCE_TYPE,
            spotify_url,
            deezer_url,
            resolution_status,
        )


@pytest.mark.pg
class TestSchemaBootstrap:
    @pytest.mark.asyncio
    async def test_second_boot_is_a_no_op(self, pg_source, pg_pool):
        await set_up_streaming_catalog_schema(pg_source)
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'lml_cache' AND table_name = ANY($1::text[])",
                list(_TABLES),
            )
        assert sorted(r["table_name"] for r in rows) == sorted(_TABLES)

    @pytest.mark.asyncio
    async def test_boot_installs_no_regress_triggers_on_both_result_tables(self, pg_pool):
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT event_object_table FROM information_schema.triggers "
                "WHERE event_object_schema = 'lml_cache' "
                "AND event_object_table = ANY($1::text[])",
                ["streaming_album_service", "streaming_track_result"],
            )
        assert sorted(r["event_object_table"] for r in rows) == [
            "streaming_album_service",
            "streaming_track_result",
        ]

    @pytest.mark.asyncio
    async def test_boot_installs_status_indexes(self, pg_pool):
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'lml_cache' "
                "AND indexname = ANY($1::text[])",
                ["idx_streaming_album_service_status", "idx_streaming_track_result_status"],
            )
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_service_check_rejects_unknown_service(self, pg_pool):
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.CheckViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SERVICE, album_id, "myspace", "pending", None)

    @pytest.mark.asyncio
    async def test_album_normalized_pair_is_unique(self, pg_pool):
        await _make_album(pg_pool)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _make_album(pg_pool)

    @pytest.mark.asyncio
    async def test_track_unique_on_album_artist_title(self, pg_pool):
        album_id = await _make_album(pg_pool)
        await _make_track(pg_pool, album_id, artist="Stereolab", title="Pop Quiz")
        with pytest.raises(asyncpg.UniqueViolationError):
            await _make_track(pg_pool, album_id, artist="Stereolab", title="Pop Quiz")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("omitted", ["source", "source_type"])
    async def test_track_source_columns_are_required(self, pg_pool, omitted):
        """Every legacy SQLite track row carries both provenance columns; the
        seed (PR C) must not be able to silently drop them."""
        album_id = await _make_album(pg_pool)
        values = {"source": _TRACK_SOURCE, "source_type": _TRACK_SOURCE_TYPE, omitted: None}
        with pytest.raises(asyncpg.NotNullViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    _INSERT_TRACK,
                    album_id,
                    "Juana Molina",
                    "la paradoja",
                    values["source"],
                    values["source_type"],
                    None,
                    None,
                    "pending",
                )

    @pytest.mark.asyncio
    async def test_boot_installs_truncate_guards_on_both_result_tables(self, pg_pool):
        """TRUNCATE never fires row-level triggers, so the no-regress guards
        need statement-level BEFORE TRUNCATE companions. (Queried via
        ``pg_trigger`` — ``information_schema.triggers`` omits TRUNCATE
        triggers entirely; bit 32 of ``tgtype`` marks TRUNCATE.)"""
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.relname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'lml_cache' AND NOT t.tgisinternal "
                "AND (t.tgtype::integer & 32) = 32 "
                "AND c.relname = ANY($1::text[])",
                ["streaming_album_service", "streaming_track_result"],
            )
        assert sorted(r["relname"] for r in rows) == [
            "streaming_album_service",
            "streaming_track_result",
        ]

    @pytest.mark.asyncio
    async def test_service_row_requires_existing_album(self, pg_pool):
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SERVICE, 999_999, "spotify", "pending", None)

    @pytest.mark.asyncio
    async def test_album_delete_restricted_while_service_rows_exist(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute("DELETE FROM lml_cache.streaming_album WHERE id = $1", album_id)

    @pytest.mark.asyncio
    async def test_explicit_id_insert_is_preserved(self, pg_pool):
        """The seed (PR C) inserts SQLite ids verbatim; IDENTITY must be BY DEFAULT."""
        async with pg_pool.acquire() as conn:
            row_id = await conn.fetchval(
                "INSERT INTO lml_cache.streaming_album "
                "(id, normalized_artist, normalized_title, display_artist, display_title) "
                "VALUES (4242, 'jessica pratt', 'on your own love again', "
                "'Jessica Pratt', 'On Your Own Love Again') RETURNING id"
            )
        assert row_id == 4242


@pytest.mark.pg
class TestServiceRowGuard:
    """The no-regress trigger on ``streaming_album_service``."""

    @pytest.mark.asyncio
    async def test_pending_to_found_is_allowed(self, pg_pool):
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_SERVICE, album_id, "apple_music", "pending", None)
            await conn.execute(
                "UPDATE lml_cache.streaming_album_service "
                "SET status = 'found', url = $3 WHERE album_id = $1 AND service = $2",
                album_id,
                "apple_music",
                _APPLE_URL,
            )
            url = await conn.fetchval(
                "SELECT url FROM lml_cache.streaming_album_service "
                "WHERE album_id = $1 AND service = $2",
                album_id,
                "apple_music",
            )
        assert url == _APPLE_URL

    @pytest.mark.asyncio
    async def test_url_change_to_new_url_is_allowed(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        replacement = _SPOTIFY_URL + "?si=revalidated"
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_album_service SET url = $2 "
                "WHERE album_id = $1 AND service = 'spotify'",
                album_id,
                replacement,
            )
            url = await conn.fetchval(
                "SELECT url FROM lml_cache.streaming_album_service "
                "WHERE album_id = $1 AND service = 'spotify'",
                album_id,
            )
        assert url == replacement

    @pytest.mark.asyncio
    async def test_nulling_a_found_url_is_rejected(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET url = NULL "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_blanking_a_found_url_to_empty_string_is_rejected(self, pg_pool):
        """The pipelines' URL extractors default to ``''`` on a missing key —
        an empty string discards a collected URL as surely as NULL."""
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET url = '' "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_delete_rejection_message_names_the_scope(self, pg_pool):
        """Pin the operative message body, not just the GUC name, so a future
        reword can't quietly hollow out the runbook pointer."""
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="DELETE blocked"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_album_service WHERE album_id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("demoted_status", ["not_found", "error"])
    async def test_demoting_found_status_is_rejected(self, pg_pool, demoted_status):
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET status = $2 "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                    demoted_status,
                )

    @pytest.mark.asyncio
    async def test_delete_is_rejected_without_opt_in(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_album_service WHERE album_id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_opt_in_allows_url_removal(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_OPT_IN)
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET url = NULL, "
                    "status = 'not_found' WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )
            row = await conn.fetchrow(
                "SELECT url, status FROM lml_cache.streaming_album_service "
                "WHERE album_id = $1 AND service = 'spotify'",
                album_id,
            )
        assert row["url"] is None
        assert row["status"] == "not_found"

    @pytest.mark.asyncio
    async def test_opt_in_allows_delete(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_OPT_IN)
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_album_service WHERE album_id = $1",
                    album_id,
                )
            remaining = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_album_service WHERE album_id = $1",
                album_id,
            )
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_opt_in_is_transaction_local(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_OPT_IN)
            # Same connection, new transaction: the GUC must not leak.
            with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_album_service WHERE album_id = $1",
                    album_id,
                )


@pytest.mark.pg
class TestTrackRowGuard:
    """The no-regress trigger on ``streaming_track_result``."""

    @pytest.mark.asyncio
    async def test_pending_to_resolved_is_allowed(self, pg_pool):
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(pg_pool, album_id)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_track_result SET spotify_url = $2, "
                "resolution_status = 'api_match' WHERE id = $1",
                track_id,
                _SPOTIFY_URL,
            )
            url = await conn.fetchval(
                "SELECT spotify_url FROM lml_cache.streaming_track_result WHERE id = $1",
                track_id,
            )
        assert url == _SPOTIFY_URL

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url_column", ["spotify_url", "deezer_url"])
    @pytest.mark.parametrize("blank_value", ["NULL", "''"])
    async def test_discarding_a_found_track_url_is_rejected(self, pg_pool, url_column, blank_value):
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool,
            album_id,
            spotify_url=_SPOTIFY_URL,
            deezer_url="https://www.deezer.com/album/302127",
            resolution_status="api_match",
        )
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_track_result SET {url_column} = {blank_value} "
                    "WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resolved_status", ["local_match", "api_match"])
    @pytest.mark.parametrize("demoted_status", ["not_found", "error", "false_positive"])
    async def test_demoting_resolved_status_is_rejected(
        self, pg_pool, resolved_status, demoted_status
    ):
        """The album-side guard rejects found→not_found/error; tracks need the
        same for their resolution vocabulary, else a re-run that misses can
        silently un-resolve collected matches."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool, album_id, spotify_url=_SPOTIFY_URL, resolution_status=resolved_status
        )
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET resolution_status = $2 "
                    "WHERE id = $1",
                    track_id,
                    demoted_status,
                )

    @pytest.mark.asyncio
    async def test_opt_in_allows_status_demotion(self, pg_pool):
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool, album_id, spotify_url=_SPOTIFY_URL, resolution_status="api_match"
        )
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_OPT_IN)
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET resolution_status = "
                    "'false_positive', spotify_url = NULL WHERE id = $1",
                    track_id,
                )
            status = await conn.fetchval(
                "SELECT resolution_status FROM lml_cache.streaming_track_result WHERE id = $1",
                track_id,
            )
        assert status == "false_positive"

    @pytest.mark.asyncio
    async def test_delete_is_rejected_without_opt_in(self, pg_pool):
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(pg_pool, album_id)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_track_result WHERE id = $1", track_id
                )

    @pytest.mark.asyncio
    async def test_opt_in_allows_delete(self, pg_pool):
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(pg_pool, album_id)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_OPT_IN)
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_track_result WHERE id = $1", track_id
                )
            remaining = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_track_result WHERE id = $1", track_id
            )
        assert remaining == 0


@pytest.mark.pg
class TestTruncateGuard:
    """TRUNCATE never fires row-level triggers, so the two result tables get
    statement-level ``BEFORE TRUNCATE`` guards. ``streaming_album`` itself is
    covered transitively: bare TRUNCATE errors on the inbound FKs, and CASCADE
    reaches the children's guards."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("table", ["streaming_album_service", "streaming_track_result"])
    async def test_truncate_is_rejected_without_opt_in(self, pg_pool, table):
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(f"TRUNCATE lml_cache.{table}")

    @pytest.mark.asyncio
    async def test_truncate_cascade_from_album_is_rejected(self, pg_pool):
        await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute("TRUNCATE lml_cache.streaming_album CASCADE")

    @pytest.mark.asyncio
    async def test_opt_in_allows_truncate(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_OPT_IN)
                await conn.execute("TRUNCATE lml_cache.streaming_album_service")
            remaining = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_album_service WHERE album_id = $1",
                album_id,
            )
        assert remaining == 0


@pytest.mark.pg
class TestCoverageBaseline:
    @pytest.mark.asyncio
    async def test_metric_upsert_round_trip(self, pg_pool):
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.streaming_coverage_baseline (metric, value) "
                "VALUES ('apple_music_found', 294) "
                "ON CONFLICT (metric) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            )
            await conn.execute(
                "INSERT INTO lml_cache.streaming_coverage_baseline (metric, value) "
                "VALUES ('apple_music_found', 301) "
                "ON CONFLICT (metric) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
            )
            value = await conn.fetchval(
                "SELECT value FROM lml_cache.streaming_coverage_baseline "
                "WHERE metric = 'apple_music_found'"
            )
        assert value == 301
