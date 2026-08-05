# Plan: LML#1069 — album-first Bandcamp discovery (label/imprint/VA-hosted releases)

Issue: https://github.com/WXYC/library-metadata-lookup/issues/1069 (sub-issue of #832, epic #830). Measurement pass posted 2026-08-01: https://github.com/WXYC/library-metadata-lookup/issues/1069#issuecomment-5153787240

## Context (established by the measurement pass — do not re-derive)

- Mechanism decision is settled by the environment: the `bandcamp.com/search` HTML page is behind a Fastly JS client challenge, so option (b) (search-page scraping) is dead. Option (a) — the autocomplete API with `param="a"` (album-type results) — works over plain HTTP, returns `band_name` for artist binding, and passed a 111-album stratified probe with 17/17 precision at the 80/80 floor and ~12–18% recall on raw queries.
- Projected yield ≈7,800 new direct Bandcamp URLs (current verified coverage: 2,810), one autocomplete request per album.
- Album-first strictly dominated artist-first in-sample (artist-first: 2% on the untouched tail, and its lone hit was a subset of album-first's), and it also heals artist-first false negatives (non-obvious slugs, custom domains).
- Known API quirks: the `url` field comes back doubled (`https://x.bandcamp.comhttps://x.bandcamp.com/album/...`); custom domains (`music.sufjan.com`) appear in results; library titles carry format junk (`12"`, `[missing]`, `(studio)`) that pollutes queries.
- Custom-domain URLs are safe to store: `release/bandcamp_url_parser.py::bandcamp_album_id_from_url` returns `None` for non-`*.bandcamp.com` hosts and the identity mint is skipped (no error path); the streaming-links emission chain treats `bandcamp_url` as opaque.
- The runtime probe's live resolve (`lookup/streaming_url_postprocess.py::_warm_streaming_url_cache`) is a fire-and-forget background task off the response path, bounded by `_effective_probe_timeout_s` + a process-global semaphore — adding a fallback request there does not touch the `/lookup` hot path.

## Deliverables

Two PRs, sequenced. PR 1 is the substance; PR 2 is a small, separately-verifiable adoption step.

### PR 1 — shared album-first discovery + offline drain phase

**1. `clients/bandcamp.py` — shared discovery method (the #1069 acceptance-criterion seam):**

- `fix_autocomplete_url(url: str) -> str` — de-double the autocomplete `url` field (keep the last `https://…` segment). Must pass through already-clean URLs unchanged, so it keeps working if Bandcamp fixes the quirk.
- `search_albums(query: str) -> list[dict]` — autocomplete call with `param="a"`; keep only `type == "a"` rows; return `{"artist": band_name, "title": name, "url": fix_autocomplete_url(url)}`. Reuses `_request_with_retry` (existing 1 req/s limiter, semaphore 2, 429 backoff).
- `find_album_match_via_search(artist: str, title: str) -> SourceMatch | None` — builds the query as `f"{artist} {clean_title_for_query(title)}"`, calls `search_albums`, and matches under the two-sided 80/80 floor (stricter than the artist-catalog path's title-only 70 because general search results are not artist-scoped). Matching routes through the shared `find_best_source_match` (`clients/streaming/matching.py:706`) so `record_match_telemetry(service="bandcamp", surface="album")` fires with parity to every other adapter (LML#592) — run it on raw fields first, then once more on `normalize_bc_title`-normalized fields, accepting the first pass that clears the floor. No bespoke floor loop.

**2. Normalization helpers (module-level in `clients/bandcamp.py`, pure functions):**

- `clean_title_for_query(title)` — strips library format suffixes for the *query string only* (scoring still sees the raw title): trailing `12"`/`7"`/`10"`, bracketed `[missing]`/`[…]` tags, trailing `(studio)`. Conservative, fixture-tested list; when in doubt, leave the token in.
- `normalize_bc_title(title)` — recall-recovery normalization applied to BOTH sides as a secondary acceptance test (accept if raw OR normalized clears the floor): strip trailing bracketed catalog tags (`[KLP186]`), trailing year-range parentheticals (`(1978-83)`), and collapse ` / ` ↔ `/` spacing. Each pattern must be justified by a named near-miss from the measurement report (Pine Hill Haints, Z'ev, The Dutchess & The Duke, Christoph De Babalon) and each must have a negative test proving it does NOT let the known true-negative shapes through (the Rainbow tribute, the Leo Sayer DJ edit).
- Scope guard: these are Bandcamp-side helpers only. `clients/streaming/matching.py::score_match` is shared with Spotify/Apple/Deezer and is NOT touched.

**3. `scripts/bandcamp_pipeline.py` — new `--phase album-search`:**

- Candidate selection via a new named `ResultsDB.get_pending_album_search()` selector (every album query in this module is a named, unit-tested method; `get_pending_bandcamp_lookup` requires a non-empty slug and cannot be reused): `bandcamp_url IS NULL AND is_compilation = 0 AND bandcamp_status = 'pending' AND (bandcamp_slug IS NULL OR bandcamp_slug = '')`. Rows with a *real* recorded slug stay owned by the existing Phase-2 catalog backlog — album-search must not mark them `not_found` and pre-empt a pending catalog scrape (the measurement's "marginal yield ≈0" claim was made on slug-less populations and does not cover them). The `''` sentinel rows ("artist searched, no band found") are prime album-first candidates and have no Phase-2 claim. `--include-not-found` extends the target to the 7,671 `not_found` rows (only ever attempted artist-first). `--limit N` for chunked runs.
- Per row: `find_album_match_via_search(display_artist, display_title)` — exactly one autocomplete request (plus optional verify, below).
- Writes are opt-in behind `--execute` (mirrors the #1062 drain-harness convention; the legacy phases keep their existing write-by-default `--dry-run` gate untouched). Without `--execute`: resolve, tally, and write a `--report-json` of would-be writes. Flag interaction is explicit: `--execute` is only valid with `--phase album-search`; passing `--execute` together with `--dry-run` is an argparse error; `--dry-run` with album-search is a no-op (it is already dry by default). The per-phase safety defaults are called out in the argparse help text so the asymmetry with legacy phases is visible to the operator.
- Write semantics (fill-only, reusing existing `ResultsDB` methods — **no schema change**):
  - hit → `update_bandcamp_url(id, url)` (sets `found` + `checked_at`; existing method).
  - miss on a `pending` row → `mark_bandcamp_not_found(id)` (album-first is now the primary mechanism per the measurement; a future artist-first sweep is not worth preserving `pending` for — in-sample its marginal yield over album-first was zero).
  - miss on a `not_found` row → no write (already durably recorded; keeps the run idempotent).
  - transient HTTP failure (`None` response after retries) → no write, tallied `fetch_failed`, row stays re-runnable — same posture as the #661 fix.
- `--verify-hits` (default ON when `--execute`): before writing, fetch the matched album page and require the `og:title` (`"Title, by Artist"`) to clear the same 80/80 floor against the library row; mismatch → skip write, tally `verify_failed`, log. Costs ~1 extra request per hit (~7.8k requests ≈ 2h over the whole drain) and is the insurance the "wrong direct link is worse than a search URL" house rule pays for.
- Resumability falls out of the status columns (`found`/`not_found` + `checked_at`), same as the existing phases. Progress logging every 100 rows; summary counts at exit (`hits / misses / verify_failed / fetch_failed / skipped`).

**4. Tests (TDD, red-green per unit; markers per `docs/testing.md`):**

- Unit: `fix_autocomplete_url` (doubled, clean, custom-domain, garbage); `clean_title_for_query` + `normalize_bc_title` parameterized over the measurement's named near-misses and true-negatives; `search_albums` response parsing against the captured real autocomplete responses in `tests/fixtures/bandcamp/` (see Appendix — includes the doubled URL and interleaved `t`-type rows); `find_album_match_via_search` floor logic (accept 100/100, reject 80-artist/66-title, accept via normalized side); `ResultsDB.get_pending_album_search` selection semantics (slug-NULL and slug-`''` in; real-slug and compilation rows out) in `tests/unit/test_results_db.py`; album-search phase against `ResultsDB(":memory:")` per the established convention (hit writes url+found, pending-miss writes not_found, real-slug rows never touched, not_found-miss no-op, `--execute` gating, verify-failure skips write).
- `external_api`-marked smoke: golden repro (George Theodorakis → `into-the-light.bandcamp.com/album/...`) live end-to-end, with the same runtime-skip-on-unanswerable guard as the live Discogs smoke (a Bandcamp 429/outage must skip, not redden the external lane).
- Local gate before push: `ruff check`, `ruff format --check`, `uv run --no-sync mypy . --ignore-missing-imports`, full unit suite.

**5. Docs:** update `docs/scripts.md`'s Bandcamp pipeline section with the new `--phase album-search` and its `--execute` / `--verify-hits` / `--include-not-found` / `--report-json` flags (CLAUDE.md routes readers there; docs land in the same PR).

### PR 2 — runtime probe adoption (LML#573 tier)

- `BandcampClient.find_album_match` (the `BaseStreamingClient` contract method the postprocess registry calls) falls back to `find_album_match_via_search(artist, title)` when the artist-first path yields nothing. One method, both tiers — satisfying the issue's "shared method" acceptance criterion.
- Cost analysis: worst case +1 rate-limited autocomplete request inside a background, semaphore-bounded, timeout-bounded warm task. No `/lookup` hot-path change (the #651 constraint). Persistence stays behind the existing `lml_persist_streaming_url_bandcamp` flag — no new flag.
- Verify on staging before promoting: trigger a lookup for the golden repro artist/album, confirm the background probe caches the `into-the-light` URL in `lml_cache.album_streaming_url_cache`, and watch the probe-timeout counters for regression.
- Kept as its own PR so the drain (pure offline win) is not coupled to any runtime-behavior review.

## Operator runbook (after PR 1 merges — operator actions, not the agent's)

1. Download the canonical DB (`GET /admin/download-streaming-db`) to a working copy.
2. Dry-run `--phase album-search --limit 500 --report-json …`; expect ~12–18% hit rate; spot-check the report.
3. `--execute` in chunks off-peak (full sweep ≈62k autocomplete requests ≈ 17.5h at 1 req/s + ~2h verify overhead — plan 3–4 overnight chunks via `--limit`; one bulk LML consumer at a time, do not overlap the YTM drain).
4. Republish via `POST /admin/upload-streaming-db`; post before/after verified-Bandcamp counts on #1069 and epic #830.

## Non-goals / explicitly out of scope

- VA/compilation discovery (`band_name` binding can't work for `Various Artists – …`; 0/6 in sample) — file a follow-up ticket, title-first design sketch in the measurement comment.
- Mechanism (b) / headless-browser scraping.
- Any `/lookup` hot-path change.
- Prod drain execution (operator-gated).
- Re-scoping #832 (drop a comment there pointing at the measurement's "album-first should be primary" finding; its remaining population is this drain's target set anyway).

## Risks

- **Unofficial API drift**: the autocomplete endpoint has no contract; the doubled-URL quirk could be fixed or the shape could change. Parser is written to handle both forms; the `external_api` smoke test catches drift in CI's external lane.
- **Precision on a general index**: mitigated three ways — two-sided 80/80 floor, conservative normalization justified case-by-case, and `--verify-hits` og:title confirmation before any write.
- **Politeness**: existing client limiter (1 req/s, semaphore 2, 429 backoff with Retry-After) is reused unchanged; the drain adds no new concurrency.
- **not_found semantics**: marking pending misses `not_found` forecloses a future artist-first pass over them. Accepted deliberately (measured marginal yield ≈0); `bandcamp_checked_at` timestamps make the cohort identifiable if we ever want to revisit.

## Sequencing & sizing

1. Worktree off `origin/main` (repo default tree sits on `prod`).
2. PR 1 (~500–700 lines with tests) → `/code-review` loop → rebase-merge.
3. PR 2 (small) → staging verification → `/code-review` → rebase-merge.
4. File the VA follow-up ticket + the #832 re-scope comment.
5. Hand the runbook to the operator.

---

## Appendix: implementer bootstrap (self-contained cold-start kit)

### Environment

```bash
git -C /Users/jake/Developer/WXYC/library-metadata-lookup fetch origin
git -C /Users/jake/Developer/WXYC/library-metadata-lookup worktree add \
    ../library-metadata-lookup-worktrees/1069-bandcamp-album-first -b feat/1069-bandcamp-album-first origin/main
cd ../library-metadata-lookup-worktrees/1069-bandcamp-album-first
uv sync --extra dev            # REQUIRED in a fresh worktree
uv run --no-sync pytest        # ALWAYS --no-sync; bare `uv run pytest` grabs homebrew pytest
uv run --no-sync mypy . --ignore-missing-imports   # CI has a required mypy job pre-commit doesn't cover
```

### Durable artifacts (captured 2026-08-01)

Originally written to an untracked `plans/lml-1069-artifacts/` directory. That directory no longer exists; each file's current home is given below.

- `tests/fixtures/bandcamp/autocomplete_a_golden_repro.json` (tracked) — REAL `param="a"` response for the golden-repro artist+title query: 17 results, `a`-type at rank 1. Use this as the parsing-test fixture rather than hand-writing JSON; `tests/unit/test_bandcamp_album_search.py` already reads it from there.
- `scripts/measure_1069.py` (tracked) — the measurement script (has a working `og:title` extraction regex and the URL-de-doubling logic to port).
- `autocomplete_b_george_theodorakis.json` — REAL `param="b"` response for `q=George Theodorakis`: 33 results (1 `b`-type at index 0, 1 `a`-type at index 1 — the label-hosted golden-repro album the current type filter drops, 31 `t`-type). Shows the doubled-`url` quirk verbatim. Run output, not tracked; kept in a local `artifacts/` holding directory. Re-capturable with the command below.
- `measure_1069_report.json` — full per-row measurement output (111 rows: sampled ids, per-row probe results, scores, verify og:title). Source of truth for every number in the issue comment. Run output, not tracked; kept in a local `artifacts/` holding directory. Regenerable by re-running `scripts/measure_1069.py`.
- Re-capture command if fixtures need refreshing: `curl -sf -A "Mozilla/5.0" --get --data-urlencode "q=<query>" --data-urlencode "param=a" "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"`.

### Golden repro (first failing test)

Library row `id=56319`: `display_artist="George Theodorakis"`, `display_title="The Rules of the Game: Original Studio Recordings (1978-1996)"`. Expected match: `https://into-the-light.bandcamp.com/album/the-rules-of-the-game-original-studio-recordings-1978-1996` (a reissue-label page; the artist's own page is `georgerakis.bandcamp.com` and does NOT carry the album). Scores vs the `a`-type result: artist 100 / title 100.

### Verified floor semantics (do not re-derive)

`find_best_source_match` → `find_best_match` scores candidates with `score_match` and accepts via `is_acceptable_match` (`matching.py:305`) — **both scores ≥ 80**, exactly the floor the measurement used, so measured precision/recall transfers. `find_best_source_match` additionally emits the required LML#592 telemetry (`service=` is a required kwarg — forgetting it is a `TypeError` by design). Check at build time whether `find_best_match` also applies the LML#719 `title_subset_is_degenerate` guard; if it does, that's a free extra protection, not a conflict.

### Near-miss corpus (exact strings; parameterize the normalization tests from this table)

| library artist | library title | matched artist | matched title | a | t | verdict → test expectation |
|---|---|---|---|---|---|---|
| Pine Hill Haints | Ghost Dance | The Pine Hill Haints | Ghost Dance [KLP186] | 100 | 71.0 | TRUE — bracketed catalog tag strip must recover |
| Z'ev | as/if/when | Z'ev | As / If / When (1978-83) | 100 | 47.1 | TRUE — slash spacing + year-range strip must recover |
| The Dutchess & The Duke | Sunset/Sunrise | The Dutchess & The Duke | Sunset / Sunrise | 100 | 66.7 | TRUE — slash spacing must recover |
| Christoph De Babalon | Hilf dir Selbsts | Christoph de Babalon | 044 (Hilf Dir Selbst!) | 100 | 57.9 | TRUE — leading catalog number; recover only if a conservative pattern exists, else document as accepted loss |
| Kristoff K. Roll | A l'ombre des ondes | Kristoff K.Roll | À l’ombre des ondes | 71.0 | 94.7 | TRUE — artist-side punctuation spacing; optional stretch, artist normalization is otherwise out of scope |
| Alice Cohen | Artificial Fairytales | Alice Cohen & The Channel 14 Weather Team | Artificial Fairytales | 42.3 | 100 | TRUE but OUT OF SCOPE — credit variant; the artist floor keeps it out deliberately |
| Battles | B EP | Battles | EP C/B EP | 100 | 28.6 | TRUE (combined reissue) but OUT OF SCOPE — do not chase |
| Sole | and the Skyrider Band | sole & the skyrider band | Sole & The Skyrider Band S/T | 28.6 | 73.5 | TRUE but OUT OF SCOPE — library artist/title split artifact |
| Rainbow | Richie Blackmore's Rainbow | Various Artists | Ride The Rainbow - The Ultimate Tribute to Ritchie Blackmore’s Rainbow | 36.4 | 52.1 | FALSE — tribute; MUST stay rejected after every normalization |
| Leo Sayer | Thunder in My Heart | Dj Sonixx | Meck, Leo Sayer - Thunder in My Heart Again (2 VERSIONS) | 22.2 | 50.7 | FALSE — DJ edit; MUST stay rejected after every normalization |
| U-Roy | Now | Ory J | U don't Know EP | 40.0 | 40.0 | FALSE — noise; MUST stay rejected |
| The Mercury Program | The Mercury Program | The Program Initiative | Mercury [Phase 1] | 63.4 | 43.8 | FALSE — wrong artist; MUST stay rejected (catalog-tag strip must NOT flip it) |

Every `normalize_bc_title` pattern must cite its TRUE row(s) and prove the FALSE rows still reject in the same parameterized test.
