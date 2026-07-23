"""Row-level PG canonical for the offline streaming-availability catalog (LML#842).

The offline enrichment pipeline (``scripts/streaming_availability/``,
``scripts/track_streaming/``, ``scripts/bandcamp_pipeline.py``) historically
wrote a whole SQLite file (``streaming_availability.db``) that was clobber-
uploaded to object storage — the two-lineages anti-pattern #672 paid down for
the volume/bucket fork. This module owns the DDL for its replacement: four
tables in the LML-owned ``lml_cache.*`` schema (discogs-etl#288 Option 3) on
the shared discogs-cache PostgreSQL.

* ``lml_cache.streaming_album`` — one row per deduplicated library album
  (identity, Discogs match group). Seeded from the SQLite ``albums`` table
  with ids preserved via ``OVERRIDING SYSTEM VALUE`` — the identity is
  ``GENERATED ALWAYS``, so a plain INSERT with an explicit id is rejected
  outright (``COPY`` sits outside that net; see the ``_DDL_ALBUM`` comment).
* ``lml_cache.streaming_album_service`` — one row per (album, service) probe
  outcome. Normalized service rows (vs the wide SQLite columns) dissolve the
  drift-column problem: ``tidal``/``youtube_music``/``soundcloud`` are
  ordinary service values, and adding a service extends ``_SERVICES`` — the
  widen-only DO block merges it into the deployed CHECK on the next boot.
* ``lml_cache.streaming_track_result`` — compilation-track resolution; stays
  wide because ``resolution_status`` is per-track and only spotify/deezer
  apply.
* ``lml_cache.streaming_coverage_baseline`` — one row per coverage metric,
  refreshed only from the write path (pipeline runs), read by the export's
  floor assertion. Restores #672's batch-regression detection (many small
  permitted removals adding up) that the per-row triggers can't see.

Distinct from the runtime ``lml_cache.album_streaming_url_cache`` (lookup
post-process cache keyed on normalized request strings): this is the offline
catalog keyed on library-album identity. Do not conflate them.

**No-regress guards.** ``BEFORE UPDATE OR DELETE`` row triggers plus ``BEFORE
TRUNCATE`` statement triggers on all four tables structurally replace the
#672 whole-file coverage guard: any DML transition that would discard
collected streaming data is rejected at the database, not detected after the
fact. Blocked transitions: nulling *or blanking to ''* a found url or a
collected slug (the pipelines' URL extractors default to ``''``); ANY
transition out of a collected status — ``found`` for albums and
album-services, the ``local_match``/``api_match`` pair for tracks — rather
than a demotion blocklist, which two individually-legal hops would launder
(``found`` → ``pending`` → ``not_found``); unlinking an album's collected
Discogs match; lowering a coverage baseline; any ``DELETE``; any
``TRUNCATE``. Streaming URLs are rate-limited API results and expensive to
replace (org data-safety rules). The guards police DML only — a tripwire
against accidental pipeline/operator writes, not a security perimeter: any
role with DDL rights can drop them. Legitimate revocation (bad-URL stripping,
dedup repair) opts in per transaction; the single GUC deliberately arms *all*
guarded operations at once, so an opted-in transaction should touch exactly
the rows it SELECTed first::

    BEGIN;
    SELECT set_config('lml_cache.allow_url_removal', 'on', true);
    -- SELECT the scope first, then the targeted UPDATE/DELETE
    COMMIT;

The operator runbook lands in ``docs/scripts.md`` in PR F. No DAO revocation
method exists on purpose — the only URL-stripping scripts are in the archived
do-not-port set.

**Bootstrap convention note.** Triggers and PL/pgSQL functions have no ``IF
NOT EXISTS``, so this module extends the ``lml_cache.*`` boot convention with
``CREATE OR REPLACE FUNCTION`` / ``CREATE OR REPLACE TRIGGER`` (PG14+; the
discogs-cache runs PG17) — idempotent by replacement, byte-stable across
boots, applied as one transaction (see ``set_up_streaming_catalog_schema``).
``set_up_streaming_catalog_schema`` is called from the ``main.py`` lifespan
(fifth ``set_up_*`` call) and, in later PRs, from the offline DAO's
``connect()`` so scripts don't depend on a deployed app having booted first.
Staging and prod share the discogs-cache PG, so the ``CREATE OR REPLACE``
guard bodies are last-boot-wins across environments: a guard edit must stay
backward-compatible with the other environment's pipelines until both are
promoted.
"""

