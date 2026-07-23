"""Integration (``@pytest.mark.pg``) tests for the streaming-catalog schema (LML#842 PR A).

``entity/streaming_catalog.py`` owns the DDL for the row-level PG canonical of
the offline streaming-availability catalog: ``lml_cache.streaming_album`` (one
row per deduplicated library album), ``lml_cache.streaming_album_service``
(one row per album x service probe), ``lml_cache.streaming_track_result``
(compilation-track resolution), and ``lml_cache.streaming_coverage_baseline``
(write-side floor metrics). This file drives the real DDL against an actual
PostgreSQL: bootstrap idempotency, the widen-only service CHECK, the
uniqueness/FK contract, and — the load-bearing part — the no-regress guard
matrix on all four tables that structurally replaces the #672 whole-file
coverage guard: collected streaming data cannot be discarded (a ``url`` or
``slug`` nulled or blanked, a resolved status demoted, a Discogs match
unlinked, a coverage baseline lowered, a row deleted, or a table truncated)
unless the transaction opts in via
``set_config('lml_cache.allow_url_removal', 'on', true)``.

Run with: pytest -m pg -v tests/integration/test_streaming_catalog.py
"""

from __future__ import annotations

from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from entity.sources import PgSource
from entity.streaming_catalog import (
    _SERVICE_IN_LIST,
    _TABLES,
    set_up_streaming_catalog_schema,
)
from tests.integration.conftest import opted_in, skip_if_named_tables_populated

# ``_TABLES`` is parents-before-children (creation order), so its reverse is
# FK-safe for plain DROP TABLE.
_DROP_ORDER = tuple(reversed(_TABLES))

# ``library_ids``/``formats`` are NOT NULL with no default (seed provenance
# must be explicit), so every album insert supplies them.
_INSERT_ALBUM = (
    "INSERT INTO lml_cache.streaming_album "
    "(normalized_artist, normalized_title, display_artist, display_title, "
    "library_ids, formats) "
    "VALUES ($1, $2, $3, $4, '[]'::jsonb, '[]'::jsonb) RETURNING id"
)

_INSERT_SERVICE = (
    "INSERT INTO lml_cache.streaming_album_service "
    "(album_id, service, status, url) VALUES ($1, $2, $3, $4)"
)

_INSERT_SERVICE_WITH_SLUG = (
    "INSERT INTO lml_cache.streaming_album_service "
    "(album_id, service, status, url, slug) VALUES ($1, $2, $3, $4, $5)"
)

