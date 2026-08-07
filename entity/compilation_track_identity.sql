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
-- Statements come verbatim from `entity/compilation_track_identity.py`; do not hand-edit
-- this file -- a per-module unit test (`tests/unit/test_compilation_track_identity_schema.py`)
-- pins the statement text. The runtime source of truth is
-- `entity/compilation_track_identity.py`'s `set_up_compilation_track_identity_schema`, which issues these statements
-- (`IF NOT EXISTS` forms) on every boot.

CREATE SCHEMA IF NOT EXISTS lml_cache;

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
-- rather than a printed vinyl-side position.

CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_identity (
    library_id           INTEGER NOT NULL,
    track_artist         TEXT NOT NULL,
    track_title          TEXT NOT NULL,
    source               TEXT NOT NULL CHECK (source IN ('discogs', 'musicbrainz')),
    external_id          TEXT,
    confidence           REAL,
    method               TEXT,
    resolved_artist_name TEXT,
    track_artist_raw     TEXT NOT NULL,
    track_title_raw      TEXT,
    track_position       TEXT,
    attempted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT compilation_track_identity_verdict_coherent CHECK (
        (external_id IS NULL) = (confidence IS NULL)
        AND (external_id IS NULL) = (method IS NULL)
    ),
    PRIMARY KEY (library_id, track_artist, track_title, source)
);

-- The retry cohort, and the only access path the primary key does not
-- already serve: `library_id` is the PK's leading column, so a plain
-- (library_id) index would be dead weight. Partial on the miss predicate so
-- it stays small as resolution coverage grows.

CREATE INDEX IF NOT EXISTS idx_compilation_track_identity_misses
    ON lml_cache.compilation_track_identity (library_id)
    WHERE external_id IS NULL;
