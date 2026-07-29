# LML#930 PR2 — Load-adaptive enrichment shedding for low-priority lookups

## Problem (one-paragraph recap)

[LML#930](https://github.com/WXYC/library-metadata-lookup/issues/930) (epic [#1819](https://github.com/WXYC/library-metadata-lookup/issues/1819), "protect local catalog search from LML enrichment degradation") has two slices. PR1 ([#977](https://github.com/WXYC/library-metadata-lookup/pull/977), merged `383868d`) bounded the enrichment tail by the caller deadline: when the caller budget (`X-Caller-Budget-Ms`) or the universal hard cap is spent between tail steps, the remaining live-Discogs/Apple tail is shed and the response returns `degraded: true` + `degraded_reason: deadline_exceeded`. PR2 is the residual: shed low-priority batch/backfill work *before* interactive work when the lookup **event loop is saturated even while Discogs is healthy** — the loop-starvation case the [#755](https://github.com/WXYC/library-metadata-lookup/issues/755) Discogs-saturation breaker (which only fires on 429s / exhausted `X-Discogs-Ratelimit-Remaining`) structurally cannot see.

**Reframe (important — read before scoping urgency).** The dominant driver of that starvation was a per-request PostHog `flush()` blocking the event loop, fixed in [#881](https://github.com/WXYC/library-metadata-lookup/issues/881) (prod HEAD `8c83fee`, guarded by #949) — *not* fundamentally the enrichment fan-out. Prod interactive `/lookup` loop-lag p50 dropped **3941ms → 0.83ms** in the 24h after it landed (Sentry `tags[lml.cache.event_loop_lag_ms,number]`, org `wxyc`, spans dataset). So this slice is now a **safety valve** for the residual flood tail (interactive p99 still ~4.4s) and future regressions, *not* a live-fire fix. It ships **shadow-first** (measure only) precisely because it should rarely fire in the current healthy regime, and must be calibrated against a real/induced flood — never a calm window — before it is allowed to change any response.

## Goal & non-goals

**Goal.** When a **low-priority** request (`X-Caller-Class=5` single `/lookup`, or any `/lookup/bulk` item — both already set the `is_discogs_low_priority()` context var) is handled while the process event-loop-lag gauge ([#907](https://github.com/WXYC/library-metadata-lookup/issues/907)) exceeds a threshold, LML runs the (SpineDeadline-bounded) search and then **sheds the live-Discogs/Apple enrichment tail**, returning the library rows found so far with `degraded: true` + `degraded_reason: cache_only`. Interactive traffic (class 1–4 / headerless) is **never** shed. The behavior is gated behind an **enforce** flag defaulting **off**: on merge the gate only *measures* how often it *would* shed (`would_shed` telemetry), a byte-identical no-op, so the threshold can be calibrated on live traffic before enforce is ever flipped.

**Non-goals.**
- **Entry-level rejection / empty responses.** We deliberately run the search (useful library results) and shed only the enrichment tail, not the whole request. A hard reject was considered and rejected: it returns nothing to a backfill caller and forces a new `DegradedReason` (cross-repo schema change) where `cache_only` — already published and *reserved* for this path — fits.
- **A new `DegradedReason` value.** We reuse the existing, currently-unemitted `cache_only` (`generated/api_models.py`; api.yaml already says *"set by LML#930's … admission path"*). No `wxyc-shared` / api.yaml change, no codegen, no cross-repo dependency.
- **Re-implementing or modifying the #755 Discogs-saturation breaker.** Orthogonal signal (loop-lag vs Discogs 429/remaining). The two compose; #755 is untouched.
- **Changing PR1's caller-deadline tail shed.** Composes with it (see *Composition*). AC2 ("caller deadline bounds the whole spine + tail") is already satisfied by PR1.
- **Bounding the search leg itself** for low-priority requests. The search is already bounded by the #865 SpineDeadline; only the tail is shed here.
- **Making the shed a Settings field.** The threshold + enforce are runtime `os.getenv` levers (no-redeploy, like `LML_SEARCH_BUDGET_MS`), not `@lru_cache`'d Settings (which need a reboot to change).
- **BS-side consumption of `degraded`.** That is #943 / wxyc-canary#82's job; unchanged here.

## Design

### The signal — the #907 loop-lag gauge, alone

`core/event_loop_lag.py::get_event_loop_lag_ms() -> float | None` returns the process-global EWMA (α=0.3, 0.5s samples) of event-loop scheduling lag, or `None` when unsampled (sampler off, first ~0.5s of process life, or the `lml_event_loop_lag_gauge` kill switch off). This is the purpose-built "loop saturated while Discogs is healthy" signal; it is already measured on every request but consumed by no admission decision today.

**Predicate:** `low_priority and lag_ms is not None and lag_ms > threshold_ms`.

- `None` → **never shed** (fail-safe: no signal means shedding blind; this also implicitly gates the whole feature on `lml_event_loop_lag_gauge` being on, since the sampler only runs then).
- EWMA only, **no hysteresis** — the EWMA already smooths a lone GC-pause spike; a sustained high value means sustained pressure.
- **Known limitation** (inherent, same as the gauge's telemetry use): if the loop is so starved the sampler task itself cannot run, the gauge freezes stale-low and we under-shed exactly when we'd most want to. The EWMA climbs before that point; acceptable for a safety valve, documented not engineered around.

**Threshold default = 500ms**, read from `LML_ADMISSION_LOOP_LAG_SHED_MS` at request time via the shared **`core/search.py::resolve_positive_int_env`** helper (the canonical runtime-env pattern already backing `LML_SEARCH_BUDGET_MS` / `LML_LOOKUP_MAX_CONCURRENT`; gives WARN-on-junk and ≤0-rejection for free — review finding 2). Justification from the live gauge distribution (Sentry, 30d): the traffic we shed is low-priority, and its loop-lag is sharply **bimodal** — idle ~1.5ms (bulk p50–p75) vs flood ~4000ms (bulk p95+). 500ms sits in a canyon-wide empty gap: 333× above the idle floor (zero false-positive risk on quiet batch traffic) yet 8× below flood level (fires during real floods, pinning the flood equilibrium ~8× lower). The number is a starting default; shadow-mode `would_shed` telemetry recalibrates it against an induced flood before enforce.

### The gate — one check, after the search, before the tail loop

In `lookup/orchestrator.py::perform_lookup`, immediately after `search_state = await _step_search_pipeline(...)` and **before** the PR1 `tail_steps` tuple/loop:

```python
admission_reason = evaluate_admission_shed(
    low_priority=is_discogs_low_priority(),
    lag_ms=get_event_loop_lag_ms(),
)
if admission_reason is not None:      # only in ENFORCE mode; None in shadow
    return _build_degraded_response(state, search_state, degraded_reason=admission_reason)
```

`_build_degraded_response` is PR1's helper verbatim — it returns the library rows accumulated so far, no live enrichment, `degraded=True` + the reason, and stamps the filterable `lml.degraded_reason` Sentry tag. Keying on `is_discogs_low_priority()` (already set to `True` on every `/lookup/bulk` item and on class-5 `/lookup`) covers **both** endpoints with one implementation and structurally cannot fire on interactive traffic.

**Budget headroom (review finding 1):** `lookup/orchestrator.py` is 1278/1300 lines — only 22 to spare, and this repo puts a verbose rationale comment on each gate (PR1's tail block is ~17). The gate must therefore stay terse: the rationale lives in `admission.py`'s function docstrings (its own budget), and the call site is the ~5 lines above plus a one-line pointer comment. Phase 7 hard-verifies `wc -l lookup/orchestrator.py ≤ 1300`; if it can't fit honestly, recalibrate the ceiling per the module's own `test_module_budgets.py` formula and note it — never trim the gate's correctness to fit.

### Shadow vs. enforce

`evaluate_admission_shed` **always** emits `would_shed` telemetry when the predicate holds, but only **returns** a reason (i.e. actually sheds) when `LML_ADMISSION_SHED_ENFORCE` is truthy. Default off → merge is a byte-identical no-op that immediately begins collecting the live would-shed rate. This mirrors the repo's measure-first idiom (#681 pre-flip flag tags, #879 "measure first").

### Response shape — reuse `DegradedReason.cache_only`

`cache_only` ("LML served cached data without refreshing from upstream") is the honest label: we return the search result and skip the live enrichment fan-out. It is already in the published enum and emitted by nothing today. Semantic note for the reviewer: on a library *hit* the search is fully local (FTS), so "cache_only" is exact; on a library *miss* the search leg may have touched Discogs before the tail was shed — the reason names the dominant, caller-relevant fact ("enrichment was not refreshed from upstream"), consistent with how the #755 breaker degrade also runs the search then sheds the tail. Distinguishable from a hard failure by construction: `degraded=True` on a `200`, vs a `499` reap / `500`.

### Composition with PR1 (deadline shed) and #755 (breaker)

- **Enforce mode:** the admission gate is checked once, before the tail loop, so a shed short-circuits to `cache_only` and PR1's per-step `deadline_exceeded` checks never run for that request. Correct: a proactive whole-class load shed is the proximate cause.
- **Shadow mode:** the gate emits telemetry and returns `None`, falling through to the normal tail loop, so PR1's per-step deadline shed still governs (`deadline_exceeded`) exactly as today.
- **#755 breaker:** unchanged. If a tail step still raises `DiscogsBreakerOpenError`, the existing catch degrades to `upstream_unavailable`. Three orthogonal sheds — "chose not to refresh (load)" / "ran out of time (deadline)" / "couldn't ask (saturation)" — with distinct reasons.

### Monitoring surface (first-class; in shadow mode this *is* the deliverable)

Following the #683 two-channel rule (filterable **tags** + aggregatable **measurements**), mirroring `tail_deadline.py::project_tail_shed_telemetry`:

| Surface | Name | When | Purpose |
|---|---|---|---|
| tag | `lml.admission.would_shed = "true"` | predicate holds (shadow **or** enforce) | would-shed rate slice |
| tag | `lml.admission.enforced = "true"/"false"` | on a would-shed | separate shadow-observed from actually-shed |
| tag **+ PostHog property** | `lml.low_priority` = `"true"/"false"` (Sentry tag) **and** a `low_priority` PostHog property at each handler's `send_to_posthog` | **every** `/lookup` + `/lookup/bulk` request | slice the existing loop-lag measurement by the traffic we'd actually shed (class-5 single `/lookup` is otherwise invisible). Loop-lag rides `cache_stats` to **both** PostHog and Sentry, so the slice must exist on both surfaces — mirrors `endpoint_family` (Sentry tag + independent PostHog property), not a Sentry-only tag (review finding 3). |
| measurement | `lml.admission.loop_lag_ms` | on a would-shed | flood magnitude at the decision (p50/p95/p99) |
| cache_stat counter | `admission_would_shed` (seeded `0` in `_LML_CACHE_STATS_EXTRA_KEYS`) | 1 on a would-shed | alertable baseline would-shed rate, present even at 0 (like `BREAKER_OPEN_STAT_KEY`) |
| (reused) | `lml.degraded_reason = "cache_only"` | on an enforced shed | already set by `_build_degraded_response` |
| (reused) | `lml.cache.event_loop_lag_ms` | every request | **effect** metric: interactive `/lookup` p95/p99 should drop after enforce |

All projection is best-effort (swallow-and-log at WARNING), never breaking the request path.

### Folded-in PR1 nit — deadline-clamped Apple timeout ≠ Apple outage

PR1's per-item Apple probe clamps its 4s ceiling to the remaining deadline (`item.py:280`, `clamp_probe_timeout_s`). When the budget is nearly spent, the clamp returns ~0.01s, the `wait_for` fires instantly, and `enrich_one` logs a WARNING `"AppleMusicClient.<m> timed out"` per row — which reads as an Apple outage when it was self-inflicted deadline pressure. This PR is the natural home (it *is* the "honest degradation signals" slice). Fix in the `except TimeoutError` at `item.py:315`: compute `deadline_clamped = apple_probe_timeout_s < base_apple_timeout_s` (both already in scope) — when true, log at DEBUG and set `apple_music.timeout.deadline_clamped = True` on the transaction instead of the WARNING; a genuine (unclamped, full-4s) Apple timeout still WARNs as today.

**Land this as its own commit.** It touches a different module (`item.py`) and test (`test_enrichment_track_cache.py`) with no technical dependency on the admission shed, and it changes per-row hot-path logging that currently has **no** test asserting the WARNING — so it must be independently reviewable/revertable. The thematic link ("honest degradation signals") is not a technical coupling; the two changes just happen to share a PR.

### What we deliberately do **not** change

- The interactive in-flight cap (#706), bulk-global permit (#716/#953), #927 Discogs sub-semaphore, #755 breaker — all untouched; this gate sits above them as a proactive tail shed, not a new queue.
- `perform_lookup`'s signature (frozen, #722) and the search pipeline.
- The `_build_degraded_response` / `_build_timed_out_response` helpers (reused as-is).

## Files touched

- **`lookup/admission.py`** (NEW, ~120 lines — shed policy only): `should_shed_low_priority_tail` (pure predicate), `resolve_shed_threshold_ms` (delegates to `resolve_positive_int_env`, default 500 — finding 2), `is_shed_enforced` (default-off opt-in: parse via a `_TRUE_FLAG_VALUES = frozenset({"1","true","yes","on"})` — mirroring the inverse-polarity `_FALSE_FLAG_VALUES` idiom at `lookup/artist_resolution.py:41` rather than a bespoke ad-hoc set, so accepted spellings match the rest of the repo — finding 6), `evaluate_admission_shed` (orchestration → `DegradedReason.cache_only | None`, emits telemetry), `project_admission_shed_telemetry`, `ADMISSION_WOULD_SHED_STAT_KEY`. `record_low_priority_tag` is **not** here — it's a pure per-request traffic-class tag (finding 5), so it lives beside `record_endpoint_family_tag`.
- **`lookup/endpoint_family.py`**: add `record_low_priority_tag(low_priority)` (Sentry-tag helper) next to `record_endpoint_family_tag`, matching that module's shape (finding 5). Its docstring must note it is called **after `resolve_caller_class`** (router.py ~516, inside the `try`) — *not* at `record_endpoint_family_tag`'s documented `_record_event_loop_lag` sibling site (router.py:492), where `low_priority` is not yet resolved — so a future reader doesn't "align" it upward to where the value is undefined.
- **`lookup/orchestrator.py`**: import `evaluate_admission_shed`, `is_discogs_low_priority`, `get_event_loop_lag_ms`; add the one terse gate after `_step_search_pipeline` (budget-aware — see *The gate*).
- **`lookup/router.py`**: seed `ADMISSION_WOULD_SHED_STAT_KEY` into `_LML_CACHE_STATS_EXTRA_KEYS`; call `record_low_priority_tag(low_priority)` in `handle_lookup` (after the class resolve) and `handle_bulk_lookup` (`True`); **and** add a `low_priority` property to each handler's `send_to_posthog` call so the PostHog surface is sliceable too (finding 3), mirroring how `endpoint_family`/`caller_reason` set both.
- **`lookup/enrichment/item.py`**: the deadline-clamped-timeout log discrimination.
- **`docs/env-vars.md`**: `LML_ADMISSION_LOOP_LAG_SHED_MS`, `LML_ADMISSION_SHED_ENFORCE` (document the exact accepted enforce spellings alongside the sibling flags — finding 6).
- **`docs/architecture.md`**: the shed in the lookup-pipeline / degraded-response sections.
- **`tests/unit/test_admission.py`** (NEW), **`tests/unit/test_orchestrator.py`** (+cases), **`tests/unit/test_traffic_class_observability.py`** (+`lml.low_priority` tag/property wiring on both handlers + the seeded counter key — finding 4), **`tests/unit/test_enrichment_track_cache.py`** (+clamped-timeout log), **`tests/unit/test_module_budgets.py`** (+`admission.py` at a **200** ceiling — the module's own "smallest multiple of 50 at or above 1.3×size" formula for the planned ~120-line file: 1.3×120 → 156 → 200 — and re-assert orchestrator ≤ 1300).

## TDD steps

### Phase 1 — `lookup/admission.py` pure predicate + resolvers
Failing tests → implement. `should_shed_low_priority_tail`: True only when `low_priority and lag is not None and lag > threshold`; False for `low_priority=False` (any lag), `lag=None`, `lag <= threshold`. `resolve_shed_threshold_ms` default 500 + env override + junk→default; `is_shed_enforced` default False + `_TRUE_FLAG_VALUES` (`1/true/yes/on`) truthy + junk→False, mirroring `artist_resolution.py`'s `_FALSE_FLAG_VALUES` frozenset (inverse polarity).

### Phase 2 — telemetry projection + traffic-class tag
`project_admission_shed_telemetry` sets the `would_shed`/`enforced` tags + the `loop_lag_ms` measurement + bumps the `admission_would_shed` recorder; assert via a captured Sentry scope + a cache-stats recorder spy; assert it swallows an injected SDK exception (best-effort contract). In `endpoint_family.py`: `record_low_priority_tag` sets `lml.low_priority` on both branches and swallows an injected exception (matching `record_endpoint_family_tag`).

### Phase 2b — router wiring (finding 4)
In `tests/unit/test_traffic_class_observability.py` (the home of the sibling endpoint_family/caller_reason wiring tests): assert `handle_lookup` tags `lml.low_priority` with the *resolved* class value (class-5 → true, class 1–4/absent → false) **and** sets the `low_priority` PostHog property; assert `handle_bulk_lookup` tags/sets it `true` unconditionally; assert `ADMISSION_WOULD_SHED_STAT_KEY` is present in `_LML_CACHE_STATS_EXTRA_KEYS` (seeded to 0).

### Phase 3 — `evaluate_admission_shed` orchestration
would_shed + enforce → returns `DegradedReason.cache_only` **and** emitted `enforced=true` telemetry; would_shed + shadow → returns `None` **and** emitted `enforced=false` telemetry; not-low-priority / lag None / lag≤threshold → returns `None`, **no** telemetry. Parametrize by class.

### Phase 4 — orchestrator gate integration
In `perform_lookup` (mock services, monkeypatch `get_event_loop_lag_ms` + `is_discogs_low_priority` + the enforce env): low-priority + high lag + enforce → response `degraded=True, degraded_reason=cache_only`, **tail steps not awaited**, search results preserved; low-priority + high lag + shadow → tail **runs**, `degraded` stays whatever the tail decided (False on the happy path), `would_shed` telemetry present; interactive + high lag → tail runs, never shed; lag None / below threshold → tail runs.

### Phase 5 — the AC test (enrichment-heavy degrades before protected search)
One test: under an injected high gauge value, a low-priority request sheds to `cache_only` while an interactive request completes the full tail — proving low-priority degrades first and interactive is protected.

### Phase 6 — Apple-probe clamped-timeout log (separate commit)
Land this as its own commit (see *Folded-in PR1 nit*) — orthogonal module/test, no dependency on the shed. Given a spine deadline that clamps the Apple probe below its base and a client that times out: assert no `AppleMusicClient … timed out` WARNING, that `apple_music.timeout.deadline_clamped` is set, and (control) that an unclamped timeout still WARNs.

### Phase 7 — module budget + docs + pre-flight
Add `admission.py` to `test_module_budgets.py` at a **200** ceiling (its own "smallest multiple of 50 at or above 1.3×size" formula: 1.3×120 → 156 → 200); **re-run the whole module-budget test and confirm `lookup/orchestrator.py` still passes ≤ 1300** (finding 1) — recalibrate the orchestrator ceiling per the module's formula only if the terse gate genuinely can't fit. Update `docs/env-vars.md` (both knobs + the exact accepted `LML_ADMISSION_SHED_ENFORCE` spellings) + `docs/architecture.md`. Run `ruff check .`, `ruff format --check .`, `pytest -m 'not pg and not external_api and not stack and not slow'`, mypy.

## Acceptance mapping (#930)

| AC | Where |
|---|---|
| Sheds / cache-only for the least-critical class first, independent of #755 | The gate: `cache_only` for `is_discogs_low_priority()` keyed on the #907 loop-lag gauge (orthogonal to #755's Discogs signal). |
| Caller deadline bounds the whole spine + tail; tests cover the tail | **PR1** (#977). Unchanged here. |
| Explicit degraded/cache-only shape, distinguishable from a hard failure | `degraded=True` + `degraded_reason=cache_only` on a `200`; Phase 4/5 tests. |
| Admission-control test: enrichment-heavy degrades before protected search | Phase 5. |
| Does not re-implement #755; composes | *Composition* section; #755 untouched, distinct reasons. |

## Verification & rollback

- **Merge → staging** with `LML_ADMISSION_SHED_ENFORCE` unset (shadow): byte-identical response behavior; `lml.admission.would_shed` / `lml.admission.loop_lag_ms` begin populating.
- **Calibrate:** query `would_shed` rate + `loop_lag_ms` distribution across an **induced** flood (the #929 rung-a staging load-test saturating `/lookup/bulk`), *not* a calm window — the 30d↔24h swing proves calm windows are uninformative. Confirm the shed fires on the flood and never on quiet bulk; adjust `LML_ADMISSION_LOOP_LAG_SHED_MS` if the gap moved.
- **Enforce on staging** (`LML_ADMISSION_SHED_ENFORCE=true`, Railway var — no redeploy); re-run the flood; confirm low-priority items return `cache_only` while an interactive probe keeps full enrichment and its `event_loop_lag_ms` drops.
- **Enforce on prod** via the same var after staging sign-off.
- **Rollback:** unset `LML_ADMISSION_SHED_ENFORCE` (instant, no redeploy) → back to shadow; unset both to fully disable measurement.

## Risk register

| Risk | Mitigation |
|---|---|
| Threshold mis-set → sheds legitimate batch work | Shadow-first + induced-flood calibration before enforce; instant env rollback; the idle→flood gap is ~2600× wide so mis-set is unlikely. |
| Gauge freezes stale-low under extreme starvation → under-shed | Documented inherent limit; the EWMA climbs before the sampler starves; this is a safety valve, not the primary loop-health lever (#881 is). |
| `cache_only` on a library-miss whose search touched Discogs reads as a stretch | Reason names the dominant caller-relevant fact (enrichment not refreshed); documented; matches the #755 degrade shape. |
| Reduced enrichment coverage for backfills during floods | Intended; backfills are retryable (attempt-at markers) and re-enrich when the loop is calm; only fires under genuine sustained flood. |
| Multi-worker (#747) | Gauge + `is_discogs_low_priority` are per-process, so per-worker — same caveat as every sibling primitive; re-derive before `UVICORN_WORKERS>1`. |

## Cross-references

- Epic [#1819](https://github.com/WXYC/library-metadata-lookup/issues/1819) · issue [#930](https://github.com/WXYC/library-metadata-lookup/issues/930) · PR1 [#977](https://github.com/WXYC/library-metadata-lookup/pull/977)
- Signal: [#907](https://github.com/WXYC/library-metadata-lookup/issues/907) loop-lag gauge · reframe: [#881](https://github.com/WXYC/library-metadata-lookup/issues/881) flush fix
- Composes with: [#755](https://github.com/WXYC/library-metadata-lookup/issues/755) breaker · [#865] SpineDeadline · low-priority plumbing [#928]/[#953]/[#927]
- Degraded contract: [#943] (`@wxyc/shared` `degraded`/`degraded_reason`) · consumer wxyc-canary#82
- Staging validation: [#929](https://github.com/WXYC/library-metadata-lookup/issues/929) rung-a load-test
