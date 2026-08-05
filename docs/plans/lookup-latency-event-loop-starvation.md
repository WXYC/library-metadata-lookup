# LML `/lookup` latency: single-worker event-loop starvation

**Status:** diagnosis complete, remediation proposed — for review before implementation (rev 2, 2026-07-22 PT — Apple rate-limit lever A′ folded in; §7 issue #1 de-duped against LML#903)
**Date:** 2026-07-22 (PT)
**Parent context:** `plans/lookup-latency-plan.md` (the 7-lever program), `plans/706-cold-lookup-tail-latency.md` (LML#706), the "Post-launch service hardening" project ([#32](https://github.com/orgs/WXYC/projects/32))
**Supersedes the framing of:** the latency-plan "B2" lever (Backend-Service#1748) as *the* remaining user-facing latency fix — see §7.

## TL;DR

Production LML `/api/v1/lookup` is **p50 10.1 s / p95 23.6 s / max 36.5 s** (Sentry, LML project, last 3 h, n=297). That p50 decomposes into **two roughly-equal ~5 s components**:

1. A live **Apple Music probe** (`apple_music.search`) on ~90 % of lookups — **p50 3.6 s / p95 5.1 s**.
2. A **~5 s event-loop starvation tax** that *every* lookup pays regardless of its own work, because LML runs a **single uvicorn worker** and each in-flight request holds a slot for seconds, saturating the one event loop.

The starvation tax is what made a hand-invoked, fully cache-warm lookup (25 ms of real work, zero external I/O) take 1.5–9 s. It is invisible to LML's Server-Timing header (which itemises ~25 ms) because it happens *between and after* the instrumented `track_step()` spans, not inside them.

**This is not what Backend-Service#1748 (B2) addresses.** B2 bounds the *Backend-Service* shared LML-client `Semaphore(5)` queue; the traffic measured here flows `request-o-matic → LML` and never touches that limiter, and that limiter's queue is empty in steady state anyway (§6). The real levers are: get the Apple probe off the synchronous path — or, unblocked and surface-safe, **drain its self-throttled 1 req/s queue** (§2.5, Lever A′) — and stop running LML on a single event loop.

## 1. How we got here

Investigating the disposition of B2/#1748, the operative question was whether the BS-side limiter queue still backs up to minutes post-flood-gating. It does not (§6). But hand-run `lookup "roygbiv by boards of canada"` traces against production surfaced a different, larger problem: on a fully warm cache the itemised LML steps sum to ~25 ms while the round-trip is 1.5–9 s. That gap is the subject of this document.

## 2. Evidence

### 2.1 The `lookup` CLI traces (request-o-matic → LML, warm cache)

Five back-to-back identical invocations. Server-Timing from request-o-matic, which relabels LML's own Server-Timing entries with an `LML:` prefix:

| Run | Sum of itemised `LML:` steps | `LML round-trip (server)` | Unaccounted | Cache state |
|---|---|---|---|---|
| 1 | ~1,600 ms (incl. Discogs 855) | 4,358 ms | ~2,750 ms | PG warm |
| 2 | ~26 ms | **8,998 ms** | **~8,970 ms** | in-mem warm, 0 API |
| 3 | ~23 ms | 2,972 ms | ~2,950 ms | in-mem warm, 0 API |
| 4 | ~26 ms | 1,504 ms | ~1,480 ms | in-mem warm, 0 API |
| 5 | ~26 ms | 4,805 ms | ~4,780 ms | in-mem warm, 0 API |

Run 2 is the key data point: **12 in-memory cache hits, 0 PostgreSQL, 0 Apple, 0 Discogs API — i.e. essentially no external I/O — yet 8,998 ms server-side.** A coroutine that does ~no I/O can only take 9 s if the event loop never scheduled it.

### 2.2 LML Sentry aggregates (`/api/v1/lookup`, last 3 h, n=297)

Transaction duration: **p50 10,074 ms / p95 23,562 ms / max 36,507 ms.**

Child-span breakdown (by `sum(span.duration)`):

| `span.op` | count | per-lookup | p50 | p95 |
|---|---|---|---|---|
| `apple_music.search` | 266 | ~0.9 | **3,578 ms** | 5,078 ms |
| `db` | 8,848 | **~30** | 2.4 ms | 14.6 ms |
| `cache.get` | 723 | ~2.4 | 18.5 ms | 844 ms |
| `cache.put` | 24 | — | 92 ms | 9,734 ms |

Two structural facts fall out: ~90 % of lookups pay a multi-second Apple probe, and each lookup issues ~30 DB round-trips (the N+1 fan-out that L4a/L4c already trimmed on the lean path). Note also every real query is followed by an asyncpg `SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;` span — the per-acquire connection reset (conn-churn) called out in the latency plan.

### 2.3 The waterfall — where the invisible seconds sit

Trace `ac5ce08395ad4c158fcc245c85df652d` (transaction 10,191 ms), offsets from transaction start T₀:

| Offset | Span | Duration |
|---|---|---|
| T+0.007 s | `db` library_release_override SELECT | 39 ms |
| T+0.051 → T+5.097 s | `apple_music.search` "songs" (wraps the Apple HTTP GET) | 5,046 ms |
| T+5.098 s | `db` album_streaming_url_cache SELECT | 4 ms |
| T+5.106 s | `db` entity.identity SELECT | 3.7 ms |
| T+5.114 s | last instrumented span **finishes** | — |
| **T+5.114 → T+10.191 s** | **no spans — 5.08 s gap — then response returns** | **5,077 ms** |

The transaction root (`http.server /api/v1/lookup`) runs T₀ → T+10.191 s. All awaited work completes at T+5.114 s. The trailing **5.08 s is the coroutine descheduled**, waiting for the single event loop to run its synchronous tail (build `LookupResponse`, pydantic-serialise, emit Server-Timing, return) and hand the response back to uvicorn.

A second trace `0daaf101a6404712965a2ab0fef32164` (7,765 ms) shows the identical additive shape: 2,627 ms Apple probe + ~5 s residual after the last await. **The ~5 s starvation tax is present regardless of the Apple probe's own duration** — it is added on top.

### 2.4 The deployment fact

`entrypoint.sh`:

```sh
exec su -s /bin/sh appuser -c "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"
```

No `--workers` and no `UVICORN_WORKERS`/`WEB_CONCURRENCY` read. `railway.toml` **does** exist but declares only `[build]` (`builder = "DOCKERFILE"`) and `[deploy]` (`healthcheckPath = "/health"`, `restartPolicyType = "ON_FAILURE"`, `restartPolicyMaxRetries = 10`) — **no `[deploy].startCommand`** — so the start command is the Dockerfile `ENTRYPOINT` → `entrypoint.sh` above. Prod Railway variables set no worker/concurrency override either (checked: only `FORCE_RESTART` and `LML_STREAMING_WARM_CONCURRENCY=1`; `UVICORN_WORKERS`, `DISCOGS_RATE_BUCKET_ENABLED`, `DISCOGS_RATE_LIMIT`, `DISCOGS_MAX_CONCURRENT` all unset → defaulting). **uvicorn with no `--workers` runs a single process → one event loop → one CPU's worth of Python executing all request coroutines' synchronous portions.** `library.db` access is `aiosqlite` (off-loop background thread, single shared connection) so SQLite is not the loop-blocker; the discogs-cache `db` spans are asyncpg (non-blocking). (An implementer adding workers can set them via `entrypoint.sh`, a `[deploy].startCommand` in `railway.toml`, or by having the entrypoint honor `UVICORN_WORKERS` — the latter is LML#747's chosen surface; note `restartPolicyMaxRetries = 10` governs per-container, not per-worker, crash recovery.)

### 2.5 The Apple probe's 1 req/s is a self-throttle, not Apple's limit

The `apple_music.search` span is bounded by LML's **own** `AsyncLimiter(60, 60)` — 60 calls per 60 s = **1 req/s sustained** (60-burst) — on the shared `AppleMusicClient` (`clients/streaming/apple_music.py:114`), *not* by Apple. Apple publishes no official Apple Music API rate limit; the "60 req/sec" in `docs/plans/apple-music-non-library-persistence.md:142` is a misread of that `(60, 60)` = 60-per-60s config. The community estimate is ~20 req/s per token (unverified).

Empirically we have large headroom **and** the queue is already doing damage (Sentry, wxyc/library-metadata-lookup, `span.op:apple_music.search`, last 7 d, n=17,785):

| `apple_music.search.result` | count | share |
|---|---:|---:|
| **`null` (cancelled at the 4 s `wait_for`)** | 9,907 | **56 %** |
| `hit` | 6,309 | 35 % |
| `miss` | 1,533 | 9 % |
| `error` | 36 | 0.2 % |
| **429 (exhausted *or* transient, from logs)** | **0** | **0 %** |

- **Zero 429s of any kind** in 7 d at 1 req/s + 60-burst — Apple never throttled us once. Headroom to raise is real (ceiling unknown but empirically well above what we send).
- **56 % of probes time out at the 4 s ceiling.** A `null` result means the `apple_music.search` span exited via `CancelledError` (a `BaseException`, so it escapes the `except Exception` and never sets `result`) — i.e. the caller's `asyncio.wait_for(..., 4 s)` fired. The raw Apple HTTP GET is ~338 ms; the rest of the 4 s is the 1 req/s acquire wait. **The queue congestion is already nulling a majority of Apple probes today** — the same null BS then persists (§5.1).
- **Attribution.** The `null` (timeout) spans come only from the `wait_for`-wrapped call sites (the inline `find_track_url` / `find_track_metadata` probes and the #706 background warm). The un-wrapped `/streaming-check` `find_album_match` (`streaming/orchestrator.py:79`, no `wait_for`) never nulls but runs to the httpx 10 s ceiling — it is what produces the p99 10.2 s tail. So this one span op blends bounded and unbounded callers; a happy-path-only slice needs a caller filter.

## 3. Root cause (pinned)

**LML serialises requests on a single uvicorn worker's event loop, and that loop is saturated.** The saturation is driven by a feedback loop:

- ~90 % of lookups fire a live `apple_music.search` that keeps the request in-flight for 3.6–5 s.
- Many such requests are therefore in-flight simultaneously (high concurrency).
- Each request's *synchronous, non-yielding* work — pydantic (de)serialisation, PyO3 `wxyc_etl` text normalisation (GIL-holding), Sentry span assembly for ~40 spans/request, orchestrating ~30 DB round-trips — must run on the one loop.
- With high concurrency, a given coroutine's continuations (especially the post-await response tail) queue behind everyone else's synchronous work and wait seconds to be scheduled. That wait is the ~5 s starvation tax.

Warm requests skip the Apple probe but still land on the same saturated loop, so they still pay the tax — which is why an all-cache-hit lookup took 9 s.

**Why the Server-Timing header hides it:** LML's `RequestTelemetry.track_step()` measures wall-clock *inside* each pipeline step. The starvation happens before the first step, between steps, and (dominantly) after the last step in the response-return tail — none of which is wrapped by a `track_step()`. So the header faithfully reports ~25 ms of in-step work while the request wall-clock is 10 s.

## 4. The one remaining unknown

The *precise* on-CPU attribution of the loop-blocking synchronous work — how much is Sentry span assembly vs pydantic serialisation vs PyO3 normalisation vs sheer coroutine-scheduling overhead — is not yet measured. It does not change the top two remediations (both reduce concurrency or add loops), but it determines the payoff of the secondary "trim per-request loop work" lever (§5.3). **Closing it — primary route:** add an in-process **event-loop-lag gauge** (sample `loop.time()` drift on a fixed interval) and watch it under the enrichment flood; this is reliable and is folded into issue #3. **Best-effort only:** an on-CPU flame graph via `py-spy record` — impractical on the current image without changes (it's `python:3.12-slim`, runs as non-root `appuser`, no py-spy installed, and Railway containers typically don't grant the `SYS_PTRACE`/PID-namespace access py-spy needs), so treat it as a follow-up that requires an image + capability change, not the first move.

## 5. Remediation options (ranked)

### 5.1 Lever A — take the `find_track_url` Apple probe off the synchronous path (highest structural leverage; gated on #782)

The 3.6 s `apple_music.search "songs"` span is the **`find_track_url`** per-track deep-link probe inside `metadata_enrichment` — the same probe **L1/#893** added a cache-first peek for (`lml_cache.track_streaming_url_cache`). It is synchronous on the trace because L1's peek *missed* (cold track cache for that artist/album/song) and ran the live probe inline. Lever A extends the LML#706 treatment already applied to the **album** streaming URL — cache-read-only on the response path, live probe deferred to a bounded background warm (`LML_PERSIST_STREAMING_URLS` doc, `docs/env-vars.md:28`) — to the `find_track_url` probe as well.

- **Removes** ~3.6 s p50 from ~90 % of lookups, and — critically — **collapses request in-flight time from seconds to ~25 ms, breaking the concurrency feedback loop**, which drains the ~5 s starvation tax as a second-order effect. Structurally the biggest lever.
- **This is a deliberate reversal of L1's design choice.** L1 runs `find_track_url` *synchronously on miss precisely to preserve the first-lookup URL*. Making it async means `apple_music_url` is **null on first sight** of a new (artist, album, song), warmed for the next lookup — exactly the shape of the **open #782 / BS#1192 regression** (`plans/lookup-latency-plan.md:121`): a fast response carrying a null `apple_music_url` that **Backend-Service persists forever**, so the "appears on the next lookup" recovery never fires.
- **Therefore gated:** issue #1 must **depend on #782/BS#1192** being closed, or on a BS-side "do not persist a null `apple_music_url` from an incomplete/timed-out enrichment" guarantee, before the null-on-first-sight contract is safe. It also needs product sign-off — and the surface trace (§7 issue #1) shows the cost is **worse than the album/artwork eventual-consistency**, not the same: the listener flowsheet feed serves a frozen, write-once value and iOS renders a null `apple_music_url` as a **dead greyed button**, so a brand-new play's Apple-Music button can stay absent **indefinitely** (until a replay re-enriches or a manual backfill), not just for a few seconds.
- **Risk + tests (mandatory-TDD repo):** must respect the `LML_PERSIST_STREAMING_URLS` / `*_apple_music` kill-switch flags and must not re-introduce the album-cache-poisoning mode that gated L1 (Fork-B history). Regression tests required *first*: kill-switch off ⇒ no background write; a warm hit returns the exact track deep-link (no album-cache cross-write); no persisted-null path that BS can latch.
- **Do Lever A′ (§5.1a) first.** Raising the self-throttled rate limit removes most of this probe's latency *while keeping it synchronous and non-null* — so it needs neither the #782 gate nor product sign-off, and it also shrinks the current 56 % null rate. Lever A is the eventual structural cure (removes the probe from the response path entirely); A′ is the unblocked partial that captures most of the win now. Sequence A′ → (gate clears) → A.

### 5.1a Lever A′ — raise the Apple Music rate limit (surface-safe, unblocked; partial substitute for A)

Lever A removes the probe by deferring it; **A′ keeps the probe synchronous but makes it fast**, by widening the 1 req/s self-throttle that §2.5 shows is the actual cost (the HTTP call is ~338 ms; the rest is queue wait). Because the response still carries a non-null `apple_music_url` on first sight, A′ **sidesteps the #782/BS#1192 gate and needs no product sign-off** — it cannot produce the persisted-null dead-button that gates A.

- **Removes most of the ~3.6 s p50** from the ~90 % of lookups that probe Apple, *and* cuts the current **56 % timeout→null rate** (§2.5) — a rare win-win: lower latency **and** more non-null Apple deep-links (fewer greyed iOS buttons on the played-track surface).
- **Second-order help on the starvation tax (§3).** Cutting probe in-flight time from ~4 s to ~0.5 s cuts the concurrency that saturates the single event loop, so A′ also shrinks the ~5 s tax Lever B targets — a partial that helps *both* heads of the problem, unlike A (Apple only) or B (loop only).
- **The change:** raise `_RATE_LIMIT` from `(60, 60)` = 1/s to e.g. `(300, 60)` = 5/s or `(600, 60)` = 10/s (still under the ~20/s community estimate), and make it an **env knob** (mirroring `LML_STREAMING_WARM_CONCURRENCY` via `resolve_positive_int_env`) so it's a no-redeploy Railway lever with instant rollback — it's a hardcoded module constant (`clients/streaming/apple_music.py:114`) today. Default unchanged ⇒ merge is zero-behavior-change until the var is set.
- **Caveats:** (1) **the Apple token is shared** — staging and the `docs/scripts.md` resolver scripts run on prod creds, so the safe rate is the *aggregate* across all consumers, not prod `/lookup` alone; (2) sustained high rates are untested (bursts to the 60-capacity bucket are proven fine at 0 429s, but a sustained 10/s is not — roll up in steps); (3) it interacts with the BS#1631 backfill — a higher ceiling drains live probes faster but also lets the backfill hit Apple harder, so the durable volume relief is still caller-side (Lever D); (4) the existing 429-retry + `Retry-After` honor + the #755/#787 saturation breaker are the guardrail if we overshoot. Watch the §2.5 429-count + null-rate queries after each bump.
- **Tests (mandatory-TDD):** env-knob resolution (unparseable/zero/negative → default with WARN, mirroring the timeout knob) + a limiter-rate assertion. No product-contract test — the response shape is unchanged (still synchronous, still non-null on hit).

### 5.2 Lever B — run more than one event loop, via LML#747 (simplest near-term mitigation)

This is **not new work — it is LML#747** ("Make LML safe to run with `UVICORN_WORKERS > 1`"). Adding uvicorn workers (or, now that storage is bucket-backed and replica-ready since #834 — `docs/env-vars.md:16` — Railway replicas) parallelises the synchronous per-request work across processes so the post-await tail is not starved. Inherit #747's documented prerequisites; do **not** file this as a fresh issue.

- **Removes** the starvation tax roughly proportionally to the number of loops, without touching product behaviour.
- **Hard prerequisite — the shared Discogs 60/min token.** Each process runs its **own** `AsyncLimiter` at `DISCOGS_RATE_LIMIT` (default 50/min), its own `DISCOGS_MAX_CONCURRENT` semaphore, and its own LML#755 saturation breaker (all per-event-loop, `docs/env-vars.md:34`). Prod has **`DISCOGS_RATE_BUCKET_ENABLED` unset (= false)** today, so a bare `UVICORN_WORKERS=2` would run two independent 50/min limiters ⇒ ~100/min aggregate against the shared **60/min** ceiling → 429s → breaker trips. Before bumping workers, do **both** of the following (they are not interchangeable): **(a) enable LML#841's shared PG rate bucket** (`DISCOGS_RATE_BUCKET_ENABLED=true` — built exactly for "prod replicas plus staging" to meter one 60/min token against a single row; merge-OFF is zero-behavior-change), **and (b) still divide `DISCOGS_RATE_LIMIT` down to `floor(50/N)`** per the horizontal-scaling runbook. Reason (b) is required even with (a) on: #841 **fails open to the per-process local `AsyncLimiter`** on any PG error/outage (`docs/deployment.md:153`), so if the local limiter is left at 50, a discogs-cache PG blip makes all N processes fall back to N×50/min — the exact over-issue relocated to the PG-down window. Also re-size `DISCOGS_BREAKER_REMAINING_FLOOR` / `DISCOGS_BREAKER_FAILURE_THRESHOLD` + `LML_LOOKUP_MAX_CONCURRENT` / `LML_BULK_GLOBAL_MAX_CONCURRENT` (these stay per-process even with #841 on — #841 moves only the *rate* dimension).
- **Other cost:** each worker/replica loads a full app copy — a **separate in-memory Discogs cache** (`cache.get` tier; hit rate splits across N cold caches) and a **separate asyncpg pool** to the discogs-cache PG (connection count ×N — check against the discogs-cache connection budget). Verify Railway instance size (memory ×N).
- **Sequencing + tests:** cheap stopgap while Lever A is built; pick `N` conservatively (2–3). Per #747, the prerequisites are the test surface — a multi-worker smoke that asserts aggregate Discogs egress stays ≤ 60/min with #841 on, plus any per-process-state / `os.replace` stale-inode checks #747 already enumerates.

### 5.3 Lever C — trim per-request loop work (secondary; gated on §4)

Reduce the synchronous cost each request imposes on the loop: lower Sentry span cardinality (~40 spans/request — sample the ~30 DB spans), reduce the DB round-trip count further (L4a/L4c lean path already helps; the conn-churn reset-on-release is pure overhead), and/or move PyO3 normalisation off-loop. Only worth it in proportion to what §4's profile attributes to each.

### 5.4 Lever D — de-flood (caller-side, already tracked)

The concurrency that saturates the loop is dominated by the Backend-Service enrichment consumer's continuous cold-miss lookups. The durable caller-side fix is Backend-Service#1591 (closed) plus the cron gating already in place. This reduces the *number* of concurrent Apple-probe requests; complementary to Lever A but not a substitute (a single organic burst can still saturate a single loop).

**Recommendation:** run **Lever A′ (rate-limit) and Lever B (LML#747)** as the near-term pair — both are unblocked and touch no product contract, and they attack the two different heads (Apple-probe latency and single-loop starvation). Ship **A′ first** as the cheapest win: an env-knobbed rate bump that lowers latency *and* the current 56 % null rate with instant rollback. In parallel take **Lever B** — enable LML#841's shared rate bucket, re-size the per-process #755/concurrency knobs, then set `UVICORN_WORKERS=2–3` after the PG connection-budget check (its Discogs-multiplication hazard already has a built solution in #841). Pursue **Lever A** (full async) as the eventual structural cure but **gated on #782/BS#1192** (persisted-null) resolving — a conscious reversal of L1's synchronous-on-miss choice, needing product sign-off; A′ makes A non-urgent by capturing most of its win without the gate. Run §4's profile first so Lever C is data-driven. Treat B2/#1748 per §7.

## 6. Why B2/#1748 is not the lever here (and its correct disposition)

The `lookup` traffic in this diagnosis is `request-o-matic → LML` (a Python client), which does not pass through Backend-Service's `shared/lml-client` `Semaphore(5)`. Independently, that BS limiter's queue is empty in steady state: `lml.queue_depth` on the BS `lml.lookup` span is **0 across every caller in the last 24 h** (Sentry, BS project), and the only caller that ever backed up in the last 3 d was `library-enrich-artwork` (max depth 279), a **self-healing** path — on a shed it returns results without artwork and re-enriches on the next read, needing no `pending`-row/backfill-cron recovery.

Consequences for B2/#1748:

- It is a **Backend-Service-side resilience valve**, not a fix for the latency measured here. Downgrade it from "last user-facing latency lever" to defense-in-depth.
- Its `blocked-by #895` (the flowsheet-metadata-backfill safety-net cron) is **misattributed**: the caller that actually backs up (`library-enrich-artwork`) never lands a row in `pending`, so it needs no cron; the caller that owns the `pending → cron` path (`enrichment-worker`) shows queue depth 0. The #895 dependency (itself blocked by #1011, which wants to *retire* that cron) can be dropped, dissolving the #895/#1011 knot.

## 7. Proposed issue breakdown (for review)

1. **LML#903 — async `find_track_url` / cache-first track streaming resolution (Lever A).** *Already filed* as [LML#903](https://github.com/WXYC/library-metadata-lookup/issues/903) ("Take the Apple find_track_url probe off the /lookup critical path"), sub-issue of the perf epic (#803/#338), building on L1's track-cache (#893) — **do not re-file this as a new issue.** **Change site:** the L1 miss branch in `lookup/enrichment/item.py` (probe select ~:200, cache read ~:207, live probe ~:254), mirroring the album deferral in `lookup/streaming_url_postprocess.py` + `lookup/enrichment/background.py`; the contract it reverses is pinned by `tests/unit/test_enrichment_track_cache.py:10` (MISS → live `find_track_url` inline). **Depends on #782 / BS#1192** (persisted-null) — do not ship the null-on-first-sight contract until BS is guaranteed not to latch a null `apple_music_url`. The surface trace hardens *why*: the listener flowsheet feed serves a **frozen, write-once, non-self-healing** value (`GET /flowsheet` coalesces persisted `album_metadata`/`flowsheet` columns; enrichment is claim-once on `pending`), and iOS renders a null `apple_music_url` as a **dead greyed button** — so recovery "on the next lookup" never fires for an already-persisted row. It is *not* the "few seconds" the album/artwork path accepts. Product sign-off on deep-link eventual-consistency. Tests-first per §5.1. **Action:** update #903 to carry this depends-on gate explicitly and to name Lever A′ (#1a) as its unblocked precursor. *Highest structural priority, but blocked.*
1a. **LML — raise the Apple Music rate limit behind an env knob (Lever A′).** **Filed as [LML#904](https://github.com/WXYC/library-metadata-lookup/issues/904)** (sub-issue of #803, sibling to #903), **unblocked** (no #782 gate, no product sign-off — the probe stays synchronous and non-null). Code PR in progress on `feat/apple-music-rate-knob` (env knob `LML_APPLE_MUSIC_RATE_PER_MIN` at `clients/streaming/apple_music.py`, resolver `resolve_apple_music_rate_limit`, tests + `docs/env-vars.md` entry). **Change site:** `_RATE_LIMIT` at `clients/streaming/apple_music.py:114` → env-tunable via a `resolve_positive_int_env`-style knob (mirror `LML_STREAMING_WARM_CONCURRENCY`); default unchanged so merge is zero-behavior-change until the Railway var is set. Roll the rate up in steps (1→5→10/s), watching the §2.5 429-count + null-rate queries, respecting the shared-token aggregate (staging + `docs/scripts.md` resolver scripts). Tests: env-knob resolution + limiter-rate assertion (§5.1a). **Code PR from `origin/main` in its own worktree.** *Ship first — cheapest win, and the only Apple-probe lever that helps both the probe latency and the §3 starvation tax.*
2. **LML#747 — safe multi-worker (Lever B).** Not a new issue — **execute the existing LML#747**. Prerequisites: enable LML#841 shared rate bucket **and** divide `DISCOGS_RATE_LIMIT` to `floor(50/N)` (§5.2 fail-open reasoning), re-size the per-process #755/concurrency knobs, PG connection-budget check, then `UVICORN_WORKERS=2–3`. Storage is already bucket-backed/replica-ready (#834). **Ops-only — Railway env-vars + `entrypoint.sh`/`railway.toml` start command; no code PR.** *Ship as the near-term primary lever.*
3. **LML — Server-Timing / observability gap (the one code PR).** The header (and Sentry) miss the pre/post-await starvation. Add an event-loop-lag gauge (also closes §4) + a Server-Timing bucket for the streaming-resolution phase, via the existing `telemetry.as_server_timing(extra=...)` seam at `lookup/router.py:348`, with a header-assertion unit test for the new leg. New sub-issue of #803/#338. Branch from `main` in its own worktree (see workflow note).
4. **BS#1748 (B2) — disposition change (separate BS-repo note).** Re-scope to the resilience-valve framing, drop the `blocked-by #895` edge (misattributed per §6). This is Backend-Service issue-graph surgery, not LML work — track it in a BS-repo note/comment, kept out of the LML epic's single-repo sub-issue set.

**Execution status (2026-07-22 PT).** `/review-plan` re-run over the rev-2 additions (A′ + §2.5) → **Approve with suggestions**, all 5 findings folded into the A′ implementation. Filed/updated:
- **#1a → [LML#904](https://github.com/WXYC/library-metadata-lookup/issues/904)** (filed, sub-issue of #803) — **code PR [#905](https://github.com/WXYC/library-metadata-lookup/pull/905)** on `feat/apple-music-rate-knob`, **CI fully green**, ready to merge on go (zero-behavior-change until the Railway var is set).
- **#1 (#903)** — updated: structured **blocked-by #782** edge added + comment recording the gate rationale, the dead-button hardening, #904 as the unblocked precursor, and the test-assertion-line cite correction.
- **#2 (#747)** — updated: comment adding the fourth prerequisite (shared Discogs 60/min token → enable #841 **and** `floor(50/N)` for the fail-open window) + the demand-side (starvation-tax) motivation.
- **#3 → [LML#907](https://github.com/WXYC/library-metadata-lookup/issues/907)** (filed, sub-issue of #803) — event-loop-lag gauge riding the `cache_stats` → PostHog/Sentry seam; Server-Timing streaming leg split out as optional.
- **#4 (BS#1748)** — disposition comment posted (downgrade to resilience valve; recommend dropping the misattributed `blocked-by #895`).
- **Incidental unblock:** a pre-existing Codegen Freshness drift (wxyc-shared merged `Concert.station_recommended_rank` at 16:48 PDT) was reding every LML PR; fixed by regen PR #906 (merged to main), then #905 rebased green.

**Workflow note:** the working tree is currently on `prod` with `plans/` untracked — do **not** originate code from this checkout. Issues **#1a** (rate-limit env knob) and **#3** (observability) are the code changes; branch each from `origin/main` in its own worktree (repo branch strategy: `main`→staging, `prod`→production). Issue #1 (#903) is blocked; Lever B (#747) and the B2 disposition are ops/issue-graph changes with no LML code PR.

## Appendix — reproducing the measurements

- LML transaction: Sentry → wxyc/library-metadata-lookup, `transaction:/api/v1/lookup is_transaction:true`, fields `p50/p95/max(span.duration)`.
- Child-span mix: same, `is_transaction:false`, group by `span.op`, `sum(span.duration)`.
- Waterfall exemplars: traces `ac5ce08395ad4c158fcc245c85df652d` (10.19 s) and `0daaf101a6404712965a2ab0fef32164` (7.77 s), fields `span.op`, `precise.start_ts`, `precise.finish_ts`.
- BS limiter queue: Sentry → wxyc/backend-service, `span.description:lml.lookup`, `max/p95(lml.queue_depth)` grouped by `lml.caller`, 24 h vs 3 d vs 14 d.
- Worker config: repo `entrypoint.sh`; prod `railway variables --service library-metadata-lookup --environment production --kv`.
