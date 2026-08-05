# LML lookup latency — remediation plan

**Status:** draft (2026-07-21, rev 2 — review findings folded in). **Goal (as stated):** all lookups <1s. **Goal (achievable, reframed below):** interactive lookups p95 <1s; cold/enrichment lookups bounded (<5s) and off the synchronous path; eliminate the multi-minute coordinator waits.

Backed by prod Sentry (2026-07-21) + code audits of the two pinned trees below + the Project-32 hardening-epic map.

### Pinned source trees (read before touching any anchor)

| Repo | Local path | Pin | Notes |
|---|---|---|---|
| **library-metadata-lookup** | the WXYC-org checkout (`WXYC/library-metadata-lookup`) | `prod @ 94eb220` | Default working tree is on `prod`; baseline new work against `origin/main` (5 ahead) or this pin. All `lookup/`, `enrichment/`, `discogs/`, `core/` anchors below resolve at `94eb220`. |
| **Backend-Service** | the WXYC-org checkout (`WXYC/Backend-Service`) | `main @ 5f67115b` (2026-07-21 19:40 PDT) | **Not** a stray top-level `Backend-Service` clone outside the WXYC workspace — one such clone is an unrelated `daisyguide` repo and was the source of the review's stale-tree false alarm. The WXYC-org tree is current with `origin/main` and contains every BS anchor (`shared/lml-client/src/index.ts`, `apps/enrichment-worker/{handler,enrich}.ts`, `apps/backend/controllers/library.controller.ts`). |

Every BS line anchor is pinned to `5f67115b`; if the tree has moved when an implementer picks this up, re-resolve the symbol (grep target named in each row) rather than trusting the line number. Cut a fresh worktree from each pin before Phase 0/1/3 work (branch/worktree names in §7).

---

## 1. Diagnosis: a three-layer synchronous cascade

Current prod (24h): `/api/v1/lookup` **p50 15.2s / p95 50.8s / p99 66s**; **only 0.8% of ok lookups are <1s, 10% <5s.** `/library/search` is fine (p50 12ms) — the problem is exclusively the enrichment/lookup path.

```
DJ play / album add / iOS artwork / webhook
   ├─ flowsheet INSERT (pending) ─pg_notify→ enrichment.consumer.tick ──► lookupMetadata ─┐ (1 LML call PER ROW, no cache pre-check)
   ├─ read/write/proxy ─► lml.coordinator.lookup ─(cache/coalesce ⅔)─► return             │
   │        └─(miss ⅓)─► activeLimiter.run ─► lookupMetadata ─────────────────────────────┤
   │             [p50 225s / p95 820s = UNBOUNDED, un-timed Semaphore(5) queue wait]       │
   └─ backfill jobs (own process, own Semaphore(5)) ─► lookupMetadata ─────────────────────┤
                                                                                            ▼
                                                                     LML /api/v1/lookup (p50 15s)
                                                                     → Apple 4.8s + 17 PG round-trips + Discogs
```

