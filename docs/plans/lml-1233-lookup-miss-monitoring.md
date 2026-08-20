# Lookup-miss monitoring and per-commit regression attribution

Status: proposed. Target: three sequenced PRs plus one baseline wait.

## Problem

Changes to the matching algorithm can silently reduce recall, and today nothing detects that. Two distinct capabilities are missing:

1. **Detection** — no alert watches lookup misses at all. The only LML-authored PostHog insights in project `551103` are the four from LML#683/#1170 (row-less flag counters plus the all-events volume guard); everything else in that project is PostHog's default onboarding set.
2. **Attribution** — even with detection, nothing ties a miss-rate movement to the change that caused it. Events carry no deployed-commit marker, and production traffic mix moves independently of the algorithm, so a production timeseries cannot by itself implicate a commit.

This plan addresses both, and treats them as genuinely different instruments rather than one metric viewed two ways.

## Measurements this plan is built on

All figures are production `lookup_completed`, pulled 2026-08-19 ~21:09 PDT from PostHog project `551103`, scoped `environment = 'production' AND endpoint_family = 'lookup'` over a trailing 21 days unless stated otherwise. **The window is only two days** (2026-08-18 and 2026-08-19) because the LML#1170 project cutover is forward-only and ingestion was interrupted before it — see [`observability-rowless-flag.md`](../observability-rowless-flag.md) for the cutover's history boundary. Every number below is therefore directional, adequate for design but **not** adequate for threshold-setting. That gap is why Phase 2 has an explicit baseline wait.

Counts drift by a few tens of events between pulls taken minutes apart — production is serving live traffic into the same trailing window. Where two tables here disagree slightly on a total, that is the cause; the canonical denominators are the ones in the Phase 2 scoping table, taken from a single query.

| day (PT) | requests | zero-result | miss % |
|---|---|---|---|
| 2026-08-19 | 5,273 | 692 | 13.1% |
| 2026-08-18 | 381 | 21 | 5.5% |

### Finding 1 — `search_type` is not a miss signal

`search_type = 'none'` appears in **zero** production events, while zero-result responses are distributed across the labels that read as successes:

| search_type | events | of which zero-result |
|---|---|---|
| `direct` | 3,368 | 19 |
| `alternative` | 1,336 | 335 (25%) |
| `compilation` | 774 | 359 (46%) |
| `fallback` | 174 | 0 |
| `song_as_artist` | 2 | 0 |

The mechanism is `get_search_type_from_state` (`core/search.py:1045-1077`). It returns `SEARCH_TYPE_NONE` in two cases — `strategies_tried` is empty (no strategy ran at all), and the trailing fall-through at `core/search.py:1077` for a last strategy the `if`/`elif` chain does not handle. `SearchStrategyType.ARTIST_ONLY` (`core/search.py:316`) is exactly such a member; it is harmless today only because it is defined but never appended to `strategies_tried`. In every other case it returns a label derived from `state.strategies_tried[-1]`: the **last strategy attempted**, with no reference to whether that strategy produced rows. `SWAPPED_INTERPRETATION` returns `alternative` whether it matched or not; `TRACK_ON_COMPILATION` and `SONG_AS_TRACK` both return `compilation` the same way; and `found_on_compilation` short-circuits to `compilation` at the top of the function. The 335 zero-result `alternative` and 359 zero-result `compilation` events above are that behavior, not an anomaly.

So `search_type` answers "which lane ran last," never "what was found." This is the same trap recorded for BS#1359, which reads the field as a trust signal.

`none` is *reachable but rare*, and its rarity is not the point — it is emitted by `_build_timed_out_response` (`lookup/orchestrator.py:1390-1411`) and by the no-strategy-ran branch, neither of which occurred in this two-day window. (Two further `none` sites, `lookup/router.py:707` and `:830`, are 499 client-disconnect reaps that return before the telemetry send at `:798`, so those never reach PostHog at all.) An earlier draft of this plan claimed `none` was unreachable by construction; that was wrong, and the correct mechanism above is the stronger argument anyway.

**Consequence: the miss predicate is `results_count = 0`, and no part of this plan may key off `search_type` — neither to define a miss nor to exclude one.**

### Finding 2 — a miss cannot currently be separated from a shed

