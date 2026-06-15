# Plan: persistent Apple Music URL lookup for albums regardless of library status

## Goal

For every `/api/v1/lookup` request, return a non-null `apple_music_url` whenever Apple Music has the album — independent of whether the album has a `library.db` row. Persist the resolved URL so subsequent lookups for the same (artist, album) hit a cache rather than re-querying Apple Music.

## Problem (today)

`lookup/orchestrator.py:2333` runs the Apple Music probe with `(row_artist, search_term)` where `row_artist = item.alternate_artist_name or item.artist` (line 2255) — the artist of the **library row that survived resolution**, not the request's artist. Two failure modes flow from this:

1. **No library match** → either zero results returned by the orchestrator (Sessa case from the live repro) or a wrong-row fallback (Hyd → "Angel" by "Angel"). The probe is either skipped entirely or runs with the wrong artist, producing `apple_music_url: null`.
2. **Library match with shaky title** → LML#487 widens the synth branch to fire when the row's title doesn't fuzzy-match the request, LML#505 then nulls any curated streaming URLs. Apple has no search-URL fallback at `:2509-2525`, so it stays null.

No part of LML currently caches Apple Music URLs persistently — every probe is a fresh API call. The 17-21% Sentry "miss" rate is a floor on the user-visible gap, since misses caused by the 80/80/80 floor are tagged as "hit".

## Approach

Add a post-process step at the end of `enrich_one` that runs AFTER all existing per-item enrichment. For every item whose `apple_music_url` is `None` AND the request has a non-empty artist + album:

