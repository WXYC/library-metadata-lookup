# API Endpoints

Non-lookup endpoints exposed by the service. The `POST /lookup` endpoint is covered in [`architecture.md`](architecture.md).

## Identity Resolution Endpoints

The service exposes REST endpoints for querying the `entity.identity` table in the discogs-cache PostgreSQL database. These endpoints are consumed by semantic-index (via `--entity-source=lml`) and other pipeline tools.

- `GET /identity/resolve?name=Stereolab` -- Look up a single artist name. Returns 200 with external IDs or 404.
- `POST /identity/bulk` with `{"names": ["Stereolab", "Autechre", ...]}` -- Resolve a batch of names. Returns `identities` (found) and `unresolved` (not found).
- `POST /api/v1/identity/bulk-resolve-libraries` -- Cross-cache-identity contract endpoint per the 2026-05-09 pivot (BS#800). Backend POSTs library rows; LML composes per-source provenance via §3.4.1.1 Rules 2-6 and returns one verdict per row (`kind: single_artist | compilation | unresolved`). Implementation lives in `identity/bulk_resolve.py`; sits under `/api/v1/` so it inherits `LML_API_KEY` bearer auth.
- `POST /api/v1/identity/resolve` -- Release-identity resolve endpoint added by LML#526. Accepts `{kind, source, external_id}` and returns `{identity_id, kind, minted}`. Idempotent: the same triple always returns the same `identity_id`; only the first call mints. Validation runs *before* any DB write — Discogs `external_id <= 0`, malformed Bandcamp URLs, and unknown sources are rejected with 422, so no poisoned identity rows can be created. Concurrency-safe: two concurrent mints converge on one row via the per-source `UNIQUE` constraints on `entity.release_identity` (see `entity/release_identity.sql` for the canonical DDL). Validation + coercion live in `identity/release_validation.py`; storage helpers (`mint_or_get_release_identity`, `get_release_identity_by_source`, `log_release_reconciliation`) live in `entity/store.py`. v1 accepts only `kind: release`; `kind: artist` is the documented follow-up. Use `GET /identity/resolve?name=<artist>` for the artist-name-keyed case — separate endpoint on the open router.

These endpoints return 503 when `DATABASE_URL_DISCOGS` is not set or the entity schema is not applied.

## Cache Refresh Endpoint

`POST /api/v1/cache/refresh-for-identities` warms LML's Discogs cache for a batch of `entity.release_identity.id` values. Source-agnostic by design (LML#525) — Backend hands LML the canonical identity IDs and lets LML do the source-to-external-id mapping. Used by Backend-Service's rotation-artist-backfill cron (WXYC/Backend-Service#1381) so BS no longer needs to hold Discogs IDs directly (those are unstable across Discogs reshuffles).

Request: `{"identity_ids": [42, 99, 12345]}`. Max 50 ids per request — over the cap returns 400. Empty list returns 422. Duplicates are allowed; each runs independently.

Behavior per id:
1. Read `entity.release_identity` once for the whole batch (one PG round trip, not N).
2. For each `(source, external_id)` pair on the row, dispatch to the per-source release-cache refresh. Today only `discogs_release` is wired; `discogs_master`, `musicbrainz_release`, `spotify_album`, `apple_music_album`, and `bandcamp` return `release_outcome = "not_implemented"`.
3. For each refreshed Discogs release, walk `release.artists[*].artist_id` (skipping `artist_id <= 0` — the LML#525 sentinel guard extending the LML#518 / LML#546 caller-validates audit posture) and refresh the per-artist cache.
4. Roll up per-source outcomes into the per-id `status`: `warmed | not_found | not_implemented | error`. `not_found` covers "no row in `entity.release_identity`" (no 500); `warmed` requires at least one source `release_outcome == "success"`; `error` is reserved for "no useful release-cache work happened" so it is the only retry signal. Tombstones (LML#510) count as `success` — the cache state is current.

Response carries per-id results only — no top-level counters. Callers derive them via `Counter(r.status for r in results)`, matching the `BulkLookupResultItem` precedent.

The dispatcher loop mirrors `/api/v1/lookup/bulk` (`lookup/router.py`): `asyncio.gather` under a bounded semaphore (`LML_BULK_MAX_CONCURRENT`, default 10, shared with the lookup endpoint), per-item `try/except` so one identity's failure cannot poison siblings, and a `watch_disconnect` sentinel race so a client abort cancels the gather and frees downstream Discogs rate-limit / semaphore permits. Per-replica Discogs concurrency / rate-limit gates from `discogs/ratelimit.py` apply for free through `DiscogsService.get_release` / `get_artist_details`.

Implementation lives in `cache/`:
- `cache/models.py` — Pydantic shapes (the api.yaml entry generates structurally identical types on the Backend-Service side).
- `cache/dispatch.py` — per-identity orchestration: the per-source dispatch table, the walk-to-artists step with the `> 0` sentinel guard, and the per-id `status` rollup (all pure-logic, unit-tested in `tests/unit/test_cache_dispatch.py`).
- `cache/router.py` — the FastAPI handler, batch concurrency primitives shared with `lookup/router.py` via `core/bulk_concurrency.py`.
- `entity/store.py:get_release_identity_provenance_bulk` — the bulk PG read returning `{identity_id: [(source, external_id), ...]}`.

Sentry spans: `cache.refresh.batch` around the gather, `cache.refresh.identity` per identity, `cache.refresh.release` per source leg, `cache.refresh.artist` per walk target. Numeric attributes (`identity_id`, `external_id`) are set on the span at open, never via late binding.

## Streaming Check Endpoint

`POST /api/v1/streaming-check` checks whether an album is available on streaming platforms. Used by tubafrenzy and Backend-Service to set the `on_streaming` flag when a new release is added to the library.

Request: `{"artist": "Stereolab", "title": "Aluminum Tunes"}`

Response includes `on_streaming` (true/false/null) and per-service match details with URLs and confidence scores. Checks run concurrently across Spotify, Deezer, Apple Music, and Bandcamp. The endpoint is stateless -- it does not cache results.

Requires `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` for Spotify checks. Other services (Deezer, Apple Music, Bandcamp) need no auth. If Spotify credentials are not set, Spotify checks are skipped.

Telemetry (LML#639): every request emits a `streaming_check_completed` PostHog summary event (when `ENABLE_TELEMETRY` is on) carrying per-request total/step timing, an `outcome` (`success`/`error`) + `error_type`, and a per-service verdict (`verdict_<service>`, plus `on_streaming`, `match_count`, `errored_count`, `errored_sources`). Both outcomes share one schema. On success each `verdict_<service>` is `matched`/`errored`/`absent` derived from the response — note partial (per-service) failures come back as a 200 with `on_streaming=null` and the failing services in `errored_sources`/`verdict_<service>=errored` (LML#376). On a total failure (the 500 path, when `check_streaming_availability` itself raises) the event is still emitted with `outcome=error`, `error_type` set, and every verdict `unknown`. The `cache` and `api_calls` fields are emitted with a stable, all-zero shape — the path is application-cache-free and the clients record no API calls yet; real values land in [LML#641](https://github.com/WXYC/library-metadata-lookup/issues/641). The emit is best-effort and wrapped in its own swallow (a telemetry failure logs at WARNING and never fails the check nor masks the 500, since this endpoint is on the synchronous flowsheet-add path).

## Release Resolve Endpoint

`POST /api/v1/releases/resolve` takes a Discogs release URL or Bandcamp album URL (or an explicit `(source, id)` pair) and returns canonical release metadata, cross-source identifiers, and a streaming-availability snapshot — everything tubafrenzy's rotation-release create form needs to prefill in one round trip.

Request (one of):
```json
{ "url": "https://www.discogs.com/release/12345" }
{ "url": "https://artist.bandcamp.com/album/slug" }
{ "source": "discogs_release", "id": "12345" }
```

Response:
```json
{
  "source": "discogs_release",
  "source_id": "12345",
  "canonical": { "artist": "Juana Molina", "title": "DOGA", "label": "Sonamos", "catno": "SON-001", "year": 2024, "country": null, "formats": [] },
  "identifiers": { "discogs_release_id": 12345, "discogs_artist_id": 999, "spotify_album_id": "abc...", "bandcamp_album_url": null, ... },
  "streaming": { "on_streaming": true, "sources": { ... } },
  "warnings": []
}
```

Implementation lives in `release/`:
- `url_parser.py` — pure URL → `(source, id)` parser. Supports Discogs `/release/<id>` and `/master/<id>` (with locale prefixes and slugs) and Bandcamp `<artist>.bandcamp.com/album/<slug>`.
- `discogs_resolver.py` — wraps `DiscogsService.get_release()` so the existing 3-tier cache + API rate-limit handling applies.
- `bandcamp_resolver.py` — fetches the Bandcamp album page (rate-limited via the existing `BandcampClient`) and parses the embedded JSON-LD `MusicAlbum` blob. Self-released (publisher subdomain == artist subdomain) returns `label: null`.
- `orchestrator.py` — dispatches by source, runs `check_streaming_availability`. When the input is a Bandcamp URL, the Bandcamp leg of the streaming check is short-circuited (skipped) — we already know the answer and re-fuzzy-matching would burn 2+ rate-limited HTTP calls. Identity write-back via the existing `EntityStore.upsert_identity()` (`ON CONFLICT ... DO UPDATE` with `COALESCE` — never clobbers); Bandcamp `bandcamp_id` is the URL's slug, matching what `bandcamp_pipeline.py` writes.

Genre and style are intentionally not surfaced — the rotation form has no genre field, so the music director picks manually.

The endpoint always returns 200 with a `warnings[]` array. Partial failures (Discogs rate limit, malformed Bandcamp page, missing master support) become warnings rather than 5xx; the form falls back to manual entry.

`source` may be `"discogs_release"`, `"discogs_master"`, `"bandcamp"`, or `"unknown"`. Consumers should always check `warnings` before consuming `canonical`.