_INSERT_SERVICE_FULL = (
    "INSERT INTO lml_cache.streaming_album_service "
    "(album_id, service, status, url, service_item_id, confidence, "
    "matched_artist, matched_title) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)"
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

_SPOTIFY_URL = "https://open.spotify.com/album/2u30gztZTylY4RG7IvfXs8"
_APPLE_URL = "https://music.apple.com/us/album/aluminum-tunes/1443179207"
# Opaque album id — never dereferenced by tests or code; any digits work.
_DEEZER_URL = "https://www.deezer.com/album/103248"

_SERVICE_CHECK_OID = (
    "SELECT oid FROM pg_constraint WHERE conname = 'streaming_album_service_valid' "
    "AND conrelid = 'lml_cache.streaming_album_service'::regclass"
)


@pytest_asyncio.fixture
async def pg_source(pg_pool):
    """A ``PgSource`` borrowing the test pool (no-op close)."""
    return PgSource(pool=pg_pool)


@pytest_asyncio.fixture(autouse=True)
async def set_up_catalog_schema(pg_pool, pg_source):
    """Reset just the four streaming-catalog tables, then apply the DDL.

    Surgical: drops only the tables this suite owns (not the whole
    ``lml_cache`` schema), so sibling LML caches present in the shared test PG
    stay intact. The shared table-scoped guard vetoes on ANY pre-existing row:
    this suite always tears its tables down, so rows here mean either a
    mispointed ``DATABASE_URL_TEST`` (e.g. the shared discogs-cache PG after
    the PR C seed) or crash residue holding real inserts — both veto-worthy,
    neither droppable. Guard functions survive the table drops; the
    bootstrap's ``CREATE OR REPLACE`` re-binds them, which is itself part of
    the idempotency contract under test.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(
            conn, tuple(("lml_cache", table) for table in _DROP_ORDER)
        )
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


async def _make_matched_album(pg_pool) -> int:
    """An album whose Discogs match has been collected (status found plus a
    linked release id) — the state the album-row guard protects."""
    album_id = await _make_album(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE lml_cache.streaming_album SET discogs_status = 'found', "
            "discogs_release_id = 118423, discogs_artist = 'Stereolab', "
            "discogs_title = 'Aluminum Tunes' WHERE id = $1",
            album_id,
        )
    return album_id


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


async def _make_full_service_row(pg_pool) -> int:
    """A found spotify probe with every collected match-metadata column
    populated — the state the metadata-discard guard protects."""
    album_id = await _make_album(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            _INSERT_SERVICE_FULL,
            album_id,
            "spotify",
            "found",
            _SPOTIFY_URL,
            "2u30gztZTylY4RG7IvfXs8",
            0.97,
            "Stereolab",
            "Aluminum Tunes",
        )
    return album_id


async def _make_resolved_track(pg_pool, album_id: int) -> int:
    """A resolved track with the full resolution-metadata group populated
    (linkage, confidences) — the state the resolution-discard guard protects.
    Populated via UPDATE, which doubles as coverage that promotion writes
    pass the guard."""
    track_id = await _make_track(
        pg_pool,
        album_id,
        spotify_url=_SPOTIFY_URL,
        deezer_url=_DEEZER_URL,
        resolution_status="pending",
    )
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE lml_cache.streaming_track_result SET resolution_status = 'api_match', "
            "resolved_via = 'album_match', resolved_album_id = $2, "
            "resolved_release_id = 118423, spotify_confidence = 0.93, "
            "deezer_confidence = 0.88 WHERE id = $1",
            track_id,
            album_id,
        )
    return track_id


async def _seed_baseline(pg_pool, value: int = 294) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO lml_cache.streaming_coverage_baseline (metric, value) "
            "VALUES ('apple_music_found', $1)",
            value,
        )


async def _replace_service_check(conn, in_list: str) -> None:
    """Hand-swap the named CHECK, simulating a table deployed by another code
    version (older = narrower, newer = wider than what this code ships)."""
    await conn.execute(
        "ALTER TABLE lml_cache.streaming_album_service "
        "DROP CONSTRAINT streaming_album_service_valid, "
        f"ADD CONSTRAINT streaming_album_service_valid CHECK (service IN ({in_list}))"
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
    async def test_boot_installs_no_regress_triggers_on_all_four_tables(self, pg_pool):
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT event_object_table FROM information_schema.triggers "
                "WHERE event_object_schema = 'lml_cache' "
                "AND event_object_table = ANY($1::text[])",
                list(_TABLES),
            )
        assert sorted(r["event_object_table"] for r in rows) == sorted(_TABLES)

    @pytest.mark.asyncio
    async def test_boot_installs_status_indexes(self, pg_pool):
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'lml_cache' "
                "AND indexname LIKE 'idx_streaming_%status%'"
            )
        by_name = {r["indexname"]: r["indexdef"] for r in rows}
        # exactly these two — the reshaped composite track index has superseded
        # (dropped) the original single-column idx_streaming_track_result_status
        assert set(by_name) == {
            "idx_streaming_album_service_status",
            "idx_streaming_track_result_status_id",
        }
        # the composite serves get_pending_tracks / get_local_miss_tracks's
        # (resolution_status = X ORDER BY id LIMIT n) from one index
        assert "(resolution_status, id)" in by_name["idx_streaming_track_result_status_id"]

    @pytest.mark.asyncio
    async def test_service_check_rejects_unknown_service(self, pg_pool):
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.CheckViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SERVICE, album_id, "myspace", "pending", None)

    @pytest.mark.asyncio
    async def test_service_url_empty_string_is_rejected_at_insert(self, pg_pool):
        """The pipelines' URL extractors default to ``''`` on a missing key; a
        blank must never enter as collected data (NULL stays the one "no url"
        value — the PR B DAO normalizes ``''`` to NULL at the boundary)."""
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.CheckViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SERVICE, album_id, "spotify", "pending", "")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url_column", ["spotify_url", "deezer_url"])
    async def test_track_url_empty_string_is_rejected_at_insert(self, pg_pool, url_column):
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.CheckViolationError):
            await _make_track(pg_pool, album_id, **{url_column: ""})

    @pytest.mark.asyncio
    async def test_album_normalized_pair_is_unique(self, pg_pool):
        await _make_album(pg_pool)
        with pytest.raises(asyncpg.UniqueViolationError):
            await _make_album(pg_pool)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("omitted", ["library_ids", "formats"])
    async def test_album_seed_provenance_columns_are_required(self, pg_pool, omitted):
        """``library_ids``/``formats`` carry seed provenance on every legacy
        SQLite album row; no column default may paper over a seed (PR C) that
        forgets to map them."""
        kept = "formats" if omitted == "library_ids" else "library_ids"
        with pytest.raises(asyncpg.NotNullViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.streaming_album "
                    "(normalized_artist, normalized_title, display_artist, display_title, "
                    f"{kept}) VALUES ('sun ra', 'lanquidity', 'Sun Ra', 'Lanquidity', '[]'::jsonb)"
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "non_array",
        ["'null'::jsonb", "'\"vinyl\"'::jsonb", "'42'::jsonb", "'{}'::jsonb"],
    )
    @pytest.mark.parametrize("column", ["library_ids", "formats"])
    async def test_album_provenance_jsonb_must_be_an_array(self, pg_pool, column, non_array):
        """jsonb ``null`` (and scalars/objects) satisfy NOT NULL — SQL NULL is
        the only spelling the no-default tripwire above catches. Without a
        shape CHECK, a seed that maps a missing value via ``json.dumps(None)``
        stores ``'null'::jsonb`` and the loud-omission promise never fires;
        consumers doing ``jsonb_array_elements`` then error on scalars or
        silently yield nothing."""
        other = "formats" if column == "library_ids" else "library_ids"
        with pytest.raises(asyncpg.CheckViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.streaming_album "
                    "(normalized_artist, normalized_title, display_artist, display_title, "
                    f"{column}, {other}) VALUES "
                    f"('sun ra', 'lanquidity', 'Sun Ra', 'Lanquidity', {non_array}, '[]'::jsonb)"
                )

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
    async def test_boot_installs_truncate_guards_on_all_four_tables(self, pg_pool):
        """TRUNCATE never fires row-level triggers, so the no-regress guards
        need statement-level BEFORE TRUNCATE companions on every guarded
        table. ``streaming_album``'s is defense-in-depth — bare TRUNCATE on it
        errors at the FK level before any trigger fires, so presence is the
        only testable property. (Queried via ``pg_trigger`` —
        ``information_schema.triggers`` omits TRUNCATE triggers entirely; bit
        32 of ``tgtype`` marks TRUNCATE. The ``tgfoid`` predicate pins that
        each trigger is bound to the shared guard function, not merely that
        SOME truncate trigger exists.)"""
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT c.relname FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'lml_cache' AND NOT t.tgisinternal "
                "AND (t.tgtype::integer & 32) = 32 "
                "AND t.tgfoid = 'lml_cache.guard_streaming_truncate'::regproc "
                "AND c.relname = ANY($1::text[])",
                list(_TABLES),
            )
        assert sorted(r["relname"] for r in rows) == sorted(_TABLES)

    @pytest.mark.asyncio
    async def test_service_row_requires_existing_album(self, pg_pool):
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SERVICE, 999_999, "spotify", "pending", None)

    @pytest.mark.asyncio
    async def test_album_delete_restricted_by_fk_even_with_opt_in(self, pg_pool):
        """ON DELETE RESTRICT is an independent layer under the row guard:
        even an opted-in transaction cannot take collected probe rows down
        with their album — children must be removed explicitly first."""
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.ForeignKeyViolationError):
            async with pg_pool.acquire() as conn:
                async with conn.transaction():
                    await opted_in(conn)
                    await conn.execute(
                        "DELETE FROM lml_cache.streaming_album WHERE id = $1", album_id
                    )

    @pytest.mark.asyncio
    async def test_album_plain_explicit_id_insert_is_rejected(self, pg_pool):
        """IDENTITY is GENERATED ALWAYS: only the seed (PR C), which states
        ``OVERRIDING SYSTEM VALUE`` explicitly, may supply ids. The PR D/E
        script ports come from a direct-sqlite lineage that historically
        managed its own ids — exactly the class of caller that could silently
        insert explicit ids without advancing the sequence, priming later
        pipeline inserts for unique-violation failures."""
        with pytest.raises(asyncpg.GeneratedAlwaysError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.streaming_album "
                    "(id, normalized_artist, normalized_title, display_artist, display_title, "
                    "library_ids, formats) "
                    "VALUES (4242, 'jessica pratt', 'on your own love again', "
                    "'Jessica Pratt', 'On Your Own Love Again', '[]'::jsonb, '[]'::jsonb)"
                )

    @pytest.mark.asyncio
    async def test_album_seed_explicit_id_with_overriding_is_preserved(self, pg_pool):
        """The one-time seed (PR C) preserves SQLite ``albums.id`` values via
        ``OVERRIDING SYSTEM VALUE`` so cross-table references stay valid."""
        async with pg_pool.acquire() as conn:
            row_id = await conn.fetchval(
                "INSERT INTO lml_cache.streaming_album "
                "(id, normalized_artist, normalized_title, display_artist, display_title, "
                "library_ids, formats) OVERRIDING SYSTEM VALUE "
                "VALUES (4242, 'jessica pratt', 'on your own love again', "
                "'Jessica Pratt', 'On Your Own Love Again', '[]'::jsonb, '[]'::jsonb) "
                "RETURNING id"
            )
        assert row_id == 4242

    @pytest.mark.asyncio
    async def test_track_plain_explicit_id_insert_is_rejected(self, pg_pool):
        """Same GENERATED ALWAYS posture on the track table, not just the album's."""
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.GeneratedAlwaysError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO lml_cache.streaming_track_result "
                    "(id, album_id, artist, title, source, source_type) "
                    "VALUES (7777, $1, 'Sessa', 'Grandeza', $2, $3)",
                    album_id,
                    _TRACK_SOURCE,
                    _TRACK_SOURCE_TYPE,
                )

    @pytest.mark.asyncio
    async def test_track_seed_explicit_id_with_overriding_is_preserved(self, pg_pool):
        """The seed also inserts SQLite ``track_results.id`` values verbatim."""
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            row_id = await conn.fetchval(
                "INSERT INTO lml_cache.streaming_track_result "
                "(id, album_id, artist, title, source, source_type) OVERRIDING SYSTEM VALUE "
                "VALUES (7777, $1, 'Sessa', 'Grandeza', $2, $3) RETURNING id",
                album_id,
                _TRACK_SOURCE,
                _TRACK_SOURCE_TYPE,
            )
        assert row_id == 7777

    @pytest.mark.asyncio
    @pytest.mark.parametrize("table", ["streaming_album", "streaming_track_result"])
    async def test_id_columns_are_generated_always(self, pg_pool, table):
        """The plain-INSERT rejections above hold only while the identity
        stays ``GENERATED ALWAYS`` (``attidentity = 'a'``); a drive-by
        relaxation to BY DEFAULT would keep every other test green while
        silently re-opening sequence-desync seeding."""
        async with pg_pool.acquire() as conn:
            # ``attidentity`` is the internal ``"char"`` type, which asyncpg
            # decodes as bytes; cast to text for a plain string comparison.
            attidentity = await conn.fetchval(
                "SELECT attidentity::text FROM pg_attribute "
                "WHERE attrelid = ($1)::regclass AND attname = 'id'",
                f"lml_cache.{table}",
            )
        assert attidentity == "a"

    @pytest.mark.asyncio
    async def test_reference_sql_file_applies_over_a_bootstrapped_schema(self, pg_pool):
        """The unit parity tests prove the ``.sql`` twin *textually* matches
        ``_DDL_STATEMENTS``; this proves the file is executable SQL as shipped
        — comments, dollar-quoted bodies and all — by applying it verbatim
        over the bootstrapped schema (idempotent, like a second boot)."""
        sql = (Path(__file__).resolve().parents[2] / "entity" / "streaming_catalog.sql").read_text(
            encoding="utf-8"
        )
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
            rows = await conn.fetch(
                "SELECT DISTINCT event_object_table FROM information_schema.triggers "
                "WHERE event_object_schema = 'lml_cache' "
                "AND event_object_table = ANY($1::text[])",
                list(_TABLES),
            )
        assert sorted(r["event_object_table"] for r in rows) == sorted(_TABLES)


