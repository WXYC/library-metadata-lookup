# Deployment

## Infrastructure

- Hosted on Railway. Each push to `main`/`prod` produces a single CI-gated deploy via `railway up`. Railway's native GitHub auto-deploy is disabled on both the staging and production `library-metadata-lookup` environments (LML#602), so the two-deploy race the [`commit_sha`](#commit_sha-deploy-identity) section describes no longer occurs
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

### `commit_sha` (deploy identity)

`GET /health` returns a `commit_sha` field identifying the deployed commit. It is resolved in priority order by `_resolve_commit_sha` in `routers/health.py`:

1. **A `COMMIT_SHA` file baked into the image.** A blank `COMMIT_SHA` placeholder is tracked at the repo root; CI overwrites it with `echo "${{ github.sha }}" > COMMIT_SHA` (the "Record deployed commit SHA" step) immediately before `railway up` in both `deploy-staging` and `deploy-production`. This is the authoritative source in prod and staging. For the file to reach the running image it must clear **two** filters, so it is deliberately listed in **neither**: `.gitignore` (`railway up` honors it and would drop the file from the upload tarball) and `.dockerignore` (the Dockerfile build — `railway.toml` `builder = "DOCKERFILE"`, `COPY . .` — honors it and would drop the file from the image). It ships blank, so a checkout (local dev / CI) reads empty content, which coerces to `null` per the tier rules below.
2. **`RAILWAY_GIT_COMMIT_SHA`** — Railway auto-injects this, but only on its *git-native* deploys (not on `railway up`). Used as a fallback.
3. **`null`** — local dev, CI, and tests (no file, no env var).

Empty or whitespace-only values at any tier coerce to `null`, so the "null when unset" contract holds and downstream equality checks are never fooled by `""`.

```json
{ "status": "healthy", "version": "0.1.0", "commit_sha": "abc123...", "services": { ... } }
```

#### Why a baked file rather than `RAILWAY_GIT_COMMIT_SHA` (LML#509)

Before LML#602, every push to `main`/`prod` produced **two** deployments: Railway's git-native trigger (carries the SHA) and the CI `railway up` source-deploy (no git metadata). The CI deploy landed ~3 min later — after the lint/typecheck/test/pg gates — and superseded the git-native one, so the deploy actually serving traffic had no `RAILWAY_GIT_COMMIT_SHA`. Reading only the env var left `commit_sha` permanently `null` in prod, which wedged WXYC/Backend-Service's `rotation-artist-backfill` cron (its deploy guard refused to run against an unidentifiable deploy). The baked file attaches deploy identity to the `railway up` deploy — the only deploy now, since LML#602 disabled the git-native trigger.

The two tiers were originally what made the deploy race safe either way: if the `railway up` deploy won, the baked **file** carried the SHA; if the git-native deploy won instead (it has no baked file — that step only runs in the CI job), it *does* carry `RAILWAY_GIT_COMMIT_SHA`, so the **env tier** carried it. With the git-native trigger disabled (LML#602) there is no longer a race — `commit_sha` always comes from the baked file, and the `RAILWAY_GIT_COMMIT_SHA` env tier is now inert. It is kept as a harmless fallback that would re-arm automatically if the GitHub source were ever re-connected.

> **Single CI-gated deploy (LML#602, done):** Railway's native GitHub auto-deploy is disabled on both the prod and staging `library-metadata-lookup` services (the GitHub source was disconnected from each environment), so each push now produces exactly one deploy — the CI-gated `railway up` one. `commit_sha` therefore comes solely from the baked **file** tier; the `RAILWAY_GIT_COMMIT_SHA` env tier is inert (harmless). This was a Railway dashboard/API change, not a code change, and is reversible — re-connecting the GitHub source restores the two-deploy behavior.

#### Cross-repo deploy gating

This is the canonical "is commit X deployed?" signal for cross-repo deploy gates. `settings.app_version` is hardcoded to `"0.1.0"` and is not bumped per deploy, so it cannot serve that role. Downstream callers that depend on a specific LML behavior (e.g. WXYC/Backend-Service rotation-scoped Discogs backfills that require LML#503's `fetched_at` stub-discriminator semantics) should poll `/health` and compare `commit_sha` against the merge SHA of the feature they need before scheduling the job — short-circuiting wasted API budget when staging happens to be running an older deploy.
