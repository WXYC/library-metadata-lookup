# Plan — #706: cut cold `/api/v1/lookup` tail latency by taking the streaming-URL live probe off the hot path

Issue: [WXYC/library-metadata-lookup#706](https://github.com/WXYC/library-metadata-lookup/issues/706). Target: cold-cache `/lookup` p99 < 8 s (the #338 charter), warm p50 unchanged (~5 ms), streaming-URL enrichment still populating, with a regression guard.

This plan is verified against `origin/main` (== `prod`, `147acf1`).

## Root cause (verified at code)

Two mechanisms, and they compound into congestion collapse:

1. **Synchronous streaming fan-out, outside every budget.** `apply_streaming_url_postprocess` (`lookup/orchestrator.py:3673`) runs *inside* `enrich_artwork_results`, which is awaited at `:4240` — after `execute_search_pipeline` (`:4080`) and its budget. The post-process explicitly has "no shared outer wallclock", only per-service `wait_for` ceilings (`lookup/streaming_url_postprocess.py:222-232`). The 25 s `LML_SEARCH_HARD_TIMEOUT_MS` wraps only the search pipeline (`core/search.py:762`), so the enrichment + post-process phases are uncapped.

2. **Single loop + 5-conn pool + unbounded in-flight ⇒ the per-call ceilings don't hold.** `entrypoint.sh` runs `uvicorn main:app` with no `--workers` (one loop per replica; confirmed by `discogs/fallthrough.py:79`). The asyncpg pool is `max_size=5` (`core/dependencies.py:94`, `entity/sources.py:101`), shared across the trigram match, the streaming-URL cache get/put, and identity mints. Single `/lookup` has **no** in-flight cap — the `Semaphore` at `lookup/router.py:399` is inside `handle_bulk_lookup` (`:324`), not `handle_lookup` (`:208`). Under load, cold requests pile up (Little's law: ~service-time × arrival-rate in flight), each holding the loop for seconds of external I/O and contending for 5 connections, so `asyncio.wait_for` timers fire late (the issue's "Apple span hits 25 s despite a 5 s ceiling") and trivial PG spans inflate (`cache.put` p95 24.8 s). This is the day-over-day escalation the canary saw.

Two refinements to the issue's framing that shape the fix:

- **It is not SQLite.** `library/db.py` uses `aiosqlite` (`:117`), so FTS5/LIKE runs off-loop on a worker thread. The sync `sqlite3` import is only in `routers/admin.py` (upload/download), not the lookup path. The loop-blocking source is the external HTTP fan-out + pool contention, not the catalog read.
- **Artwork ≠ streaming URLs, and only one Apple call is artwork-bearing.** There are two Apple call sites. The *inline synthesis-path* probe (`orchestrator.py:3438`, `find_track_metadata`) yields `artwork_url` + `release_year` — the field whose absence caused the May iOS outage; it **must stay synchronous**. The *inline happy-path* probe (`:3433`, `find_track_url`) yields only a URL (artwork on that branch comes from the Discogs row). The post-process (`:3673`) only ever mutates URL fields (`update[cfg.url_field]`), never artwork. So every streaming call except the synthesis-path `find_track_metadata` is pure URL enrichment and is safe to move off the response path.

## The load-bearing product decision (already endorsed in the issue)

Streaming URLs become **eventually consistent**: the first lookup of an album not yet in `lml_cache.album_streaming_url_cache` returns without that service's URL; a bounded background task fills the cache within seconds, so the *next* lookup is warm. Artwork stays synchronous (the synthesis-path Apple probe is untouched). This is exactly the issue's own suggested approach ("Streaming URLs are enrichment; they need not block the response") and acceptance criterion #3 measures the **cache fill rate**, not per-request presence — which the background warm preserves. If a consumer (request-o-matic / Backend-Service) turns out to require first-request URL presence, the design changes; the issue's framing says it does not.

## Why the seam is already clean

- The cache layer already separates **read-only** from **read-through-probe**: `get_cached_streaming_url` (`entity/streaming_url_cache.py:186`, pure SELECT) vs `resolve_streaming_url_with_cache` (`:272`, cache → live `find_album_match` → UPSERT + the caller's mint). The hot path switches to the former; the background task runs the latter.
- The background-task scaffold exists in the same function: `_background_tasks` (`orchestrator.py:274`), the lazily-built `_warm_cache_semaphore` / `_WARM_CACHE_CONCURRENCY=4` bound (`:125-136`), and the `_warm_bio_cache` exemplar (`:3872`) — including its load-bearing note: **"No Sentry tag is set here: the request scope has long since closed."**
- `pg` (`get_discogs_cache_pg`, `core/dependencies.py:219-241`) and `entity_store` borrow the **shared process-global pool**, not a request-scoped resource — safe to use from a task that outlives the request.

## PR sequence

Three chained PRs, each TDD (red→green→refactor per `docs/testing.md`), each < 1000 lines, each on its own worktree off `origin/main`. PR3 is gated on measuring PR1+PR2 in prod first.

### PR 1 — Post-process becomes cache-read + bounded background warm (the cure)

**Change** (`lookup/streaming_url_postprocess.py`): in `apply_streaming_url_postprocess`, replace the per-service `resolve_streaming_url_with_cache` gather with `get_cached_streaming_url` (read-only). On a hit, fill `update[cfg.url_field]` synchronously (no mint — cache hits already minted on original resolution). On a miss, schedule **one** bounded, deduplicated background task per (service, artist, album) that runs the existing `resolve_streaming_url_with_cache` (live probe → UPSERT) and, on `live_resolved`, the existing `_mint_identity`. Net hot-path cost: N fast PG SELECTs + task scheduling; zero synchronous external HTTP.

Specifics that the review must see honored:
- **Dedup**: module-level in-flight `set` (`_streaming_warm_in_flight`) keyed on `key = (service_key, to_match_form(artist), to_match_form(album))` — the same normalization `_fetch_cached_row` uses (`:227-228`). Add `key` before `create_task`; clean it up in the done-callback explicitly: `task.add_done_callback(lambda _: _streaming_warm_in_flight.discard(key))`. The set is **process-global** (one per worker process, like `_background_tasks`), *not* request-scoped: two identical cold lookups arriving close together on different request tasks therefore enqueue a single probe. The invariant test asserts exactly this (one probe for N concurrent identical misses).
- **Bound**: a **new, separate** `_STREAMING_WARM_CONCURRENCY` + its own lazily-built semaphore — *not* the bio `_WARM_CACHE_CONCURRENCY` (`:125-136`), because the upstreams and rate limits differ (bio → Discogs API; streaming → per-service clients). Unlike the bio constant (a hardcoded `4`), make this one **env-tunable** as `LML_STREAMING_WARM_CONCURRENCY` (default 4) via the existing `core.search.resolve_positive_int_env` helper — deliberately, because it gates a brand-new background-load path introduced to fix an incident, and a no-redeploy throttle/kill from Railway is the explicit lesson of the Bandcamp hot-path regression. The separate-knob choice follows the `_BANDCAMP_PROBE_TIMEOUT_S`-vs-`_DEFAULT_PROBE_TIMEOUT_S` precedent (`streaming_url_postprocess.py:87,95`). Park each task in `_background_tasks` with `add_done_callback(_background_tasks.discard)` (in addition to the dedup-set cleanup above).
- **Sentry scope fix**: `_project_sentry` (`:311-336`) currently writes to the *active transaction*. Keep `_project_sentry` **hot-path-only** — it keeps writing `cache_hit` and a new `cache_miss_enqueued` to the active transaction (still in-request, correct attribution). The background task's done-callback **logs the `live_*` outcome directly and never calls `_project_sentry`** (mirrors `_warm_bio_cache`'s "request scope has long since closed" note, `orchestrator.py:3881-3882`), so no background write can land on `scope.transaction` and mis-attribute to whatever request runs next. A process-level counter is an acceptable alternative to the log line; the active scope is the thing to avoid.
- **No behavior change to the synthesis-path artwork probe** (`orchestrator.py:3438`) or the happy-path URL probe (`:3433`) in this PR — scope stays inside the post-process function.

**Tests** (`tests/unit/test_lookup_streaming_url_postprocess.py`; cache-layer behavior already covered in `tests/unit/test_streaming_resolve_with_cache.py`):
- RED first: cache miss ⇒ `client.find_album_match` is **not awaited** during the `apply_streaming_url_postprocess` call (assert via a probe-method spy), and exactly one background task is created.
- Cache hit ⇒ `update[url_field]` filled synchronously, no task, no probe.
- Background task, when awaited (drain `_background_tasks` in the test), runs the probe, UPSERTs, and mints on `live_resolved`; uses the passed `pg`/`entity_store` (shared pool), not request state.
- Two concurrent identical misses ⇒ one probe (dedup).
- Sentry: background `live_resolved` does **not** call `set_data` on the active transaction (assert the active scope is untouched).

**Files**: `lookup/streaming_url_postprocess.py` (dedup set + background-warm task helper + separate semaphore + the `_project_sentry` hot-path-only split; **new imports needed**: `from wxyc_etl.text import to_match_form` and `from core.search import resolve_positive_int_env`, neither currently imported in this module), `tests/unit/test_lookup_streaming_url_postprocess.py`. The module docstring rewrite (`:1-50`) must replace the current "no shared outer wallclock" synchronous-gather narrative (`:29-45`) with the cache-read-sync-fill + miss-enqueues-one-bounded-task narrative — prose drafted during implementation, shaped by the red-phase tests, not pre-written here. No change to the bulk path: it already passes `bandcamp=None` (LML#573 PR-3, `lookup/router.py:437`) and that precondition is assumed, not modified. **`docs/env-vars.md`**: document `LML_STREAMING_WARM_CONCURRENCY` (default 4, positive-int, caps concurrent background streaming-URL warm probes; set low to throttle or `0`/`1` to all-but-disable during an incident). **Docs**: the module docstring (`:1-50`) is the canonical description of the new cache-read + background-warm flow (update it here). Do **not** overload `docs/architecture.md`'s Discogs 3-tier "fallthrough seam" section (a different pattern) — instead add a one-line streaming-URL-cache entry to that doc's lookup-flow / key-files list noting the `/lookup` hot path is cache-read-only with background write-back. Est. ~300–400 lines (≈100–150 impl in one module + ≈200–250 test cases: hit-fills-sync, miss-enqueues-one-task, dedup, background-runs-probe+mint, Sentry-scope-untouched), well under the 1000-line cap; if it exceeds 400, keep it one PR but split into reviewable commits.

### PR 2 — Regression guard: assert no synchronous streaming probe on the hot path (+ optional enrichment wall-clock)

**Change**: the durable guard for acceptance #4 is an **architectural invariant test**, not a flaky latency assertion: a cold `/lookup` (mocked clients whose `find_album_match` / `find_track_url` raise if awaited synchronously) must return without awaiting any streaming probe, proving the inline fan-out can't silently re-block. This is deterministic and CI-enforced. Optionally add an env-tunable outer wall-clock budget over the enrichment phase (`metadata_enrichment` step, `orchestrator.py:4238`) mirroring `resolve_search_budget_ms` (`core/search.py:132`) as belt-and-suspenders — but note that a wall-clock alone fires late under starvation (it is a guard; the off-path move in PR1 is the cure).

**Tests** — two levels, each in the location its scope dictates:
- *Post-process unit invariant* in `tests/unit/test_lookup_streaming_url_postprocess.py`: `apply_streaming_url_postprocess` awaits no probe on a miss (already the core of PR1's tests; PR2 hardens it into an explicit "raise-if-awaited" spy).
- *Endpoint invariant*: add new test cases to the **existing** `tests/integration/test_api_lookup_hard_timeout.py` (this endpoint harness lives in `integration/`, using the `app_client` fixture + `@pytest.mark.asyncio` — the right level for a whole-`/lookup` assertion). Make the assertions **deterministic, not wall-clock-timed** (a timing threshold would be flaky and could mask a slow response instead of catching it): gate the mocked probe on an `asyncio.Event` the test controls, assert the `/lookup` response returns **while the probe is still blocked** (proving the probe is off the response path), then release the Event and drain `_background_tasks` to confirm the warm completed. For the concurrency case, fire K=5 lookups with `httpx.AsyncClient(transport=ASGITransport(app=app))` (the pattern already in `tests/integration/test_bulk_lookup_endpoint.py`, which `app_client` may not support concurrently) and assert the same causality (all responses return before any gated probe is released), not a wall-clock bound. The optional wall-clock budget, if it lands, extends the same file alongside the existing hard-cap tests.

**Files**: tests (`tests/unit/test_lookup_streaming_url_postprocess.py`, `tests/integration/test_api_lookup_hard_timeout.py`); optional `core/search.py`-style resolver + one wrap site in `orchestrator.py`; `docs/env-vars.md` if a new env var is added. Est. ~150–250 lines.

### PR 3 — (conditional, measure PR1+PR2 in prod first) cap in-flight `/lookup`

Only if Sentry still shows cold p99 ≥ 8 s after PR1+PR2. Add a `Semaphore` to `handle_lookup` (`lookup/router.py:208`) mirroring the bulk path's `max_concurrency_from_env` pattern (`:398-399`), env-tunable, to structurally break the congestion-collapse feedback loop and protect the background warmers + upstream APIs from burst. Tests mirror `tests/integration/test_bulk_lookup_endpoint.py`'s concurrency tests. Est. ~150–250 lines.

## Risks & mitigations

- **Eventual consistency surprises a consumer** — mitigated by the explicit product decision above (issue-endorsed) and by keeping artwork synchronous; verify the streaming-URL cache fill rate post-deploy (acceptance #3).
- **Background warms re-create load on the same loop/pool** — they're bounded (semaphore) + deduplicated + off the response path; shorter cold responses also cut in-flight count (Little's law), relieving the pool. PR3 caps it structurally if needed.
- **Sentry mis-attribution from background scope** — explicitly handled in PR1 (background outcomes never touch the active transaction).
- **Mint moves to background** — already best-effort and swallowed (`streaming_url_postprocess.py:300-308`); no contract change.
- **Hot path regression slips back in** — the PR2 invariant test fails CI if any synchronous streaming probe returns to `/lookup`.

## Acceptance & verification

**Merge-blocking (CI, deterministic):**
- [ ] PR1 unit tests green: cache hit fills synchronously; miss enqueues exactly one task; dedup; background task runs probe + mint off the request; `_project_sentry` never writes background outcomes to the active scope.
- [ ] PR2 invariant test green: no synchronous streaming probe on `/lookup` (the guard for criterion #4).

**Post-deploy signals (observability, not CI — staged: PR1 → PR2 → measure before PR3):**
- [ ] Sentry (`is_transaction:true environment:production transaction:"/api/v1/lookup"`): cold p99 < 8 s sustained, **measured under production concurrency** (the regression only manifests under load — a single cold request passes even with the bug).
- [ ] Warm p50 unchanged (~5 ms).
- [ ] `lml_cache.album_streaming_url_cache` fill rate holds (background warm populating); spot-check that a cold album's URL appears on the *second* lookup.
- [ ] `wxyc-canary` `lml-auth` probe back to ~730 ms baseline.

(The GitHub issue body should carry this same merge-blocking-vs-post-deploy split so a PR reviewer doesn't block on, or merge past, the wrong signal.)

## Out of scope / follow-ups

- **uvicorn worker count / asyncpg pool sizing.** Bumping `max_size` from 5 or running N workers is an ops change with history (the `entity/sources.py:86-101` comment references the #241 orphaned-pool fix) — and PR1 removes the cache get/put + mint from the hot path, so pool pressure should drop without it. Measure first; file separately if still needed.
- **The inline happy-path Apple URL probe** (`orchestrator.py:3433`) could also move off-path (it's URL-only), but it touches the delicate LML#487/#505/#462 happy/synthesis branching — defer to a follow-up only if PR1+PR2+PR3 miss the target.
- **Bandcamp** stays off in prod (`LML_PERSIST_STREAMING_URL_BANDCAMP=false`); no change.

## Worktree / workflow

Each PR on its own worktree off `origin/main` (`git worktree add .worktrees/706-pr1 origin/main`, etc.), branch per PR, conventional `feat(#706):` / `test(#706):` commits with a body. File the GitHub issue (this plan as the body) + PR with `Closes #706` per the org workflow; this is an epic-adjacent item under [Post-launch service hardening](https://github.com/orgs/WXYC/projects/32) → check whether it belongs under epic [A-LML perf #338](https://github.com/WXYC/library-metadata-lookup/issues/338) before filing standalone.