@pytest.mark.pg
class TestServiceCheckWiden:
    """Boot-time service-CHECK maintenance must be widen-only: it may add
    services the code ships, but must never narrow away (or abort on) services
    a deployed table already admits, and a boot with nothing to add must leave
    the constraint untouched entirely."""

    @pytest.mark.asyncio
    async def test_second_boot_leaves_the_check_constraint_untouched(self, pg_pool, pg_source):
        """Re-booting over an up-to-date table must not DROP+ADD the CHECK:
        the constraint OID is the tell (a rewrite allocates a new one), and a
        rewrite costs an ACCESS EXCLUSIVE lock plus a full-table re-validation
        scan on every production boot."""
        async with pg_pool.acquire() as conn:
            oid_before = await conn.fetchval(_SERVICE_CHECK_OID)
        await set_up_streaming_catalog_schema(pg_source)
        async with pg_pool.acquire() as conn:
            oid_after = await conn.fetchval(_SERVICE_CHECK_OID)
        assert oid_before is not None
        assert oid_after == oid_before

    @pytest.mark.asyncio
    async def test_boot_preserves_a_preexisting_unknown_service(self, pg_pool, pg_source):
        """A deployed table may admit services this code version doesn't ship
        (a newer deploy widened it, then rolled back). Boot must merge, never
        narrow: narrowing re-validates and aborts on the collected rows using
        the extra service, taking the whole bootstrap transaction — and every
        table it creates — down with it."""
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await _replace_service_check(conn, _SERVICE_IN_LIST + ", 'pandora'")
            await conn.execute(
                _INSERT_SERVICE, album_id, "pandora", "found", "https://example.test/pandora"
            )
            oid_before = await conn.fetchval(_SERVICE_CHECK_OID)

        await set_up_streaming_catalog_schema(pg_source)

        async with pg_pool.acquire() as conn:
            # Nothing to add (existing set is a superset), so the constraint
            # must be untouched — and the collected pandora row intact.
            oid_after = await conn.fetchval(_SERVICE_CHECK_OID)
            preserved = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_album_service WHERE service = 'pandora'"
            )
            await conn.execute(_INSERT_SERVICE, album_id, "spotify", "pending", None)
        assert oid_after == oid_before
        assert preserved == 1

    @pytest.mark.asyncio
    async def test_boot_widens_a_narrower_check_then_stabilizes(self, pg_pool, pg_source):
        """The production migration path: a table frozen at an older, narrower
        service set picks up the shipped services on the next boot (one
        constraint rewrite), and the boot after that recognizes the widened
        constraint and leaves it alone. The second half is the round-trip
        guard: a generated CHECK form PG deparses into something the next
        boot can't parse back would rewrite — or corrupt — the list on every
        boot."""
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await _replace_service_check(conn, "'spotify', 'deezer'")
        with pytest.raises(asyncpg.CheckViolationError):
            async with pg_pool.acquire() as conn:
                await conn.execute(_INSERT_SERVICE, album_id, "bandcamp", "pending", None)

        await set_up_streaming_catalog_schema(pg_source)
        async with pg_pool.acquire() as conn:
            await conn.execute(_INSERT_SERVICE, album_id, "bandcamp", "pending", None)
            oid_after_widen = await conn.fetchval(_SERVICE_CHECK_OID)

        await set_up_streaming_catalog_schema(pg_source)
        async with pg_pool.acquire() as conn:
            oid_after_reboot = await conn.fetchval(_SERVICE_CHECK_OID)
        assert oid_after_reboot == oid_after_widen

    @pytest.mark.asyncio
    async def test_boot_recovers_when_constraint_is_absent_over_out_of_set_rows(
        self, pg_pool, pg_source
    ):
        """Constraint dropped out-of-band (manual repair gone sideways) while
        the table holds rows using a service this code doesn't ship. The
        re-ADD must fold the table's live service values into the rebuilt
        list: re-validating against ``_SERVICES`` alone fails on those rows
        and aborts the whole bootstrap transaction — on EVERY boot, until a
        human intervenes, which is a self-selecting incident state (the
        constraint-absent case only arises when something already went
        wrong)."""
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "ALTER TABLE lml_cache.streaming_album_service "
                "DROP CONSTRAINT streaming_album_service_valid"
            )
            await conn.execute(
                _INSERT_SERVICE, album_id, "pandora", "found", "https://example.test/pandora"
            )

        await set_up_streaming_catalog_schema(pg_source)

        async with pg_pool.acquire() as conn:
            oid = await conn.fetchval(_SERVICE_CHECK_OID)
            preserved = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_album_service WHERE service = 'pandora'"
            )
            # The rebuilt constraint must still admit the shipped services...
            await conn.execute(_INSERT_SERVICE, album_id, "spotify", "pending", None)
            # ...and still reject services from neither set.
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(_INSERT_SERVICE, album_id, "myspace", "pending", None)
        assert oid is not None
        assert preserved == 1


