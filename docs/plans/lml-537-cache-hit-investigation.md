# LML #537 — Discogs cache-hit investigation: probes #1, #2, #3, #6

**Issue:** [WXYC/library-metadata-lookup#537](https://github.com/WXYC/library-metadata-lookup/issues/537) — *Discogs cache hit ratio plateaued at ~50% (`search_releases_by_track` at 34%) after write_release fix*

**Scope:** Implement four of the six concrete next steps from the issue. The remaining two (#4 steady-state estimate — a back-of-envelope calc; #5 PG row-count audit — a daily snapshot to capture over a week) are non-code follow-ups.

| # | Probe | Shape | New file |
|---|---|---|---|
| #1 | Cache miss provenance sample | One-shot script: log mine + PG query → CSV | `scripts/cache_miss_provenance.py` |
| #2 | Dynamic-write contribution histogram | One-shot script: PG aggregate → stdout table | `scripts/cache_warm_histogram.py` |
| #3 | Ranking-jitter test | One-shot script: live Discogs probe + overlap report | `scripts/discogs_ranking_jitter.py` |
| #6 | Rate-limiter telemetry tag | Code change: tag `lml.discogs.{semaphore,rate_limiter}` spans | `discogs/service.py`, `discogs/fallthrough.py` |

The three scripts are diagnostic, not part of the request hot path. They live under `scripts/` next to the existing audits (`audit_va_writeback_pollution.py`, `benchmark_cache.py`, `measure_artwork_match_floor.py`) and follow that file's shape: argparse + `DATABASE_URL_DISCOGS` env-driven, CSV / stdout output, dry-run by default where state is touched (none of these write).

The code change is small and behind a contextvar — no behavior change, only Sentry-side observability.

## #6 — Rate-limiter telemetry tag (code change)

### The deepening shape

The issue text asks for `cache_state=hit|miss` on `lml.discogs.rate_limiter` and `lml.discogs.semaphore` spans so the wait-time histogram can be split. But `_request_with_retry` (`discogs/service.py:351`) is only ever entered on a cache miss (or a no-PG-leg method like `get_master`) — at this layer `cache_state` is structurally one of:

- `miss` — PG was checked and missed (the common case)
- `skip` — `should_skip_cache()` returned true (benchmark / A/B)
- `cooldown` — PG read short-circuited by `_cool_down_active()` (#324 fallback)
- `no_pg` — `pg_read is None` by design (`get_master`, `get_label_image`, `search_releases_by_album_title`)

Splitting the histogram by which **method** triggered the API call is the actually-useful signal (see issue: 34% on `search_releases_by_track` vs 97.5% on `get_artist_details` — the slow tail is dominated by one method). So we add **two** tags, not one:

- `lml.discogs.cache_state` — one of the four values above
- `lml.discogs.method` — the seam's `label` (`get_release`, `search`, `validate_track_on_release`, `search_releases_by_track`, `get_artist_details`, etc.)

Both literal-`miss`-only and the split-by-method case are then queryable in Sentry. The two-tag shape matches the spirit of the issue ("split the wait-time histogram"), while staying truthful at the layer where the spans actually live.

### Mechanism

The `_request_with_retry` method is generic over caller — it doesn't know the seam's label. Two options:

| | Pros | Cons |
|---|---|---|
| **A.** Pass `label` / `cache_state` as kwargs into `_request_with_retry` | Explicit at call site; no globals | Touches 9 call sites in `service.py` (lines 509, 539, 665, 741, 933, 1048, 1073, 1152, 1178); breaks any test that calls `_request_with_retry` directly |
| **B.** ContextVar (`_REQUEST_CONTEXT`) set in the seam, read in `_request_with_retry` | Single read/write site each; mirrors the existing `should_skip_cache()` contextvar pattern in `discogs/memory_cache.py`; no signature churn | Implicit context; can leak if a test forgets to reset |

**Decision: B.** It mirrors `should_skip_cache()` (also lives in `discogs/memory_cache.py` and is read deep inside the seam). The leak risk is bounded by a `contextvars.copy_context()` per call: each `fallthrough()` invocation enters a fresh scope. We use a `Token`-paired set/reset (`contextvars.ContextVar.set()` returns a `Token`; the `finally` resets via that token) so no test can leak.

The contextvar is named `_request_context_var` to match the existing `_skip_cache_var` style in `discogs/memory_cache.py` (lowercase + `_var` suffix), not CONSTANT_CASE.

The flow:

```
fallthrough(label="get_release", ...)
  → sets _request_context_var to {"method": "get_release", "cache_state": "miss"|"skip"|"cooldown"|"no_pg"}
  → calls api_fetch()  (which eventually calls _request_with_retry)
    → _request_with_retry reads _request_context_var; tags the two spans
  → finally: _request_context_var.reset(token)
```

`cache_state` is computed in `fallthrough()`:
- `skip` if `should_skip_cache()` returned True
- `no_pg` if `pg_read is None`
- `cooldown` if PG read was short-circuited by `_cool_down_active()`
- `miss` otherwise

A cache **hit** never reaches `_request_with_retry` so we don't need a `hit` value at this layer. (If a future caller wants to wrap the L2 read in the same wait-budget instrumentation, the hit value is trivially available — but that's not this PR.)

### TDD

1. **Red:** `tests/unit/test_discogs_request_telemetry.py::test_semaphore_span_tagged_with_method` — patches `sentry_sdk.start_span` to record spans, drives `fallthrough(label="get_release", ...)` with an `api_fetch` that calls `_request_with_retry`, asserts both spans carry `lml.discogs.method = "get_release"` and `lml.discogs.cache_state = "miss"`. Expected failure: tags absent.
2. **Green:** Add `_request_context_var: ContextVar[dict[str, str] | None]` in `discogs/fallthrough.py`. Set it before the API-fetch leg with the computed `cache_state`. In `_request_with_retry`, after `start_span`, call `span.set_data("lml.discogs.method", ctx["method"])` and `span.set_data("lml.discogs.cache_state", ctx["cache_state"])`. Reset via `Token` in `finally`.
3. **Coverage:** add four parametrized cases for `cache_state` ∈ {`miss`, `skip`, `no_pg`, `cooldown`}, all in `test_semaphore_span_tagged_with_method`'s parametrize block. The `cooldown` case manipulates `discogs.fallthrough._cool_down_until` directly (mirrors the existing `_reset_cool_down_for_tests` pattern).
4. **CancelledError reset test:** a separate `test_context_resets_on_cancelled_error` function — drives `fallthrough` with an `api_fetch` that raises `asyncio.CancelledError`, then asserts `_request_context_var.get()` is `None` after the call. Verifies the `finally` resets via `Token` survives cancellation.
5. **Negative test:** without the contextvar set (direct `_request_with_retry` call from outside the seam, e.g., the existing health probe), the span tags are absent — `span.set_data` is only called when `_request_context_var.get()` is not None. This keeps current direct callers and tests untouched.

### Files touched

- `discogs/fallthrough.py` — add `_REQUEST_CONTEXT` contextvar, set/reset around API-fetch leg.
- `discogs/service.py` — read contextvar in `_request_with_retry`, tag both spans.
- `tests/unit/test_discogs_request_telemetry.py` — new test file.

Estimated diff: ~80 lines of code + ~120 lines of test.

## #1 — Cache miss provenance sample script

### Inputs

- Either: a log file (NDJSON or text) on disk — the user `railway logs` exports a window before running.
- Or: stdin (`cat railway-log.txt | python scripts/cache_miss_provenance.py`).
- `DATABASE_URL_DISCOGS` env var (mandatory) for the PG side.

Discrimination: log format. The existing `fallthrough.py:236` emits `logger.debug("Cache miss (%s)", label)` and the `_add_discogs_breadcrumb("cache_miss", bc_data)` breadcrumb carries `release_id` / `artist_id` (set at the call site). Production logs run at INFO and won't have the `Cache miss (label)` debug line — but the orchestrator and `cache_service.write_release` calls do log at INFO when an API hit succeeds. The pragmatic source for "miss → API call" events is the `cache_lookup_*` breadcrumb stream, but breadcrumbs aren't in stdout logs by default.

**Decision:** widen the script's input shape to accept multiple forms (regex-driven parsing), and if no input matches in the current production log shape, the script prints a sample of the lines it tried and asks the user to expand `LOG_PATTERNS` for the actual format. This is a one-shot diagnostic — failing loudly with a sample is fine.

The script's tunable: a list of compiled regexes that capture `(timestamp, label, release_id, artist_id?)`. The default set captures:
- `Cache miss (search_releases_by_track)` (debug — needs `LOG_LEVEL=DEBUG` window)
- `Discogs API: GET /releases/{id}` (always emitted)
- `Discogs API: GET /artists/{id}`
- `Discogs API: GET /database/search?...`

The release/artist-id-bearing API call lines are the actually-reliable proxy.

### Classification

For each captured `release_id`:
- `release_existed`: `SELECT id FROM release WHERE id = $1` returns a row
- `release_track_count`: `SELECT count(*) FROM release_track WHERE release_id = $1`
- `method`: from the regex match (`get_release` / `search_releases_by_track` / `validate_track_on_release` / etc.)
- `artist_seen_in_window`: any earlier cache-hit line in the same input window for the same `artist_id` (set lookup; `True`/`False`)

Output: CSV at `/tmp/lml-537-cache-miss-provenance.csv` with columns `timestamp,method,release_id,artist_id,release_existed,release_track_count,artist_seen_in_window`.

### Acceptance

- Runs against a Railway log export without code import-time dependence on the cache (uses `psycopg` directly like the sibling `audit_va_writeback_pollution.py`).
- Dry: no PG writes.
- A `--limit N` flag for the issue's "50 random events" sample. Defaults to 50.
- Test: `tests/unit/test_cache_miss_provenance.py` mocks `psycopg.connect` and a synthetic log fixture, asserts CSV row shape. Two cases: release-row-exists-with-tracks vs. release-row-absent.

### Files touched

- `scripts/cache_miss_provenance.py` (new)
- `tests/unit/test_cache_miss_provenance.py` (new)

Estimated: ~150 lines of script + ~80 lines of test.

## #2 — Dynamic-write contribution histogram script

A direct port of the issue's one-line query:

```sql
SELECT count(*) AS rows, date_trunc('day', artwork_checked_at) AS day
FROM release
WHERE artwork_checked_at IS NOT NULL
GROUP BY 2
ORDER BY 2;
```

Wrapped in a small script for repeatability:

- `DATABASE_URL_DISCOGS` env var
- Runs the query, prints a stdout table (right-aligned counts, ISO date)
- A summary line: total rows with `artwork_checked_at IS NOT NULL`, daily mean since `2026-06-07` (post-Alembic-0009 deploy), and the ratio of post-2026-06-07 days to pre.
- `--since YYYY-MM-DD` flag to override the cutoff (default: 2026-06-07).
- `--csv` flag to emit machine-readable output.

### Acceptance

- One PG query, idempotent, read-only.
- Test: `tests/unit/test_cache_warm_histogram.py` mocks `psycopg.connect`, returns a fixture set of (day, count) rows, asserts the printed summary and post-deploy/pre-deploy ratio math.

### Files touched

- `scripts/cache_warm_histogram.py` (new)
- `tests/unit/test_cache_warm_histogram.py` (new)

Estimated: ~80 lines of script + ~60 lines of test.

## #3 — Ranking-jitter test script

Live probe against the Discogs API (no PG involvement).

- Reads `DISCOGS_TOKEN` (mandatory).
- Takes `--track TITLE --artist NAME` on the command line. Defaults to a canonical example from the issue (`Moments of Soft Persuasion` and friends).
- Issues two identical `/database/search?type=release&q=<artist>+<track>` calls separated by `--delay-seconds N` (default 30s — within Discogs's 60s rate-limit window).
- Captures the full release-ID list from each response, computes:
  - `len(set_a)`, `len(set_b)`
  - `overlap = len(set_a & set_b)`
  - `jaccard = overlap / len(set_a | set_b)`
  - Position-stability: average rank-delta for IDs present in both calls.
- Optional `--repeat N` to issue N pairs and emit summary stats.

### Wire format

We can call the live API directly with `httpx.AsyncClient` and the issue's documented base (`api.discogs.com`); no need to spin up the full `DiscogsService` (its semaphore + L1 cache would muddy the test). This is a deliberately *raw* probe.

### Acceptance

- Stdout output: a one-line summary per pair plus a final aggregate.
- A `--json` flag for machine consumption (so the result is grep-able in a future issue update).
- Test: `tests/unit/test_discogs_ranking_jitter.py` patches `httpx.AsyncClient.get` with two synthetic Discogs payloads (high overlap and low overlap fixtures), asserts the jaccard math and position-stability calc.

### Files touched

- `scripts/discogs_ranking_jitter.py` (new)
- `tests/unit/test_discogs_ranking_jitter.py` (new)

Estimated: ~120 lines of script + ~80 lines of test.

## Doc updates

- `docs/scripts.md` gets a one-liner per new script under a new "Cache investigation (LML#537)" section, matching the existing layout (each script gets purpose + run command). Per CLAUDE.md, project-scope docs are maintained alongside the code that adds them.

## Out of scope

- **#4 — Steady-state estimate.** A back-of-envelope math problem, not code. Will be a comment on #537 after this lands and a few days of data accumulate.
- **#5 — PG row-count audit.** A daily snapshot to capture over a week. The histogram from #2 already gives the cumulative shape; the daily delta is the same data in a different cut. Will be re-run from #2 on a schedule out-of-band.
- The two operational secret leaks called out in the issue's "Out of scope" — separate issue.
- The `MAX_SEARCH_RESULTS` early-exit gap (already #536).
- Cosmetic test nits (already noted in the issue's "Out of scope").

## Risks

- **#6 contextvar leakage.** The seam's `Token`-paired set/reset is correct under cancellation (`finally` always runs). Test coverage includes a CancelledError case to assert the contextvar resets.
- **#1 log shape divergence.** If production logs don't carry the patterns we expect, the script fails loudly with a sample — not silently. User runs it once, sees the sample, expands `LOG_PATTERNS` for the actual format. Cheaper than guessing.
- **#3 Discogs rate-limit.** Two consecutive requests within 30s are well within the 60-req/60s limit; `--delay-seconds` is configurable for slower probes. If the script is run repeatedly it could nibble at the LML production token's budget — script logs token usage via the `X-Discogs-Ratelimit-Remaining` header.

## Acceptance criteria

- All four files (3 scripts + 1 code change) land in one PR closing #537.
- All new tests pass under `pytest tests/unit/`.
- Pre-commit hook (`ruff check` + `ruff format --check`) clean on staged files.
- The PR's body summarises which probe each file maps to and what each script's expected output looks like — so future-Jake (or another agent) can re-run them from #537's context without re-reading this plan.

## Order of operations

1. #6 first — smallest, TDD'd, most likely to need iteration on the span-tagging approach.
2. #2 next — simplest script, validates the PG-connection shape (`psycopg.connect` against `DATABASE_URL_DISCOGS`).
3. #1 — builds on #2's PG connection pattern; harder because of the log-parsing piece.
4. #3 — independent of the other three.
5. Pre-push CI checks.
6. Issue + PR.
