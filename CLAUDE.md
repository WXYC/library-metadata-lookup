# Claude Code Instructions for Library Metadata Lookup

## Project Overview

Library Metadata Lookup is a FastAPI service for WXYC radio that searches the library catalog and cross-references results with Discogs metadata. It was extracted from [request-o-matic](https://github.com/WXYC/request-o-matic) to separate search/lookup concerns from message parsing and Slack posting.

## Architecture

### Lookup Flow

1. **Artist Correction**: Fuzzy match artist against library catalog to fix typos
2. **Album Resolution**: If song provided without album, query Discogs for album names
3. **Search Pipeline**: Execute strategies in order until results are found (see below)
4. **Track Validation**: If fallback returned all artist albums, validate each against Discogs tracklists
5. **Artwork Fetch**: Fetch album art from Discogs for each result
6. **Context Message**: Generate context string for the caller

### Search Strategy Pipeline

Strategies are defined declaratively in `core/search.py` and executed in order:

| Strategy | Trigger | Implementation |
|---|---|---|
| `ARTIST_PLUS_ALBUM` | Has artist, album, or song | `search_library_with_fallback()` |
| `SWAPPED_INTERPRETATION` | No results + "X - Y" format | `search_with_alternative_interpretation()` |
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
- `core/search.py` -- Declarative search strategy pattern
- `core/matching.py` -- Stopwords, compilation detection, ambiguous format detection, diacritics normalization
- `core/dependencies.py` -- FastAPI DI for LibraryDB + DiscogsService

### Discogs Cache (Optional)

The service supports an optional PostgreSQL cache for Discogs data:

1. Query local PostgreSQL cache first
2. On cache miss, query Discogs API
3. Write API results back to cache
4. Gracefully degrade to API-only if cache unavailable

Set `DATABASE_URL_DISCOGS` to enable. The cache schema is defined in [WXYC/discogs-cache](https://github.com/WXYC/discogs-cache).

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
- `SENTRY_DSN` -- Sentry error tracking
- `POSTHOG_API_KEY` -- PostHog telemetry
- `LIBRARY_DB_PATH` -- Path to SQLite database (default: `library.db`)
- `ADMIN_TOKEN` -- Bearer token for admin endpoints (upload endpoint)

## Code Style

- Line length: 100 chars
- Use `black` for formatting, `ruff` for linting
- Type hints encouraged
- Async/await for all I/O operations

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
| Unit Tests | All pushes + PRs | -- |
| Deploy to Staging | Push to `main` | lint, typecheck, test |
| Smoke Test (Staging) | Push to `main` | deploy-staging |
| Integration Tests | Push to `main` | smoke-test-staging |
| Deploy to Production | Push to `prod` | lint, typecheck, test |
| Smoke Test (Production) | Push to `prod` | deploy-production |

### Library Database Upload

The `library.db` file lives on a Railway volume, not in git. It's uploaded via:

```
POST /admin/upload-library-db
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: multipart/form-data
```

The upload endpoint validates the SQLite file, closes the current DB connection,
atomically replaces the file, and returns `{"status": "ok", "row_count": <int>}`.

The ETL script in [discogs-cache](https://github.com/WXYC/discogs-cache) (`scripts/sync-library.sh`) handles daily uploads to both staging and production.

### Health Check Behavior

When `library.db` is missing (e.g., on first deploy before first upload):
- `get_library_db()` returns a LibraryDB instance with `is_available() = False`
- Health endpoint returns `{"status": "unhealthy", "services": {"database": "error"}}` (503)
- Service is functional for non-database endpoints
- After uploading library.db, next request triggers reconnection

## Scripts

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

## Relationship to Other Repos

- **[request-o-matic](https://github.com/WXYC/request-o-matic)** -- The caller. Parses messages, calls this service, posts to Slack.
- **[wxyc-shared](https://github.com/WXYC/wxyc-shared)** -- Shared API contract (`api.yaml`). Defines `LookupRequest`, `LookupResponse`, and related schemas. Python models are generated via `scripts/generate_api_models.sh` and committed to `generated/api_models.py`. The generated models are used as API boundary types; internal domain models (`LibraryItem`, `DiscogsSearchResult`) are converted via `to_catalog_item()` / `to_match_result()` methods.
- **[discogs-cache](https://github.com/WXYC/discogs-cache)** -- ETL pipeline that populates the PostgreSQL Discogs cache consumed by `discogs/cache_service.py`. The pipeline is filtered by `library.db`, which discogs-cache generates from the WXYC MySQL catalog via `scripts/export_to_sqlite.py`. The `library.db` file serves dual purpose: runtime search for this service and primary input to the discogs-cache pipeline. The library ETL scripts (`export_to_sqlite.py`, `sync-library.sh`) live in discogs-cache.

## Example Music Data for Tests

WXYC is a freeform station. When creating test fixtures or mock data, use representative artists instead of mainstream acts like Queen, Radiohead, or The Beatles. The canonical data source is `wxyc-shared/src/test-utils/wxyc-example-data.json`.

Preferred defaults for fixtures:
- `LibraryItem`: `artist="Stereolab", title="Aluminum Tunes", genre="Rock"`
- `LOOKUP_BODY`: `{"artist": "Jessica Pratt", "album": "On Your Own Love Again", "raw_message": "Jessica Pratt - On Your Own Love Again"}`
- Other good choices: Juana Molina / "DOGA" (Sonamos), Cat Power / "Moon Pix" (Matador), Chuquimamani-Condori / "Edits" (self-released), Duke Ellington & John Coltrane / "Duke Ellington & John Coltrane" (Impulse Records), Sessa / "Pequena Vertigem de Amor" (Mexican Summer), Large Professor / "1st Class" (Matador Records)
