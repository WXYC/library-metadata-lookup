# Row-less flag observability & alerts (`LML_RESOLVE_NONLIBRARY_RELEASE`)

Runbook for the automated degradation alerts on the `LML_RESOLVE_NONLIBRARY_RELEASE` row-less carry-through feature (LML#628), wired in [LML#683](https://github.com/WXYC/library-metadata-lookup/issues/683). The counters and flag tag these alerts consume are emitted by [LML#681](https://github.com/WXYC/library-metadata-lookup/issues/681) through the existing `cache_stats` seam — see the `LML_RESOLVE_NONLIBRARY_RELEASE` entry in [`env-vars.md`](env-vars.md) for the emit side. This doc is the **consume** side: what is watched, the thresholds and why, and what to do when one fires.

## Why this exists

The feature's worst failure mode is **silent by design**. The row-less resolve path swallows its Discogs probe errors (returns `[]`/`None`), so a Discogs outage shows up as *fewer* row-less surfaces, **not** an error spike in Sentry — LML#681 explicitly scoped silent-degradation alerting *out*. The only machine-readable signals are the LML#681 counters, and before LML#683 nothing watched them. The cold-path Discogs cost is also the exact sensitivity behind the 2026-05-13 regression ([LML#337](https://github.com/WXYC/library-metadata-lookup/issues/337)): a call-rate spike here can degrade `/lookup` latency for the whole live stack. These alerts make a Discogs call-rate spike, a positive-cache hit collapse, or a Postgres cache outage **page automatically** instead of being noticed late or never.

## Signals

All ride the per-request `cache_stats` dict (seeded in `_LML_CACHE_STATS_EXTRA_KEYS`, `lookup/router.py`). PostHog receives them as the `cache` property of the `lookup_completed` event (`cache.<key>`); Sentry receives them as transaction measurements (`lml.cache.<key>`, projected by `_project_cache_stats_to_transaction`, `traces_sample_rate=1.0`).

| Key | Meaning | Degradation it proves |
|-----|---------|-----------------------|
| `api_calls` | Discogs API calls made on the request | Call-rate spike → `/lookup` latency risk (the LML#337 cost guard; primary rollback trigger) |
| `release_resolution_cache_hit` / `_miss` | LML#632 positive-cache outcome (`get_cached_release_id`) | Miss climbing while hits stop = swallowed-error **Discogs outage** proxy |
| `release_resolution_cache_unavailable` | PG-exception branch of the positive cache | Sustained > 0 = **Postgres positive-cache outage** (direct signal) |
| `lml_resolve_nonlibrary_release` | Flag state, `0`/`1`, recorded once per context | Used to **scope every alert to flag-on traffic** |
| `nonlibrary_release_surfaced` | Flag-gated row-less `id=0` emissions | Context (feature actually surfacing results); not alerted |

## Baseline (the numbers the thresholds are set from)

Telemetry went live in prod 2026-06-27. Baseline pulled 2026-06-30 over the **3-day flag-on window** (`lml_resolve_nonlibrary_release=1`), `lookup_completed` only (bulk excluded — on `/lookup/bulk` the counters are batch totals across up to 100 items). The flag is fully enabled, so 100% of `lookup_completed` traffic is flag-on (7,560 requests).

**PostHog population (`lookup_completed`, flag-on, 3d):**

- `api_calls` per request: avg **0.75**, p50 0, p90 2, p95 3, p99 9, max 36. **73%** of requests make **zero** Discogs calls (library/cache hit). Trailing-6h average ranges **0.41–1.71** (1.71 was a cold-start morning).
- Positive cache: hits **163**, misses **228**, **unavailable 0 in every one of the 72 hours**. Hit fraction `hits/(hits+misses)` ≈ **0.42** overall; **0.31–0.61** across trailing-12h windows (warming upward).
- `nonlibrary_release_surfaced`: 291 (feature working).

**Sentry population** (all transaction types, EAP spans, 18,795 spans/3d — more diluted than PostHog because it includes non-lookup transactions): avg `api_calls` **0.483** (hourly avg maxes at **1.27**), `release_resolution_cache_miss` **0.010**, `release_resolution_cache_unavailable` **0.0**.

## PostHog alerts — project [Request-O-Matic](https://us.posthog.com/project/293013) (id 293013)

Backed by single-row HogQL insights (trailing-window, flag-on, `lookup_completed` only), checked **hourly**, delivered by email subscription (no Slack integration configured on this project yet — see [Routing](#routing)). [Alerts tab](https://us.posthog.com/project/293013/insights?tab=alerts).

| Alert | Metric (window) | Condition | Rationale | Insight |
|-------|-----------------|-----------|-----------|---------|
| **Discogs call-rate** | avg `api_calls`/req (trailing **6h**) | **> 3.0** | ~3–4× the 0.72–0.95/day baseline and 1.75× the worst observed 6h window (1.71). A sustained 6h avg > 3 means per-request Discogs fan-out roughly quadrupled and held — the LML#337 cold-cache signature. | [BcOGYggg](https://us.posthog.com/project/293013/insights/BcOGYggg) |
| **Hit-rate collapse** | `hits/(hits+misses)` (trailing **12h**, guarded to `(hits+misses)≥15` else reports healthy) | **< 0.15** | Baseline 0.31–0.61 over 12h windows; floor 0.15 is ~2× below the warm-up low and only trips on a near-total hit collapse — the swallowed-error **Discogs-outage** proxy (misses keep coming, hits stop because nothing gets cached). | [fR5ol5u0](https://us.posthog.com/project/293013/insights/fR5ol5u0) |
| **PG cache unavailable** | sum `release_resolution_cache_unavailable` (trailing **6h**) | **> 10** | Baseline is **exactly 0** across all 72 hours / 7,560 requests. > 10 in 6h means genuinely sustained Postgres positive-cache unavailability (tolerates a one-off transient). | [qNnAdLGl](https://us.posthog.com/project/293013/insights/qNnAdLGl) |

## Sentry alerts — project [`wxyc/library-metadata-lookup`](https://wxyc.sentry.io/alerts/rules/?project=4511363514302464)

Metric alerts on the EAP spans dataset (`dataset: events_analytics_platform`, filter `is_transaction:true`). **Sentry has disabled transaction-based metric alerts org-wide** ("Creation of transaction-based alerts is disabled, as we migrate to the span dataset"), so the alertable series is the EAP attribute form `avg(tags[lml.cache.<key>,number])`, **not** the `avg(measurements[lml.cache.<key>])` transactions-dataset form. Note also that in the legacy `transactions` dataset only `api_calls` was extracted (miss/unavailable read as no-data there) — another reason these run on EAP, where all three are present. 60-minute window, email to jake@wxyc.org.

| Alert | Aggregate | Critical / Warning | Rule |
|-------|-----------|--------------------|------|
| **Discogs call-rate** | `avg(tags[lml.cache.api_calls,number])` | **3.0** / 2.0 | [441584](https://wxyc.sentry.io/alerts/rules/details/441584/) |
| **PG cache unavailable** | `avg(tags[lml.cache.release_resolution_cache_unavailable,number])` (per-request rate) | **0.10** / 0.02 | [441716](https://wxyc.sentry.io/alerts/rules/details/441716/) |
| **Cache miss-rate** | `avg(tags[lml.cache.release_resolution_cache_miss,number])` (per-request rate) | **0.10** / 0.05 | [441717](https://wxyc.sentry.io/alerts/rules/details/441717/) |

Sentry thresholds are set against the Sentry (diluted, all-transaction) population where the baselines are avg `api_calls` 0.48 (hourly max 1.27), `miss` 0.010, `unavailable` 0.0. The call-rate 3.0 has ~2.4× headroom over the worst observed hour. The Sentry `miss` alert is a **coarse per-request-rate proxy** — Sentry metric alerts can't express the `hits/(hits+misses)` ratio, so the authoritative "miss climbing without offsetting hit" detector is the PostHog **Hit-rate collapse** alert; the Sentry `miss` and `unavailable` alerts are the fast per-transaction guards that fire on a *dense* (severe) signal. (EAP timeseries can under-report a very sparse signal, so treat PostHog as authoritative for mild/slow degradations and Sentry as the acute pager.)

## Response — what to do when one fires

1. **Confirm it's real, not a traffic artifact.** Open the firing insight/rule. For call-rate, check whether Discogs is actually being hit harder (cold cache after a deploy, a burst of novel artists) vs a measurement glitch. For hit-rate/unavailable, cross-check the other surface (PostHog ↔ Sentry) and the [Discogs cache](https://github.com/WXYC/discogs-etl) health.
2. **Decide on the kill switch.** The feature is opt-in per-request and fully reversible. If the call-rate spike is sustained and `/lookup` latency is affected (or the hit-rate/unavailable alert confirms an upstream Discogs/PG outage the row-less path is amplifying), disable the flag:

   ```bash
   railway variables --set "LML_RESOLVE_NONLIBRARY_RELEASE=false" \
     --service library-metadata-lookup --environment production
   ```

   Railway redeploys on the variable change. **Verify** the flip landed: `/health` returns healthy and flag-on traffic drops to zero — the `cache.lml_resolve_nonlibrary_release` tag flips to `0` in PostHog and `lml.cache.lml_resolve_nonlibrary_release` to `0` in Sentry. (A Railway variable-set redeploy can occasionally fail silently — confirm via `/health` and the tag, don't assume.) With the flag off, non-library releases stay dropped (pre-LML#628 behavior); no data is lost, and re-enabling later just resumes the carry-through.
3. **Root-cause before re-enabling.** Kill switch buys time; it is not the fix. A call-rate spike usually points at cache coldness (see [LML#337](https://github.com/WXYC/library-metadata-lookup/issues/337) and the post-launch hardening epics); a hit-rate collapse or `unavailable` streak points at the Discogs cache or the `lml_cache.release_resolution_cache` Postgres, respectively.

## Routing

Delivery is email to jake@wxyc.org on both surfaces. There is **no Slack integration** on the Request-O-Matic PostHog project (`integrations-list kind=slack` → none), so PostHog Slack delivery would need the integration wired first (Settings → Integrations), after which an alert can route to a channel via a `cdp-functions-create` destination. Sentry likewise delivers by email; a Slack action can be added to each rule once a Slack integration exists on the Sentry org. Wire the team channel when available and swap/extend the actions.

## Notes / gotchas

- **Flag-on scoping** is via the `lml_resolve_nonlibrary_release=1` tag, not an environment filter. The flag is fully enabled (staging + prod), so today that captures all `lookup_completed` traffic; the scope stays correct if the flag is ever rolled back.
- **Bulk is excluded** by querying `lookup_completed` only (never `lookup.bulk_completed`). On `/lookup/bulk` the counters are batch totals across the whole shared `cache_stats` context, which would mix single- and batch-shaped values.
- **Don't touch the LML#681 emit code** to change these alerts — the keys are shape-stable (`_LML_CACHE_STATS_EXTRA_KEYS`, LML#544) and correct; alerting only consumes them.
- **These alerts are UI/API config, not alert-as-code.** LML has no versioned alert config today; this doc is the source of truth for the thresholds and rationale. If the team later wants them versioned, that's a separate decision.
