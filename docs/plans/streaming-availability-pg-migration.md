# Plan: migrate streaming_availability.db to row-level `lml_cache.*` PG canonical (LML#842)

Issue: [WXYC/library-metadata-lookup#842](https://github.com/WXYC/library-metadata-lookup/issues/842). Baseline: `origin/main` (the working tree sits on `prod`, which lags main by 20 commits; all work branches from `origin/main`).

## Goal

Make streaming-availability data canonical in row-level PG tables in the LML-owned `lml_cache.*` schema, where writers upsert rows instead of replacing a 53 MB SQLite file. The whole-file clobber becomes structurally impossible; the #672 coverage guard is reimplemented as row-level invariants; the `/admin/upload-streaming-db` + `/admin/download-streaming-db` endpoints are demoted to backup/restore conveniences; discogs-etl's daily read migrates in a coordinated PR. Migration is seed-and-hard-cutover — no dual-write period (#672 already paid down the two-lineages anti-pattern once).

## Preconditions — verified 2026-07-20

- **Blocker cleared.** WXYC/discogs-etl#313 closed 2026-07-21 00:31 UTC: prod discogs-cache PG now runs `shared_buffers=2GB` (25% of the confirmed 8 GB box), cache hit ratio 81.8% → 99.4%, `span.op:db` p95 ≈ 38 ms inside lookups. The "pre-tuning PG should not take on new data" deferral no longer applies. The streaming dataset is small (53 MB SQLite ≈ tens of MB of PG heap for ~65k album rows + track rows) against 2 GB of shared_buffers.
- **Epic state.** #834 PRs 1–4 (#835/#836/#837/#840) merged; staging cutover #838 closed 2026-07-20. Prod cutover #839 is still open, so **prod still serves the admin endpoints in volume mode** (bucket-mode code is on main, not yet promoted to `prod`). This plan does not depend on #839's ordering: the seed reads the canonical copy via `GET /admin/download-streaming-db`, whose wire contract is identical in volume mode and bucket mode. If #842's cutover lands before #839, the streaming half of #839's verification checklist becomes moot (only library.db still needs the bucket); if #839 lands first, nothing here changes.
- **Runtime never reads the file.** Verified against origin/main: the only runtime SQLite reads of `streaming_availability.db` are inside the `/admin/upload-streaming-db` coverage guard itself (`routers/admin.py`, `_streaming_coverage`). `/api/v1/streaming-check` is live-API fan-out only; `/api/v1/lookup` consumes streaming URLs only via the `streaming_links` table that `scripts/export_streaming_links.py` writes into `library.db`. This migration is entirely offline/data-plane; the request hot path is untouched.
- **One PG, one canonical.** Staging and prod LML share the same discogs-cache PG (`DATABASE_URL_DISCOGS`), so `lml_cache.*` gives a single canonical dataset with no staging/prod object divergence — a simplification over today's per-environment bucket objects. It also means every step that touches these tables is touching production data: never-delete-collected-data discipline applies from the first seed onward.
- **ETL rebuild safety already structural.** discogs-etl's truncate guard rejects schema-qualified names outright, and `lml_cache.*` tables are safe by omission from `CACHE_TABLES_TO_TRUNCATE_*`; monthly cache rebuilds cannot touch these tables.

## Current state (facts the design rests on)

### The SQLite schema (source of truth: `scripts/streaming_availability/results_db.py`)

Two tables:

- **`albums`** — one wide row per deduped library album. Identity/display fields (`normalized_artist`, `normalized_title` with a UNIQUE pair, `display_artist`, `display_title`, `library_ids` JSON, `formats` JSON, `genre`, `label`, `is_compilation`, `is_single`), a Discogs match group (`discogs_release_id/artist/title/status`), and four per-service column groups {status, url, confidence, matched_artist, matched_title, checked_at} for deezer/spotify/apple/bandcamp (+ `bandcamp_slug`, `spotify_id`).
- **`track_results`** — track-level resolution for singles/compilations: `album_id`, `artist`, `title`, `position`, `source`, `source_type`, `resolution_status`, `resolved_via`, `resolved_album_id`, `resolved_release_id`, `spotify_url/confidence`, `deezer_url/confidence`, `checked_at`, `created_at`, UNIQUE(`album_id`, `artist`, `title`).

**Schema drift in the canonical file:** `tidal_url`, `youtube_music_url`, `soundcloud_url` exist in the real prod DB (hand-migrated; `export_streaming_links.py` reads them daily and is green) but are never created by `results_db.py`. `spotify_revalidated_at` and `artist_on_spotify` are referenced only by archived scripts. The seed must introspect the actual file's columns, not the code schema.

### Script inventory (18 direct-I/O scripts; disposition below)

| # | Script | R/W | Disposition |
|---|--------|-----|-------------|
| 1 | `scripts/streaming_availability/results_db.py` (DAO + schema) | RW | **Port** — becomes the PG DAO; the chokepoint |
| 2 | `scripts/streaming_availability/__main__.py` (master pipeline) | RW | **Port** (via DAO) |
| 3 | `scripts/streaming_availability/report.py` | R | **Port** (via DAO) |
| 4 | `scripts/track_streaming/__main__.py` | RW | **Port** (via DAO) |
| 5 | `scripts/bandcamp_pipeline.py` | RW | **Port** (already uses `ResultsDB`) |
| 6 | `scripts/discogs_rematch.py` | RW | **Port** (direct sqlite3 → DAO) |
| 7 | `scripts/search_unmatched_compilations.py` | RW | **Port** (direct sqlite3 → DAO) |
| 8 | `scripts/revalidate_misses.py` | RW | **Port** (direct sqlite3 → DAO; drop the drifted `artist_on_spotify` dependency or carry the column) |
| 9 | `scripts/match_compilations.py` | R | **Port** (read-only; outputs CSV/SQL files) |
| 10 | `scripts/regenerate_report_stats.py` | R | **Port** (COUNT queries → PG) |
| 11 | `scripts/export_streaming_links.py` | R (streaming) / W (library.db) | **Port read side to PG** — the discogs-etl-coordinated piece; keep zero LML-internal imports so a bare clone can still run it |
| 12–18 | `canonicalize_albums.py`, `enrich_discogs_matches.py`, `spotify_artist_catalog.py`, `musicbrainz_matching.py`, `merge_cta.py`, `revalidate_spotify.py`, `validate_streaming_urls.py` | mixed | **Leave as SQLite-era artifacts** — all archived one-offs (2026-04-25 archive commit). Add a header note: they run only against a SQLite snapshot produced by the new export tool. Do not port. |

Support modules (`dedup.py` reads library.db, `discogs_enricher.py` already reads PG, `pipeline.py` and the `track_streaming/` helpers are in-memory) need no changes beyond call-site plumbing.

### Consumers and writers today

- **Daily read (external):** discogs-etl `sync-library.yml` (cron `0 12 * * *`) curls `GET /admin/download-streaming-db`, then runs LML's `export_streaming_links.py --streaming-db <file> --library-db <fresh library.db>` to write the `streaming_links` table, guarded by `STREAMING_APPLE_FLOOR` (≥100 non-null apple URLs) before uploading library.db back to LML. discogs-etl never parses the streaming DB itself. The same workflow already injects `DATABASE_URL_DISCOGS` (for its cache-health step), so the migrated path needs no new secrets there.
- **Weekly write:** LML `refresh-streaming.yml` (cron `0 0 * * 0`) downloads the canonical file, runs the incremental Spotify/Deezer pipeline (`scripts.streaming_availability --retry-errors`), dry-run-verifies the export, and re-uploads through the #672 guard.
- **Manual writes:** full-catalog / Apple / track / Bandcamp runs, executed locally, round-tripping the same endpoints.
- **The #672 guard** (`routers/admin.py`): on upload, compares 5 metrics (albums count; non-null `apple_url`/`spotify_url`/`deezer_url` counts; usable `track_results` count) against the currently-stored object with 5% tolerance; 409 on regression or unreadable baseline; `?force=true` override.

## Design

### D1. PG schema: normalized service rows for albums, wide row for tracks

Three tables in `lml_cache.*`, DDL owned by a new `entity/streaming_catalog.py` (mirroring the `entity/streaming_url_cache.py` pattern: module-level DDL constants, an idempotent `set_up_streaming_catalog_schema(pg)`, a documentation-only `.sql` twin):

```sql
CREATE TABLE IF NOT EXISTS lml_cache.streaming_album (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- seeded with SQLite ids via OVERRIDING SYSTEM VALUE, sequence advanced past max
    normalized_artist TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    display_artist TEXT NOT NULL,
    display_title TEXT NOT NULL,
    library_ids JSONB NOT NULL DEFAULT '[]',
    formats JSONB NOT NULL DEFAULT '[]',
    genre TEXT,
    label TEXT,
    is_compilation BOOLEAN NOT NULL DEFAULT FALSE,
    is_single BOOLEAN NOT NULL DEFAULT FALSE,
    discogs_release_id BIGINT,
    discogs_artist TEXT,
    discogs_title TEXT,
    discogs_status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (normalized_artist, normalized_title)
);

CREATE TABLE IF NOT EXISTS lml_cache.streaming_album_service (
    album_id BIGINT NOT NULL REFERENCES lml_cache.streaming_album(id) ON DELETE RESTRICT,
    service TEXT NOT NULL,           -- 'spotify' | 'deezer' | 'apple_music' | 'bandcamp' | 'tidal' | 'youtube_music' | 'soundcloud'
    status TEXT NOT NULL DEFAULT 'pending',
    url TEXT,
    slug TEXT,                       -- bandcamp only
    service_item_id TEXT,            -- spotify_id today; service-scoped
    confidence REAL,
    matched_artist TEXT,
    matched_title TEXT,
    checked_at TIMESTAMPTZ,
    PRIMARY KEY (album_id, service),
    CONSTRAINT streaming_album_service_valid CHECK (service IN ('spotify','deezer','apple_music','bandcamp','tidal','youtube_music','soundcloud'))
);
CREATE INDEX IF NOT EXISTS idx_streaming_album_service_status ON lml_cache.streaming_album_service (service, status);

CREATE TABLE IF NOT EXISTS lml_cache.streaming_track_result (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- same OVERRIDING SYSTEM VALUE seed spelling
    album_id BIGINT NOT NULL REFERENCES lml_cache.streaming_album(id) ON DELETE RESTRICT,
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    position TEXT,
    source TEXT,
    source_type TEXT,
    resolution_status TEXT NOT NULL DEFAULT 'pending',
    resolved_via TEXT,
    resolved_album_id BIGINT,
    resolved_release_id BIGINT,
    spotify_url TEXT,
    spotify_confidence REAL,
    deezer_url TEXT,
    deezer_confidence REAL,
    checked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (album_id, artist, title)
);
CREATE INDEX IF NOT EXISTS idx_streaming_track_result_status ON lml_cache.streaming_track_result (resolution_status);
```

**Why normalized service rows (vs mirroring the wide SQLite `albums` row):** (a) it matches the `lml_cache.album_streaming_url_cache` precedent (per-service rows keyed by a service discriminator with a CHECK, nullable url, checked_at) as the issue requires; (b) the schema-drift problem dissolves — `tidal`/`youtube_music`/`soundcloud` become ordinary service values instead of hand-migrated columns, and adding a service is a CHECK-widen (the `_DDL_ALTER_CHECK` idempotent-widen pattern already exists in `entity/streaming_url_cache.py`); (c) the row-level never-regress invariant (D3) is one trigger on one table instead of 4+ column-group cases. The cost — every script's per-album multi-service reads become a join or a small pivot helper in the DAO — is paid once in the DAO port, which the scripts need anyway. `track_results` stays wide: its `resolution_status` is per-track (not per-service), and only spotify/deezer apply; normalizing it buys nothing.

**Why keep the discogs match group on `streaming_album`:** it is match metadata about the album identity (which Discogs release this library album is), not a streaming availability result; three scripts filter on `discogs_release_id IS NULL`.

**Naming:** `streaming_album` / `streaming_album_service` / `streaming_track_result` (singular, like `album_streaming_url_cache`'s row-per-decision naming; distinct from the runtime `album_streaming_url_cache` so nobody conflates the offline catalog with the lookup-postprocess cache).

### D2. DAO port, not per-script rewrites

`ResultsDB` (in `scripts/streaming_availability/results_db.py`) becomes a PG-backed async DAO (asyncpg, `DATABASE_URL_DISCOGS`, same DSN semantics as the runtime), keeping its method surface (`get_stats`, `update_result`, `mark_bandcamp_not_found`, the pending-fetch queries, etc.) so `__main__.py`, `report.py`, `bandcamp_pipeline.py`, and `track_streaming/__main__.py` port mostly by construction-site changes. The DAO exposes a `services` pivot helper so callers that want the old wide shape get it in one place. The six direct-sqlite3 active scripts (#6–#11 in the table) are converted to the DAO (or, for the two read-only reporters, to direct parameterized PG queries). Hard cutover: the DAO speaks only PG; no dual-backend flag. SQLite remains only in (a) the archived scripts, untouched, and (b) the new snapshot export tool (D5).

DDL bootstrap runs in both places, idempotently: the FastAPI lifespan (a fifth `set_up_*_schema` call in `main.py`, per the CLAUDE.md lml_cache rule — origin/main already has four: streaming_url_cache, release_resolution_cache, library_release_override, and #841's discogs_rate_bucket; note the `prod` working tree predates the fourth) and the DAO's `connect()` (so offline scripts don't depend on a deployed app having booted first). **Shared parameter contract:** `set_up_streaming_catalog_schema(pg)` takes the same duck-typed `pg.execute(...)` object as `set_up_streaming_url_cache_schema(pg: PgSource)`; the lifespan passes its existing `PgSource`, and the DAO wraps its own asyncpg pool in a `PgSource` at connect time so both call sites share one bootstrap function.

### D3. Coverage guard → row-level invariants (three layers)

1. **Structural (trigger):** a `BEFORE UPDATE OR DELETE` trigger on `streaming_album_service` rejects transitions that discard a found URL — `OLD.url IS NOT NULL AND NEW.url IS NULL`, `OLD.status = 'found' AND NEW.status IN ('not_found','error')`, and all `DELETE`s — unless the transaction has opted in via `set_config('lml_cache.allow_url_removal', 'on', true)`. Same idea on `streaming_track_result` for its two URL columns. This is the structural replacement for the whole-file guard: the clobber isn't detectable-then-rejected, it's impossible by default. The GUC check stays in the trigger (one line) because it is what makes *documented manual SQL* the escape hatch for legitimate revocation (false-positive stripping, dedup repair) without superuser `DISABLE TRIGGER` gymnastics — but **no DAO revocation method ships in this migration**: the only URL-stripping scripts (`revalidate_spotify.py`, `validate_streaming_urls.py`) are in the archived do-not-port set, so a revocation API would be speculative. If a stripping flow is ever ported, it grows the explicit DAO method then. The runbook snippet (BEGIN; set_config; targeted UPDATE with a SELECT-first scope check; COMMIT) goes in `docs/scripts.md`. **DDL form:** triggers and PL/pgSQL functions have no `IF NOT EXISTS`; the bootstrap uses `CREATE OR REPLACE FUNCTION` + `CREATE OR REPLACE TRIGGER` (supported since PG14; the discogs-cache runs PG17 per wxyc-etl's `Dockerfile.pg17`), which is idempotent by replacement. This deliberately extends the `lml_cache.*` bootstrap convention beyond CREATE-TABLE-only — first trigger/function in the schema; called out in the PR description and the CLAUDE.md schema-ownership blurb so the convention change is explicit, not incidental.
2. **DAO discipline:** upsert methods never write NULL over a populated URL (`COALESCE(EXCLUDED.url, existing.url)` shape where appropriate).
3. **Export floor (kept) + write-side baseline:** `export_streaming_links.py` keeps discogs-etl's `STREAMING_APPLE_FLOOR` behavior and adds a pre-export assertion comparing per-service found-counts against a 5%-tolerance floor derived from `lml_cache.streaming_coverage_baseline` — a tiny one-row-per-metric table (`metric TEXT PRIMARY KEY, value BIGINT, updated_at`). **The baseline is refreshed only from the write path** — at the end of a successful pipeline run (`streaming_availability`/`track_streaming`/`bandcamp_pipeline` via a shared DAO call, and thus by `refresh-streaming.yml`) — never by the daily read-only export. Refreshing on the read side would make the floor track a slow bleed downward and defeat the batch-regression detection this table exists to restore (#672's property that the per-row trigger alone can't see: many small permitted removals adding up). `--force` skips the export assertion, loudly, mirroring today's `?force=true`.

### D4. Seed and verification (data-safety-critical)

New `scripts/seed_streaming_catalog.py` (entity-named, matching the `scripts/seed_library_release_overrides.py` precedent):

1. Download the canonical file via `GET /admin/download-streaming-db` against production (mode-agnostic re #839). Retain it as `streaming_availability.pre-pg-migration.db` — kept indefinitely (bucket + local), the never-delete snapshot.
2. Introspect the file's real columns (`PRAGMA table_info`), mapping drift columns to service rows (`tidal_url` → service `tidal`, etc.). Unknown columns hard-fail with a listing rather than silently dropping data.
3. Upsert into PG preserving SQLite `id`s (both tables) — the identities are `GENERATED ALWAYS` (PR A hardened this from the planned BY DEFAULT), so the seed INSERTs use the deliberate `OVERRIDING SYSTEM VALUE` spelling; a plain explicit-id INSERT is rejected. Then `setval` the identity sequences past max(id). `ON CONFLICT DO NOTHING` on re-run — the seed is idempotent and never overwrites (if a row differs from an existing PG row, report it, don't clobber).
4. **Verify:** for every metric in the old guard set (albums count, per-service non-null URL count including drift services, usable track_results count) plus per-status counts per service, assert PG == snapshot exactly (tolerance 0 — this is a copy, not a refresh). Emit the comparison table; nonzero diff = failure, no cutover.
5. Because writers are a weekly cron + manual runs, the freeze window is trivial: seed runs mid-week, and the final delta re-seed (same idempotent script) runs immediately before the cutover merge, after which file writers no longer exist.

Runs against the shared PG = production data; per repo data-safety rules the actual seed execution is gated on explicit user go-ahead (dry-run mode first, printing counts only).

### D5. Endpoint demotion + backup lineage

- `POST /admin/upload-streaming-db` and `GET /admin/download-streaming-db` are **demoted, not deleted**: download becomes "export a SQLite snapshot generated from PG on demand" is *not* worth building into the runtime (53 MB generation inside a request); instead both endpoints keep serving the bucket object, now explicitly documented as a **backup artifact, not the canonical** — and a new offline `scripts/export_streaming_snapshot.py` (PG → SQLite file in the legacy schema) regenerates that object. `refresh-streaming.yml` gains a post-run snapshot-upload step (`?force=true` never needed: snapshot is generated from canonical, guard demoted to sanity-check). This keeps a disaster-recovery lineage for expensive API data (the PG has no other backup story we control) without recreating two live lineages: the snapshot is write-only-from-canonical, consumed by nothing at runtime, and feeds the archived one-off scripts if ever needed.
- The #672 in-endpoint guard code stays as-is for the upload path (it now guards only the backup object; low stakes) — no code churn there beyond docstrings/docs.

### D6. discogs-etl coordinated PR (small by design)

- `.github/workflows/sync-library.yml`: delete the download-streaming-db step; add `DATABASE_URL_DISCOGS` to the sync step env (secret already exists in that repo).
- `scripts/sync-library.sh`: **rewrite the enrichment gate** at the top of the streaming block — today it is `if [[ -f "$STREAMING_DB" && -f "$LML_DIR/scripts/export_streaming_links.py" ]]` (line ~152), and in PG mode `$STREAMING_DB` never exists, so the block would silently skip, `streaming_links` would never be built, and the `STREAMING_APPLE_FLOOR` assertion (line ~171–178) would then hard-abort the daily prod sync with 0 apple URLs. New gate: `[[ -n "$DATABASE_URL_DISCOGS" && -f "$LML_DIR/scripts/export_streaming_links.py" ]]`; invocation becomes `export_streaming_links.py --database-url "$DATABASE_URL_DISCOGS" --library-db "$DB_PATH"`. The floor assertion itself is unchanged and still runs against the PG-produced `streaming_links` table (this is verified in the updated e2e test). Note the inner invocation failure is deliberately non-fatal today ("continuing without"); the floor assertion is the backstop that aborts before upload — that layering is preserved.
- `export_streaming_links.py` keeps zero LML-internal imports; it uses **asyncpg** (LML's driver), which discogs-etl already declares (`pyproject.toml` deps include `asyncpg>=0.29.0`) — no dependency change needed in that repo. Two consequences stated plainly: the script's synchronous `main` gets an async core (`asyncio.run`), and the service-row → wide-column pivot is **duplicated as raw SQL** in the export (`MAX(url) FILTER (WHERE service = 'spotify')`-style aggregation) rather than importing the DAO's pivot helper, because the zero-internal-imports rule (bare-clone invocability from discogs-etl) forbids the import. The duplication is covered by the parity test: export-from-PG must produce identical `streaming_links` rows to export-from-equivalent-SQLite-fixture.
- Docs: `README.md`, `docs/architecture.md`, `docs/automation.md` references to the file download.
- Rollout order: LML's export script grows `--database-url` while retaining `--streaming-db` for exactly one release window (the flag, not dual canonical data — reads only), so discogs-etl's daily job cannot be broken by a merge-order race; discogs-etl's PR flips the invocation; a follow-up LML commit drops `--streaming-db`. Both PRs cross-reference; the discogs-etl e2e test (`tests/e2e/test_sync_library_e2e.py`) needs its mock streaming DB replaced by a mock PG fixture or the invocation updated.

### D7. Workflows

- `refresh-streaming.yml`: drop download/upload steps; add `DATABASE_URL_DISCOGS` secret (public proxy DSN — new secret in the LML repo); pipeline runs directly against PG; add the snapshot-export upload step (D5). Local full-catalog/Apple/Bandcamp runbooks in `docs/scripts.md` update from "download → run → upload" to "run against PG" (they already need Spotify/Apple creds locally; they gain the DSN).
- Timing note: offline PG writes are trivial row upserts; no interaction with the lookup hot path worth scheduling around post-#313, but keep the weekly cron where it is (Sunday 00:00 UTC) anyway.

## PR chain (all ≤ ~1000 lines, TDD throughout, each PR CI-green before the next; one worktree per PR, branched from `origin/main`)

1. **PR A — schema + invariants** (`feat/842-a-streaming-catalog-schema`): `entity/streaming_catalog.py` (+ `.sql` twin) with the three tables, trigger function + triggers, `streaming_coverage_baseline` table, lifespan bootstrap wire-in, `-m pg` integration tests for DDL idempotency (double bootstrap) and the trigger semantics matrix (found→NULL rejected; GUC opt-in accepted; DELETE rejected; pending→found allowed). No DAO, no callers.
2. **PR B — PG DAO** (`feat/842-b-streaming-catalog-dao`): the PG rewrite of `ResultsDB` (asyncpg, injectable DSN/pool, pivot helper, baseline-refresh call), exercised by its own `-m pg` test suite. Split from PR A per review: the old `results_db.py` is 551 lines, so schema+DAO together would blow the ~1000-line budget and bundle two review concerns.
3. **PR C — seed + snapshot tools** (`feat/842-c-seed-and-snapshot`): `scripts/seed_streaming_catalog.py` (dry-run default, idempotent, drift-introspection, exact-count verification) + `scripts/export_streaming_snapshot.py` (PG → legacy-schema SQLite) + round-trip test (seed a fixture SQLite → PG → snapshot → identical metrics). **Gate: run the real seed (explicit user go-ahead — shared PG is production data) after this merges; verify; retain snapshot.**
4. **PR D — pipeline ports** (`feat/842-d-pipeline-ports`): `streaming_availability/` package (`__main__`, `report`) + `track_streaming/__main__.py` + `bandcamp_pipeline.py` onto the PG DAO; delete the SQLite `results_db` module.
5. **PR E — standalone script ports** (`feat/842-e-script-ports`): `discogs_rematch`, `search_unmatched_compilations`, `revalidate_misses`, `match_compilations`, `regenerate_report_stats`; archive-header notes on the 7 one-offs.
6. **PR F — export + consumers cutover** (`feat/842-f-export-cutover`): `export_streaming_links.py` `--database-url` mode (keeping `--streaming-db` transitionally), the parity test, `refresh-streaming.yml` PG mode + snapshot step, docs (`docs/scripts.md`, `docs/deployment.md`, `docs/architecture.md`, CLAUDE.md router line). **Paired discogs-etl PR** (D6) merges immediately after; then the delta re-seed + hard cutover: next daily sync runs PG-only.
7. **PR G — retire the file path** (`feat/842-g-retire-file-path`): drop `--streaming-db` from the export script, demote endpoint docs (D5 wording), close-out comment on #842 with the verification tables; file the follow-up to simplify #839's streaming checklist if #839 is still open by then.

Rollback at any point before PR F's discogs-etl flip: consumers still read the file lineage, which stays intact and untouched through PR E (writers keep writing it until PR D/E land — see freeze note in D4: between PR D landing and PR F's flip, the weekly cron is paused via `workflow_dispatch`-only or a guard commit, so the file lineage is frozen-but-valid rather than stale-and-trusted). Rollback after cutover: re-generate the file from PG via the snapshot tool — the direction that's safe because PG is canonical.

## Testing

- Unit: DAO query-shape tests, drift-column mapping, guard-floor math (port the `_check_streaming_regression` table-driven tests to the new floor assertion).
- Integration (`-m pg`, local throwaway postgres@16 on 5433 per the established pattern): DDL idempotency (double bootstrap), trigger semantics matrix, seed round-trip, export produces identical `streaming_links` rows from PG vs from an equivalent SQLite fixture (parity test — the key cutover-correctness test). Tests ride the existing `pg_pool` fixture / `DATABASE_URL_TEST` from `tests/integration/conftest.py` — hence the DAO's injectable DSN/pool (PR B); no second connection path in tests.
- discogs-etl e2e: update `test_sync_library_e2e.py` in the coordinated PR.
- CI markers stay in sync (`check-ci-marker-sync` is enforced).

## Out of scope

- Porting the 7 archived one-off scripts (header notes only).
- Any change to `/api/v1/streaming-check`, `lml_cache.album_streaming_url_cache`, or the lookup post-process warm path.
- #839 itself (HITL prod cutover for the volume/bucket; independent).
- Consolidating `streaming_album_service` with `album_streaming_url_cache` (different key spaces: library-album identity vs normalized artist/album strings; a future issue could dedupe probes across them).

## Open items to confirm during implementation

- Exact drift-column set in the real prod file (seed introspection will enumerate; plan assumes tidal/youtube_music/soundcloud + possibly `spotify_revalidated_at`/`artist_on_spotify`, which map to service rows / get dropped-with-report respectively).
- Whether `revalidate_misses.py`'s `artist_on_spotify` input still matters (if yes: a nullable `artist_on_spotify BOOLEAN` on `streaming_album`; if no: drop with note). Default: carry it as a column since the data exists and deleting collected data is against the rules.
- LML repo secret for `DATABASE_URL_DISCOGS` (public proxy DSN) — user-provisioned before PR E's workflow change can go green.
