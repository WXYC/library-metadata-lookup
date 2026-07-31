-- V/A recall index schema for LML#1019.
--
-- This file is the canonical DDL reference for the
-- `lml_cache.compilation_track_location` table -- a reverse recall index
-- answering "which library shelf locations contain track T credited to
-- artist A?" for the interactive `/lookup` union (LML#1022). It lives in the
-- LML-owned `lml_cache.*` schema (per WXYC/discogs-etl#288, Option 3), not
-- the discogs-cache-owned `entity.*` contract, and is bootstrapped BOTH from
-- LML's own FastAPI lifespan (`main.py`) AND standalone by
-- `scripts/build_compilation_track_location.py`, which runs outside the
-- service from a discogs-etl-cloned checkout.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- The runtime source of truth is `entity/compilation_track_location.py`
-- (`set_up_compilation_track_location_schema`), which issues these
-- statements via the `IF NOT EXISTS` forms on every boot. If the canonical
-- shape changes, update both places (and the parity assertions in
-- tests/unit/test_compilation_track_location_schema.py).

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.compilation_track_location (
    -- The WXYC library.id shelf location -- the compilation's physical
    -- card-catalog slot (e.g. a "Various Artists - X" or "Soundtracks - L"
    -- shelf row), not a per-track identity.
    library_id          INTEGER NOT NULL,
    -- Discogs track position (e.g. "A1", "2"). Falls back to the track
    -- sequence number when Discogs carries no position string, so the
    -- primary key component is never empty.
    track_position      TEXT NOT NULL,
    -- Credited track artist, normalized via wxyc_etl.text.to_match_form --
    -- this IS the reverse-lookup key, not a display string. A runtime probe
    -- (LML#1022) normalizes its typed artist the same way before an exact
    -- match against this column.
    track_artist        TEXT NOT NULL,
    -- Credited track title, normalized the same way as track_artist.
    track_title         TEXT NOT NULL,
    -- Coarse credit tier from release_track_artist.extra/role. Precision is
    -- carried by ranking in LML#1022 (credit-tier -> title-ratio -> id), not
    -- by filtering here -- every credit is admitted.
    credit_role         TEXT NOT NULL CHECK (credit_role IN ('primary', 'featured', 'extra')),
    -- The comp's matched Discogs release (art re-resolution key). `> 0`
    -- mirrors the LML#401/#518 sentinel guard (release ids start at 1).
    discogs_release_id  INTEGER NOT NULL CHECK (discogs_release_id > 0),
    -- Precomputed cover URL (lookup/artwork.py:_resolve_fallback_artwork),
    -- so a hit never needs a live Discogs call to render art. NULL when the
    -- release has no cover and no artist-image fallback.
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