from __future__ import annotations

import re

from entity.sources import PgSource

# The services a catalog probe can target. The named CHECK pins this set at
# the DB level; the seed maps the SQLite wide columns (spotify_*, deezer_*,
# apple_*, bandcamp_*) and the real prod file's drift columns (tidal_url,
# youtube_music_url, soundcloud_url) onto these values. Adding a service
# extends this tuple and the widen DO block below picks it up.
_SERVICES = (
    "spotify",
    "deezer",
    "apple_music",
    "bandcamp",
    "tidal",
    "youtube_music",
    "soundcloud",
)

# Import-time tripwire: service values are spliced into SQL literals and read
# back out of pg_get_constraintdef's deparse by the widen block's quoted-
# literal extraction. A quote (or any non-slug character) in a value would
# fragment that round-trip, so reject it before it can reach any DDL. An
# explicit raise, not ``assert`` — the repo convention keeps import-time
# tripwires alive under ``python -O`` (see lookup/orchestrator.py).
if not all(re.fullmatch(r"[a-z0-9_]+", s) for s in _SERVICES):
    raise RuntimeError(f"non-slug service value in _SERVICES: {_SERVICES}")

# Shared IN-list literal so the CREATE-time CHECK and the widen DO block's
# code-side array are generated from the same source — they can never drift.
_SERVICE_IN_LIST = ", ".join(f"'{s}'" for s in _SERVICES)

# Transaction-local GUC consulted by every guard function. Set via
# ``set_config('lml_cache.allow_url_removal', 'on', true)`` — the ``true``
# (is_local) confines the opt-in to the enclosing transaction, so protection
# is restored automatically at COMMIT/ROLLBACK.
ALLOW_URL_REMOVAL_GUC = "lml_cache.allow_url_removal"

# Every guard RAISE renders this hint as its own final message line — one
# hoisted constant so the runbook pointer can't drift per-site. The doubled
# quotes are SQL-literal escaping: the text is spliced into the guard
# functions' single-quoted RAISE format strings.
_OPT_IN_HINT = (
    f"opt in via set_config(''{ALLOW_URL_REMOVAL_GUC}'', ''on'', true) in this transaction"
)

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

# ``GENERATED ALWAYS`` on this identity and ``streaming_track_result``'s: the
# one-time seed (PR C) inserts the SQLite ``albums.id`` / ``track_results.id``
# values verbatim via ``OVERRIDING SYSTEM VALUE`` so cross-table references
# stay valid, then advances each sequence past max(id). Any other explicit id
# arriving via plain INSERT errors loudly — the ported direct-sqlite scripts
# (PRs D/E) historically managed ids by hand, exactly the class of writer
# that must not bypass the sequence silently. ``COPY`` sits outside that net
# (PG loads explicit ids into a GENERATED ALWAYS column without OVERRIDING
# SYSTEM VALUE and never advances the sequence), so ports must load via
# INSERT — or pair any COPY with an explicit ``setval``.
# ``library_ids``/``formats`` deliberately carry NO column default: every
# legacy SQLite row has both, and a default would let a seed that forgets to
# map them pass silently. NOT NULL with no default makes an *omitted* column
# loud, and the jsonb-shape CHECK makes the sneaky spellings loud too —
# ``'null'::jsonb`` and scalars satisfy NOT NULL; only SQL NULL trips it.
_DDL_ALBUM = """\
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
)\
"""

# Named CHECK constraint (``streaming_album_service_valid``) avoids reliance
# on PG auto-naming so the widen DO block below can manage it by name. ``ON
# DELETE RESTRICT`` (not CASCADE): deleting an album must never silently take
# its collected probe rows with it. Confidence columns are DOUBLE PRECISION,
# not REAL: SQLite's REAL storage class is an 8-byte double, and PG REAL
# (float4) would silently narrow the seeded values (matches the
# ``entity/discogs_rate_bucket.py`` float convention). ``url`` additionally
# rejects ``''`` at the column level (NULL-tolerant CHECK): the pipelines'
# URL extractors default to ``''`` on a missing key, and NULL must stay the
# one "no url" value. The CHECK covers INSERT and also backstops UPDATE — it
# rejects ``''`` even inside an opted-in transaction, and it blocks NULL→``''``
# on a row with no collected url, a transition the row guard's OLD-side
# predicate waves through. ``slug`` is transition-guarded but not CHECK-banned:
# legacy SQLite rows may legitimately carry ``''`` slugs.
_DDL_ALBUM_SERVICE = f"""\
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
            {_SERVICE_IN_LIST})
    ),
    CONSTRAINT streaming_album_service_url_nonempty CHECK (url <> '')
)\
"""

