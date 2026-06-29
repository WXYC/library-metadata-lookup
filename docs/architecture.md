# Architecture

## Lookup Flow

1. **Artist Correction**: Fuzzy match artist against library catalog to fix typos
2. **Album Resolution**: If song provided without album, query Discogs for album names
3. **Search Pipeline**: Execute strategies in order until results are found (see below)
4. **Track Validation**: If fallback returned all artist albums, validate each against Discogs tracklists. When validation can't confirm any candidate AND we're showing artist-fallback results, `find_library_albums_with_cached_track()` consults the local PG cache directly ("releases by this artist whose tracklist contains this song") and promotes any matching library album over the unrelated fallback. Cache-only — never falls back to the API.
5. **Artwork Fetch**: Fetch album art from Discogs for each result
6. **Metadata Enrichment**: Populate `release_year`, `artist_bio`, `wikipedia_url`, streaming URLs. Release/artist details are fetched only for `items_with_artwork[0]` — BS/iOS only consume the top-1 result, so paying N Discogs round-trips for non-top-1 items is waste. Streaming-URL fallbacks stay per-result. Gating is *positional*: if the top-1 entry has `artwork=None`, no item carries release-year/bio/wiki, even if items further down have artwork. The lookup pipeline guarantees the strongest match is in position 0, so this is fine in practice. See `enrich_artwork_results()` in `lookup/orchestrator.py`.
7. **Context Message**: Generate context string for the caller

### `LookupRequest` opt-in flags

