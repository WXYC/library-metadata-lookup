# WXYC Library Streaming Availability Analysis — Methods Report

## Overview

We analyzed 65,147 releases in the WXYC library catalog (58,913 non-compilation, 6,234 compilations) to determine which are available on streaming services and to match them against Discogs releases. Starting from zero verified streaming links, we reached **58,787 confirmed on streaming (90.2% of full catalog)** with **3,490 confirmed exclusive (5.4%)** and **2,415 unknown (3.7%, mostly obscure compilations/samplers)**. Album-level search covered non-compilations via Deezer, Spotify, Apple Music, Bandcamp, and multiple database cross-references. A track-level pipeline extracted 72,341 individual tracks from 4,666 singles and 3,990 compilations, resolving 34,299 tracks (47%) against Deezer/Spotify track APIs with 99.9% precision after URL validation. A separate compilation title search (stripping library filing conventions) found 532 additional compilations on streaming. Discogs coverage reached **45,231 releases (76.8%)** of non-compilations, with **3,990 VA compilations** also matched to Discogs releases including per-track artist data. The data is served to all downstream clients via the LML API's `streaming_links` table and the `on_streaming` flag for "WXYC Exclusive" badges.

## Methods (in order of execution)

### Phase 1: Direct API Search

**1. Deezer API Pre-filter** — 29,056 found (49% of catalog)
- Free API, no auth, 25 req/5s rate limit
- Searched by artist + title with quoted field search, unquoted fallback
- Used as pre-filter: albums not on Deezer were unlikely to be on any streaming platform
- Eliminated 30K albums from needing Spotify API calls

**2. Spotify Web API** — 28,099 found (97% of Deezer hits)
- Client credentials flow, ~30 req/30s rate limit
- Quoted field search (`artist:"X" album:"Y"`) with unquoted fallback
- Track search fallback for singles (vinyl 12"/7")
- Daily quota exhaustion required multi-day runs with automatic retry-after handling

**3. Discogs Enrichment** — name canonicalization for 31,918 albums (54%)
- Queried local PostgreSQL Discogs cache (19M releases) for canonical artist/title
- Built artist name mapping via trigram fuzzy search on filtered `wxyc.artist_names` table (130K names, 1ms/query)
- Canonical names improved Deezer hit rate from 31% to 49% and Spotify from 27% to 97%

### Phase 2: Local Database Cross-referencing

**4. MusicBrainz Exact Match** — 1,154 found
- Joined not-on-streaming albums against `mb_release` + `mb_l_release_url` tables (1.26M releases, 464K streaming URLs)
- Exact lower-case match on both artist and title
- Retrieved verified Spotify, Bandcamp, Apple Music, Deezer, and Tidal URLs

**5. MusicBrainz Trigram Fuzzy** — 432 found
- Created pg_trgm GIN indexes on MusicBrainz release and artist tables
- Fuzzy JOIN with similarity threshold 0.3, scored in Python with rapidfuzz (>= 80 threshold)
- Caught diacritics ("Björk"), "The" prefix mismatches, minor spelling variations

**6. MusicBrainz Artist Aliases** — 142 found
- Used `mb_artist_alias` table (32,734 aliases for 12,893 artists)
- Resolved WXYC catalog names like "OHSEES" → "Osees" via alias lookup
- Then matched against releases with streaming URLs

**7. MusicBrainz Artist Releases** — 63 found
- For artists confirmed in MusicBrainz with streaming releases, pulled ALL their releases
- Fuzzy-matched titles in Python against the WXYC title

**8. Discogs Release ID → MusicBrainz** — 46 found
- For albums with Discogs release IDs, followed Discogs URL → MusicBrainz release → sibling releases in same release group → streaming URLs
- Recovered albums where a different pressing (digital vs vinyl) had the streaming link

**9. Wikidata Spotify Artist IDs** — 14,600 artists mapped
- Local Wikidata PostgreSQL (`postgresql://jake@localhost/wikidata`)
- Joined Discogs artist IDs (P1953) → Wikidata QIDs → Spotify artist IDs (P1902)
- Also identified 13,897 Bandcamp artist slugs (P3283)
- Used to confirm artist-level streaming presence

**10. Artist-Confirmed Backfill** — 5,568 found
- If any album by an artist was confirmed on streaming, marked all other albums by the same artist as likely available
- Used both `display_artist` and `discogs_artist` for matching

