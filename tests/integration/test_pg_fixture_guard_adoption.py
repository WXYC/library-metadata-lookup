"""Regression net for LML#768 — the entity-dropping pg fixtures adopt the
shared data-safety guard and the hoisted ``ENTITY_IDENTITY_DDL``.

#764 landed ``skip_if_drop_targets_populated`` + ``ENTITY_IDENTITY_DDL`` in
``conftest.py`` and adopted them in the two artists-route suites. The rest
of the pg suite predated both: four fixtures ran ``DROP SCHEMA entity
CASCADE`` with no populated-DB veto (a mispointed ``DATABASE_URL_TEST`` ->
the local discogs-cache would wipe hours of rate-limited reconciliation),
and five files carried verbatim ``entity.identity`` DDL copies that a
discogs-cache alembic column-add (WXYC/wiki#83) would silently skip.

Two nets here:

* A **static** check (no PG) that every migrated file imports the shared
  guard + DDL constant and carries no inline ``CREATE TABLE entity.identity``
  — this is what would have flagged a missed file during the hoist.
* A **behavioral** ``pg`` check that ``skip_if_drop_targets_populated``
  actually vetoes (with the promised message) when a scratch
  ``entity.identity`` holds a row — the live half of the AC's spot-check,
  automated so a future refactor can't quietly defang the guard.
"""

from __future__ import annotations

import re
from pathlib import Path

import asyncpg
import pytest

from tests.integration.conftest import (
    ENTITY_IDENTITY_DDL,
    RECONCILER_TABLE_DDL,
    skip_if_drop_targets_populated,
    skip_if_named_tables_populated,
)

# Every file the #768 hoist touches: the four previously-unguarded fixtures
# plus ``test_entity_resolution.py`` (fifth DDL copy + its near-variant
# guard). All must route ``entity.identity`` creation through the shared
# constant and gate their drops on the shared guard.
_MIGRATED_FILES = (
    "test_bulk_resolve_libraries.py",
    "test_release_identity.py",
    "test_cache_refresh_for_identities.py",
    "test_charset_torture_entity_identity.py",
    "test_entity_resolution.py",
)

_INLINE_IDENTITY_DDL = re.compile(r"CREATE\s+TABLE\s+entity\.identity", re.IGNORECASE)

# Every ``lml_cache.*`` cache suite whose fixture drops tables it owns. Each
# must veto through the table-scoped guard (``skip_if_named_tables_populated``)
# and drop only its own tables — never ``DROP SCHEMA lml_cache CASCADE``,
# which takes every sibling cache (and, post-LML#842, the seeded streaming
# catalog) with it.
_LML_CACHE_FIXTURE_FILES = (
    "test_release_resolution_cache.py",
    "test_streaming_url_persistent_lookup.py",
    "test_track_streaming_url_cache_pg.py",
    "test_purge_va_apple_track_cache_pg.py",
    "test_streaming_catalog.py",
    "test_streaming_catalog_dao.py",
    "test_library_release_override.py",
    "test_seed_library_release_overrides.py",
    "test_api_keys_pg.py",
    "test_compilation_track_location_schema.py",
    "test_build_compilation_track_location_pg.py",
    "test_compilation_track_location_read.py",
    "test_compilation_track_identity_schema.py",
    "test_compilation_track_identity_store.py",
)

# Any DROP SCHEMA aimed at lml_cache, however spelled (with/without IF
# EXISTS, arbitrary whitespace) — mirrors the ``_INLINE_IDENTITY_DDL``
# pattern above so a cosmetic rewrite can't slip past an exact literal.
_DROP_LML_CACHE_SCHEMA = re.compile(r"DROP\s+SCHEMA\s+(?:IF\s+EXISTS\s+)?lml_cache", re.IGNORECASE)


def _source(name: str) -> str:
    return (Path(__file__).parent / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _MIGRATED_FILES)
def test_migrated_file_has_no_inline_identity_ddl(name: str) -> None:
    """No file recreates ``entity.identity`` by hand — a stale copy would
    stay green against a schema the shared constant has since grown."""
    assert not _INLINE_IDENTITY_DDL.search(_source(name)), (
        f"{name} still carries an inline `CREATE TABLE entity.identity` — "
        "use conftest.ENTITY_IDENTITY_DDL so a discogs-cache column-add "
        "can't leave this suite green against a stale shape."
    )