@pytest.mark.pg
class TestAlbumRowGuard:
    """The no-regress trigger on ``streaming_album``. The FK RESTRICT only
    protects albums that HAVE child rows; a childless album (just seeded, or
    children removed by an earlier opted-in cleanup) and the collected Discogs
    match linkage need their own guard."""

    @pytest.mark.asyncio
    async def test_childless_album_delete_is_rejected(self, pg_pool):
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute("DELETE FROM lml_cache.streaming_album WHERE id = $1", album_id)

    @pytest.mark.asyncio
    async def test_demoting_discogs_status_found_is_rejected(self, pg_pool):
        album_id = await _make_matched_album(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album SET discogs_status = 'pending' WHERE id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_nulling_discogs_release_id_is_rejected(self, pg_pool):
        album_id = await _make_matched_album(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album SET discogs_release_id = NULL WHERE id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_discogs_match_promotion_and_correction_are_allowed(self, pg_pool):
        album_id = await _make_matched_album(pg_pool)
        async with pg_pool.acquire() as conn:
            # A re-match may CORRECT the linked release; only unlinking loses
            # collected data.
            await conn.execute(
                "UPDATE lml_cache.streaming_album SET discogs_release_id = 456 WHERE id = $1",
                album_id,
            )
            release_id = await conn.fetchval(
                "SELECT discogs_release_id FROM lml_cache.streaming_album WHERE id = $1",
                album_id,
            )
        assert release_id == 456

    @pytest.mark.asyncio
    @pytest.mark.parametrize("column", ["normalized_artist", "normalized_title"])
    async def test_rekeying_album_identity_is_rejected(self, pg_pool, column):
        """Re-keying identity re-labels every collected child probe row: the
        data survives physically but is discarded semantically, attached to
        an album it wasn't collected for."""
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_album SET {column} = 'someone else' WHERE id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_album_id_rekey_via_default_is_rejected(self, pg_pool):
        """``UPDATE ... SET id = DEFAULT`` slips past GENERATED ALWAYS (it
        draws a fresh sequence value rather than erroring); the guard's
        identity predicate is the layer that blocks it."""
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album SET id = DEFAULT WHERE id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_album_id_direct_update_is_rejected_by_identity(self, pg_pool):
        """A direct id rewrite is blocked below the guard layer by GENERATED
        ALWAYS itself."""
        album_id = await _make_album(pg_pool)
        with pytest.raises(asyncpg.GeneratedAlwaysError):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album SET id = id + 100000 WHERE id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank_value", ["NULL", "''"])
    @pytest.mark.parametrize("column", ["discogs_artist", "discogs_title"])
    async def test_discarding_collected_discogs_match_metadata_is_rejected(
        self, pg_pool, column, blank_value
    ):
        """``discogs_artist``/``discogs_title`` record WHAT was matched —
        collected alongside the release id and discarded as surely by nulling
        or blanking them while the link itself survives (the extractors' two
        "empty" spellings, mirroring the service-row metadata gate)."""
        album_id = await _make_matched_album(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_album SET {column} = {blank_value} WHERE id = $1",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_correcting_discogs_match_metadata_is_allowed(self, pg_pool):
        """A re-match REPLACES the collected names; only discarding (→ NULL)
        loses data, mirroring the release-id correction rule."""
        album_id = await _make_matched_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_album SET "
                "discogs_artist = 'Stereolab & Nurse With Wound', "
                "discogs_title = 'Simple Headphone Mind' WHERE id = $1",
                album_id,
            )
            matched = await conn.fetchval(
                "SELECT discogs_artist FROM lml_cache.streaming_album WHERE id = $1", album_id
            )
        assert matched == "Stereolab & Nurse With Wound"

    @pytest.mark.asyncio
    async def test_metadata_update_is_allowed(self, pg_pool):
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_album SET genre = 'Rock', label = 'Drag City' "
                "WHERE id = $1",
                album_id,
            )
            genre = await conn.fetchval(
                "SELECT genre FROM lml_cache.streaming_album WHERE id = $1", album_id
            )
        assert genre == "Rock"

    @pytest.mark.asyncio
    async def test_opt_in_allows_demotion_and_delete(self, pg_pool):
        album_id = await _make_matched_album(pg_pool)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await opted_in(conn)
                await conn.execute(
                    "UPDATE lml_cache.streaming_album SET discogs_status = 'pending', "
                    "discogs_release_id = NULL WHERE id = $1",
                    album_id,
                )
                await conn.execute("DELETE FROM lml_cache.streaming_album WHERE id = $1", album_id)
            remaining = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_album WHERE id = $1", album_id
            )
        assert remaining == 0


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
        an empty string discards a collected URL as surely as NULL. The
        ``match`` pins WHICH layer rejects: the BEFORE-trigger guard fires
        ahead of the column CHECK on UPDATE, and its message is the one that
        points at the opt-in (the CHECK still backstops opted-in blanking and
        NULL→'' on uncollected rows)."""
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET url = '' "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank_value", ["NULL", "''"])
    async def test_blanking_a_collected_slug_is_rejected(self, pg_pool, blank_value):
        """Bandcamp probes can store a slug with no url; the slug IS the
        collected result on such rows and must be guarded like a url."""
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SERVICE_WITH_SLUG, album_id, "bandcamp", "found", None, "stereolab"
            )
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_album_service SET slug = {blank_value} "
                    "WHERE album_id = $1 AND service = 'bandcamp'",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_slug_replacement_is_allowed(self, pg_pool):
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SERVICE_WITH_SLUG, album_id, "bandcamp", "found", None, "stereolab"
            )
            await conn.execute(
                "UPDATE lml_cache.streaming_album_service SET slug = 'stereolab-remastered' "
                "WHERE album_id = $1 AND service = 'bandcamp'",
                album_id,
            )
            slug = await conn.fetchval(
                "SELECT slug FROM lml_cache.streaming_album_service "
                "WHERE album_id = $1 AND service = 'bandcamp'",
                album_id,
            )
        assert slug == "stereolab-remastered"

    @pytest.mark.asyncio
    async def test_opt_in_allows_slug_blanking(self, pg_pool):
        album_id = await _make_album(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                _INSERT_SERVICE_WITH_SLUG, album_id, "bandcamp", "found", None, "stereolab"
            )
            async with conn.transaction():
                await opted_in(conn)
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET slug = NULL "
                    "WHERE album_id = $1 AND service = 'bandcamp'",
                    album_id,
                )
            slug = await conn.fetchval(
                "SELECT slug FROM lml_cache.streaming_album_service "
                "WHERE album_id = $1 AND service = 'bandcamp'",
                album_id,
            )
        assert slug is None

    @pytest.mark.asyncio
    async def test_delete_is_rejected_naming_scope_and_opt_in(self, pg_pool):
        """One raise pins both halves of the contract: the operative message
        body ("DELETE blocked" — so a reword can't hollow out the runbook
        pointer) and the opt-in GUC name."""
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError) as excinfo:
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_album_service WHERE album_id = $1",
                    album_id,
                )
        message = str(excinfo.value)
        assert "DELETE blocked" in message
        assert "allow_url_removal" in message

    @pytest.mark.asyncio
    async def test_rekeying_service_row_service_is_rejected(self, pg_pool):
        """Relabeling a found spotify row as deezer discards the spotify
        result AND forges a deezer one in a single in-set UPDATE the CHECK
        constraint can't see."""
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET service = 'deezer' "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_rekeying_service_row_album_is_rejected(self, pg_pool):
        """Repointing a probe row at a different album mis-attributes the
        collected result; the FK stays satisfied, so only the guard sees it."""
        album_id = await _make_found_service_row(pg_pool)
        other_album_id = await _make_album(pg_pool, artist="juana molina", title="doga")
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET album_id = $2 "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                    other_album_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blank_value", ["NULL", "''"])
    @pytest.mark.parametrize("column", ["service_item_id", "matched_artist", "matched_title"])
    async def test_discarding_collected_match_metadata_is_rejected(
        self, pg_pool, column, blank_value
    ):
        """``service_item_id``/``matched_artist``/``matched_title`` record
        what the probe matched — collected API results, discarded as surely
        by blanking as the url itself."""
        album_id = await _make_full_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_album_service SET {column} = {blank_value} "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_nulling_collected_confidence_is_rejected(self, pg_pool):
        album_id = await _make_full_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET confidence = NULL "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )

    @pytest.mark.asyncio
    async def test_replacing_match_metadata_is_allowed(self, pg_pool):
        """A re-probe may CORRECT the matched names and confidence; only
        discarding loses data."""
        album_id = await _make_full_service_row(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_album_service "
                "SET matched_title = 'Aluminum Tunes (Switched On Volume 3)', confidence = 0.99 "
                "WHERE album_id = $1 AND service = 'spotify'",
                album_id,
            )
            row = await conn.fetchrow(
                "SELECT matched_title, confidence FROM lml_cache.streaming_album_service "
                "WHERE album_id = $1 AND service = 'spotify'",
                album_id,
            )
        assert row["matched_title"] == "Aluminum Tunes (Switched On Volume 3)"
        assert row["confidence"] == 0.99

    @pytest.mark.asyncio
    @pytest.mark.parametrize("demoted_status", ["not_found", "error", "pending", "skipped"])
    async def test_demoting_found_status_is_rejected(self, pg_pool, demoted_status):
        """ANY transition away from ``found`` is a loss — including back to
        ``pending``/``skipped``, which a demotion blocklist would wave through
        (and which re-queues the row for a redundant rate-limited probe)."""
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
    async def test_two_step_status_laundering_is_rejected_at_the_first_hop(self, pg_pool):
        """found → pending → not_found: under a demotion blocklist each hop is
        individually allowed (``pending`` isn't on the list, and hop two
        starts from a non-found status), laundering the demotion the direct
        hop blocks. Blocking every transition out of ``found`` kills the
        launder at hop one."""
        album_id = await _make_found_service_row(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service SET status = 'pending' "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )
        async with pg_pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT status FROM lml_cache.streaming_album_service "
                "WHERE album_id = $1 AND service = 'spotify'",
                album_id,
            )
        assert status == "found"

    @pytest.mark.asyncio
    async def test_opt_in_allows_url_removal(self, pg_pool):
        album_id = await _make_found_service_row(pg_pool)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await opted_in(conn)
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
                await opted_in(conn)
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
                await opted_in(conn)
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
    @pytest.mark.parametrize(
        ("from_status", "to_status"),
        [("local_match", "api_match"), ("api_match", "local_match")],
    )
    async def test_resolved_to_resolved_lateral_move_is_allowed(
        self, pg_pool, from_status, to_status
    ):
        """Re-resolution may flip ``local_match`` <-> ``api_match``; both are
        resolved states, no data is discarded, the guard must not fire."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool, album_id, spotify_url=_SPOTIFY_URL, resolution_status=from_status
        )
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_track_result SET resolution_status = $2 WHERE id = $1",
                track_id,
                to_status,
            )
            status = await conn.fetchval(
                "SELECT resolution_status FROM lml_cache.streaming_track_result WHERE id = $1",
                track_id,
            )
        assert status == to_status

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url_column", ["spotify_url", "deezer_url"])
    async def test_discarding_a_found_track_url_is_rejected(self, pg_pool, url_column):
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool,
            album_id,
            spotify_url=_SPOTIFY_URL,
            deezer_url=_DEEZER_URL,
            resolution_status="api_match",
        )
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_track_result SET {url_column} = NULL "
                    "WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("url_column", ["spotify_url", "deezer_url"])
    async def test_blanking_a_found_track_url_is_rejected(self, pg_pool, url_column):
        """`''` discards as surely as NULL. The ``match`` pins WHICH layer
        rejects: the BEFORE-trigger guard fires ahead of the column CHECK on
        UPDATE, and its message is the one that points at the opt-in."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool,
            album_id,
            spotify_url=_SPOTIFY_URL,
            deezer_url=_DEEZER_URL,
            resolution_status="api_match",
        )
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_track_result SET {url_column} = '' WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resolved_status", ["local_match", "api_match"])
    @pytest.mark.parametrize(
        "demoted_status", ["not_found", "error", "false_positive", "pending", "local_miss"]
    )
    async def test_demoting_resolved_status_is_rejected(
        self, pg_pool, resolved_status, demoted_status
    ):
        """ANY transition out of the resolved pair is a loss — including back
        to ``pending``/``local_miss``, which a demotion blocklist would wave
        through and which silently un-resolves collected matches."""
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
    async def test_two_step_status_laundering_is_rejected_at_the_first_hop(self, pg_pool):
        """local_match → pending → not_found: each hop is individually allowed
        under a demotion blocklist, laundering the demotion the direct hop
        blocks. Blocking every transition out of the resolved pair kills the
        launder at hop one."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool, album_id, spotify_url=_SPOTIFY_URL, resolution_status="local_match"
        )
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET resolution_status = 'pending' "
                    "WHERE id = $1",
                    track_id,
                )
        async with pg_pool.acquire() as conn:
            status = await conn.fetchval(
                "SELECT resolution_status FROM lml_cache.streaming_track_result WHERE id = $1",
                track_id,
            )
        assert status == "local_match"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("column", ["artist", "title"])
    async def test_rekeying_track_identity_is_rejected(self, pg_pool, column):
        """Re-keying (album_id, artist, title) re-attributes a collected
        resolution to a track it wasn't resolved for."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(pg_pool, album_id)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_track_result SET {column} = 'someone else' "
                    "WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    async def test_rekeying_track_album_is_rejected(self, pg_pool):
        """Repointing a track at a different album keeps the FK satisfied, so
        only the guard sees the re-attribution."""
        album_id = await _make_album(pg_pool)
        other_album_id = await _make_album(pg_pool, artist="juana molina", title="doga")
        track_id = await _make_track(pg_pool, album_id)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET album_id = $2 WHERE id = $1",
                    track_id,
                    other_album_id,
                )

    @pytest.mark.asyncio
    async def test_track_id_rekey_via_default_is_rejected(self, pg_pool):
        """``UPDATE ... SET id = DEFAULT`` slips past GENERATED ALWAYS (it
        draws a fresh sequence value rather than erroring); the guard's
        identity predicate is the layer that blocks it — same seam as the
        album twin above."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(pg_pool, album_id)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET id = DEFAULT WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "column",
        [
            "resolved_via",
            "resolved_album_id",
            "resolved_release_id",
            "spotify_confidence",
            "deezer_confidence",
        ],
    )
    async def test_discarding_collected_resolution_metadata_is_rejected(self, pg_pool, column):
        """The resolved_*/confidence group records HOW a track resolved —
        collected linkage the album guard's discogs columns get but tracks
        historically didn't; nulling any of it discards collected data."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_resolved_track(pg_pool, album_id)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE lml_cache.streaming_track_result SET {column} = NULL WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    async def test_blanking_resolved_via_is_rejected(self, pg_pool):
        """``resolved_via`` is TEXT, so ``''`` discards it as surely as NULL
        (same blank-or-null shape as the url and slug guards)."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_resolved_track(pg_pool, album_id)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET resolved_via = '' WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    async def test_correcting_resolution_metadata_is_allowed(self, pg_pool):
        """Re-resolution may CORRECT the linkage; only discarding loses data."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_resolved_track(pg_pool, album_id)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_track_result SET resolved_release_id = 456, "
                "spotify_confidence = 0.99 WHERE id = $1",
                track_id,
            )
            release_id = await conn.fetchval(
                "SELECT resolved_release_id FROM lml_cache.streaming_track_result WHERE id = $1",
                track_id,
            )
        assert release_id == 456

    @pytest.mark.asyncio
    async def test_setting_resolution_status_null_is_rejected_as_a_demotion(self, pg_pool):
        """A NULL new status must hit the guard's demotion gate (a clear,
        opt-in-pointing error), not fall through to the bare NOT NULL
        violation: ``NOT IN`` returns NULL for a NULL operand, and an
        unhardened predicate silently waves NULL past the guard layer."""
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool, album_id, spotify_url=_SPOTIFY_URL, resolution_status="api_match"
        )
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET resolution_status = NULL "
                    "WHERE id = $1",
                    track_id,
                )

    @pytest.mark.asyncio
    async def test_opt_in_allows_status_demotion(self, pg_pool):
        album_id = await _make_album(pg_pool)
        track_id = await _make_track(
            pg_pool, album_id, spotify_url=_SPOTIFY_URL, resolution_status="api_match"
        )
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await opted_in(conn)
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
                await opted_in(conn)
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_track_result WHERE id = $1", track_id
                )
            remaining = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_track_result WHERE id = $1", track_id
            )
        assert remaining == 0