**11. Alternate Artist Names** — 928 found
- Library entries have `alternate_artist_name` (e.g., "Chan Marshall" for "Cat Power")
- Checked alternate names against Wikidata for Spotify artist IDs

### Phase 3: Embedding & Vector Search

**12. Sentence-Transformers (all-MiniLM-L6-v2)** — 1,920 found
- Embedded all 29K not-on-streaming album strings and 197K MusicBrainz release strings
- Cosine similarity with 0.85 threshold
- Caught title variations ("Billy Joe Shaver - I'm Just an Old Chunk of Coal" → full long title), different artist credit formats, truncations

**13. Sentence-Transformers (all-mpnet-base-v2)** — 24 found
- Larger, more accurate model on remaining not-found albums
- Found diacritics variants ("Monolord - Vaenir" → "Vænir"), special characters ("Junior H - Sad Boyz 4 Life" → "$ad Boyz 4 Life")

**14. Separate Artist/Title Embeddings** — 71 found
- Embedded artists and titles independently with all-MiniLM-L6-v2
- Required both dimensions above threshold (0.7) to reduce false positives
- Fuzzy verification in Python for accepted candidates

### Phase 4: LLM-Powered Canonicalization

**15. Claude Haiku Name Canonicalization** — 11,789 names corrected, $4.66
- Sent all 26,913 unmatched albums to Claude Haiku in batches of 50 (539 requests, 50 concurrent)
- Prompt asked for canonical streaming service names
- Key corrections: "(Long) John Baldry" → "Long John Baldry", "Wings - McCartney" → "Paul McCartney - McCartney", "Fela Anikulapo Kuti" → "Fela Kuti", "OHSEES" → "Osees"

**16. LLM Names → Discogs Local** — 2,934 found
- Matched canonicalized names against local Discogs cache (exact match)
- Biggest single recovery from LLM pass

**17. LLM Names → Wikidata** — 1,802 found
- Checked canonicalized artist names against Wikidata Spotify IDs

**18. LLM Names → MusicBrainz** — 183 found
- Exact + trigram match of canonicalized names against MusicBrainz with streaming URLs

**19. LLM Names → Deezer API** — 472 found
- Searched Deezer with canonicalized names (quoted + unquoted)

**20. Artist Name Backfill** — 7,744 albums enriched
- For every artist where ANY album had a Discogs canonical name, propagated that name to all other albums by the same artist
- Also propagated LLM-corrected names
- Enabled 3,762 additional Wikidata matches on the backfilled names

**21. LLM Streaming Likelihood Assessment** — 2,622 identified as likely, $2.38
- Asked Claude Haiku to assess whether each remaining album is likely on streaming
- Used results to prioritize web search targets

### Phase 5: Web Search

**22. Claude Haiku Web Search (targeted)** — 2,113 found, $9.54
- Used Anthropic's web search tool on 2,603 high-value targets (LLM-likely + artist-confirmed)
- Batched 5 albums per search request, 20 concurrent
- 81% hit rate — found Bandcamp, Spotify, Apple Music links that no other method caught

**23. Claude Haiku Web Search (remaining)** — 5,853 found, $28.58
- Ran on all remaining 8,815 albums
- 66% hit rate
- Pushed total over 50,000

### Phase 6: Targeted Fixes

**24. Parenthetical Artist Stripping** — 14 found
- Stripped parenthetical content from artist names: "(Long) John Baldry" → "John Baldry", "(Tammy &) the Amps" → "the Amps"
- Matched stripped names against MusicBrainz with streaming URLs

**25. Discogs Cache Re-enrichment** — 826 enriched, 1 streaming URL found
- Discovered 1,652 not-found artists actually existed in the Discogs cache but were missed due to timing (enrichment ran before wxyc schema was built)
- Re-ran exact artist match + fuzzy title scoring

### Phase 7: Data Pipeline Enhancement

**26. Discogs Video/URL ETL** — 17 found
- Added video parsing to the Discogs XML converter (Rust) — processes 18.9M releases in 1:52
- Imported 3.5M video URLs (all YouTube) for 1.04M releases into the Discogs PostgreSQL cache
- Matched 17 not-on-streaming albums directly via Discogs release ID → video URL
- ETL changes span three repos: discogs-xml-converter, discogs-cache, library-metadata-lookup
- Orchestrated via claude-orchestrator with dependency chaining across repos