@pytest.mark.parametrize("name", _MIGRATED_FILES)
def test_migrated_file_imports_shared_guard_and_ddl(name: str) -> None:
    """Every entity-dropping fixture file pulls the shared helpers in, so
    its drops are veto-gated and its stub matches prod."""
    src = _source(name)
    assert "skip_if_drop_targets_populated" in src, (
        f"{name} does not reference the shared data-safety guard."
    )
    assert "ENTITY_IDENTITY_DDL" in src, f"{name} does not reference the shared entity DDL."


@pytest.mark.parametrize("name", _LML_CACHE_FIXTURE_FILES)
def test_lml_cache_fixture_file_uses_table_scoped_guard(name: str) -> None:
    """Every lml_cache-dropping suite vetoes through the shared table-scoped
    guard before touching anything — a mispointed ``DATABASE_URL_TEST`` at the
    shared discogs-cache PG must skip, not silently drop collected cache data."""
    src = _source(name)
    assert "skip_if_named_tables_populated" in src, (
        f"{name} does not reference the shared table-scoped data-safety guard."
    )


def test_every_lml_cache_dropping_suite_is_registered() -> None:
    """Discovery net for the roster itself: the two checks above only bind
    files someone remembered to add to ``_LML_CACHE_FIXTURE_FILES``. Sweep
    every integration file for the co-occurrence of ``lml_cache`` with a
    ``DROP TABLE``/``TRUNCATE`` (coarse on purpose — a qualified name built
    in an f-string still names the schema somewhere in the file) and require
    each hit to be on the roster, so a new cache suite can't sit outside the
    guard checks unnoticed. This file self-excludes: it exercises the guard
    against scratch lml_cache tables by design."""
    pattern = re.compile(r"DROP\s+TABLE|TRUNCATE", re.IGNORECASE)
    flagged = {
        path.name
        for path in Path(__file__).parent.glob("*.py")
        if path.name != Path(__file__).name
        and "lml_cache" in (src := path.read_text(encoding="utf-8"))
        and pattern.search(src)
    }
    unregistered = flagged - set(_LML_CACHE_FIXTURE_FILES)
    assert not unregistered, (
        f"{sorted(unregistered)} touch lml_cache with DROP TABLE/TRUNCATE but are not in "
        "_LML_CACHE_FIXTURE_FILES — add them so the guard-adoption checks bind them "
        "(or route their teardown through skip_if_named_tables_populated first)."
    )


@pytest.mark.parametrize("name", _LML_CACHE_FIXTURE_FILES)
def test_lml_cache_fixture_file_never_drops_the_whole_schema(name: str) -> None:
    """No cache suite may ``DROP SCHEMA lml_cache CASCADE`` — the schema hosts
    every LML cache (streaming-URL, release-resolution, rate bucket, override,
    streaming catalog), so a schema-wide drop has cross-suite blast radius and
    bypasses any per-table populated veto."""
    assert not _DROP_LML_CACHE_SCHEMA.search(_source(name)), (
        f"{name} drops the whole lml_cache schema — drop only the tables the "
        "suite owns, behind skip_if_named_tables_populated."
    )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_named_tables_guard_vetoes_only_on_rows(pg_pool) -> None:
    """The table-scoped guard's contract: a missing table passes (first-run
    bootstrap), an empty table passes (clean teardown state), a single row
    vetoes with a message naming the table."""
    scratch = ("lml_cache", "guard_probe_scratch")
    async with pg_pool.acquire() as conn:
        try:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS lml_cache")
            await conn.execute("DROP TABLE IF EXISTS lml_cache.guard_probe_scratch")

            # Missing table: no veto.
            await skip_if_named_tables_populated(conn, (scratch,))

            # Empty table: no veto.
            await conn.execute("CREATE TABLE lml_cache.guard_probe_scratch (id int)")
            await skip_if_named_tables_populated(conn, (scratch,))

            # One row: veto, naming the table.
            await conn.execute("INSERT INTO lml_cache.guard_probe_scratch VALUES (1)")
            with pytest.raises(pytest.skip.Exception) as excinfo:
                await skip_if_named_tables_populated(conn, (scratch,))
            message = str(excinfo.value)
            assert "Refusing to DROP" in message
            assert "lml_cache.guard_probe_scratch" in message
        finally:
            await conn.execute("DROP TABLE IF EXISTS lml_cache.guard_probe_scratch")


