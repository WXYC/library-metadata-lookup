"""Post-bootstrap schema verification for ``lml_cache.*`` DDL (LML#1210).

A staging boot on 2026-08-17 logged an ``asyncpg.exceptions.LockNotAvailableError``
from ``bootstrap_lml_cache_table`` while running
``ALTER TABLE lml_cache.artist_wikipedia_bio ADD COLUMN IF NOT EXISTS
last_attempted_at ...`` -- and then came up and served. It was harmless only by
luck: the column already existed, so the failed ALTER changed nothing. Two
things were wrong with that outcome regardless:

1. **Nothing checked.** "Schema ready" was asserted from statement success
   alone. Had the column genuinely been missing, the process would have served
   against a wrong-shape table, and because the bio cache's reads and writes go
   through ``swallowing_fetch``/``swallowing_execute``, the only symptom would
   have been queries quietly returning "miss".
2. **The failure ate its siblings.** ``set_up_artist_wikipedia_bio_schema``
   makes four separate ``bootstrap_lml_cache_table`` calls (deliberately
   separate transactions -- see its P0-1 comment). The exception from call 3
   propagated out of the whole function, so call 4 -- the ``last_refused_at``
   ALTER -- was never attempted that boot. One transient lock timeout silently
   skipped an unrelated, still-needed migration.

The fix verifies what the statements claimed to ensure, against the live
catalog, on both the success and the failure path. The verification is derived
from the statements themselves rather than from a hand-maintained per-table
registry, so it cannot drift away from the DDL it guards.

The load-bearing safety rule is :func:`test_a_call_is_only_understood_if_every_
statement_is`: a failed call is downgraded to a warning **only** when every one
of its statements was a form the parser recognizes. A callable statement, a
``DO`` block, or any unrecognized SQL means the call cannot be proven harmless
-- because the parser cannot know what an unrecognized statement was supposed
to do -- so the original exception propagates exactly as before.
"""

import logging
from contextlib import asynccontextmanager

import pytest

from entity.ddl import (
    LmlCacheSchemaMismatchError,
    bootstrap_lml_cache_table,
    verifying_lml_cache_shape,
)


@pytest.fixture(autouse=True)
def _verification_on():
    """Verification is off by default and entered only by the lifespan loop.

    These tests are about what it does when it IS on, so they enter the same
    scope ``main.py``'s ``_run_lml_cache_bootstraps`` does. The default-off
    behavior itself is covered by the ten ``test_*_schema.py`` suites, which
    drive the real bootstraps through catalog-less fakes and would fail loudly
    if verification ever started running unasked.
    """
    with verifying_lml_cache_shape():
        yield


_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"
_TABLE = "CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio (\n  id BIGINT\n)"
_ADD_COLUMN = (
    "ALTER TABLE lml_cache.artist_wikipedia_bio "
    "ADD COLUMN IF NOT EXISTS last_attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()"
)
_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_bio_misses\n    ON lml_cache.artist_wikipedia_bio (library_id)"
)
# The real _DDL_RENAME_LAST_CHECKED_AT_COLUMN shape: a DO block that CONTAINS an
# ALTER. The parser must not read expectations out of its body -- the whole
# point of the block is that its ALTER runs conditionally.
_DO_BLOCK = (
    "DO $$\nBEGIN\n    ALTER TABLE lml_cache.artist_wikipedia_bio\n"
    "        RENAME COLUMN last_checked_at TO last_attempted_at;\nEND $$"
)


class _BoomError(RuntimeError):
    """Stands in for asyncpg's LockNotAvailableError without the import."""


class _FakeTransaction:
    def __init__(self, source: "_FakePgSource") -> None:
        self._source = source

    async def __aenter__(self) -> "_FakeTransaction":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, source: "_FakePgSource") -> None:
        self._source = source

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._source)

    async def execute(self, sql: str, *args: object) -> str:
        self._source.executed.append(sql)
        if sql in self._source.failing_statements:
            raise _BoomError(f"canceling statement due to lock timeout: {sql}")
        return "CREATE"

    async def fetch(self, sql: str, *args: object) -> list[dict[str, object]]:
        self._source.fetched.append((sql, args))
        if self._source.verification_raises is not None:
            raise self._source.verification_raises
        return self._source.catalog_rows(sql, args)


