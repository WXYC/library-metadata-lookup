# Deployment

## Infrastructure

- Hosted on Railway with CI-driven deploys (automatic deploys disabled)
- Railway volume mounted at `/data` stores `library.db` persistently across deploys
- Optional PostgreSQL cache for Discogs data via `DATABASE_URL_DISCOGS` (gracefully degrades to API-only)
- `LIBRARY_DB_PATH=/data/library.db` on Railway

## Branch Strategy

- **`main`** -- CI deploys to **staging** after lint + typecheck + unit tests pass
- **`prod`** -- CI deploys to **production** after lint + typecheck + unit tests pass
- Both environments get smoke tests after deploy

## CI/CD Pipeline (`.github/workflows/ci.yml`)

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

See [`testing.md`](testing.md#pytest-markers-architecture-a) for what each test job runs.

## CI pin maintenance

Three classes of pin in `.github/workflows/*.yml` exist for supply-chain reasons (mirrors WXYC/request-o-matic#124's free-tier hardening; see WXYC/wiki#67 for the org-wide rollout). They will bit-rot and need occasional bumps:

- **`@railway/cli@<version>`** in the `Install Railway CLI` step of `ci.yml` (`deploy-staging`, `deploy-production`) and `set-railway-var.yml`. Three lines total. Failure mode is loud (deploy step fails with a CLI error). Bump by checking `npm view @railway/cli version` and updating all three lines together; mismatched pins across these workflows would mean staging, production, and ops use different CLIs against the same Railway project. Railway ships fast (~40 versions in 60 days as of 2026-05); pin "current" rather than chasing every release. Last bump: 66d2eb4 (2026-05-13, pinned to 4.58.0).
- **Workflow-level `permissions:`** scoped to the minimum each workflow needs:
  - `ci.yml`, `cross-cache-identity-flags.yml`, `set-railway-var.yml`: `contents: read` (no GITHUB_TOKEN writes).
  - `charset-corpus-drift.yml`: `contents: read` plus `packages: read` (the reusable workflow pulls `@wxyc/shared` from `npm.pkg.github.com`).
  - `refresh-streaming.yml`: `contents: write` (creates / uploads to `streaming-data-v1` GitHub Release via `GH_TOKEN`).
  Failure mode is silent — a job that needs a missing scope (e.g. `pull-requests: write`) fails its API call but the workflow stays green. When adding a step that needs to comment on PRs, push tags, mint releases, etc., explicitly grant the scope at the job level (or widen the workflow-level floor only if every job in the file needs it).
- **Reusable-workflow refs pinned to `@gha/v1`**, not `@main` — `WXYC/wxyc-etl/.github/workflows/check-ci-marker-sync.yml@gha/v1` (in `ci.yml`) and `WXYC/wxyc-shared/.github/workflows/check-charset-corpus-drift.yml@gha/v1` (in `charset-corpus-drift.yml`). The publishing repos treat `gha/v1` as a moving major tag — re-pointed forward on non-breaking changes, frozen on breaking changes (which get a fresh `gha/v2`). Don't downgrade either to `@main`; if a `gha/v2` migration arrives, follow the procedure at the top of the publishing repo's CLAUDE.md.

Run `actionlint .github/workflows/*.yml` locally before pushing workflow changes; it validates `permissions:` syntax, action-version pins, and shell-script blocks (via shellcheck), and catches the silent-mistake class of errors above before CI does.

## Library Database Upload

The `library.db` file lives on a Railway volume, not in git. It's uploaded via:

```
POST /admin/upload-library-db
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: multipart/form-data
```

The upload endpoint validates the SQLite file, closes the current DB connection,
atomically replaces the file, and returns `{"status": "ok", "row_count": <int>}`.

The ETL script in [discogs-cache](https://github.com/WXYC/discogs-etl) (`scripts/sync-library.sh`) handles daily uploads to both staging and production.

## Streaming Database Backup (Upload + Download)

`streaming_availability.db` is the analysis database for streaming-availability search results. It's a sibling of `library.db` on the Railway volume. Two symmetric admin endpoints, both gated by `ADMIN_TOKEN`:

```
POST /admin/upload-streaming-db    # multipart upload, validates `albums` table
GET  /admin/download-streaming-db  # FileResponse stream of the volume copy (404 if missing)
```

The download endpoint lets the daily library-sync pipeline (WXYC/discogs-etl) read the file directly from the Railway volume instead of round-tripping it through a GitHub Release, making the volume the canonical source.

## Health Check Behavior

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
