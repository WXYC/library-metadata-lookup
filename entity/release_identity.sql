-- Release-identity schema for LML#526.
--
-- This file is the canonical DDL reference for `entity.release_identity` and
-- `entity.release_reconciliation_log`. The runtime schema is owned by the
-- discogs-cache repo (mirroring how `entity.identity` is owned today — see
-- WXYC/discogs-etl/scripts/import_csv.py:267 for the protected-table list).
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. The sibling discogs-cache PR can copy the statements byte-for-byte.
--   3. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without needing to run the full discogs-cache pipeline.
--
-- The integration test fixtures in tests/integration/test_release_identity.py
-- create the same tables; if the canonical shape changes, update both places.

CREATE TABLE IF NOT EXISTS entity.release_identity (
    id SERIAL PRIMARY KEY,
    -- Per-source UNIQUE columns. The mint protocol relies on these — see
    -- entity/store.py::mint_or_get_release_identity. Adding a new source
    -- means adding a column AND its UNIQUE here AND a sentinel rule in
    -- identity/release_validation.py.
    discogs_release_id INTEGER UNIQUE,
    discogs_master_id INTEGER UNIQUE,
    musicbrainz_release_id TEXT UNIQUE,
    spotify_album_id TEXT UNIQUE,
    apple_music_album_id TEXT UNIQUE,
    bandcamp_album_url TEXT UNIQUE,
    reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS entity.release_reconciliation_log (
    id SERIAL PRIMARY KEY,
    identity_id INTEGER NOT NULL REFERENCES entity.release_identity(id),
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    confidence REAL,
    method TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
