# LML#525 — `POST /api/v1/cache/refresh-for-identities`

## Problem

There is no source-agnostic "warm LML's cache for these canonical releases" surface. Callers that want to back-fill the Discogs cache for a known set of releases have to hit `/api/v1/discogs/release/{id}` and `/api/v1/discogs/artist/{id}` directly, which bakes in three couplings the issue (LML#525) details: callers have to hold Discogs IDs (unstable across Discogs reshuffles), there's no multi-source fan-out, and walk-to-artists belongs in LML.

This plan tracks the LML-side implementation. The companion Backend-Service ticket is BS#1381.

## Desired end state

`POST /api/v1/cache/refresh-for-identities` registered under `_lml_protected`, taking `{ "identity_ids": [int, ...] }` (max 50), responding with a per-id results array (no top-level counters — derived by caller). Internal dispatcher mirrors `/api/v1/lookup/bulk` (asyncio.gather + bounded semaphore + `_watch_disconnect` cancellation). The Discogs leg is wired; `discogs_master`, `musicbrainz_release`, `bandcamp`, `spotify_album`, `apple_music_album` return `release_outcome = "not_implemented"`.

## Module layout

```
cache/
  __init__.py
  models.py        # Pydantic request/response shapes (LML-local)
  router.py        # POST /cache/refresh-for-identities + dispatcher loop
  dispatch.py      # Per-source release-refresh + walk-to-artists dispatch table
```

`cache/` matches the existing domain-owned router convention (`identity/`, `lookup/`, `release/`, `discogs/`). `dispatch.py` is split out so the dispatcher table is unit-testable without spinning up the FastAPI app.

Models live in `cache/models.py` initially. The api.yaml addition (step 10) generates a parallel set on the Backend-Service side; LML's internal models can survive as Pydantic-local definitions in v1 because the response is not consumed by any LML-internal code path other than the router itself — same pattern as `BulkLookupResultItem` in `lookup/models.py`.

## Pydantic shapes (cache/models.py)

```python
ArtistRefreshOutcomeStatus = Literal["success", "error", "not_implemented"]
SourceRefreshOutcomeStatus = Literal["success", "error", "not_implemented"]
CacheRefreshItemStatus = Literal["warmed", "not_found", "not_implemented", "error"]


class ArtistRefreshOutcome(BaseModel):
    external_id: str
    outcome: ArtistRefreshOutcomeStatus
    message: str | None = None


class SourceRefreshOutcome(BaseModel):
    release_outcome: SourceRefreshOutcomeStatus
    artists: list[ArtistRefreshOutcome] = []
    message: str | None = None


class CacheRefreshResultItem(BaseModel):
    identity_id: int
    status: CacheRefreshItemStatus
    sources: dict[str, SourceRefreshOutcome] | None = None
    message: str | None = None


class BulkCacheRefreshRequest(BaseModel):
    identity_ids: list[int] = Field(..., min_length=1)


class BulkCacheRefreshResponse(BaseModel):
    results: list[CacheRefreshResultItem]
```

Per-id `status` rollup is computed in the dispatcher, not on the Pydantic model — keeps the model dumb and the rollup tested in isolation.

## Store change (entity/store.py)

Add `get_release_identity_provenance_bulk(identity_ids: list[int]) -> dict[int, list[tuple[str, str]]]`. Reads `entity.release_identity` rows directly (per-source columns, NOT the reconciliation log — issue rationale: simpler shape, single query).

SQL pattern mirrors the existing `_BULK_GET_IDENTITY_EXACT_SQL` (uses `WHERE id = ANY($1::int[])`):

```sql
SELECT id,
       discogs_release_id, discogs_master_id, musicbrainz_release_id,
       spotify_album_id, apple_music_album_id, bandcamp_album_url
FROM entity.release_identity
WHERE id = ANY($1::int[])
```

Pythonside, decompose each row into the `(source, external_id)` pairs that are non-null:

- `discogs_release_id` → `("discogs_release", str(id))`
- `discogs_master_id` → `("discogs_master", str(id))`
- `musicbrainz_release_id` → `("musicbrainz_release", mbid)`
- `spotify_album_id` → `("spotify_album", id)`
- `apple_music_album_id` → `("apple_music_album", id)`
- `bandcamp_album_url` → `("bandcamp", url)`

Return dict keyed by `identity_id`. Missing identity_ids are absent from the dict (caller renders them as `not_found`).

External IDs are stringified at the boundary so callers don't have to branch on `int`-vs-`str` types. The router's `ArtistRefreshOutcome.external_id` is `str` per the issue spec; this matches.

## Dispatcher (cache/dispatch.py)

Three layers:

1. **Source-release dispatch table** — `{source: callable}` mapping each source key to a coroutine `(external_id, discogs_service) -> (release_record_or_none, status, message)`. Today only `"discogs_release"` is wired; the rest return `("not_implemented", "...")`.

2. **Per-identity orchestration** — `refresh_identity(identity_id, source_pairs, discogs_service) -> CacheRefreshResultItem`:
   - For each `(source, external_id)` pair, look up the dispatch entry. Call it under a Sentry span (`cache.refresh.release`).
   - When the source returned a release record (Discogs only today), walk `release.artists` and refresh each artist via the second dispatch table. **The walk-to-artist extractor includes `c.artist_id > 0 and c.artist_id is not None` — this is the LML#525-specific sentinel guard.**
   - Roll up per-source release/artist outcomes into per-id `status` per the priority order from the issue.
   - Wraps the per-identity work in a `cache.refresh.identity` span.

3. **Source-artist dispatch** — `{source: callable}` mapping for the walk step. Today only `"discogs"` (note: not `"discogs_release"` — Discogs's release.artists list belongs to the canonical Discogs artist source). Returns `ArtistRefreshOutcome`. Wrapped in `cache.refresh.artist` span.

Sentry numeric attributes are set at span creation via `start_span(..., attributes=...)` — never via late `setAttribute` (per the issue's note on BS#1081 convention; in Python the equivalent is `attributes=` to `start_span()` for numeric fields, or `span.set_data` for non-numeric/string attributes that don't get the string-indexed-numeric trap).

## Router (cache/router.py)

Largely mirrors `lookup/router.py:259-500`. Differences:

- Cap is 50 (not 100), enforced before Pydantic validation (so the manual 400-on-overflow returns before Pydantic's 422 for empty body).
- No `cache_stats` projection — this endpoint doesn't drive lookup stats; the relevant signal is Sentry spans + per-id outcome counts.
- No `posthog_telemetry` projection — cron-shaped traffic, not user-shaped. Sentry transaction is the durable signal.
- The `_run_one` body calls `refresh_identity(identity_id, source_pairs.get(identity_id, []), discogs_service)` and per-item exception isolation returns `CacheRefreshResultItem(identity_id=..., status="error", sources=None, message=type(e).__name__)`.
- Bulk PG query runs **once** at batch entry, before fan-out (`store.get_release_identity_provenance_bulk(request.identity_ids)`), so the fan-out doesn't burn N round-trips.
- `_max_concurrency_from_env()` is **shared** with `lookup/router.py`: hoist it to a small module (e.g. `core/bulk_concurrency.py`) and import from both. The env knob `LML_BULK_MAX_CONCURRENT` and default 10 are reused per the issue ("Reuse `LML_BULK_MAX_CONCURRENT` (default 10). Both endpoints have the same outer/inner gate shape").
- `_watch_disconnect` and `_cancel_and_drain` are reused. Move them alongside `_max_concurrency_from_env()` so both routers can import without circular dep.

## Wiring (main.py)

Add a sibling include after `identity_api_v1_router`:

```python
from cache.router import router as cache_router
...
app.include_router(cache_router, prefix="/api/v1", tags=["cache"], dependencies=_lml_protected)
```

## Tests

### Unit-level (tests/unit/)

- `tests/unit/test_cache_dispatch.py` — dispatch table tests:
  - `discogs_master` returns `not_implemented`
  - Empty source list → per-id `not_implemented`
  - Per-id `status` rollup priority (warmed > not_implemented > error)
  - Walk-to-artist guard: synthesize a release record with `artists=[ArtistCredit(artist_id=0, name="VA"), ArtistCredit(artist_id=42, name="Real")]` and assert only artist_id 42 is dispatched.

### Integration (tests/integration/test_cache_refresh_for_identities.py, `@pytest.mark.pg`)

Use the same `set_up_entity_schema` fixture pattern as `test_release_identity.py` (drop+recreate the schema). Seed `entity.release_identity` rows directly with `INSERT INTO entity.release_identity (...) VALUES (...)` for the source-column matrix tests.

Use an `AsyncMock` `DiscogsService` injected via `app.dependency_overrides[get_discogs_service]` so we control:
- `get_release(release_id)` return value (real `ReleaseMetadataResponse` with `artists`)
- `get_artist_details(artist_id)` return value
- Whether a call raises (for error-path tests)

Test cases:

1. **happy path, single Discogs release with two artists** → status `warmed`, `sources["discogs_release"].release_outcome == "success"`, two entries in `artists` with `outcome == "success"`.
2. **unknown identity_id** → status `not_found`, `sources is None`, no 500.
3. **tombstone (404 → `ReleaseMetadataResponse(not_found=True, ...)`)** → status `warmed`, `release_outcome == "success"`, `artists == []` (the public `get_release` boundary translates tombstones to None per service.py:957 — but here we'll test the fall-through behavior; if the boundary returns None we treat that as success for the cache state ("we know there's no release" is current state) but the walk-to-artist step has no artists to walk. Tombstone-as-success means we don't error; we just record the leg as ran.
4. **`discogs_master` only** → status `not_implemented`, `sources["discogs_master"].release_outcome == "not_implemented"`.
5. **`discogs_release` + `discogs_master` both populated** → status `warmed` (via discogs_release leg), `sources["discogs_master"].release_outcome == "not_implemented"`, `sources["discogs_release"].release_outcome == "success"`.
6. **walk-to-artist sentinel guard** — Discogs release returns `artists=[ArtistCredit(artist_id=0, name="VA"), ArtistCredit(artist_id=42, name="Real")]`. Assert `mock.get_artist_details` was called once with `42`, never with `0`. Assert `sources["discogs_release"].artists` has one entry, `external_id == "42"`.
7. **client disconnect during gather** — mirror the existing pattern (race a fake disconnect; the gather is cancelled, response is 499).
8. **batch over 50** → 400.
9. **empty `identity_ids`** → 422.
10. **duplicate `identity_ids`** → 200, each runs independently, response carries duplicates.
11. **per-item failure isolated** — one identity_id's Discogs call raises, that id gets `status == "error"`, siblings unaffected.

Skip explicit semaphore-fairness assertion in the integration test (covered structurally by reusing `LML_BULK_MAX_CONCURRENT` + the same dispatcher shape). Add it later if drift surfaces.

## TDD order (Red → Green → Refactor per Red-cycle)

0. **Prerequisite: hoist concurrency primitives** — Create `core/bulk_concurrency.py` and move `_max_concurrency_from_env()`, `_watch_disconnect()`, `_cancel_and_drain()` out of `lookup/router.py`. Re-export public names from the new module; swap `lookup/router.py`'s definitions for imports. Run the existing `tests/integration/test_lookup_pipeline.py` (or any test that exercises `/lookup/bulk`) to confirm `/lookup/bulk` still passes. This step has no new test of its own — its correctness is "the existing test surface still passes" — but it has to land before any router-level code in the new module.

1. `test_get_release_identity_provenance_bulk_empty_input` → implement empty-list short-circuit.
2. `test_get_release_identity_provenance_bulk_single_discogs_release` → implement the SQL + row-to-pairs decomposition.
3. `test_get_release_identity_provenance_bulk_multi_source_row` → expand decomposition.
4. `test_get_release_identity_provenance_bulk_missing_ids_absent` → confirm absence-not-None contract.
5. `test_dispatch_discogs_master_returns_not_implemented` → write dispatch table skeleton.
6. `test_dispatch_discogs_release_calls_get_release_and_walks_artists` → wire the Discogs leg.
7. `test_walk_to_artist_skips_id_zero` → add the sentinel guard at the extractor.
8. `test_rollup_warmed_when_any_success` → implement per-id rollup.
9. `test_rollup_not_implemented_when_only_not_implemented` → expand rollup.
10. `test_rollup_error_when_all_failed` → expand rollup.
11. Router-level integration tests (1-11 above), each one driving incremental changes to the router.

## Acceptance criteria mapping (from LML#525)

Every box in the issue's "Acceptance criteria" section maps to one of the above tests + the dispatcher / router code. Recap:

- Registered in main.py — main.py edit, smoke-tested by `app_client.post("/api/v1/cache/refresh-for-identities", ...)` returning 200.
- Response shape matches Pydantic sketch — covered by `BulkCacheRefreshResponse` schema + tests 1-3.
- Per-id rollup priority — covered by rollup tests 8-10.
- Per-source three-value enum + tombstone-as-success — covered by test 3.
- `get_release_identity_provenance_bulk` on store — covered by store tests 1-4.
- Discogs release leg wired, others not_implemented — covered by tests 4-5.
- Walk-to-artist with `> 0` guard — covered by test 6.
- 50-id cap, 400 on overflow, 422 on empty, duplicates allowed — covered by tests 8-10.
- Dispatcher mirrors `/lookup/bulk` (asyncio.gather + LML_BULK_MAX_CONCURRENT + try/except + `_watch_disconnect`) — covered by client-disconnect test 7 + per-item failure isolation test 11.
- Discogs work through `get_semaphore()` / `get_rate_limiter()` — inherited for free by calling `discogs_service.get_release` / `get_artist_details` (those go through `_request_with_retry` which acquires both gates).
- Per-item failures don't tear down batch — covered by test 11.
- Unknown identity_id returns `not_found`, `sources=None` — covered by test 2.
- `discogs_master`-only not_implemented vs. dual-populated split — covered by tests 4 + 5.
- Sentry spans `cache.refresh.identity` / `.release` / `.artist` with `identity_id`/`source`/`external_id` attributes — instrumented in dispatcher; assertion via Sentry test harness if available (otherwise verified manually pre-merge and surfaced in the PR description).
- Integration tests cover the listed scenarios — items 1-11 above.
- Response shape locked in `wxyc-shared/api.yaml` — separate commit step.
- Docs updated in `docs/api-endpoints.md` — separate commit step.

## Out-of-scope (per LML#525 "Out of scope")

- `discogs_master` release-cache refresh + master persistence layer
- MusicBrainz release-cache refresh (gated on LML#217)
- Bandcamp / Spotify / Apple Music album-cache refresh
- `get_release_bulk` cache-only projection (LML#520-style)
- `force: true` request flag
- Per-source semaphore registry (Discogs has one; MB/etc. will when wired)

## Risks / open questions

- **Tombstone → public boundary returns `None`** (service.py:957). The dispatcher needs to discriminate "release-leg ran and the cache is now current" (success) from "release-leg got `None`" (could be tombstone-success or could be a real error). The fix: catch tombstone via the `get_release` boundary's behavior — when the underlying call doesn't raise but returns `None`, we treat as `release_outcome = "success"` with empty `artists`. If we need finer-grained tombstone-vs-error discrimination later, that's a follow-up (would require either reading `cache_service` directly or a new bool return).
  - Update: per the issue, "tombstones count as `success`. … Consumers that need to distinguish 'warm-real' from 'warm-tombstone' should read `release.not_found` from the cache directly, not infer from the response." So our boundary treatment (success on either real release or `None`-from-tombstone) is correct contract-wise; we just need a try/except around the call to discriminate "raised" from "returned None."

- **Sentry attributes API** — Python `sentry_sdk` doesn't have BS#1081's exact `Sentry.startSpan({ attributes })` signature; the equivalent is `start_span(op=..., name=..., attributes={...})` (numeric attributes set at span creation) and `set_data(key, value)` for late binding. For numeric attributes we set them via `set_data` immediately after `start_span` (no late binding through nested helpers), which matches the spirit of the BS#1081 convention.

- **Concurrency primitives hoisted to `core/bulk_concurrency.py`** — touches `lookup/router.py` to swap the import. Small mechanical change, but does cross a module boundary. Worth flagging in PR description so reviewer doesn't ask why `lookup/router.py` shows up in the diff.
