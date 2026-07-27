# Deployment

## Infrastructure

- Hosted on Railway. Each push to `main`/`prod` produces a single CI-gated deploy via `railway up`. Railway's native GitHub auto-deploy is disabled on both the staging and production `library-metadata-lookup` environments (LML#602), so the two-deploy race the [`commit_sha`](#commit_sha-deploy-identity) section describes no longer occurs
- Storage is a per-environment **Railway Bucket** (S3-compatible object store), not a Railway volume: `library.db` and `streaming_availability.db` live in the bucket, and the FastAPI lifespan fetches `library.db` to `LIBRARY_DB_PATH` on boot before serving. This is the zero-downtime, replica-ready posture from the volume-eviction epic (LML#834); it superseded the `/data` volume in the 2026-07 cutover (see the [cutover runbook](#cutover-runbook-data-volume--railway-bucket))
- Optional PostgreSQL cache for Discogs data via `DATABASE_URL_DISCOGS` (gracefully degrades to API-only)
- `LIBRARY_DB_PATH=/data/library.db` on Railway — the boot-fetch destination; `/data` is now the Dockerfile-created ephemeral image dir, not a mounted volume

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

Four classes of pin in `.github/workflows/*.yml` exist for supply-chain reasons (mirrors WXYC/request-o-matic#124's free-tier hardening; see WXYC/wiki#67 for the org-wide rollout). They will bit-rot and need occasional bumps:

- **`@railway/cli@<version>`** in the `Install Railway CLI` step of `ci.yml` (`deploy-staging`, `deploy-production`) and `set-railway-var.yml`. Three lines total. Failure mode is loud (deploy step fails with a CLI error). Bump by checking `npm view @railway/cli version` and updating all three lines together; mismatched pins across these workflows would mean staging, production, and ops use different CLIs against the same Railway project. Railway ships fast (~40 versions in 60 days as of 2026-05); pin "current" rather than chasing every release. Last bump: 66d2eb4 (2026-05-13, pinned to 4.58.0).
- **Workflow-level `permissions:`** scoped to the minimum each workflow needs:
  - `ci.yml`, `cross-cache-identity-flags.yml`, `set-railway-var.yml`: `contents: read` (no GITHUB_TOKEN writes).
  - `charset-corpus-drift.yml`: `contents: read` plus `packages: read` (the reusable workflow pulls `@wxyc/shared` from `npm.pkg.github.com`).
  - `refresh-streaming.yml`: `contents: read` (its only release interaction is downloading `library.db` via `GH_TOKEN` — a read). The canonical `streaming_availability.db` round-trip goes to the Railway **Bucket** through the admin upload/download endpoints via `ADMIN_TOKEN` + `PRODUCTION_URL` (see [Streaming Database Backup](#streaming-database-backup-upload--download)), not GITHUB_TOKEN.
  Failure mode is silent — a job that needs a missing scope (e.g. `pull-requests: write`) fails its API call but the workflow stays green. When adding a step that needs to comment on PRs, push tags, mint releases, etc., explicitly grant the scope at the job level (or widen the workflow-level floor only if every job in the file needs it).
- **Reusable-workflow refs pinned to `@gha/v1`**, not `@main` — `WXYC/wxyc-etl/.github/workflows/check-ci-marker-sync.yml@gha/v1` (in `ci.yml`) and `WXYC/wxyc-shared/.github/workflows/check-charset-corpus-drift.yml@gha/v1` (in `charset-corpus-drift.yml`). The publishing repos treat `gha/v1` as a moving major tag — re-pointed forward on non-breaking changes, frozen on breaking changes (which get a fresh `gha/v2`). Don't downgrade either to `@main`; if a `gha/v2` migration arrives, follow the procedure at the top of the publishing repo's CLAUDE.md.
- **`ACTIONLINT_VERSION`** in `actionlint.yml` (the `Workflow Lint` workflow). rhysd/actionlint publishes no root `action.yml`, so the job downloads a pinned binary via the official `download-actionlint.bash` script — the env var feeds both the script's release-tag ref and the binary version it fetches, keeping them in lockstep. Failure mode is loud (the lint step fails). Bump by checking the latest release at https://github.com/rhysd/actionlint/releases and updating the single `ACTIONLINT_VERSION` value. Last pin: 1.7.12.

The `Workflow Lint` workflow (job `actionlint` in `actionlint.yml`) runs `actionlint` on every PR (and `main`/`prod` push) that touches `.github/workflows/**`, failing on lint errors. It validates `permissions:` syntax, `uses:` reference and `${{ }}` expression/context syntax, and shell-script blocks in `run:` steps (via shellcheck), catching the silent-mistake class of errors above in CI. It is advisory, not a required status check — if you ever mark it required in branch protection, pair it with an always-runs companion job, because a `paths`-filtered check that gets skipped never reports its status and would block every PR that doesn't touch a workflow file. Run `actionlint .github/workflows/*.yml` locally before pushing to get the same signal without burning a CI cycle.

## Library Database Upload

The `library.db` file lives in the Railway Bucket, not in git — and is fetched to `LIBRARY_DB_PATH` on boot (see [Infrastructure](#infrastructure)). It's uploaded via:

```
POST /admin/upload-library-db
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: multipart/form-data
```

The upload endpoint validates the SQLite file, **writes it to the bucket first** (so a
store failure aborts before any local mutation), then closes the current DB connection,
atomically replaces the local `LIBRARY_DB_PATH` copy, and returns
`{"status": "ok", "row_count": <int>}`.

The ETL script in [discogs-cache](https://github.com/WXYC/discogs-etl) (`scripts/sync-library.sh`) handles daily uploads to both staging and production.

## Streaming Database Backup (Upload + Download)

`streaming_availability.db` is the analysis database for streaming-availability search results — it holds Apple/Spotify/Deezer URLs, track-level results, and Discogs match state. It lives in the Railway Bucket alongside `library.db`. Two symmetric admin endpoints, both gated by `ADMIN_TOKEN`:

```
POST /admin/upload-streaming-db    # multipart upload, validates `albums` table + coverage guard
GET  /admin/download-streaming-db  # streams the bucket object (404 if missing)
```

### The bucket is the single canonical lineage (LML#672)

Before LML#672 this file was maintained as **two copies that drifted**: a `streaming-data-v1` GitHub Release asset (written weekly by `refresh-streaming.yml`, CI-only creds so Spotify/Deezer-only) and this working copy (the rich DB with Apple + `track_streaming`, uploaded manually — then on the Railway volume, now the bucket object). The daily library-sync read the *release*, so production `library.db` silently carried **zero Apple Music links** while hundreds sat unused in the canonical copy.

The bucket is now the enforced single source (the Railway volume held this role from #672 until the #834 eviction; the round-trip invariant is identical). Every writer round-trips it (download → modify → upload):

- `refresh-streaming.yml` (LML, weekly): downloads the bucket copy, runs the Spotify/Deezer incremental, uploads it back. (`library.db`, the input catalog, still comes from the release.)
- The occasional manual Apple + `track_streaming` run: same download → enrich → upload round-trip, so it never clobbers the weekly incremental and vice versa.
- `sync-library.yml` (WXYC/discogs-etl, daily): reads the bucket copy via `GET /admin/download-streaming-db` to enrich `library.db`.

The `streaming_availability.db` release asset has been **retired** (LML#672 cutover): nothing writes or reads it anymore. The `streaming-data-v1` release still hosts **`library.db`** (written by discogs-etl `sync-library.yml`, read by `refresh-streaming.yml` as the input catalog).

### Coverage-regression guard on upload

`POST /admin/upload-streaming-db` is a full-file replace, so a thin copy could overwrite a rich one (this is how the 288 Apple URLs → 0 incident happened). The upload is guarded: it computes coverage for five metrics — `COUNT(apple_url)`, `COUNT(spotify_url)`, `COUNT(deezer_url)` and `COUNT(*)` over `albums`, plus a count of **usable** `track_results` rows (resolved, with a non-null URL — mirroring the predicate `export_streaming_links.py` applies, so a copy that keeps the rows but nulls every track URL is still caught) — comparing the uploaded file against **the copy currently on disk at replace time** (not an uploader baseline). The upload is rejected with **HTTP 409** if any metric drops below `prior × (1 − 0.05)` **or** goes non-zero → zero. If the on-disk file exists but can't be read (corrupt/locked), the upload also fails **closed** with 409 rather than treating the missing baseline as a first upload. Pass `?force=true` to override an intentional shrink or replace an unreadable baseline (logged loudly). Comparing against live on-disk state means a stale-content writer that lost Apple is rejected on the Apple regression and re-applies on its next cycle — this is eventual rejection across sequential cycles, not a lock across concurrent uploads (the writers here — a weekly cron and a rare manual run — are effectively non-concurrent).

### Railway-uptime failure mode

Making the bucket canonical couples the daily prod sync to LML/Railway being reachable at sync time (vs. the durable GitHub-hosted release). This is deliberate and the failure mode is safe: discogs-etl's `GET /admin/download-streaming-db` step **hard-fails** on a non-200 or empty/invalid file, so a Railway outage at sync time **aborts** the sync and production keeps **yesterday's** `library.db` (which still has its streaming links) rather than publishing a zero-link db. A flaked download never strips links.

Operational prerequisite: `refresh-streaming.yml` needs the `ADMIN_TOKEN` and `PRODUCTION_URL` repo secrets set on **WXYC/library-metadata-lookup** (they previously lived only on the discogs-etl side).

## Cutover runbook: `/data` volume → Railway Bucket

The seed-then-hard-cutover procedure for moving both SQLite artifacts off the Railway volume and into a per-environment Railway Bucket (the volume-eviction epic, WXYC/library-metadata-lookup#834). Run it **once per environment, staging first**; the staging run is the rehearsal for prod. There is **no dual-path transition code and no organic-fill window** — that was the #672 two-lineages anti-pattern this epic exists to avoid. The bucket-mode code (the boot-fetch + store-backed endpoints) is selected purely by whether `LML_BUCKET_NAME` + `LML_BUCKET_ENDPOINT` are set (see [env-vars.md](env-vars.md)), so a cutover is entirely a data-seed + variable-reference change plus a volume detach — no code deploy is part of the cutover itself.

> **Status — executed.** Staging cutover 2026-07-18 (LML#838), production cutover 2026-07-20 (LML#839). Both environments now run bucket-only; the shared `/data` volume was soft-deleted 2026-07-20 (48h grace before hard-purge). The procedure below is retained as the reusable template for any future volume→bucket move (e.g. standing up a new environment).

### Prerequisites

- The bucket-mode code is already deployed to the target environment (PR 3, WXYC/library-metadata-lookup#837, on that environment's branch — `main`→staging, `prod`→production).
- Railway CLI authenticated against the `request-o-matic` project, and an S3-protocol client (`aws` CLI or a `boto3` snippet) for the out-of-band seed.
- **A quiet window.** Avoid the daily `sync-library.sh` window (discogs-etl, uploads `library.db`) and the weekly `refresh-streaming.yml` window, so a failed verify can be rolled back without racing a writer. Cutting over just **after** a daily sync completes also guarantees the volume `library.db` and the `streaming-data-v1` release copy are freshly identical, which the seed relies on.

### Procedure (per environment)

1. **Snapshot both files and retain them** (never-delete-collected-data rule). The two files have different provenance, and only one has a download endpoint:
   - `streaming_availability.db` — **precious**: volume-canonical since #672, the single copy of expensive rate-limited Apple/Spotify/Deezer results. Snapshot it via `GET /admin/download-streaming-db` (Bearer `ADMIN_TOKEN`) and keep the file. This is the byte-source for the seed.
   - `library.db` — **reproducible**: read-only at runtime, regenerated daily by discogs-etl and published to the `streaming-data-v1` GitHub release. **There is no `GET /admin/download-library-db` endpoint** — use the current release asset (or a fresh `sync-library.sh` output) as the seed byte-source. Because the next daily sync overwrites the bucket object via `POST /admin/upload-library-db` anyway, the seed only needs to be a valid recent catalog so boot-fetch succeeds and `/health` goes green before the volume is removed.
2. **Create the bucket and seed it out-of-band.** Create a Railway Bucket in the target environment. Note its variable panel — the exact preset names must be read off the actual bucket at execution time, but the documented contract (see [env-vars.md](env-vars.md)) is `BUCKET`, `ENDPOINT`, and the boto3-native `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. Put both objects at their **bare-filename keys** (the keys the app reads/writes — `LIBRARY_DB_FILENAME` / `STREAMING_DB_FILENAME` in `routers/admin.py`), matching the seed client's addressing style to the bucket's: **virtual-hosted** for buckets created after Railway's mid-2026 change (the default — `LML_BUCKET_ADDRESSING_STYLE=virtual`, what both WXYC environments use), **path-style** only for legacy buckets (`LML_BUCKET_ADDRESSING_STYLE=path`, as the bucket's Credentials tab states). `S3ObjectStore`'s default has been virtual since LML#856:

   ```sh
   export AWS_ACCESS_KEY_ID=…  AWS_SECRET_ACCESS_KEY=…        # from the bucket's variable panel
   ENDPOINT=…  BUCKET=…                                        # the bucket's ENDPOINT / BUCKET
   aws --endpoint-url "$ENDPOINT" s3api put-object --bucket "$BUCKET" --key library.db                 --body ./library.db
   aws --endpoint-url "$ENDPOINT" s3api put-object --bucket "$BUCKET" --key streaming_availability.db  --body ./streaming_availability.db
   ```

   Then **verify the seed against the snapshot** — compare object size (and, for `streaming_availability.db`, checksum) via `s3api head-object` against the retained local copy before trusting it. (`aws-cli` defaults to `auto`, which resolves a virtual-hosted bucket — the default — correctly; a **legacy path-style** bucket instead needs `aws configure set default.s3.addressing_style path` or a path-configured client. Best practice, used in both WXYC cutovers: seed with the app's own `S3ObjectStore` config so the addressing matches exactly what the running service will use — a mismatch then fails on your laptop, before any service flip.)
3. **Enable bucket mode** on the LML service: set the two variable references `LML_BUCKET_NAME`→ bucket `BUCKET`, `LML_BUCKET_ENDPOINT`→ bucket `ENDPOINT`, plus the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` presets. Leave `LIBRARY_DB_PATH=/data/library.db` **unchanged** — after the volume is gone, `/data` is still the Dockerfile-created, `appuser`-owned writable dir the boot-fetch copies the fetched bytes into. Deploy (`railway redeploy`, or a no-op push) so the process restarts and the lifespan boot-fetch runs.
4. **Verify** (bucket mode is live, volume still attached — the safe overlap point):
   - `/health` is green (`database: ok`) — this **is** the boot-fetch success signal; a red `database` means the fetch failed and the service is serving degraded.
   - `GET /admin/download-streaming-db` round-trips **byte-identical** to the retained snapshot.
   - A live `/api/v1/lookup` returns a real library hit (proves the boot-fetched `library.db` is being served).
   - **Coverage guard is live**: a force-less **thin** `streaming_availability.db` upload is rejected **409** (don't actually replace the good file — upload a deliberately-smaller db and confirm the 409, or trust the #836 guard tests plus a `--dry-run` style check).
5. **Remove the volume.** Delete it (`railway volume … delete`) — a **soft-delete with a ~48h grace window** (`deletedAt` = delete time + 48h; the data stays recoverable until the hard-purge). Three things learned in the WXYC cutover, each of which will bite otherwise:
   - **A shared volume deletes globally.** The WXYC `/data` volume was a *single* volume id mounted in both staging and prod, so one `delete` flipped it `isPendingDeletion` in **every** environment at once. Check `railway volume -e <env> … list --json` across environments before assuming the delete is scoped to one.
   - **Delete ≠ unmount.** A delete removes the volume from the service config, but the **running container keeps its mount** until it's replaced. You must **`railway redeploy`** each environment so it comes up on the Dockerfile-created `/data` dir. Do this **before** the hard-purge, or the volume is yanked out from under an open SQLite fd on a live container.
   - **`RAILWAY_VOLUME_*` vars are stale echo.** After the redeploy they still appear in `railway variables` — that is not a live mount. Verify decoupling with `/proc/mounts` (no `/data` line via `railway ssh -e <env> … 'grep " /data " /proc/mounts'`), not the vars.

   With no volume attached, deploys become zero-downtime.
6. **Observe a zero-downtime deploy**: run a request loop (`while true; do curl -s .../health; sleep 0.5; done`, or probe `GET /` for a fast dependency-free 404) through one deploy window and confirm no blip — the old deployment keeps serving until the new one passes its `/health` gate (which now requires a successful boot-fetch).

### Rollback

Bucket mode is a pure configuration state. To revert before the volume is removed: **unset** `LML_BUCKET_NAME` / `LML_BUCKET_ENDPOINT` (storage falls back to local-directory mode rooted at `LIBRARY_DB_PATH`'s parent — the pre-eviction volume layout) and redeploy. If the volume was already removed, re-add the volume mount at `/data` first, then unset the vars. The retained snapshots are the recovery source if a seed was corrupted.

> **⚠️ Re-mounting a volume needs the ownership fix back.** This cutover deleted the runtime `chown -R appuser:appuser /data` from `entrypoint.sh` (it was dead weight with no volume mounted). Railway mounts volumes **root-owned**, and that mount masks the Dockerfile's build-time `chown`, so a freshly re-added `/data` volume leaves `appuser` unable to *write* — the failure is silent-ish: reads of the existing `library.db` still succeed (world-readable), so `/health` can look green while every write path (the daily `POST /admin/upload-library-db`, streaming enrichment) fails with `EACCES`. When rolling back onto a re-added volume, first restore ownership — either fix it out-of-band (`railway ssh -e <env> … 'chown -R appuser:appuser /data'`, which runs as root) or temporarily re-add the `chown -R appuser:appuser /data 2>/dev/null || true` line to `entrypoint.sh` and redeploy.

### Soak (staging → prod gate)

After the **staging** cutover, leave staging in bucket mode through **at least one daily `sync-library.sh` run** (proves the `POST /admin/upload-library-db` bucket write + local hot-swap end-to-end) and **ideally one weekly `refresh-streaming.yml`** (proves the streaming round-trip against the store) before starting the **prod** cutover (WXYC/library-metadata-lookup#839). Prod's extra acceptance bar: the next daily library sync and next weekly streaming refresh both green **with zero changes in discogs-etl or the workflow repos**, and discogs-etl's daily `GET /admin/download-streaming-db` read green.

## Horizontal-scaling runbook: enabling N≥2 replicas

Removing the volume makes replicas *architecturally* possible (Railway won't attach a volume to replicas), but **enabling N≥2 is deliberately out of scope for the volume-eviction epic** and gated behind the flip criteria below. The blocker is the per-process Discogs limiter/semaphore/breaker (`discogs/ratelimit.py`): N replicas each running the stock limiter would drive N×50/min against the shared 60/min Discogs token. Scaling is therefore pure env-var arithmetic (divide the shared-budget knobs by N) plus awareness of the per-replica state that stops being process-global.

> **LML#841 lifts the *rate* dimension out of that arithmetic.** With `DISCOGS_RATE_BUCKET_ENABLED=true`, the per-minute egress permit is drawn from a **shared PG token bucket** (one `lml_cache.discogs_rate_bucket` row), so all N replicas meter the 50/min rate against a single row — **leave `DISCOGS_RATE_LIMIT` at its stock 50 across replicas** instead of dividing. Only the rate knob comes off the divide list; the **concurrency** semaphore (`DISCOGS_MAX_CONCURRENT`) and the breaker floor stay per-process and still divide/raise, because concurrency and shed policy are local decisions the bucket doesn't touch. See the `DISCOGS_RATE_BUCKET_*` entries in [`env-vars.md`](env-vars.md).

> **Fail-open trade-off (read before leaving the rate at 50 under N≥2).** The gate fails **open to the local `AsyncLimiter`** on any discogs-cache PG error. At N=1 the sole process's local limiter is kept drained by the local-first acquire, so its fail-open pace ≈ the real budget. But a replica whose share of the global budget is below its local cap under-utilizes its local limiter, which then drifts toward full; if PG dies, **all N replicas fail open to their full local limiters — ~N×`DISCOGS_RATE_LIMIT`/min during the outage** (the pre-#841 undivided behavior, relocated to the PG-down window). That is *no worse than running the bucket OFF*, but it does mean **leaving the rate at 50 removes the divide-by-N net specifically for the fail-open window**. If you want a bounded fail-open, keep `DISCOGS_RATE_LIMIT` divided to `floor(50/N)` even with the bucket ON: the healthy path is still metered globally by PG (so the division costs only a little burst headroom when some replicas are idle), and a PG outage then degrades to ~50/min cluster-wide instead of N×50. The #755 breaker is the backstop either way.

### Flip criteria — all three must hold

1. **WXYC/discogs-etl#313 landed** (discogs-cache PG `shared_buffers` tuning) **and the #706 cold-tail re-measured healthy.** Replicas double/triple connection load onto that PG; flipping before its buffers are tuned re-creates the #706 tail.
2. **WXYC/Backend-Service#1591 landed, or the backfill floods are otherwise controlled.** Replicas add availability, not flood immunity — an uncontrolled flood saturates every replica.
3. **Observed CPU-bound latency under *organic* load, or an explicit availability requirement.** Absent a real signal, N=1 is simpler and cheaper. (Note the backfill flood is not organic load — see the caveat in criterion 2.)

### Knob inventory

Worst-case Discogs egress and PG demand are **cluster-wide sums** across replicas, so the shared-budget knobs divide by N. The per-replica compute gates do not (each replica has its own event loop).

| Knob | At N≥2 | Why |
|---|---|---|
| `DISCOGS_RATE_LIMIT` | **divide** → `floor(50/N)` — **unless `DISCOGS_RATE_BUCKET_ENABLED=true`, then leave at 50** (but see the fail-open trade-off above — dividing anyway bounds the PG-outage window) | Shared 60/min Discogs token. Per-process limiters must sum under 60 — *but* the LML#841 shared PG token bucket meters the 50/min against one row cluster-wide, so the static division is only needed when the bucket is OFF *or* as a bounded fail-open floor. |
| `DISCOGS_MAX_CONCURRENT` | **divide** → `floor(5/N)` (floor 1) | Per-process egress semaphore; cluster concurrent egress is the sum. |
| `DISCOGS_BREAKER_REMAINING_FLOOR` | **raise** by ~`N × per-replica DISCOGS_MAX_CONCURRENT` | The floor is read off the **shared** `X-Discogs-Ratelimit-Remaining`; up to `N × per-replica concurrent` requests can be in flight cluster-wide when any one replica first observes the floor, so raise it to absorb that overshoot. |
| `LML_DISCOGS_POOL_MAX_SIZE` | **shrink** (pre-#313) so `N × pool` stays under discogs-cache PG `max_connections` / memory budget | Shared PG connection budget (the LML#241/#357 FD-exhaustion lesson at cluster scale). Relax once #313 tunes the PG. |
| `LML_LOOKUP_MAX_CONCURRENT` | **unchanged** | Per-replica in-flight lookup cap — one replica's event loop. **But** it defaults to `min(8, LML_DISCOGS_POOL_MAX_SIZE)`, so shrinking the pool lowers its effective value; re-check after the pool shrink. |
| `LML_BULK_GLOBAL_MAX_CONCURRENT`, `LML_BULK_MAX_CONCURRENT`, `LML_STREAMING_WARM_CONCURRENCY` | **unchanged** per replica | Per-replica compute gates. `LML_BULK_GLOBAL_MAX_CONCURRENT` also defaults to `LML_DISCOGS_POOL_MAX_SIZE`, so a pool shrink pulls it down with it — size deliberately if you rely on the default. |

### Worked example — N=2 (stock defaults today)

| Knob | N=1 (today) | N=2 |
|---|---|---|
| `DISCOGS_RATE_LIMIT` | 50 | 25 (bucket OFF) / **50** (`DISCOGS_RATE_BUCKET_ENABLED=true`) |
| `DISCOGS_MAX_CONCURRENT` | 5 | 2 |
| `DISCOGS_BREAKER_REMAINING_FLOOR` | 3 | ~7 (`3 + 2×2`) |
| `LML_DISCOGS_POOL_MAX_SIZE` | 5 | shrink so `2×pool` ≤ PG budget (pre-#313); revisit post-#313 |

### Known permanent N≥2 degradations

These are inherent to per-process state going per-replica — documented, accepted, not bugs:

- **Split L1 TTL caches** — each replica has its own in-memory cache, so hit rate drops and Discogs call volume rises versus N=1.
- **Split single-flight dedup** — the #537 dedup becomes per-replica, so two replicas can do the same live probe concurrently.
- **N half-open breaker trials** — each replica's breaker independently admits one trial during recovery, so up to N trials hit the shared token before any one closes.
- **`library.db` freshness skew** — the daily `POST /admin/upload-library-db` hot-swaps only the **replica that received the request**; the others keep serving yesterday's catalog until they restart. A replica on a stale catalog is *degraded* (misses new arrivals), not broken — mitigated by the redeploy step below.

### Flip procedure

1. Set the divided/raised env vars from the knob table (`DISCOGS_RATE_LIMIT`, `DISCOGS_MAX_CONCURRENT`, `DISCOGS_BREAKER_REMAINING_FLOOR`, and pre-#313 `LML_DISCOGS_POOL_MAX_SIZE`). If `DISCOGS_RATE_BUCKET_ENABLED=true`, **skip the `DISCOGS_RATE_LIMIT` division** — the shared PG bucket already meters the rate cluster-wide; keep it at 50.
2. Bump the replica count on the LML service.
3. Add a `railway redeploy` step **after the daily library sync** so every replica boot-fetches the fresh `library.db` (closes the freshness-skew window above).

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

### `discogs_breaker_state` and its effect on `discogs_api` (LML#757)

`GET /health` also reads the LML#755 Discogs saturation circuit-breaker's current state (`discogs/breaker.py`'s `BreakerState`, via the read-only `.state` property — the probe never calls `allow_request()` or anything else that would advance the state machine or consume a trial) and surfaces it verbatim as a top-level `discogs_breaker_state` field: `closed`, `open`, or `half-open` (or `null` when Discogs is unconfigured — no breaker exists, and the field is not consulted so no inert breaker is materialized just to answer the probe). It is deliberately kept out of the `services` object rather than added as another `services.*` key — `services` values feed the `healthy`/`degraded` aggregation via an `("ok", "unavailable")` membership check, and `"closed"` is neither, so folding it in there would spuriously flip a fully healthy deploy to `degraded`.

The breaker's state is authoritative over `services.discogs_api`'s independent live probe: when the breaker is `open` or `half-open` (both are "shedding" — a half-open breaker still sheds every caller except its single in-flight trial), `services.discogs_api` short-circuits to `rate-limited` and the probe's own live call to Discogs is skipped entirely — it does not run and then get overridden. This closes the two-independent-detectors gap behind the 2026-07-13/14 incident: the breaker latched `half-open` and shed 100% of live Discogs calls for roughly 8 hours while the old `services.discogs_api` probe, dispatched independently of the breaker, kept landing in a momentary token-bucket refill and reporting `ok` the whole time — so the one signal an operator or the wxyc-canary would check said everything was fine while the lookup path was actually degraded to cache-only. Only when the breaker is `closed` does `services.discogs_api` defer to the live probe's own result (the vocabulary table above).

```json
{ "status": "degraded", "version": "0.1.0", "commit_sha": "abc123...", "discogs_breaker_state": "open", "discogs_live_requests_total": 1842, "services": { "database": "ok", "discogs_api": "rate-limited", "discogs_cache": "ok" } }
```

Two known limitations follow from `/health` reading the breaker's raw `.state` and treating any shedding state as authoritative:

- **Idle post-cooldown `open` reads as `degraded` until traffic resumes.** The `open → half-open` recovery transition (and the LML#787 watchdog) fire only inside `allow_request()`, which only the live lookup path calls — reading `.state` never advances the state machine. So after an `open` episode's cool-down elapses, if no live Discogs lookups follow, `.state` stays `open` and `/health` keeps reporting `discogs_api: rate-limited` / `status: degraded` even though the next request would admit a recovery trial and likely close the breaker. This is honest (recovery is unproven until a request tries), but a canary that probes `/health` without generating live Discogs traffic (cache-hit library searches don't advance the breaker) can see a sustained `degraded` on an already-recovered-pending-traffic breaker — size any wxyc-canary#79 sustained-shed alarm window with this idle tail in mind. **`discogs_live_requests_total` (LML#940, below) is the eventual fix**: a caller diffs it across polls to tell a real sustained shed (climbing) apart from this idle tail (flat).
- **A shed masks a concurrent auth/network fault on `discogs_api`.** When the breaker is `open`/`half-open` the live probe is skipped, so a coincident token/auth drift (401/403) or connection failure surfaces as `rate-limited` rather than `auth-error`/`network-error`. The `discogs_api.check` Sentry trace tag also goes quiet during a shed (the probe that sets it doesn't run). Drop to `discogs_breaker_state: closed` windows to read live Discogs auth/network health.

### `discogs_live_requests_total` (LML#940 — the wxyc-canary idle-tail fix)

`GET /health` also returns a top-level `discogs_live_requests_total`: a process-global, monotonic count of live-Discogs request *attempts*. It increments at the sole `breaker.allow_request()` call site inside `DiscogsService._request_with_retry` (`discogs/service.py`), **before** the breaker's admit/shed decision — so an attempt the breaker sheds (`allow_request()` returning `None`) still counts. Cache hits short-circuit in `discogs/fallthrough.py` before ever reaching that call site, so they are correctly excluded. Implementation lives in `discogs/live_request_counter.py`, modeled on the `core/event_loop_lag.py` process-global-primitive pattern.

Unit and semantics:

- **Counts live-Discogs-leg attempts, not `/lookup` calls.** One uncached `/lookup` fans out roughly five live Discogs calls (the primary search plus supplemental strategies), so this total runs well ahead of `/lookup` request volume — it is not a request-rate metric, and counting `/lookup` calls instead would defeat the point: a busy-cache / idle-Discogs window would show "traffic flowing" even though live Discogs demand is zero, which is exactly the idle-latched-open scenario this field must NOT mistake for real traffic.
- **Monotonic total, resets to 0 on process restart.** There is no rolling window. A caller polling on a fixed cadence (e.g. the wxyc-canary's 5-minute tick) diffs `total_now - total_prev`; a negative diff means the process restarted mid-window and carries no signal for that tick (treat as an abstain, not a drop to zero).
- **Present even when Discogs is unconfigured.** Unlike `discogs_breaker_state`, no breaker is read to answer this field — it's a plain global-int read, so a fresh process with no Discogs token reports `0` (correct: no live-Discogs traffic has happened, or ever will, for that process).
- **Kept out of `services`**, same reasoning as `discogs_breaker_state`: `services` values feed the `("ok", "unavailable")` `all_configured_ok` check, and a number there would spuriously flip a healthy deploy to `degraded`.
- **Single-worker scope today.** Per-worker (not service-wide) if `UVICORN_WORKERS > 1` ever ships (LML#747) — same caveat as the breaker and the event-loop-lag gauge.

This is the read-only volume signal the idle-tail limitation above was missing: a caller can now distinguish "breaker `open`/`half-open` and this total is climbing across polls" (a real shed under load — page) from "breaker `open`/`half-open` and this total is flat" (the idle tail — no live-Discogs traffic has been attempted since the last poll, so recovery is simply unproven, not blocked — don't page). Consuming this signal in the wxyc-canary is tracked as a follow-up filed once this field ships; see WXYC/library-metadata-lookup#940.

```json
{ "status": "degraded", "version": "0.1.0", "commit_sha": "abc123...", "discogs_breaker_state": "open", "discogs_live_requests_total": 1842, "services": { "database": "ok", "discogs_api": "rate-limited", "discogs_cache": "ok" } }
```

### `commit_sha` (deploy identity)

`GET /health` returns a `commit_sha` field identifying the deployed commit. It is resolved in priority order by `_resolve_commit_sha` in `routers/health.py`:

1. **A `COMMIT_SHA` file baked into the image.** A blank `COMMIT_SHA` placeholder is tracked at the repo root; CI overwrites it with `echo "${{ github.sha }}" > COMMIT_SHA` (the "Record deployed commit SHA" step) immediately before `railway up` in both `deploy-staging` and `deploy-production`. This is the authoritative source in prod and staging. For the file to reach the running image it must clear **two** filters, so it is deliberately listed in **neither**: `.gitignore` (`railway up` honors it and would drop the file from the upload tarball) and `.dockerignore` (the Dockerfile build — `railway.toml` `builder = "DOCKERFILE"`, `COPY . .` — honors it and would drop the file from the image). It ships blank, so a checkout (local dev / CI) reads empty content, which coerces to `null` per the tier rules below.
2. **`RAILWAY_GIT_COMMIT_SHA`** — Railway auto-injects this, but only on its *git-native* deploys (not on `railway up`). Used as a fallback.
3. **`null`** — local dev, CI, and tests (no file, no env var).

Empty or whitespace-only values at any tier coerce to `null`, so the "null when unset" contract holds and downstream equality checks are never fooled by `""`.

```json
{ "status": "healthy", "version": "0.1.0", "commit_sha": "abc123...", "discogs_breaker_state": "closed", "discogs_live_requests_total": 1842, "services": { ... } }
```

#### Why a baked file rather than `RAILWAY_GIT_COMMIT_SHA` (LML#509)

Before LML#602, every push to `main`/`prod` produced **two** deployments: Railway's git-native trigger (carries the SHA) and the CI `railway up` source-deploy (no git metadata). The CI deploy landed ~3 min later — after the lint/typecheck/test/pg gates — and superseded the git-native one, so the deploy actually serving traffic had no `RAILWAY_GIT_COMMIT_SHA`. Reading only the env var left `commit_sha` permanently `null` in prod, which wedged WXYC/Backend-Service's `rotation-artist-backfill` cron (its deploy guard refused to run against an unidentifiable deploy). The baked file attaches deploy identity to the `railway up` deploy — the only deploy now, since LML#602 disabled the git-native trigger.

The two tiers were originally what made the deploy race safe either way: if the `railway up` deploy won, the baked **file** carried the SHA; if the git-native deploy won instead (it has no baked file — that step only runs in the CI job), it *does* carry `RAILWAY_GIT_COMMIT_SHA`, so the **env tier** carried it. With the git-native trigger disabled (LML#602) there is no longer a race — `commit_sha` always comes from the baked file, and the `RAILWAY_GIT_COMMIT_SHA` env tier is now inert. It is kept as a harmless fallback that would re-arm automatically if the GitHub source were ever re-connected.

> **Single CI-gated deploy (LML#602, done):** Railway's native GitHub auto-deploy is disabled on both the prod and staging `library-metadata-lookup` services (the GitHub source was disconnected from each environment), so each push now produces exactly one deploy — the CI-gated `railway up` one. `commit_sha` therefore comes solely from the baked **file** tier; the `RAILWAY_GIT_COMMIT_SHA` env tier is inert (harmless). This was a Railway dashboard/API change, not a code change, and is reversible — re-connecting the GitHub source restores the two-deploy behavior.

#### Cross-repo deploy gating

This is the canonical "is commit X deployed?" signal for cross-repo deploy gates. `settings.app_version` is hardcoded to `"0.1.0"` and is not bumped per deploy, so it cannot serve that role. Downstream callers that depend on a specific LML behavior (e.g. WXYC/Backend-Service rotation-scoped Discogs backfills that require LML#503's `fetched_at` stub-discriminator semantics) should poll `/health` and compare `commit_sha` against the merge SHA of the feature they need before scheduling the job — short-circuiting wasted API budget when staging happens to be running an older deploy.
