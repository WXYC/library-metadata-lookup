"""Regenerate ``entity/streaming_catalog.sql`` from the runtime DDL (LML#842).

The ``.sql`` twin is GENERATED, never hand-edited: its statements are emitted
verbatim from ``entity/streaming_catalog.py``'s ``_DDL_STATEMENTS`` and its
prose lives here, as full-line ``--`` comments keyed to the statement they
precede. Two unit tests pin the pairing (tests/unit/test_streaming_catalog_schema.py):
a normalized statement-equality test (so the file can't drift from the runtime
tuple even if someone bypasses this script) and a byte-equality test against
``build_reference()`` (so the file can't drift from this script's prose either).

To change a statement, edit ``entity/streaming_catalog.py``; to change the
header or a comment, edit this file. Then regenerate:

    uv run python -m scripts.regenerate_streaming_catalog_sql

Comments are keyed by each statement's FIRST LINE (all 22 are unique — an
import-time check below enforces that), not by tuple index, so inserting or
reordering statements never silently shifts prose onto the wrong statement:
an unmatched or leftover key raises instead.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from entity.streaming_catalog import _DDL_STATEMENTS  # noqa: E402

_SQL_PATH = _REPO_ROOT / "entity" / "streaming_catalog.sql"

_HEADER = """\
-- GENERATED FILE — regenerate via:
--   uv run python -m scripts.regenerate_streaming_catalog_sql
-- Statements come verbatim from entity/streaming_catalog.py
-- (_DDL_STATEMENTS); this prose lives in the generator. Do not edit this
-- file by hand: unit tests pin both the statement text and these bytes.
--
-- Streaming-catalog schema for LML#842: the row-level PG canonical replacing
-- the whole-file streaming_availability.db lineage.
--
-- This file is the canonical DDL reference for the four `lml_cache` tables the
-- offline streaming enrichment pipeline writes: `streaming_album` (one row per
-- deduplicated library album, Discogs match group included),
-- `streaming_album_service` (one row per album x service probe outcome),
-- `streaming_track_result` (compilation-track resolution), and
-- `streaming_coverage_baseline` (write-side floor metrics for the export's
-- regression assertion). All live in the LML-owned `lml_cache.*` schema (per
-- WXYC/discogs-etl#288, Option 3) and are bootstrapped from LML's own FastAPI
-- lifespan and (from PR B) the offline DAO — no discogs-cache coordination;
-- discogs-cache tooling never touches `lml_cache.*`.
--
-- Distinct from `lml_cache.album_streaming_url_cache` (the runtime lookup
-- post-process cache keyed on normalized request strings): this is the offline
-- catalog keyed on library-album identity.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- Manual application MUST be all-or-nothing, exactly like the runtime
-- bootstrap (one transaction, so a mid-apply failure can never leave tables
-- standing without their no-regress guards):
--
--   psql "$DATABASE_URL" --single-transaction -v ON_ERROR_STOP=1 \\
--       -c "SET LOCAL lock_timeout = '10s'" \\
--       -c "SELECT pg_advisory_xact_lock(842001)" \\
--       -f entity/streaming_catalog.sql
--
-- (--single-transaction wraps the -c preamble and the file in ONE
-- transaction, matching the runtime bootstrap's bounded lock waits and
-- serialized concurrent boots.) Never apply it statement-by-statement
-- without those flags.
--
-- The runtime source of truth is `entity/streaming_catalog.py`
-- (`set_up_streaming_catalog_schema`), which issues these statements on every
-- boot: `IF NOT EXISTS` for schema/tables/indexes, `CREATE OR REPLACE` for the
-- guard functions and triggers (triggers have no `IF NOT EXISTS`; OR REPLACE
-- is the idempotent form, PG14+ — this deliberately extends the `lml_cache.*`
-- bootstrap convention beyond CREATE-TABLE-only), and a widen-only DO block
-- for the named service CHECK. The bootstrap runs all of it as one transaction
-- on one connection, after `SET LOCAL lock_timeout = '10s'` and
-- `SELECT pg_advisory_xact_lock(842001)` — bounded lock waits, serialized
-- concurrent boots.
--
-- The guards police DML only — a tripwire against accidental pipeline or
-- operator writes discarding collected (rate-limited) streaming data, not a
-- security perimeter: any role with DDL rights can drop them. A transaction
-- opts in via SELECT set_config('lml_cache.allow_url_removal', 'on', true);
-- is_local=true confines the opt-in to that transaction. Operator runbook:
-- docs/scripts.md (lands in PR F).
"""

# Prose emitted immediately before the statement whose first line matches the
# key. Keys must match exactly one statement and every key must be consumed —
# both enforced in build_reference().
_COMMENTS: dict[str, str] = {
    "CREATE TABLE IF NOT EXISTS lml_cache.streaming_album (": """\
-- One row per deduplicated library album. GENERATED ALWAYS on this identity
-- and streaming_track_result's: the one-time seed inserts the legacy SQLite
-- ids verbatim via the deliberate OVERRIDING SYSTEM VALUE spelling (so
-- track_results.album_id references stay valid), then advances each sequence
-- past max(id); a plain INSERT with an explicit id is rejected outright.
-- (COPY sits outside that net — it loads explicit ids without OVERRIDING
-- SYSTEM VALUE and never advances the sequence — so ports must load via
-- INSERT, or pair any COPY with an explicit setval.) library_ids/formats are
-- JSON arrays in SQLite TEXT today (JSONB here) and deliberately carry NO
-- default: a seed that forgets to map them must fail loudly, not insert '[]'.
-- The named jsonb-shape CHECK catches the spellings NOT NULL can't —
-- 'null'::jsonb, scalars, objects all satisfy NOT NULL. The discogs_* columns
-- are the Discogs match group — album identity, not a streaming probe result,
-- hence kept here rather than as a service row.""",
    "CREATE TABLE IF NOT EXISTS lml_cache.streaming_album_service (": """\
-- One row per (album, service) probe outcome. ON DELETE RESTRICT: deleting an
-- album must never silently take its collected probe rows with it. The named
-- CHECK pins the allowed service set; the legacy SQLite drift columns
-- (tidal_url, youtube_music_url, soundcloud_url) map onto these values at
-- seed time, and a new service is added by extending `_SERVICES` in the
-- runtime module — the DO block below merges it into an existing table's
-- constraint. status is 'pending' | 'found' | 'not_found' | 'error' (plus
-- service-specific values the pipelines already use; deliberately not
-- CHECK-pinned). slug is bandcamp-only; service_item_id is a service-scoped
-- opaque id (spotify_id today). Confidence is DOUBLE PRECISION (not REAL):
-- SQLite REAL is an 8-byte double; float4 would silently narrow seeded
-- values. url rejects '' at the column level (NULL-tolerant CHECK) so NULL
-- stays the one "no url" value; slug is transition-guarded but not
-- CHECK-banned because legacy rows may carry '' slugs.""",
    "DO $catalog_check$": """\
-- Widen-only maintenance of the named service CHECK, so an already-created
-- table (where CREATE TABLE IF NOT EXISTS is a no-op) picks up service values
-- added after its creation. Deparses the deployed constraint and
-- distinguishes three states: PARSEABLE (matches the exact
-- service = ANY (ARRAY[...]) shape this bootstrap emits; quoted literals are
-- extracted with a quote-aware pattern -- handles an escaped quote inside a
-- literal, e.g. 'o''brien' -- and round-tripped before being trusted, then
-- merged only when the shipped set adds something, never narrowing, skipping
-- the rewrite entirely on a steady-state boot); ABSENT (dropped out-of-band;
-- the re-ADD folds in every service value already live in the table so a
-- recovery boot can't brick on rows outside the shipped set); and
-- FOREIGN-FORM (a hand-repaired regex CHECK, an array-literal constant, or
-- anything the round-trip can't reproduce byte-for-byte -- policy is WARN
-- AND SKIP: RAISE WARNING naming the unparsed deparse and leave the
-- constraint untouched, never drop-and-rebuild or rebuild-from-live-rows,
-- since a foreign form implies deliberate out-of-band operator action). The
-- rewrite emits the IN (...) form on purpose: PG deparses IN as
-- = ANY (ARRAY[...]) and the extraction reads quoted literals from that
-- deparse; an array-literal constant would deparse as ONE literal and
-- corrupt the next boot's extraction.""",
    "CREATE INDEX IF NOT EXISTS idx_streaming_album_service_status": """\
-- Pending-scan support for the pipelines ("next albums to probe on service
-- X"), mirroring the legacy per-service status indexes. Shape is provisional
-- until PR B's real get_pending/coverage queries land; IF NOT EXISTS never
-- redefines an existing index, so a reshape needs a NEW name plus a drop of
-- this one.""",
    "CREATE TABLE IF NOT EXISTS lml_cache.streaming_track_result (": """\
-- Compilation-track resolution; stays wide (vs service rows) because
-- resolution_status is per-track and only spotify/deezer apply to tracks.
-- source/source_type are NOT NULL: every legacy SQLite row carries both
-- provenance columns and the seed must not silently drop them. The UNIQUE
-- doubles as the FK-side index for the ON DELETE RESTRICT check (leading
-- album_id). The urls CHECK bans only the empty string and is NULL-tolerant.""",
    "DROP INDEX IF EXISTS lml_cache.idx_streaming_track_result_status": """\
-- Reshape of the track status index: drop the original single-column
-- (resolution_status) form so the composite below can supersede it under a
-- new name (CREATE INDEX IF NOT EXISTS never redefines an existing index).
-- No-op once the old index is gone.""",
    "CREATE INDEX IF NOT EXISTS idx_streaming_track_result_status_id": """\
-- get_pending_tracks / get_local_miss_tracks scan one resolution_status
-- ORDER BY id LIMIT n; the composite (resolution_status, id) serves both the
-- equality filter and the id ordering from one index (ordered read + early
-- LIMIT stop, no Sort node).""",
    "CREATE TABLE IF NOT EXISTS lml_cache.streaming_coverage_baseline (": """\
-- Write-side floor metrics (one row per metric, e.g. 'apple_music_found').
-- Refreshed only at the end of successful pipeline runs — never by the daily
-- read-only export — so the export's floor assertion can't track a slow bleed
-- downward. Restores #672's batch-regression detection (many small permitted
-- removals adding up) that the per-row triggers can't see.""",
    "CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_album()": """\
-- No-regress guards: any DML transition that would discard collected
-- streaming data (nulling or blanking-to-'' a found url or a collected slug,
-- ANY transition out of found / the resolved local_match|api_match pair — a
-- total gate, not a demotion blocklist two legal hops could launder,
-- unlinking an album's Discogs match, re-keying a row's identity, discarding
-- collected match/resolution metadata, lowering or renaming a coverage
-- baseline, any DELETE, any TRUNCATE) is rejected at the database unless the
-- transaction opts in:
--
--   BEGIN;
--   SELECT set_config('lml_cache.allow_url_removal', 'on', true);
--   -- SELECT the scope first, then the targeted UPDATE/DELETE
--   COMMIT;
--
-- The third set_config argument (is_local) confines the opt-in to the
-- transaction; protection is restored automatically at COMMIT/ROLLBACK.
-- Runbook: docs/scripts.md (lands in PR F).
--
-- The album guard exists because FK RESTRICT only protects albums that HAVE
-- child rows; childless albums and the collected Discogs match linkage
-- (unlinking to NULL blocked; re-matching to a different release allowed)
-- need their own guard. It also blocks re-keying the album identity (id —
-- reachable even under GENERATED ALWAYS via SET id = DEFAULT — and the
-- normalized artist/title pair) and discarding collected Discogs match
-- metadata (to NULL or '', the extractors' two "empty" spellings); corrected
-- replacements stay allowed.""",
    "CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_album_service()": """\
-- Total status gate (IS DISTINCT FROM 'found'), not a demotion blocklist:
-- a blocklist lets two individually-legal hops launder the demotion
-- (found → pending → not_found) and re-queues the row for a redundant
-- rate-limited probe. Re-keying the (album_id, service) PK is blocked — it
-- relabels a collected probe as a different album/service. Discarding
-- collected match metadata (the text columns to NULL or '', confidence to
-- NULL) is blocked; replacing it with corrected values is allowed.""",
    "CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_track_result()": """\
-- Same total gate on the resolved pair; the lateral local_match <-> api_match
-- move stays allowed (both resolved, nothing discarded). The gate is
-- NULL-hardened (NOT IN yields NULL, not true, for a NULL operand).
-- Re-keying the track identity and discarding collected resolution metadata
-- (resolved_via blank-or-null; the resolved ids and confidences to NULL) are
-- blocked; corrections are allowed.""",
    "CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_coverage_baseline()": """\
-- Outside an opted-in transaction the floor only ratchets upward (equal is
-- fine — a re-run that found the same coverage). Renaming a metric is
-- blocked too: with DELETE blocked, rename blocked, and the PK rejecting a
-- duplicate metric, sidelining a collected floor via INSERT-then-swap is
-- structurally impossible, while a brand-new metric's first INSERT stays
-- legal. The value gate is NULL-hardened (NULL < x is NULL, not true).""",
    "CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_truncate()": """\
-- TRUNCATE never fires row-level triggers, so without these the row guards
-- leave a one-statement wipe path open. One shared TG_TABLE_NAME-generic
-- statement-level guard closes it for all four tables; streaming_album's is
-- defense-in-depth (bare TRUNCATE on it errors at the inbound FKs before
-- triggers fire, and CASCADE reaches the children's guards) but stays in
-- case the FK topology ever changes. Same GUC opts in.""",
}


def build_reference() -> str:
    """Return the full ``.sql`` file content the runtime DDL implies."""
    first_lines = [statement.splitlines()[0] for statement in _DDL_STATEMENTS]
    if len(set(first_lines)) != len(first_lines):
        raise RuntimeError("statement first lines are no longer unique — rekey _COMMENTS")
    unmatched = set(_COMMENTS) - set(first_lines)
    if unmatched:
        raise RuntimeError(f"_COMMENTS keys match no statement first line: {sorted(unmatched)}")
    parts = [_HEADER]
    for statement, first_line in zip(_DDL_STATEMENTS, first_lines, strict=True):
        if first_line in _COMMENTS:
            parts.append(_COMMENTS[first_line])
        parts.append(statement + ";")
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    _SQL_PATH.write_text(build_reference(), encoding="utf-8")
    print(f"wrote {len(_DDL_STATEMENTS)} statements to {_SQL_PATH.relative_to(_REPO_ROOT)}")