# Widen-only maintenance of the named service CHECK. A static DROP+ADD (the
# ``entity/streaming_url_cache.py`` pattern) would (a) take an ACCESS
# EXCLUSIVE lock plus a full re-validation scan on EVERY boot, and (b) narrow
# the constraint under a rollback deploy, aborting the whole bootstrap
# transaction against collected rows that use a service the older code
# doesn't ship. This DO block instead deparses the deployed constraint,
# extracts its quoted literals, and rewrites only when the shipped
# ``_SERVICES`` adds something — merging, never narrowing, and leaving the
# constraint (and its OID) untouched on a steady-state boot. The rewrite must
# emit the ``service IN (...)`` form: PG deparses IN as ``= ANY (ARRAY[...])``
# and the extraction reads the quoted literals out of that deparse, whereas an
# array-literal Const (``'{...}'::text[]``) deparses as ONE literal and would
# corrupt the next boot's extraction. When the constraint is ABSENT (dropped
# out-of-band), the re-ADD folds in every service value already live in the
# table, so the recovery boot's re-validation can't fail against collected
# out-of-set rows — which would otherwise abort EVERY subsequent bootstrap
# until manual repair.
_DDL_ALBUM_SERVICE_WIDEN_CHECK = f"""\
DO $catalog_check$
DECLARE
    existing_def text;
    existing_services text[];
    code_services text[] := ARRAY[
        {_SERVICE_IN_LIST}];
    merged_list text;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO existing_def
        FROM pg_constraint
        WHERE conrelid = 'lml_cache.streaming_album_service'::regclass
            AND conname = 'streaming_album_service_valid';
    IF existing_def IS NOT NULL THEN
        SELECT array_agg(m[1]) INTO existing_services
            FROM regexp_matches(existing_def, '''([^'']+)''', 'g') AS m;
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
$catalog_check$\
"""

# Pending-scan support for the pipelines ("next albums to probe on service X"),
# mirroring the SQLite per-service status indexes. Invariant this encodes for
# the PR B DAO: work is discovered by scanning ``status = 'pending'`` rows, so
# the album-insert path must fan out a pending row per service (or
# ``get_pending`` must anti-join albums x services) — an album with no service
# row is invisible to this scan. Shape is provisional until PR B's real
# ``get_pending``/coverage queries land; ``IF NOT EXISTS`` never redefines an
# existing index, so a reshape needs a NEW name plus a drop of this one.
_DDL_ALBUM_SERVICE_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_streaming_album_service_status\n"
    "    ON lml_cache.streaming_album_service (service, status)"
)

# ``UNIQUE (album_id, artist, title)`` doubles as the FK-side index for the
# ON DELETE RESTRICT check (leading album_id), so no separate album index.
# ``source``/``source_type`` are NOT NULL: every legacy SQLite row carries
# both, and the seed must not silently drop provenance. The urls CHECK is
# NULL-tolerant (a NULL operand makes the AND null, which CHECK passes): it
# bans only the empty string, same rationale as the album-service ``url``.
_DDL_TRACK_RESULT = """\
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
)\
"""

# ``get_pending_tracks`` / ``get_local_miss_tracks`` scan a single
# ``resolution_status`` ORDER BY id LIMIT n; the composite
# ``(resolution_status, id)`` serves both the equality filter and the id
# ordering from one index (ordered index read + early LIMIT stop, no Sort
# node). Reshaped from the original single-column ``(resolution_status)`` now
# that PR B's real track scans have landed: ``CREATE INDEX IF NOT EXISTS``
# never redefines an existing index, so the new shape takes a NEW name and the
# old one is dropped first. Both statements are idempotent — the drop is a
# no-op once the old index is gone, the create once the new one exists.
_DDL_TRACK_RESULT_STATUS_INDEX_DROP = (
    "DROP INDEX IF EXISTS lml_cache.idx_streaming_track_result_status"
)
_DDL_TRACK_RESULT_STATUS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_streaming_track_result_status_id\n"
    "    ON lml_cache.streaming_track_result (resolution_status, id)"
)