@pytest.mark.pg
@pytest.mark.asyncio
async def test_guard_vetoes_when_scratch_identity_holds_a_row(pg_pool) -> None:
    """Live half of the AC spot-check: one seeded ``entity.identity`` row
    makes the shared guard skip with the promised veto message.

    Builds the scratch schema through ``ENTITY_IDENTITY_DDL`` (the exact
    stub the migrated fixtures now use), seeds a single row, and asserts
    the guard the fixtures call FIRST raises ``Skipped`` naming the table.
    Teardown drops the whole schema so the next test's sweep sees zero
    entity relations — the no-false-veto invariant the issue calls out.
    """
    async with pg_pool.acquire() as conn:
        # Pre-condition: a mispointed real cache would already fail here; on
        # the clean test DB this is a no-op guard that must NOT veto.
        await skip_if_drop_targets_populated(conn, ())
        try:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")
            await conn.execute("CREATE SCHEMA entity")
            await conn.execute(ENTITY_IDENTITY_DDL)
            await conn.execute(
                "INSERT INTO entity.identity (library_name, discogs_artist_id) "
                "VALUES ('Stereolab', 2154)"
            )

            with pytest.raises(pytest.skip.Exception) as excinfo:
                await skip_if_drop_targets_populated(conn, ())
            message = str(excinfo.value)
            assert "Refusing to DROP" in message
            assert "entity.identity" in message
        finally:
            await conn.execute("DROP SCHEMA IF EXISTS entity CASCADE")


@pytest.mark.pg
@pytest.mark.asyncio
async def test_trigram_fixture_setup_survives_empty_release_artist_residue(pg_pool) -> None:
    """An *empty* residual ``release_artist`` must not error the trigram setup.

    ``skip_if_drop_targets_populated`` only vetoes when the target holds rows,
    so an empty ``release_artist`` left by a crash-interrupted prior run (SIGKILL
    after the fixture's ``CREATE TABLE`` but before its teardown ``DROP``) sails
    past the guard. Before the drop-before-create fix, the fixture's bare
    ``CREATE TABLE release_artist`` then raised ``DuplicateTableError`` — where
    the pre-#768 existence check would have skipped. This reproduces that
    residue and asserts the migrated setup sequence (guard, then
    drop-before-create) is idempotent instead.
    """
    async with pg_pool.acquire() as conn:
        try:
            # Simulate crash residue: an empty release_artist from an
            # interrupted prior run whose teardown never ran.
            await conn.execute("DROP TABLE IF EXISTS release_artist")
            await conn.execute(
                f"CREATE TABLE release_artist ({RECONCILER_TABLE_DDL['release_artist']})"
            )

            # Empty table → guard must NOT veto (it only vetoes on rows). If it
            # raised Skipped here the test would report skipped, not passed.
            await skip_if_drop_targets_populated(conn, ("release_artist",))

            # The divergence this fix closes: a *bare* CREATE over the residue
            # raises (the pre-fix fixture path), which is why the migrated setup
            # must drop first.
            with pytest.raises(asyncpg.exceptions.DuplicateTableError):
                await conn.execute(
                    f"CREATE TABLE release_artist ({RECONCILER_TABLE_DDL['release_artist']})"
                )

            # The migrated setup drops before creating, so re-running it over
            # the empty residue is a no-op recreate rather than a duplicate.
            await conn.execute("DROP TABLE IF EXISTS release_artist")
            await conn.execute(
                f"CREATE TABLE release_artist ({RECONCILER_TABLE_DDL['release_artist']})"
            )
        finally:
            await conn.execute("DROP TABLE IF EXISTS release_artist")
