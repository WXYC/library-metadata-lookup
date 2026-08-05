# Plan — LML#716: process-global in-flight budget for bulk-family items

Issue: [WXYC/library-metadata-lookup#716](https://github.com/WXYC/library-metadata-lookup/issues/716). Verified against `origin/main` (`74687e8`); implementation branch `716-bulk-global-inflight-budget` in the `library-metadata-lookup-716` worktree.

## Decisions already made (recorded on the issue, 2026-07-10 — do not re-litigate)

1. **Option 1**: a shared process-global permit pool for bulk-family *items* — one semaphore consulted inside each dispatcher's per-item runner, separate env knob. Queue-don't-shed; no wire-contract change.
2. **`/streaming-check` is excluded.** It borrows no asyncpg pool connection (verified: no `pool.acquire` / `lml_cache` writes under `streaming/`). Its loop-time residual is tracked separately in LML#753.
3. **Default sizing tracks `discogs_pool_max_size()`** (`core/dependencies.py:38`) so the budget can't rot out of step with `LML_DISCOGS_POOL_MAX_SIZE`.
4. **The identity bulk-resolve per-request pool-sized semaphore stays** (both gates): per-request semaphore = within-request fairness bound, global budget = cross-request bound.

## Problem (from the issue, condensed)

The #714 cap (`LML_LOOKUP_MAX_CONCURRENT`) bounds single `/lookup` only. The three bulk-family dispatchers each have a *per-request* semaphore but no *cross-request* bound, and all share one event loop + the discogs-cache asyncpg pool (default 5): `POST /api/v1/lookup/bulk` (per-batch `LML_BULK_MAX_CONCURRENT`, default 10 — N concurrent batches admit N×10 items), `POST /api/v1/identity/bulk-resolve-libraries` (pool-sized per-request semaphore; unbounded concurrent requests), `POST /api/v1/cache/refresh-for-identities` (same shape). A bulk-composed cold storm can still starve the pool exactly as in the 2026-06-25 incident.

## Design

### New primitive — `core/bulk_concurrency.py`

A process-global item-permit semaphore plus a telemetry-instrumented acquisition context manager, following the #714 pattern (`lookup/router.py:_get_lookup_semaphore` / `_project_inflight_capped`):

- **Env knob**: `LML_BULK_GLOBAL_MAX_CONCURRENT`, resolved via `resolve_positive_int_env` (`core/search.py:83`) — unparseable/zero/negative WARN + fall back (a 0 cap would deadlock every bulk item forever). Default: `discogs_pool_max_size()`, imported lazily inside the builder if a `core.bulk_concurrency` → `core.dependencies` module-level import creates a cycle (`core/dependencies.py` does not import `core.bulk_concurrency` today, so a top-level import should be safe — verify at implementation).
- **Lazily built module global** (`_bulk_global_semaphore: asyncio.Semaphore | None`), constructed on first use so the env is read at request time (the no-redeploy Railway lever) and tests can reset it between event loops — the same loop-affinity rationale documented on `tests/conftest.py:_reset_lookup_inflight_cap` (line 63).
- **Public surface**: `async def acquire_bulk_global_permit()` — an `asynccontextmanager` that (a) pre-checks `locked()` (exact on Python ≥3.12: includes waiters), (b) measures queue wait when contended, (c) emits telemetry (below), (d) yields inside the permit. All three dispatchers call this one seam; the semaphore global itself stays private.

### Telemetry (the LML#683 lesson: tags + measurements, never `set_data` alone)

- Sentry **tag** `lml.bulk.global_capped: true` set on the current scope when an item observed a contended acquire — idempotent across items, so the request transaction is filterable.
- Sentry **measurement** `lml.bulk.global_wait_ms` — running **max** of per-item queue waits for the transaction (measurements are single-valued per transaction; max is the operator-relevant number for "how starved was this request").
- A per-item span data point (`lml.bulk.global_wait_ms` on the item span) for drill-down, alongside the tag — matching how `_project_inflight_capped` layers span + transaction signals.

### Acquisition order (deadlock-free by construction)

In every dispatcher the order is **per-request/batch semaphore outer → global permit inner**, acquired inside the per-item runner. Global permits release independently of batch permits (both via `async with` unwinding), and no coroutine ever holds a global permit while waiting on a batch permit, so the nesting cannot deadlock. The disconnect path is unchanged: `watch_disconnect` wins the race → `cancel_and_drain(gather_future)` → `CancelledError` unwinds both `async with` frames → both permits release. A test pins the global-permit release (issue constraint).

### Touchpoints

| File | Change |
|---|---|
| `core/bulk_concurrency.py` | New: `LML_BULK_GLOBAL_MAX_CONCURRENT` resolution, lazy global semaphore, `acquire_bulk_global_permit()` CM with telemetry, module docstring update |
| `lookup/router.py` | `_run_one` (inside `async with semaphore:`, ~line 541): wrap the `perform_lookup` block in `acquire_bulk_global_permit()`. Update the #714 scope-note comment (~lines 87–97): the bulk-family residual is now bounded by the LML#716 global permit; `/streaming-check` residual routes to #753; state the two-gate ceiling explicitly — peak discogs-pool contention = `LML_LOOKUP_MAX_CONCURRENT` + `LML_BULK_GLOBAL_MAX_CONCURRENT` when both saturate (reviewer note, 2026-07-10) |
| `identity/router.py` | `_resolve_one` (inside `async with semaphore:`, line 278): same wrap. Extend the `_bulk_resolve_default_concurrency` doc comment (~lines 62–76) with the both-gates rationale |
| `cache/router.py` | The refresh dispatcher's per-item runner (inside its batch semaphore): same wrap |
| `tests/conftest.py` | Extend the autouse reset: `core.bulk_concurrency._bulk_global_semaphore = None` before and after each test — sibling of `_reset_lookup_inflight_cap`, same order-dependent-failure rationale |
| `docs/env-vars.md` | New `LML_BULK_GLOBAL_MAX_CONCURRENT` entry. State the **sum invariant**: peak discogs-pool contention = `LML_LOOKUP_MAX_CONCURRENT` + this budget (the existing entries reason about the pool in isolation). Update the `LML_DISCOGS_POOL_MAX_SIZE` "kept in sync" paragraph and the `LML_LOOKUP_MAX_CONCURRENT` scope note to reference the new bound. Per-worker caveat: if LML#747 ships, this becomes a per-worker bound |
| `docs/architecture.md` | Only if it references the bulk-concurrency shape (check at implementation); otherwise no change |

### Explicitly out of scope

- **Budget deduction**: #714 deducts queue wait from `X-Caller-Budget-Ms` for single `/lookup`. Bulk items receive a per-item copy of the caller budget, and the LML#345 follow-up will redefine it as a batch-level budget — deducting global wait per-item now would entangle with that redesign. Telemetry (the wait measurement) captures the data needed to revisit.
- `/streaming-check` (→ #753), single-`/lookup` throttling (already #714), a truly process-wide all-endpoint cap (the issue's scope note routes that to a separate ticket if ever wanted).
- Removing the identity per-request semaphore (decision 4: it stays).

## TDD sequence (vertical slices, one RED→GREEN per behavior)

Test file: extend `tests/unit/test_bulk_lookup_endpoint.py` (which owns `test_concurrency_bounded_by_semaphore`, line 401) for the bulk-lookup slices; sibling test files for identity/cache slices, following their existing endpoint-test homes. All through public HTTP interfaces with `perform_lookup`/refresh legs stubbed at the DI seam, per repo test conventions.

1. **Sizing behavior**: `acquire_bulk_global_permit` admits exactly `LML_BULK_GLOBAL_MAX_CONCURRENT` concurrent holders; env unset → bound equals `discogs_pool_max_size()`; invalid value → WARN + default. (Direct unit test on the public CM — it is the module's public interface.)
2. **Cross-request bound, bulk lookup**: two concurrent `/lookup/bulk` batches (instrumented stub `perform_lookup` counting in-flight items) never exceed the global bound — the issue's first acceptance criterion, mirroring `test_concurrency_bounded_by_semaphore` but across two POSTs.
3. **Cross-endpoint bound**: a `/lookup/bulk` batch concurrent with an `/identity/bulk-resolve-libraries` request shares one budget (combined in-flight ≤ bound).
4. **Cache refresh under the budget**: `/cache/refresh-for-identities` items count against the same budget.
5. **Disconnect releases global permits**: client disconnect mid-batch → after `cancel_and_drain`, the global semaphore is back to full capacity (the issue's pinned constraint).
6. **Fairness**: a 5-item batch completes while a large drain batch holds the budget (FIFO interleaving — the operator-relevant property, adopted as an acceptance criterion on the issue).
7. **Telemetry**: a request whose items queued carries the `lml.bulk.global_capped` tag and a `lml.bulk.global_wait_ms` measurement; an uncontended request carries neither.
8. **Refactor pass** per the TDD skill: dedupe the three runner integration points if a shared helper shape emerges; keep tests green.

Docs (env-vars sum invariant, scope-note comment updates) land in the same PR after GREEN.

## Acceptance criteria (from the issue + adopted additions)

- Two concurrent 100-item `/lookup/bulk` batches never exceed the shared bound (test 2).
- Single-`/lookup` latency protection: structurally guaranteed — single `/lookup` never acquires the bulk budget, and the budget bounds total bulk pool pressure to `discogs_pool_max_size()` by default; prod verification via the `lml.bulk.global_wait_ms` series post-deploy (in-suite latency assertions would be flaky by construction).
- No new error mode: excess items queue; no 429/503 anywhere (tests 2–4 assert all items complete OK).
- Global permits release on client disconnect (test 5).
- Fairness under a long drain (test 6).
- Env knob observable + documented with the sum invariant and per-worker caveat.

## Delivery

Single PR from `716-bulk-global-inflight-budget`, `Closes #716`, estimated ≤800 lines including tests + docs. CI locally first (`ruff check`, `ruff format --check`, `uv run --no-sync python -m pytest`), then push; `main` → staging auto-deploy; prod promotion is a separate user-driven push per repo convention.
