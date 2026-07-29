-- Streaming-URL cache schema for LML#573.
--
-- This file is the canonical DDL reference for the polymorphic
-- `lml_cache.album_streaming_url_cache` table. Unlike `entity.release_identity`
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
-- The runtime source of truth is `entity/streaming_url_cache.py`
-- (`set_up_streaming_url_cache_schema`), which issues these statements via the
-- `IF NOT EXISTS` forms on every boot. If the canonical shape changes, update
-- both places (and the parity assertions in
-- tests/unit/test_streaming_url_cache_schema.py).

CREATE SCHEMA IF NOT EXISTS lml_cache;

CREATE TABLE IF NOT EXISTS lml_cache.album_streaming_url_cache (
    -- Service discriminator. The named CHECK constraint pins the allowed set
    -- so a typo'd service key fails loudly at write time rather than minting a
    -- silently-unreadable row. PR-3 added 'bandcamp'; a new service is added by
    -- extending this list AND the widen DO block below (the runtime drives
    -- both from `_SERVICES`).
    service TEXT NOT NULL,
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    -- NULL `url` records a known miss; `last_checked_at` gates its TTL
    -- (LML#576 staleness is a SQL-side filter). A non-null `url` is a durable
    -- hit that never goes stale.
    url TEXT,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (service, artist_normalized, album_normalized),
    CONSTRAINT album_streaming_url_cache_service_valid CHECK (
        service IN ('apple_music_album', 'spotify_album', 'bandcamp')
    )
);

-- Widen-only maintenance of the named service CHECK (LML#886, ported from
-- `entity/streaming_catalog.py`'s `_DDL_ALBUM_SERVICE_WIDEN_CHECK`), so an
-- already-created table (where `CREATE TABLE IF NOT EXISTS` is a no-op)
-- picks up service values added after its creation. Deparses the deployed
-- constraint, extracts its quoted literals, and rewrites only when the
-- shipped set adds something — merging, never narrowing (a rollback deploy
-- must not abort the bootstrap against rows using a newer service), and
-- skipping the rewrite entirely on a steady-state boot (stable constraint
-- OID, no ACCESS EXCLUSIVE lock, no re-validation scan). The rewrite emits
-- the IN (...) form on purpose: PG deparses IN as = ANY (ARRAY[...]) and the
-- extraction reads quoted literals from that deparse; an array-literal
-- constant would deparse as ONE literal and corrupt the next boot's
-- extraction. When the constraint is ABSENT (dropped out-of-band), the
-- re-ADD folds in every service value already live in the table, so a
-- recovery boot can't brick on rows outside the shipped set.

DO $cache_check$
DECLARE
    existing_def text;
    existing_services text[];
    code_services text[] := ARRAY[
        'apple_music_album', 'spotify_album', 'bandcamp'];
    merged_list text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO existing_def
        FROM pg_constraint
        WHERE conrelid = 'lml_cache.album_streaming_url_cache'::regclass
            AND conname = 'album_streaming_url_cache_service_valid';
    IF existing_def IS NOT NULL THEN
        SELECT array_agg(m[1]) INTO existing_services
            FROM regexp_matches(existing_def, '''([^'']+)''', 'g') AS m;
        IF existing_services @> code_services THEN
            RETURN;
        END IF;
    ELSE
        SELECT array_agg(DISTINCT service) INTO existing_services
            FROM lml_cache.album_streaming_url_cache;
    END IF;
    SELECT string_agg(DISTINCT quote_literal(s), ', ' ORDER BY quote_literal(s))
        INTO merged_list
        FROM unnest(coalesce(existing_services, ARRAY[]::text[]) || code_services) AS s;
    EXECUTE 'ALTER TABLE lml_cache.album_streaming_url_cache '
        'DROP CONSTRAINT IF EXISTS album_streaming_url_cache_service_valid, '
        'ADD CONSTRAINT album_streaming_url_cache_service_valid CHECK (service IN ('
        || merged_list || '))';
END;
$cache_check$;
