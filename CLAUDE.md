# Claude Code Instructions for Library Metadata Lookup

## Project Overview

Library Metadata Lookup is a FastAPI service for WXYC radio that searches the library catalog and cross-references results with Discogs metadata. It was extracted from [request-o-matic](https://github.com/WXYC/request-o-matic) to separate search/lookup concerns from message parsing and Slack posting.

## Architecture

### Lookup Flow

1. **Artist Correction**: Fuzzy match artist against library catalog to fix typos
2. **Album Resolution**: If song provided without album, query Discogs for album names
3. **Search Pipeline**: Execute strategies in order until results are found (see below)
4. **Track Validation**: If fallback returned all artist albums, validate each against Discogs tracklists. When validation can't confirm any candidate AND we're showing artist-fallback results, `find_library_albums_with_cached_track()` consults the local PG cache directly ("releases by this artist whose tracklist contains this song") and promotes any matching library album over the unrelated fallback. Cache-only — never falls back to the API.
5. **Artwork Fetch**: Fetch album art from Discogs for each result
6. **Context Message**: Generate context string for the caller

### Search Strategy Pipeline

Strategies are defined declaratively in `core/search.py` and executed in order:

| Strategy | Trigger | Implementation |
|---|---|---|
| `ARTIST_PLUS_ALBUM` | Has artist, album, or song | `search_library_with_fallback()` |
| `SWAPPED_INTERPRETATION` | No results + "X - Y" / "X, Y" / "X. Y" format | `search_with_alternative_interpretation()` |
| `TRACK_ON_COMPILATION` | Song not found + artist + song | `search_compilations_for_track()` |
| `SONG_AS_ARTIST` | No results + song but no artist | `search_song_as_artist()` |

All strategy implementations live in `lookup/orchestrator.py`.

### Key Files

- `lookup/orchestrator.py` -- Core search logic: `perform_lookup()` and all helper functions
- `lookup/models.py` -- Re-exports generated API contract models (`LookupRequest`, `LookupResponse`, `LookupResultItem`)
- `generated/api_models.py` -- Pydantic v2 models generated from `wxyc-shared/api.yaml`
- `lookup/router.py` -- `POST /lookup` endpoint
- `library/db.py` -- SQLite FTS5 search with LIKE + fuzzy fallback chain. Detects `compilation_track_artist` table at connect time; when present, artist searches include compilations featuring that artist via JOIN/UNION.
- `discogs/service.py` -- Discogs API client with optional PostgreSQL cache
- `discogs/cache_service.py` -- PostgreSQL cache (asyncpg + pg_trgm)
- `discogs/memory_cache.py` -- In-memory TTL cache (cachetools)
- `core/search.py` -- Declarative search strategy pattern + ambiguous format detection
- `discogs/markup_parser.py` -- Discogs markup parser: tokenize/resolve `[a=Name]`, `[a12345]`, `[b]...[/b]`, etc. into structured `ResolvedToken` models. Includes `EntityResolver` protocol and `DiscogsServiceResolver` adapter for async ID resolution. Translated from iOS `DiscogsMarkupParser.swift`.
- `discogs/matching.py` -- Discogs-specific normalization (strip_discogs_suffix, normalize_for_track_comparison, normalize_artist_for_validation)
- `core/dependencies.py` -- FastAPI DI for LibraryDB + DiscogsService
- `streaming/router.py` -- `POST /streaming-check` endpoint for single-release streaming availability
- `streaming/orchestrator.py` -- Concurrent streaming checks across Spotify, Deezer, Apple Music, Bandcamp
- `streaming/models.py` -- Request/response Pydantic models (`StreamingCheckRequest`, `StreamingCheckResponse`)
- `streaming/dependencies.py` -- FastAPI DI for streaming service clients (SpotifyClient, DeezerClient, etc.)
- `identity/router.py` -- `GET /identity/resolve` and `POST /identity/bulk` endpoints for identity resolution
- `identity/models.py` -- Pydantic models for identity resolution responses
- `identity/dependencies.py` -- FastAPI DI for EntityStore (reuses `DATABASE_URL_DISCOGS` pool)
- `scripts/entity_resolution/store.py` -- Entity store CRUD against `entity.identity` PG table

### Identity Resolution Endpoints

The service exposes REST endpoints for querying the `entity.identity` table in the discogs-cache PostgreSQL database. These endpoints are consumed by semantic-index (via `--entity-source=lml`) and other pipeline tools.

- `GET /identity/resolve?name=Stereolab` -- Look up a single artist name. Returns 200 with external IDs or 404.
- `POST /identity/bulk` with `{"names": ["Stereolab", "Autechre", ...]}` -- Resolve a batch of names. Returns `identities` (found) and `unresolved` (not found).
- `POST /api/v1/identity/bulk-resolve-libraries` -- Cross-cache-identity contract endpoint per the 2026-05-09 pivot (BS#800). Backend POSTs library rows; LML composes per-source provenance via §3.4.1.1 Rules 2-6 and returns one verdict per row (`kind: single_artist | compilation | unresolved`). Implementation lives in `identity/bulk_resolve.py`; sits under `/api/v1/` so it inherits `LML_API_KEY` bearer auth.

Both endpoints return 503 when `DATABASE_URL_DISCOGS` is not set or the entity schema is not applied.

### Streaming Check Endpoint

`POST /api/v1/streaming-check` checks whether an album is available on streaming platforms. Used by tubafrenzy and Backend-Service to set the `on_streaming` flag when a new release is added to the library.

Request: `{"artist": "Stereolab", "title": "Aluminum Tunes"}`

Response includes `on_streaming` (true/false/null) and per-service match details with URLs and confidence scores. Checks run concurrently across Spotify, Deezer, Apple Music, and Bandcamp. The endpoint is stateless -- it does not cache results.

Requires `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` for Spotify checks. Other services (Deezer, Apple Music, Bandcamp) need no auth. If Spotify credentials are not set, Spotify checks are skipped.

### Release Resolve Endpoint

`POST /api/v1/releases/resolve` takes a Discogs release URL or Bandcamp album URL (or an explicit `(source, id)` pair) and returns canonical release metadata, cross-source identifiers, and a streaming-availability snapshot — everything tubafrenzy's rotation-release create form needs to prefill in one round trip.

Request (one of):
```json
{ "url": "https://www.discogs.com/release/12345" }
{ "url": "https://artist.bandcamp.com/album/slug" }
{ "source": "discogs_release", "id": "12345" }
```

Response:
```json
{
  "source": "discogs_release",
  "source_id": "12345",
  "canonical": { "artist": "Juana Molina", "title": "DOGA", "label": "Sonamos", "catno": "SON-001", "year": 2024, "country": null, "formats": [] },
  "identifiers": { "discogs_release_id": 12345, "discogs_artist_id": 999, "spotify_album_id": "abc...", "bandcamp_album_url": null, ... },
  "streaming": { "on_streaming": true, "sources": { ... } },
  "matched_via": "discogs_cache",
  "warnings": []
}
```

`matched_via` (per WXYC/library-metadata-lookup#329) tags the provenance tier for the canonical metadata so callers can label results "fresh from Discogs" vs "from cache". Values: `discogs_cache` (served from the local discogs-cache PG), `discogs_live_api` (local cache missed, fell through to the live Discogs API — the result is then written back to PG so the next request for the same release is free), or `null` (no canonical metadata was produced — rate-limit, not-found, non-Discogs source, or unrecognized URL). Live-tier hits also bump an ad-hoc `discogs_live_release_hit` counter on the request's cache_stats recorder so freshness-gap impact is visible in PostHog / Sentry. The cache write-back, rate-limit handling (60 req/min token bucket + concurrency semaphore in `discogs/ratelimit.py`), exponential backoff with jitter on 429s, and graceful degradation to `null` on quota exhaustion are all inherited from `DiscogsService.get_release`; the resolver is just a thin projection on top.

Implementation lives in `release/`:
- `url_parser.py` — pure URL → `(source, id)` parser. Supports Discogs `/release/<id>` and `/master/<id>` (with locale prefixes and slugs) and Bandcamp `<artist>.bandcamp.com/album/<slug>`.
- `discogs_resolver.py` — wraps `DiscogsService.get_release()` so the existing 3-tier cache + API rate-limit handling applies.
- `bandcamp_resolver.py` — fetches the Bandcamp album page (rate-limited via the existing `BandcampClient`) and parses the embedded JSON-LD `MusicAlbum` blob. Self-released (publisher subdomain == artist subdomain) returns `label: null`.
- `orchestrator.py` — dispatches by source, runs `check_streaming_availability`. When the input is a Bandcamp URL, the Bandcamp leg of the streaming check is short-circuited (skipped) — we already know the answer and re-fuzzy-matching would burn 2+ rate-limited HTTP calls. Identity write-back via the existing `EntityStore.upsert_identity()` (`ON CONFLICT ... DO UPDATE` with `COALESCE` — never clobbers); Bandcamp `bandcamp_id` is the URL's slug, matching what `bandcamp_pipeline.py` writes.

Genre and style are intentionally not surfaced — the rotation form has no genre field, so the music director picks manually.

The endpoint always returns 200 with a `warnings[]` array. Partial failures (Discogs rate limit, malformed Bandcamp page, missing master support) become warnings rather than 5xx; the form falls back to manual entry.

`source` may be `"discogs_release"`, `"discogs_master"`, `"bandcamp"`, or `"unknown"`. Consumers should always check `warnings` before consuming `canonical`.

### Discogs Cache (Optional)

The service supports an optional PostgreSQL cache for Discogs data:

1. Query local PostgreSQL cache first
2. On cache miss, query Discogs API
3. Write API results back to cache
4. Gracefully degrade to API-only if cache unavailable

Set `DATABASE_URL_DISCOGS` to enable. The cache schema is defined in [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl).

### External-Cache Fallback for `/api/v1/lookup` (Phase 1.5 mojibake recovery)

`POST /api/v1/lookup` accepts an opt-in `include_external_caches: bool` flag (default `false`). When the WXYC library catalog returns no results AND the request supplies an `artist` field AND the flag is set, the orchestrator runs a fuzzy artist-name search against the discogs-cache PostgreSQL DB; on miss it falls through to musicbrainz-cache. The matched canonical name is wrapped in a synthetic `LookupResultItem` (`library_item.id = 0`, `call_number = "(external)"`, `library_url = ""`) so the caller's existing scoring code applies as-is. The response carries an `external_source` field — `'library' | 'discogs' | 'musicbrainz' | null` — for provenance.

Used by the lossy-mojibake matcher (`tubafrenzy/scripts/db/recovery/lossy_mojibake_recovery.py`) to recover canonical artist names for skeletons not in the WXYC physical catalog. Implementation in `lookup/external_search.py`; the discogs-cache trigram query lives in `discogs/cache_service.py:search_artists_by_name`. Both legs UNION their primary artist table with the alias/variation table (discogs: `artist_name_variation`; musicbrainz: `mb_artist_alias`) so ASCII transliterations and alternate spellings hit, and the canonical primary name is what comes back.

Wiring:
- `DATABASE_URL_DISCOGS` (already required for the standard cache) covers the discogs leg.
- `DATABASE_URL_MUSICBRAINZ` is new — when unset the MB leg is skipped silently.
- Existing callers (no flag) see no behavior change and no extra queries.

## Development

### Running locally

```bash
uvicorn main:app --reload
```

### Branches

- **`main`** -- Development. Push here to deploy to **staging**.
- **`prod`** -- Production. Push here to deploy to **production**.

## Testing

### Unit Tests

All external services (LibraryDB, DiscogsService) are mocked. Run frequently:

```bash
uv run pytest tests/unit/ -v
```

### Test Patterns

- Use factories from `tests/factories.py`: `make_library_item()`, `make_discogs_result()`, `LOOKUP_BODY`
- Mock `discogs_service` with `AsyncMock` and construct `DiscogsSearchResponse`/`DiscogsSearchResult` models
- `DiscogsSearchResult` requires `release_id: int` and `release_url: str` (no defaults)
- Mock `LibraryDB` with `AsyncMock` including `search`, `find_similar_artist`, `connect`, `close`
- Use `unittest.mock.patch` for `lookup.orchestrator.lookup_releases_by_track` in pipeline tests
- `test_orchestrator.py` tests `perform_lookup()` end-to-end with mocked dependencies
- `test_orchestrator_helpers.py` tests individual helper functions in isolation

### Bug Fix Protocol

For every lookup bug where a search fails to find the correct release:

1. Create a **unit test** in `tests/unit/` that reproduces the bug with mocked data
2. Create an **integration test** in `tests/integration/` that verifies the fix against real APIs
3. Integration test should assert that false positives are excluded AND correct results are included

### TDD (Required)

All code changes in this repo follow test-driven development. This is not optional.

1. **Red**: Write a failing test that describes the desired behavior. Run it and confirm it fails.
2. **Green**: Write the minimum implementation to make the test pass.
3. **Refactor**: Clean up the implementation while keeping tests green.

Concretely this means:
- New features: write tests for the new behavior first, watch them fail, then implement.
- Bug fixes: write a test that reproduces the bug first, confirm it fails, then fix.
- Refactors: ensure existing tests pass before and after. Add tests for any behavior not already covered.
- Do not write implementation code without a corresponding failing test preceding it.

## Environment Variables

Required:
- `DISCOGS_TOKEN` -- Discogs API token

Optional:
- `DATABASE_URL_DISCOGS` -- PostgreSQL URL for Discogs cache
- `DATABASE_URL_MUSICBRAINZ` -- PostgreSQL URL for the musicbrainz-cache. Powers the MB leg of the `/api/v1/lookup` external-cache fallback (Phase 1.5 mojibake recovery). Optional; when unset the MB leg is skipped.
- `SPOTIFY_CLIENT_ID` -- Spotify client ID for streaming availability checks
- `SPOTIFY_CLIENT_SECRET` -- Spotify client secret for streaming availability checks
- `SENTRY_DSN` -- Sentry error tracking. Init lives in [`wxyc-fastapi`](https://github.com/WXYC/wxyc-fastapi); LML calls `init_sentry(service_name="library-metadata-lookup", environment=settings.environment, ...)` at startup. The default `HttpxIntegration` is on, so outbound calls (Discogs, Spotify, Deezer, Apple Music, Bandcamp) are traced.
- `POSTHOG_API_KEY` -- PostHog telemetry. Client construction lives in `wxyc-fastapi` as a process-wide singleton; LML's `core/dependencies.get_posthog_client` only wraps it with the LML-side `enable_telemetry` flag. The shared client warns once per process when this is unset.
- `LIBRARY_DB_PATH` -- Path to SQLite database (default: `library.db`)
- `ADMIN_TOKEN` -- Bearer token for admin endpoints (upload endpoint)
- `STREAMING_WEBHOOK_URLS` -- Comma-separated URLs to POST streaming status changes after library.db upload
- `ETL_NOTIFY_KEY` -- Bearer token used by LML when *pushing* the streaming-status webhook to tubafrenzy
- `LML_API_KEY` -- Bearer token required from tubafrenzy / Backend-Service callers (see "Inbound Auth" below)
- `LML_REQUIRE_AUTH` -- When `true`, enforce `LML_API_KEY` on protected endpoints. Default `false` (see rollout phasing in `core/auth.py`)
- `LML_RESOLVE_ARTIST_CANONICAL` -- When `true`, the canonical-artist resolver pre-pass in `search_compilations_for_track` swaps the inbound artist name for the top trigram match in `discogs-cache` when the similarity score >= `CANONICAL_ARTIST_SIMILARITY_FLOOR` (`lookup/orchestrator.py`). Default `false`. The pre-pass *always* runs in shadow mode (emits an INFO log line and a `resolver_pre_pass` Sentry transaction data attribute on every lookup) regardless of the flag, so the floor can be calibrated against real traffic before enforcement is enabled. Flip to `true` in Railway after `scripts.resolver_calibration` reports an FP-rate ≤ 0.5% for the chosen floor. See WXYC/library-metadata-lookup#318.

### Cross-cache-identity feature flags

Per-cache toggles for which `wxyc_library` hook table the matcher reads (legacy schema vs. new normalized schema), plus an emergency rollback flag for the §3.2.2.1 manual-override skip endpoint. All default `false`.

The **canonical inventory** (with naming-convention rationale, phase state machine, rollout-checklist locations, and approval gates) lives in `WXYC/Backend-Service/CLAUDE.md` "Cross-cache-identity feature flags (canonical inventory)". When a flag is renamed or its default changes, both the canonical Backend section AND this list must update in the same PR; CI on this repo grep-asserts the names listed here match the §4.2 inventory.

| Flag | Scope | Default | Set true when |
|---|---|---|---|
| `LML_USE_NEW_HOOK_DISCOGS` | per-cache (Docker discogs, port 5433) | `false` | Docker discogs cache parity-check passes 7 consecutive days |
| `LML_USE_NEW_HOOK_DISCOGS_FULL` | per-cache (Homebrew full discogs) | `false` | Homebrew (full) discogs cache parity-check passes 7 consecutive days |
| `LML_USE_NEW_HOOK_MUSICBRAINZ` | per-cache | `false` | musicbrainz cache parity-check passes 7 consecutive days |
| `LML_USE_NEW_HOOK_WIKIDATA` | per-cache | `false` | wikidata cache parity-check passes 7 consecutive days |
| `LML_MANUAL_OVERRIDE_CHECK_DISABLED` | global | `false` | Emergency rollback only — disables the §3.2.2.1 manual-override skip endpoint call. Backend's write rules still reject low-confidence reruns of manual rows correctly, just less efficiently. |

Production location: Railway environment variables for the LML service. Updater is Jake via the Railway dashboard. The per-cache flags require 7 consecutive days of clean parity-check audit (E5 daily report); `LML_MANUAL_OVERRIDE_CHECK_DISABLED` is for unscheduled emergency rollback only.

Plan reference: `WXYC/wiki/plans/library-hook-canonicalization-plan.md` §4.2.

### Inbound Auth (`LML_API_KEY`)

`core/auth.py:require_lml_key` is a FastAPI dependency that validates `Authorization: Bearer <LML_API_KEY>` on every tubafrenzy / Backend-Service-facing endpoint. Wired in `main.py` via `app.include_router(..., dependencies=[Depends(require_lml_key)])` for the `lookup`, `library`, `discogs`, and `streaming` routers.

Three auth surfaces in this service, intentionally separate:

| Surface | Direction | Env var | Implementation |
|---|---|---|---|
| Inbound from tubafrenzy / Backend-Service | tubafrenzy → LML | `LML_API_KEY` | `core/auth.py:require_lml_key` (router-level dep) |
| `/admin/*` (library.db upload) | ETL → LML | `ADMIN_TOKEN` | `routers/admin.py:_validate_auth` (per-route call) |
| Outbound streaming-status webhook | LML → tubafrenzy | `ETL_NOTIFY_KEY` | `routers/admin.py:_send_streaming_webhook` (sends `Authorization: Bearer ...`) |

Untouched: `/health`, `/identity/resolve`, `/identity/bulk`. Identity routes are consumed by semantic-index too, so locking them down is a separate decision.

**Rollout:** `LML_REQUIRE_AUTH` defaults to `false` so the dep can ship before consumers send the header. Once tubafrenzy and Backend-Service are updated, flip the flag in Railway to enforce.

## Code Style

- Line length: 100 chars
- Use `ruff format` for formatting, `ruff check` for linting
- Type hints encouraged
- Async/await for all I/O operations
- Pre-commit hook runs `ruff check` + `ruff format --check` on staged `.py` files. Activate with: `git config core.hooksPath .githooks`

## Deployment

### Infrastructure

- Hosted on Railway with CI-driven deploys (automatic deploys disabled)
- Railway volume mounted at `/data` stores `library.db` persistently across deploys
- Optional PostgreSQL cache for Discogs data via `DATABASE_URL_DISCOGS` (gracefully degrades to API-only)
- `LIBRARY_DB_PATH=/data/library.db` on Railway

### Branch Strategy

- **`main`** -- CI deploys to **staging** after lint + typecheck + unit tests pass
- **`prod`** -- CI deploys to **production** after lint + typecheck + unit tests pass
- Both environments get smoke tests after deploy

### CI/CD Pipeline (`.github/workflows/ci.yml`)

| Job | Trigger | Depends on |
|---|---|---|
| Lint & Format | All pushes + PRs | -- |
| Type Check | All pushes + PRs | -- |
| Default Tests | All pushes + PRs | -- |
| External API Tests | All pushes + PRs | -- |
| PG Tests | All pushes + PRs | -- |
| CI Marker Sync | All pushes + PRs | -- |
| Deploy to Staging | Push to `main` | lint, typecheck, test, pg |
| Smoke Test (Staging) | Push to `main` | deploy-staging |
| Deploy to Production | Push to `prod` | lint, typecheck, test, pg |
| Smoke Test (Production) | Push to `prod` | deploy-production |

### Pytest markers (architecture A)

Markers route CI by infrastructure, not taxonomy. See the WXYC test-patterns guide ([WXYC/wiki, plans/test-patterns.md, Section 3](https://github.com/WXYC/wiki/blob/main/plans/test-patterns.md#3-marker-conventions)) for the canonical vocabulary; LML uses two:

| Marker | Meaning | CI provisions |
|---|---|---|
| `pg` | needs a PostgreSQL service | `postgres:16-alpine` service container, `DATABASE_URL_TEST` |
| `external_api` | needs a real third-party API key (Discogs) | `DISCOGS_TOKEN` secret |

Default `pytest` (no `-m`) runs every unmarked test across `tests/unit/`, `tests/integration/`, and `tests/e2e/`. Tier directories are documentation; CI routes by markers.

**Default Tests** runs `pytest -v --cov=...` -- pyproject's `addopts = "-m 'not pg and not external_api'"` excludes the infra-tagged tests.

**External API Tests** runs `pytest -v -m external_api`. The `tests/e2e/discogs/*` suite and the `TestDiscogsApiSearch` / `TestEntityResolution` classes in `tests/integration/test_api_discogs.py` hit the real Discogs API; they self-skip at collection if `DISCOGS_TOKEN` is unset (PR runs from forks may have no secret access).

**PG Tests** runs `pytest -v -m pg` against a `postgres:16-alpine` service container on port 5433. The `EntityStore` CRUD tests in `tests/integration/test_entity_resolution.py` run end-to-end against a fresh `entity` schema. The Discogs reconciliation tests skip themselves when the `release_artist` table is missing -- that table is part of the discogs-cache fixture and is too large to load in CI. `tests/integration/test_va_discogs_lookup.py` self-skips without `DATABASE_URL_DISCOGS`, which is intentional in CI.

**CI Marker Sync** invokes the reusable workflow at `WXYC/wxyc-etl/.github/workflows/check-ci-marker-sync.yml` to guarantee that every `@pytest.mark.<X>` actually used by a test is either re-selected by some CI `pytest -m` invocation or explicitly opted out via a `# ci-sync-skip: <marker> reason: <text>` comment in `pyproject.toml`. This guards against the silent-deselection bug pattern (WXYC/discogs-etl#103, WXYC/library-metadata-lookup#159).

### Library Database Upload

The `library.db` file lives on a Railway volume, not in git. It's uploaded via:

```
POST /admin/upload-library-db
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: multipart/form-data
```

The upload endpoint validates the SQLite file, closes the current DB connection,
atomically replaces the file, and returns `{"status": "ok", "row_count": <int>}`.

The ETL script in [discogs-cache](https://github.com/WXYC/discogs-etl) (`scripts/sync-library.sh`) handles daily uploads to both staging and production.

### Streaming Database Backup (Upload + Download)

`streaming_availability.db` is the analysis database for streaming-availability search results. It's a sibling of `library.db` on the Railway volume. Two symmetric admin endpoints, both gated by `ADMIN_TOKEN`:

```
POST /admin/upload-streaming-db    # multipart upload, validates `albums` table
GET  /admin/download-streaming-db  # FileResponse stream of the volume copy (404 if missing)
```

The download endpoint lets the daily library-sync pipeline (WXYC/discogs-etl) read the file directly from the Railway volume instead of round-tripping it through a GitHub Release, making the volume the canonical source.

### Health Check Behavior

When `library.db` is missing (e.g., on first deploy before first upload):
- `get_library_db()` returns a LibraryDB instance with `is_available() = False`
- Health endpoint returns `{"status": "unhealthy", "services": {"database": "error"}}` (503)
- Service is functional for non-database endpoints
- After uploading library.db, next request triggers reconnection

The `services.discogs_api` field on `GET /health` carries one of a fixed vocabulary of values defined by `DiscogsApiCheckResult` in `discogs/service.py`:

| Value | Meaning |
|---|---|
| `ok` | Probe succeeded (200) |
| `auth-error` | Token rejected (401, 403) — usually rotation drift |
| `rate-limited` | Discogs is throttling us (429) |
| `upstream-error` | Discogs returned 5xx |
| `network-error` | Connection or timeout failure (`httpx.ConnectError`/`TimeoutException`/`NetworkError`) |
| `error` | Unknown / unclassified failure |
| `unavailable` | `discogs_service` not configured (no token) |

Any value other than `ok` / `unavailable` flips the overall status to `degraded` (or `unhealthy` if a core service like `database` is also down).

The probe also projects its result onto the active Sentry trace as the `discogs_api.check` tag (e.g. `discogs_api.check=auth-error`), so historic `/health` incidents can be queried by failure mode in the Sentry trace explorer without re-pulling Railway logs.

## Scripts

### Streaming Report Stats Regenerator (`scripts/regenerate_report_stats.py`)

Refreshes the data-driven values in `streaming_analysis_report.md`. The report contains `<!-- gen:KEY -->VALUE<!-- /gen -->` markers, each bound to a query in `QUERIES`. The script runs every query, replaces each marker's value, and errors loudly on any unknown or unresolved marker.

**When to run:** After any change to `streaming_availability.db` (pipeline runs, validation passes, etc.) or after adding a new method to the report. Re-run before committing to keep the doc honest.

**Workflow for a new method:** (1) write the prose for the new Method block in the report by hand, (2) if it produces a number worth tracking in a headline table, register a `Query` in `QUERIES` and reference it via a marker, (3) run the regenerator. The script will fail if any marker has no registered query, or any query has no marker — preventing silent drift between the registry and the doc.

**Usage:**
```bash
TUBAFRENZY_DB_PASSWORD=... uv run python -m scripts.regenerate_report_stats [--dry-run]
```

Markers backed by tubafrenzy MySQL are skipped (with a warning) if `TUBAFRENZY_DB_PASSWORD` is not set; SQLite-backed markers always run.

### Discogs Cache Benchmark (`scripts/benchmark_cache.py`)

Benchmarks PG cache vs Discogs API response times for `search()`. Useful for evaluating cache effectiveness after discogs-cache ETL runs.

**Usage:**
```bash
.venv/bin/python scripts/benchmark_cache.py --iterations 3
```

Loads `DISCOGS_TOKEN` and `DATABASE_URL_DISCOGS` from `.env` or the Railway CLI linked project.

### API Model Generation (`scripts/generate_api_models.sh`)

Generates Pydantic v2 models from `wxyc-shared/api.yaml`. Uses a local sibling `wxyc-shared` directory if available, otherwise downloads from GitHub. The generated file (`generated/api_models.py`) is committed to git. Re-run after api.yaml changes.

**Usage:**
```bash
bash scripts/generate_api_models.sh
```

Requires `datamodel-code-generator` (included in dev dependencies).

### VA Disambiguation (`scripts/va_disambiguate/`)

Disambiguates "Various Artists" entries in the WXYC flowsheet and populates the `COMPILATION_TRACK_ARTIST` table with per-track artist credits from the Discogs cache.

**Usage:**
```bash
uv run python -m scripts.va_disambiguate [OPTIONS]
```

Options: `--dry-run` (extract only), `--stats` (show progress), `--apply` (execute SQL), `--confidence-threshold FLOAT` (default 0.70), `--verbose`.

Generates two SQL files for review before application: `va_flowsheet_updates.sql` (updates `ARTIST_NAME`/`ARTIST_ID` on flowsheet entries) and `va_catalog_inserts.sql` (inserts into `COMPILATION_TRACK_ARTIST`). Requires `DATABASE_URL_DISCOGS` and `TUBAFRENZY_DB_PASSWORD` environment variables.

### Bandcamp Pipeline (`scripts/bandcamp_pipeline.py`)

Unified pipeline for discovering Bandcamp artist slugs and matching them to WXYC albums. Connects two phases via `asyncio.Queue` so album matching begins as soon as slugs are discovered.

**Phase 1 (Search):** Queries Bandcamp's autocomplete API to discover artist slugs. Writes to `bandcamp_slug` column in `streaming_availability.db`.

**Phase 2 (Lookup):** For artists with known slugs, scrapes the Bandcamp catalog page and fuzzy-matches album titles using `score_match()`. Writes album-specific URLs to `bandcamp_url`.

**Usage:**
```bash
python -m scripts.bandcamp_pipeline [--phase {search,lookup,both}] [--include-streaming] [--artist-fallback] [--dry-run] [--limit N] [--db-path PATH]
```

Options: `--phase` (default: both, runs concurrently), `--include-streaming` (search all artists, not just not-on-streaming), `--artist-fallback` (write artist-level URL when no album match), `--dry-run` (report what would happen without changes), `--limit N` (max artists/slugs to process).

Uses `BandcampClient` (`scripts/bandcamp_client.py`) extending `BaseStreamingClient` with rate limiting (1 req/s, semaphore 2) and 429 retry with exponential backoff. Optionally loads Wikidata slugs via `DATABASE_URL_WIKIDATA`.

### Resolver Calibration (`scripts/resolver_calibration/`)

Sweeps the trigram-similarity floor used by `lookup/orchestrator.resolve_canonical_artist` (the pre-pass for `search_compilations_for_track` — see WXYC/library-metadata-lookup#318). Builds three labeled datasets — positives from `artist_name_variation` and `entity.identity`, negatives from sampled close-but-distinct `artist` pairs — and writes a precision/recall sweep + a borderline-band CSV.

**Output** (defaults to `docs/resolver-calibration/`):
- `calibration_sweep.csv` — threshold, TP rate, FP rate, sample sizes, swap count.
- `borderline.csv` — pairs whose score sits in `[chosen_floor − 0.05, chosen_floor + 0.05]`, sorted by score, for eyeball QA of the decision boundary.

After running, update `CANONICAL_ARTIST_SIMILARITY_FLOOR` in `lookup/orchestrator.py` if the chosen floor differs from the in-tree value, and commit the CSVs alongside a one-page `docs/resolver-calibration/README.md` documenting the FP-rate tolerance that drove the choice. The same script is used to re-validate after large discogs-cache refreshes; commit fresh CSVs each run so the calibration history stays in version control.

**Usage:**
```bash
DATABASE_URL_DISCOGS=postgresql://... \
  uv run python -m scripts.resolver_calibration \
    --output-dir docs/resolver-calibration/ \
    --positive-sample-size 5000 \
    --negative-sample-size 5000 \
    --fp-rate-target 0.005
```

### Artist Name Variation Audit (`scripts/variation_audit/`)

Cross-references WXYC library catalog artist name variations against local Discogs and MusicBrainz datasets. Classifies each relationship (ALIAS, MEMBER_OF_GROUP, SEPARATE_ARTIST, COLLABORATION, SPELLING_VARIANT, SPLIT_RELEASE) and identifies artists that should have their own library code. Output includes flowsheet play counts and own-release counts for prioritization.

**Pre-step** (extract Discogs artist CSVs, ~7 seconds):
```bash
mkdir -p /tmp/discogs_artists
ln -sf /path/to/discogs_artists.xml /tmp/discogs_artists/artists.xml
discogs-xml-converter /tmp/discogs_artists/ --output-dir /tmp/discogs_artists/
```

**Usage:**
```bash
uv run python -m scripts.variation_audit \
  --library-db library.db \
  --graph-db ../semantic-index/data/wxyc_artist_graph.db \
  --sql-dump ../tubafrenzy/wxycmusic-full-2026-03-28.sql \
  --discogs-csv-dir /tmp/discogs_artists/ \
  --mb-alias-tsv ../musicbrainz-cache/data/mbdump/artist_alias \
  --mb-artist-tsv ../musicbrainz-cache/data/mbdump/artist \
  --output-dir ../docs/variation-audit/
```

Discogs CSVs and MusicBrainz TSVs are optional; the script gracefully degrades using only the semantic-index entity resolution and member-of data when external files are missing.

## Relationship to Other Repos

- **[request-o-matic](https://github.com/WXYC/request-o-matic)** -- The caller. Parses messages, calls this service, posts to Slack.
- **[wxyc-shared](https://github.com/WXYC/wxyc-shared)** -- Shared API contract (`api.yaml`). Defines `LookupRequest`, `LookupResponse`, and related schemas. Python models are generated via `scripts/generate_api_models.sh` and committed to `generated/api_models.py`. The generated models are used as API boundary types; internal domain models (`LibraryItem`, `DiscogsSearchResult`) are converted via `to_catalog_item()` / `to_match_result()` methods.
- **[wxyc-etl](https://github.com/WXYC/wxyc-etl)** -- Shared Rust library with Python bindings (PyO3/maturin). Provides `wxyc_etl.text` (artist name normalization, diacritics stripping, compilation detection) and `wxyc_etl.schema` (library.db column definitions). These were previously duplicated locally in `core/matching.py`.
- **[discogs-cache](https://github.com/WXYC/discogs-etl)** -- ETL pipeline that populates the PostgreSQL Discogs cache consumed by `discogs/cache_service.py`. The pipeline is filtered by `library.db`, which discogs-cache generates from the WXYC MySQL catalog via `scripts/export_to_sqlite.py`. The `library.db` file serves dual purpose: runtime search for this service and primary input to the discogs-cache pipeline. The library ETL scripts (`export_to_sqlite.py`, `sync-library.sh`) live in discogs-cache.

## Example Music Data for Tests

WXYC is a freeform station. When creating test fixtures or mock data, use representative artists instead of mainstream acts like Queen, Radiohead, or The Beatles. The canonical data source is `wxyc-shared/src/test-utils/wxyc-example-data.json`.

Preferred defaults for fixtures:
- `LibraryItem`: `artist="Stereolab", title="Aluminum Tunes", genre="Rock"`
- `LOOKUP_BODY`: `{"artist": "Jessica Pratt", "album": "On Your Own Love Again", "raw_message": "Jessica Pratt - On Your Own Love Again"}`
- Other good choices: Juana Molina / "DOGA" (Sonamos), Cat Power / "Moon Pix" (Matador), Chuquimamani-Condori / "Edits" (self-released), Duke Ellington & John Coltrane / "Duke Ellington & John Coltrane" (Impulse Records), Sessa / "Pequena Vertigem de Amor" (Mexican Summer), Large Professor / "1st Class" (Matador Records)