# Write-side floor metrics (one row per metric, e.g. 'apple_music_found').
# Refreshed only at the end of successful pipeline runs — never by the daily
# read-only export — so the export's floor assertion can't track a slow bleed
# downward.
_DDL_COVERAGE_BASELINE = """\
CREATE TABLE IF NOT EXISTS lml_cache.streaming_coverage_baseline (
    metric TEXT PRIMARY KEY,
    value BIGINT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)\
"""

# The FK RESTRICT only protects albums that HAVE child rows; a childless
# album (just seeded, or children removed by an earlier opted-in cleanup) and
# the collected Discogs match linkage need their own guard. A corrected
# ``discogs_release_id`` (re-match to a different release) is allowed — only
# unlinking (→ NULL) loses collected data. Same shape for the match text:
# discarding collected ``discogs_artist``/``discogs_title`` — to NULL *or*
# ``''``, the extractors' two "empty" spellings — is blocked, replacing them
# with corrected text is allowed. Re-keying the row identity is blocked
# too — the id leg stays reachable even under GENERATED ALWAYS via ``SET id =
# DEFAULT`` (draws a fresh sequence value; a direct ``SET id = <n>`` dies
# earlier at parse analysis), and moving the normalized pair silently points
# every child probe row at a different album.
_DDL_GUARD_ALBUM_FN = f"""\
CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_album()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('{ALLOW_URL_REMOVAL_GUC}', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_album: DELETE blocked (id=%) — '
            'would discard collected streaming data; '
            '{_OPT_IN_HINT}',
            OLD.id;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.normalized_artist IS DISTINCT FROM OLD.normalized_artist
        OR NEW.normalized_title IS DISTINCT FROM OLD.normalized_title THEN
        RAISE EXCEPTION 'streaming_album: re-keying album identity blocked (id=%) — '
            'collected child probe rows would follow the wrong album; '
            '{_OPT_IN_HINT}',
            OLD.id;
    END IF;
    IF (OLD.discogs_artist IS NOT NULL AND OLD.discogs_artist <> ''
            AND (NEW.discogs_artist IS NULL OR NEW.discogs_artist = ''))
        OR (OLD.discogs_title IS NOT NULL AND OLD.discogs_title <> ''
            AND (NEW.discogs_title IS NULL OR NEW.discogs_title = '')) THEN
        RAISE EXCEPTION 'streaming_album: discarding collected Discogs match metadata '
            'blocked (id=%); '
            '{_OPT_IN_HINT}',
            OLD.id;
    END IF;
    IF OLD.discogs_status = 'found' AND NEW.discogs_status IS DISTINCT FROM 'found' THEN
        RAISE EXCEPTION 'streaming_album: demoting discogs_status found to % blocked (id=%); '
            '{_OPT_IN_HINT}',
            NEW.discogs_status, OLD.id;
    END IF;
    IF OLD.discogs_release_id IS NOT NULL AND NEW.discogs_release_id IS NULL THEN
        RAISE EXCEPTION 'streaming_album: unlinking discogs_release_id blocked (id=%); '
            '{_OPT_IN_HINT}',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$\
"""