class _FakePgSource:
    """A live catalog the verification queries can be answered from.

    Deliberately models the *deployed* schema separately from the statements
    issued, because the whole subject of these tests is the two disagreeing.
    """

    def __init__(
        self,
        *,
        schemas: set[str] | None = None,
        tables: set[str] | None = None,
        columns: set[tuple[str, str]] | None = None,
        indexes: set[str] | None = None,
        failing_statements: set[str] | None = None,
        verification_raises: Exception | None = None,
    ) -> None:
        self.schemas = schemas if schemas is not None else {"lml_cache"}
        self.tables = tables if tables is not None else set()
        self.columns = columns if columns is not None else set()
        self.indexes = indexes if indexes is not None else set()
        self.failing_statements = failing_statements or set()
        self.verification_raises = verification_raises
        self.executed: list[str] = []
        self.fetched: list[tuple[str, tuple]] = []

    def catalog_rows(self, sql: str, args: tuple) -> list[dict[str, object]]:
        lowered = sql.lower()
        if "pg_indexes" in lowered:
            return [{"name": i} for i in sorted(self.indexes)]
        if "pg_attribute" in lowered:
            # A column only exists if its table does -- modelling them
            # independently would let a test assert something PG cannot produce.
            return [
                {"table_name": t, "column_name": c}
                for t, c in sorted(self.columns)
                if t in self.tables
            ]
        if "pg_class" in lowered:
            return [{"name": t} for t in sorted(self.tables)]
        if "pg_namespace" in lowered:
            return [{"name": s} for s in sorted(self.schemas)]
        raise AssertionError(f"unexpected verification query: {sql}")

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConnection(self)


def _healthy_source(**overrides) -> _FakePgSource:
    """A catalog that already satisfies every expectation in this module."""
    defaults: dict = {
        "tables": {"artist_wikipedia_bio"},
        "columns": {("artist_wikipedia_bio", "last_attempted_at")},
        "indexes": {"idx_bio_misses"},
    }
    defaults.update(overrides)
    return _FakePgSource(**defaults)


@pytest.mark.asyncio
class TestVerificationOnTheSuccessPath:
    """Statement success is no longer taken as proof the shape is right."""

    async def test_success_with_a_matching_catalog_returns_quietly(self, caplog):
        pg = _healthy_source()

        with caplog.at_level(logging.WARNING):
            await bootstrap_lml_cache_table(pg, _SCHEMA, _TABLE, _ADD_COLUMN)

        assert caplog.records == []

    async def test_success_but_a_missing_column_raises_a_mismatch(self):
        """Out-of-band drift: every statement reported success, but the column
        the ALTER promised is not there. Before #1210 this booted as 'ready'."""
        pg = _healthy_source(columns=set())

        with pytest.raises(LmlCacheSchemaMismatchError) as excinfo:
            await bootstrap_lml_cache_table(pg, _SCHEMA, _TABLE, _ADD_COLUMN)

        assert "last_attempted_at" in str(excinfo.value)

    async def test_success_but_a_missing_table_raises_a_mismatch(self):
        pg = _healthy_source(tables=set(), columns=set())

        with pytest.raises(LmlCacheSchemaMismatchError) as excinfo:
            await bootstrap_lml_cache_table(pg, _SCHEMA, _TABLE)

        assert "artist_wikipedia_bio" in str(excinfo.value)

    async def test_success_but_a_missing_index_raises_a_mismatch(self):
        pg = _healthy_source(indexes=set())

        with pytest.raises(LmlCacheSchemaMismatchError) as excinfo:
            await bootstrap_lml_cache_table(pg, _SCHEMA, _TABLE, _INDEX)

        assert "idx_bio_misses" in str(excinfo.value)

    async def test_statements_with_nothing_to_verify_issue_no_query(self):
        """No expectations parsed -> no verification round trip. Keeps the
        no-op cost of this feature at zero for callable-only bootstraps."""

        async def _custom(conn):
            return None

        pg = _healthy_source()

        await bootstrap_lml_cache_table(pg, _custom)

        assert pg.fetched == []


