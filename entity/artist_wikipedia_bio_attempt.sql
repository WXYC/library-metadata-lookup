-- Durable write-nothing attempt record for the Wikipedia bio program
-- (LML#1192 review round 4, P0-8).
--
-- Canonical DDL reference for `lml_cache.artist_wikipedia_bio_attempt`: one
-- row per Discogs artist id that a write-nothing outcome (fetch_error /
-- unresolvable / unexpected_error / truncated / declined) was recorded for, whether by the offline
-- drain (`scripts/warm_wikipedia_bios.py`) or the background miss-warm task
-- (`lookup/enrichment/wikipedia_warm.py`). Without this record, the drain's
-- `--incremental` mode's fixed `au.artist_id` ordering with no cursor
-- re-selects the same write-nothing artist ids, in the same order, on
-- every future run -- once the catalog otherwise drains, this residue
-- becomes the candidate list, 100% failure-ish, aborting every session
-- immediately and masking real progress elsewhere in the catalog.
--
-- Lifespan-bootstrapped since LML#1192 review round 6 (C2-3) -- NOT just
-- the offline drain's private bookkeeping; see
-- `entity/artist_wikipedia_bio_attempt.py`'s module docstring for why
-- (this generated comment intentionally does not restate it, to avoid a
-- second copy that can drift).
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
-- Statements come verbatim from `entity/artist_wikipedia_bio_attempt.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_artist_wikipedia_bio_attempt_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/artist_wikipedia_bio_attempt.py`'s `set_up_artist_wikipedia_bio_attempt_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

CREATE SCHEMA IF NOT EXISTS lml_cache;

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
-- "unexpected_error" / "truncated" / "declined"), audit-only, never compared.

CREATE TABLE IF NOT EXISTS lml_cache.artist_wikipedia_bio_attempt (
    discogs_artist_id BIGINT PRIMARY KEY,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome TEXT NOT NULL
);
