-- V/A recall index schema for LML#1019.
--
-- Canonical DDL reference for `lml_cache.compilation_track_location` -- a
-- reverse recall index answering "which library shelf locations contain
-- track T credited to artist A?" for the interactive `/lookup` union
-- (LML#1022). Bootstrapped BOTH from LML's own FastAPI lifespan (`main.py`)
-- AND standalone by `scripts/build_compilation_track_location.py`, which
-- runs outside the service from a discogs-etl-cloned checkout.
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
-- Statements come verbatim from `entity/compilation_track_location.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_compilation_track_location_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/compilation_track_location.py`'s `set_up_compilation_track_location_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

CREATE SCHEMA IF NOT EXISTS lml_cache;

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
-- artist-image fallback.

CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_location (
    library_id          INTEGER NOT NULL,
    track_position      TEXT NOT NULL,
    track_artist        TEXT NOT NULL,
    track_title         TEXT NOT NULL,
    credit_role         TEXT NOT NULL CHECK (credit_role IN ('primary', 'featured', 'extra')),
    discogs_release_id  INTEGER NOT NULL CHECK (discogs_release_id > 0),
    artwork_url         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (library_id, track_position, track_artist)
);

-- Reverse index (LML#1019 acceptance criterion): a runtime probe filters by
-- the normalized (track_artist, track_title) pair. Both columns already
-- store normalized values, so a plain btree suffices -- no expression index
-- needed.

CREATE INDEX IF NOT EXISTS idx_compilation_track_location_reverse
    ON lml_cache.compilation_track_location (track_artist, track_title);