- `extended: bool` (default `false`) — When true, the top-1 result's `artwork` block carries additional fields LML already loads during enrichment but normally discards: `discogs_artist_id`, `tracklist`, `genres`, `styles`, `label`, `full_release_date`, `artist_image_url`, `profile_tokens`, `writer_credits`. Bio parsing uses `CachedOnlyResolver` (cache-only deep parse) when `DATABASE_URL_DISCOGS` is set; falls back to sync `parse()` otherwise. Designed to let Backend-Service collapse `/proxy/metadata/album` to one LML call, eliminating the follow-up `/discogs/release/{id}` and `/discogs/artist/{id}` round-trips.
  - `writer_credits` (LML#699, for BMI performance-list reporting) is the songwriter/composer subset of the resolved release's already-fetched credits, classified by the writer-role heuristic in `discogs/writer_roles.py` (strip `[qualifier]` → split on `,` → fold hyphen/space → match a fixed base set: `Written-By`, `Composed By`, `Music By`, `Lyrics By`, `Songwriter`, `Words By`; `Arranged By`/`Adapted By` excluded). The `[qualifier]` strip runs before the comma split so a bracket-internal comma (`Written-By [Words, Music]`) doesn't fragment the role. No new Discogs call. `provenance` resolves at two precisions: **track** — when the played `song` matches a tracklist position (`find_track_position` in `discogs/service.py`) and that track carries its own writer credits (the `release_track_artist` `extra=1` rows, read by `cache_service.get_release` into the internal `ReleaseMetadataResponse.track_writers` map, keyed by display position), the credit is scoped to the playcut and tagged `provenance: track` with `track_position`; this path is correct even on a compilation, so it deliberately bypasses the VA guard. **release** — otherwise it falls back to the writer-role subset of `top1_release.extra_artists` (the whole-release approximation), tagged `provenance: release` with `track_position: null`, and is omitted for compilations/Various-Artists (the release-level credit is meaningless per-track). Omitted when no writer resolves — never fabricated. Both precisions additionally require artist-identity verification (the LML#504 split gate's `is_artist_derived_eligible`, intersected with the album gate): a fuzzy album-title match to a *different* artist's release suppresses `writer_credits` entirely rather than leaking the wrong composers into a royalty-reporting payload. Surfacing it on `DiscogsMatchResult` lets it ride the existing `album_metadata` passthrough to Backend-Service, which populates a `flowsheet` composer column with artist-as-proxy fallback.
- `warm_cache: bool` (default `false`) — When true, schedules a fire-and-forget `asyncio.create_task` after the response is composed that runs the *deep* async parse (API-capable resolver) on the top-1 bio. Warms the PG cache for `[a…]`/`[r…]`/`[m…]` references so subsequent reads render richer. Bounded process-wide by `_WARM_CACHE_CONCURRENCY` (currently 4) to cap Discogs API amplification. Read-path callers leave this `false`; write-path callers (Backend-Service's `flowsheet-linkage.service.ts` on flowsheet-entry creation) set it `true`.

## Search Strategy Pipeline

The strategy seam (`Outcome` value type, `Strategy` Protocol, `_apply` write site, `execute_search_pipeline` runner) lives in `core/search.py`. Each concrete strategy is a `@dataclass(frozen=True)` class in `lookup/strategies/`, one module per strategy; the orchestrator-side execute funcs that do the actual library/Discogs work still live in `lookup/orchestrator.py` and are injected into each strategy at construction time.

| Strategy | Trigger | Class | Execute func |
|---|---|---|---|
| `ARTIST_PLUS_ALBUM` | Has artist, album, or song | `lookup/strategies/artist_plus_album.py` | `search_library_with_fallback()` |
| `SWAPPED_INTERPRETATION` | No results + "X - Y" / "X, Y" / "X. Y" format | `lookup/strategies/swapped_interpretation.py` | `search_with_alternative_interpretation()` |
| `TRACK_ON_COMPILATION` | Song not found + artist + song | `lookup/strategies/track_on_compilation.py` | `search_compilations_for_track()` |
| `SONG_AS_ARTIST` | No results + song but no artist | `lookup/strategies/song_as_artist.py` | `search_song_as_artist()` |
| `SONG_AS_TRACK` | SONG_AS_ARTIST ran and returned empty (catalog-track-search §4.2) | `lookup/strategies/song_as_track.py` | `search_song_as_track()` |

Strategies never mutate `SearchState`; they return an `Outcome` and the runner applies it via `_apply`. This makes cancellation safety structural — a cancelled `attempt()` is a no-op against state by construction. Production wiring lives in `lookup.strategies.build_strategies`, called from `perform_lookup`.

`SWAPPED_INTERPRETATION` is **Discogs-touching** (LML#622): once it identifies the artist side, it cross-references the *other* part as a track and narrows to the release(s) that actually contain it, rather than returning the artist's whole discography. The release→library matching reuses the same kernel as `SONG_AS_TRACK` (`_match_track_releases_to_library` — Discogs track search → `search_album_fuzzy` → deferred tracklist validation, bounded by `_chunked_gather` + a `MAX_SEARCH_RESULTS` early-exit). SWAPPED passes both `artist=` (filters the Discogs search) and `require_artist=` (re-filters the matched library rows to the identified artist), so the narrowing stays the artist's own release(s) and the keyword-supplement fallback can't leak another artist's release in; VA compilations are SONG_AS_TRACK's domain (it runs with `artist=None`). When narrowing fires it emits a `matched_via` map (an `Outcome.track_match`, like `SONG_AS_TRACK`); when nothing cross-references — or no `discogs_service` is wired — it returns the artist-filtered result via `Outcome.found` as before. Of the strategies, only `ARTIST_PLUS_ALBUM` is library-only.

## Key Files

- `lookup/orchestrator.py` -- Core search logic: `perform_lookup()` and all helper functions
- `lookup/models.py` -- Re-exports generated API contract models (`LookupRequest`, `LookupResponse`, `LookupResultItem`)
- `generated/api_models.py` -- Pydantic v2 models generated from `wxyc-shared/api.yaml`
- `lookup/router.py` -- `POST /lookup` endpoint
- `library/db.py` -- SQLite FTS5 search with LIKE + fuzzy fallback chain. Detects `compilation_track_artist` table at connect time; when present, artist searches include compilations featuring that artist via JOIN/UNION.
- `discogs/service.py` -- Discogs API client with optional PostgreSQL cache
- `discogs/cache_service.py` -- PostgreSQL cache (asyncpg + pg_trgm). Tier 5 in the [org cache-hierarchy reference](https://github.com/WXYC/wiki/blob/main/architecture/cache-hierarchy.md).
- `discogs/memory_cache.py` -- In-memory TTL cache (cachetools). Tier 4 in the [org cache-hierarchy reference](https://github.com/WXYC/wiki/blob/main/architecture/cache-hierarchy.md); per-cache TTLs + maxsizes documented there with the upstream and downstream tiers in context.
- `discogs/fallthrough.py` -- Read-through cache seam (#393): one `fallthrough()` function owns L2-PG → L3-API + optional write-back + cool-down on cache outage. Five `discogs/service.py` methods call it. See the "Discogs cache fallthrough seam" section below for the per-method policy table.
- `core/search.py` -- Declarative search strategy pattern + ambiguous format detection
- `discogs/markup_parser.py` -- Discogs markup parser: tokenize/resolve `[a=Name]`, `[a12345]`, `[b]...[/b]`, etc. into structured `ResolvedToken` models. Includes `EntityResolver` protocol and `DiscogsServiceResolver` adapter for async ID resolution. Translated from iOS `DiscogsMarkupParser.swift`.
- `discogs/matching.py` -- Discogs-specific normalization (strip_discogs_suffix, normalize_for_track_comparison, normalize_artist_for_validation)
- `core/dependencies.py` -- FastAPI DI for LibraryDB + DiscogsService
- `streaming/router.py` -- `POST /streaming-check` endpoint for single-release streaming availability; emits a best-effort `streaming_check_completed` PostHog summary event on both success and the 500 failure path (timing + `outcome`/`error_type` + per-service verdict; `cache`/`api_calls` stable-but-zeroed pending LML#641). The emit is in its own swallow so telemetry can never fail the check nor mask a 500 (synchronous flowsheet-add path)
- `streaming/orchestrator.py` -- Concurrent streaming checks across Spotify, Deezer, Apple Music, Bandcamp
- `streaming/models.py` -- Request/response Pydantic models (`StreamingCheckRequest`, `StreamingCheckResponse`)
- `streaming/dependencies.py` -- FastAPI DI for streaming service clients (SpotifyClient, DeezerClient, etc.)
- `identity/router.py` -- `GET /identity/resolve` and `POST /identity/bulk` endpoints for identity resolution
- `identity/models.py` -- Pydantic models for identity resolution responses
- `identity/dependencies.py` -- FastAPI DI for EntityStore (reuses `DATABASE_URL_DISCOGS` pool)
- `entity/store.py` -- Entity store CRUD against `entity.identity` PG table
- `entity/sources.py` -- `PgSource` / `SparqlSource` transports backing the entity store
- `clients/bandcamp.py` -- Bandcamp HTTP client (autocomplete + page scraping)
- `clients/streaming/{spotify,deezer,apple_music,base,matching}.py` -- Streaming-service HTTP clients (Spotify, Deezer, Apple Music) sharing `base.BaseStreamingClient`, plus the streaming-side text matching/scoring helpers
- `entity/streaming_url_cache.py` -- Persistent `(service, artist, album)` → streaming-URL cache in `lml_cache.album_streaming_url_cache` (LML#573). Exposes the non-probing reads `get_cached_streaming_url` (URL only) and `peek_cached_streaming_url` (URL + "has a fresh decision" bit), vs. the read-through `resolve_streaming_url_with_cache` (cache → live probe → UPSERT) seam.
- `lookup/streaming_url_postprocess.py` -- `/api/v1/lookup` per-service streaming-URL backstop. **Cache-read-only on the response path** (LML#706): a `peek` makes a three-way choice — hit fills the URL synchronously; known-recent miss does nothing; genuine miss enqueues one bounded, deduplicated background warm (`resolve_streaming_url_with_cache` → cache write-back → mint), so the live probe never blocks the response. Bound via `LML_STREAMING_WARM_CONCURRENCY`; the warm is suppressed on `/lookup/bulk` via `set_suppress_streaming_warm`. (Distinct from the Discogs fallthrough seam below — a different cache pattern.)

## Discogs Cache (Optional)

The service supports an optional PostgreSQL cache for Discogs data:

1. Query local PostgreSQL cache first
2. On cache miss, query Discogs API
3. Write API results back to cache
4. Gracefully degrade to API-only if cache unavailable

Set `DATABASE_URL_DISCOGS` to enable. The cache schema is defined in [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl).

## Discogs cache fallthrough seam (`discogs/fallthrough.py`)

The 3-tier read-through (in-memory L1 via `@async_cached` → PostgreSQL L2 → Discogs API L3 + optional write-back) is concentrated in `discogs/fallthrough.py:fallthrough()`. Each cache-bearing method in `discogs/service.py` calls the seam with its own `pg_read` / `api_fetch` / `pg_write` closures and, where relevant, the negative-cache hooks. The L1 `@async_cached(<CACHE>)` decorator stays per-method — it owns a separate, already-deep concern (per-type TTL, cache-key normalization, the `should_skip_cache()` bypass).

### Per-method write-back policy

The drift the deepening (#393) removes — every `pg_write=None` call site documents *why* it's read-only:

| Method | `pg_write` | Why |
|---|---|---|
| `get_release` | `cache_service.write_release` | Full read-through. `ReleaseMetadataResponse` maps cleanly to the cache's normalized schema. |
| `get_artist_details` | `cache_service.write_artist_details` | Full read-through. Same shape. |
| `search_releases_by_track` | `None` | Read-only by design — the PG side queries the normalized `release_track` index populated by the ETL pipeline. Writing arbitrary Discogs `/database/search` hits back wouldn't fit that schema. Negative-cache write still fires via `pg_negative_record`. |
| `search` | `None` | Read-only by design — same shape. |
| `validate_track_on_release` | `None` | Read-only by design. Tri-state PG read short-circuits when the cache has an answer; on miss, the API path calls `get_release` which writes back to the `release` cache via its own seam call. The validation verdict itself isn't separately cached. |

When adding a new cache-bearing method, pass an explicit `pg_write=` argument (or `pg_write=None` with a one-line comment explaining why). The seam's signature makes the policy decision visible at the call site.

### Cool-down on cache outage (LML#324)

When a `pg_read` raises one of a narrow set of "DB unreachable" exceptions — `asyncpg.exceptions.PostgresConnectionError`, `CannotConnectNowError`, `InterfaceError`, `UndefinedTableError` (the dedup-swap window), or the local `CacheUnavailableError` — the seam arms a process-wide cool-down for `_COOL_DOWN_SECONDS` (default 30s). While active, `pg_read` is short-circuited and the seam jumps straight to the API leg. `asyncio.CancelledError` propagates; everything else (programming errors, `asyncio.TimeoutError`) falls through to the API without arming.

The arming exception set lives in `_ARMING_EXCEPTIONS` in `discogs/fallthrough.py` and is pinned by tests in `tests/unit/test_fallthrough.py` so a future asyncpg release adding new exception classes can't silently widen the arming criteria.

Cool-down arm projects `data.cache_fallback_fired = {reason, error_class, cool_down_seconds}` onto the active Sentry transaction, matching the pattern used by `_log_album_title_fallback` in `lookup/orchestrator.py`. Queryable in the Sentry trace explorer to track outage incidents.

## External-Cache Fallback for `/api/v1/lookup` (Phase 1.5 mojibake recovery)

`POST /api/v1/lookup` accepts an opt-in `include_external_caches: bool` flag (default `false`). When the WXYC library catalog returns no results AND the request supplies an `artist` field AND the flag is set, the orchestrator runs a fuzzy artist-name search against the discogs-cache PostgreSQL DB; on miss it falls through to musicbrainz-cache. The matched canonical name is wrapped in a synthetic `LookupResultItem` (`library_item.id = 0`, `call_number = "(external)"`, `library_url = ""`) so the caller's existing scoring code applies as-is. The response carries an `external_source` field — `'library' | 'discogs' | 'musicbrainz' | null` — for provenance.

Used by the lossy-mojibake matcher (`tubafrenzy/scripts/db/recovery/lossy_mojibake_recovery.py`) to recover canonical artist names for skeletons not in the WXYC physical catalog. Implementation in `lookup/external_search.py`; the discogs-cache trigram query lives in `discogs/cache_service.py:search_artists_by_name`. Both legs UNION their primary artist table with the alias/variation table (discogs: `artist_name_variation`; musicbrainz: `mb_artist_alias`) so ASCII transliterations and alternate spellings hit, and the canonical primary name is what comes back.

Wiring:
- `DATABASE_URL_DISCOGS` (already required for the standard cache) covers the discogs leg.
- `DATABASE_URL_MUSICBRAINZ` is new — when unset the MB leg is skipped silently.
- Existing callers (no flag) see no behavior change and no extra queries.

## Streaming-match telemetry (LML#592)

Every winning streaming match emits a lightweight `matcher.match` Sentry span (via `record_match_telemetry` in `clients/streaming/matching.py`) carrying the per-axis fuzzy scores. It is emitted once per resolved match on both surfaces: the album path (`find_best_source_match`, all adapters) and the Apple track path (`find_track_metadata`, which inlines its own `token_set_ratio` floor). Span data keys:

- `matcher.service` — `"apple_music" | "spotify" | "deezer" | "bandcamp"`
- `matcher.surface` — `"album" | "track"`
- `matcher.artist_score` / `matcher.title_score` — raw fuzzy scores (0-100) of the winner
- `matcher.marginal_artist_clear` — `true` when the artist score is marginal (`[SCORE_MATCH_ACCEPTANCE_FLOOR, MARGINAL_ARTIST_CEILING)`) against a high title (`>= HIGH_TITLE_FLOOR`) — the short-name-collision signature (e.g. "Wand" matching "Wanda" on a shared album title)
- `matcher.query_artist` / `matcher.matched_artist` / `matcher.query_title` / `matcher.matched_title` — the request value vs. the winning candidate's value, attached **only on the marginal subset**. The flag + scores measure the marginal-clear *rate*; these strings let a production sample be hand-labeled for the false-positive *rate* (the scores alone can't tell an organic false match `Wand`→`Wanda` from a legitimate `Wand`→`WAND` casing/punctuation diff — both land at ~88). Marginal-only so names don't ride the whole match stream.

The count of `matcher.match` spans is the denominator and the `marginal_artist_clear` subset is the numerator of the marginal-clear rate. This is **instrumentation only** — it measures the 80/80 floor's behavior so the artist-axis floor can be tightened later on evidence, not assumption. Telemetry failures are swallowed (best-effort), so they never break a lookup.
