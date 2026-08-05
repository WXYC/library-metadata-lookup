# LML Capability Matrix

Endpoint-by-endpoint reference for `library-metadata-lookup` consumed by callers across the WXYC stack. Produced for M0.4 (Mojibake P0 audit).

App entry point: `main.py` (FastAPI, mounts seven routers: health, admin, lookup, library, discogs, identity, streaming).

## Endpoint table

| Method + Path | Auth | Request | Response | Confidence in response | Fuzzy / normalization |
|---|---|---|---|---|---|
| `GET /health` (`routers/health.py:49-92`) | None | None | `{status, version, commit_sha, services{database, discogs_api, discogs_cache}}` (200/503). `commit_sha` is the Railway-injected git SHA of the running deploy, or `null` off-Railway — see `docs/deployment.md` "`commit_sha` (deploy identity)". | n/a | n/a |
| `POST /admin/upload-library-db` (`routers/admin.py:114-195`) | **Bearer** `ADMIN_TOKEN` | multipart SQLite file | `{status, row_count, timestamp, webhook?}` | n/a | n/a |
| `POST /api/v1/lookup` (`lookup/router.py:23-96`) | None | `LookupRequest{artist?, song?, album?, raw_message?}` | `LookupResponse{results?[], search_type, song_not_found?, found_on_compilation?, corrected_artist?, context_message?, cache_stats?}` | per-result `artwork.confidence` (0-1, populated when Discogs match exists) | **Full pipeline** — FTS5 → LIKE-with-stopword → rapidfuzz `token_set_ratio` (≥70). Artist correction via `find_similar_artist` (rapidfuzz, threshold 85) auto-replaces `parsed.artist`. Strategy chain: ARTIST_PLUS_ALBUM → SWAPPED_INTERPRETATION → TRACK_ON_COMPILATION → SONG_AS_ARTIST. |
| `GET /api/v1/library/search` (`library/router.py:16-64`) | None | query: `q?, artist?, title?, limit (1-100, default 10)` | `LibrarySearchResponse{results[{id,title,artist,call_letters,...,on_streaming?}], total, query?}` | none | Same FTS5 → LIKE → fuzzy chain as `/lookup`. **`artist=` filter is stricter** — it requires `LOWER(artist) LIKE '<arg>%'` style prefix match (no diacritic stripping on this path), so `artist=μ-Ziq` returns 0 against a library row stored as `µ-Ziq`. The `q=` parameter goes through the full normalization chain and *does* match. |
| `GET /api/v1/discogs/track-releases` (`discogs/router.py`) | None | query: `track` (required), `artist?`, `limit (1-100, default 20)` | `{results[{album?, artist?, release_id, release_url, artwork_url?, confidence?}], total, cached}` | per-result `confidence` (0-1) | Discogs API native search; no library-side normalization. |
| `GET /api/v1/discogs/release/{release_id}` | None | path: `release_id:int` | `ReleaseMetadataResponse{title, year?, artist, label?, genres[], formats[], tracks[], urls[], images[], release_url}` | n/a | n/a |
| `GET /api/v1/discogs/artist/{artist_id}` | None | path: `artist_id:int` | `ArtistDetails{artist_id, name, profile?, profile_tokens?, image_url?, name_variations[], aliases[], members[], urls[], cached}` | n/a | n/a |
| `GET /api/v1/discogs/entity/{entity_type}/{entity_id}` | None | path: `entity_type∈{artist,release,master}`, `entity_id:int` | `EntityResolveResponse{name, type, id}` | n/a | n/a |
| `GET /api/v1/discogs/tracks/autocomplete` | None | query: `artist`, `q`, `release?`, `limit (1-100, default 20)` | `TracksAutocompleteResponse{results[str], total, artist, cached}` | n/a | Prefix match against discogs-cache `tracks` table. |
| `GET /identity/resolve` (`identity/router.py:49-?`) | None | query: `name` (required) | `IdentityResponse{library_name, discogs_artist_id?, wikidata_qid?, musicbrainz_artist_id?, spotify_artist_id?, apple_music_artist_id?, bandcamp_id?, reconciliation_status}` (200/404/503) | none | **Exact** lookup against `entity.identity.library_name`. No fuzzy, no diacritic stripping. Caller must pass the canonical form. |
| `POST /identity/bulk` (`identity/router.py:80-101`) | None | `{names: string[]}` | `BulkIdentityResponse{identities[…IdentityResponse], unresolved[name]}` | none | Same exact-match per name. Sequential, not parallel. Designed for semantic-index batch import (~30K names). |
| `POST /api/v1/streaming-check` (`streaming/router.py:27-61`) | None | `{artist, title}` | `StreamingCheckResponse{on_streaming(bool|null), sources{spotify?, deezer?, apple_music?, bandcamp?}}` each `{url, confidence:0-100}` | per-source `confidence` (0-100) | Provider-specific matching; no library-side normalization. |

