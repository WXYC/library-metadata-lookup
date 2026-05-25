# LML#372 — Cancel-aware bulk-lookup gather

## Problem (one-paragraph recap)

`POST /api/v1/lookup/bulk` fans items into the 5-permit Discogs semaphore under an `asyncio.gather`. When a client's `AbortController` fires (BS runtime: 30 s; per-row cron: 8 s), uvicorn closes the socket but does **not** propagate cancellation into the handler. The gather keeps draining; queued items keep holding the semaphore; the next batch piles on. Result: 10 consecutive 30 s-flat-line batches with 20/20 errors observed in the 2026-05-24 22:21–22:34 UTC autopsy ([BS#1064 comment](https://github.com/WXYC/Backend-Service/issues/1064#issuecomment-4531269302)).

Issue: [LML#372](https://github.com/WXYC/library-metadata-lookup/issues/372). This plan ships **mitigation (1) only** — cancel-aware gather. Mitigations (2) admission control, (3) raise the semaphore, and (4) fair queueing are explicitly out of scope; (2) is the natural next ship if (1) leaves a residual cliff.

## Goal & non-goals

**Goal.** When the calling client closes its socket, the LML handler observes the disconnect, cancels the in-flight `gather`, and releases the Discogs semaphore permits held by abandoned items — before the next batch arrives.

**Non-goals.**
- Changing the public contract of `POST /api/v1/lookup/bulk`. Behavior for callers that stay connected is identical.
- Changing the 5-permit semaphore. Discogs's 60/min ceiling is the real cap; growing the semaphore moves the cliff, doesn't remove it.
- Admission control / 503 on queue depth. Sibling ticket; layered if needed.
- Fair queueing across callers. Out of scope.
- Per-item budgeting changes. The existing `X-Caller-Budget-Ms` passthrough stays as-is; the `LML#345` follow-up redefines it as a batch-level budget separately.
- Sibling `LML#370` (cascade-exhaustion ceiling) and `LML#371` (uvicorn span observability gap) — separate tickets, separate PRs.

## Design

### Cancel-aware execution

Replace the single line at `lookup/router.py:341`:

```python
results = await asyncio.gather(*(_run_one(i, item) for i, item in enumerate(request.items)))
```

with a structured pattern that races the gather against a disconnect sentinel and cancels the gather if the client departs first. Python 3.12 (per `pyproject.toml:requires-python = ">=3.12"`) — `asyncio.TaskGroup` is available, but TaskGroup propagation semantics on external cancellation are awkward for our "abandon and return partial telemetry" intent; a plain `asyncio.wait([gather_task, sentinel_task], return_when=FIRST_COMPLETED)` over named tasks is cleaner and matches Starlette's own background-cancellation pattern.

Sketch (final form lives in `lookup/router.py`):

```python
async def _watch_disconnect(request: Request, *, poll_interval_s: float = 0.25) -> None:
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(poll_interval_s)

# ...inside handle_bulk_lookup, replacing the bare gather:
gather_task = asyncio.create_task(
    asyncio.gather(*(_run_one(i, item) for i, item in enumerate(request.items))),
    name="lml.bulk.gather",
)
sentinel_task = asyncio.create_task(_watch_disconnect(http_request), name="lml.bulk.disconnect")

with sentry_sdk.start_span(op="lml.bulk.batch", name=f"{len(request.items)} items") as span:
    span.set_data("lml.bulk.size", len(request.items))
    span.set_data("lml.bulk.max_concurrent", max_concurrent)
    done, pending = await asyncio.wait(
        {gather_task, sentinel_task}, return_when=asyncio.FIRST_COMPLETED
    )

    client_aborted = sentinel_task in done and not gather_task.done()
    span.set_data("lml.bulk.client_aborted", client_aborted)
    if client_aborted:
        # Tag is global-scope (filterable across all routes); span data carries
        # the bulk-specific context. The key-name asymmetry is intentional.
        sentry_sdk.set_tag("lml.client_aborted", "true")
        gather_task.cancel()
        # Drain the cancellation; per-item _run_one already isolates Exception,
        # so CancelledError propagates cleanly through gather().
        with contextlib.suppress(asyncio.CancelledError):
            await gather_task
        # Free the sentinel; gather_task is already done.
        # No partial-results path: returning an HTTPException matches the
        # transport reality (the client is gone) and keeps the telemetry
        # branch dead-simple.
        raise HTTPException(status_code=499, detail="client disconnected")

    # Happy path: cancel the sentinel, harvest results.
    sentinel_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sentinel_task
    results = gather_task.result()
```

**Status code 499** — Nginx-style "client closed request". Not a standard HTTP code but widely understood; uvicorn won't actually deliver the body (the socket is closed) so the code is for our logs/Sentry only. The alternative is letting the exception propagate as a 500, which would pollute Sentry with self-inflicted errors. We can also return without raising — but FastAPI will still serialize a body the client won't read; `raise HTTPException` short-circuits cleanly and keeps the `lml.bulk.batch` span as `internal_error` for filtering.

### Why this propagates cancellation correctly

1. `_run_one` (router.py:295–336) catches `Exception`, **not** `BaseException`. `asyncio.CancelledError` inherits from `BaseException` in Python 3.8+. Cancellation flows through.
2. `discogs/service.py:343-388` uses explicit `acquire()` + `try/finally release()`. The `finally` runs on `CancelledError`. Semaphore permit returns immediately.
3. `asyncio.Semaphore.acquire()` on Python 3.12 is cancellation-safe — no permit leak if cancelled mid-wait (Python 3.10+ fixed the historical leak).
4. `gather()` on a task that's cancelled raises `CancelledError`; cancelling the wrapping `gather_task` propagates `cancel()` to each child coroutine-task that `gather` spawned internally. (`asyncio.gather` wraps coroutine args in tasks at call time.)

### Telemetry

Per the acceptance criteria:
- **Sentry tag**: `lml.client_aborted=true` on the active scope (so the `lml.bulk.batch` transaction is filterable in trace explorer).
- **Span data**: `lml.bulk.client_aborted` (bool) on the `lml.bulk.batch` span.
- **Log line**: `logger.warning("bulk lookup aborted by client after %d/%d items started", started, total)` — `started` is "tasks that began executing"; cheap to compute by tracking a counter inside `_run_one` (increment before the semaphore await; this is best-effort, not load-bearing).
- **PostHog**: skip on abort. The `batch_telemetry.send_to_posthog` block at router.py:345 is for completed-batch analytics; partial-batch abort events would skew the existing `match_count` / `no_match_count` / `error_count` aggregates. If we later need per-abort counting, that's a separate event (`lookup.bulk.aborted`), not a mutilated `lookup.bulk` event.

### What we deliberately do **not** change

- `_run_one` exception isolation — unchanged. `CancelledError` is NOT caught; it propagates through `gather` and up to the `gather_task` wrapper, where we suppress it explicitly.
- `_project_cache_stats_to_transaction` — on abort, the in-process cache stats reflect partial work. That's acceptable; the existing `_project_cache_stats_to_transaction(get_cache_stats())` call won't run because we raise before reaching it. (No-op on abort is correct: the trace's `lml.cache.*` data attributes would be a partial-batch lie.)
- `discogs/service.py` semaphore code — already correct under cancellation. Adding instrumentation here belongs to LML#371.
- `perform_lookup` itself — read-path, idempotent cache writes, no transaction integrity hazard if cancelled mid-Discogs-fetch.

## Files touched

| File | Change | LoC |
|---|---|---:|
| `lookup/router.py` | Disconnect sentinel + cancel-aware gather wrapping; `lml.client_aborted` tag/span data; warning log on abort. | ~50 |
| `tests/unit/test_bulk_lookup_endpoint.py` | New `TestBulkLookupClientAbort` class — 4 tests (see plan). | ~150 |
| `tests/factories.py` | If needed: helper for a `Request` whose `is_disconnected()` flips after N polls. (Likely just inline in the new test class.) | 0–20 |

No changes to:
- `discogs/service.py` (semaphore release already correct under `CancelledError`)
- `lookup/orchestrator.py` (read-path; cancellation-safe)
- `config/settings.py` (no new knobs)
- `main.py` (no router-wiring changes)
- `pyproject.toml` (no new deps)

## TDD steps

Following the repo's TDD-required convention (LML CLAUDE.md "TDD (Required)"):

1. **Red 1 — disconnect cancels gather.**
   Test: `test_client_disconnect_cancels_in_flight_items`. Use a `perform_lookup` mock that sleeps long enough to outlast the disconnect; patch `Request.is_disconnected` with `AsyncMock` (it's awaited; a plain `Mock` returns a coroutine-less truthy that breaks `await`) so it returns `False` once then `True`; assert the handler raises `HTTPException(499)`, that the mock's `await_count` is strictly less than the input length (later items never executed), AND that `posthog_client.capture` was never called — the abort path must not emit a mutilated `lookup.bulk` event.

2. **Green 1.** Implement sentinel + `asyncio.wait` + cancel + suppress as above. Confirm Red 1 passes.

3. **Red 2 — semaphore permits released after abort.**
   Test: `test_client_disconnect_releases_semaphore_permits`. Construct an `asyncio.Semaphore(5)`; patch `get_semaphore` to return it; mock `perform_lookup` to acquire it via the real Discogs path (or, simpler: a synthetic `_run_one` that acquires the same semaphore and sleeps). After abort, assert `semaphore._value == 5`. Mark `# noqa` for `_value` access if ruff complains; the alternative is `await asyncio.wait_for(semaphore.acquire(), timeout=0.1)` × 5 which is functionally equivalent.

4. **Green 2.** Should pass without further changes — relies on `discogs/service.py:387-388`'s existing `finally semaphore.release()`. If it doesn't, that's the bug to fix.

5. **Red 3 — Sentry tag set on abort.**
   Test: `test_client_disconnect_sets_sentry_tag`. Use `sentry_sdk.start_transaction(...)` in the test harness (or `with sentry_sdk.push_scope():` and inspect after); after abort, assert the scope carries `lml.client_aborted=true`. The pattern is in the existing wxyc-fastapi test suite if we need a reference.

6. **Green 3.** `sentry_sdk.set_tag` call inside the abort branch.

7. **Red 4 — happy path unchanged.**
   Already covered by `test_happy_path_two_items` and `test_results_preserve_input_order`. Re-run; if either breaks, the wrapping introduced a regression (likely from gather-task-creation timing or the sentinel polling interfering). No new test needed unless those two pass and we want belt-and-suspenders coverage of "client stays connected, sentinel cancels cleanly on return".

8. **Refactor.** Extract `_watch_disconnect` and the abort branch into module-private helpers if the handler grows past ~40 added lines. Probably not necessary at this scope.

## Test plan summary

| Test | What it pins |
|---|---|
| `test_client_disconnect_cancels_in_flight_items` | Mid-batch disconnect aborts gather; not all items execute; PostHog not called on the abort path. |
| `test_client_disconnect_releases_semaphore_permits` | Semaphore returns to full capacity after abort. |
| `test_client_disconnect_sets_sentry_tag` | `lml.client_aborted=true` lands on the scope. |
| `test_happy_path_two_items` (existing) | No regression on connected-client path. |
| `test_results_preserve_input_order` (existing) | No regression on connected-client path with mixed match/no_match/error. |

**Out of unit-test scope** — verified via the acceptance-criteria repro instead:

Re-run BS album-level-backfill at `BACKFILL_BULK_RATE_PER_MIN=4` against staging LML; observe (via Sentry trace explorer) that per-batch wall time stays bounded across batches 17–30+ and no 30 s flat-line cliff appears. Pre-merge if possible; post-merge if staging lacks the load. The cliff is reproducible from BS so this is a real check, not theater.

## Rollout

1. PR opens against `main`. CI runs lint + typecheck + unit + pg + external_api + marker-sync.
2. Merge to `main` → auto-deploy to staging.
3. **Staging soak**: trigger BS album-level-backfill at `BACKFILL_BULK_RATE_PER_MIN=4` against staging LML for 10 minutes. Confirm in Sentry trace explorer:
   - `lml.bulk.batch` spans with `lml.client_aborted=true` appear when batches exceed BS's 30 s budget.
   - `lml.discogs.semaphore` wait spans do not exhibit the monotonic-deepening pattern from the 22:21–22:34 UTC autopsy.
4. PR to `prod` (cherry-pick or fast-forward from `main` per repo's two-branch convention).
5. **Prod observation** — 30 min after deploy: query Sentry `lml.client_aborted:true` to confirm tag is firing under live BS+rom+cron traffic; sanity-check that aborts correlate with caller-side timeouts (not new errors).

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `is_disconnected()` poll interferes with handler perf | Low | Low | 0.25 s poll is cheap; only one per request; release-resources behavior wins. |
| Cancellation propagation skips a child task (gather wraps args at creation) | Low | Medium | Test 1 directly pins this; reproduce in CI not theory. |
| Cancellation triggers a partial-state write somewhere downstream | Very low | Low | LML bulk path is read-only; cache writes are idempotent upserts. |
| Sentinel task leaks if happy path exits early | Low | Low | Happy path explicitly `sentinel_task.cancel()`; covered by existing happy-path test. |
| Staging soak doesn't reproduce because staging lacks load | Medium | Low | Fall back to prod soak with a feature flag — but we don't need a flag if unit tests + staging smoke pass; the patch surface is small and reversible. |
| Status 499 breaks a caller's response parsing | Very low | Low | BS / rom both treat any non-2xx as an error and fall back; the socket is already closed so the caller will never see the code anyway. |

## Definition of done

- All 5 tests (3 new + 2 existing) pass locally and in CI.
- `ruff check`, `ruff format --check`, `mypy` clean.
- Sentry trace explorer query `lml.client_aborted:true` returns results during staging BS-backfill soak.
- No regression in `lml.discogs.semaphore` wait p95 from pre-merge baseline (verified post-deploy).
- PR cross-references LML#372 with `Closes #372`; PR body links the autopsy comment.

## Out-of-scope follow-ups (not this PR)

- **Mitigation (2)** — admission control on semaphore queue depth (separate ticket if needed).
- **LML#370** — per-item cascade-exhaustion ceiling.
- **LML#371** — uvicorn span observability gap.
- **`lookup.bulk.aborted` PostHog event** — if we later want to count aborts per caller; separate decision.