@pytest.mark.asyncio
class TestVerificationOnTheFailurePath:
    """The observed 2026-08-17 incident, and the case it was one column away from."""

    async def test_failed_ddl_against_an_already_correct_shape_is_a_warning(self, caplog):
        """The observed incident: the ALTER lock-timed out, but the column was
        already there, so nothing was degraded. That must read as a warning, not
        as a traceback that also aborts the caller's remaining bootstraps."""
        pg = _healthy_source(failing_statements={_ADD_COLUMN})

        with caplog.at_level(logging.WARNING):
            await bootstrap_lml_cache_table(pg, _ADD_COLUMN)

        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "already" in caplog.records[0].getMessage()

    async def test_failed_ddl_against_a_wrong_shape_raises_a_mismatch(self):
        """The case the incident was one column away from: the ALTER failed AND
        the column is genuinely absent, so the cache would silently degrade."""
        pg = _healthy_source(columns=set(), failing_statements={_ADD_COLUMN})

        with pytest.raises(LmlCacheSchemaMismatchError) as excinfo:
            await bootstrap_lml_cache_table(pg, _ADD_COLUMN)

        assert "last_attempted_at" in str(excinfo.value)

    async def test_the_mismatch_chains_the_original_failure(self):
        """The lock timeout is the cause and must stay in the traceback -- it is
        what tells an operator this was contention rather than a code defect."""
        pg = _healthy_source(columns=set(), failing_statements={_ADD_COLUMN})

        with pytest.raises(LmlCacheSchemaMismatchError) as excinfo:
            await bootstrap_lml_cache_table(pg, _ADD_COLUMN)

        assert isinstance(excinfo.value.__cause__, _BoomError)

    async def test_sibling_bootstrap_calls_still_run_after_a_harmless_failure(self, caplog):
        """The concrete regression from the incident.

        ``set_up_artist_wikipedia_bio_schema``'s call 3 raised, so call 4 -- an
        unrelated, still-needed ALTER -- never ran. Swallowing a *provably*
        harmless failure is what lets the sequence continue.
        """
        pg = _healthy_source(
            columns={
                ("artist_wikipedia_bio", "last_attempted_at"),
                ("artist_wikipedia_bio", "last_refused_at"),
            },
            failing_statements={_ADD_COLUMN},
        )
        add_refused = (
            "ALTER TABLE lml_cache.artist_wikipedia_bio "
            "ADD COLUMN IF NOT EXISTS last_refused_at TIMESTAMPTZ"
        )

        with caplog.at_level(logging.WARNING):
            await bootstrap_lml_cache_table(pg, _ADD_COLUMN)
            await bootstrap_lml_cache_table(pg, add_refused)

        assert add_refused in pg.executed, "the sibling ALTER must still be attempted"


