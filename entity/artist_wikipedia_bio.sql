-- Artist Wikipedia bio cache schema for the Wikipedia-preferred-artist-bio
-- program (docs/plans/lml-1192-wikipedia-artist-bio.md; LML#513/#1192).
--
-- Canonical DDL reference for `lml_cache.artist_wikipedia_bio`: one row per
-- Discogs artist id, carrying the lead-paragraph extract fetched from the
-- Phase-A-picked Wikipedia page (or a NULL extract recording a negative --
-- 404 / disambiguation / empty / a rejected `description`).
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
-- Statements come verbatim from `entity/artist_wikipedia_bio.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_artist_wikipedia_bio_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/artist_wikipedia_bio.py`'s `set_up_artist_wikipedia_bio_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

CREATE SCHEMA IF NOT EXISTS lml_cache;

-- `discogs_artist_id` -- the PRIMARY KEY; one row per artist, no composite
-- key. `wikipedia_url` -- the Phase-A pick the extract came from; the read
-- side additionally filters on this matching the CALLER's current pick, so
-- a Phase-A recalibration (or an Discogs `artist_url` change) self-heals a
-- stale row to a miss rather than serving prose against a URL LML no
-- longer believes is right. `slug_score` -- the Phase-A extractor's
-- confidence at fetch time (audit only, never compared). `lang` -- the
-- Wikipedia edition the extract came from. `extract` -- NULL records a
-- negative result (see `clients/wikipedia.py`); a positive result is never
-- empty (the client itself rejects an empty extract before returning).
-- `fetched_at` -- read-side TTL cutoff, on two DIFFERENT clocks depending
-- on `extract IS NULL` (`entity/artist_wikipedia_bio.py`'s module
-- docstring has the full freshness rationale). `last_checked_at` -- the
-- offline drain's own progress cursor (LML#1192 review round 4, finding
-- 13), distinct from `fetched_at`'s content-age meaning -- see the footer
-- below for tables that predate the column.

CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio (
    discogs_artist_id BIGINT PRIMARY KEY,
    wikipedia_url TEXT NOT NULL,
    slug_score SMALLINT NOT NULL,
    lang TEXT NOT NULL,
    extract TEXT,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- LML#1192 review round 4, P0-1: this table is created ONLY here
-- (#1194) -- the drain PR (#1196) reads/writes `last_checked_at` but its
-- own `CREATE TABLE IF NOT EXISTS` is a no-op once this one has already
-- run. A fresh install already has the column from the CREATE TABLE
-- above; the runtime bootstrap additionally issues this idempotent ALTER
-- (not shown as a top-level statement -- it is a follow-up upgrade for a
-- table that predates the column, a no-op once the column exists) so an
-- existing prod table gains it in place. Mirrors
-- `entity/streaming_url_cache.py`'s `is_error` upgrade (LML#1121):
--
--   ALTER TABLE lml_cache.artist_wikipedia_bio ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now();