**Layer 1 — LML lookup is expensive (~15s median).** Two root causes:
- **Apple Music probe: 93% of lookups, p50 4.8s** (4s `wait_for` + a shared **1 req/s** AsyncLimiter inside the span; `apple_music.py:114,215`, `timeouts.py:25`). Runs live, outside every deadline.
- **~17 PG round-trips/lookup against an 8-conn pool.** = release-hydration N+1 (`get_release` 9 reads `cache_service.py:1066` + `get_artist_details` 5 reads `:1728`) × unbounded track-validation fan-out (up to ~50 Discogs searches, `validation.py:107`) × per-query connection churn (`RESET ALL` ×32,976 = ~7.5 acquire/release per lookup). Individually fast (#313 held: p50 2.6ms) but they serialize + starve the loop under the flood → the 51s/66s tail.
- **Structural enabler:** the caller budget + #370 hard cap cover **only steps 2–3**; the whole tail (miss-probe, validation, artwork, Apple, identity) is in a `try` that catches only `DiscogsBreakerOpenError`, no wall-clock bound (`orchestrator.py:1131-1137`).

**Layer 2 — the BS coordinator turns 15s into 5–57 min.** `lml.coordinator.lookup` 7d: **p50 330s, p95 3,399s (57 min).** It's blocked in BS's **own** LML-client limiter: `activeLimiter.run()` does an **unbounded, un-timed `Semaphore(5).acquire()`** before the HTTP span opens (`shared/lml-client/src/index.ts`: `Semaphore.acquire` at :128, compose gate at :234-238, serve-seam `activeLimiter` at :515-516). LML at 15s holds each of 5 permits ~15-30s → drain ~0.33/s → callers queue with no deadline (the 30s abort is armed only post-admission). Queue wait is invisible as HTTP = the "225s of nothing."

**Layer 3 — the enrichment consumer is a self-perpetuating flood.** CDC-driven, **one LML call per new flowsheet row, no `album_metadata` pre-check** (`apps/enrichment-worker/handler.ts:193`, `lookupMetadata` call) → a release played 50× pays 50 cold lookups. The worker **bypasses the coordinator** (no coalescing) and each of ~30 backfill containers builds its **own** `Semaphore(5)`, so LML's real concurrency = the sum, overrunning its server cap of 5.

---

## 2. Feasibility — why "<1s for all" must be reframed

Live artwork/streaming for a non-catalog album is a genuine multi-second external dependency (Apple/Discogs). It cannot be made <1s while synchronous. Achievable instead:
- **Interactive lookups <1s** — served cache-first (the read path already is; extend to artwork).
- **Cold enrichment off the synchronous path** — background jobs; nobody waits. Cold internal lookup bounded (~2-5s), async.
- **Recommended SLO:** p95 interactive `/lookup` <1s; p95 cold-enrichment <5s; coordinator wait p99 <2s (fast-fail, not 57min).

Per-shape (LML audit): **(c) artist-only already <1s**; **(b) artist+album** reaches <1s only with Apple removed (lever L1) + hydration collapse; **(a) artist+track** cold-cache realistic floor ~1.5-3s even with all levers — needs the Phase-4 contract change for true <1s.

---

## 3. The plan (sequenced for fastest relief → durable fix)

### Phase 0 — Stabilize + clean baseline (ops, reversible, ~hours)
- **De-flood to measure clean.** The continuous load is the CDC enrichment worker + coordinator misses (06:00 backfill cron is already gated, BS#1668). Throttle the enrichment worker (lower its per-process concurrency) or briefly pause it to capture an uncontended `/lookup` baseline — this is the #706 "re-measure and dispose" step, currently blocked precisely because the consumer is a continuous flood. Tradeoff: delayed metadata enrichment while throttled. **Reversible.**
- **Un-gate the BS#1668 recovery cron BEFORE any shedding ships (prerequisite for B2).** Shedding (B2 + the non-blocking coordinator) strands rows unless the `sweep→pending→backfill-cron` recovery path is live — recovery is the cron (BS#895), currently flood-gated (BS#1668). This is sequenced first because B2 must not merge while recovery is gated. Verify the cron is un-gated and draining `pending` before B2 hits prod. **Reversible.**
- Note: config alone can't fix the coordinator hang (the queue is *unbounded* — lowering concurrency lengthens it). The real fix is code (Phase 3, B2).

### Phase 1 — LML hot-path quick wins (LML PRs, TDD, ~days) → cold p50 15s → ~3-5s
| # | Lever | Where | Impact | Risk |
|---|---|---|---|---|
| **L1** | **Cache-FIRST happy-path Apple probe** (NOT "background" — see §6). Peek `album_streaming_url_cache` synchronously (~2.5ms PG) before the live probe: hit → skip Apple; miss → run live synchronously (preserves first-lookup URL) **and write the cache**. Today the inline probe never populates that cache (`item.py:434` front-runs the postprocess), which is *why* 93% hit Apple live. | `enrichment/item.py:199-203,434`; `streaming_url_postprocess.py:305` | **−4.8s on repeat lookups** (the flood *is* repeats) | preserves first-lookup URL ⇒ no #782/BS#1192 persisted-null regress. Artwork probe untouched. |
| **~~L2~~** | **Folded into L1** (§6). A blanket "skip Apple for backfill" is unsafe: the backfill's non-library items need the synchronous **synthesis-path artwork** probe (`item.py:205`); nulling it recreates the outage *class*. Cache-first (L1) already skips Apple for the repeat albums that make up the flood. First-lookup non-library artwork needs the Phase-4 fast-partial contract, not a skip flag. | — | (subsumed by L1) | — |
| **L3** | **Bound the validation fan-out with a plain `Semaphore(5)` gather — NOT `_chunked_gather` (#808, open).** Wrap `validate_one` in an `asyncio.Semaphore(5)` and `asyncio.gather` over **all** rows. **Do NOT route this through `_chunked_gather`** — that helper enforces the LML#543 per-invocation `LML_SEARCH_MAX_API_CALLS` cap and `return`s early between chunks (`concurrency.py:96-103`), silently dropping the tail rows once the shared `api_calls` counter saturates. Under the cold-cache flood every unvalidated row fires a Discogs search, so the cap trips fast and truncates validation input *by count* — the exact #801/#802 recall bug (lost *Richard D. James Album*) this lever exists to prevent. If `_chunked_gather` reuse is unavoidable, the api-call cap must be explicitly disabled/rebaselined on this path. | `validation.py:107` (currently a plain unbounded `asyncio.gather`) | correctness + tail-bound (not a big p50 win; primary path already ~5-wide via `limit_results`) | low — but only if the cap is kept off this path |
| **L4a** | **Drop 4 dead hydration children** read on every lookup, used nowhere: `release_video`, `artist_alias`, `artist_name_variation`, `artist_member`. | `cache_service.py:1187,1771,1775,1779` | 14→10 round-trips | **zero** |
| **L5b** | **Fix LML#881: posthog_flush middleware blocks the loop with synchronous `queue.join()` on every request.** Make it non-blocking. | (per #881) | removes a universal event-loop stall | low |

### Phase 2 — LML structural (LML PRs, ~1-2 weeks) → relieves pool contention (the tail multiplier)
| # | Lever | Where | Impact |
|---|---|---|---|
| **L4c → then 4a/b** | **Lead with 4c: collapse to 1-2 `json_agg`/`LATERAL` round-trips preserving the FULL object shape** — no consumer breakage, no cache poisoning; subsumes L5. Field-drops (4a `release_video` = only truly-dead field; 4b gate `extended` children) require a **separate lean `/lookup` read path** — never mutate the shared cached `get_release`/`get_artist_details` (consumed by `/discogs/*`, warmer, genre-agg). `release_track_artist` = #699 writer credits, **never drop** (§6). | `cache_service.py:1066,1728`; new lean method | **14→2 round-trips** |
| **L5** | One connection per no-HTTP read-burst (not per query); route identity through `bulk_resolve_library_names`. Mostly **absorbed by 4c** (json_agg = 1 acquire + 1 round-trip). | `cache_service.py` gather sites; `orchestrator.py:202`→`entity/store.py:828` | ~17→5-6 acquires. **Do NOT hold a conn across the Apple/Discogs HTTP hops** (reproduces #706 starvation) |
| **L6** | Tune trigram `work_mem`/`statement_timeout` empirically (#806, open; human EXPLAIN/p95). | `cache_service.py:385` | modest |

### Phase 3 — BS load reduction (BS PRs, ~1-2 weeks) — **highest systemic ROI**
| # | Lever | Where (`Backend-Service main@5f67115b`) | Impact | Owner |
|---|---|---|---|---|
| **B1** | **Cache-first enrich path — skip only on CONFIRMED match** (§6). Read `album_metadata` before `lookupMetadata`; skip LML **only if a load-bearing field is non-null** (discogs_url/release_id/artwork_url). For null / search-URL-only shells still call LML (TTL/backoff) so false-no-matches self-heal — else you freeze a BS#1089 poisoned null. | `apps/enrichment-worker/handler.ts:193`; `apps/enrichment-worker/enrich.ts` (null-persist logic ~:274-298) | **Largest call-count cut** — kills the 50×/album amplifier | BS#878/#879 + respects BS#1089 |
| **B2** | **Bound the LML-client limiter queue + total deadline < 60s + circuit breaker.** Deadline must be **< `STRANDED_TTL_SECONDS` (60s)** so a shed doesn't strand mid-sweep; shed → synth-URL/`pending`, never terminal `failed`. Converts 225-820s hangs → fast fails; also fixes the wasted-Discogs-token sweep race (§6). **Prereq: BS#1668 recovery cron un-gated (Phase 0).** | `shared/lml-client/src/index.ts`: `Semaphore` :118-136, compose gate :234-245, serve seam :512-517 | Kills the coordinator's multi-min waits | BS#876 charter; **file new** |
| **B3** | **Batch enrichment via `bulkLookupMetadata`** (≤100/call, already exists, unused). | `apps/enrichment-worker/handler.ts:193`; `shared/lml-client/src/index.ts:634` | ~100× fewer round-trips under bursts | BS#877 |
| **B4** | **Cross-process Discogs admission** (per-process `Semaphore(5)` × ~30 processes overruns LML's cap of 5). Shared (Redis/PG-advisory) or divided budget. | `shared/lml-client/src/index.ts` limiter construction (:234) | stops overrunning LML's real ceiling | BS#876 |
| **B5** | Gate `checkStreamingAvailability` (bypasses the limiter on every album add). | `shared/lml-client/src/index.ts` streaming-check export via `apps/backend/controllers/library.controller.ts:106` | removes an ungoverned LML call class | **file new** |

### Phase 4 — the contract change for true interactive <1s (architecture, ~weeks)
- **LML fast-partial-response:** return metadata immediately with artwork/streaming = null; fill via the existing async cache-warm generalized to artwork (**new `lml_cache.album_artwork_cache`** — LML owns `lml_cache.*`, lifespan-bootstrapped in `main.py`, no cross-repo dance).
- **Coordinator becomes non-blocking:** return cached/partial now, enrich async, deliver via CDC/SSE. Aligns with BS#877 C4 (iOS SSE subscribe, #269, open).
- **Wire-contract step (do not skip):** returning `LookupResponse` with `artwork_url`/`streaming` explicitly null-but-present is a schema question. Confirm the current `LookupResponse` already permits null for those fields (likely — they're optional today); if the partial shape needs a *new* field (e.g. a `partial: true` / `pending_enrichment` marker or a distinct status enum), it goes through the wxyc-shared dance: edit `api.yaml`, run `scripts/generate_api_models.sh`, commit `generated/api_models.py`, land the paired wxyc-shared PR first (regen CI downloads shared `main`; see the API-model merge-order note). Response-enum *additions* are non-breaking (oasdiff WARN, minor bump); a new required field is breaking.
- This is where user-facing lookups become <1s cache reads.

---

## 4. Issue mapping (build on the epic, file only the gaps)

**Reference / residual of existing epics (don't duplicate):** L3=**LML#808**(open) · L6=**LML#806**(open) · L5b=**LML#881**(open) · B1=**BS#878/#879** residual · B3=**BS#877** · enrichment-flood cluster already owned (BS#1591 shipped, #895/#1011/#1064/#1665/#1199/#1137/#1139/#1640). Already shipped, measure *after*: LML#804, #865/#866/#867 (step-2 deadline+dedupe, 07-20).

**UNTRACKED — file new (the anchors of this plan):**
1. **LML: cache-first Apple probe (L1).** Peek `album_streaming_url_cache` → hit skips Apple, miss runs live + writes the cache. (L2 is folded in — there is no `skip_streaming_probe` flag; a blanket skip recreates the outage class per §6.) Only diagnosed today (LML#706/#782); no committed fix.
2. **LML: collapse the hydration N+1 + connection churn (L4/L5).** Untracked (closest #507/#803 = narration only).
3. **BS: cap the coordinator/limiter wait — bounded queue + deadline + breaker (B2).** Untracked (adjacent BS#1117/#1053). Depends on BS#1668 recovery cron un-gate.
4. **BS: gate `checkStreamingAvailability` (B5).**
5. **LML: extend deadline coverage to the full tail** (steps 4/4b/6), or the Phase-4 fast-partial-response epic (carries the wxyc-shared contract step).

---

## 5. Expected trajectory
- After **Phase 1** (Apple off hot path + bound fan-out + dead-read drop): cold `/lookup` p50 ~15s → ~3-5s; p95 collapses (tail was Apple×loop-starvation).
- After **Phase 3 B1+B2** (cache-first enrich + bounded limiter): LML call *volume* down sharply, coordinator waits 57min → sub-second fast-fail; contention tax (~9s uninstrumented) largely gone.
- After **Phase 2** (hydration collapse + conn reuse): cold p50 → ~2-3s.
- After **Phase 4** (fast-partial + non-blocking coordinator): interactive p95 <1s. Target met for the reframed SLO.

**Recommended order of attack:** (Phase 0 un-gate BS#1668) → B1 → B2 → L1 → L3 → L4c → L5. B1+B2 remove most of the load and all the multi-minute waits; L1 (cache-first Apple) makes the repeat-lookup flood cheap; L3-L5 dismantle the pool-contention amplifier. (L2 folded into L1. B4 = cross-process admission only — do **not** route enrichment through the coordinator: first-caller-wins budget-mixing, and B1 already gives enrichment its dedup.)

---

## 6. Regression safety (Chesterton's-Fence pass)

The 2026-05-14 outage was a **latency→timeout→empty-response** bug (#337: cold lookups 9-17s > BS's 5s timeout → BS got nothing → iOS blank), **not** a null-field bug — so every latency lever *reduces* outage risk. The regression to guard is the **orthogonal** mode: a *fast* response carrying a **null `apple_music_url`/`artwork_url` that BS persists forever** (#782 / BS#1192, still open). The pass caught **four would-be regressions in the naive plan** and reformulated each:

1. **L1 must be cache-FIRST, not "background."** The inline `find_track_url` is the only source of `apple_music_url` on a first lookup; backgrounding it makes BS persist a null for *every* cold album (worse than today's flood-only nulls). Cache-first (peek → hit-skips-Apple, miss-runs-live-and-writes-cache) keeps first-lookup presence AND removes Apple from the repeat lookups that are the flood. Only the **URL** is ever cached/deferred; the **synthesis-path artwork probe stays synchronous** (`item.py:205`).
2. **L3 must be CONCURRENCY-bound via a plain `Semaphore(5)`, not `_chunked_gather`, and never result-count-capped.** Two traps here, not one. (a) Truncating the validation input to top-5-by-rowid is the #801/#802 recall bug. (b) `_chunked_gather` *looks* like a safe concurrency bound but carries the LML#543 `LML_SEARCH_MAX_API_CALLS` cap that `return`s early between chunks (`concurrency.py:96-103`) — under the flood every row fires a search, the shared `api_calls` counter saturates, and the tail rows drop silently: the same count-truncation by another name. Use `asyncio.Semaphore(5)` + `asyncio.gather` over **all** rows so every candidate is validated at ≤5 concurrency regardless of the api-call counter.
3. **L4 field-drops can't touch the shared cached method.** `get_release`/`get_artist_details` are read-through-cached and consumed off-`/lookup` (`/discogs/*`, warmer, genre-agg); dropping/gating fields there breaks those consumers and poisons the cache. Lead with **4c (json_agg, same shape)**; any field-drop needs a **separate lean `/lookup` read path**. (`release_track_artist` = #699 writer credits, never drop.)
4. **B1 must skip only on a CONFIRMED match.** "Skip if any row exists" freezes a **false no-match** (null artwork written during cold-cache degradation, `apps/enrichment-worker/enrich.ts`) forever — recreating BS#1089 poisoning inside `album_metadata`. Skip only when a load-bearing field is non-null; keep re-calling null/search-URL-only shells so they self-heal.

**Cross-cutting dependency (now a sequenced Phase-0 step):** fast-fail/shed (B2 + coordinator) leans on the `sweep→pending→backfill-cron` recovery path — recovery is NOT an immediate CDC re-tick (CDC fires on INSERT only), it's the cron (BS#895), **currently flood-gated (BS#1668)**. Un-gate or otherwise guarantee a live recovery path before shipping shedding (Phase 0), or shed rows strand.

**Meta-point:** the corrected plan doesn't *remove* the fences — it **completes** them. L1 finishes #706's own deferred line-92 follow-up; L3 is exactly #808; B1 realizes BS#878's once-per-album intent within BS#1089's guard; B2 closes BS#906's undesigned overflow case. Every lever stays net-positive for latency and *reduces* the #337/outage risk.

---

## 7. Workflow, worktrees & TDD (per-lever)

Each lever is its own line of development: cut a worktree from the pinned tree **before writing code**, file the issue first, PR with `Closes #<n>`. Naming follows the existing parent-dir convention (`<Repo>-<topic>`). TDD is mandatory in both repos (LML `docs/testing.md`; write the failing test first, confirm red, then implement). Markers: LML uses `pg` / `external_api`; anything hitting Postgres or a live Discogs/Apple call carries the marker so CI routes it.

| Lever | Repo | Branch / worktree | Closes | Red test (write first) | Type / marker |
|---|---|---|---|---|---|
| **L1** cache-first Apple | LML | `library-metadata-lookup-l1-apple-cache-first` | new (§4.1) | peek-hit skips the Apple call; miss runs it live **and** writes `album_streaming_url_cache`; first-lookup URL still present (no persisted null) | unit (`tests/unit/enrichment/`) + integration `pg` for the cache write |
| **L3** bound validation | LML | `library-metadata-lookup-l3-validation-bound` | LML#808 | **all** rows validated (none dropped) with `api_calls` counter pre-saturated above `LML_SEARCH_MAX_API_CALLS`; concurrency never exceeds 5 | unit (`tests/unit/lookup/`, monkeypatch the counter) |
| **L4a** drop dead children | LML | `library-metadata-lookup-l4a-dead-hydration` | new (§4.2) | assert the 4 dead child tables are not queried during a lookup (spy on cache_service reads) | unit |
| **L4c** json_agg collapse | LML | `library-metadata-lookup-l4c-json-agg` | new (§4.2) | shape-parity: lean method output == current per-child hydration output for a fixture release/artist | unit + integration `pg` |
| **L5b** posthog non-blocking | LML | `library-metadata-lookup-l5b-posthog-flush` | LML#881 | middleware does not `queue.join()` synchronously on the request path | unit |
| **B1** cache-first enrich | BS | `Backend-Service-b1-enrich-cache-first` | BS#878/#879 residual | skip LML only when a load-bearing field non-null; **re-call** on null / search-URL-only shell (self-heal) | integration (`pg` + mocked LML) |
| **B2** bounded limiter | BS | `Backend-Service-b2-limiter-deadline` | new (§4.3) | queue wait bounded; total deadline < `STRANDED_TTL_SECONDS`; shed → `pending`/synth-URL, never terminal `failed` | unit (fake timers) + integration |
| **B3** bulk enrich | BS | `Backend-Service-b3-bulk-lookup` | BS#877 | burst of N rows issues one `bulkLookupMetadata` (≤100), not N calls | unit |
| **B5** gate streaming-check | BS | `Backend-Service-b5-gate-streaming-check` | new (§4.4) | `checkStreamingAvailability` goes through the limiter (not an ungoverned direct call) | unit |

Phase 4 (fast-partial + non-blocking coordinator) is scoped as its own epic once Phases 1–3 land; its wxyc-shared contract step (§3 Phase 4) is a predecessor PR in `wxyc-shared` before the LML/BS consumers.
