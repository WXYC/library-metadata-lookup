-- Verified library-release override schema for LML#850.
--
-- This file is the canonical DDL reference for the
-- `lml_cache.library_release_override` table. Unlike `entity.release_identity`
-- (the discogs-cache-owned identity contract, migrated via alembic), this table
-- lives in the LML-owned `lml_cache.*` schema (per WXYC/discogs-etl#288, Option
-- 3) and is bootstrapped from LML's own FastAPI lifespan — no discogs-cache
-- coordination. discogs-cache tooling never touches `lml_cache.*`.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- The runtime source of truth is `entity/library_release_override.py`
-- (`set_up_library_release_override_schema`), which issues these statements via
-- the `IF NOT EXISTS` forms on every boot. If the canonical shape changes,
-- update both places (and the parity assertions in
-- tests/unit/test_library_release_override_schema.py).

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.library_release_override (
    -- WXYC library release id == tubafrenzy LIBRARY_RELEASE.ID == LibraryItem.id
    -- (the value that renders `libraryRelease?id={id}`). Exact-integer key, so
    -- the override sidesteps every normalizer-equivalence question the fuzzy
    -- read path raises.
    library_id          INTEGER PRIMARY KEY,
    -- The verified Discogs release id. `> 0` mirrors the LML#401/#518 sentinel
    -- guard (release ids start at 1) so a structurally-invalid pin fails at
    -- write time rather than binding the `release_id=0` sentinel downstream.
    discogs_release_id  INTEGER NOT NULL CHECK (discogs_release_id > 0),
    -- Provenance + rollback handle: a seed run is undone by
    -- `DELETE ... WHERE source = 'alex-l-2026'`.
    source              TEXT NOT NULL DEFAULT 'alex-l-2026',
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
