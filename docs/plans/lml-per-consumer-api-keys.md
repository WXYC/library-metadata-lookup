# LML per-consumer API keys — replace the shared `LML_API_KEY` with a `lml_cache.api_keys` table

No GitHub issue yet — this plan gets filed as one (with this doc as the issue body) once it clears review, per the org's Planning & PRs convention. Motivated by the 2026-07-23 `LML_API_KEY` rotation incident: rotating LML's own copy left Backend-Service's EC2 `.env` and wxyc-canary's Secrets Manager copy stale, producing a multi-hour 403 lockout that was misdiagnosed twice (thought to be Backend-Service's `backend` container, then `enrichment-worker`) before being traced to canary's independently-stale copy — because nothing in the current scheme can attribute a request to a specific caller. (Backend-Service staying 403 after the rotation had a second, distinct contributor — a `TARGET`/`RESTART_TARGET` shell-variable-shadowing bug in `set-ec2-env-var.yml`'s SSH restart step meant `backend` never actually restarted with the rotated key; that bug is already fixed in the same file and isn't part of what this plan addresses.)

## Problem recap (verified against code)

- `core/auth.py:require_lml_key` (`core/auth.py:74-115`) validates `Authorization: Bearer <token>` against exactly one value, `settings.lml_api_key`, via a plain `!=` (`core/auth.py:112`). No per-caller identity, no expiry, no scoping, no revocation short of rotating the one value everyone shares.
- Four known consumers today, each holding an independent copy of the **same** value:
  - **tubafrenzy** — `XYCConfig.getLmlApiKey()` resolves system property `lml.api.key` → env var `LML_API_KEY` → properties file → `null` (`XYCConfig.java:142-149`), consumed by `LibrarySearchClient`/`ResolveReleaseClient` at startup (`StartupListener.java:151-157`). Sourced from a GitHub Actions secret at deploy (`.github/scripts/install-webapps.sh`). **Not** in that script's `REQUIRED_VARS` (`DB_PASSWORD POSTHOG_API_KEY SENTRY_DSN LIBRARY_SEARCH_URL MIRROR_API_KEY` — no LML key), unlike `MIRROR_API_KEY`, which fails the deploy if unset. A missing/wrong LML key on tubafrenzy today fails **silently**: `LibrarySearchClient.applyAuth` no-ops when `apiKey == null` and sends no `Authorization` header at all (`LibrarySearchClient.java:162-168`), relying entirely on `LML_REQUIRE_AUTH` staying off.
  - **Backend-Service** — `process.env.LML_API_KEY` read transitively by `@wxyc/lml-client`, shared by `apps/backend`, `apps/enrichment-worker`, and the backfill `jobs/*` (all read the same EC2 `.env`). Pushed via `.github/workflows/set-ec2-env-var.yml` (GH secret → SSH → upsert `.env` → restart one named container).
  - **wxyc-canary** — AWS Secrets Manager, fetched live on every Lambda invocation via `resolveLmlApiKey()` (`wxyc-canary/src/handler.ts`) — no caching, no redeploy needed to pick up a new value. The best-behaved consumer today, which is exactly why the 07-23 incident's root cause looked like a Backend-Service problem for hours: canary's independent, stale copy is what kept 403ing after both BS-side fixes landed.
  - **Human operator** — `scripts/artist_resolve_drain/__main__.py:72,163` reads `$LML_API_KEY` or `--api-key`, run ad hoc from a laptop.
- Root causes this plan addresses:
  1. **Lockstep rotation.** The instant any one copy is updated, every other copy is instantly wrong — there's no overlap window, so rotation is a race against however long it takes to update every consumer.
  2. **No attribution.** `_log_auth_rejection` (`core/auth.py:55-71`) logs `client_ip`/`user_agent`, both weak — in the 07-23 incident every rejected caller showed `user_agent: node`, indistinguishable from each other. Diagnosis had to fall back to reasoning about request *cadence* instead of reading the caller off the request.
  3. **No revocation without a full rotation.** A single leaked copy can't be killed in isolation; fixing it means rotating for everyone.

## Goals

- Every consumer gets its own distinct credential, resolvable to a caller name at request time.
- Rotation becomes issue-new → confirm → revoke-old, not a synchronized cutover — the 07-23 failure mode becomes structurally impossible for future rotations.
- The request-path check stays as cheap as today's (in-memory), despite the source of truth moving into Postgres.
- The migration itself introduces no lockout: existing `LML_API_KEY` traffic keeps working until every consumer is confirmed migrated.

## Non-goals (this plan)

- **No OIDC/JWT/short-lived tokens.** Investigated: Backend-Service already runs a real OIDC provider (`shared/authentication/src/auth.definition.ts` — `oidcProvider()` + `jwt()` plugins, a working `oidc-trusted-clients.ts` registration pattern), but every existing client uses login-oriented grants — authorization-code+PKCE (Wiki.js, Flowsheet Verifier) or device-code (DJ iOS approval). wxyc-canary's own OIDC client is a synthetic test of *that login flow*, not a machine-to-machine credential. There's no `client_credentials`-style grant configured anywhere. Adding one is a real, separable effort — out of scope here.
- **No HMAC request signing.** Stronger (secret never rides the wire), but real implementation lift, and tubafrenzy (legacy Java servlets) is the hard consumer to retrofit it onto.
- **No self-service "mint a key" HTTP API.** Four known consumers; a CLI script is enough.
- **No per-process split within Backend-Service.** One row covers all of BS (`apps/backend`, `apps/enrichment-worker`, backfill jobs) to start. The table makes splitting this later cheap — more `INSERT`s, no code change — so deferring it isn't a lock-in.
- **No API gateway product** (Kong / AWS API Gateway / Cloudflare API Shield). Checked better-auth (1.6.20, already a Backend-Service dependency) for a ready-made API-key/machine-token plugin — not present in the installed package (`dist/plugins/` has `bearer`, `oidc-provider`, `device-authorization`, `jwt`, etc., no `api-key`). None of this changes the recommendation: disproportionate infra for four consumers.
- **No new auth kill-switch flag.** `require_lml_key`'s new behavior is strictly additive (accepts the legacy key *and* table-backed keys), so the existing `LML_REQUIRE_AUTH` toggle remains the one emergency escape hatch. A second flag gating only the new path wouldn't correspond to any coherent "safe mode" — see Rollback.

## Constraints

- TDD required, repo-wide, not optional (`docs/testing.md`).
- `lml_cache.*` is LML-owned: lifespan-bootstrapped, `CREATE ... IF NOT EXISTS`, no alembic (org CLAUDE.md schema-ownership rule). This table follows the exact convention already used by `entity/streaming_url_cache.py`, `entity/track_streaming_url_cache.py`, `entity/release_resolution_cache.py`, `entity/discogs_rate_bucket.py`, `entity/library_release_override.py`, and `entity/streaming_catalog.py`.
- Hot-path latency sensitivity: LML is mid the org's "Post-launch service hardening" initiative, itself triggered by a prior lookup-latency regression. `require_lml_key` runs on every protected request; this change must not add a synchronous DB round-trip to that path.
- PR size target <1000 lines (org CLAUDE.md) — lands as multiple chained PRs, not one.
- Data safety: rotation/revocation operations are always scoped to a single `id`/`caller_name`, never a blanket update.
- The new table lives in the same discogs-cache Postgres pool as the other `lml_cache.*` tables (bootstrapped off `get_discogs_pool()` in `main.py`), not a separate credential store.

## Design

### Schema — `entity/api_keys.py`

Mirrors `entity/track_streaming_url_cache.py`'s shape exactly (module docstring, `_DDL_SCHEMA`/`_DDL_TABLE` constants, a `set_up_api_keys_schema(pg: PgSource)` bootstrap function using `pg.execute(...)`/`pg.fetchall(...)`, same as the sibling modules):

```python
_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS lml_cache"

_DDL_TABLE = """\
CREATE TABLE IF NOT EXISTS lml_cache.api_keys (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    caller_name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    note TEXT
)\
"""

_SELECT_ACTIVE_SQL = """\
SELECT key_hash, caller_name FROM lml_cache.api_keys WHERE revoked_at IS NULL\
"""

_TOUCH_LAST_USED_SQL = """\
UPDATE lml_cache.api_keys SET last_used_at = now()
WHERE key_hash = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')\
"""

_INSERT_SQL = """\
INSERT INTO lml_cache.api_keys (caller_name, key_hash, note)
VALUES ($1, $2, $3) RETURNING id, created_at\
"""

_REVOKE_SQL = """\
UPDATE lml_cache.api_keys SET revoked_at = now()
WHERE id = $1 AND revoked_at IS NULL RETURNING caller_name\
"""
```

No `UNIQUE(caller_name)` — deliberately. A rotation in progress means two live rows for the same caller (old not-yet-revoked, new not-yet-confirmed); that's the intended state, not a bug. No `CHECK` on allowed caller names either — low cardinality, low churn, and `lml_cache.*`'s no-alembic convention means evolving a `CHECK` later would need a hand-rolled `ALTER` anyway (see `entity/track_streaming_url_cache.py`'s `_DDL_ALTER_CHECK` for what that idempotent-widen pattern looks like if it's ever wanted); plain `TEXT` plus code review of the seed script is enough at this scale.

### Token format + hashing

- `generate_token()` → `lml_<32 random url-safe bytes via secrets.token_urlsafe>`. The prefix costs nothing and makes an accidental leak grep-able later (same idea as `ghp_`/`sk_live_` prefixes).
- `hash_token(token)` → `hashlib.sha256(token.encode()).hexdigest()`. Plain SHA-256, no per-token salt: these are high-entropy random values, not low-entropy passwords, so there's no dictionary/rainbow-table risk a salt would mitigate — the same posture GitHub and Stripe use for API-key storage.
- Only the hash is ever persisted anywhere. The plaintext is shown exactly once, at mint time, by the seed script below, and is never logged.

### In-process cache — `core/api_key_cache.py` (keeps the hot path DB-free)

- `ApiKeyCache` holds an in-memory `dict[key_hash, caller_name]`, built from `_SELECT_ACTIVE_SQL`.
- `start_api_key_cache(pg, refresh_seconds) -> Task | None` mirrors the existing `start_sampler()`/`stop_sampler()` pair (`main.py:309-321`, LML#907's event-loop-lag sampler): loads the initial snapshot synchronously before the app starts serving, then a background `asyncio` loop calls `refresh()` every `refresh_seconds` (new setting `lml_api_key_cache_refresh_seconds`, default 300, `ge=1` — the same 5-minute cadence this org already reasons in elsewhere, e.g. the canary schedule; `ge=1` matches the validation-at-load convention every other interval/timeout setting already follows, e.g. `discogs_search_statement_timeout_ms: ge=1` at `config/settings.py:161-174`, so a bad value fails loudly at boot instead of busy-looping the refresh task). Best-effort: a failed start logs and continues rather than blocking boot, same posture as the lag sampler.
- **`refresh()` failure handling — the one place this design must not correlate all four consumers' failures.** A transient discogs-cache PG blip during `refresh()` must **not** clear or replace the current snapshot — only a successful fetch swaps the dict. Mirror `core/event_loop_lag.py:141-146`'s guard verbatim: wrap the fetch in `try/except Exception`, `logger.exception(...)` and keep serving the last-known-good snapshot, never bubble the error into an empty cache. This matters specifically because of the "Changed misconfiguration check" below: once PR 4 removes the legacy fallback, an `ApiKeyCache` that ever goes empty on error would 500 all four consumers simultaneously on a single blip — the same correlated-failure shape as the 07-23 incident this plan exists to prevent.
- `resolve(token) -> str | None` — hash, dict lookup, zero I/O on the request path.
- `touch_last_used(key_hash)` — fired via `asyncio.create_task` (not awaited) on a hit, so it never adds request latency. `_TOUCH_LAST_USED_SQL`'s own `WHERE` clause throttles writes to roughly hourly per key. **Must be anchored with a strong reference**, not just fired and forgotten: `asyncio.create_task` returns a weak reference, and without anchoring it the GC can reap the task mid-flight, silently dropping the write. This repo has hit exactly this bug before and fixed it the same way three times already — `lookup/enrichment/background.py:32-40`, `lookup/streaming_url_postprocess.py:126-130`, and `routers/admin.py:46-48` all keep a module-level `_background_tasks: set[asyncio.Task] = set()` with a done-callback that removes the task on completion. `core/api_key_cache.py` follows the identical pattern (a fourth instance of it in this codebase), since both the Rollout section's "confirm `last_used_at` ticks" verification and the CLI's `list` command depend on these writes actually landing.
- Wired into `main.py`'s `if settings.database_url_discogs:` block in two distinct parts, stated unambiguously so the implementer doesn't conflate them:
  - **(a) Schema DDL as a `bootstraps` tuple entry.** `set_up_api_keys_schema` goes in as one more `(label, bootstrap)` entry in the existing best-effort `bootstraps` tuple, same try/except-per-item posture as the six already there. The bootstraps entries are pure idempotent DDL lambdas whose return value the loop discards (`main.py:290-302`), which is exactly what a one-shot `CREATE ... IF NOT EXISTS` is.
  - **(b) Cache start as a separate start/stop pair.** `start_api_key_cache` is **not** a bootstraps entry — a long-lived refresh task can't be, since the loop discards returns. It runs *after* the bootstraps loop but **still inside the same `if settings.database_url_discogs:` block**, so it reuses the identical `source = PgSource(pool=pool)` already constructed there (`main.py:235`) rather than acquiring a separate pool. This mirrors how `lag_sampler_task` (`main.py:309-321`) is a distinct start/stop pair, not a bootstraps entry.
  - If `database_url_discogs` is unset, or pool acquisition fails, the whole block is skipped (existing behavior) and `start_api_key_cache` is simply never called — `ApiKeyCache` stays unstarted, `resolve()` always returns `None`, and every request falls through to the legacy-key branch (or, post-PR4, the fail-closed path in Risks). Only the **stop handle** is threaded out to the lifespan's shutdown block, alongside `lag_sampler_task`, so the refresh task is cancelled cleanly on shutdown.
  - **Expose a module-global accessor/reset seam for the singleton `ApiKeyCache`** in `core/api_key_cache.py` — `get_api_key_cache()` / `reset_api_key_cache()`, mirroring `core/event_loop_lag.py`'s `get_event_loop_lag_ms()` / `reset_gauge()`. `require_lml_key` resolves the live cache through this accessor, and its new unit tests need the reset to seed a known cache and isolate between cases.

### `require_lml_key` changes (`core/auth.py`)

Extends the existing behavior matrix; every current branch is unchanged:

- `LML_REQUIRE_AUTH=false` → no-op. *(unchanged)*
- Header missing → 401. *(unchanged)*
- Malformed scheme → 403. *(unchanged)*
- **New:** bearer token's hash resolves via `ApiKeyCache.resolve()` → pass; fire `touch_last_used` (fire-and-forget); attribute the caller somewhere queryable (see Observability below).
- **New, transitional:** token equals the legacy `settings.lml_api_key` (if still configured) → pass; log at WARNING with `reason="legacy_shared_key_used"` — distinct from today's rejection reasons, specifically so not-yet-migrated traffic stays visible for the length of the rollout.
- Neither matches → 403, `reason="invalid_token_value"`. *(unchanged)*
- **Changed misconfiguration check:** today, `LML_REQUIRE_AUTH=true` + `LML_API_KEY` unset → 500. New equivalent: `LML_REQUIRE_AUTH=true` + no legacy key configured + the active-key cache is empty → 500. Keeps the fail-loud distinction between "the deploy is broken" and "this caller isn't authorized."

### Observability — actually answering "who is calling us"

Per-request log lines on every success would be as high-volume as authenticated traffic itself (BS's live proxy path is the dominant caller and is not low-frequency) — not worth it as a bare `logger.info`. Default to a **Sentry span tag** on the request (`caller_name`, stamped by `require_lml_key` on a hit) — LML already threads span attributes for `Server-Timing` (BS#881), so this extends an existing mechanism rather than adding a new one. Checked whether to instead ride the `cache_stats`/PostHog counter convention: `init_cache_stats` is only wired into `lookup/router.py`, `streaming/router.py`, and `artists/router.py`, but `require_lml_key` is a shared dependency across all **eight** protected routers (`main.py:365-392`: `lookup`, `library`, `discogs`, `streaming`, `release`, `identity_api_v1` — distinct from the open `/identity/resolve`,`/identity/bulk` router — `artists`, and `cache`), so that mechanism has no attachment point on 5 of the 8 routes this change covers (`library`, `discogs`, `release`, `identity_api_v1`, `cache`). The span tag has no such gap, since every protected request already runs inside a span.

### Seed / revoke / list CLI — `scripts/api_keys/__main__.py`

Packaged the same way as `scripts/artist_resolve_drain` (a `scripts/<name>/__main__.py` single-purpose module), but that script is a pure HTTP client with no database connection, so it can't model the actual write mechanic here. The real precedent is `scripts/seed_library_release_overrides.py:165-183` — this codebase's only existing script that writes directly to an `lml_cache.*` table: `PgSource(dsn=get_settings().database_url_discogs)`, bootstrap the schema before writing (`set_up_library_release_override_schema`, idempotent `IF NOT EXISTS`), a bare pooled connection for the write, `try/finally` closing the pool. `scripts/api_keys` follows the same shape against `entity/api_keys.py`'s `set_up_api_keys_schema`:

- `python -m scripts.api_keys seed --caller wxyc-canary --note "..."` — generates a token, inserts its hash, prints the plaintext exactly once with a "will not be shown again" warning. Never logs it.
- `python -m scripts.api_keys revoke --id <id>` — sets `revoked_at`, prints the `caller_name` it revoked for confirmation.
- `python -m scripts.api_keys list` — prints `id, caller_name, created_at, last_used_at, revoked_at`. Never `key_hash` or plaintext. This is also the direct operational answer to "can we delete this key yet."

## Implementation plan (chained PRs)

### PR 1 — schema + cache + dual-accept auth (library-metadata-lookup)

TDD order (`docs/testing.md`):
1. **RED** — extend `tests/unit/test_auth.py`: table-backed hash hit passes; hash miss falls through to legacy key and passes; hash miss with no legacy match fails (`invalid_token_value`); revoked hash fails identically to a never-existing one (don't leak the distinction to the caller); empty cache **and** no legacy key configured → 500.
2. **GREEN** — `entity/api_keys.py` (schema/DDL/queries), `core/api_key_cache.py` (`ApiKeyCache`, `start_api_key_cache`/`stop_api_key_cache`, `get_api_key_cache`/`reset_api_key_cache` seam), `core/auth.py` (`require_lml_key` dual-accept), `main.py` wiring (one `bootstraps` tuple entry for the schema + one separate start/stop pair for the cache, per the Design section's (a)/(b) split), `config/settings.py` (`lml_api_key_cache_refresh_seconds`, default 300, `ge=1`).
3. **Docs** — document the new `LML_API_KEY_CACHE_REFRESH_SECONDS` var in `docs/env-vars.md` **as part of this PR** (every other interval/timeout setting is documented there — e.g. `LML_EVENT_LOOP_LAG_GAUGE` at `docs/env-vars.md:52`, `DISCOGS_SEARCH_STATEMENT_TIMEOUT_MS` at :49 — and global CLAUDE.md requires documenting new env vars in the PR that introduces them, not deferring to PR 4). Note the default (300), the `ge=1` floor, and that it bounds revocation staleness (a revoked key stays live up to this long, per Risks).
4. **Integration** (`-m pg`): bootstrap creates `lml_cache.api_keys` idempotently; a seeded row round-trips through `ApiKeyCache.refresh()`; `touch_last_used` is a no-op on a second call within the hour.
5. **Refactor** pass.

### PR 2 — seed/revoke/list CLI + tests (library-metadata-lookup)

Isolated from PR 1's hot-path change; reviews independently. Every script in `scripts/` has a corresponding entry in `docs/scripts.md` — including `scripts/seed_library_release_overrides.py`, the closest analog — with no exceptions; add one for `scripts/api_keys` (usage for `seed`/`revoke`/`list`, the "prints plaintext once, never logs it" safety note) and update the `docs/scripts.md` router blurb in the top-level `CLAUDE.md` to name it alongside the other scripts already listed there.

### PR 3 — tubafrenzy `REQUIRED_VARS` fix (tubafrenzy)

Add the LML key env var to `REQUIRED_VARS` in `.github/scripts/install-webapps.sh`, closing the silent-fail gap found during design. Independent of the rest of the rollout — can land anytime, even before PR 1.

### Rollout (HITL — live secrets, one consumer at a time)

Run interactively, not from an AFK agent — same posture as `docs/plans/838-staging-cutover-runbook.md`. Per step: seed a new row for that caller → hand the plaintext to that consumer's own secret store → confirm `last_used_at` ticks on the new hash (or the caller-attribution signal from Observability, once that's landed) → the legacy shared key stays live throughout, since it isn't consumer-specific and there's nothing to revoke until every consumer is off it.

Order, safest/most-observable first:

1. **wxyc-canary** — already live-fetches per invocation (a value swap needs no redeploy), lowest blast radius, and it's the tripwire that would catch a problem with any of the *other* consumers — proving the new scheme against it first is the cheapest confidence check available.
2. **Operator script** (`artist_resolve_drain`) — one person, immediately verifiable, zero blast radius if wrong.
3. **Backend-Service** — higher traffic, three processes sharing one row (per Non-goals). By this point the mechanism has already been proven against two lower-stakes consumers.
4. **tubafrenzy** — bundle with PR 3 so the `REQUIRED_VARS` gap doesn't reopen on the next unrelated tubafrenzy deploy.

### PR 4 — cleanup (library-metadata-lookup)

Once all four consumers show zero `reason=legacy_shared_key_used` log lines for 7 consecutive days (a full weekly cycle, so any low-frequency caller gets a chance to show up): remove the legacy-fallback branch from `require_lml_key`, remove `LML_API_KEY` from LML's Railway env, update `docs/env-vars.md`'s "Inbound Auth" section and `README.md` to describe the table instead of the single var. While in there: `docs/env-vars.md:74` already understates the protected surface (says "the `lookup`, `library`, `discogs`, and `streaming` routers" — 4 of the current 8, per the Observability section above), predating this plan; correct the full router list at the same time rather than let that drift persist.

## Testing plan

- Unit: `hash_token`/`generate_token` — stable digest, sufficiently random/prefixed output.
- Unit: `require_lml_key` — all five branches in the behavior matrix above, including the changed 500 condition.
- Unit: `ApiKeyCache.resolve` — hit, miss, revoked-treated-as-miss.
- Unit: `ApiKeyCache.refresh()` — a fetch that raises leaves the existing snapshot untouched (`resolve` still succeeds for a previously-loaded key); only a successful fetch replaces it.
- Unit: `touch_last_used`'s scheduled task actually runs to completion under a real event loop (not just that `create_task` was called) — guards the strong-reference anchoring in `core/api_key_cache.py`'s `_background_tasks` set, the same regression this repo already tests for around `lookup/enrichment/background.py`'s equivalent set.
- Integration (`-m pg`): schema bootstrap idempotency; seed → cache refresh → resolve round-trip; `touch_last_used` throttling.
- Unit: seed/revoke/list CLI — inserts a row and returns plaintext once; revoke sets `revoked_at` and is idempotent (second revoke on the same id is a no-op, not an error); list never surfaces `key_hash`.

## Rollback

- **Single consumer:** point it back at the still-live legacy `LML_API_KEY` — no other consumer is affected, since dual-accept means legacy traffic never stopped working.
- **A bad row** (wrong hash, wrong caller_name): revoke it; that consumer falls back to the legacy key until re-seeded.
- **PR 1 itself:** `LML_REQUIRE_AUTH` is untouched by this change and remains the master kill switch — flipping it off disables all auth checking, new and legacy alike, the same emergency path that already exists today.

## Risks

- **Shared PG instance.** `key_hash` values are readable by anyone with raw `psql` access to the discogs-cache instance — broader than "LML's application code," though schema-ownership rules mean no *other application* queries `lml_cache.*`. Hashing (not plaintext) keeps this from being a direct credential leak; treat it like any other credential-adjacent table access, nothing new introduced.
- **Cache staleness window.** Revocation takes up to `lml_api_key_cache_refresh_seconds` (default 5 min) to take effect, not instantly. Fine for planned rotation; not a substitute for an immediate response if a key is being actively abused (in that case: flip `LML_REQUIRE_AUTH` off, or rotate the DB credentials themselves).
- **Post-PR4, auth becomes hard-dependent on discogs-cache PG availability — new for this codebase.** The refresh-failure guard above protects a *previously-populated* cache from being cleared by a transient error, but not a cold boot where the pool never completes its first successful load — in that case `ApiKeyCache` starts empty, and once PR 4 removes the legacy fallback, "empty cache" hits the same 500 misconfiguration branch as a genuine misconfig. Every other `lml_cache.*` consumer in this codebase degrades to a no-op on a PG outage (e.g. `entity/track_streaming_url_cache.py`'s `get`/`set` swallow PG errors and fall through to a live probe); this table is the first where a PG outage fails *closed* for all four consumers at once — the same correlated-failure shape this plan exists to eliminate, just moved from "rotation" to "discogs-cache PG outage." `LML_REQUIRE_AUTH=false` is the same documented emergency mitigation named above; worth deciding during PR 4 whether that's an acceptable standing risk or whether the empty-cache-at-boot case should degrade open instead (e.g. treat "cache never loaded" as distinct from "cache loaded and is legitimately empty").
- **BS granularity (accepted, see Non-goals).** One row for all of Backend-Service means a compromised BS key doesn't distinguish which of its three processes leaked it.

## Acceptance criteria

- [ ] `lml_cache.api_keys` created via the standard lifespan bootstrap; DDL idempotent (matches sibling `entity/*.py` modules).
- [ ] `require_lml_key` accepts a valid table-backed key, rejects a revoked one, and still accepts the legacy shared key throughout the transition.
- [ ] `ApiKeyCache` adds no synchronous DB call to the request path.
- [ ] All four consumers migrated and confirmed via `last_used_at`/log evidence.
- [ ] tubafrenzy's `REQUIRED_VARS` gap closed.
- [ ] Legacy fallback removed once migration is confirmed; `docs/env-vars.md`/`README.md` updated.

## Worktree

Before any implementation (PR 1 onward): `git fetch origin && git worktree add .worktrees/lml-per-consumer-api-keys -b lml-per-consumer-api-keys origin/main`. The default tree here is currently on the `prod` branch (which lags `origin/main`) — per global CLAUDE.md, new work always starts from a fresh worktree off `origin/main`, not off whatever the ambient tree happens to be on (`project_prod_tree_lags_main`).

This plan lives at `docs/plans/lml-per-consumer-api-keys.md`, the single tracked plan home (`docs/plans/README.md`) since LML#1124's document consolidation retired the old untracked top-level `plans/` directory. It is already committed on `origin/main`, so any fresh worktree checked out from there has it available to reference from the start.
