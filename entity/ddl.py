"""Shared ``lml_cache.*`` DDL primitives (LML#1038).

Before this module, every one of the 8 ``entity/`` store modules that owns a
``lml_cache.*`` table restated ``CREATE SCHEMA IF NOT EXISTS lml_cache``
verbatim, and the "widen a named service CHECK without narrowing it" pattern
existed in three independently-drifted generations: a static
``DROP CONSTRAINT``/``ADD CONSTRAINT`` pair (the original
``entity/track_streaming_url_cache.py`` port), an un-validated 33-line
deparse-and-merge DO block (``entity/streaming_url_cache.py``, LML#886), and
the newest generation with round-trip validation and a foreign-form
warn-and-skip policy (``entity/streaming_catalog.py``, LML#890). This module
is the single home for both:

* :data:`LML_CACHE_SCHEMA_DDL` -- the one schema-creation statement every
  bootstrap issues first.
* :func:`widen_service_check` / :func:`build_widen_service_check_sql` -- the
  LML#890 generation, generalized from ``streaming_catalog.py``'s
  album-service-specific block to any ``(table, constraint, services)``
  triple. The two older ports are deleted in favor of this one; adopting it
  on ``streaming_url_cache.py``'s and ``track_streaming_url_cache.py``'s
  tables is a deliberate validation upgrade (round-trip-verified extraction
  and a warn-instead-of-clobber policy for a foreign-form constraint), not a
  behavior-preserving rename -- see LML#1038's PR body for the callout.

LML#1210 adds the fourth piece: post-bootstrap shape verification. Statement
success was the only evidence a bootstrap had worked, and it is not sufficient
evidence in either direction. A staging boot on 2026-08-17 hit a 10s lock
timeout on ``ADD COLUMN IF NOT EXISTS last_attempted_at`` and then logged
"schema ready" for the next label and served -- harmless only because the
column already existed. Had it not, the process would have served against a
wrong-shape table whose reads and writes go through
``entity/cache_toolkit.py``'s ``swallowing_fetch``/``swallowing_execute``, so
the sole symptom would have been queries quietly returning "miss".

:func:`parse_expected_shape` reads what a call's statements claim to ensure --
derived from the statements themselves, never a hand-maintained registry that
could drift away from the DDL it guards -- and :func:`bootstrap_lml_cache_table`
checks it against ``pg_catalog`` on both the success and the failure path,
raising :class:`LmlCacheSchemaMismatchError` on a genuine mismatch and
downgrading a provably-harmless failure to a warning. Verification is entered
by ``main.py``'s lifespan loop via :func:`verifying_lml_cache_shape` and is off
elsewhere; see that function for why it is scoped rather than unconditional.

PR-2 adds the third piece, :func:`bootstrap_lml_cache_table`: the shared
transactional + ``lock_timeout`` posture only 2 of the 8 bootstraps had
before it (``streaming_url_cache.py``, ``streaming_catalog.py``) -- the other
6 issued their ``CREATE SCHEMA`` / ``CREATE TABLE`` / ... statements as
separate autocommitted calls, so a mid-boot failure could leave a schema
created but its table missing. This is a deliberate behavior upgrade (its own
PR per the ticket, not folded into PR-1's pure refactor). Matches
``entity/cache_toolkit.py``'s scope split (that module is the get/set toolkit
companion; this one is DDL/bootstrap machinery) -- this module still only
orchestrates DDL, never a row read/write.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from entity.sources import PgSource

logger = logging.getLogger(__name__)

#: Issued first by every ``lml_cache.*`` bootstrap. ``lml_cache`` is
#: LML-owned (discogs-etl#288, Option 3) -- no alembic, no discogs-cache
#: coordination -- unlike the discogs-cache-owned ``entity.*`` schema, which
#: ``entity/store.py`` deliberately ships no DDL for.
LML_CACHE_SCHEMA_DDL = "CREATE SCHEMA IF NOT EXISTS lml_cache"


@runtime_checkable
class _Executor(Protocol):
    """The minimal shape :func:`widen_service_check` needs from ``pg``.

    Satisfied by both ``entity.sources.PgSource`` (autocommit, pool-backed)
    and a raw ``asyncpg.Connection`` already inside a transaction (the
    heavy-tier bootstraps acquire one explicitly) -- callers pass whichever
    one their bootstrap's posture already holds.
    """

    async def execute(self, query: str, *args: Any) -> str: ...


def build_widen_service_check_sql(*, table: str, constraint: str, services: Sequence[str]) -> str:
    """Render the widen-only named-CHECK maintenance DO block for one table.

    ``table`` is the bare (unqualified) ``lml_cache`` table name, e.g.
    ``"album_streaming_url_cache"``; ``constraint`` is the named CHECK's
    identifier; ``services`` is the shipped allowlist. The rendered block
    always targets a column literally named ``service`` -- every current
    caller's CHECK is on a ``service`` column, matching this function's name.

    Ported from ``entity/streaming_catalog.py``'s
    ``_DDL_ALBUM_SERVICE_WIDEN_CHECK`` (LML#890) and generalized. Semantics
    (see the original for the full rationale, preserved verbatim here):
    deparses the deployed constraint and distinguishes three states --
    PARSEABLE (matches the exact ``service = ANY (ARRAY[...])`` shape this
    bootstrap emits; literals are extracted with a quote-aware pattern and
    round-tripped before being trusted; merges only when the shipped
    ``services`` adds something, leaving the constraint -- and its OID --
    untouched on a steady-state boot), ABSENT (dropped out-of-band; the
    re-ADD folds in every service value already live in the table so the
    recovery boot's re-validation can't fail against collected out-of-set
    rows), and FOREIGN-FORM (deployed but not in a shape this bootstrap can
    confidently parse -- policy is WARN AND SKIP, never drop-and-rebuild or
    rebuild-from-live-rows). The rewrite always emits the ``service IN
    (...)`` form: PG deparses ``IN`` as ``= ANY (ARRAY[...])`` and the
    extraction reads quoted literals out of that deparse, whereas an
    array-literal Const (``'{...}'::text[]``) deparses as ONE literal and
    would corrupt the next boot's extraction.

    The dollar-quote tag is the fixed ``$widen_service_check$`` -- arbitrary
    and cosmetic (each rendered block is one self-contained statement, never
    nested inside another dollar-quoted body), but shared across every call
    site so the generated text is otherwise identical modulo the
    table/constraint/services substitution.
    """
    service_in_list = ", ".join(f"'{s}'" for s in services)
    table_fqn = f"lml_cache.{table}"
    return f"""\
DO $widen_service_check$
DECLARE
    existing_def text;
    inner_array text;
    existing_services text[];
    rebuilt_array text;
    code_services text[] := ARRAY[
        {service_in_list}];
    merged_list text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO existing_def
        FROM pg_constraint
        WHERE conrelid = '{table_fqn}'::regclass
            AND conname = '{constraint}';
    IF existing_def IS NOT NULL THEN
        inner_array := substring(
            existing_def FROM '^CHECK \\(\\(service = ANY \\(ARRAY\\[(.*)\\]\\)\\)\\)$'
        );
        IF inner_array IS NULL THEN
            RAISE WARNING '{constraint}: deployed CHECK (%) is not in the '
                'expected service = ANY (ARRAY[...]) shape this bootstrap can parse; '
                'leaving it untouched (foreign-form policy: warn-and-skip)', existing_def;
            RETURN;
        END IF;
        SELECT array_agg(replace(m[1], '''''', '''')) INTO existing_services
            FROM regexp_matches(inner_array, '''((?:[^'']|'''')*)''', 'g') AS m;
        SELECT string_agg(quote_literal(s) || '::text', ', ') INTO rebuilt_array
            FROM unnest(existing_services) AS s;
        IF rebuilt_array IS DISTINCT FROM inner_array THEN
            RAISE WARNING '{constraint}: deployed CHECK (%) has literals this '
                'bootstrap could not confidently round-trip; leaving it untouched '
                '(foreign-form policy: warn-and-skip)', existing_def;
            RETURN;
        END IF;
        IF existing_services @> code_services THEN
            RETURN;
        END IF;
    ELSE
        SELECT array_agg(DISTINCT service) INTO existing_services
            FROM {table_fqn};
    END IF;
    SELECT string_agg(DISTINCT quote_literal(s), ', ' ORDER BY quote_literal(s))
        INTO merged_list
        FROM unnest(coalesce(existing_services, ARRAY[]::text[]) || code_services) AS s;
    EXECUTE 'ALTER TABLE {table_fqn} '
        'DROP CONSTRAINT IF EXISTS {constraint}, '
        'ADD CONSTRAINT {constraint} CHECK (service IN ('
        || merged_list || '))';
END;
$widen_service_check$\
"""


async def widen_service_check(
    pg: _Executor, *, table: str, constraint: str, services: Sequence[str]
) -> None:
    """Apply the widen-only named-CHECK maintenance for one ``lml_cache`` table.

    Builds the DO block via :func:`build_widen_service_check_sql` and issues
    it as a single ``execute`` on ``pg`` -- one statement, so a heavy-tier
    caller already inside ``conn.transaction()`` gets it atomically alongside
    its other DDL, and a light-tier caller passing a bare
    ``entity.sources.PgSource`` gets it autocommitted like its sibling
    ``execute`` calls.
    """
    sql = build_widen_service_check_sql(table=table, constraint=constraint, services=services)
    await pg.execute(sql)


#: One statement a bootstrap issues: either a bare SQL string (the common
#: case -- executed as-is), or an async callable receiving the live
#: ``asyncpg.Connection`` for a statement that needs more than
#: "execute this literal" (``widen_service_check``'s per-table call, or
#: ``entity/streaming_catalog.py``'s skip-if-the-live-definition-already-
#: matches logic for its ``CREATE OR REPLACE FUNCTION``/``TRIGGER`` forms).
LmlCacheBootstrapStatement = str | Callable[[Any], Awaitable[None]]

_BOOTSTRAP_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '10s'"


_verifying_shape: ContextVar[bool] = ContextVar("lml_cache_verifying_shape", default=False)


@contextmanager
def verifying_lml_cache_shape() -> Iterator[None]:
    """Enable post-bootstrap shape verification for the enclosed block (LML#1210).

    Off by default, and entered in exactly one place: ``main.py``'s
    ``_run_lml_cache_bootstraps``, around the lifespan's bootstrap loop. Two
    reasons it is scoped rather than unconditional:

    * **It is a boot concern.** The point is to decide whether the process is
      about to serve against a wrong-shape table. A script calling a
      ``set_up_*_schema`` helper directly (e.g.
      ``scripts/streaming_availability/catalog_dao.py``) is not that, and
      should not pay a catalog round trip per bootstrap.
    * **It keeps the statement-recording unit tests honest.** The ten
      ``test_*_schema.py`` suites drive the real bootstraps through fakes that
      record SQL and return no rows -- indistinguishable, from inside
      verification, from a live PG that genuinely lacks every object. Rather
      than teach ten near-duplicate doubles to model a catalog they exist to
      not model, verification stays off unless a caller asks for it.

    A ContextVar rather than a module global so a test can enable it without
    leaking into the next one, and so concurrent tasks can't see each other's
    setting.
    """
    token = _verifying_shape.set(True)
    try:
        yield
    finally:
        _verifying_shape.reset(token)


class LmlCacheSchemaMismatchError(RuntimeError):
    """A bootstrap finished but the live catalog does not have the shape it ensures.

    Distinct from the underlying DDL error on purpose (LML#1210): a raw
    ``LockNotAvailableError`` says "a statement did not run", which may be
    harmless, while this says "the deployed table is the wrong shape and the
    cache that reads it is about to degrade silently". ``main.py``'s bootstrap
    loop logs the two differently for exactly that reason.
    """


# --- Post-bootstrap shape verification (LML#1210) ---------------------------
#
# Each pattern is matched with ``re.match`` against the whole stripped
# statement, so it is ANCHORED at the start. That is load-bearing, not
# incidental: ``entity/artist_wikipedia_bio.py``'s conditional-rename statement
# is a ``DO`` block whose body CONTAINS ``ALTER TABLE lml_cache.
# artist_wikipedia_bio``, and a substring search would mine an expectation out
# of a statement whose entire purpose is to apply only under a runtime
# condition. Anchoring makes an unrecognized wrapper stay unrecognized.

_CREATE_SCHEMA_RE = re.compile(r"CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*;?\s*$", re.I)
_CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+lml_cache\.(\w+)\s*\(", re.I)
_ADD_COLUMN_RE = re.compile(
    r"ALTER\s+TABLE\s+lml_cache\.(\w+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)\b", re.I
)
_CREATE_INDEX_RE = re.compile(
    r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+IF\s+NOT\s+EXISTS\s+(\w+)\s+ON\s+lml_cache\.(\w+)\b", re.I
)


@dataclass(frozen=True)
class ExpectedShape:
    """What a bootstrap's statements claim the catalog will contain afterwards.

    Derived from the statements themselves rather than from a hand-maintained
    per-table registry, so it cannot drift away from the DDL it guards -- the
    failure mode a registry would have (LML#1204's lesson about hand-synced
    rosters growing a net-shaped hole).

    ``understood`` is False when ANY statement in the call was a form this
    parser does not recognize -- a callable, a ``DO`` block, a ``CREATE
    FUNCTION``, an index on a non-``lml_cache`` table. It gates the one place
    the verification is allowed to *suppress* an error, because a statement
    whose effect is invisible here can never be shown to have been a no-op.
    """

    schemas: set[str] = field(default_factory=set)
    tables: set[str] = field(default_factory=set)
    columns: set[tuple[str, str]] = field(default_factory=set)
    indexes: set[str] = field(default_factory=set)
    understood: bool = True

    def __bool__(self) -> bool:
        """True when there is anything at all to verify."""
        return bool(self.schemas or self.tables or self.columns or self.indexes)

    def describe(self) -> str:
        """One-line rendering of this expectation set, for log messages.

        Worded identically to :func:`_missing_objects` so an operator reading
        both lines in one incident is not comparing two spellings of one object.
        """
        return ", ".join(
            [
                *(f"schema {s}" for s in sorted(self.schemas)),
                *(f"table lml_cache.{t}" for t in sorted(self.tables)),
                *(f"column lml_cache.{t}.{c}" for t, c in sorted(self.columns)),
                *(f"index lml_cache.{i}" for i in sorted(self.indexes)),
            ]
        )


def parse_expected_shape(statements: Iterable[LmlCacheBootstrapStatement]) -> ExpectedShape:
    """Read the catalog objects ``statements`` ensure, one statement at a time.

    Recognizes the four idempotent forms the ``lml_cache.*`` bootstraps
    actually ship: ``CREATE SCHEMA IF NOT EXISTS``, ``CREATE TABLE IF NOT
    EXISTS lml_cache.x (``, ``ALTER TABLE lml_cache.x ADD COLUMN IF NOT
    EXISTS y``, and ``CREATE [UNIQUE] INDEX IF NOT EXISTS i ON lml_cache.x``.
    Anything else leaves :attr:`ExpectedShape.understood` False.

    Deliberately does NOT parse a ``CREATE TABLE``'s column list. Verifying the
    table exists is enough for that form -- if the CREATE ran, its columns came
    with it -- and a parser for the full body (nested parens, inline
    constraints, defaults) would be fragile in exchange for nothing. The column
    check exists for the ALTER form, which is the one that can leave a live
    table one column short: the silent-degradation case ``entity/
    artist_wikipedia_bio.py``'s P0-1 comment describes.
    """
    schemas: set[str] = set()
    tables: set[str] = set()
    columns: set[tuple[str, str]] = set()
    indexes: set[str] = set()
    understood = True
    # Captures are folded to lowercase because PostgreSQL folds unquoted
    # identifiers and the catalog stores them folded; comparing an as-written
    # capture would report a healthy object as absent. A quoted identifier
    # never reaches here -- \w+ cannot match a quote -- so folding is
    # unconditionally correct.
    for statement in statements:
        if callable(statement):
            understood = False
            continue
        text = statement.strip()
        if match := _CREATE_SCHEMA_RE.match(text):
            schemas.add(match.group(1).lower())
        elif match := _CREATE_TABLE_RE.match(text):
            tables.add(match.group(1).lower())
        elif match := _ADD_COLUMN_RE.match(text):
            # A multi-clause ALTER ("ADD COLUMN a, ADD COLUMN b") and a
            # semicolon-joined pair both match this pattern while hiding every
            # clause after the first, and PG applies the clauses atomically --
            # so a failure anywhere rolls back the hidden ones too. An
            # expectation that UNDER-reports its statement's effect is the one
            # direction that is unsafe: it would let the failure path prove
            # "already satisfied" about a table that is genuinely short.
            if "," in text[match.end() :] or ";" in text:
                understood = False
            else:
                columns.add((match.group(1).lower(), match.group(2).lower()))
        elif match := _CREATE_INDEX_RE.match(text):
            indexes.add(match.group(1).lower())
            tables.add(match.group(2).lower())
        else:
            understood = False
    return ExpectedShape(schemas, tables, columns, indexes, understood)


_SCHEMA_PRESENCE_SQL = "SELECT nspname AS name FROM pg_namespace WHERE nspname = ANY($1::text[])"

_TABLE_PRESENCE_SQL = """
SELECT c.relname AS name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'lml_cache'
  AND c.relkind IN ('r', 'p')
  AND c.relname = ANY($1::text[])
"""

_TABLE_COLUMN_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = 'lml_cache'
  AND c.relkind IN ('r', 'p')
  AND c.relname = ANY($1::text[])
"""

_INDEX_PRESENCE_SQL = (
    "SELECT indexname AS name FROM pg_indexes "
    "WHERE schemaname = 'lml_cache' AND indexname = ANY($1::text[])"
)


async def _missing_objects(pg: PgSource, expected: ExpectedShape) -> list[str]:
    """Return human-readable descriptions of everything ``expected`` lacks.

    Reads ``pg_catalog`` directly rather than introducing a migration
    framework: ``lml_cache.*`` is LML-owned and lifespan-bootstrapped by design
    (discogs-etl#288 Option 3).
    """
    missing: list[str] = []
    async with pg.acquire() as conn:
        if expected.schemas:
            rows = await conn.fetch(_SCHEMA_PRESENCE_SQL, sorted(expected.schemas))
            present = {row["name"] for row in rows}
            missing += [f"schema {s}" for s in sorted(expected.schemas - present)]

        # Table presence is queried directly rather than inferred from the
        # column rows. An ALTER-only call (the bio cache's three separate
        # column bootstraps) expects no tables of its own, so inferring
        # presence would let a missing table pass as "no columns to check".
        present_tables: set[str] = set()
        tables_of_interest = expected.tables | {table for table, _ in expected.columns}
        if tables_of_interest:
            rows = await conn.fetch(_TABLE_PRESENCE_SQL, sorted(tables_of_interest))
            present_tables = {row["name"] for row in rows}
            missing += [f"table lml_cache.{t}" for t in sorted(tables_of_interest - present_tables)]

        if present_columns_needed := {c for c in expected.columns if c[0] in present_tables}:
            rows = await conn.fetch(
                _TABLE_COLUMN_SQL, sorted({table for table, _ in present_columns_needed})
            )
            present_columns = {(row["table_name"], row["column_name"]) for row in rows}
            # Only columns on a table that exists -- a missing table is already
            # reported above, and listing each of its columns would bury it.
            missing += [
                f"column lml_cache.{t}.{c}"
                for t, c in sorted(present_columns_needed - present_columns)
            ]

        if expected.indexes:
            rows = await conn.fetch(_INDEX_PRESENCE_SQL, sorted(expected.indexes))
            present = {row["name"] for row in rows}
            missing += [f"index lml_cache.{i}" for i in sorted(expected.indexes - present)]
    return missing


async def _verify_or_raise(pg: PgSource, expected: ExpectedShape) -> None:
    """Raise :class:`LmlCacheSchemaMismatchError` if the catalog is short anything.

    A failure of the verification query itself is logged and swallowed: the DDL
    already applied cleanly on this path, so a broken diagnostic must not turn a
    healthy boot into a failed one.
    """
    try:
        missing = await _missing_objects(pg, expected)
    except Exception:
        logger.warning(
            "lml_cache bootstrap shape verification could not run; the DDL itself "
            "succeeded, so this boot proceeds unverified",
            exc_info=True,
        )
        return
    if missing:
        raise LmlCacheSchemaMismatchError(
            "lml_cache bootstrap reported success but the deployed schema is missing: "
            + ", ".join(missing)
        )


async def bootstrap_lml_cache_table(
    pg: PgSource, *statements: LmlCacheBootstrapStatement, advisory_key: int | None = None
) -> None:
    """Run one ``lml_cache.*`` table's bootstrap DDL as a single transaction.

    Every statement runs on one pooled connection inside one
    ``conn.transaction()``, behind a ``SET LOCAL lock_timeout = '10s'``
    preamble that bounds how long boot DDL waits behind a conflicting lock
    instead of queueing indefinitely and stalling the lifespan. All-or-
    nothing: a mid-boot failure rolls back the whole transaction rather than
    leaving a schema created but its table (or a widened CHECK) missing --
    the posture ``entity/streaming_url_cache.py`` and
    ``entity/streaming_catalog.py`` already had; every ``lml_cache.*``
    bootstrap gets it now (LML#1038 PR-2).

    ``statements`` run in the given order; a plain ``str`` is issued via
    ``conn.execute(statement)`` unchanged, and a callable is awaited with the
    connection so it can run its own multi-step logic against the SAME
    transaction (``widen_service_check`` and
    ``entity/streaming_catalog.py``'s skip-if-unchanged
    function/trigger statements both compose this way).

    ``advisory_key``, when given, issues
    ``SELECT pg_advisory_xact_lock(<key>)`` immediately after the
    ``lock_timeout`` preamble, serializing concurrent bootstraps of this
    table from a source OTHER than ``main.py``'s lifespan (which already
    wraps every lifespan-invoked bootstrap in one session-scoped advisory
    lock -- see ``main.py``'s ``_LML_CACHE_BOOTSTRAP_ADVISORY_LOCK_KEY``).
    Pass it only for a table with a genuine non-lifespan caller (e.g.
    ``entity/streaming_catalog.py``'s bootstrap, also called directly by
    ``scripts/streaming_availability/catalog_dao.py``); the session lock
    alone suffices for every other table -- see the LML#1038 PR-2 body for
    the per-table decision and its rationale.

    **Shape verification (LML#1210).** Statement success used to be the only
    evidence that a bootstrap worked. It is not enough in either direction, and
    both directions were observed or one column away from being observed on
    2026-08-17:

    * On success, the deployed catalog is checked against what the statements
      claim to ensure (:func:`parse_expected_shape`). A mismatch raises
      :class:`LmlCacheSchemaMismatchError` rather than letting the process serve
      against a wrong-shape table, whose only symptom would be
      ``entity/cache_toolkit.py``'s ``swallowing_fetch``/``swallowing_execute``
      quietly returning "miss".
    * On failure, the same check decides whether the failure MATTERED. The
      staging boot that prompted this ran ``ADD COLUMN IF NOT EXISTS
      last_attempted_at`` into a 10s lock timeout against a table that already
      had the column: nothing was degraded, but the exception still propagated
      out of ``set_up_artist_wikipedia_bio_schema`` and skipped its fourth call,
      an unrelated ALTER that was still needed. When every expectation is
      already satisfied, the failure is logged as a warning and swallowed, so
      the caller's remaining bootstrap calls still run.

    The swallow is deliberately narrow: it applies only when EVERY statement in
    the call was a form the parser recognizes. One callable, ``DO`` block, or
    unrecognized statement and the original exception propagates untouched --
    an invisible statement cannot be shown to have been a no-op, and silently
    skipping a CHECK widening or a guard trigger is not a trade worth making
    for a nicer log line.
    """
    expected = parse_expected_shape(statements) if _verifying_shape.get() else ExpectedShape()
    try:
        async with pg.acquire() as conn:
            async with conn.transaction():
                await conn.execute(_BOOTSTRAP_LOCK_TIMEOUT)
                if advisory_key is not None:
                    await conn.execute(f"SELECT pg_advisory_xact_lock({advisory_key})")
                for statement in statements:
                    if callable(statement):
                        await statement(conn)
                    else:
                        await conn.execute(statement)
    except Exception as exc:
        if not (expected and expected.understood):
            raise
        try:
            missing = await _missing_objects(pg, expected)
        except Exception:
            # Never let the diagnostic mask the real error. The PG that just
            # refused the DDL is not necessarily healthy enough to answer a
            # catalog query, and the DDL failure is the actionable one. Say so
            # anyway: without this line, a bootstrap-failed log where the shape
            # was never checked is indistinguishable from one where it was
            # checked and found fine.
            logger.warning(
                "lml_cache bootstrap shape verification could not run after a failed "
                "DDL; the failure below is reported unverified",
                exc_info=True,
            )
            raise exc from None
        if missing:
            raise LmlCacheSchemaMismatchError(
                "lml_cache bootstrap DDL failed and the deployed schema is missing: "
                + ", ".join(missing)
            ) from exc
        logger.warning(
            "lml_cache bootstrap DDL failed but the deployed schema already satisfies "
            "it (%s); treating as a no-op so the remaining bootstraps still run",
            expected.describe(),
            exc_info=True,
        )
        return

    if expected:
        await _verify_or_raise(pg, expected)
