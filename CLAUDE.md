# Claude Code Instructions for Library Metadata Lookup

Library Metadata Lookup (LML) is a FastAPI service for WXYC radio that searches the library catalog and cross-references results with Discogs metadata. Extracted from [request-o-matic](https://github.com/WXYC/request-o-matic) to separate search/lookup concerns from message parsing and Slack posting.

## Topic guides

CLAUDE.md is a router for the always-loaded reference card. Topic depth lives in `docs/`:

- **[`docs/architecture.md`](docs/architecture.md)** — Lookup flow (7-step pipeline), `LookupRequest` opt-in flags (`extended`, `warm_cache`), search strategy pipeline + table, key files, optional Discogs 3-tier cache, fallthrough seam (per-method write-back policy, cool-down on outage), external-cache fallback for mojibake recovery, `Server-Timing` response header (`LML_EMIT_SERVER_TIMING`, BS#881)
- **[`docs/api-endpoints.md`](docs/api-endpoints.md)** — Non-lookup endpoints: identity resolution (`/identity/resolve`, `/identity/bulk`, `/api/v1/identity/bulk-resolve-libraries`, `/api/v1/identity/resolve` — release-identity mint/lookup; validation in `identity/release_validation.py`, schema in `entity/release_identity.sql`), `/api/v1/artists/resolve/bulk` (bulk bare-name artist resolution, verify-before-mint; LML#759), `/api/v1/artists/genres/bulk` (bulk artist-level Discogs genre/style aggregation from the discogs-cache with self-healing API fallback; LML#781), `/api/v1/cache/refresh-for-identities` (source-agnostic cache warmer; LML#525), `/api/v1/streaming-check`
- **[`docs/env-vars.md`](docs/env-vars.md)** — Full environment-variable reference (Discogs token, cache DB URLs, Spotify, Sentry, PostHog, search budget + hard timeout, cross-cache-identity feature flags, inbound auth surfaces)
- **[`docs/testing.md`](docs/testing.md)** — Unit/integration test patterns, bug-fix protocol, TDD rules, pytest markers (`pg`, `external_api`), CI marker sync
- **[`docs/deployment.md`](docs/deployment.md)** — Railway infrastructure (**Bucket-backed storage, no volume** — LML#834), branch strategy, CI/CD pipeline table, CI pin maintenance (Railway CLI, workflow `permissions:`, `@gha/v1` reusable refs), library.db / streaming_availability.db upload + download, the volume→bucket **cutover runbook** + **horizontal-scaling runbook**, `/health` semantics
- **[`docs/scripts.md`](docs/scripts.md)** — Streaming report stats regenerator, Discogs cache benchmark, API model generation, VA disambiguation, Bandcamp pipeline, resolver calibration, artist name variation audit, bulk artist-resolve drain (LML#759 PR D; drives prod `POST /api/v1/artists/resolve/bulk` over the BS#1614 name set), per-consumer API key seed/revoke/list (`scripts/api_keys/`; LML per-consumer API keys plan)
- **[`docs/observability-rowless-flag.md`](docs/observability-rowless-flag.md)** — `LML_RESOLVE_NONLIBRARY_RELEASE` degradation alerts (LML#683): the PostHog + Sentry alerts on the LML#681 `cache.*` counters, their thresholds + 3-day baseline rationale, and the kill-switch response runbook; also the LML#879 `discogs_rate_gate_fail_open` unsampled counter for the shared Discogs rate bucket and the LML#1049 `discogs_artist_breaker_shed` unsampled counter for artist-path saturation-breaker sheds

For the org-wide cache-hierarchy reference (LML's in-memory + PG cache tiers in context with upstream iOS and downstream Backend-Service tiers), see [`WXYC/wiki/architecture/cache-hierarchy.md`](https://github.com/WXYC/wiki/blob/main/architecture/cache-hierarchy.md).

Read the relevant topic doc before doing work in that area.

## Running locally

```bash
uvicorn main:app --reload
```

Branches: **`main`** → staging on push; **`prod`** → production on push.

## TDD (Required)

All code changes in this repo follow test-driven development. This is not optional.

1. **Red**: Write a failing test that describes the desired behavior. Run it and confirm it fails.
2. **Green**: Write the minimum implementation to make the test pass.
3. **Refactor**: Clean up the implementation while keeping tests green.

Bug fixes: write a test that reproduces the bug first, confirm it fails, then fix. Do not write implementation code without a corresponding failing test preceding it. Full protocol (unit + integration coverage, marker conventions) in [`docs/testing.md`](docs/testing.md).

## Code Style

- Line length: 100 chars
- Use `ruff format` for formatting, `ruff check` for linting
- Type hints encouraged
- Async/await for all I/O operations
- Pre-commit hook runs `ruff check` + `ruff format --check` on staged `.py` files. Activate with: `git config core.hooksPath .githooks`

## Relationship to Other Repos

- **[request-o-matic](https://github.com/WXYC/request-o-matic)** -- The caller. Parses messages, calls this service, posts to Slack.
- **[wxyc-shared](https://github.com/WXYC/wxyc-shared)** -- Shared API contract (`api.yaml`). Defines `LookupRequest`, `LookupResponse`, and related schemas. Python models are generated via `scripts/generate_api_models.sh` and committed to `generated/api_models.py`. The generated models are used as API boundary types; internal domain models (`LibraryItem`, `DiscogsSearchResult`) are converted via `to_catalog_item()` / `to_match_result()` methods.
- **[wxyc-etl](https://github.com/WXYC/wxyc-etl)** -- Shared Rust library with Python bindings (PyO3/maturin). Provides `wxyc_etl.text` (artist name normalization, diacritics stripping, compilation detection) and `wxyc_etl.schema` (library.db column definitions). These were previously duplicated locally in `core/matching.py`.
- **[discogs-cache](https://github.com/WXYC/discogs-etl)** -- ETL pipeline that populates the PostgreSQL Discogs cache consumed by `discogs/cache_service.py`. The pipeline is filtered by `library.db`, which discogs-cache generates from the WXYC MySQL catalog via `scripts/export_to_sqlite.py`. The `library.db` file serves dual purpose: runtime search for this service and primary input to the discogs-cache pipeline. The library ETL scripts (`export_to_sqlite.py`, `sync-library.sh`) live in discogs-cache.

## PostgreSQL schema ownership

The shared Discogs-cache PostgreSQL database has two schemas LML touches, with a hard ownership boundary ([discogs-etl#288](https://github.com/WXYC/discogs-etl/issues/288), Option 3):

- **`entity.*` is discogs-cache-owned** — the cross-service identity contract (`entity.identity`, `entity.release_identity`, and their reconciliation logs). It is created and migrated **only** via discogs-cache alembic migrations. LML reads/writes rows but must **not** `CREATE TABLE` in `entity.*`. Adding a new release-identity column still pays the three-repo dance (wxyc-shared enum → discogs-cache alembic → LML); see [WXYC/wiki#83](https://github.com/WXYC/wiki/issues/83).
- **`lml_cache.*` is LML-owned** — application caches with no external readers and no wire contract (e.g. `lml_cache.album_streaming_url_cache`). LML bootstraps these from the FastAPI lifespan in `main.py` (`CREATE SCHEMA IF NOT EXISTS lml_cache` + `CREATE TABLE IF NOT EXISTS`), with no discogs-cache coordination. discogs-cache tooling never touches `lml_cache.*` (its truncate guard at `scripts/import_csv.py` rejects schema-qualified names outright).

Rule of thumb: if another repo reads it, it belongs in `entity.*` and goes through alembic; if it's a private LML cache, it belongs in `lml_cache.*` and is lifespan-bootstrapped. (Completed via LML#573: the streaming-URL cache lives in `lml_cache.album_streaming_url_cache`; the grandfathered `entity.album_apple_music_lookup_cache` was dropped in #573's PR-2.)

## Example Music Data for Tests

WXYC is a freeform station. When creating test fixtures or mock data, use representative artists instead of mainstream acts like Queen, Radiohead, or The Beatles. The canonical data source is `wxyc-shared/src/test-utils/wxyc-example-data.json`.

Preferred defaults for fixtures:
- `LibraryItem`: `artist="Stereolab", title="Aluminum Tunes", genre="Rock"`
- `LOOKUP_BODY`: `{"artist": "Jessica Pratt", "album": "On Your Own Love Again", "raw_message": "Jessica Pratt - On Your Own Love Again"}`
- Other good choices: Juana Molina / "DOGA" (Sonamos), Cat Power / "Moon Pix" (Matador), Chuquimamani-Condori / "Edits" (self-released), Duke Ellington & John Coltrane / "Duke Ellington & John Coltrane" (Impulse Records), Sessa / "Pequena Vertigem de Amor" (Mexican Summer), Large Professor / "1st Class" (Matador Records)
