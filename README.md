# Library Metadata Lookup

A FastAPI service for WXYC radio that searches the library catalog and cross-references results with Discogs metadata. Extracted from [request-o-matic](https://github.com/WXYC/request-o-matic) to separate search/lookup concerns from message parsing and Slack posting.

## What it does

Given an artist, song, and/or album, the service:

1. Corrects artist spelling via fuzzy matching against the library catalog
2. Resolves album names from Discogs when only a song title is provided
3. Searches the library catalog with a multi-strategy fallback chain
4. Validates fallback results against Discogs tracklists
5. Fetches album artwork from Discogs
6. Returns enriched results with metadata

## API

### `POST /api/v1/lookup`

Primary endpoint. Accepts a parsed request and returns library results with artwork.

```json
{
  "artist": "Stereolab",
  "song": "Percolator",
  "raw_message": "Play Percolator by Stereolab"
}
```

Response:

```json
{
  "results": [
    {
      "library_item": {
        "id": 10,
        "artist": "Stereolab",
        "title": "Emperor Tomato Ketchup",
        "call_letters": "S",
        "artist_call_number": 1,
        "release_call_number": 1,
        "genre": "Rock",
        "format": "CD"
      },
      "artwork": {
        "album": "Emperor Tomato Ketchup",
        "artist": "Stereolab",
        "release_id": 123456,
        "release_url": "https://www.discogs.com/release/123456",
        "artwork_url": "https://img.discogs.com/...",
        "confidence": 0.95
      }
    }
  ],
  "search_type": "direct",
  "song_not_found": false,
  "found_on_compilation": false,
  "context_message": null,
  "corrected_artist": null,
  "cache_stats": null
}
```

### `GET /api/v1/library/search`

Direct library catalog search.

### `POST /api/v1/discogs/search`

Search Discogs releases.

### `GET /api/v1/discogs/track-releases`

Find all releases containing a specific track.

### `GET /api/v1/discogs/release/{release_id}`

Get full release metadata from Discogs.

### `GET /health`

Health check with real connectivity probes for the database, Discogs API, and Discogs cache.

## Search Strategy Pipeline

The lookup orchestrator tries strategies in order until results are found:

| Strategy | Condition | What it does |
|---|---|---|
| `ARTIST_PLUS_ALBUM` | Has artist, album, or song | Search by artist + album(s) from Discogs, fall back to artist + song, then artist only |
| `SWAPPED_INTERPRETATION` | No results + ambiguous "X - Y" format | Try both "X as artist, Y as title" and vice versa |
| `TRACK_ON_COMPILATION` | Song not found + has artist and song | Cross-reference Discogs track listings with library to find compilations |
| `SONG_AS_ARTIST` | No results + song parsed but no artist | Try the parsed song title as an artist name |

After the pipeline, if results came from an artist-only fallback (`song_not_found=True`), each album is validated against Discogs tracklists to filter to only albums containing the requested track.

## Project Structure

```
library-metadata-lookup/
  main.py                      # FastAPI app entry point
  config/settings.py           # Environment-based configuration
  core/
    dependencies.py            # DI for LibraryDB + DiscogsService
    matching.py                # Fuzzy matching, diacritics normalization
    search.py                  # Search strategy pattern
    telemetry.py               # PostHog telemetry
    logging.py, sentry.py, exceptions.py
  discogs/
    service.py                 # Discogs API client with caching
    cache_service.py           # PostgreSQL cache (asyncpg + pg_trgm)
    memory_cache.py            # In-memory TTL cache
    lookup.py                  # Track/artist lookup helpers
    models.py, ratelimit.py, router.py
  library/
    db.py                      # SQLite FTS5 + fuzzy fallback search
    models.py, router.py
  lookup/
    orchestrator.py            # Core search pipeline (extracted from request-parser)
    models.py                  # LookupRequest, LookupResponse
    router.py                  # POST /lookup endpoint
  services/
    parser.py                  # Minimal ParsedRequest model (no Groq)
  tests/
    factories.py               # Shared test factories (make_library_item, make_discogs_result)
    unit/                      # 399 mocked unit tests (97% source coverage)
    integration/               # 45 integration tests with real SQLite/FTS5
```

## Development

### Prerequisites

- Python 3.12+
- Uses the shared venv from request-parser for now:

```bash
/Users/jake/Developer/request-parser/venv/bin/python
```

### Running locally

```bash
uvicorn main:app --reload
```

### Running tests

```bash
# Unit tests only (default, fast -- 399 tests)
uv run pytest tests/unit/ -v

# Integration tests only (real SQLite/FTS5 -- 45 tests)
uv run pytest -m integration -v

# All tests with coverage (444 tests, 97% source coverage)
uv run pytest -m "" --cov=. --cov-report=term-missing -v
```

Integration tests use a real in-memory SQLite database with FTS5 and seed data.
They are excluded by default (`addopts = "-m 'not integration'"` in `pyproject.toml`).

## Environment Variables

**Required:**
- `DISCOGS_TOKEN` -- Discogs API token for artwork and track lookups

**Optional:**
- `DATABASE_URL_DISCOGS` -- PostgreSQL URL for Discogs cache (e.g. `postgresql://user:pass@host:5432/discogs`)
- `SENTRY_DSN` -- Sentry error tracking
- `POSTHOG_API_KEY` -- PostHog telemetry
- `LIBRARY_DB_PATH` -- Path to SQLite library database (default: `library.db`)
- `LOG_LEVEL` -- Logging level (default: `INFO`)

### Discogs cache TTL settings

- `DISCOGS_TRACK_CACHE_TTL` -- In-memory track cache TTL in seconds (default: 3600)
- `DISCOGS_RELEASE_CACHE_TTL` -- In-memory release cache TTL (default: 14400)
- `DISCOGS_SEARCH_CACHE_TTL` -- In-memory search cache TTL (default: 3600)
- `DISCOGS_CACHE_MAXSIZE` -- Max entries per cache (default: 1000)

### Discogs rate limiting settings

- `DISCOGS_RATE_LIMIT` -- Max requests/minute (default: 50)
- `DISCOGS_MAX_CONCURRENT` -- Max concurrent requests (default: 5)
- `DISCOGS_MAX_RETRIES` -- Max retries on 429 errors (default: 2)

## Deployment

Hosted on Railway.

- `main` branch auto-deploys to **staging**
- `prod` branch auto-deploys to **production**
- Health check at `/health` with real dependency probes
- Uses the same `Postgres-Nard` PostgreSQL instance as request-parser for Discogs cache

## Relationship to request-parser

This service extracts all search/lookup logic from `request-parser/routers/request.py`. After migration:

- **request-parser**: parse message (Groq) -> call this service -> post to Slack
- **library-metadata-lookup**: search library + Discogs -> return enriched results

The API contract is defined in [`wxyc-shared/api.yaml`](https://github.com/WXYC/wxyc-shared) with generated models for Python, TypeScript, Swift, and Kotlin.
