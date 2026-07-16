-- Positive release-resolution cache schema for LML#632.
--
-- This file is the canonical DDL reference for the
-- `lml_cache.release_resolution_cache` table. Like
-- `lml_cache.album_streaming_url_cache` (LML#573) and unlike
-- `entity.release_identity` (the discogs-cache-owned identity contract,
-- migrated via alembic), this table lives in the LML-owned `lml_cache.*`
-- schema (per WXYC/discogs-etl#288, Option 3) and is bootstrapped from LML's
-- own FastAPI lifespan -- no discogs-cache coordination. discogs-cache tooling
-- never touches `lml_cache.*`.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- The runtime source of truth is `entity/release_resolution_cache.py`
-- (`set_up_release_resolution_cache_schema`), which issues these statements via
-- the `IF NOT EXISTS` forms on every boot. If the canonical shape changes,
-- update both places (and the parity assertions in
-- tests/unit/test_release_resolution_cache_schema.py).

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.release_resolution_cache (
    artist_normalized TEXT NOT NULL,
    title_normalized TEXT NOT NULL,
    is_track BOOLEAN NOT NULL,
    release_id INTEGER,
    crowd_out BOOLEAN NOT NULL DEFAULT false,
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artist_normalized, title_normalized, is_track),
    CONSTRAINT release_id_validity CHECK (release_id IS NULL OR release_id > 0)
);

-- LML#824: `crowd_out` marks a short-TTL crowd-out miss (bounded resolve
-- truncated its candidate set) apart from a full-7-day exhausted miss. The
-- runtime bootstrap additionally issues this idempotent ALTER so a table that
-- predates the column gains it in place (a fresh install already has it from the
-- CREATE TABLE above, making the ALTER a no-op):
--
--   ALTER TABLE lml_cache.release_resolution_cache
--       ADD COLUMN IF NOT EXISTS crowd_out BOOLEAN NOT NULL DEFAULT false;
