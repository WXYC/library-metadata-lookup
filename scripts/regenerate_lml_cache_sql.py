"""Regenerate nine ``lml_cache.*`` DDL sidecars from their runtime Python constants (LML#1038).

Companion to ``scripts/regenerate_streaming_catalog_sql.py`` (kept separate:
``entity/streaming_catalog.py``'s DDL is a 22-statement, per-statement-commented
reference with its own generator and its own richer parity test). This script
covers the other eight ``entity/*.py`` modules that own a ``lml_cache.*``
table, each simple enough to share one small rendering routine: a file-level
header, then each statement preceded by its own prose comment (never inline
within the statement -- inline per-column ``--`` comments were, before this
script existed, hand-duplicated prose that no test ever actually verified
against the runtime DDL, since the pre-existing parity tests strip comments
before comparing; moving prose ABOVE each statement and generating both
mechanically is a correctness improvement, not just a mechanical port).

Every statement below is a Python string LITERAL copied from the owning
``entity/<module>.py`` (not re-derived at import time), so a change to the
runtime constant that forgets to update this script is caught by each
module's own parity test asserting normalized statement-equality against the
generated ``.sql`` file -- the drift can never survive both a code review and
a green test suite. Two modules' bootstraps additionally issue an ADDITIVE
statement that is illustrative-only in the sidecar (a literal-valued seed
example, a documented-but-not-executed ALTER for a table that predates a
column) -- each is called out in its own section below.

To change a table's shape, edit the owning ``entity/<module>.py`` and this
script together; to change only a header or comment, edit this script. Then
regenerate:

    uv run python -m scripts.regenerate_lml_cache_sql
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from entity import (  # noqa: E402
    api_keys,
    artist_wikipedia_bio,
    artist_wikipedia_bio_attempt,
    compilation_track_identity,
    compilation_track_location,
    discogs_rate_bucket,
    library_release_override,
    release_resolution_cache,
    streaming_url_cache,
    track_streaming_url_cache,
)
from entity.ddl import build_widen_service_check_sql  # noqa: E402


@dataclass(frozen=True)
class SidecarSpec:
    """One module's generated ``.sql`` sidecar.

    ``statements`` are rendered verbatim (each becomes ``statement + ";"``)
    in order; ``comments`` maps a statement's first line to the prose
    rendered immediately before it (every key must match exactly one
    statement, exactly like ``regenerate_streaming_catalog_sql.py``'s
    ``_COMMENTS``). ``footer`` is optional trailing prose with no matching
    runtime statement -- used only for an illustrative, non-executable
    example (see ``discogs_rate_bucket`` and ``release_resolution_cache``
    below).
    """

    header: str
    statements: tuple[str, ...]
    comments: dict[str, str] = field(default_factory=dict)
    footer: str = ""


def build_reference(spec: SidecarSpec) -> str:
    """Return the full ``.sql`` file content ``spec`` implies."""
    first_lines = [statement.splitlines()[0] for statement in spec.statements]
    if len(set(first_lines)) != len(first_lines):
        raise RuntimeError(f"statement first lines are not unique: {first_lines}")
    unmatched = set(spec.comments) - set(first_lines)
    if unmatched:
        raise RuntimeError(f"comment keys match no statement first line: {sorted(unmatched)}")
    parts = [spec.header]
    for statement, first_line in zip(spec.statements, first_lines, strict=True):
        if first_line in spec.comments:
            parts.append(spec.comments[first_line])
        parts.append(statement + ";")
    if spec.footer:
        parts.append(spec.footer)
    return "\n\n".join(parts) + "\n"


def _ownership_boilerplate(module_name: str, setup_fn: str) -> str:
    """The paragraph every sidecar's header repeats: ``lml_cache`` ownership + why the file exists."""
    return f"""\
-- It lives in the LML-owned `lml_cache.*` schema (per WXYC/discogs-etl#288,
-- Option 3) -- not the discogs-cache-owned `entity.*` identity contract,
-- which is created and migrated only via discogs-cache alembic migrations --
-- and is bootstrapped from LML's own FastAPI lifespan; discogs-cache tooling
-- never touches `lml_cache.*`.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- GENERATED FILE -- regenerate via:
--   uv run python -m scripts.regenerate_lml_cache_sql
-- Statements come verbatim from `entity/{module_name}.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_{module_name}_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/{module_name}.py`'s `{setup_fn}`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot."""


