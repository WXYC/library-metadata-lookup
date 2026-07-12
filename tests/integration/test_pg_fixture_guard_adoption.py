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

import pytest

from tests.integration.conftest import (
    ENTITY_IDENTITY_DDL,
    skip_if_drop_targets_populated,
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


def _source(name: str) -> str:
    return (Path(__file__).parent / name).read_text()


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