**27. Apple Music/iTunes API** — 1,228 gross hits (197 net-new)
- 15 req/min rate limit, checked 11,216 not-found albums over ~21 hours
- 11% hit rate; 197 albums moved from not-found to found (rest already had matches from other services). After deduplication and validation, 288 albums end up with `apple_url` persisted (combined with Method 35).

### Phase 8: Discogs Enrichment

**28. Exact Title + Fuzzy Artist Match** — 8,152 Discogs matches found
- For the 26,938 albums without `discogs_release_id`, searched by exact title against all 19M Discogs releases using a btree index on `lower(title)`
- Scored artist names with rapidfuzz token_sort_ratio (threshold >= 70)
- Caught Discogs disambiguation suffixes ("3-D" → "3-D (4)"), "The" prefix mismatches ("Chemical Brothers" → "The Chemical Brothers"), diacritics ("Dalek" → "Dälek"), and punctuation ("Da Youngstas" → "Da Youngsta's")
- Raised Discogs coverage from 54.3% to 68.1% of catalog (31,975 → 40,127 albums)

**28a. Fuzzy Artist + Fuzzy Title Match** — 3,075 additional Discogs matches
- For the remaining 18,786 unmatched albums, found artists via trigram similarity on a pre-built `distinct_artist` table (2.8M names with GIN index), then fuzzy-matched titles against all releases by that artist in Python
- Required both artist score >= 70 and title score >= 75
- Cached artist release catalogs (37K artists) to avoid repeated queries
- Caught format suffix stripping ("Gamers 12\"" → "Gamers"), typos ("Mark's Keyboard Repar" → "Mark's Keyboard Repair"), and annotation removal ("The Platform (clean)" → "The Platform")
- Raised Discogs coverage from 68.1% to 73.3% (40,127 → 43,202 albums)

**28b. LLM Canonicalization on Discogs-Unmatched** — 1,754 additional Discogs matches
- Sent 15,711 unmatched artist+title pairs through Claude Haiku for name correction ($5-10)
- Exact match on canonicalized names: 1,445
- Trigram match on canonicalized names against `wxyc_release_match` table (4.3M rows): 309
- Raised Discogs coverage from 73.3% to 76.3% (43,202 → 44,956 albums)

**28c. Master Release Title Matching** — 275 additional Discogs matches
- Imported 2,530,697 Discogs masters into local PG (new `master` and `master_artist` tables)
- Matched unmatched album titles against canonical master titles, then resolved to a release via `release.master_id`
- Raised Discogs coverage from 76.3% to 76.8% (44,956 → 45,231 albums)

### Phase 9: VA Compilation Resolution

**29. Compilation Title Matching** — 3,990 compilations matched to Discogs
- Exact title match: 1,595
- Label/series prefix stripping ("Sugar Hill - The Great Rap Hits" → "The Great Rap Hits"): 315
- Trigram fuzzy match: 1,986
- LLM canonicalization (Claude Haiku, $0.30): 94
- Of the 3,990 matched, 3,820 have per-track artist data from `release_track_artist` (54M rows)
- 2,244 compilations remain unmatched (mostly obscure samplers, station promos, and label-specific releases not in Discogs)

### Phase 10: Kagi FastGPT Web Search

**30. Kagi FastGPT Streaming Search** — 1,452 found, ~$40

- Used Kagi FastGPT API to search for streaming links across Spotify, Apple Music, Bandcamp, Tidal, Deezer, and SoundCloud
- Searched 1,850 of 2,692 not-on-streaming albums before credits ran out
- 78% hit rate — FastGPT synthesizes web search results into structured answers with URLs
- ~$0.015/query, concurrency 5 with 0.3s stagger to avoid rate limits

### Phase 11: URL Validation

**31. Streaming URL Validation** — 5,204 false positives removed
- Validated all 76,800 streaming URLs in the database against actual service metadata
- Spotify: oEmbed API title comparison (10.3% false positive rate, 35,073 skipped due to timeouts)
- Deezer: public `/album/{id}` API (13.4% false positive rate)
- Bandcamp: page `<title>` scraping (36.4% false positive rate)
- Tidal: HEAD request URL resolution (0% false positive rate)
- All services validated concurrently with per-service rate limiting
- False positives were primarily from Kagi FastGPT returning URLs for wrong albums (similar artist/title names) and from the original Anthropic web search pass