## Fuzzy-match behaviour, in detail

This section is the load-bearing input to M1.2 (the LML-assisted lossy mojibake matcher).

### Artist correction (`library/db.py:429-449`, `find_similar_artist`)

- Input: any string. Pulls all distinct `artist` values from `library`, prefilters by 3-char prefix on `wxyc_etl.text.normalize_artist_name(artist)`.
- Scoring: `rapidfuzz.fuzz.token_set_ratio(normalize_artist_name(query), normalize_artist_name(candidate))`.
- `normalize_artist_name` strips diacritics via NFKD + casefold and removes a small set of stopwords / punctuation (it does **not** delete every non-ASCII char — Greek mu becomes `u`, but Cyrillic letters survive).
- Threshold default 85. On hit, the corrected name replaces `parsed.artist` and is surfaced to callers as `corrected_artist`.

### Library catalog search (`library/db.py:168-427`)

Three stages, each fed only when the previous returns 0 results:

1. **FTS5** over the `library_fts` virtual table (indexes `artist`, `title`). Query is the raw `q` string. FTS5 itself is case-insensitive and tokenizer-driven; it tolerates diacritics through Unicode casefold but does **not** strip them. Special characters (slashes, dashes, parens) can throw `sqlite3.OperationalError`, which is caught and falls through.
2. **LIKE fallback** (`_fallback_like_search`). Steps: (a) `normalize_for_comparison()` to strip diacritics, (b) regex `[^a-z0-9\s]` → space, (c) split, (d) drop tokens length ≤1 or in `STOPWORDS`. Each remaining token must match `(artist LIKE '%w%' OR title LIKE '%w%' OR alternate_artist_name LIKE '%w%' OR album_artist LIKE '%w%')`. AND across tokens.
3. **Fuzzy fallback** (`_fuzzy_search`). Same normalization, then `rapidfuzz.fuzz.token_set_ratio` against a 3-char-prefix-filtered candidate set scoring `f"{artist} {title}"`. Threshold 70. Returns top `limit` results, sorted by score descending.

Cache: TTLCache for `find_similar_artist` and `search` results, cleared on `/admin/upload-library-db`.

### Strategy pipeline in `/lookup` (`core/search.py:213-258`, `lookup/orchestrator.py`)

1. `ARTIST_PLUS_ALBUM` — calls `db.search()` with the assembled query, then enforces `artist_matches_item` (artist column starts-with the search artist after normalization, or matches via `alternate_artist_name`) and an album-overlap filter (≥2 common content tokens, or title-prefix when title has ≤2 tokens).
2. `SWAPPED_INTERPRETATION` — for `raw_message` shaped `X - Y` / `X, Y` / `X. Y`, retries with both `X|Y` and `Y|X` orderings.
3. `TRACK_ON_COMPILATION` — Discogs track lookup → matches each release against library with fuzzy album matching (rapidfuzz.fuzz.ratio, threshold 80) → validates the track on the release via Discogs.
4. `SONG_AS_ARTIST` — retries with the parsed song treated as an artist name.

### Identity endpoints

`entity.identity` is keyed by exact `library_name`. There is **no fuzzy match and no diacritic stripping on this path**. Two consequences for mojibake:

- A row stored with the corrupted form is invisible to a query for the corrected form, and vice versa. Any caller that walks names from a freshly-V012'd source against an un-resyncd `entity.identity` will get 404s until the entity store is repopulated (M2.x territory; out of scope here).
- This is the right batch endpoint for callers that have already canonicalized names (semantic-index `--entity-source=lml`, the planned LML-assisted lossy matcher should *not* use this path for fuzzy lookup).

## Live verification (production LML, 2026-04-26)

| Query | Endpoint | Result |
|---|---|---|
| `q=μ-Ziq` (Greek mu, U+03BC) | `GET /library/search` | 3 hits — finds rows stored as `µ-Ziq [mu-Ziq]` (MICRO SIGN, U+00B5) via FTS5 + diacritic strip path. |
| `q=u-Ziq` (ASCII) | `GET /library/search` | 3 hits — diacritic-stripped fuzzy fallback. |
| `artist=μ-Ziq` | `GET /library/search` | **0 hits** — artist-prefix filter does not strip diacritics. ⚠️ |
| `artist=µ-Ziq` (MICRO SIGN) | `GET /library/search` | 3 hits — exact byte match. |
| `POST /lookup {artist:"μ-Ziq", album:"Lunatic Harness"}` | `/lookup` | `search_type=direct`, returns `µ-Ziq [mu-Ziq] / Lunatic Harness`. ✅ |
| `POST /lookup {artist:"u-Ziq", album:"Lunatic Harness"}` | `/lookup` | `search_type=alternative`, **0 hits**. ⚠️ |
| `POST /lookup {artist:"mu-Ziq", album:"Lunatic Harness"}` | `/lookup` | `search_type=alternative`, **0 hits**. ⚠️ |
| `POST /lookup {artist:"µ-Ziq", album:"Lunatic Harness"}` | `/lookup` | `search_type=direct`, hit. ✅ |
| `POST /lookup {artist:"μ-Ziq"}` (no album) | `/lookup` | `search_type=fallback`, 5 hits. ✅ |
| `POST /lookup {artist:"Σtella"}` | `/lookup` | 0 hits — artist not in WXYC catalog (not a fuzzy bug). |
| `GET /identity/resolve?name=Stereolab` | `/identity/resolve` | 200 / 404 normally; **503** when `DATABASE_URL_DISCOGS` is unset or the `entity` schema is missing (fixed in #169). |
| `POST /identity/bulk` | `/identity/bulk` | 200 normally; **503** when entity store is unavailable, including mid-request `asyncpg.PostgresError` (fail-closed; fixed in #169). |

## Codepoint subtlety: U+00B5 vs U+03BC

The library stores `µ-Ziq` with **U+00B5 (MICRO SIGN)**, not **U+03BC (GREEK SMALL LETTER MU)**. They render identically in nearly every font but are distinct codepoints with distinct UTF-8 bytes (`C2 B5` vs `CE BC`). NFKC compatibility decomposition maps U+00B5 → U+03BC, but the LML normalization stack does **not** apply NFKC; it only strips diacritics via NFKD + filtering. As a consequence:

- The two forms collide *only* through the diacritic-strip fallback (both → `u`), not through any direct comparison.
- V012 corrects mojibake bytes back to whatever the original UTF-8 source had. If the original was U+00B5, the catalog already matches. If it was U+03BC, the catalog will need a sync after V012 lands.
- Callers should not depend on either codepoint being authoritative.

## Recommendation for M1.2 (LML-assisted lossy matcher)

Use `POST /api/v1/lookup` with `artist` and (when available) `album`. This is the only endpoint that runs the full normalization pipeline including diacritic stripping and rapidfuzz fallback, exposes `corrected_artist`, and returns library + Discogs metadata in one call. Top-1 result is sorted, so taking `results[0]` plus `artwork.confidence` is a workable score for shortlisting human-review candidates. Sequential calls per name are fine (no batch endpoint for `/lookup` exists; ~few-hundred candidates is the expected scale).

Do **not** use `GET /library/search?artist=…` for fuzzy mojibake matching — its artist filter is byte-strict.

Do **not** use `/identity/resolve` or `/identity/bulk` for fuzzy lookups — they are exact-match by design.

A new endpoint is **not** required.