1. Look up `(artist_normalized, album_normalized)` in a new PG cache table.
2. If hit and not stale, use the cached value (URL or known-miss sentinel).
3. If miss or stale, call `apple_music.find_track_metadata` (or `find_album_match` when no song is supplied) using the **request's** artist/album/song values, not the row's.
4. Write the result back to the cache (URL on hit, null + timestamp on miss for TTL'd re-checks).
5. If the resolved URL is non-null, also mint the album_id into `entity.release_identity.apple_music_album_id` via the existing `mint_or_get_release_identity` API so the entity graph stays current.

Layered on top of the existing path: librarian overrides still win when present and trusted, the existing probe still runs in-line. The post-process is a backstop, not a replacement.

## Schema

New table, owned by `entity` schema (same convention as `entity.release_identity`):

```sql
CREATE TABLE IF NOT EXISTS entity.album_apple_music_lookup_cache (
    artist_normalized TEXT NOT NULL,
    album_normalized TEXT NOT NULL,
    apple_music_url TEXT,
    apple_music_album_id TEXT,
    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (artist_normalized, album_normalized)
);
```

Normalization uses `wxyc_etl.text.to_match_form` (already in use throughout LML for fuzzy comparison normalization). Hit/miss semantics:

- `apple_music_url IS NOT NULL` → durable hit; never re-checked unless explicitly invalidated.
- `apple_music_url IS NULL AND now() - last_checked_at < INTERVAL '7 days'` → known recent miss; skip the API call.
- `apple_music_url IS NULL AND now() - last_checked_at >= INTERVAL '7 days'` → stale miss; re-check.

The "miss with TTL" model lets the cache shrink Apple Music API load to one call per (artist, album) every 7 days for misses, and zero for hits.

DDL file: `entity/album_apple_music_lookup_cache.sql` (reference, matches the `entity/release_identity.sql` pattern).

## Schema bootstrapping

The runtime schema lives in the discogs-cache PG, owned by the discogs-cache repo. To land same-day without a multi-repo PR coordination cycle, LML applies the `CREATE TABLE IF NOT EXISTS` on FastAPI startup via a new lazy-init hook in `main.py`. The statement is idempotent; subsequent runs no-op. A follow-up PR to discogs-cache adds the table to that repo's canonical schema.

## Implementation steps

1. **Migration reference**: write `entity/album_apple_music_lookup_cache.sql` with the DDL above. Comment block notes the cross-repo expectation.
2. **Startup bootstrap**: add a helper `set_up_apple_music_cache_schema(pg) -> None` in `entity/apple_music_album_cache.py` that runs the DDL via `pg.execute(...)`. Call it from `main.py`'s lifespan hook (lines 54-70) **after** `setup_logging` but **before** `yield`. The call site:

   ```python
   # inside lifespan(), after the existing logger.info lines:
   if settings.database_url_discogs:
       try:
           pg = await get_discogs_cache_pg()  # existing pool getter from core.dependencies
           await set_up_apple_music_cache_schema(pg)
           logger.info("Apple Music album cache schema ready")
       except Exception:
           logger.exception("Apple Music cache schema bootstrap failed — cache disabled")
           # No re-raise: the cache layer's get/set will catch and degrade gracefully.
   ```

   This matches the existing posture in `lookup/orchestrator.py` for the resolver pre-pass (degrade to no-op when PG is unavailable). The cache service's `get`/`set` wrap their queries in `try/except` and return "miss" on any PG error, so a bootstrap failure doesn't break /lookup — the post-process just becomes one extra Apple API call per request, same as today. Mirrors the integration-test fixture pattern at `tests/integration/conftest.py::set_up_entity_schema`.
3. **Cache service**: new module `entity/apple_music_album_cache.py` exporting:
   - `async def set_up_apple_music_cache_schema(pg) -> None` — idempotent `CREATE TABLE IF NOT EXISTS`, called from the startup hook above.
   - `async def get(pg, artist: str, album: str) -> CacheResult` — returns `(url: str | None, is_known_miss: bool, is_stale: bool)`. `is_stale` honors the 7d miss TTL.
   - `async def set(pg, artist: str, album: str, url: str | None) -> None` — UPSERT via `INSERT ... ON CONFLICT (artist_normalized, album_normalized) DO UPDATE`.

   The module is the **single owner** of normalization. Both `get` and `set` call `wxyc_etl.text.to_match_form` on the inbound `artist` / `album` strings before keying. The orchestrator passes raw request strings; it does NOT pre-normalize. This keeps the normalization invariant local to the cache and prevents key drift if the orchestrator's normalization choices change in the future. Import: `from wxyc_etl.text import to_match_form` (already used in `clients/streaming/apple_music.py:53` for the same purpose).
4. **Orchestrator post-process**: in `lookup/orchestrator.py:enrich_one`, after the existing `update` dict is built (around line 2536) but before return, run the post-process. Reuses the in-scope `apple_music` client. Guarded by a feature flag (`LML_PERSIST_APPLE_MUSIC_URL`, default `false` initially, flipped to `true` after one staging tick) so the rollout is reversible without a re-deploy.

   The flag is added to `config/settings.py` as `lml_persist_apple_music_url: bool = Field(default=False, ...)` (matching the `LML_RESOLVE_ARTIST_CANONICAL` pattern at the existing settings file), and documented in `docs/env-vars.md` under the "feature flags" section with default + rollback guidance.
5. **Mint into release_identity**: when a fresh URL is resolved, extract the album_id and call `EntityStore.mint_or_get_release_identity("apple_music_album", album_id)`. Failures here are logged but don't block the response (the URL is already cached and returned).

   The extractor already exists at `release/orchestrator.py:217` as the module-private `_apple_album_id_from_url`, with the regex `_APPLE_ALBUM_ID_RE` defined just above (line 208). Both move together into a new `release/apple_music_url_parser.py` module (alongside the existing `release/url_parser.py`), re-exported as the public name `apple_album_id_from_url`. Backwards-compatible aliases (`_apple_album_id_from_url = apple_album_id_from_url`, `_APPLE_ALBUM_ID_RE = APPLE_ALBUM_ID_RE`) stay at the original site so any callers we missed continue to work. The `tests/unit/release/test_orchestrator.py:15-104` import + parametrized test follows the move. No regex changes — pure relocation. Grep `_APPLE_ALBUM_ID_RE` before merging to confirm no remaining external references.
6. **Backwards-compat**: the existing probe at line 2333 is unchanged. Library-row overrides and the synth branch continue to behave exactly as before. The new logic only fires when `apple_music_url` came out None at the existing assignment site.

## Telemetry

Two Sentry tags on the per-request transaction:

- `apple_music.persistent_lookup.fired = true|false` — did the post-process run?
- `apple_music.persistent_lookup.source = "cache_hit" | "cache_miss_recent" | "live_resolved" | "live_miss"` — outcome.

Lets us quantify cache hit rate post-deploy without inspecting bodies.

## Test plan (TDD)

**Fixtures**:
- Cache PG: re-use the existing `pg_pool` fixture from `tests/integration/conftest.py`. Schema bootstrap uses a **function-scoped** fixture (`set_up_apple_music_cache_schema(pg)` + DROP at teardown), mirroring `test_release_identity.py`'s function-scoped `set_up_entity_schema`. Function scope keeps tests isolated and matches the project's existing fixture convention.
- Apple Music client mock: `AsyncMock(spec=AppleMusicClient)` with `find_track_metadata.return_value = AppleMusicTrackMatch(url="https://music.apple.com/us/album/foo/1234567890", artwork_url=None, release_year=None)` for hits, `return_value=None` for misses. Reuses the existing mock pattern at `tests/unit/test_apple_music_client.py:62`.
- Feature flag: `monkeypatch.setenv("LML_PERSIST_APPLE_MUSIC_URL", "true")` per-test. The settings cache (`functools.lru_cache` on `get_settings()`) is cleared via `get_settings.cache_clear()` before each test — implementation will grep for an existing usage in `tests/unit/` to confirm a precedent and cite it; if no precedent exists, add a fixture in `tests/unit/conftest.py` that calls `cache_clear()` and is auto-used by env-var-sensitive tests.
- Frozen time: `freezegun` for the TTL test (already a project dependency per the existing `tests/unit/test_apple_music_client.py` pattern).

**Unit** (`tests/unit/test_apple_music_album_cache.py`):
1. `get` returns `(url=None, is_known_miss=False, is_stale=False)` when no row exists.
2. `set("Stereolab", "Aluminum Tunes", "https://...")` then `get("Stereolab", "Aluminum Tunes")` returns `(url="https://...", is_known_miss=False, is_stale=False)`.
3. `set("Stereolab", "Aluminum Tunes", None)` records a miss with the current timestamp; `get` returns `(url=None, is_known_miss=True, is_stale=False)`.
4. Same setup as #3, advance time 8 days with `freezegun` → `get` returns `is_stale=True`.
5. Normalization symmetry: `set("Nilüfer Yanya", "PAINLESS", url)` and `get("Nilufer Yanya", "painless")` resolve the same row.
6. UPSERT idempotency: two consecutive `set` calls with different URLs leave only one row; the latest URL wins.

**Unit** (`tests/unit/test_lookup_orchestrator_apple_music_persistence.py`):
1. **Post-process fires when feature flag ON and apple_music_url is None**: enrich_one with `LMLPersistAppleMusicURL=true` + `apple_music_url=None` + request `(artist="Hyd", album="Hold Onto Me Infinity", song="Angel")` → asserts `apple_music.find_track_metadata` called with `("Hyd", "Angel", album="Hold Onto Me Infinity")` (NOT the row's artist), cache `set` called, response carries the URL.
2. **Post-process skipped when feature flag OFF**: same setup with flag `false` → no extra Apple call, no cache write.
3. **Post-process skipped when apple_music_url already set**: existing probe populated the URL → no extra Apple call.
4. **Cache hit short-circuits Apple call**: cache `get` returns `(url="https://existing", is_known_miss=False, is_stale=False)` → no Apple call, response carries cached URL.
5. **Known-miss within TTL skips Apple call**: cache `get` returns `(url=None, is_known_miss=True, is_stale=False)` → no Apple call, response stays None.
6. **Stale miss re-checks**: cache `get` returns `(url=None, is_known_miss=True, is_stale=True)` → Apple call fires, cache `set` rewrites with current timestamp.
7. **Sentry tags**: each path (cache_hit / cache_miss_recent / live_resolved / live_miss) asserts the corresponding `apple_music.persistent_lookup.source` value via the existing `sentry_sdk.get_current_scope` mock pattern (`tests/unit/test_bulk_lookup_endpoint.py:579-802`).
8. **Mint side-effect**: live_resolved path asserts `EntityStore.mint_or_get_release_identity` called with `("apple_music_album", "1234567890")` — album_id parsed from the URL.
9. **PG error degrades gracefully**: cache `get` raises → post-process catches, falls through to live Apple call (or skip if Apple errors too), response is unaffected.

**Integration** (`tests/integration/test_apple_music_persistent_lookup.py`, `@pytest.mark.pg`):
1. **End-to-end**: real PG fixture + mocked Apple client, POST `/api/v1/lookup` with Hyd payload → response has non-null `apple_music_url`, `SELECT * FROM entity.album_apple_music_lookup_cache` shows the new row.
2. **Idempotency**: same POST twice → second hits cache, Apple mock called exactly once.
3. **release_identity mint side-effect**: `SELECT apple_music_album_id FROM entity.release_identity WHERE apple_music_album_id IS NOT NULL` includes the parsed ID after the first request.
4. **Schema bootstrap idempotent**: call `set_up_apple_music_cache_schema(pg)` twice → no error, table unchanged.

## Risk

- **Apple Music quota**: cache amortizes cost; first-time uncached lookups still pay one API call each. Apple's 60 req/sec sustained budget absorbs current `/lookup` rates with room to spare.
- **Wrong URL written to cache**: the 80/80/80 floor in `find_track_metadata` blocks low-confidence matches before they reach the cache. If a bad URL does get cached, manual eviction is a single DELETE.
- **Backend write-path strip (BS#1192)**: Backend's `metadata.service.ts:79-95` deliberately omits Apple Music synthesis. The URLs we're surfacing here are **entity-resolved**, not synthesized — they're exactly what BS#1192 expects to receive and persist. No coordination needed.
- **Race on the same (artist, album)**: PRIMARY KEY + `INSERT ... ON CONFLICT DO UPDATE` for writes. Concurrent writers converge.
- **Schema bootstrap fails**: the startup hook logs and continues; cache becomes a no-op, behavior degrades to today's. Not a deploy blocker.

## Out of scope (separate follow-ups)

- Spotify/YouTube Music/Bandcamp/SoundCloud parity (the user asked for Apple Music today).
- LML#505 invalidation tightening (separate redesign — agent finding showed the simple one-line fix is wrong due to score_match paren-stripping).
- Bulk backfill for the ~50K library albums missing Apple Music URLs (Phase 5 / ETL track per the parent investigation).

## Cross-repo coordination

The schema lives in LML for now (bootstrapped at startup via `CREATE TABLE IF NOT EXISTS`). The runtime PG is owned by the discogs-cache repo. **Filing a discogs-cache issue upfront is NOT required for this PR to ship** — the LML startup hook creates the table idempotently on first boot, and discogs-cache's `CACHE_TABLES_TO_TRUNCATE_*` lists are wipe-by-membership (not by omission), so a new table outside those lists is safe.

A follow-up issue will be filed in `WXYC/discogs-etl` AFTER this PR merges, asking the discogs-cache maintainers to adopt the schema into their canonical DDL (matching how `entity.release_identity` was eventually adopted). That follow-up is opportunistic; it does not block this PR or its rollout.

## Rollback

Two switches:
1. Feature flag `LML_PERSIST_APPLE_MUSIC_URL=false` immediately disables the post-process (no re-deploy needed). Existing behavior restored.
2. `DROP TABLE entity.album_apple_music_lookup_cache` removes the cache; the orchestrator path no-ops on PG errors anyway.