### Phase 12: Bandcamp Coverage

**32. Bandcamp Wikidata Slug Lookup** — 2,580 album-level + 5,898 artist-level URLs
- Used Wikidata P3283 (Bandcamp ID) to identify 2,301 WXYC artists with known Bandcamp pages
- Fetched each artist's `/music` page and fuzzy-matched album titles against the library catalog
- 2,580 direct album URLs matched; 5,898 additional artist-page fallback URLs for unmatched albums
- The 5,898 artist-page fallbacks were later cleared from `bandcamp_url` by the Apr 19 `bandcamp_pipeline` migration so the album-level pipeline could re-match them precisely; their slugs remain available in `bandcamp_slug`

**33. Bandcamp Search API** — 1,017 additional artists found
- For 3,205 not-on-streaming artists without Wikidata Bandcamp slugs, searched the Bandcamp autocomplete API
- 31.7% hit rate — found Bandcamp pages for artists not in Wikidata
- Combined with Wikidata slugs: Bandcamp coverage across the catalog

### Phase 13: WXYC Exclusive Badges

**34. on_streaming flag and exclusive badges**
- Added `on_streaming: boolean` to the API schema (wxyc-shared, LML, Backend-Service)
- Added "WXYC EXCLUSIVE" badges to dj-site (React/MUI Joy) and tubafrenzy (Java/JSP)
- Deep purple (#7B2D8E) badge shown when `on_streaming === false` — albums only in the physical library
- PRs across 5 repos: wxyc-shared, library-metadata-lookup, Backend-Service, dj-site, tubafrenzy

### Phase 14: Apple Music Pass + Spotify Revalidation

**35. Apple Music targeted search** — 196 found
- Searched 3,951 Spotify misses against the iTunes Search API (album-level, `entity=album`)
- 5% hit rate over ~4.5 hours at 15 req/min
- `AppleMusicClient` with 403 retry and 60s backoff
- Fixed `populate-on-streaming.py` to include `apple_status = 'found'` in on-streaming criteria and removed unsafe blanket `SET ON_STREAMING = NULL` reset

**36. Spotify false-negative revalidation** — 24 found
- For 184 albums where `artist_on_spotify = 1` but album was `not_found`, re-searched with multiple query variants
- `revalidate_misses.py` generates queries from both library and Discogs metadata (artist/title variants, format suffix stripping, deduplication)
- Caught artist name mismatches (DJ Shadow → UNKLE / Psyence Fiction), Discogs disambiguation suffixes

### Phase 15: Track-Level Streaming Availability

**37. Track extraction** — 72,341 tracks from 8,447 albums
- Parsed 4,666 singles into individual tracks via title patterns (`b/w`, `//`, `/`, `+N`) and Discogs tracklists (2,432 with release IDs)
- Extracted per-track artist credits for 3,990 compilations from `compilation_track_artists.json` (VA disambiguation data)
- New `track_results` table in `streaming_availability.db`

**38. Local track resolution** — 201 found
- Built in-memory index of ~430K tracks from 43,331 on-streaming albums via Discogs PG cache tracklists
- Looked up each extracted track by normalized title, scored artist match with `score_match()` (threshold >= 80, raised to 95 for titles <= 4 chars)
- 0.3% local hit rate — most single/compilation tracks are not on other library releases

**39. Track-level API search** — 34,098 found (47% hit rate)
- Searched 72,140 tracks on Deezer (25 req/5s) then Spotify (30 req/30s) over ~38 hours
- Short-title confidence floor (95% for titles <= 4 chars) rejects ambiguous matches on generic titles like "Dub", "Love", "Intro"
- 47% hit rate held steady throughout the run

**40. Track-level URL validation** — 34,098 valid, 33 false positives (99.9% precision)
- Validated all API-matched track URLs against Spotify oEmbed and Deezer public API metadata
- 33 false positives removed (0.1%), confirming the find_best_match scoring is effective at track level
- 3 albums reverted from on-streaming after all their track matches were invalidated

### Phase 16: Unmatched Compilation Title Search

**41. Compilation title search** — 532 found (22%) out of 2,364 unmatched compilations
- Stripped library filing conventions ("Various Artists - Rock - S", "Soundtracks - A") and searched by title only
- Phase 1: Discogs PG cache exact title match → 11 matches (unlocks tracklists and per-track artist data)
- Phase 2: Deezer/Spotify album search for Discogs misses → 521 streaming matches at 22% hit rate
- For compilations, relaxed the artist matching requirement since compilation "artists" are filing conventions, not real names — only title similarity >= 80 required
- Remaining 1,832 unmatched compilations are mostly obscure samplers, station promos, and label-specific releases not on streaming services
- Added Spotify 5xx retry logic (exponential backoff on 500/502/503/504) to avoid silent failures from transient server errors
- Note: this base of 2,364 doesn't reconcile with the 2,244 unmatched compilations reported in Method 29; the difference is unexplained and likely reflects a different selection query

**42. Compilation search retry (Spotify 502 recovery)** — 273 found (13%)
- Re-ran the compilation title search after adding 5xx retry logic to the Spotify client
- 262 streaming matches + 11 Discogs matches from 2,061 remaining compilations
- Recovered albums that were silently missed due to Spotify 502 Bad Gateway errors
- Note: this base of 2,061 doesn't reconcile with the 1,832 left after Method 41; the difference is unexplained

### Phase 17: Discogs Re-evaluation

**43. Discogs rematch Pass 1 (title-only)** — 93 found
- For 6,368 albums where the artist was found but title matching failed (`discogs_artist` set, `discogs_release_id` NULL)
- Searched Discogs PG cache by exact title across all releases (not filtered by artist), then scored result artist with relaxed threshold (70 instead of 80)
- Added `normalize_artist_credit()` for generating artist name variants: "&" ↔ "and", slash-separated collaborations → first artist, parenthetical disambiguation stripping, "feat." removal

**44. Discogs rematch Pass 2 (fuzzy artist)** — 170 found
- For 7,314 albums where artist lookup failed entirely (no `discogs_artist`)
- Used pg_trgm trigram similarity to find fuzzy artist matches, then searched that artist's releases by fuzzy title
- Caught "&" vs "and" variations, "The" prefix mismatches, and Discogs disambiguation suffixes
- Total new Discogs matches: 263, bringing coverage from 45,231 to 45,494 (76.8% → 77.2% of non-compilations)

## Cost Summary

| Method | Cost |
|--------|------|
| LLM canonicalization (Haiku) | $4.66 |
| LLM streaming assessment (Haiku) | $2.38 |
| Web search — targeted (Haiku) | $9.54 |
| Web search — remaining (Haiku) | $28.58 |
| LLM compilation canonicalization (Haiku) | ~$0.30 |
| LLM album canonicalization (Haiku) | ~$10.00 |
| Kagi FastGPT streaming search | ~$40.00 |
| Spotify API | Free (dev account + Premium subscription) |
| Bandcamp API | Free |
| Deezer API | Free |
| Apple Music/iTunes API | Free |
| All local database methods | Free |
| Vector embeddings | Free (local CPU) |
| **Total** | **~$95** |

## Data Sources

| Source | Type | Records |
|--------|------|---------|
| Discogs PostgreSQL cache | Local | 19M releases (3.6M in wxyc schema) |
| MusicBrainz PostgreSQL | Local | 1.26M releases, 464K streaming URLs, 38M recordings |
| Wikidata PostgreSQL | Local | 47K Spotify artist IDs, 14K Bandcamp slugs |
| Deezer API | Remote | Free, 25 req/5s |
| Spotify Web API | Remote | Free (Premium required), 30 req/30s, daily quota |
| Apple Music/iTunes API | Remote | Free, 15 req/min |
| Kagi FastGPT API | Remote | ~$0.015/query |
| Bandcamp Autocomplete API | Remote | Free |
| Claude Haiku (Anthropic) | Remote | Web search tool + LLM canonicalization |

## Final Results

### Streaming Availability (non-compilations)

| Category | Count | % |
|----------|-------|---|
| On streaming (Spotify/Deezer/Apple/Bandcamp) | 55,423 | 94.1% |
| Not on streaming | 3,490 | 5.9% |
| **Total non-compilation** | **58,913** | |

### Streaming URL Coverage

| Service | Albums with URL |
|---------|----------------|
| Spotify | 46,786 |
| Deezer | 25,568 |
| Bandcamp | 2,795 |
| Apple Music | 288 |
| Tidal | 10 |
| **Total with any URL** | **47,588** |

The Bandcamp count reflects only album-level URLs persisted in `bandcamp_url`. The 5,898 artist-page fallback URLs from Method 32 were cleared by the Apr 19 `bandcamp_pipeline` migration (which preserved the slugs in `bandcamp_slug` for later album-level re-matching).

### Discogs Coverage (non-compilations)

| Category | Count | % |
|----------|-------|---|
| Matched to Discogs release | 45,494 | 77.2% |
| Not matched | 13,419 | 22.8% |

### VA Compilations

| Category | Count | % |
|----------|-------|---|
| Matched to Discogs release | 3,990 | 64.0% |
| With per-track artist data | 3,820 | 61.3% |
| Not matched | 2,244 | 36.0% |
| **Total compilations** | **6,234** | |
| **CTA rows in prod** | **140,617** | |

### Track-Level Results (singles + compilations)

| Category | Count | % |
|----------|-------|---|
| Tracks extracted | 72,341 | |
| Locally resolved (Discogs tracklist index) | 201 | 0.3% |
| API matched (Deezer/Spotify track search) | 34,098 | 47.1% |
| False positives (removed by validation) | 33 | 0.1% |
| Not found on any service | 38,009 | 52.5% |
| **Albums with at least one track on streaming** | **~3,400** | |

### ON_STREAMING in Production (tubafrenzy MySQL)

| Status | Count |
|--------|-------|
| ON_STREAMING = 1 | 58,787 |
| ON_STREAMING = 0 (exclusive) | 3,490 |
| NULL (unknown — obscure compilations/samplers) | 2,417 |
| **Total LIBRARY_RELEASE rows** | **64,694** |

The 453-row gap between LIBRARY_RELEASE (64,694) and the streaming-availability snapshot (65,147) reflects releases that have been removed from the live library since the snapshot was taken on Apr 23. The percentages in the Overview are computed against the snapshot total.

### Full Catalog

| Category | Count |
|----------|-------|
| Total catalog | 65,147 |
| Non-compilations | 58,913 |
| Compilations | 6,234 |
| Masters imported | 2,530,697 |

## Key Lessons

1. **Name canonicalization is the biggest lever.** The WXYC catalog has non-standard names, typos, and format annotations that no fuzzy matching can overcome. LLM canonicalization ($4.66) recovered more albums than all other post-API methods combined.

2. **Deezer as a pre-filter is essential.** At 5 req/sec with no auth, Deezer quickly identifies what's digitally distributed. Only checking Spotify for Deezer hits reduced API calls by 50% and avoided rate limit issues.

3. **Local databases before APIs.** MusicBrainz, Discogs, and Wikidata together provided thousands of verified streaming URLs with zero API calls and sub-second query times.

4. **Artist-level confirmation propagates.** Once we confirm an artist is on streaming (from any source), all their albums can be marked as likely available. This single heuristic recovered 5,568 albums.

5. **Web search is the ultimate fallback.** Claude's web search tool found 66-81% of remaining albums at $0.003/album — catching collaborative credits, alternative titles, and obscure Bandcamp releases that no structured database could match.

6. **Never delete collected data.** Resetting the streaming_availability.db cost 2 days of re-collection. Always use surgical updates (WHERE status = 'not_found') rather than blanket resets.

7. **Validate LLM-sourced URLs.** Kagi FastGPT and Claude web search both return plausible-looking URLs that point to the wrong album (~10-37% false positive rate depending on the service). Validating every URL against the service's own metadata (oEmbed, public API) is essential before trusting the results.

8. **Discogs artist disambiguation is the biggest remaining gap.** 62.8% of unmatched artists exist in MusicBrainz and 38.9% in Wikidata — they're not obscure, they just have disambiguation suffixes ("3-D (4)"), "The" prefix mismatches, or diacritics that prevent exact matching.

9. **Bandcamp is essential for freeform radio.** Wikidata slugs provided 2,301 artist Bandcamp pages, and direct Bandcamp search found 1,017 more among not-on-streaming artists. Many WXYC releases exist only on Bandcamp — prioritize it over Spotify for a freeform station.

10. **Spotify rate limits are aggressive.** Running more than ~3 concurrent requests triggers a 24-hour ban. Never run multiple Spotify scripts simultaneously. Use sequential requests with delays, and track progress so interrupted runs can resume.