`LookupResponse` already models the distinction the metric needs. From `generated/api_models.py`, `degraded`'s own description states the contract explicitly: a genuine no-match is `results` empty **with** `degraded: false` and `timeout: false`.

| field | meaning | in PostHog today |
|---|---|---|
| `timeout` | internal hard cap fired, pipeline abandoned mid-execution (LML#370) | **no** |
| `degraded` | enrichment tail deliberately shed (LML#930) | **no** |
| `degraded_reason` | `deadline_exceeded` / `cache_only` / `upstream_unavailable` | **no** |
| `song_not_found` | fell back to artist-only; track never confirmed | **no** |
| `found_on_compilation` | track matched on a compilation | **no** |

The emitted property set is only `results_count`, `search_type`, `had_artist`, `had_album`, `had_song`, `reconciled_identity_count`, `endpoint_family`, `low_priority`, `environment`, `caller_reason` (`lookup/router.py:800-816`).

So today a recall regression and a Discogs outage produce an identical `results_count = 0` series. An alert on raw miss rate would page for infrastructure and stay silent on the algorithm — the precise inversion of what is wanted. This is the same "silent by design" failure class that motivated LML#683.

### Finding 3 — traffic mix dominates the global rate

| `low_priority` | caller | requests | miss % |
|---|---|---|---|
| `true` | `flowsheet-no-match-recheck` | 800 | **42.2%** |
| `true` | `flowsheet-metadata-backfill` | 2 | 0.0% |
| `false` | *(none — interactive)* | 3,557 | 10.4% |
| `false` | `library-enrich-artwork` | 1,325 | **0.4%** |
| `false` | `library-rotation-picker` | 6 | 16.7% |
| `false` | `proxy-album-metadata` | 3 | 33.3% |

`flowsheet-no-match-recheck` re-asks about entries that already failed to match, so a high miss rate there is the workload behaving correctly. It is ~14% of requests at ~42% miss, contributing roughly 6 points of the 13.1% global rate on its own. A global alert would mostly detect whether a backfill drain ran that day.

The dilution runs the other way too: `library-enrich-artwork` is 1,325 requests (~23%) at 0.4% miss. Its volume tracks Backend-Service enrichment activity, not anything about the algorithm, so letting it into the denominator means a BS backfill starting or stopping moves the alert's metric on its own.

**`low_priority` cleanly separates the drains, and only the drains.** The property is already emitted (`lookup/router.py:812`, derived from `X-Caller-Class`) and `low_priority = true` captures exactly `flowsheet-no-match-recheck` and `flowsheet-metadata-backfill`. **It does not capture `library-enrich-artwork`**, which arrives as `low_priority = false` — so `low_priority` alone is not a sufficient scope, and a caller exclusion is still required on top of it. Scoping on `low_priority` rather than denylisting the two drain callers by name is nonetheless the better primary filter: it is an existing dimension that classifies future drains automatically instead of requiring the list to be updated each time one appears.

**Consequence: segmentation is the metric, not a refinement of it.** The alert scopes to `low_priority = false` **and** excludes non-interactive enrichment callers by name.

### Finding 4 — deploy attribution is nearly free

CI already bakes the SHA into the image — `.github/workflows/ci.yml:221` and `:348`, `echo "$COMMIT_SHA_VALUE" > COMMIT_SHA` with the SHA passed through `env:` rather than interpolated into the run body (a deliberate no-expressions-in-run-bodies property; preserve it if these steps are touched) — and `routers/health._resolve_commit_sha()` already resolves it (file → `RAILWAY_GIT_COMMIT_SHA` → `None`). It simply never reaches an event. Attaching it makes "miss rate per deployed commit" a PostHog breakdown rather than an eyeball correlation against deploy annotations.

### Finding 5 — the Layer 3 harness already exists in miniature

`tests/e2e/conftest.py` wires a full FastAPI app against an in-memory SQLite library (10 seeded WXYC items) plus an `AsyncMock` Discogs service returning realistic `ReleaseMetadataResponse` objects. That is exactly the shape a golden corpus needs, at 10 cases instead of hundreds, hand-picked instead of prod-sampled.

This matters because the obvious alternatives are all blocked: `library.db` is gitignored (`.gitignore:31`) and absent in CI; the `pg` marker tier needs a PostgreSQL service and the local discogs-cache on `:5433` is empty; and `external_api` tests hit live Discogs, which is neither deterministic nor rate-limit-safe per-PR. A checked-in, self-contained corpus in the e2e style runs in the **default** CI job with no service dependencies.

## Design

Three layers, deliberately different instruments:

- **Layer 1 (telemetry)** makes a miss legible — separating "did not find it" from "did not finish."
- **Layer 2 (production alert)** detects that the attributable miss rate moved.
- **Layer 3 (CI corpus)** attributes a change in behavior to a specific commit, pre-merge.

Layer 2 is lagging and confounded; it answers *did something break*. Layer 3 is deterministic and per-commit; it answers *which change broke it*. Layer 3 additionally catches the failure mode `results_count` structurally cannot see — a lookup returning **wrong** rows rather than none, the LML#717 / LML#1225 class.

### Miss taxonomy

One derived property, computed at the emit site from fields already on the response:

| `miss_kind` | condition | attributable to the algorithm? |
|---|---|---|
| `hit` | `results_count > 0` | — |
| `miss_timeout` | `results_count = 0` and `timeout` | no — hard cap fired |
| `miss_degraded` | `results_count = 0` and `degraded` | no — tail shed |
| `miss_clean` | `results_count = 0`, not degraded, not timed out | **yes** |

Precedence is `timeout` → `degraded` → `clean`, so an infrastructure cause always wins over the residual bucket and `miss_clean` cannot absorb a shed. `miss_clean` is the only series Layer 2 alerts on.

`song_not_found` is emitted alongside but deliberately **not** folded into `miss_kind`: a response with rows but an unconfirmed track is a partial-recall signal, tracked as its own series rather than collapsed into a binary that would then mean two things.

**That companion series must be scoped, not read raw.** `song_not_found=True` has two unrelated producers: the artist-only fallback (rows present, track unconfirmed — the partial-recall meaning) and `_build_timed_out_response` (`lookup/orchestrator.py:1390-1411`), which returns empty `results` with `song_not_found=True` **and** `timeout=True` after a spine-deadline trip. A raw series over the field would therefore mix partial recall with deadline trips — reintroducing exactly the conflation Finding 2 exists to remove. Scope the partial-recall series to `miss_kind = 'hit'`, and record the confound in the runbook.

## Phase 1 — telemetry enrichment (PR 1)

Add to the `lookup_completed` property dict at `lookup/router.py:800`:

- `commit_sha` — the deployed SHA (see the extraction below).
- `degraded`, `degraded_reason`, `timeout`, `song_not_found`, `found_on_compilation` — read off the `response` object already in scope.
- `miss_kind` — derived per the table above.

### The derivation lands in a new module, not in the router

`lookup/router.py` is **1,223 lines against a 1,250 ceiling** (`tests/unit/test_module_budgets.py:236`) — 27 lines of headroom. A `miss_kind` derivation written inline at the emit site would consume most or all of it and trip the budget guard.

Put the derivation in a new `lookup/miss_kind.py`, following the established carve-out precedent of `lookup/endpoint_family.py` and `lookup/server_timing_legs.py`. Only the dict keys land in `router.py`. The new file **must** get a sized `MODULE_BUDGETS` entry in the same PR: the guard's second arm fails any unbudgeted `lookup/**/*.py` file with "has no entry in MODULE_BUDGETS" (`tests/unit/test_module_budgets.py:354-360`).

### `commit_sha` resolution — extract uncached, cache at the call site

`_resolve_commit_sha()` does file I/O. Calling it per lookup would put a synchronous read on the hot path, which is the class of tax LML#949 removed. But caching it inside the shared helper would silently change `/health` semantics: `routers/health.py:247` calls it **per request**, and `scripts/reconcile_deploy_via_health.sh` reads that field during the deploy gate (`ci.yml:290-295`), where a stale cached value would be actively wrong. `tests/unit/test_health_router.py` also references `_resolve_commit_sha` at 18 sites and redirects it by monkeypatching `health.COMMIT_SHA_PATH`.

So: extract an **uncached pure function** into `core/` (naturally `core/build_info.py` — `core/` sits outside the `lookup/**` budget glob), have `routers.health._resolve_commit_sha` delegate to it, and bind the value **once at module level in the lookup emit site's module** so the hot path pays nothing.

**The delegation must thread the path explicitly.** The existing seam is that `COMMIT_SHA_PATH` is read from the `health` module global *at call time* — `routers/health.py:86-90` resolves it inside the function body specifically so tests can redirect it, and the docstring says so. If the core function resolves its own default path instead, all 18 `monkeypatch.setattr(health, "COMMIT_SHA_PATH", ...)` sites in `tests/unit/test_health_router.py` silently stop taking effect while still passing. Give the core function a required path parameter, and have `health._resolve_commit_sha` pass its module global through on every call.

### Bulk: `commit_sha` only

Do **not** mirror the outcome fields onto `/lookup/bulk` (`lookup/router.py:1190`). That payload is batch-level (`batch_size`, `match_count`, `no_match_count`, `error_count`) and there is no `response` in scope to read them from — per-item `LookupResponse`s are consumed into `BulkLookupResultItem.status` inside `_run_one`, and the per-item `RequestTelemetry` is deliberately never sent (`lookup/router.py:1049-1059`). Bulk already carries `no_match_count`, and Layer 2 scopes to `endpoint_family = 'lookup'` regardless, matching the LML#683 and LML#985 precedent of excluding bulk from per-request-rate alerting.

Add `commit_sha` there for deploy attribution and stop. Per-item bulk outcome aggregates are a real possibility later, but they need a named consumer first; nothing in this plan reads them.

**Property-count check.** Adding six properties to the highest-volume event is a billing-surface change. It does not affect the event *count*, which is what the `jVDPlcf5` volume guard and the `POSTHOG_RATELIMIT_EVENTS_PER_MINUTE=60` capture limiter bound, so the LML#1170 quota posture is unchanged. Confirm during implementation that no new event is introduced — this phase adds no `capture_unsampled_counter` call, so `POSTHOG_RATELIMIT_EXEMPT_EVENTS` and its CI guard (`tests/unit/test_unsampled_counter_documented.py`) are untouched. That exemption list is degradation-counters-only and is not to be re-litigated here (LML#1217/PR#1226).

**Docs.** `docs/architecture.md`'s "Key Files" list enumerates every `lookup/*.py` module, and `tests/unit/test_module_budgets.py:13-14` explicitly ties the `core/` budget opt-in to that map. Add an entry for `lookup/miss_kind.py` and one for `core/build_info.py` in this PR, not later.

**TDD.** Unit tests over the `miss_kind` derivation first — one per taxonomy row plus precedence cases (`timeout` and `degraded` both set → `miss_timeout`), then the emit-site assertion that all six properties reach the captured payload, then the implementation.

## Phase 2 — production alert (PR 2, gated on a baseline wait)

**`miss_clean` is not yet a pure signal — read this before fitting a threshold (LML#1236).**

PR 1's `miss_kind` classifies from `timeout` / `degraded`, and those are narrower than "something upstream failed": `degraded` comes only from `BreakerOpenError` via `state.upstream_shed`, while an ordinary Discogs 5xx is swallowed in `discogs/service.py` and in `lookup/orchestrator.py`'s step-2 track lookup (which catches bare `Exception`, so a step-2 `BreakerOpenError` never reaches step 3's catch boundary either). Those land in `miss_clean`.

Two consequences for this phase. First, the baseline window will contain some contamination, so a threshold fitted to it is fitted to a slightly dirty series — prefer a threshold with headroom over a tight one, and re-check it after LML#1236 lands. Second, the response runbook must direct the reader to cross-check the Discogs health signals (`cache.api_calls`, the LML#683 alerts) before concluding a `miss_clean` spike is a recall regression. Ideally LML#1236 ships before the alert is enabled; if it does not, both points above are mandatory rather than advisory.

**Baseline first.** After PR 1 reaches production, accumulate **at least 7 days** of `miss_kind` data before choosing any threshold. Two days is not a baseline, and the observed 5.5% → 13.1% day-over-day swing is mostly the mix effect of Finding 3, not signal. This mirrors how LML#683's thresholds were set from a 3-day window with per-window ranges reported, not from a single point.

**Insight** (house pattern: single-row HogQL, trailing window, hourly check, email delivery):

- Metric: `miss_clean` rate = `countIf(miss_kind = 'miss_clean') / count()`.
- Scope, in order: `environment = 'production'`, `endpoint_family = 'lookup'`, `low_priority = false`, and `coalesce(properties.caller_reason, '') NOT IN ('library-enrich-artwork')`. The `environment` filter is mandatory and load-bearing — prod and staging share one PostHog key post-LML#1170, so omitting it folds a staging soak into the prod denominator. Preserve it in any future edit.

  Canonical denominators for that scope, one pull, 21 days:

  | scope | requests | misses | miss % |
  |---|---|---|---|
  | all `endpoint_family = 'lookup'` | 5,695 | — | 13.1% (global, confounded) |
  | `low_priority = false` | 4,893 | — | — |
  | **alert scope** (also excluding `library-enrich-artwork`) | **3,567** | **373** | **10.46%** |

  The alert scope is a materially steadier series than the global rate, and it is the one Phase 2's threshold is set against after the baseline wait.

  **On `coalesce`.** `caller_reason` is splatted in conditionally (`lookup/router.py:815`) and so is absent — reading as `NULL` — on most interactive events. A bare `NOT IN` **does** keep those rows: verified against production (both forms return an identical 4,893), and this is ClickHouse's documented behavior rather than a lucky accident — with the default `transform_null_in = 0` the `IN` operator treats `NULL` as an ordinary value, so `NULL NOT IN (...)` is `1`. The standard-SQL trap is a Postgres semantic that does not transfer. `coalesce` is specified anyway as the explicit form: it states the intent where it is read and does not depend on that setting staying at its default. Do not "simplify" it away, and do not re-open this a third time.
- Small-denominator guard: report healthy below a minimum request count in the window, exactly as the `MdnTb88V` hit-rate insight does at `(hits+misses) < 15`.
- Window: trailing 6h or 12h, chosen against the observed hourly volume once the baseline exists.

**Companion insights** (dashboard, not alerts): `miss_kind` breakdown over time, and `miss_clean` rate broken down by `commit_sha` — the latter is the per-deploy before/after view that Finding 4 unlocks.

**Documentation.** A `docs/observability-lookup-misses.md` runbook in the shape of `observability-rowless-flag.md`: what is watched, the baseline numbers and the window they came from, threshold rationale, and the response procedure. Add it to the CLAUDE.md topic-guide router. The response procedure differs meaningfully from LML#683's — there is no kill switch here, so it routes to "identify the commit via the `commit_sha` breakdown, then revert or fix" rather than "flip the flag."

**Sentry.** Deliberately out of scope. Sentry metric alerts cannot express a ratio (stated in the LML#683 runbook), and `miss_clean` rate is inherently a ratio. PostHog is the correct and only surface.

## Phase 3 — golden corpus in CI (PR 3)

A checked-in corpus of frozen cases, each self-contained: the query, the library rows to seed, the Discogs fixture responses, and the expected verdict.

**Sourcing.** Sample real production queries from PostHog rather than inventing them, stratified across the segments in Finding 3 so the corpus reflects the actual workload. Include known-hard historical cases as named regression cases: LML#801/#802 (artist+track recall), LML#717 (album title typed as song), LML#1225 (query-coverage gate), LML#1184 (row-less compilation). Note that `lookup_completed` carries no query text, so sourcing draws on the shapes and segments PostHog exposes plus cases named in issue history — not on replaying logged queries.

**Verdict granularity.** Assert on `miss_kind` plus the identity of the top result — not on full response equality, which would fail on every unrelated enrichment change and train everyone to re-baseline reflexively.

**Harness.** Extend the `tests/e2e` pattern: seeded in-memory SQLite plus a mock Discogs service driven from per-case fixture data. No network, no PostgreSQL, no `library.db`.

**No new CI job.** `pyproject.toml` sets `testpaths = ["tests"]` with `addopts = "-m 'not pg and not external_api'"`, so `tests/e2e` **already runs** in the default `pytest -n auto` job (`ci.yml:130`). The corpus is a parameterized test in that tier and needs no workflow change, no marker change, and therefore no interaction with the `check-ci-marker-sync` guard.

**Expectations are checked in — there is no base-commit diff.** Each case carries its expected verdict in the corpus file, so a regression is simply a failing test, attributed to the commit that failed it. An earlier draft also described the job "printing a verdict diff against the base commit," which would require a second checkout and a base-ref run; that is dropped as redundant. Checked-in expectations are what make re-baselining "an explicit, reviewable commit," which is the property this phase actually needs.

A case fails on regression: `hit` → any miss kind, or a change in top-result identity. Improvements — a case that starts passing more strongly than recorded — surface as a failure too, because the recorded expectation no longer describes behavior; the fix is a one-line corpus update in the same PR, with the reason stated. That symmetry is deliberate: a corpus that silently absorbs improvements will silently absorb regressions dressed as improvements.

**Scale and runtime.** Start at roughly 100-200 cases. Large enough to cover the segments, small enough to keep re-baselining reviewable in a diff. Watch the runtime: `tests/e2e/conftest.py`'s fixtures are all function-scoped, so N cases means N app builds and N SQLite seeds. If that proves slow, widen fixture scope (session-scoped app, per-case seeding) before trimming the corpus. The default job runs `pytest -n auto`, so every fixture must be xdist-parallel-safe and any on-disk artifact must go through `tmp_path`.

**Docs.** Add a `docs/testing.md` section describing the corpus tier, how a case is structured, and the re-baselining rule — that file is CLAUDE.md's routed home for test patterns and marker conventions, and a new test tier that is not documented there will not be found by the next person.

## Sequencing and risk

| PR | Depends on | Ships |
|---|---|---|
| 1 | — | `commit_sha` on **both** emit sites; outcome fields + `miss_kind` on `/lookup` **only** (see "Bulk: `commit_sha` only") |
| 2 | PR 1 in production + ≥7 days of data | Insight, alert, dashboard, runbook |
| 3 | PR 1 (for `miss_kind` as the shared verdict vocabulary) | Corpus, harness, CI job |

PR 3 does not depend on PR 2, so the baseline wait does not idle the work.

**Risks.**

- *Threshold set too early.* Directly mitigated by the Phase 2 gate; the failure mode is an alert nobody trusts, which is worse than no alert.
- *Corpus rot.* A corpus whose expectations are re-baselined casually stops detecting anything. Mitigated by requiring re-baselining to be its own reviewed commit with a stated reason.
- *Fixture drift.* Mocked Discogs responses will diverge from live Discogs over time. Accepted: the corpus tests **this repo's algorithm** given fixed inputs, which is precisely what per-commit attribution requires. Live-Discogs behavior stays covered by the existing `external_api` tier.
- *The caller exclusion drifts.* A new **drain** caller is handled automatically — `low_priority` classifies it from `X-Caller-Class` with no list to update. The residual exposure is a new *interactive-class* caller with an atypical miss profile, like `library-enrich-artwork` today: it would land in the denominator silently. Mitigated by documenting the exclusion and its rationale in the runbook, and by keeping the `miss_clean`-rate-by-`caller_reason` breakdown on the dashboard so a new caller's profile is visible before it distorts the alert.

## Tracking

This plan is filed as an LML issue before PR 1 opens, and the plan file is renamed to the `lml-<n>-lookup-miss-monitoring.md` convention (matching `lml-1192-…`, `802-…`) with the branch following suit. The issue number is what the runbook, the `MODULE_BUDGETS` entry comment, and the corpus header cite.

**The plan file must be committed in PR 1.** `scripts/check_plan_links.sh` resolves plan citations against the **git index**, so the moment any tracked file cites `docs/plans/…-lookup-miss-monitoring.md` — which the runbook will, per the `docs/plans/README.md` convention — the `plan-links.yml` guard fails until the file is tracked.

## Out of scope

- Sentry alerting (cannot express the ratio).
- Any change to `search_type` semantics or to the BS#1359 trust-gate question — Finding 1 records why this plan avoids the field, and BS#2217 remains the open item there.
- The `POSTHOG_RATELIMIT_EXEMPT_EVENTS` list (LML#1217 settled it; this plan adds no unsampled counter).
- Backfilling miss history — PostHog cannot move or recompute past events; the series starts at PR 1's deploy.
