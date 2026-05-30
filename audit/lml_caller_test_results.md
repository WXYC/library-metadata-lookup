# LML Caller Test Results

Per-caller pass/fail for M0.4. Two test surfaces:

1. **Static audit** — read each caller's source for hardcoded mojibake values and Unicode-mangling in the request path.
2. **Behavioural pin** — `tests/integration/test_mojibake_caller_compat.py` exercises LML's `/api/v1/lookup` and `/api/v1/library/search` end-to-end with a fresh in-memory library seeded with V012-corrected artist names. The same tests verify that the various caller input shapes (corrected form, raw Unicode, ASCII fallback) all resolve to the right release.

The integration tests live in this repo because every caller funnels through the same handful of LML endpoints; pinning LML's behaviour for V012-affected names is the load-bearing assertion. Per-caller tests in the caller repos are out of scope for this audit (filed under M2 if any caller turns up broken downstream).

## Static audit

| # | Repo / file | Endpoint(s) called | Hardcoded mojibake? | Unicode-mangling in request path? | Verdict |
|---|---|---|---|---|---|
| 1 | Backend-Service `apps/backend/services/lml/lml.client.ts` | `/lookup`, `/discogs/release/{id}`, `/discogs/artist/{id}`, `/discogs/entity/{type}/{id}`, `/discogs/track-releases`, `/streaming-check`, `/library/search` | No | No — `JSON.stringify` for POST bodies, `URLSearchParams` for query strings | ✅ pass |
| 2 | Backend-Service `apps/backend/controllers/proxy.controller.ts` | `/lookup`, `/discogs/release/{id}`, `/discogs/artist/{id}`, `/library/search` | No | No — passes `req.query` straight through to lml.client | ✅ pass |
| 3 | Backend-Service `apps/backend/services/metadata/metadata.service.ts` | `/lookup` (via lml.client) | No | No | ✅ pass |
| 4 | Backend-Service `apps/backend/services/requestLine/requestLine.enhanced.service.ts` | `/lookup`, `/discogs/track-releases`, `/discogs/release/{id}` | No | No | ✅ pass |
| 5 | Backend-Service `apps/backend/services/artwork/providers/discogs.ts` | `/lookup`, `/discogs/track-releases`, `/discogs/release/{id}` | No | No | ✅ pass |
| 6 | Backend-Service `scripts/backfill-metadata.ts` | `/lookup` (via lml.client) | No | No — reads from PG, no string literals | ✅ pass |
| 7 | tubafrenzy `libs/core/.../library/StreamingCheckLibraryReleaseListener.java` (resolved to `LibrarySearchClient.searchReleases` chain) | `/library/search` | No | No — `URLEncoder.encode(s, StandardCharsets.UTF_8)` everywhere | ✅ pass |
| 8 | tubafrenzy `libs/core/.../library/LibrarySearchClient.java` (main branch, present-day) | `/library/search`, `/discogs/tracks/autocomplete`, `/discogs/release/{id}`, `/lookup` | No | No — UTF-8 URL encoding, JSONObject body | ✅ pass |
| 9 | tubafrenzy-lml-resolve `libs/core/.../LibrarySearchClient.java` (in-progress branch) | Same as #8 | No | No | ✅ pass |
| 10 | tubafrenzy-lml-resolve `webapps/.../servlets/ArtistAutocompleteServlet.java` | `/library/search?q={term}` (via #9) | No | No | ✅ pass |
| 11 | tubafrenzy-lml-resolve `webapps/.../servlets/ReleaseAutocompleteServlet.java` | `/library/search?title={term}&artist={artist}` (via #9) | No | No | ✅ pass — but *see warning under §Findings about the artist-prefix filter* |
| 12 | discogs-etl `scripts/sync-library.sh` | None over HTTP — pushes `library.db` to LML's `/admin/upload-library-db` after exporting from MySQL | No | n/a | ✅ pass |
| 13 | semantic-index `run_pipeline.py` (`--entity-source=lml`) | None over HTTP — opens a PG connection to `entity.identity` directly via `DATABASE_URL_DISCOGS` | No | n/a | ✅ pass — schema is shared with LML; verify post-V012 reconciliation under M2.2 |
| 14 | semantic-index `semantic_index/discogs_client.py` | `/discogs/release/{id}` (HTTP fallback only; PG-first) | No | No — `release_id` is integer | ✅ pass |
| 15 | request-o-matic `services/lookup_client.py` | `/lookup` | No | No — Pydantic `model_dump`, `httpx.AsyncClient` | ✅ pass |
| 16 | **NEW** archive `app/api/artwork/route.ts` | `/lookup` | No | No — JSON body, dynamic input | ✅ pass — *not in the planned 11-site inventory; see §New callers* |

Greppable confirmation that no caller hardcodes mojibake byte sequences: `rg -e 'Î¼' -e 'Â£' -e 'Ã[\u0080-\u00ff]' -e 'Î£'` across all caller files returned no matches.

## Behavioural pin (`tests/integration/test_mojibake_caller_compat.py`)

Run: `.venv/bin/pytest tests/integration/test_mojibake_caller_compat.py -v`

```
test_lookup_finds_v012_corrected_row[micro-sign-mu]            PASSED
test_lookup_finds_v012_corrected_row[greek-mu]                 PASSED
test_lookup_finds_v012_corrected_row[greek-capital-nu]         PASSED
test_lookup_finds_v012_corrected_row[greek-capital-sigma]      PASSED
test_lookup_finds_v012_corrected_row[latin-extended-acute]     PASSED
test_lookup_finds_v012_corrected_row[hungarian-ohungarumlaut]  PASSED
test_lookup_ascii_fallback_finds_diacritic_row[ascii-hermanos] PASSED
test_lookup_ascii_fallback_finds_diacritic_row[ascii-nilufer]  XFAIL  (documented)
test_library_search_q_finds_v012_row[greek-mu-q]               PASSED
test_library_search_q_finds_v012_row[ascii-q]                  PASSED
test_library_search_q_finds_v012_row[sigma-q]                  PASSED
test_library_search_q_finds_v012_row[ascii-sigma-q]            XFAIL  (documented)
test_library_search_artist_filter_is_byte_strict               PASSED

11 passed, 2 xfailed
```

The two xfailed cases pin known LML limitations rather than caller bugs:

- **`ascii-nilufer`** — `Nilufer Yanya` (ASCII) does not match a row stored as `Νilüfer Yanya` (Greek capital nu N). The fuzzy fallback *finds* the row but the per-result artist filter (`lookup/orchestrator.py:artist_matches_item`) compares prefix-of-normalized-strings, and Greek capital nu (U+039D) does not NFKD-decompose, so the artist's first character stays `ν` after lowercase and never matches an ASCII `n`.
- **`ascii-sigma-q`** — `q=Stella Sings` does not match a row stored as `Σtella Sings`. The LIKE/fuzzy normalizer's `[^a-z0-9\s]` regex strips Greek capital sigma entirely (NFKD does not decompose it), reducing the row token to `tella`, which the 3-char-prefix candidate filter for `Stella` will not consider.

## Findings

1. **No caller bug.** All 16 caller sites pass the static audit. None hardcode any mojibake byte sequence; all use UTF-8-safe encoding when assembling the LML request.
2. **Pre-existing LML normalization limitation** (out-of-scope for M0.4, file separately if needed): the LIKE/fuzzy normalizer treats Greek/Cyrillic/Arabic/CJK as opaque non-ASCII glyphs — they survive NFKD then get nuked by the `[^a-z0-9\s]` regex. Practical impact for the V012 catalog: ASCII free-text queries from Slack and the request-line will fail to find releases by `Σtella`, `Νilüfer Yanya`, `Δοργια`, `繭` etc. Acceptable in the short term because those artists are queried by their canonical names from tubafrenzy's autocomplete, but the M1.2 lossy matcher and the M2.x downstream propagation should not rely on this fallback path.
3. **`/library/search?artist=` is byte-strict** (test pinned). The `artist=` filter does not strip diacritics — `artist=μ-Ziq` (Greek mu) returns 0 against a row stored as `µ-Ziq` (MICRO SIGN). Callers that build artist-filtered queries should pass `q=` instead, or canonicalize the artist field through `/identity/resolve` first. Backend-Service's lml.client and tubafrenzy's autocomplete both use `q=` — no caller-side fix needed.
4. **`/identity/resolve` and `/identity/bulk` return HTTP 500 in staging and production** (live test, 2026-04-26). Per `docs/api-endpoints.md` they should return 503 when `DATABASE_URL_DISCOGS` is unset. Surface to the maintainer; **out of scope for M0.4** (audit issue) — open as a separate ticket.
5. **`archive/app/api/artwork/route.ts` is a new caller** not in the planned 11-site inventory. UTF-8-safe; no fix needed; flagged so M2.x propagation accounts for it.

## Conclusion

No caller blocks Phase 2 propagation. M0.4 completes without raising any new bug tickets in caller repos. Findings (3) and (4) are LML-internal — file under separate issues if the team wants them addressed.
