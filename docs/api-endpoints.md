# API Endpoints

Non-lookup endpoints exposed by the service. The `POST /lookup` endpoint is covered in [`architecture.md`](architecture.md).

## Identity Resolution Endpoints

The service exposes REST endpoints for querying the `entity.identity` table in the discogs-cache PostgreSQL database. These endpoints are consumed by semantic-index (via `--entity-source=lml`) and other pipeline tools.

- `GET /identity/resolve?name=Stereolab` -- Look up a single artist name. Returns 200 with external IDs or 404.
- `POST /identity/bulk` with `{"names": ["Stereolab", "Autechre", ...]}` -- Resolve a batch of names. Returns `identities` (found) and `unresolved` (not found).
- `POST /api/v1/identity/bulk-resolve-libraries` -- Cross-cache-identity contract endpoint per the 2026-05-09 pivot (BS#800). Backend POSTs library rows; LML composes per-source provenance via §3.4.1.1 Rules 2-6 and returns one verdict per row (`kind: single_artist | compilation | unresolved`). Implementation lives in `identity/bulk_resolve.py`; sits under `/api/v1/` so it inherits `LML_API_KEY` bearer auth.

Both endpoints return 503 when `DATABASE_URL_DISCOGS` is not set or the entity schema is not applied.

## Streaming Check Endpoint

`POST /api/v1/streaming-check` checks whether an album is available on streaming platforms. Used by tubafrenzy and Backend-Service to set the `on_streaming` flag when a new release is added to the library.

Request: `{"artist": "Stereolab", "title": "Aluminum Tunes"}`

Response includes `on_streaming` (true/false/null) and per-service match details with URLs and confidence scores. Checks run concurrently across Spotify, Deezer, Apple Music, and Bandcamp. The endpoint is stateless -- it does not cache results.

Requires `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` for Spotify checks. Other services (Deezer, Apple Music, Bandcamp) need no auth. If Spotify credentials are not set, Spotify checks are skipped.

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
