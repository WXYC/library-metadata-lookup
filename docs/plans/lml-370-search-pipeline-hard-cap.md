# LML#370 — Hard cap on `execute_search_pipeline` wall time

## Problem (one-paragraph recap)

The wall-clock budget shipped in [LML#340](https://github.com/WXYC/library-metadata-lookup/issues/340) short-circuits `execute_search_pipeline` only when `state.results` is non-empty (`core/search.py:495`). For cascade-exhaustion queries — every strategy returns zero — the gate never fires, and the request grinds for as long as the strategies and their downstream Discogs probes will run. Observed 2026-05-24 13:21–15:52 UTC: 30 spans over 5 s on `/api/v1/lookup`, worst tail 414 s. BS's 30 s `AbortController` bounds *user-visible* latency, but LML keeps draining Discogs API quota and holding the 5-permit semaphore long after the caller has given up. Telemetry pollution (p95 = 24 s over 7 d) on top.

Issue: [LML#370](https://github.com/WXYC/library-metadata-lookup/issues/370). Sibling tickets: [#372](https://github.com/WXYC/library-metadata-lookup/issues/372) (cancel-aware bulk gather — propagates *external* client-disconnect into the bulk-route outer gather) and [#345](https://github.com/WXYC/library-metadata-lookup/issues/345) (the `X-Caller-Budget-Ms` header that #340's gate already honors). #370 sits between them: a per-lookup *internal* ceiling that fires regardless of whether anyone is still listening.

## Goal & non-goals

**Goal.** No `/api/v1/lookup` server-side wall time exceeds a configurable hard cap (default 25 s, 5 s headroom under BS's 30 s), regardless of `state.results`. When the cap fires the response carries `timeout: true` so callers can distinguish "no match" from "ran out of time." In-flight Discogs probes inside the currently-running strategy are cancelled, freeing semaphore permits before the cap-fire response hits the wire.

**Non-goals.**
- Changing soft-budget semantics (`SEARCH_BUDGET_MS`, `LML_SEARCH_BUDGET_MS`). Existing #340 behavior — "all-empty cascade keeps grinding past the soft budget" — is preserved *up to the hard cap*. The hard cap is a new, higher knob, not a replacement.
- Changing `X-Caller-Budget-Ms` semantics. The caller-budget header continues to feed `resolve_effective_search_budget_ms` for the soft gate; the hard cap is independent.
- BS-side wiring to read `LookupResponse.timeout`. Sibling ticket after this lands.
- BS-side wiring to send `X-Caller-Budget-Ms`. Already covered by [BS#876](https://github.com/WXYC/Backend-Service/issues/876) (Epic B coordinator); not in scope here.
- Bulk-route cancel-aware gather. That's #372.
- Per-caller fair queueing, admission control on queue depth, growing the Discogs semaphore. All separate; #372's plan enumerates them.

## Design

### Two-layer gate

Layer 1: **loop-level hard cap.** At the top of each loop iteration in `execute_search_pipeline` (`core/search.py:487`), before the existing soft-budget gate, check `elapsed_ms > hard_cap_ms`. If so, set `state.timed_out = True`, emit telemetry, `break`. Independent of `state.results`.

Layer 2: **per-strategy `asyncio.wait_for`.** Wrap each `await strategy.execute(...)` call in `asyncio.wait_for(coro, timeout=remaining_budget_seconds)` where `remaining_budget_seconds = max(0.01, (hard_cap_ms - elapsed_ms) / 1000)`. The minimum-0.01 floor avoids a degenerate `wait_for(coro, timeout=0)` that would `TimeoutError` synchronously.

**`try/except` scope.** The existing strategy dispatch at `core/search.py:510-559` is a per-strategy `if/elif` chain where each branch unpacks the strategy's return tuple differently (`results, fallback_used` for ARTIST_PLUS_ALBUM, `results, discogs_titles` for TRACK_ON_COMPILATION, `results, matched_via_by_id` for SONG_AS_TRACK, etc.). The cleanest scope is a single `try` wrapping the *entire* `if/elif` body and a single `except asyncio.TimeoutError` after it — not one `try` per branch. Sketch:

```python
try:
    if strategy.name == SearchStrategyType.ARTIST_PLUS_ALBUM:
        results, fallback_used = await asyncio.wait_for(
            strategy.execute(db, parsed, state.albums_for_search),
            timeout=remaining_budget_seconds,
        )
        if results:
            state.results = results
        if strategy.updates_song_not_found and fallback_used:
            state.song_not_found = True
    elif strategy.name == SearchStrategyType.SWAPPED_INTERPRETATION:
        # ...same wait_for wrap...
    elif ...
except asyncio.TimeoutError:
    state.timed_out = True
    state.strategies_timed_out.append(strategy.name)
    _log_hard_cap_fired(
        elapsed_ms=(time.monotonic() - start) * 1000,
        skipped=[s.name for s in strategies[idx + 1:]],
        timed_out_in=strategy.name,
        hard_cap_ms=hard_cap_ms,
    )
    break
```

The single-`try` shape preserves the per-branch unpacking and avoids duplicating the catch handler five times. `asyncio.TimeoutError` is the only exception caught — any other exception from a strategy bubbles to the caller unchanged (matches current behavior, since the existing loop has no exception handling).

**Why both layers.** The 414 s outlier came from one strategy running past the loop, not from the loop iterating slow strategies — option #1 from the ticket body (loop gate alone) would not have stopped it. The `wait_for` is what propagates `CancelledError` into in-flight `asyncio.gather()` probes inside `search_compilations_for_track` and `search_song_as_track`, which is what actually frees the Discogs semaphore on cap-fire. The loop gate is a cheap safety belt — it fires when a strategy *returns* between the cap and the next iteration (so `wait_for` didn't catch it) and when future strategies are added that don't go through `wait_for` for some reason.

### Why `wait_for` propagates cancellation correctly

Same reasoning as #372's plan §"Why this propagates cancellation correctly":

1. The Discogs probe functions catch `Exception`, not `BaseException`. `asyncio.CancelledError` inherits from `BaseException` (Python 3.8+). Cancellation flows through.
2. `discogs/service.py:343-388` uses explicit `acquire()` + `try/finally release()`. The `finally` runs on `CancelledError`. Semaphore permits return immediately.
3. `asyncio.Semaphore.acquire()` on Python 3.12 (per `pyproject.toml`) is cancellation-safe — no permit leak if cancelled mid-wait (fixed in 3.10+).
4. `asyncio.wait_for(coro, timeout=...)` wraps the coro in a Task and cancels it on timeout; cancelling a `gather()`-wrapping task propagates `cancel()` to each child task.

### Interaction with the soft budget

Both gates fire on the same loop. Order matters:

```python
elapsed_ms = (time.monotonic() - start) * 1000
if elapsed_ms > hard_cap_ms:
    # Hard cap: fire regardless of state.results.
    state.timed_out = True
    _log_hard_cap_fired(...)
    break
if elapsed_ms > budget_ms and state.results:
    # Soft budget: unchanged from #340.
    _log_search_budget_exceeded(...)
    break
```

The two telemetry hooks are independent — a single iteration can't fire both because the first `break` exits. The `timed_out` field is set only by the hard cap.

### Interaction with `caller_budget_ms`

Unchanged. `resolve_effective_search_budget_ms(caller_budget_ms)` continues to compute the soft budget per #340 + #345. The hard cap is computed separately via `resolve_search_hard_timeout_ms()` (env var only — callers cannot raise the hard cap via header; that would defeat the safety floor).

In practice: a caller with a 5 s budget sees the soft gate fire at ~4.8 s if it has results, or the hard cap fire at 25 s if all strategies are empty. A caller with no budget header sees the env-default soft budget (4 s) or the hard cap (25 s).

### Response shape

Add `timeout: boolean` to `LookupResponse` in `wxyc-shared/api.yaml` (default `false`, additive). In `lookup/orchestrator.py:perform_lookup`, project `search_state.timed_out` into the response construction site at line ~1957 onward. One-line change at the construction site.

### Knob inventory

| Name | Default | Override | Purpose |
|---|---|---|---|
| `SEARCH_HARD_TIMEOUT_MS` (new constant) | 25000 | `LML_SEARCH_HARD_TIMEOUT_MS` env var | Loop-level hard cap + per-strategy `wait_for` timeout |
| `SEARCH_BUDGET_MS` (existing #340) | 4000 | `LML_SEARCH_BUDGET_MS` | Unchanged. Soft gate for "have results, next strategy marginal." |
| `TRANSPORT_OVERHEAD_MS` (existing #345) | 200 | code constant | Unchanged. Soft-gate caller-budget overhead. |

No new env vars beyond the one hard-cap knob. Rollback: `LML_SEARCH_HARD_TIMEOUT_MS=600000` effectively disables the cap (10 min — well past any caller timeout).

### What we deliberately do **not** change

- `SEARCH_BUDGET_MS`, `LML_SEARCH_BUDGET_MS`, `resolve_search_budget_ms()`, `resolve_effective_search_budget_ms()`, `_log_search_budget_exceeded()` — all untouched.
- `caller_budget_ms` header parsing in `lookup/router.py:118, 241`.
- `discogs/service.py` semaphore code — already correct under cancellation (per #372's analysis).
- `lookup/orchestrator.py:perform_lookup` aside from the one-line `timeout=` projection.
- `SearchState.results` semantics. When the hard cap fires, `state.results` reflects whatever the last completed strategy produced (possibly empty); we don't clear it.

## Files touched

| Repo | File | Change | LoC |
|---|---|---|---:|
| `wxyc-shared` | `api.yaml` | Add `timeout: boolean` (default `false`) to `LookupResponse`. Bump api.yaml version per repo convention. | ~5 |
| `library-metadata-lookup` | `core/search.py` | New `SEARCH_HARD_TIMEOUT_MS` constant + env var + `resolve_search_hard_timeout_ms()`. New `SearchState.timed_out` + `strategies_timed_out` fields. New `_log_hard_cap_fired()` Sentry projection. Loop gate + per-strategy `asyncio.wait_for` in `execute_search_pipeline`. | ~80 |
| `library-metadata-lookup` | `generated/api_models.py` | Regenerated from updated `api.yaml`. | mechanical |
| `library-metadata-lookup` | `lookup/orchestrator.py` | One-line: `timeout=search_state.timed_out` in `LookupResponse(...)` construction. | 1 |
| `library-metadata-lookup` | `tests/unit/test_search.py` | New test class covering env-var resolution, hard cap with empty results, soft/hard independence, Sentry projection, `wait_for` cancellation of in-flight `gather`. | ~200 |
| `library-metadata-lookup` | `tests/integration/test_api_lookup_hard_timeout.py` | New: TestClient POST `/api/v1/lookup` with mocked sleeping Discogs, assert `timeout: true` on response. **Unmarked** (no `pg` or `external_api`) — pure TestClient + AsyncMock, runs in the default suite. Filename matches the existing `test_api_*.py` convention. | ~80 |
| `library-metadata-lookup` | `CLAUDE.md` | Document the new env var alongside `LML_SEARCH_BUDGET_MS`. | ~5 |

No changes to:
- `lookup/router.py` — `caller_budget_ms` plumbing already exists, no signature change.
- `discogs/service.py` — semaphore semantics already correct.
- `core/dependencies.py`, `main.py`, `pyproject.toml`.

## TDD steps

Order matters; each step is red-green-refactor.

### Phase 0: wxyc-shared (separate PR, lands first)

0a. Start worktree in wxyc-shared. Red: contract test that `LookupResponse(timeout=True).model_dump()` includes the field; default `False`.
0b. Green: add `timeout: boolean` to `LookupResponse` in `api.yaml` with description. Bump `info.version` from `1.6.0` → `1.6.1` (patch for additive optional field — wxyc-shared CLAUDE.md Tag Stability Policy permits patch bumps for additive non-breaking changes).
0c. Run TypeScript codegen, ship PR. Wait for merge + npm publish.

### Phase 1: LML — env-var resolution

1a. Bump `@wxyc/shared` dep in this repo, run `bash scripts/generate_api_models.sh`, commit the regenerated `generated/api_models.py`.
1b. Red: parameterized test for `resolve_search_hard_timeout_ms` — default-when-unset, valid-int, non-int → WARN + default, non-positive → WARN + default. Clone the `resolve_search_budget_ms` test fixture exactly.
1c. Green: add `SEARCH_HARD_TIMEOUT_MS = 25000`, `SEARCH_HARD_TIMEOUT_ENV_VAR = "LML_SEARCH_HARD_TIMEOUT_MS"`, `resolve_search_hard_timeout_ms() -> int` — copy-paste-rename of the existing `resolve_search_budget_ms()` resolver. **Signature: zero parameters** (env-var only). Unlike `resolve_effective_search_budget_ms(caller_budget_ms)`, the hard cap deliberately ignores `X-Caller-Budget-Ms` — callers cannot raise the safety floor via header. The docstring must include a one-line contrast: "Unlike `resolve_effective_search_budget_ms(caller_budget_ms)`, this resolver is env-var-only; callers cannot override the hard cap via HTTP header." Verified no `SEARCH_HARD_TIMEOUT` collision in the repo before adding.

### Phase 2: LML — loop-level hard cap

2a. Red: test that `execute_search_pipeline` returns within hard-cap + 100 ms with `state.timed_out=True` and `state.results == []` when every strategy is mocked to `await asyncio.sleep(10)` and return `([], None)`. Set hard cap to 1 s via monkeypatched env var to keep the test fast.
2b. Green: add `timed_out: bool = False` and `strategies_timed_out: list[SearchStrategyType] = field(default_factory=list)` to `SearchState`. Add the hard-cap gate at the top of the loop, before the existing soft-budget gate.
2c. Red: test that `_log_hard_cap_fired` projects `lml.hard_cap_fired=True` + `hard_cap_skipped_strategies=[...]` + `hard_cap_elapsed_ms=...` onto the active Sentry transaction. Clone the existing `_log_search_budget_exceeded` Sentry test. Also: no-op when no transaction.
2d. Green: add `_log_hard_cap_fired()` — clone of `_log_search_budget_exceeded()` with the new attribute names.

### Phase 3: LML — per-strategy `wait_for`

3a. Red: test that a strategy hanging in `asyncio.gather(probe_a(), probe_b(), probe_c())` for 60 s gets cancelled when hard cap is 1 s; all three probes record their own `CancelledError` in a shared list. Asserts that cancellation actually propagated into the inner gather, not just that the strategy was abandoned. This is the load-bearing test — without it the fix improves wall time but not the Discogs semaphore queue depth. Test sketch:

```python
async def test_wait_for_propagates_into_inner_gather(monkeypatch):
    monkeypatch.setenv("LML_SEARCH_HARD_TIMEOUT_MS", "1000")
    cancelled: list[str] = []

    async def slow_probe(name: str) -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(name)
            raise  # re-raise so gather() sees the cancellation

    async def fan_out_strategy(*_args, **_kwargs):
        await asyncio.gather(slow_probe("a"), slow_probe("b"), slow_probe("c"))
        return ([], None)  # unreachable

    strategies = [SearchStrategy(
        name=SearchStrategyType.ARTIST_PLUS_ALBUM,
        condition=lambda *_: True,
        execute=fan_out_strategy,
    )]
    parsed = ParsedRequest(artist="x", song=None, album=None)

    start = time.monotonic()
    state = await execute_search_pipeline(parsed, mock_db, "x", strategies)
    elapsed = time.monotonic() - start

    assert elapsed < 1.5  # cap + headroom
    assert state.timed_out is True
    assert set(cancelled) == {"a", "b", "c"}  # all three inner probes cancelled
```

The `re-raise` after recording cancellation is load-bearing: `asyncio.gather` only sees the cancellation if children re-raise `CancelledError`. Swallowing it would mask the bug.
3b. Green: wrap `await strategy.execute(...)` calls in `asyncio.wait_for(coro, timeout=remaining_budget_seconds)`. Catch `asyncio.TimeoutError`, set `state.timed_out=True`, append to `strategies_timed_out`, `break`.
3c. Red: test that `strategies_timed_out` is populated correctly when a strategy times out mid-pipeline (one strategy completes fast, second strategy hangs and is cancelled, third strategy doesn't run because we break).
3c′. Red: edge case — `elapsed_ms` overshoots `hard_cap_ms` between the loop-gate check and `wait_for` (simulate with a synchronous `time.sleep` in the strategy condition function, or monkeypatch `time.monotonic` to jump). Confirm `wait_for(0.01)` raises `TimeoutError` cleanly and the same `except` handler catches it — same end state as a normal timeout, no special-case path needed.
3d. Refactor: extract the per-strategy execution into a small helper if the loop body grows past ~40 lines (defer if not).

### Phase 4: LML — independence regression tests

4a. Red: test that the soft budget continues to short-circuit normally when results exist and hard cap is much higher (1 s soft, 25 s hard, strategies return results in 50 ms each but the second strategy sleeps 2 s — pipeline short-circuits on soft budget with `timed_out=False`). Guards against accidental collapse of the two knobs.
4b. Red: confirm the existing #340 test "all strategies empty + slow strategy still runs" passes with the new defaults (hard cap 25 s is well above the test's mock-strategy timing).
4c. Green: no code change expected — these are pinning tests for the design.

### Phase 5: LML — response-shape projection

5a. Red: integration test in `tests/integration/test_lookup_hard_timeout.py` — TestClient POST `/api/v1/lookup` against a real router with `DiscogsService` mocked to sleep on every call. Assert HTTP 200, body validates against the regenerated `LookupResponse` model, body has `timeout: true`, `results: []`, response arrives within hard cap + 1 s.
5b. Green: one-line change in `lookup/orchestrator.py:perform_lookup` to pass `timeout=search_state.timed_out` into the `LookupResponse(...)` construction.

### Phase 6: Docs + pre-flight

6a. Update `CLAUDE.md` Environment Variables section: add `LML_SEARCH_HARD_TIMEOUT_MS` row to the Optional list, mirroring the `LML_SEARCH_BUDGET_MS` entry already documented in `core/search.py` module docstring.
6b. Local CI mirror: `ruff check .`, `ruff format --check .`, `mypy .`, `uv run pytest -m "not pg and not external_api" -v`. (No PG/external-API tests added.)
6c. `actionlint .github/workflows/*.yml` — no workflow changes, expected clean.
6d. Push, open PR with `Closes #370` in the body, "Depends on" cross-reference to the wxyc-shared PR.

## Acceptance mapping

| Ticket AC | Covered by |
|---|---|
| No `/api/v1/lookup` request exceeds `SEARCH_HARD_TIMEOUT_MS` regardless of `state.results` | Phase 2 (loop gate) + Phase 3 (`wait_for` for in-strategy tails). Tested in 2a + 3a. |
| Response shape includes `timeout: true` when hard cap fires | Phase 0 (api.yaml) + Phase 5 (orchestrator projection). Tested in 5a. |
| Telemetry: `lml.hard_cap_fired` on the Sentry span | Phase 2 (`_log_hard_cap_fired`). Tested in 2c. |
| Unit test: cascade × empty × 10s-per-strategy + 25s cap → returns ~25s with `timeout=true` and empty `results` | Phase 2 test 2a (with monkeypatched cap = 1 s for speed; same shape). |
| Re-measure: no `/api/v1/lookup` server-side span >30 s in a 6h window on prod | Post-deploy Sentry trace explorer check. Not a CI gate. |

## Verification & rollback

- **Pre-deploy:** all unit + integration tests pass locally and in CI; ruff + mypy clean; the regenerated `generated/api_models.py` diff is mechanical and matches the api.yaml change.
- **Post-deploy:** query Sentry trace explorer for `lml.lookup` transaction spans over a 6 h window; assert max < 30 s and `lml.hard_cap_fired:true` count matches the expected cascade-exhaustion volume from the ticket's 30-span sample.
- **Rollback (no deploy):** set `LML_SEARCH_HARD_TIMEOUT_MS=600000` in Railway. The cap is effectively disabled; behavior reverts to current #340 + #345.
- **Rollback (deploy):** revert the LML PR. The wxyc-shared `timeout` field stays in the contract — it's additive and harmless to existing consumers that don't read it.

## Risk register

| Risk | Mitigation |
|---|---|
| `asyncio.wait_for` cancellation leaks a Discogs semaphore permit | Already verified by #372's analysis (acquire/release in `try/finally`). Add test 3a to pin the behavior. |
| Hard cap of 25 s is too tight — legitimate cascade-exhaustion queries that *would* find a match in 30 s now return empty with `timeout: true` | Env-var tunable; can bump without deploy. Initial 25 s leaves 5 s headroom under BS's 30 s, and the ticket's 30-span sample shows tails of 48–414 s — the cap catches *those*, not legitimate slow-but-eventual successes. |
| New `wait_for` wrapper changes exception surface — strategies that previously raised `asyncio.TimeoutError` now get caught here instead of bubbling | Audit strategy implementations for explicit `TimeoutError` raises. None expected (Discogs probes raise `httpx.TimeoutException` which is unrelated to `asyncio.TimeoutError`). Confirm in Phase 3 review. |
| BS's 30 s `AbortController` fires before LML's 25 s hard cap on a query where LML *would* have completed at 28 s — caller gets `499` from BS, not `timeout: true` from LML | Acceptable. The 5 s headroom is chosen so this only happens for queries that *also* would have hit the hard cap shortly after. Future tuning can narrow the headroom if BS's coordinator (#876) normalizes timeouts. |

## Cross-references

- [LML#338](https://github.com/WXYC/library-metadata-lookup/issues/338) — Epic A parent.
- [LML#340](https://github.com/WXYC/library-metadata-lookup/issues/340) — A3, predecessor. The fix here closes the documented gap in #340's `and state.results` clause.
- [LML#345](https://github.com/WXYC/library-metadata-lookup/issues/345) — A8, predecessor. The caller-budget header that drives the soft gate; unchanged.
- [LML#372](https://github.com/WXYC/library-metadata-lookup/issues/372) — sibling, in-flight in `.worktrees/lml-372-cancel-aware-gather`. Cancel-aware *bulk* gather (external client-disconnect). Layered with #370's per-strategy `wait_for` (internal cap on a single lookup's cascade). Different mechanisms, same end-effect on Discogs semaphore queue depth.
- [BS#873](https://github.com/WXYC/Backend-Service/issues/873) — paired BS-side mitigation (30 s `AbortController` stop-gap). The 25 s hard cap is sized for this; if BS#876 normalizes BS's timeout, the hard cap should move with it.
- BS sibling ticket (to be filed) — wire `shared/lml-client/src/index.ts` to read `LookupResponse.timeout` in the catch-arm so BS can distinguish "no match" from "ran out of time."
