-- Verified library-release override schema for LML#850.
--
-- Canonical DDL reference for `lml_cache.library_release_override`: the
-- durable pin that lets a hand-verified Discogs-release correction win over
-- the per-request fuzzy match on `POST /api/v1/lookup`. WXYC DJ Alex L.
-- walked the card catalog one release at a time and recorded the correct
-- Discogs link for each; this table stores those pins keyed by the WXYC
-- library release id.
--
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
-- Statements come verbatim from `entity/library_release_override.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_library_release_override_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/library_release_override.py`'s `set_up_library_release_override_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

CREATE SCHEMA IF NOT EXISTS lml_cache;

-- `library_id` -- WXYC library release id == tubafrenzy LIBRARY_RELEASE.ID
-- == LibraryItem.id (the value that renders `libraryRelease?id={id}`). Exact
-- integer key, so the override sidesteps every normalizer-equivalence
-- question the fuzzy read path raises. `discogs_release_id` -- the verified
-- Discogs release id; `> 0` mirrors the LML#401/#518 sentinel guard (release
-- ids start at 1) so a structurally-invalid pin fails at write time rather
-- than binding the `release_id=0` sentinel downstream. `source` --
-- provenance + rollback handle: a seed run is undone by
-- `DELETE ... WHERE source = 'alex-l-2026'`.

CREATE TABLE IF NOT EXISTS lml_cache.library_release_override (
    library_id          INTEGER PRIMARY KEY,
    discogs_release_id  INTEGER NOT NULL CHECK (discogs_release_id > 0),
    source              TEXT NOT NULL DEFAULT 'alex-l-2026',
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
