-- Positive release-resolution cache schema for LML#632.
--
-- Canonical DDL reference for `lml_cache.release_resolution_cache`: the
-- durable positive cache that lets the 2nd-and-later add of the same
-- non-library release short-circuit the Discogs probe + per-release
-- track-validation entirely. Stores only the resolved Discogs `release_id`
-- (or NULL for a known miss) -- the existing by-id `get_release` cache fills
-- in the rest.
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
-- Statements come verbatim from `entity/release_resolution_cache.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_release_resolution_cache_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/release_resolution_cache.py`'s `set_up_release_resolution_cache_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

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
-- truncated its candidate set) apart from a full-7-day exhausted miss. A
-- fresh install already has the column from the CREATE TABLE above; the
-- runtime bootstrap additionally issues this idempotent ALTER (not shown as
-- a top-level statement -- it is a follow-up upgrade for a table that
-- predates the column, a no-op once the column exists) so an existing prod
-- table gains it in place:
--
--   ALTER TABLE lml_cache.release_resolution_cache ADD COLUMN IF NOT EXISTS crowd_out BOOLEAN NOT NULL DEFAULT false;