# All four row triggers deliberately carry no ``UPDATE OF`` column list: an
# OF list fires on SET-list *membership* (not value change), so a future
# BEFORE trigger rewriting a guarded column would bypass it, and it would be
# a second hand-maintained column enumeration drifting from the function
# bodies. Fire on every UPDATE; the function decides.
_DDL_GUARD_ALBUM_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_album_no_regress
BEFORE UPDATE OR DELETE ON lml_cache.streaming_album
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_album()\
"""

# Status gate is total (``IS DISTINCT FROM 'found'``), not a demotion
# blocklist: a blocklist lets two individually-legal hops launder the
# demotion (found → pending → not_found), and a bounce back to pending also
# re-queues the row for a redundant rate-limited probe. Re-keying the
# (album_id, service) PK is blocked — it relabels a collected probe as a
# different album/service. Discarding collected match metadata (the text
# columns to NULL or ``''`` — the extractors' two "empty" spellings —
# ``confidence`` to NULL) is blocked; replacing it with corrected values is
# allowed. On the CHECK-banned url columns (here and the track guard) the
# OLD-side ``<> ''`` legs are redundant-by-design: the guards keep one
# uniform two-spellings predicate, and they are re-asserted every boot while
# a CHECK dropped out-of-band stays dropped.
_DDL_GUARD_ALBUM_SERVICE_FN = f"""\
CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_album_service()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('{ALLOW_URL_REMOVAL_GUC}', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_album_service: DELETE blocked (album_id=%, service=%) — '
            'would discard collected streaming data; '
            '{_OPT_IN_HINT}',
            OLD.album_id, OLD.service;
    END IF;
    IF NEW.album_id IS DISTINCT FROM OLD.album_id
        OR NEW.service IS DISTINCT FROM OLD.service THEN
        RAISE EXCEPTION 'streaming_album_service: re-keying a probe row blocked '
            '(album_id=%, service=%); '
            '{_OPT_IN_HINT}',
            OLD.album_id, OLD.service;
    END IF;
    IF (OLD.url IS NOT NULL AND OLD.url <> '')
        AND (NEW.url IS NULL OR NEW.url = '') THEN
        RAISE EXCEPTION 'streaming_album_service: discarding a found url blocked '
            '(album_id=%, service=%) — legitimate removal sets NULL, never the empty string; '
            '{_OPT_IN_HINT}',
            OLD.album_id, OLD.service;
    END IF;
    IF (OLD.slug IS NOT NULL AND OLD.slug <> '')
        AND (NEW.slug IS NULL OR NEW.slug = '') THEN
        RAISE EXCEPTION 'streaming_album_service: discarding a collected slug blocked '
            '(album_id=%, service=%); '
            '{_OPT_IN_HINT}',
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
            '{_OPT_IN_HINT}',
            OLD.album_id, OLD.service;
    END IF;
    IF OLD.status = 'found' AND NEW.status IS DISTINCT FROM 'found' THEN
        RAISE EXCEPTION 'streaming_album_service: demoting found status to % blocked '
            '(album_id=%, service=%); '
            '{_OPT_IN_HINT}',
            NEW.status, OLD.album_id, OLD.service;
    END IF;
    RETURN NEW;
END;
$$\
"""

_DDL_GUARD_ALBUM_SERVICE_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_album_service_no_regress
BEFORE UPDATE OR DELETE ON lml_cache.streaming_album_service
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_album_service()\
"""

# Same total gate on the resolved pair; the lateral local_match <-> api_match
# move stays allowed (both resolved, nothing discarded). The gate is
# NULL-hardened (``NOT IN`` yields NULL, not true, for a NULL operand — an
# unhardened predicate waves a NULL status past the guard into the bare
# NOT NULL error). Re-keying the track identity and discarding collected
# resolution metadata (``resolved_via`` blank-or-null; the resolved ids and
# confidences to NULL) are blocked; corrections are allowed.
_DDL_GUARD_TRACK_RESULT_FN = f"""\
CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_track_result()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('{ALLOW_URL_REMOVAL_GUC}', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_track_result: DELETE blocked (id=%) — '
            'would discard collected streaming data; '
            '{_OPT_IN_HINT}',
            OLD.id;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.album_id IS DISTINCT FROM OLD.album_id
        OR NEW.artist IS DISTINCT FROM OLD.artist
        OR NEW.title IS DISTINCT FROM OLD.title THEN
        RAISE EXCEPTION 'streaming_track_result: re-keying track identity blocked (id=%); '
            '{_OPT_IN_HINT}',
            OLD.id;
    END IF;
    IF ((OLD.spotify_url IS NOT NULL AND OLD.spotify_url <> '')
            AND (NEW.spotify_url IS NULL OR NEW.spotify_url = ''))
        OR ((OLD.deezer_url IS NOT NULL AND OLD.deezer_url <> '')
            AND (NEW.deezer_url IS NULL OR NEW.deezer_url = '')) THEN
        RAISE EXCEPTION 'streaming_track_result: discarding a found track url blocked '
            '(id=%) — legitimate removal sets NULL, never the empty string; '
            '{_OPT_IN_HINT}',
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
            '{_OPT_IN_HINT}',
            OLD.id;
    END IF;
    IF OLD.resolution_status IN ('local_match', 'api_match')
        AND (NEW.resolution_status IS NULL
            OR NEW.resolution_status NOT IN ('local_match', 'api_match')) THEN
        RAISE EXCEPTION 'streaming_track_result: demoting resolution_status % to % '
            'blocked (id=%); '
            '{_OPT_IN_HINT}',
            OLD.resolution_status, NEW.resolution_status, OLD.id;
    END IF;
    RETURN NEW;
END;
$$\
"""

_DDL_GUARD_TRACK_RESULT_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_track_result_no_regress
BEFORE UPDATE OR DELETE ON lml_cache.streaming_track_result
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_track_result()\
"""

# Outside an opted-in transaction the floor only ratchets upward (equal is
# fine — a re-run that found the same coverage), so the export's regression
# assertion can't be quietly defused by lowering or deleting the baseline it
# compares against. Renaming a metric is blocked too: with DELETE blocked,
# rename blocked, and the PK rejecting a duplicate metric, sidelining a
# collected floor via INSERT-then-swap is structurally impossible — while a
# brand-new metric's first INSERT stays legal. The value gate is
# NULL-hardened (``NULL < x`` is NULL, not true; unhardened it would wave a
# NULL value past the guard into the bare NOT NULL error).
_DDL_GUARD_BASELINE_FN = f"""\
CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_coverage_baseline()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('{ALLOW_URL_REMOVAL_GUC}', true) = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'streaming_coverage_baseline: DELETE blocked (metric=%) — '
            'would discard the collected coverage floor; '
            '{_OPT_IN_HINT}',
            OLD.metric;
    END IF;
    IF NEW.metric IS DISTINCT FROM OLD.metric THEN
        RAISE EXCEPTION 'streaming_coverage_baseline: renaming metric % to % blocked — '
            'would sideline its collected floor; '
            '{_OPT_IN_HINT}',
            OLD.metric, NEW.metric;
    END IF;
    IF NEW.value IS NULL OR NEW.value < OLD.value THEN
        RAISE EXCEPTION 'streaming_coverage_baseline: lowering baseline % from % to % blocked; '
            '{_OPT_IN_HINT}',
            OLD.metric, OLD.value, NEW.value;
    END IF;
    RETURN NEW;
END;
$$\
"""

_DDL_GUARD_BASELINE_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_coverage_baseline_no_regress
BEFORE UPDATE OR DELETE ON lml_cache.streaming_coverage_baseline
FOR EACH ROW EXECUTE FUNCTION lml_cache.guard_streaming_coverage_baseline()\
"""

# TRUNCATE never fires row-level triggers, so without these the row guards
# leave a one-statement wipe path open. One shared TG_TABLE_NAME-generic
# statement-level guard closes it for all four tables. ``streaming_album``'s
# is defense-in-depth: bare TRUNCATE on it errors at the inbound FKs before
# any trigger fires, and CASCADE reaches the children's guards — but the
# direct guard stays in case the FK topology ever changes. Same GUC opts in.
_DDL_GUARD_TRUNCATE_FN = f"""\
CREATE OR REPLACE FUNCTION lml_cache.guard_streaming_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF current_setting('{ALLOW_URL_REMOVAL_GUC}', true) = 'on' THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION '%: TRUNCATE blocked — would discard all collected streaming data; '
        '{_OPT_IN_HINT}',
        TG_TABLE_NAME;
END;
$$\
"""

_DDL_GUARD_ALBUM_TRUNCATE_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_album_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_album
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate()\
"""

_DDL_GUARD_ALBUM_SERVICE_TRUNCATE_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_album_service_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_album_service
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate()\
"""

_DDL_GUARD_TRACK_RESULT_TRUNCATE_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_track_result_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_track_result
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate()\
"""

_DDL_GUARD_BASELINE_TRUNCATE_TRIGGER = """\
CREATE OR REPLACE TRIGGER streaming_coverage_baseline_no_truncate
BEFORE TRUNCATE ON lml_cache.streaming_coverage_baseline
FOR EACH STATEMENT EXECUTE FUNCTION lml_cache.guard_streaming_truncate()\
"""

# The four catalog tables in creation order (parents before children, per the
# FKs). The one authoritative list: tests import it instead of retyping the
# names, and derive FK-safe drop order by reversing it.
_TABLES = (
    "streaming_album",
    "streaming_album_service",
    "streaming_track_result",
    "streaming_coverage_baseline",
)

# Ordered: schema first, parents before children (FKs), tables before their
# widen block/indexes/triggers. Kept as a module constant so the bootstrap
# and the unit parity tests reference one list; the .sql reference mirrors
# this exact sequence (pinned by a normalized exact-equality test).
_DDL_STATEMENTS = (
    _DDL_SCHEMA,
    _DDL_ALBUM,
    _DDL_ALBUM_SERVICE,
    _DDL_ALBUM_SERVICE_WIDEN_CHECK,
    _DDL_ALBUM_SERVICE_STATUS_INDEX,
    _DDL_TRACK_RESULT,
    _DDL_TRACK_RESULT_STATUS_INDEX_DROP,
    _DDL_TRACK_RESULT_STATUS_INDEX,
    _DDL_COVERAGE_BASELINE,
    _DDL_GUARD_ALBUM_FN,
    _DDL_GUARD_ALBUM_TRIGGER,
    _DDL_GUARD_ALBUM_SERVICE_FN,
    _DDL_GUARD_ALBUM_SERVICE_TRIGGER,
    _DDL_GUARD_TRACK_RESULT_FN,
    _DDL_GUARD_TRACK_RESULT_TRIGGER,
    _DDL_GUARD_BASELINE_FN,
    _DDL_GUARD_BASELINE_TRIGGER,
    _DDL_GUARD_TRUNCATE_FN,
    _DDL_GUARD_ALBUM_TRUNCATE_TRIGGER,
    _DDL_GUARD_ALBUM_SERVICE_TRUNCATE_TRIGGER,
    _DDL_GUARD_TRACK_RESULT_TRUNCATE_TRIGGER,
    _DDL_GUARD_BASELINE_TRUNCATE_TRIGGER,
)

# Bootstrap preamble, run inside the single wrapping transaction:
#
# - ``lock_timeout`` bounds how long boot DDL waits behind a conflicting lock
#   (e.g. a long pipeline transaction on these tables) instead of queueing
#   indefinitely and stalling the lifespan.
# - ``pg_advisory_xact_lock`` serializes concurrent bootstraps. ``CREATE OR
#   REPLACE FUNCTION`` has no built-in self-serialization, so two simultaneous
#   boots (deploy overlap, app + offline DAO) can otherwise hard-abort with
#   "tuple concurrently updated". Key 842001 = LML#842; arbitrary but stable,
#   auto-released at COMMIT/ROLLBACK. Advisory keys share one keyspace per
#   database — record new discogs-cache advisory keys in discogs-etl's
#   CLAUDE.md before allocating another.
_BOOTSTRAP_ADVISORY_LOCK_KEY = 842001
_BOOTSTRAP_LOCK_TIMEOUT = "SET LOCAL lock_timeout = '10s'"
_BOOTSTRAP_ADVISORY_LOCK = f"SELECT pg_advisory_xact_lock({_BOOTSTRAP_ADVISORY_LOCK_KEY})"


async def set_up_streaming_catalog_schema(pg: PgSource) -> None:
    """Apply the idempotent streaming-catalog DDL (tables, indexes, guards).

    Called from the ``main.py`` lifespan (same best-effort posture as the
    sibling ``set_up_*_schema`` helpers: if the discogs-cache PG is
    unreachable, the caller logs and continues) and, from PR B on, from the
    offline DAO's ``connect()`` — both call sites share this one bootstrap.

    Runs as a single transaction on one pooled connection: every statement
    here is transactional DDL, so a mid-boot failure leaves either the whole
    catalog or none of it — never tables without their guard triggers. The
    preamble bounds lock waits and serializes concurrent bootstraps (see the
    constants above). The ``CREATE OR REPLACE`` function/trigger forms are the
    idempotent equivalents of ``IF NOT EXISTS`` (which triggers don't
    support); replacing an identical definition is a no-op in effect. The
    service-CHECK maintenance semantics (widen-only, steady-state no-op) are
    documented on ``_DDL_ALBUM_SERVICE_WIDEN_CHECK`` above.
    """
    async with pg.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_BOOTSTRAP_LOCK_TIMEOUT)
            await conn.execute(_BOOTSTRAP_ADVISORY_LOCK)
            for statement in _DDL_STATEMENTS:
                await conn.execute(statement)
