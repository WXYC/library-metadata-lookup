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

Bootstrap *orchestration* (the transactional + ``lock_timeout`` posture, and
the advisory-lock story) is deliberately NOT here -- this module stays
DDL-text generation only, matching ``entity/cache_toolkit.py``'s scope split
(that module is the get/set toolkit companion; this one is DDL/bootstrap
machinery). See LML#1038 PR-2 for the shared ``bootstrap_lml_cache_table``
helper.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

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
