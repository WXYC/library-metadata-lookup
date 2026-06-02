# LML #478 — Fuzzy-score Discogs artwork candidates before taking `results[0]`

**Issue:** [WXYC/library-metadata-lookup#478](https://github.com/WXYC/library-metadata-lookup/issues/478)

**Sibling concerns:** #440 (Spotify search-URL not verified), #442 (Apple-side album-first lookup — deeper case where the right release isn't in top-N). #478 is the simplest of the three: score the results we already have.

## Problem

`fetch_artwork_for_items` → `fetch_one` in [`lookup/orchestrator.py:1607-1638`](https://github.com/WXYC/library-metadata-lookup/blob/main/lookup/orchestrator.py#L1607-L1638) calls `discogs_service.search(...)` and unconditionally takes `response.results[0]`. There is no fuzzy-match floor, no ranking by the supplied artist/album. Discogs's search ranking optimizes for popularity/recency, not textual proximity — so when an item appears on multiple releases (compilations, reissues, live albums, multi-release artists), the top result can be the wrong release and the field returns wrong artwork rather than `None`.

**Concrete repro:** Noura Mint Seymali's "Hebebeb (Zrag)" appears on both *Tzenni* (2014) and *Yenbett* (2025). The DJ supplies the correct album title; LML returns the wrong release's artwork.

The matching primitive that would fix this already exists. [`clients/streaming/matching.py:find_best_match`](https://github.com/WXYC/library-metadata-lookup/blob/main/clients/streaming/matching.py#L145-L195) scores candidates with `score_match` on artist + title, applies `is_acceptable_match`'s 80/80 floor, and returns the highest combined-score match. Every streaming client (Spotify, Apple, Deezer, Bandcamp) uses it. The Discogs artwork path skipped it.

## Desired end state

In `fetch_one`, after `discogs_service.search` returns, score candidates against the same `(artist, album)` strings the search used and pick the highest combined-score result that clears 80/80. When nothing clears, return `None` — downstream already tolerates a `None` artwork result (this is `enrich_artwork_results`'s top-1-`None` synthesis path post-LML#401).

## The change

### Where: `lookup/orchestrator.py`, `fetch_one` inside `fetch_artwork_for_items`

Current shape (lines 1607–1638):

```python
async def fetch_one(item: LibraryItem) -> DiscogsSearchResult | None:
    try:
        album = discogs_titles.get(item.id, item.title)
        if is_self_titled(album or ""):
            album = item.artist
        artist = item.alternate_artist_name or item.artist or ""
        if is_compilation_artist(artist):
            artist = "Various"

        response = await discogs_service.search(
            DiscogsSearchRequest(album=album, artist=artist, ...)
        )
        if response.results:
            result = response.results[0]            # ← unconditional pick
            if not result.artwork_url:
                fallback = await _resolve_fallback_artwork(...)
                ...
            return result
        return None
```

New shape — replace the `result = response.results[0]` line with a scored pick against the **post-mutation** `artist` / `album` locals:

```python
        if response.results:
            result = _pick_best_artwork_match(
                response.results,
                query_artist=artist,
                query_album=album or "",
            )
            if result is None:
                return None
            if not result.artwork_url:
                fallback = await _resolve_fallback_artwork(...)
                ...
            return result
        return None
```

### The helper

`DiscogsSearchResult` already has `album: str | None` and `artist: str | None` (see `discogs/models.py:186-194`), so the scoring primitives drop in cleanly. `find_best_match` in `matching.py` returns a flat `dict` shaped for streaming URLs, not the original typed object — wrong shape for this caller. Two viable factorings:

**Option A — inline loop in orchestrator.** ~10 lines using `score_match` + `is_acceptable_match` from `clients.streaming.matching`. Minimum surface area. Fine if no other caller emerges.

**Option B — typed sibling in `matching.py`.** Add `find_best_typed_match(results, query_artist, query_title, artist_fn, title_fn) -> T | None` that returns the highest-scoring **original** item (not a dict). Generic over `T`. Drops in here and is reusable if a similar "rank typed candidates by artist/title" need arises elsewhere (e.g., Apple album-first work on #442 may want it).

**Recommendation: Option B**, as a small generic helper next to `find_best_match`. Two reasons: (1) `matching.py` is already the org's canonical home for "score artist + title pairs," (2) #442 is queued and will likely want the same primitive against typed Apple results. Cost is ~15 lines + one test for the helper. If reviewers prefer minimality, fall back to Option A — the orchestrator change is identical, only the helper location moves.

Helper signature (Option B):

```python
# clients/streaming/matching.py
T = TypeVar("T")

def find_best_typed_match(
    results: Iterable[T],
    *,
    query_artist: str,
    query_title: str,
    artist_fn: Callable[[T], str | None],
    title_fn: Callable[[T], str | None],
) -> T | None:
    """Return the highest-scoring result that clears the 80/80 floor, or None.

    Unlike find_best_match, returns the original typed object instead of a
    flattened dict. None-valued artist/title from a result score as 0 against
    any non-empty query.
    """
    best: T | None = None
    best_score = 0.0
    for item in results:
        r_artist = artist_fn(item) or ""
        r_title = title_fn(item) or ""
        a_score = score_match(query_artist, r_artist)
        t_score = score_match(query_title, r_title)
        if not is_acceptable_match(a_score, t_score):
            continue
        combined = (a_score + t_score) / 2
        if combined > best_score:
            best_score = combined
            best = item
    return best
```

Orchestrator call site:

```python
from clients.streaming.matching import find_best_typed_match

result = find_best_typed_match(
    response.results,
    query_artist=artist,
    query_title=album or "",
    artist_fn=lambda r: r.artist,
    title_fn=lambda r: r.album,
)
```

### Constraints honored

1. **Post-mutation inputs.** The score uses the same `artist` / `album` strings the search consumed (after `is_self_titled` → artist swap and `is_compilation_artist` → "Various" swap). Acceptance criterion 3.
2. **`_resolve_fallback_artwork` still runs.** It runs on whatever the picked match is — best-scored instead of `results[0]`. No change in the artist/label-image cascade.
3. **`None` return path is already supported.** `enrich_artwork_results` synthesizes a `DiscogsSearchResult(release_id=0, release_url="")` carrying just streaming URLs when artwork is `None` (LML#401). iOS/BS already key off that contract.
4. **Tie-break.** `find_best_typed_match` returns the **first** result reaching `best_score`; on ties it keeps Discogs's original ordering. Mirrors `find_best_match`'s behavior.

## Pre-merge measurement (acceptance criterion 5)

Need a measured estimate of the flip-to-`None` rate on real traffic before deploying. The artwork-enrichment path is cache-hot for recent flowsheet entries, so the measurement runs purely against the local Discogs cache — no live API calls.

**Script:** `scripts/measure_artwork_match_floor.py` (one-shot, not added to cron).

1. Read N=1000 most recently-enriched flowsheet rows from BS (or, simpler: sample N `library_items` recently passed to `fetch_artwork_for_items` from staging logs).
2. For each, replay the same `DiscogsSearchRequest` against the live `DiscogsService` (cache + API as usual).
3. Apply the new `find_best_typed_match` floor to the returned candidates.
4. Compare: would the picked release_id change? Would the result flip to `None`?
5. Emit a CSV with: `library_id, artist, album, old_release_id, new_release_id, flipped_to_none, top1_artist_score, top1_title_score, best_combined_score`.
6. Human spot-check K=20 of the `flipped_to_none` rows (open the Discogs release pages, confirm the old top-1 was indeed the wrong album).

**Pass bar:** ≤ 5% flip-to-`None` rate, and spot-checks of flipped rows confirm they were genuinely wrong releases. Above 5%, escalate before merging — likely indicates the 80/80 floor is mis-tuned for the Discogs distribution (e.g., subtitle-bearing releases like "Confield (Remastered Edition)" score lower against a bare library title than the streaming-side equivalent).

Result written into the PR description under "Measurement," not committed to the repo.

## Test plan

### Unit tests in `tests/unit/test_orchestrator_helpers.py`, class `TestFetchArtworkForItems`

1. **`test_picks_correct_release_over_misleading_top1`** — the Noura case. Mock `discogs_service.search` to return `[wrong, right]`:
   - `wrong = make_discogs_result(release_id=YENBETT, album="Yenbett", artist="Noura Mint Seymali", artwork_url=...)`
   - `right = make_discogs_result(release_id=TZENNI, album="Tzenni", artist="Noura Mint Seymali", artwork_url=...)`
   - Library item titled "Tzenni" by "Noura Mint Seymali".
   - Assert `results[0][1].release_id == TZENNI`.

2. **`test_returns_none_when_no_candidate_clears_floor`** — mock returns two candidates with clearly-wrong artist + album (e.g., `album="Some Other Album", artist="Other Artist"`). Library item is "Tzenni" by "Noura Mint Seymali". Assert `results[0][1] is None` and `_resolve_fallback_artwork` was **not** called (no result to fall back from).

3. **`test_self_titled_scores_against_mutated_album`** — library item `title="S/t"`, `artist="Pavement"`. `album` mutates to "Pavement". Mock candidate has `album="Pavement", artist="Pavement"`. Assert match returned (≥ 80/80 against the mutated album, not "S/t").

4. **`test_compilation_artist_scores_against_various`** — library item `artist="Various Artists - Rock - D"`, `title="Disco Not Disco"`. `artist` mutates to "Various". Mock candidate has `artist="Various", album="Disco Not Disco"`. Assert match returned. This is the regression risk of the existing `test_uses_discogs_titles_for_compilation_lookup` test — verify it still passes unchanged.

5. **`test_picks_higher_combined_score_among_acceptable_candidates`** — mock returns `[acceptable_85, acceptable_95]`. Assert the higher-scoring one wins, regardless of Discogs's ordering.

### Helper tests (if Option B) — `tests/unit/test_streaming_matching.py` or wherever `find_best_match` is tested today

- `test_find_best_typed_match_returns_original_object`
- `test_find_best_typed_match_handles_none_fields` (artist or title returning None on the candidate)
- `test_find_best_typed_match_returns_none_when_no_candidate_clears`

### Existing-test regressions

Several existing tests use `make_discogs_result(release_id=...)` with factory defaults (`album="Aluminum Tunes", artist="Stereolab"`) against a library item with a different artist/title (e.g., Autechre's *Confield*). Under the new floor the mocked result fails 80/80 and `fetch_one` returns `None`, breaking the test's setup.

**Remedy (apply uniformly to every affected test):** add `album=<item.title>, artist=<item.artist>` kwargs to the `make_discogs_result(...)` call so the mock matches the queried `LibraryItem`. Example:

```python
# before
make_discogs_result(release_id=28138, artwork_url=None)
# after
make_discogs_result(release_id=28138, album="Confield", artist="Autechre", artwork_url=None)
```

**Audit scope:** every test in **`tests/unit/test_orchestrator_helpers.py`** AND **`tests/unit/test_orchestrator.py`** AND **`tests/unit/test_orchestrator_gaps.py`** that constructs a `DiscogsSearchResult` (directly or via `make_discogs_result`) inside a `fetch_artwork_for_items` setup. Grep: `rg 'make_discogs_result|DiscogsSearchResult\(' tests/unit/test_orchestrator*.py`. For each hit, check whether the mock feeds `fetch_artwork_for_items` (directly or through a higher-level path that calls it). If yes and the factory defaults don't match the item's artist/title, pass matching kwargs.

Confirmed hits in `test_orchestrator_helpers.py` from current state (line numbers may drift):

- `test_falls_back_to_artist_image` (~1229)
- `test_falls_back_to_label_image` (~1255)
- `test_no_fallback_when_artwork_exists` (~1281)
- `test_returns_result_when_all_fallbacks_fail` (~1303)
- `test_fallback_when_get_release_returns_none` (~1324)
- `test_uses_release_artwork_when_search_misses` (~1340)
- `test_artwork_search_omits_label_and_format_when_absent` (~1212)

`test_orchestrator.py` and `test_orchestrator_gaps.py` need the same pass — list grew long enough during planning that an exhaustive enumeration here would drift; run the grep and apply the remedy per-hit.

**Verification:** `npm run test:unit` (or `pytest tests/unit/test_orchestrator*.py`) passes locally before pushing. Any test failing with `assert results[0][1] is not None` against a mismatched factory default is the symptom — fix by adding matching kwargs, not by lowering the floor.

## Acceptance criteria mapping

| Issue criterion | This plan covers it via |
|---|---|
| Ranks `response.results` via `find_best_match` (or equivalent) on `(artist, album)` 80/80 | `find_best_typed_match` helper + orchestrator call-site swap |
| Returns `None` when nothing clears 80/80 | `find_best_typed_match` returns `None`; orchestrator returns it through |
| Self-titled / compilation-artist mutations accounted for in the score input | Score uses the same post-mutation `artist` / `album` locals the search used |
| Noura Mint Seymali / "Tzenni" vs. "Yenbett" test | `test_picks_correct_release_over_misleading_top1` |
| Backfill-sample measurement documented before merging | `scripts/measure_artwork_match_floor.py` + PR-description "Measurement" section |

## Out of scope

- **#440** (Spotify `spotify_url` is a search-URL template, not verified). Different code path, same theme; separate PR.
- **#442** (Apple album-first lookup — the right album isn't in the search top-N). Different problem shape: this PR scores the results we have, #442 changes which results we ask for.
- **Tightening or loosening the 80/80 floor.** Reuse the existing constant. If measurement shows >5% flip-to-`None`, escalate as a follow-up instead of baking a different constant into this PR.
- **Bringing the same floor to `proxy.controller`'s direct Discogs paths in Backend-Service.** BS already routes through LML for artwork (per BS CLAUDE.md "All Discogs access … routes through LML"); the proxy paths inherit this fix automatically.

## Risk

- **False-negative blowup on subtitle-heavy releases.** "Album (Remastered Edition)" vs. "Album" scores high under `token_sort_ratio` (the constant in `score_match`), but compound subtitle drift could occasionally push a legitimate match below 80. The pre-merge measurement is the mitigation; if the rate is unacceptable, the right move is to extend `strip_format_suffix` in `matching.py` (helps every streaming caller too), not to lower the floor for the Discogs path.
- **Order-dependent tie-breaks.** If two candidates tie on combined score, we keep Discogs's original ordering. This matches `find_best_match`'s behavior; not a regression.

## Rollout

Standard merge → auto-deploy. No flag, no migration, no data backfill. The behavior change is contained to a single read path and reversible by reverting one commit.

## Out-of-scope follow-ups to file post-merge (only if pre-merge measurement surfaces them)

- Extend `strip_format_suffix` to handle additional subtitle patterns observed in the measurement.
- Apply `find_best_typed_match` to the artist-image / label-image cascade if measurement shows those paths also serve wrong-scope artwork.