@pytest.mark.pg
class TestTruncateGuard:
    """TRUNCATE never fires row-level triggers, so every guarded table gets a
    statement-level ``BEFORE TRUNCATE`` guard. ``streaming_album``'s fires
    only via CASCADE — bare TRUNCATE on it errors at the FK level first — so
    its behavioral coverage is the CASCADE test; the other three are directly
    truncatable."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "table",
        ["streaming_album_service", "streaming_track_result", "streaming_coverage_baseline"],
    )
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
                await opted_in(conn)
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


@pytest.mark.pg
class TestCoverageBaselineGuard:
    """The no-regress trigger on ``streaming_coverage_baseline``: outside an
    opted-in transaction the floor only ratchets upward, so the export's
    regression assertion can't be quietly defused by lowering (or deleting)
    the baseline it compares against."""

    @pytest.mark.asyncio
    async def test_lowering_a_baseline_is_rejected(self, pg_pool):
        await _seed_baseline(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_coverage_baseline SET value = 200 "
                    "WHERE metric = 'apple_music_found'"
                )

    @pytest.mark.asyncio
    async def test_renaming_a_metric_is_rejected(self, pg_pool):
        """Renaming sidelines the floor: the export looks up
        ``apple_music_found`` and finds nothing — quietly defusing the very
        assertion this guard exists to protect."""
        await _seed_baseline(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_coverage_baseline "
                    "SET metric = 'apple_music_found_old' WHERE metric = 'apple_music_found'"
                )

    @pytest.mark.asyncio
    async def test_rename_plus_reinsert_defusal_dies_at_the_first_step(self, pg_pool):
        """INSERTs are unguarded on purpose: with rename blocked and DELETE
        blocked, re-INSERTing a guarded metric dies on the PK, so the
        INSERT-side defusal (rename the floor away, re-INSERT it lower) has
        no first step — while a brand-new metric's first INSERT stays legal."""
        await _seed_baseline(pg_pool)
        # Step 1 of the attack chain; its standalone rejection is
        # test_renaming_a_metric_is_rejected above.
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_coverage_baseline "
                    "SET metric = 'apple_music_found_old' WHERE metric = 'apple_music_found'"
                )
        async with pg_pool.acquire() as conn:
            with pytest.raises(asyncpg.UniqueViolationError):
                await conn.execute(
                    "INSERT INTO lml_cache.streaming_coverage_baseline (metric, value) "
                    "VALUES ('apple_music_found', 5)"
                )
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.streaming_coverage_baseline (metric, value) "
                "VALUES ('bandcamp_found', 2816)"
            )
            value = await conn.fetchval(
                "SELECT value FROM lml_cache.streaming_coverage_baseline "
                "WHERE metric = 'apple_music_found'"
            )
        assert value == 294

    @pytest.mark.asyncio
    async def test_setting_value_null_is_rejected_as_a_lowering(self, pg_pool):
        """A NULL new value must hit the guard's lowering gate, not fall
        through to the bare NOT NULL violation: ``NEW.value < OLD.value`` is
        NULL for a NULL operand, which an unhardened predicate treats as
        pass."""
        await _seed_baseline(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE lml_cache.streaming_coverage_baseline SET value = NULL "
                    "WHERE metric = 'apple_music_found'"
                )

    @pytest.mark.asyncio
    async def test_raising_and_equal_updates_are_allowed(self, pg_pool):
        await _seed_baseline(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE lml_cache.streaming_coverage_baseline SET value = 301 "
                "WHERE metric = 'apple_music_found'"
            )
            # Equal value (a re-run that found the same coverage) must pass.
            await conn.execute(
                "UPDATE lml_cache.streaming_coverage_baseline "
                "SET value = 301, updated_at = now() WHERE metric = 'apple_music_found'"
            )
            value = await conn.fetchval(
                "SELECT value FROM lml_cache.streaming_coverage_baseline "
                "WHERE metric = 'apple_music_found'"
            )
        assert value == 301

    @pytest.mark.asyncio
    async def test_delete_is_rejected_without_opt_in(self, pg_pool):
        await _seed_baseline(pg_pool)
        with pytest.raises(asyncpg.PostgresError, match="allow_url_removal"):
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_coverage_baseline "
                    "WHERE metric = 'apple_music_found'"
                )

    @pytest.mark.asyncio
    async def test_opt_in_allows_lowering_and_delete(self, pg_pool):
        await _seed_baseline(pg_pool)
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await opted_in(conn)
                await conn.execute(
                    "UPDATE lml_cache.streaming_coverage_baseline SET value = 100 "
                    "WHERE metric = 'apple_music_found'"
                )
                await conn.execute(
                    "DELETE FROM lml_cache.streaming_coverage_baseline "
                    "WHERE metric = 'apple_music_found'"
                )
            remaining = await conn.fetchval(
                "SELECT count(*) FROM lml_cache.streaming_coverage_baseline "
                "WHERE metric = 'apple_music_found'"
            )
        assert remaining == 0