@pytest.mark.asyncio
class TestOnlyProvablyHarmlessFailuresAreSwallowed:
    """The rule that keeps the swallow narrow enough to be safe."""

    async def test_a_call_is_only_understood_if_every_statement_is(self):
        """A callable statement can do anything -- create an index, widen a
        CHECK, install a trigger. None of that is visible to the parser, so a
        call containing one can never be proven harmless."""

        async def _widen(conn):
            raise _BoomError("widen failed")

        pg = _healthy_source()

        with pytest.raises(_BoomError):
            await bootstrap_lml_cache_table(pg, _SCHEMA, _widen)

    async def test_a_do_block_is_not_understood(self):
        """A DO block's effect is conditional by construction. Its body is not
        parsed for expectations, and its presence blocks the swallow."""
        pg = _healthy_source(failing_statements={_DO_BLOCK})

        with pytest.raises(_BoomError):
            await bootstrap_lml_cache_table(pg, _DO_BLOCK)

    async def test_a_do_blocks_inner_alter_is_not_read_as_an_expectation(self):
        """Guards the anchoring of the parser. ``_DO_BLOCK`` contains the text
        ``ALTER TABLE lml_cache.artist_wikipedia_bio``; a substring match would
        mine a bogus expectation out of a statement whose whole purpose is to
        apply conditionally."""
        pg = _healthy_source(tables=set(), columns=set())

        await bootstrap_lml_cache_table(pg, _DO_BLOCK)

        assert pg.fetched == [], "a DO block must contribute no expectations"

    async def test_unrecognized_sql_blocks_the_swallow(self):
        pg = _healthy_source(failing_statements={"VACUUM lml_cache.artist_wikipedia_bio"})

        with pytest.raises(_BoomError):
            await bootstrap_lml_cache_table(pg, "VACUUM lml_cache.artist_wikipedia_bio")

    async def test_verification_failure_never_masks_the_original_error(self):
        """If the catalog query itself fails (the PG that just refused the DDL
        is not necessarily healthy), the operator must still see the DDL error,
        not a confusing secondary failure from the diagnostic."""
        pg = _healthy_source(
            failing_statements={_ADD_COLUMN},
            verification_raises=RuntimeError("connection reset"),
        )

        with pytest.raises(_BoomError):
            await bootstrap_lml_cache_table(pg, _ADD_COLUMN)

    async def test_verification_failure_on_the_success_path_is_not_fatal(self, caplog):
        """Symmetrically: the DDL applied cleanly, so a broken *diagnostic* must
        not turn a healthy boot into a failed one."""
        pg = _healthy_source(verification_raises=RuntimeError("connection reset"))

        with caplog.at_level(logging.WARNING):
            await bootstrap_lml_cache_table(pg, _SCHEMA, _TABLE, _ADD_COLUMN)

        assert any("verif" in rec.getMessage().lower() for rec in caplog.records)


class TestExpectationParsingAgainstTheRealDdl:
    """Vacuity guard: the parser must actually understand the shipped DDL.

    Every assertion above uses statements written in this file. If the parser
    silently stopped recognizing the forms the repo really ships, all of them
    would keep passing while the feature guarded nothing.
    """

    def test_the_real_bio_ddl_constants_are_understood(self):
        from entity import artist_wikipedia_bio as bio
        from entity.ddl import parse_expected_shape

        shape = parse_expected_shape((bio._DDL_SCHEMA, bio._DDL_TABLE))
        assert shape.understood
        assert "artist_wikipedia_bio" in shape.tables
        assert "lml_cache" in shape.schemas

        alters = parse_expected_shape(
            (bio._DDL_ADD_LAST_ATTEMPTED_AT_COLUMN, bio._DDL_ADD_LAST_REFUSED_AT_COLUMN)
        )
        assert alters.understood
        assert alters.columns == {
            ("artist_wikipedia_bio", "last_attempted_at"),
            ("artist_wikipedia_bio", "last_refused_at"),
        }

    def test_the_real_rename_do_block_is_not_understood(self):
        from entity import artist_wikipedia_bio as bio
        from entity.ddl import parse_expected_shape

        shape = parse_expected_shape((bio._DDL_RENAME_LAST_CHECKED_AT_COLUMN,))

        assert not shape.understood
        assert shape.columns == set()

    def test_the_real_index_ddl_is_understood(self):
        from entity import compilation_track_location as ctl
        from entity.ddl import parse_expected_shape

        shape = parse_expected_shape((ctl._DDL_SCHEMA, ctl._DDL_TABLE, ctl._DDL_INDEX))

        assert shape.understood
        assert "compilation_track_location" in shape.tables
        assert "idx_compilation_track_location_reverse" in shape.indexes
