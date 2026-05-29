# LML #393 — Deepen the Discogs cache fallthrough into one read-through module

**Issue:** [WXYC/library-metadata-lookup#393](https://github.com/WXYC/library-metadata-lookup/issues/393) — *surfaced by the 2026-05-27 architecture/deepening review of LML*

**Sibling deepenings (unblocked, separate PRs):** #394, #395. **Sibling deepening (blocked on #376):** #392.

**Folds in:** #324 (graceful fallback in `cache_service.py`) — its acceptance criteria become acceptance criteria of this PR. #324 will be closed via this PR's `Closes` block.

## Problem (matrix view)

Today, `discogs/service.py` hand-weaves a 3-tier cache pattern (in-process TTL → PostgreSQL → Discogs API + write-back) inside each public method. Eight cache-bearing methods, three different shapes:

| Method | L1 in-mem | PG read | API | PG write-back | Negative cache |
|---|---|---|---|---|---|
| `get_release` | `@async_cached(RELEASE_CACHE)` | ✓ | ✓ | **✓** | — |
| `get_artist_details` | `@async_cached(ARTIST_CACHE)` | ✓ | ✓ | **✓** | — |
| `search_releases_by_track` | `@async_cached(TRACK_CACHE)` | ✓ (gated `not artist_as_keyword`) | ✓ | **✗** | ✓ (read + write on empty API) |
| `search` | `@async_cached(SEARCH_CACHE)` | ✓ | ✓ | **✗** | — |
| `validate_track_on_release` | `@async_cached(VALIDATION_CACHE)` | ✓ (tri-state `bool \| None`) | indirect via `get_release` | **✗** (indirect via `get_release`) | — |
| `get_master` | `@async_cached(MASTER_CACHE)` | — | ✓ | — | — |
| `search_releases_by_album_title` | — | — | ✓ (intentional, #319) | — | — |
| `get_label_image` | `@async_cached(LABEL_CACHE)` | — | ✓ | — | — |

Three categories of friction:

1. **Tier policy is duplicated 5×** with subtle drift. The "PG read → on miss API → write back" ritual is rewritten in `get_release` (L697–845), `get_artist_details` (L847–926), `search_releases_by_track` (L399–579), `search` (L997–1147), and `validate_track_on_release` (L1183–1286). Each repetition encodes the same breadcrumb / cache-stat / try-except shape with localized variation.
2. **Write-back drift is invisible.** `get_release` and `get_artist_details` persist API results back to PG; the three search-shaped methods do not. There is no single place that documents the policy split, so the divergence reads as carelessness rather than as design.
3. **#324's graceful-degradation fix has no home.** When `cache_service.py` throws on a `PostgresConnectionError` (the monthly dedup-swap window in `WXYC/discogs-etl/scripts/dedup_releases.py`, Railway connection resets, pool exhaustion), every cache-bearing method needs to learn the same try/except + cool-down. With the policy duplicated 5×, the fix would land 5× too.

**Deletion test:** delete the cache leg of `get_release`. Its `try: cache → except → fall through to API → write back` block reappears in `get_artist_details` (same shape, different cache method), in `search_releases_by_track` (same shape plus negative cache plus an `artist_as_keyword` gate), in `search` (same shape with no write-back), and in `validate_track_on_release` (same shape with a tri-state return). Concentrating it is the deepening; the write-back drift is the bug it removes.

## Desired end state

One read-through module — `discogs/fallthrough.py` — owns:

- the PG-read → API-fetch → optional-write-back ritual,
- the breadcrumb + cache-stats + Sentry projection at each tier,
- the `should_skip_cache()` bypass at one site,
- the graceful-degrade cool-down from #324 (a per-process timestamp arms on `asyncpg.PostgresConnectionError` / `CacheUnavailableError` from the cache leg and suppresses retries for ~30 s),
- the negative-cache pre-check / post-write hooks (one method uses these today),
- the tri-state read shape that `validate_track_on_release` needs.

Each `DiscogsService` method states only **its cache key and its API fetch** plus, where it diverges, **its explicit write-back policy as a parameter**. The L1 `@async_cached` decorator stays as-is — it owns a separate concern (per-type TTL, normalization, in-process LRU, the skip-cache flag) and is already deep.

## The seam — a function, not a class

```python
# discogs/fallthrough.py
from typing import Awaitable, Callable, Generic, TypeVar
T = TypeVar("T")

async def fallthrough(
    *,
    label: str,                                            # breadcrumb / cache-stat key
    pg_read: Callable[[], Awaitable[T | None]] | None,     # None == no PG leg (get_master, label_image)
    api_fetch: Callable[[], Awaitable[T | None]],
    pg_write: Callable[[T], Awaitable[None]] | None = None,   # absence == read-only by design
    pg_negative_check: Callable[[], Awaitable[bool]] | None = None,
    pg_negative_record: Callable[[], Awaitable[None]] | None = None,
    is_empty: Callable[[T], bool] = lambda r: not r,        # negative-cache trigger
    is_pg_hit: Callable[[T | None], bool] = lambda v: v is not None,  # overrideable for tri-state
    breadcrumb_data: dict[str, Any] | None = None,
) -> T | None:
    ...
```

Each `None` parameter is an **explicit opt-out**, not silent absence — `pg_write=None` reads as "this method is read-only against PG by design" the same way `Outcome.empty()` reads as "this strategy fired but produced no items."

The seam handles:

1. `should_skip_cache()` short-circuit (one site).
2. Cool-down guard: if `time.monotonic() < _cool_down_until` and `pg_read` is set, skip PG and go straight to API (no PG breadcrumb either).
3. PG read leg, if configured. Wrap in `timed_pg()`. On hit (per `is_pg_hit`), emit `cache_hit` breadcrumb + `record_pg_cache_hit()` and return.
4. On PG miss, emit `cache_miss` + `record_pg_cache_miss()`. On PG **exception**, emit `cache_error` + `record_pg_cache_miss()` AND arm the cool-down for ~30 s.
5. Negative-cache pre-check (if configured). On hit, return an empty value via the caller's `is_empty` shape — wait, this needs care. The negative cache today returns `True/False` (hit/miss); on hit, the caller returns a `TrackReleasesResponse(...releases=[]...)`. The seam can't manufacture that — the caller has to. **Resolution:** the seam returns `None` on negative-cache hit; the caller (`search_releases_by_track`) wraps `None` in its empty `TrackReleasesResponse` shape. Or — better — the seam accepts an `on_negative_hit: Callable[[], T]` factory. Decision in the plan-review pass.
6. API fetch via `api_fetch()`. On non-empty result + `pg_write` configured: write back. On confirmed-empty result + `pg_negative_record` configured: write the negative entry. All write-backs are best-effort (try/except → WARN).
7. Return whatever `api_fetch()` produced.

### Tri-state for `validate_track_on_release`

The cache's `validate_track_on_release` returns `True | False | None` where `None` means miss and both bool values are valid hits. Today the orchestrator checks `if cached_result is not None`. The seam already supports this shape — `is_pg_hit=lambda v: v is not None` and `T=bool` make `False` a hit, `None` a miss. The default `is_pg_hit` is exactly that, so validation gets it for free.

The validate API path goes through `get_release` (which already writes back), so the validate call passes `pg_write=None` — its write-back is **indirect, by design**. Documented at the call site.

### Cool-down

```python
# discogs/fallthrough.py
import time
_cool_down_until: float = 0.0
_COOL_DOWN_SECONDS = 30.0

def _arm_cool_down() -> None:
    global _cool_down_until
    _cool_down_until = time.monotonic() + _COOL_DOWN_SECONDS

def _cool_down_active() -> bool:
    return time.monotonic() < _cool_down_until

def _reset_cool_down_for_tests() -> None:  # test-only helper
    global _cool_down_until
    _cool_down_until = 0.0
```

Process-wide. Railway runs single-process workers per replica so there's no cross-worker sharing problem to solve here; a per-replica cool-down is exactly the desired granularity. On cool-down arm, project `data.cache_fallback_fired = {reason, error_class}` onto the active Sentry transaction per #324's AC, and emit a structured WARN log.

### Why a function, not a class

A class (`ReadThroughCache(read, fetch, write).get(key)`) buys nothing — there is no per-instance state beyond what the closures already carry. A decorator (`@read_through(read=..., fetch=...)`) hides too much: the strategy decisions (write-back policy, negative cache, tri-state) become decorator parameters and the call-site reads as "what's special about this method gets argued through indirection," which is exactly the friction the deepening is supposed to remove.

A function with named-keyword parameters keeps every per-method policy decision **at the call site**, in plain syntax, which is what we want for read-through-the-code understanding. The shape mirrors the `Outcome` named-constructor pattern from #399 (just merged).

## Per-method write-back policy table (the documented split)

After this PR, the policy is encoded **at the call site** as the presence or absence of `pg_write`:

| Method | `pg_write` | Why |
|---|---|---|
| `get_release` | `cache_service.write_release` | Full read-through (status quo). Single `ReleaseMetadataResponse` maps cleanly to the cache's `release` / `release_artist` / `release_track` schema. |
| `get_artist_details` | `cache_service.write_artist_details` | Full read-through (status quo). Single `ArtistDetails` maps cleanly to `artist` / `artist_alias`. |
| `search_releases_by_track` | `None` | **Read-only by design.** PG side queries the normalized `release_track` index populated by the ETL pipeline. Writing arbitrary Discogs `/database/search` hits back doesn't fit that schema — they'd be denormalized release stubs without the `release_artist` / `release_track` join data the read query depends on. Negative-cache write-back still fires (a separate write hook, `pg_negative_record`). |
| `search` | `None` | **Read-only by design.** Same shape as above — the PG `search_releases` query is indexed against ETL-populated data; API search hits don't carry the indexed columns. |
| `validate_track_on_release` | `None` | **Read-only by design, indirect API write-back.** Tri-state PG read short-circuits when the cache has an answer. On miss, the API path calls `get_release` which writes back to the `release` cache via its own seam call. So the release row gets persisted; the validation verdict itself isn't separately cached on the API path (matches today's behavior). |

The table sits in `CLAUDE.md` so future drift will be flagged in review (a new method added without an explicit `pg_write` argument or comment is a missed decision, not a missed line). Drift detection is doc-level, not lint-level; the surface is small enough to grep.

## Scope boundaries

### In scope

- Create `discogs/fallthrough.py` with the function above + cool-down + `_reset_cool_down_for_tests`.
- Migrate 5 methods onto it: `get_release`, `get_artist_details`, `search_releases_by_track`, `search`, `validate_track_on_release`.
- Land #324's graceful-degradation try/except + cool-down inside the seam (one site).
- Add unit tests for the seam (`tests/unit/test_fallthrough.py`) covering: PG hit, PG miss → API, write-back fires/skipped, negative-cache hit/miss/write, cool-down arms on cache error, cool-down honors expiry, tri-state hit, `should_skip_cache()` bypass.
- Add a unit test that reproduces #324: `asyncpg.PostgresConnectionError` from `pg_read` arms cool-down, second call within 30 s skips PG and goes straight to API, third call after a forced reset uses PG again.
- Update `CLAUDE.md`: new "Discogs cache fallthrough" section documenting the seam + the policy table.

### Out of scope (deferred / not changed)

- `get_master`, `search_releases_by_album_title`, `get_label_image` — API-only methods. They COULD use the seam with `pg_read=None`, but the API path of each is small enough that the seam doesn't earn its keep. Adding them later is a one-commit follow-up if it ever becomes worth it.
- Adding new PG write-back to `search` / `search_releases_by_track` — the issue explicitly asks to confirm read-only-by-design before flipping. We're encoding it as policy, not flipping it.
- The `@async_cached` L1 decorator — already deep. The skip-cache flag, cache-key normalization (#342), and per-cache TTL split are all working. Leave it alone.
- Integration test against a real `asyncpg.PostgresConnectionError` from a deliberately-undersized pool — #324's AC mentions this. It needs a `pg`-marked integration test with a 1-connection pool and a concurrent burst. Deferred to a follow-up so this PR stays focused on the seam; tracked by leaving #324 open with the unit-test AC satisfied, OR (preferred) folding into this PR if the test fits cheaply. **Decision pending plan review.**
- Validating that `search_releases_by_track`'s `artist_as_keyword` gate behavior is preserved exactly — captured by a regression-pinning unit test that watches the `pg_read` callable is `None` when `artist_as_keyword=True`.

### Test surface

- `tests/unit/test_fallthrough.py` (new, ~12 tests):
  - `test_returns_pg_hit_without_calling_api`
  - `test_pg_miss_calls_api`
  - `test_pg_miss_with_write_calls_write_back`
  - `test_pg_miss_without_write_does_not_write_back`
  - `test_empty_api_with_negative_record_writes_negative`
  - `test_negative_cache_hit_short_circuits_api`
  - `test_pg_error_arms_cool_down_and_falls_back_to_api`
  - `test_cool_down_active_skips_pg_read`
  - `test_cool_down_expires_after_window` (`monkeypatch.setattr(time, "monotonic", ...)`)
  - `test_should_skip_cache_bypasses_all_legs`
  - `test_tri_state_false_treated_as_hit` (`is_pg_hit=lambda v: v is not None`)
  - `test_breadcrumb_data_threaded_through`
- `tests/unit/test_discogs_service.py` regression net stays green — the existing per-method tests cover the migration. If any per-method assertion checked for breadcrumbs or cache-stats fired from the method body, those calls now move into the seam and the assertions need to follow the call.

## Implementation order (TDD-friendly)

1. **Red:** write `tests/unit/test_fallthrough.py` for the seam's behavior. None of these pass — the module doesn't exist.
2. **Green:** create `discogs/fallthrough.py` with the function + cool-down. Make the seam tests green.
3. **Refactor `get_release`** to use the seam — the simplest migration (full read-through, no negative cache, no tri-state). Existing `test_discogs_service` tests stay green.
4. **Refactor `get_artist_details`** — same shape.
5. **Refactor `search`** — first read-only-by-design migration. Surface the `pg_write=None` pattern.
6. **Refactor `validate_track_on_release`** — first tri-state migration. Surface the `is_pg_hit` default.
7. **Refactor `search_releases_by_track`** — most complex (negative cache, `artist_as_keyword` gate). Surface the `pg_negative_check` / `pg_negative_record` / `on_negative_hit` hooks.
8. Update `CLAUDE.md` with the seam description + policy table.
9. Run `ruff check`, `ruff format`, `mypy`, full unit-test suite locally.
10. Open PR with `Closes #393, Closes #324`.

Per the global TDD rule, each "Refactor X" step starts from green tests, makes the swap, confirms green. No drive-by changes.

## Size estimate

~700–900 net lines:

- ~250 new (seam + tests).
- ~400 modifications across `discogs/service.py` (the 5 methods shrink — each loses 30–50 lines of try/except machinery and gains a 10–15 line `fallthrough(...)` call).
- ~50 net changes in `CLAUDE.md`.

Under the 1000-line target. If the `search_releases_by_track` migration plus its `on_negative_hit` factory pushes the diff past 1000, split into two PRs: (a) seam + the 4 read-only / full-write methods, (b) the negative-cache migration of `search_releases_by_track`. Decision deferred to the implementation pass.

## Risks

| Risk | Mitigation |
|---|---|
| Subtle behavior change in the negative-cache leg (`search_releases_by_track`) — the leg today consults the negative cache *after* the PG miss check; the seam folds them. | Add a regression test that exercises both orders explicitly: (a) PG miss + negative-cache hit → no API call, (b) PG miss + negative-cache miss + empty API → negative-cache write. |
| The `is_pg_hit` callback semantics drift between methods (e.g., search returns a `DiscogsSearchResponse` which is never `None` — empty results still mean "miss"). | Each method passes an explicit `is_pg_hit` lambda or — better — the PG-read callable itself returns `None` on empty. Decide at implementation: I lean toward making the PG-read callable's responsibility to return `T | None`, with `None` always meaning "miss." Keeps the seam's default `lambda v: v is not None` honest. |
| Cool-down false-arming on benign errors (timeout, transient). | Distinguish error classes: arm on `asyncpg.PostgresConnectionError`, `asyncpg.InterfaceError`, `asyncpg.PostgresError` subclasses related to availability; do NOT arm on `asyncio.TimeoutError` (which has its own meaning). Specific class list pinned by tests. |
| Per-process global state (`_cool_down_until`) leaks across tests. | `_reset_cool_down_for_tests` fixture in `conftest.py` (autouse for the fallthrough test module). |
| L1 in-memory decorator (`@async_cached`) caches the API result, so a cool-down-active state still returns the cached value on subsequent calls in-window — which is correct, but worth documenting. | One-line note in the seam's docstring + the `CLAUDE.md` section. |

## Convergence with #324

#324's acceptance criteria:

- [x] Unit test that simulates `asyncpg.PostgresConnectionError` from the cache pool and asserts `/lookup` returns successful — covered by the seam test (`test_pg_error_arms_cool_down_and_falls_back_to_api`) plus a thin `discogs/service.py` integration test that wires the seam to a mocked `pg_read` that raises.
- [ ] Integration test that exercises the actual exception class produced by Railway pool exhaustion — deferred (out-of-scope above).
- [x] `data.cache_fallback_fired` projected on the Sentry transaction — fold into the cool-down arm path.
- [x] Cool-down: one cache-leg failure suppresses retries for ~30 s — `_arm_cool_down`.

If we defer the integration test, #324 stays half-open and gets a tracking comment ("seam landed in #393; pool-exhaustion integration test outstanding"). The user-visible AC — `/lookup` no longer 5xx during cache-leg outages — is met by the seam alone.

## What this PR does NOT touch

- `discogs/cache_service.py` — the PG client methods stay as-is. The seam wraps them; it doesn't change their signatures or internals. (The "fold #324's try/except into the seam" line is about wrapping `cache_service` calls, not editing `cache_service` internals.)
- `lookup/orchestrator.py` — no changes; the orchestrator's strategies still call the same `discogs_service` methods. The migration is internal to `discogs/service.py`.
- The 3 sibling deepening tickets (#392/#394/#395) — separate PRs, not blocked by this one.
- The L1 `@async_cached` decorator — unchanged.

## Acceptance criteria mapping

From the issue body:

- [x] **A single read-through module; methods express key + fetch only** → `discogs/fallthrough.py` + the 5 migrated methods.
- [x] **Write-back policy decided in one place and applied consistently (or a documented per-method opt-out)** → Per-method `pg_write` argument; policy table in `CLAUDE.md`.
- [x] **Tier fallthrough has one set of tests instead of per-method coverage** → `tests/unit/test_fallthrough.py`. Per-method tests stay as regression nets but no longer carry the tier logic.
- [x] **#324's graceful-degradation try/except + cool-down lands in this module, not per-method** → `_arm_cool_down` + the cache-leg try/except inside `fallthrough()`.
- [x] **Existing discogs cache/service tests pass** → regression net.

## Decisions from plan review (2026-05-29)

The plan was approved with one HIGH and four lower-severity findings. The decisions below resolve them before implementation begins.

1. **HIGH — Negative-cache hit signal: `on_negative_hit` factory (option b).** Bare `None` collides with "API call returned nothing"; the caller needs the `cached=True` flag on negative-cache hits and `cached=False` on API empties. The seam takes `on_negative_hit: Callable[[], T] | None = None`; on negative-cache hit it returns `on_negative_hit()`. The stats / breadcrumb distinction (`pg_negative_hits` vs. plain `cache_miss`) is emitted by the seam itself; the return type carries the caller's full shape.

2. **MEDIUM — Cool-down arms on a pinned, narrow exception set.** The seam arms its cool-down only on:
   - `asyncpg.exceptions.PostgresConnectionError` (and its subclasses — base "we couldn't reach the DB")
   - `asyncpg.exceptions.CannotConnectNowError` (server starting / not ready)
   - `asyncpg.exceptions.InterfaceError` (pool exhausted, closed connection, driver-level)
   - `asyncpg.exceptions.UndefinedTableError` (the "relation does not exist" shape during a dedup-swap window)
   - the local `CacheUnavailableError` raised by `cache_service` methods
   
   Cool-down does **not** arm on `asyncio.TimeoutError`, `asyncio.CancelledError`, or any bare `Exception`. A test asserts the exact set (the test will fail loudly if a future asyncpg version moves an exception under a different base or if someone widens the arming criteria silently).

3. **MEDIUM — Pool-exhaustion integration test: defer to a follow-up issue.** The seam's unit-test coverage (the simulated `PostgresConnectionError` case) satisfies the user-visible AC of #324 — `/lookup` no longer 5xx when the cache leg throws. The deliberately-undersized-pool integration test is its own work (provisioning + concurrent-burst harness) and would inflate this PR past its size budget. I'll file a focused follow-up ticket for just the integration test, link it from a comment on #324, and close #324 from THIS PR — the integration test becomes its own tracked work, not a half-open AC on #324. (If the reviewer's "decide now" point is taken literally, the answer is "defer" — the test cost outpaces this PR's appetite.)

4. **LOW — Per-method `pg_write` policy gets an inline comment at each call site.** The three `pg_write=None` call sites (`search`, `search_releases_by_track`, `validate_track_on_release`) carry a one-line `# read-only by design: ...` comment explaining why. The `pg_write=cache_service.write_release` and `pg_write=cache_service.write_artist_details` cases are self-documenting. This keeps the policy decision visible at the call site without requiring CLAUDE.md cross-reference.

5. **LOW — `is_empty` default scope tightened.** The default `lambda r: not r` only works for primitive empties. Rather than ship a footgun, the seam only uses `is_empty` on the negative-cache write path (i.e., only when `pg_negative_record` is set), and **requires** the caller to pass an explicit `is_empty` when `pg_negative_record` is set. A `ValueError` at construction-time (parameter validation at the top of the function) prevents silent misuse. For `search_releases_by_track`, the lambda is `lambda r: not r.releases`.

## Resolved open questions

- **`get_master` / `get_label_image` / `search_releases_by_album_title` opportunistic migration?** Skip. Drive-by changes are exactly what the review-loop flags; their inclusion would add ~80 lines for aesthetic-only consolidation. Tracked as a one-line `## Possible follow-up` in the PR body in case the seam's "API-only" shape ever earns its keep on those.