_MODULES: dict[str, SidecarSpec] = {
    "api_keys": SidecarSpec(
        header=f"""\
-- Per-consumer LML API key schema (LML per-consumer API keys plan).
--
-- Canonical DDL reference for `lml_cache.api_keys`: one row per consumer
-- (tubafrenzy, backend-service, wxyc-canary, the operator drain script, ...),
-- each holding the SHA-256 hash of a distinct `lml_<random>` token.
-- `caller_name` carries deliberately no per-caller uniqueness constraint: a
-- rotation in progress means two live rows for the same caller (old
-- not-yet-revoked, new not-yet-confirmed), the intended state, not a bug.
-- `key_hash` is the only credential-bearing
-- column ever persisted; the plaintext token is shown exactly once, at mint
-- time, by `scripts/api_keys`.
--
{_ownership_boilerplate("api_keys", "set_up_api_keys_schema")}""",
        statements=(api_keys._DDL_SCHEMA, api_keys._DDL_TABLE),
    ),
    "artist_wikipedia_bio": SidecarSpec(
        header=f"""\
-- Artist Wikipedia bio cache schema for the Wikipedia-preferred-artist-bio
-- program (docs/plans/lml-1192-wikipedia-artist-bio.md; LML#513/#1192).
--
-- Canonical DDL reference for `lml_cache.artist_wikipedia_bio`: one row per
-- Discogs artist id, carrying the lead-paragraph extract fetched from the
-- Phase-A-picked Wikipedia page (or a NULL extract recording a negative --
-- 404 / disambiguation / empty / a rejected `description`).
--
{_ownership_boilerplate("artist_wikipedia_bio", "set_up_artist_wikipedia_bio_schema")}""",
        statements=(artist_wikipedia_bio._DDL_SCHEMA, artist_wikipedia_bio._DDL_TABLE),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio (": """\
-- `discogs_artist_id` -- the PRIMARY KEY; one row per artist, no composite
-- key. `wikipedia_url` -- whichever Phase-A pick the stored `extract` came
-- from; the read is keyed on `discogs_artist_id` ALONE (LML#1192 review
-- round 5 -- see `entity/artist_wikipedia_bio.py`'s module docstring for
-- the mechanism and why a prior URL-match read predicate was removed; this
-- generated comment intentionally does not restate it, to avoid a second
-- copy that can drift). `slug_score` -- the Phase-A extractor's
-- confidence at fetch time (audit only, never compared). `lang` -- the
-- Wikipedia edition the extract came from. `extract` -- NULL records a
-- negative result (see `clients/wikipedia.py`); a positive result is never
-- empty (the client itself rejects an empty extract before returning).
-- `fetched_at` -- read-side TTL cutoff, on two DIFFERENT clocks depending
-- on `extract IS NULL` (`entity/artist_wikipedia_bio.py`'s module
-- docstring has the full freshness rationale). `last_checked_at` -- the
-- offline drain's own progress cursor (LML#1192 review round 4, finding
-- 13), distinct from `fetched_at`'s content-age meaning. `last_refused_at`
-- -- when a refresh for this row was last refused by the P0-5
-- null-overwrite guard (LML#1192 review round 6, C1-1); NULL for a row
-- that has never been refused. A third clock distinct from the two above:
-- neither `fetched_at` (content age) nor `last_checked_at` alone (the
-- drain's cursor, which a refusal ALSO advances) could answer "when did we
-- last try and fail" -- see `mark_artist_wikipedia_bio_refused` and the
-- read predicate's widened positive arm. See the footer below for tables
-- that predate either column.""",
        },
        footer=f"""\
-- LML#1192 review round 4, P0-1 / round 6, C1-1: this table is created
-- ONLY here (#1194) -- the drain PR (#1196) reads/writes `last_checked_at`
-- but its own `CREATE TABLE IF NOT EXISTS` is a no-op once this one has
-- already run. A fresh install already has both columns from the CREATE
-- TABLE above; the runtime bootstrap additionally issues these idempotent
-- ALTERs, each as its OWN bootstrap_lml_cache_table call (not shown as
-- top-level statements -- each is a follow-up upgrade for a table that
-- predates the column, a no-op once the column exists), so an existing
-- prod table gains them in place. Mirrors
-- `entity/streaming_url_cache.py`'s `is_error` upgrade (LML#1121):
--
--   {artist_wikipedia_bio._DDL_ADD_LAST_CHECKED_AT_COLUMN};
--   {artist_wikipedia_bio._DDL_ADD_LAST_REFUSED_AT_COLUMN};""",
    ),
    "artist_wikipedia_bio_attempt": SidecarSpec(
        header="""\
-- Durable write-nothing attempt record for the Wikipedia bio program
-- (LML#1192 review round 4, P0-8).
--
-- Canonical DDL reference for `lml_cache.artist_wikipedia_bio_attempt`: one
-- row per Discogs artist id that a couldn't-ask outcome (fetch_error /
-- unresolvable / unexpected_error / truncated) was recorded for, whether by the offline
-- drain (`scripts/warm_wikipedia_bios.py`) or the background miss-warm task
-- (`lookup/enrichment/wikipedia_warm.py`). Without this record, the drain's
-- `--incremental` mode's fixed `au.artist_id` ordering with no cursor
-- re-selects the same write-nothing artist ids, in the same order, on
-- every future run -- once the catalog otherwise drains, this residue
-- becomes the candidate list, 100% failure-ish, aborting every session
-- immediately and masking real progress elsewhere in the catalog.
--
-- It lives in the LML-owned `lml_cache.*` schema (per WXYC/discogs-etl#288,
-- Option 3) -- not the discogs-cache-owned `entity.*` identity contract,
-- which is created and migrated only via discogs-cache alembic migrations --
-- and discogs-cache tooling never touches `lml_cache.*`.
--
-- Bootstrapped from LML's own FastAPI lifespan (LML#1192 review round 6,
-- C2-3), same as every other `lml_cache.*` table -- NOT just the offline
-- drain; see `entity/artist_wikipedia_bio_attempt.py`'s module docstring
-- for why (this generated comment intentionally does not restate it, to
-- avoid a second copy that can drift).
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- GENERATED FILE -- regenerate via:
--   uv run python -m scripts.regenerate_lml_cache_sql
-- Statements come verbatim from `entity/artist_wikipedia_bio_attempt.py`;
-- do not hand-edit this file -- `tests/unit/test_artist_wikipedia_bio_attempt_schema.py`
-- pins the statement text. The runtime source of truth is
-- `entity/artist_wikipedia_bio_attempt.py`'s
-- `set_up_artist_wikipedia_bio_attempt_schema`, which issues these
-- statements (`IF NOT EXISTS` forms) on every boot.""",
        statements=(
            artist_wikipedia_bio_attempt._DDL_SCHEMA,
            artist_wikipedia_bio_attempt._DDL_TABLE,
        ),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio_attempt (": """\
-- `discogs_artist_id` -- the PRIMARY KEY; one row per artist. BIGINT (not
-- INTEGER, LML#1192 review round 6, pass 3, A6 -- this table is joined
-- against `lml_cache.artist_wikipedia_bio.discogs_artist_id`, which is
-- BIGINT; a mismatched column type on the join key is a latent index/plan
-- hazard even where Postgres's implicit int4->int8 cast keeps the query
-- itself correct today). `attempted_at`
-- -- when this attempt was last (re-)recorded; a repeated write-nothing
-- outcome refreshes it via the UPSERT rather than growing a second row.
-- `outcome` -- the label recorded (a member of the OUTCOME_* vocabulary
-- entity/artist_wikipedia_bio_attempt.py owns: "fetch_error" / "unresolvable" /
-- "unexpected_error" / "truncated"), audit-only, never compared.""",
        },
    ),
    "compilation_track_identity": SidecarSpec(
        header=f"""\
-- Per-track compilation identity schema for LML#1020.
--
-- Canonical DDL reference for `lml_cache.compilation_track_identity` -- the
-- per-source external identity of each compilation track credit, which is
-- what `BulkResolveTrackIdentity.sources[]` needs and what the LML#1019
-- recall index deliberately does NOT carry. Bootstrapped BOTH from LML's own
-- FastAPI lifespan (`main.py`) AND standalone by
-- `scripts/backfill_compilation_track_identity.py`, which runs outside the
-- service.
--
{
            _ownership_boilerplate(
                "compilation_track_identity", "set_up_compilation_track_identity_schema"
            )
        }""",
        statements=(
            compilation_track_identity._DDL_SCHEMA,
            compilation_track_identity._DDL_TABLE,
            compilation_track_identity._DDL_INDEX,
        ),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_identity (": """\
-- `library_id` is library.db's `library.id`, which IS the legacy MySQL
-- LIBRARY_RELEASE_ID -- NOT Backend's `wxyc_schema.library.id`, which is an
-- independent serial carrying `legacy_release_id` as a separate column. A
-- naive id join against a Backend-sourced library_id silently returns zero
-- rows; the contract names the bridge as
-- `CatalogCompilationTrackRow.legacy_release_id`.
--
-- The key is CREDIT-shaped, not position-shaped: library.db's
-- `compilation_track_artist` export has no position column at all, and a
-- 66-performer track legitimately shares one position, so a position key
-- collides by construction (WXYC/Backend-Service#1989 measured the same
-- thing one repo over). `track_artist`/`track_title` are normalized via
-- `wxyc_etl.text.to_match_form` and ARE the key; `track_artist_raw` /
-- `track_title_raw` carry the verbatim CTA spellings the wire echoes back as
-- join-back keys (wxyc-shared#297).
--
-- A row is an ATTEMPT, not a match: `external_id IS NULL` means the source
-- answered with nothing, which is what makes "only retry failures" a WHERE
-- predicate and a non-empty `tracks[]` a resolved signal for
-- WXYC/Backend-Service#1991. A source that was never successfully consulted
-- (breaker shed, 429, MB PG error, MB unconfigured) writes NO ROW AT ALL --
-- recording an outage as negative evidence would poison both.
--
-- `resolved_artist_name` is deliberately absent from the coherence CHECK: a
-- tier-1 `identity_store` hit resolves with a non-NULL id and a NULL
-- canonical name (entity.identity stores no Discogs title), which is the
-- common resolved shape on a warm store.
--
-- `track_position` is nullable because 100% of positions are recovered from
-- the Discogs tracklist join, never from CTA, and that join legitimately
-- misses. Where it hits, the value may be a synthetic track-sequence ordinal
-- rather than a printed vinyl-side position.""",
            "CREATE INDEX IF NOT EXISTS idx_compilation_track_identity_misses": """\
-- The retry cohort, and the only access path the primary key does not
-- already serve: `library_id` is the PK's leading column, so a plain
-- (library_id) index would be dead weight. Partial on the miss predicate so
-- it stays small as resolution coverage grows.""",
        },
    ),
    "compilation_track_location": SidecarSpec(
        header=f"""\
-- V/A recall index schema for LML#1019.
--
-- Canonical DDL reference for `lml_cache.compilation_track_location` -- a
-- reverse recall index answering "which library shelf locations contain
-- track T credited to artist A?" for the interactive `/lookup` union
-- (LML#1022). Bootstrapped BOTH from LML's own FastAPI lifespan (`main.py`)
-- AND standalone by `scripts/build_compilation_track_location.py`, which
-- runs outside the service from a discogs-etl-cloned checkout.
--
{
            _ownership_boilerplate(
                "compilation_track_location", "set_up_compilation_track_location_schema"
            )
        }""",
        statements=(
            compilation_track_location._DDL_SCHEMA,
            compilation_track_location._DDL_TABLE,
            compilation_track_location._DDL_INDEX,
        ),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_location (": """\
-- `library_id` is the WXYC library.id shelf location -- the compilation's
-- physical card-catalog slot (e.g. a "Various Artists - X" or
-- "Soundtracks - L" shelf row), not a per-track identity. `track_position`
-- is the Discogs track position (e.g. "A1", "2"), falling back to the track
-- sequence number when Discogs carries none, so the PK component is never
-- empty. `track_artist`/`track_title` are normalized via
-- `wxyc_etl.text.to_match_form` -- this IS the reverse-lookup key, not a
-- display string; a runtime probe (LML#1022) normalizes its typed artist the
-- same way before an exact match. `credit_role` is a coarse credit tier from
-- `release_track_artist.extra`/`role` -- precision is carried by ranking in
-- LML#1022 (credit-tier -> title-ratio -> id), not by filtering here; every
-- credit is admitted. `discogs_release_id` is the comp's matched Discogs
-- release (art re-resolution key); `> 0` mirrors the LML#401/#518 sentinel
-- guard (release ids start at 1). `artwork_url` is precomputed
-- (`lookup/artwork.py:_resolve_fallback_artwork`) so a hit never needs a
-- live Discogs call to render art; NULL when the release has no cover and no
-- artist-image fallback.""",
            "CREATE INDEX IF NOT EXISTS idx_compilation_track_location_reverse": """\
-- Reverse index (LML#1019 acceptance criterion): a runtime probe filters by
-- the normalized (track_artist, track_title) pair. Both columns already
-- store normalized values, so a plain btree suffices -- no expression index
-- needed.""",
        },
    ),
    "discogs_rate_bucket": SidecarSpec(
        header=f"""\
-- Shared Discogs rate token bucket schema for LML#841.
--
-- Canonical DDL reference for the single-row `lml_cache.discogs_rate_bucket`
-- table. The per-process `AsyncLimiter` bounds each LML process to
-- `DISCOGS_RATE_LIMIT`/min, but prod + staging share ONE Discogs token
-- against ONE upstream 60/min bucket -- this table holds one row per token so
-- every process draws rate permits from a single lazily-refilled token
-- bucket (`PgTokenBucket.try_acquire` in the `.py` spends it atomically via
-- one `UPDATE ... RETURNING` with a `FOR UPDATE` CTE).
--
{_ownership_boilerplate("discogs_rate_bucket", "set_up_discogs_rate_bucket_schema")}""",
        statements=(discogs_rate_bucket._DDL_SCHEMA, discogs_rate_bucket._DDL_TABLE),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.discogs_rate_bucket (": """\
-- `bucket_key` -- one row per shared Discogs token ('discogs' is the default
-- key every process sharing the station's single Discogs application uses).
-- `tokens` -- currently-available permits (fractional: refill accrues
-- continuously). `capacity` -- burst ceiling, seeded from
-- `DISCOGS_RATE_LIMIT`; `tokens` never exceeds it. `refill_per_sec` --
-- steady-state refill in permits/second (`DISCOGS_RATE_LIMIT` / 60).
-- `last_refill` -- wall-clock of the last refill+spend; elapsed time since
-- drives the lazy refill.""",
        },
        footer="""\
-- Idempotent seed (issued by `set_up_discogs_rate_bucket_schema`, not shown
-- as a top-level statement above since it is bound from the live
-- `discogs_rate_limit` setting, not a static literal). `ON CONFLICT DO
-- NOTHING` means the FIRST process to boot wins the shared budget; a later
-- boot with a different `DISCOGS_RATE_LIMIT` (e.g. staging) must NOT
-- oscillate the row. Seeded full so a fresh bucket starts with a burst
-- allowance. The literals below illustrate the DEFAULT budget
-- (`DISCOGS_RATE_LIMIT=50` -> capacity 50, refill 50/60 ~= 0.8333/s) for a
-- copy-paste apply; an operator overriding that env var makes the app's own
-- seed -- not this illustration -- authoritative:
--
--   INSERT INTO lml_cache.discogs_rate_bucket
--       (bucket_key, tokens, capacity, refill_per_sec)
--   VALUES ('discogs', 50, 50, 0.8333333333333334)
--   ON CONFLICT (bucket_key) DO NOTHING;""",
    ),
    "library_release_override": SidecarSpec(
        header=f"""\
-- Verified library-release override schema for LML#850.
--
-- Canonical DDL reference for `lml_cache.library_release_override`: the
-- durable pin that lets a hand-verified Discogs-release correction win over
-- the per-request fuzzy match on `POST /api/v1/lookup`. WXYC DJ Alex L.
-- walked the card catalog one release at a time and recorded the correct
-- Discogs link for each; this table stores those pins keyed by the WXYC
-- library release id.
--
{_ownership_boilerplate("library_release_override", "set_up_library_release_override_schema")}""",
        statements=(
            library_release_override._DDL_SCHEMA,
            library_release_override._DDL_TABLE,
        ),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.library_release_override (": """\
-- `library_id` -- WXYC library release id == tubafrenzy LIBRARY_RELEASE.ID
-- == LibraryItem.id (the value that renders `libraryRelease?id={id}`). Exact
-- integer key, so the override sidesteps every normalizer-equivalence
-- question the fuzzy read path raises. `discogs_release_id` -- the verified
-- Discogs release id; `> 0` mirrors the LML#401/#518 sentinel guard (release
-- ids start at 1) so a structurally-invalid pin fails at write time rather
-- than binding the `release_id=0` sentinel downstream. `source` --
-- provenance + rollback handle: a seed run is undone by
-- `DELETE ... WHERE source = 'alex-l-2026'`.""",
        },
    ),
    "release_resolution_cache": SidecarSpec(
        header=f"""\
-- Positive release-resolution cache schema for LML#632.
--
-- Canonical DDL reference for `lml_cache.release_resolution_cache`: the
-- durable positive cache that lets the 2nd-and-later add of the same
-- non-library release short-circuit the Discogs probe + per-release
-- track-validation entirely. Stores only the resolved Discogs `release_id`
-- (or NULL for a known miss) -- the existing by-id `get_release` cache fills
-- in the rest.
--
{_ownership_boilerplate("release_resolution_cache", "set_up_release_resolution_cache_schema")}""",
        statements=(
            release_resolution_cache._DDL_SCHEMA,
            release_resolution_cache._DDL_TABLE,
        ),
        footer=f"""\
-- LML#824: `crowd_out` marks a short-TTL crowd-out miss (bounded resolve
-- truncated its candidate set) apart from a full-7-day exhausted miss. A
-- fresh install already has the column from the CREATE TABLE above; the
-- runtime bootstrap additionally issues this idempotent ALTER (not shown as
-- a top-level statement -- it is a follow-up upgrade for a table that
-- predates the column, a no-op once the column exists) so an existing prod
-- table gains it in place:
--
--   {release_resolution_cache._DDL_ADD_CROWD_OUT_COLUMN};""",
    ),
    "streaming_url_cache": SidecarSpec(
        header=f"""\
-- Streaming-URL cache schema for LML#573.
--
-- Canonical DDL reference for the polymorphic
-- `lml_cache.album_streaming_url_cache` table, keyed on
-- `(service, artist_normalized, album_normalized)`. Generalizes the
-- Apple-Music-specific `entity.album_apple_music_lookup_cache` (LML#571)
-- into one table keyed on `service` (LML#573).
--
{_ownership_boilerplate("streaming_url_cache", "set_up_streaming_url_cache_schema")}
--
-- The trailing widen-only DO block (LML#1038: generated by
-- `entity.ddl.build_widen_service_check_sql`, shared with
-- `entity/streaming_catalog.py` and `entity/track_streaming_url_cache.py`)
-- lets an already-created table (where `CREATE TABLE IF NOT EXISTS` is a
-- no-op) pick up service values added to `_SERVICES` after its creation --
-- see `entity/ddl.py` for the full merge-never-narrow / round-trip /
-- foreign-form-warn-and-skip rationale.""",
        statements=(
            streaming_url_cache._DDL_SCHEMA,
            streaming_url_cache._DDL_TABLE,
            build_widen_service_check_sql(
                table=streaming_url_cache._WIDEN_CHECK_TABLE,
                constraint=streaming_url_cache._WIDEN_CHECK_CONSTRAINT,
                services=streaming_url_cache._SERVICES,
            ),
        ),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache (": """\
-- `service` -- discriminator; the named CHECK constraint pins the allowed
-- set so a typo'd service key fails loudly at write time rather than
-- minting a silently-unreadable row (PR-3 added 'bandcamp'; a new service
-- extends `_SERVICES`, which both the CREATE-time CHECK and the widen block
-- below read from). `url` -- NULL records a known miss, gated by
-- `last_checked_at`'s TTL (LML#576: staleness is a SQL-side filter); a
-- non-null `url` is a durable hit that never goes stale. `is_error`
-- (LML#1121) marks a null-`url` row as a transport failure ("couldn't ask")
-- rather than a genuine confirmed absence, so it ages on the much shorter
-- `error_ttl` instead of `miss_ttl` -- see the footer below for tables that
-- predate the column.""",
        },
        footer=f"""\
-- LML#1121: `is_error` marks a null-`url` row as a transport failure
-- ("couldn't ask") apart from a genuine confirmed absence, so it ages on a
-- much shorter `error_ttl` instead of the 7-day `miss_ttl`. A fresh install
-- already has the column from the CREATE TABLE above; the runtime bootstrap
-- additionally issues this idempotent ALTER (not shown as a top-level
-- statement -- it is a follow-up upgrade for a table that predates the
-- column, a no-op once the column exists) so an existing prod table gains it
-- in place. Mirrors `entity/release_resolution_cache.py`'s `crowd_out`
-- upgrade (LML#824):
--
--   {streaming_url_cache._DDL_ADD_ERROR_COLUMN};""",
    ),
    "track_streaming_url_cache": SidecarSpec(
        header=f"""\
-- Track-scoped streaming-URL cache schema for LML#893 (lever L1).
--
-- Canonical DDL reference for `lml_cache.track_streaming_url_cache`. A
-- SEPARATE table from the album cache on purpose (see the closed PR #898
-- poisoning bug): it carries a real `song_normalized` axis in the PK so a
-- per-track Apple Music deep-link is keyed to the played track and a
-- DIFFERENT song of the same album is a MISS. The runtime never writes track
-- deep-links into `album_streaming_url_cache`.
--
{_ownership_boilerplate("track_streaming_url_cache", "set_up_track_streaming_url_cache_schema")}
--
-- The trailing widen-only DO block (LML#1038: generated by
-- `entity.ddl.build_widen_service_check_sql`, shared with
-- `entity/streaming_catalog.py` and `entity/streaming_url_cache.py`) lets an
-- already-created table pick up service values added to `_SERVICES` after
-- its creation. This table's widen path used to be a static, unconditional
-- `DROP CONSTRAINT`/`ADD CONSTRAINT` pair reissued on every boot; adopting
-- the shared generation is a deliberate validation upgrade (steady-state
-- no-op, round-trip-verified extraction, foreign-form warn-and-skip), not a
-- pure rename -- see `entity/ddl.py`.""",
        statements=(
            track_streaming_url_cache._DDL_SCHEMA,
            track_streaming_url_cache._DDL_TABLE,
            build_widen_service_check_sql(
                table=track_streaming_url_cache._WIDEN_CHECK_TABLE,
                constraint=track_streaming_url_cache._WIDEN_CHECK_CONSTRAINT,
                services=track_streaming_url_cache._SERVICES,
            ),
        ),
        comments={
            "CREATE TABLE IF NOT EXISTS lml_cache.track_streaming_url_cache (": """\
-- `service` -- discriminator; the named CHECK constraint pins the allowed
-- set so a typo'd service key fails loudly at write time. A new service is
-- added by extending `_SERVICES`, which both the CREATE-time CHECK and the
-- widen block below read from. `song_normalized` -- the axis that makes
-- this table track-granular: a different song of the same album keys to a
-- different row (a MISS), so a cached hit always deep-links to the played
-- track. `url` -- NOT NULL: this cache stores only resolved track
-- deep-links, never a "checked, not found" sentinel, structurally enforcing
-- the #782/BS#1192 guard.""",
        },
    ),
}


def main() -> None:
    for name, spec in _MODULES.items():
        path = _REPO_ROOT / "entity" / f"{name}.sql"
        content = build_reference(spec)
        path.write_text(content, encoding="utf-8")
        print(f"wrote {len(spec.statements)} statements to {path.relative_to(_REPO_ROOT)}")


if __name__ == "__main__":
    main()
