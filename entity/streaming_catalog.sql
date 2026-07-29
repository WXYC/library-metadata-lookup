-- GENERATED FILE — regenerate via:
--   uv run python -m scripts.regenerate_streaming_catalog_sql
-- Statements come verbatim from entity/streaming_catalog.py
-- (_DDL_STATEMENTS); this prose lives in the generator. Do not edit this
-- file by hand: unit tests pin both the statement text and these bytes.
--
-- Streaming-catalog schema for LML#842: the row-level PG canonical replacing
-- the whole-file streaming_availability.db lineage.
--
-- This file is the canonical DDL reference for the four `lml_cache` tables the
-- offline streaming enrichment pipeline writes: `streaming_album` (one row per
-- deduplicated library album, Discogs match group included),
-- `streaming_album_service` (one row per album x service probe outcome),
-- `streaming_track_result` (compilation-track resolution), and
-- `streaming_coverage_baseline` (write-side floor metrics for the export's
-- regression assertion). All live in the LML-owned `lml_cache.*` schema (per
-- WXYC/discogs-etl#288, Option 3) and are bootstrapped from LML's own FastAPI
-- lifespan and (from PR B) the offline DAO — no discogs-cache coordination;
-- discogs-cache tooling never touches `lml_cache.*`.
--
-- Distinct from `lml_cache.album_streaming_url_cache` (the runtime lookup
-- post-process cache keyed on normalized request strings): this is the offline
-- catalog keyed on library-album identity.
--
-- This file exists so:
--
--   1. The LML PR's reviewer has the DDL inline for comparison.
--   2. An operator can apply the schema directly to a non-discogs-cache PG
--      (e.g. local dev) without booting the full LML app.
--
-- Manual application MUST be all-or-nothing, exactly like the runtime
-- bootstrap (one transaction, so a mid-apply failure can never leave tables
-- standing without their no-regress guards):
--
--   psql "$DATABASE_URL" --single-transaction -v ON_ERROR_STOP=1 \
--       -c "SET LOCAL lock_timeout = '10s'" \
--       -c "SELECT pg_advisory_xact_lock(842001)" \
--       -f entity/streaming_catalog.sql
--
-- (--single-transaction wraps the -c preamble and the file in ONE
-- transaction, matching the runtime bootstrap's bounded lock waits and
-- serialized concurrent boots.) Never apply it statement-by-statement
-- without those flags.
--
-- The runtime source of truth is `entity/streaming_catalog.py`
-- (`set_up_streaming_catalog_schema`), which issues these statements on every
-- boot: `IF NOT EXISTS` for schema/tables/indexes, `CREATE OR REPLACE` for the
-- guard functions and triggers (triggers have no `IF NOT EXISTS`; OR REPLACE
-- is the idempotent form, PG14+ — this deliberately extends the `lml_cache.*`
-- bootstrap convention beyond CREATE-TABLE-only), and a widen-only DO block
-- for the named service CHECK. The bootstrap runs all of it as one transaction
-- on one connection, after `SET LOCAL lock_timeout = '10s'` and
-- `SELECT pg_advisory_xact_lock(842001)` — bounded lock waits, serialized
-- concurrent boots.
--
-- The guards police DML only — a tripwire against accidental pipeline or
-- operator writes discarding collected (rate-limited) streaming data, not a
-- security perimeter: any role with DDL rights can drop them. A transaction
-- opts in via SELECT set_config('lml_cache.allow_url_removal', 'on', true);
-- is_local=true confines the opt-in to that transaction. Operator runbook:
-- docs/scripts.md (lands in PR F).


CREATE SCHEMA IF NOT EXISTS lml_cache;

-- One row per deduplicated library album. GENERATED ALWAYS on this identity
-- and streaming_track_result's: the one-time seed inserts the legacy SQLite
-- ids verbatim via the deliberate OVERRIDING SYSTEM VALUE spelling (so
-- track_results.album_id references stay valid), then advances each sequence
-- past max(id); a plain INSERT with an explicit id is rejected outright.
-- (COPY sits outside that net — it loads explicit ids without OVERRIDING
-- SYSTEM VALUE and never advances the sequence — so ports must load via
-- INSERT, or pair any COPY with an explicit setval.) library_ids/formats are
-- JSON arrays in SQLite TEXT today (JSONB here) and deliberately carry NO
-- default: a seed that forgets to map them must fail loudly, not insert '[]'.
-- The named jsonb-shape CHECK catches the spellings NOT NULL can't —
-- 'null'::jsonb, scalars, objects all satisfy NOT NULL. The discogs_* columns
-- are the Discogs match group — album identity, not a streaming probe result,
-- hence kept here rather than as a service row.

CREATE TABLE IF NOT EXISTS lml_cache.streaming_album (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    normalized_artist TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    display_artist TEXT NOT NULL,
    display_title TEXT NOT NULL,
    library_ids JSONB NOT NULL,
    formats JSONB NOT NULL,
    genre TEXT,
    label TEXT,
    is_compilation BOOLEAN NOT NULL DEFAULT FALSE,
    is_single BOOLEAN NOT NULL DEFAULT FALSE,
    discogs_release_id BIGINT,
    discogs_artist TEXT,
    discogs_title TEXT,
    discogs_status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT streaming_album_provenance_arrays CHECK (
        jsonb_typeof(library_ids) = 'array' AND jsonb_typeof(formats) = 'array'
    ),
    UNIQUE (normalized_artist, normalized_title)
);

-- One row per (album, service) probe outcome. ON DELETE RESTRICT: deleting an
-- album must never silently take its collected probe rows with it. The named
-- CHECK pins the allowed service set; the legacy SQLite drift columns
-- (tidal_url, youtube_music_url, soundcloud_url) map onto these values at
-- seed time, and a new service is added by extending `_SERVICES` in the
-- runtime module — the DO block below merges it into an existing table's
-- constraint. status is 'pending' | 'found' | 'not_found' | 'error' (plus
-- service-specific values the pipelines already use; deliberately not
-- CHECK-pinned). slug is bandcamp-only; service_item_id is a service-scoped
-- opaque id (spotify_id today). Confidence is DOUBLE PRECISION (not REAL):
-- SQLite REAL is an 8-byte double; float4 would silently narrow seeded
-- values. url rejects '' at the column level (NULL-tolerant CHECK) so NULL
-- stays the one "no url" value; slug is transition-guarded but not
-- CHECK-banned because legacy rows may carry '' slugs.

CREATE TABLE IF NOT EXISTS lml_cache.streaming_album_service (
    album_id BIGINT NOT NULL REFERENCES lml_cache.streaming_album(id) ON DELETE RESTRICT,
    service TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    url TEXT,
    slug TEXT,
    service_item_id TEXT,
    confidence DOUBLE PRECISION,
    matched_artist TEXT,
    matched_title TEXT,
    checked_at TIMESTAMPTZ,
    PRIMARY KEY (album_id, service),
    CONSTRAINT streaming_album_service_valid CHECK (
        service IN (
            'spotify', 'deezer', 'apple_music', 'bandcamp', 'tidal', 'youtube_music', 'soundcloud')
    ),
    CONSTRAINT streaming_album_service_url_nonempty CHECK (url <> '')
);

-- Widen-only maintenance of the named service CHECK, so an already-created
-- table (where CREATE TABLE IF NOT EXISTS is a no-op) picks up service values
-- added after its creation. Deparses the deployed constraint and
-- distinguishes three states: PARSEABLE (matches the exact
-- service = ANY (ARRAY[...]) shape this bootstrap emits; quoted literals are
-- extracted with a quote-aware pattern -- handles an escaped quote inside a
-- literal, e.g. 'o''brien' -- and round-tripped before being trusted, then
-- merged only when the shipped set adds something, never narrowing, skipping
-- the rewrite entirely on a steady-state boot); ABSENT (dropped out-of-band;
-- the re-ADD folds in every service value already live in the table so a
-- recovery boot can't brick on rows outside the shipped set); and
-- FOREIGN-FORM (a hand-repaired regex CHECK, an array-literal constant, or
-- anything the round-trip can't reproduce byte-for-byte -- policy is WARN
-- AND SKIP: RAISE WARNING naming the unparsed deparse and leave the
-- constraint untouched, never drop-and-rebuild or rebuild-from-live-rows,
-- since a foreign form implies deliberate out-of-band operator action). The
-- rewrite emits the IN (...) form on purpose: PG deparses IN as
-- = ANY (ARRAY[...]) and the extraction reads quoted literals from that
-- deparse; an array-literal constant would deparse as ONE literal and
-- corrupt the next boot's extraction.

DO $catalog_check$
DECLARE
    existing_def text;
    inner_array text;
    existing_services text[];
    rebuilt_array text;
    code_services text[] := ARRAY[
        'spotify', 'deezer', 'apple_music', 'bandcamp', 'tidal', 'youtube_music', 'soundcloud'];
    merged_list text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO existing_def
        FROM pg_constraint
        WHERE conrelid = 'lml_cache.streaming_album_service'::regclass
            AND conname = 'streaming_album_service_valid';
    IF existing_def IS NOT NULL THEN
        inner_array := substring(
            existing_def FROM '^CHECK \(\(service = ANY \(ARRAY\[(.*)\]\)\)\)$'
        );
        IF inner_array IS NULL THEN
            RAISE WARNING 'streaming_album_service_valid: deployed CHECK (%) is not in the '
                'expected service = ANY (ARRAY[...]) shape this bootstrap can parse; '
                'leaving it untouched (foreign-form policy: warn-and-skip)', existing_def;
            RETURN;
        END IF;
        SELECT array_agg(replace(m[1], '''''', '''')) INTO existing_services
            FROM regexp_matches(inner_array, '''((?:[^'']|'''')*)''', 'g') AS m;
        SELECT string_agg(quote_literal(s) || '::text', ', ') INTO rebuilt_array
            FROM unnest(existing_services) AS s;
        IF rebuilt_array IS DISTINCT FROM inner_array THEN
            RAISE WARNING 'streaming_album_service_valid: deployed CHECK (%) has literals this '
                'bootstrap could not confidently round-trip; leaving it untouched '
                '(foreign-form policy: warn-and-skip)', existing_def;
            RETURN;
        END IF;
        IF existing_services @> code_services THEN
            RETURN;
        END IF;
    ELSE
        SELECT array_agg(DISTINCT service) INTO existing_services
            FROM lml_cache.streaming_album_service;
    END IF;
    SELECT string_agg(DISTINCT quote_literal(s), ', ' ORDER BY quote_literal(s))
        INTO merged_list
        FROM unnest(coalesce(existing_services, ARRAY[]::text[]) || code_services) AS s;
    EXECUTE 'ALTER TABLE lml_cache.streaming_album_service '
        'DROP CONSTRAINT IF EXISTS streaming_album_service_valid, '
        'ADD CONSTRAINT streaming_album_service_valid CHECK (service IN ('
        || merged_list || '))';
END;
$catalog_check$;

-- Pending-scan support for the pipelines ("next albums to probe on service
-- X"), mirroring the legacy per-service status indexes. Shape is provisional
-- until PR B's real get_pending/coverage queries land; IF NOT EXISTS never
-- redefines an existing index, so a reshape needs a NEW name plus a drop of
-- this one.

CREATE INDEX IF NOT EXISTS idx_streaming_album_service_status
    ON lml_cache.streaming_album_service (service, status);

-- Compilation-track resolution; stays wide (vs service rows) because
-- resolution_status is per-track and only spotify/deezer apply to tracks.
-- source/source_type are NOT NULL: every legacy SQLite row carries both
-- provenance columns and the seed must not silently drop them. The UNIQUE
-- doubles as the FK-side index for the ON DELETE RESTRICT check (leading
-- album_id). The urls CHECK bans only the empty string and is NULL-tolerant.

CREATE TABLE IF NOT EXISTS lml_cache.streaming_track_result (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    album_id BIGINT NOT NULL REFERENCES lml_cache.streaming_album(id) ON DELETE RESTRICT,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    position TEXT,
    source TEXT NOT NULL,
    source_type TEXT NOT NULL,
    resolution_status TEXT NOT NULL DEFAULT 'pending',
    resolved_via TEXT,
    resolved_album_id BIGINT,
    resolved_release_id BIGINT,
    spotify_url TEXT,
    spotify_confidence DOUBLE PRECISION,
    deezer_url TEXT,
    deezer_confidence DOUBLE PRECISION,
    checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (album_id, artist, title),
    CONSTRAINT streaming_track_result_urls_nonempty CHECK (
        spotify_url <> '' AND deezer_url <> ''
    )
);

-- Reshape of the track status index: drop the original single-column
-- (resolution_status) form so the composite below can supersede it under a
-- new name (CREATE INDEX IF NOT EXISTS never redefines an existing index).
-- No-op once the old index is gone.

DROP INDEX IF EXISTS lml_cache.idx_streaming_track_result_status;

-- get_pending_tracks / get_local_miss_tracks scan one resolution_status
-- ORDER BY id LIMIT n; the composite (resolution_status, id) serves both the
-- equality filter and the id ordering from one index (ordered read + early
-- LIMIT stop, no Sort node).

CREATE INDEX IF NOT EXISTS idx_streaming_track_result_status_id
    ON lml_cache.streaming_track_result (resolution_status, id);

-- Write-side floor metrics (one row per metric, e.g. 'apple_music_found').
-- Refreshed only at the end of successful pipeline runs — never by the daily
-- read-only export — so the export's floor assertion can't track a slow bleed
-- downward. Restores #672's batch-regression detection (many small permitted
-- removals adding up) that the per-row triggers can't see.

CREATE TABLE IF NOT EXISTS lml_cache.streaming_coverage_baseline (
    metric TEXT PRIMARY KEY,
    value BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- No-regress guards: any DML transition that would discard collected
-- streaming data (nulling or blanking-to-'' a found url or a collected slug,
-- ANY transition out of found / the resolved local_match|api_match pair — a
-- total gate, not a demotion blocklist two legal hops could launder,
-- unlinking an album's Discogs match, re-keying a row's identity, discarding
-- collected match/resolution metadata, lowering or renaming a coverage
-- baseline, any DELETE, any TRUNCATE) is rejected at the database unless the
-- transaction opts in:
--
--   BEGIN;
--   SELECT set_config('lml_cache.allow_url_removal', 'on', true);
--   -- SELECT the scope first, then the targeted UPDATE/DELETE
--   COMMIT;
--
-- The third set_config argument (is_local) confines the opt-in to the
-- transaction; protection is restored automatically at COMMIT/ROLLBACK.
-- Runbook: docs/scripts.md (lands in PR F).
--
-- The album guard exists because FK RESTRICT only protects albums that HAVE
-- child rows; childless albums and the collected Discogs match linkage
-- (unlinking to NULL blocked; re-matching to a different release allowed)
-- need their own guard. It also blocks re-keying the album identity (id —
-- reachable even under GENERATED ALWAYS via SET id = DEFAULT — and the
-- normalized artist/title pair) and discarding collected Discogs match
-- metadata (to NULL or '', the extractors' two "empty" spellings); corrected
-- replacements stay allowed.

CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_album()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting('lml_cache.allow_url_removal', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_album: DELETE blocked (id=%) — '
            'would discard collected streaming data; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.normalized_artist IS DISTINCT FROM OLD.normalized_artist
        OR NEW.normalized_title IS DISTINCT FROM OLD.normalized_title THEN
        RAISE EXCEPTION 'streaming_album: re-keying album identity blocked (id=%) — '
            'collected child probe rows would follow the wrong album; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    IF (OLD.discogs_artist IS NOT NULL AND OLD.discogs_artist <> ''
            AND (NEW.discogs_artist IS NULL OR NEW.discogs_artist = ''))
        OR (OLD.discogs_title IS NOT NULL AND OLD.discogs_title <> ''
            AND (NEW.discogs_title IS NULL OR NEW.discogs_title = '')) THEN
        RAISE EXCEPTION 'streaming_album: discarding collected Discogs match metadata '
            'blocked (id=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    IF OLD.discogs_status = 'found' AND NEW.discogs_status IS DISTINCT FROM 'found' THEN
        RAISE EXCEPTION 'streaming_album: demoting discogs_status found to % blocked (id=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            NEW.discogs_status, OLD.id;
    END IF;
    IF OLD.discogs_release_id IS NOT NULL AND NEW.discogs_release_id IS NULL THEN
        RAISE EXCEPTION 'streaming_album: unlinking discogs_release_id blocked (id=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE TRIGGER streaming_album_no_regress
BEFORE DELETE OR UPDATE ON lml_cache.streaming_album
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_album();

-- Total status gate (IS DISTINCT FROM 'found'), not a demotion blocklist:
-- a blocklist lets two individually-legal hops launder the demotion
-- (found → pending → not_found) and re-queues the row for a redundant
-- rate-limited probe. Re-keying the (album_id, service) PK is blocked — it
-- relabels a collected probe as a different album/service. Discarding
-- collected match metadata (the text columns to NULL or '', confidence to
-- NULL) is blocked; replacing it with corrected values is allowed.

CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_album_service()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting('lml_cache.allow_url_removal', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_album_service: DELETE blocked (album_id=%, service=%) — '
            'would discard collected streaming data; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.album_id, OLD.service;
    END IF;
    IF NEW.album_id IS DISTINCT FROM OLD.album_id
        OR NEW.service IS DISTINCT FROM OLD.service THEN
        RAISE EXCEPTION 'streaming_album_service: re-keying a probe row blocked '
            '(album_id=%, service=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.album_id, OLD.service;
    END IF;
    IF (OLD.url IS NOT NULL AND OLD.url <> '')
        AND (NEW.url IS NULL OR NEW.url = '') THEN
        RAISE EXCEPTION 'streaming_album_service: discarding a found url blocked '
            '(album_id=%, service=%) — legitimate removal sets NULL, never the empty string; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.album_id, OLD.service;
    END IF;
    IF (OLD.slug IS NOT NULL AND OLD.slug <> '')
        AND (NEW.slug IS NULL OR NEW.slug = '') THEN
        RAISE EXCEPTION 'streaming_album_service: discarding a collected slug blocked '
            '(album_id=%, service=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.album_id, OLD.service;
    END IF;
    IF (OLD.service_item_id IS NOT NULL AND OLD.service_item_id <> ''
            AND (NEW.service_item_id IS NULL OR NEW.service_item_id = ''))
        OR (OLD.matched_artist IS NOT NULL AND OLD.matched_artist <> ''
            AND (NEW.matched_artist IS NULL OR NEW.matched_artist = ''))
        OR (OLD.matched_title IS NOT NULL AND OLD.matched_title <> ''
            AND (NEW.matched_title IS NULL OR NEW.matched_title = ''))
        OR (OLD.confidence IS NOT NULL AND NEW.confidence IS NULL) THEN
        RAISE EXCEPTION 'streaming_album_service: discarding collected match metadata '
            'blocked (album_id=%, service=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.album_id, OLD.service;
    END IF;
    IF OLD.status = 'found' AND NEW.status IS DISTINCT FROM 'found' THEN
        RAISE EXCEPTION 'streaming_album_service: demoting found status to % blocked '
            '(album_id=%, service=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            NEW.status, OLD.album_id, OLD.service;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE TRIGGER streaming_album_service_no_regress
BEFORE DELETE OR UPDATE ON lml_cache.streaming_album_service
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_album_service();

-- Same total gate on the resolved pair; the lateral local_match <-> api_match
-- move stays allowed (both resolved, nothing discarded). The gate is
-- NULL-hardened (NOT IN yields NULL, not true, for a NULL operand).
-- Re-keying the track identity and discarding collected resolution metadata
-- (resolved_via blank-or-null; the resolved ids and confidences to NULL) are
-- blocked; corrections are allowed.

CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_track_result()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting('lml_cache.allow_url_removal', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_track_result: DELETE blocked (id=%) — '
            'would discard collected streaming data; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.album_id IS DISTINCT FROM OLD.album_id
        OR NEW.artist IS DISTINCT FROM OLD.artist
        OR NEW.title IS DISTINCT FROM OLD.title THEN
        RAISE EXCEPTION 'streaming_track_result: re-keying track identity blocked (id=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    IF ((OLD.spotify_url IS NOT NULL AND OLD.spotify_url <> '')
            AND (NEW.spotify_url IS NULL OR NEW.spotify_url = ''))
        OR ((OLD.deezer_url IS NOT NULL AND OLD.deezer_url <> '')
            AND (NEW.deezer_url IS NULL OR NEW.deezer_url = '')) THEN
        RAISE EXCEPTION 'streaming_track_result: discarding a found track url blocked '
            '(id=%) — legitimate removal sets NULL, never the empty string; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    IF (OLD.resolved_via IS NOT NULL AND OLD.resolved_via <> ''
            AND (NEW.resolved_via IS NULL OR NEW.resolved_via = ''))
        OR (OLD.resolved_album_id IS NOT NULL AND NEW.resolved_album_id IS NULL)
        OR (OLD.resolved_release_id IS NOT NULL AND NEW.resolved_release_id IS NULL)
        OR (OLD.spotify_confidence IS NOT NULL AND NEW.spotify_confidence IS NULL)
        OR (OLD.deezer_confidence IS NOT NULL AND NEW.deezer_confidence IS NULL) THEN
        RAISE EXCEPTION 'streaming_track_result: discarding collected resolution metadata '
            'blocked (id=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.id;
    END IF;
    IF OLD.resolution_status IN ('local_match', 'api_match')
        AND (NEW.resolution_status IS NULL
            OR NEW.resolution_status NOT IN ('local_match', 'api_match')) THEN
        RAISE EXCEPTION 'streaming_track_result: demoting resolution_status % to % '
            'blocked (id=%); '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.resolution_status, NEW.resolution_status, OLD.id;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE TRIGGER streaming_track_result_no_regress
BEFORE DELETE OR UPDATE ON lml_cache.streaming_track_result
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_track_result();

-- Outside an opted-in transaction the floor only ratchets upward (equal is
-- fine — a re-run that found the same coverage). Renaming a metric is
-- blocked too: with DELETE blocked, rename blocked, and the PK rejecting a
-- duplicate metric, sidelining a collected floor via INSERT-then-swap is
-- structurally impossible, while a brand-new metric's first INSERT stays
-- legal. The value gate is NULL-hardened (NULL < x is NULL, not true).

CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_coverage_baseline()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting('lml_cache.allow_url_removal', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_coverage_baseline: DELETE blocked (metric=%) — '
            'would discard the collected coverage floor; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.metric;
    END IF;
    IF NEW.metric IS DISTINCT FROM OLD.metric THEN
        RAISE EXCEPTION 'streaming_coverage_baseline: renaming metric % to % blocked — '
            'would sideline its collected floor; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.metric, NEW.metric;
    END IF;
    IF NEW.value IS NULL OR NEW.value < OLD.value THEN
        RAISE EXCEPTION 'streaming_coverage_baseline: lowering baseline % from % to % blocked; '
            'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
            OLD.metric, OLD.value, NEW.value;
    END IF;
    RETURN NEW;
END;
$function$;

CREATE OR REPLACE TRIGGER streaming_coverage_baseline_no_regress
BEFORE DELETE OR UPDATE ON lml_cache.streaming_coverage_baseline
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_coverage_baseline();

-- TRUNCATE never fires row-level triggers, so without these the row guards
-- leave a one-statement wipe path open. One shared TG_TABLE_NAME-generic
-- statement-level guard closes it for all four tables; streaming_album's is
-- defense-in-depth (bare TRUNCATE on it errors at the inbound FKs before
-- triggers fire, and CASCADE reaches the children's guards) but stays in
-- case the FK topology ever changes. Same GUC opts in.

CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF current_setting('lml_cache.allow_url_removal', true) = 'on' THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION '%: TRUNCATE blocked — would discard all collected streaming data; '
        'opt in via set_config(''lml_cache.allow_url_removal'', ''on'', true) in this transaction',
        TG_TABLE_NAME;
END;
$function$;

CREATE OR REPLACE TRIGGER streaming_album_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_album
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate();

CREATE OR REPLACE TRIGGER streaming_album_service_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_album_service
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate();

CREATE OR REPLACE TRIGGER streaming_track_result_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_track_result
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate();

CREATE OR REPLACE TRIGGER streaming_coverage_baseline_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_coverage_baseline
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate();
