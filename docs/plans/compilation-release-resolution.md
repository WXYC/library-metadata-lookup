# Compilation release resolution — bind `discogs_url` for Various-Artists tracks

## Problem

A flowsheet track that lives on a Various-Artists (V/A) compilation never gets a Discogs link in the app, even when LML can prove exactly which Discogs release the track sits on. Concrete case: **A Guy Called Gerald — "Message to Black Youth"**, on the compilation **"When There Is No Sun"** ([discogs.com/release/36907527](https://www.discogs.com/release/36907527), credited to "Various"). The iOS "More Info" section renders its Discogs button only when `album.discogsURL` is non-nil; that value traces to `album_metadata.discogs_url` in Backend, which LML never bound.

Streaming buttons and cover art *do* appear because they resolve through compilation-agnostic paths (track-level streaming lookup keyed by `(artist, album)`; the iOS app's own free-text Discogs artwork search). Only the structured Discogs link is missing — and it is missing for **every** compilation track, systematically.

## Root cause (traced)

Two independent facts combine:

1. **No producer fires for a found album row.** Every LML path that resolves a *specific* V/A `release_id` by track — `TRACK_ON_COMPILATION`, `SONG_AS_TRACK`, and the library-miss probe (now `lookup/strategies/library_miss.py`; `lookup/orchestrator.py:3108` at writing) — is gated on `song_not_found` / library-miss. The enrichment worker supplies the album, so:
   - `resolve_albums_for_track` short-circuits at `lookup/orchestrator.py:826` (at writing) (album present, ≠ artist) → returns `song_not_found=False` (it never runs the Discogs track lookup).
   - That `False` seeds the pipeline (`:3084`); `ARTIST_PLUS_ALBUM` finds the row and returns `Outcome.album_match`, which **leaves `song_not_found` untouched** (`core/search.py:476`).
   - `TrackOnCompilation.should_attempt = song_not_found_with_artist_and_song` (`core/search.py:633`, gate at `lookup/strategies/track_on_compilation.py:50`) → `False`. The compilation strategy never runs.

2. **The binding step re-derives the release through an artist floor that rejects "Various".** `fetch_artwork_for_items.fetch_one` (now `lookup/artwork.py`; `lookup/orchestrator.py:2145+` at writing) re-searches Discogs by `item.alternate_artist_name or item.artist` (`:2155`) — the track artist — and only swaps in the "Various" search form `if is_compilation_artist(artist)` (`:2156`), i.e. only when the *library row itself* is filed under "Various". For a track-artist row it scores candidates with `find_best_typed_match` at the 80/80 **artist floor** (`:2188`); "A Guy Called Gerald" can never clear 80 against a release credited "Various", so the result is `None` → no `DiscogsMatchResult` → no `discogs_url`.

**Why the fix is possible:** `validate_track_on_release` matches the *per-track* artist credit (`discogs/service.py:1432-1436`, `_scan_tracklist_for_match`), not the release-level credit. "Message to Black Youth" credits "A Guy Called Gerald" on its own tracklist row, so the comp validates `True` for the track artist. The artist floor lives only in the artwork re-search; routing the binding step through validation instead of the floor is the bypass.

## Approach

Extract one deep release-resolution module and make the binding step its newest caller. This is the "consolidation" framing (vs. a one-off patch in `fetch_one`): today `process_release` (`:1610`), `SONG_AS_TRACK._validate_one` (`:1024`), and the library-miss probe each re-implement "find the Discogs release this track sits on, and validate it." Collapse that into one module; the binding step calls it lazily when its floor search fails.

### The module

```python
# lookup/release_resolution.py  (new)
"""Release resolution — find and validate the Discogs release a track sits on.

Owns the probe set (Discogs release search by track + per-candidate track-credit
validation) and ranking (title match to album, stable release_id tie-break).
Does NOT own row matching (library search, artist/compilation filtering) — the
search strategies own that. Called by TRACK_ON_COMPILATION, SONG_AS_TRACK, and
the binding step's lazy fallback (floor rejection + song present).
"""

@dataclass(frozen=True)
class ResolvedRelease:
    release_id: int
    release_url: str
    is_compilation: bool
    album_title: str            # preserves today's discogs_titles value

async def resolve_release_for_track(
    song: str,
    artist: str,
    album: str | None,
    discogs_service: DiscogsService,
) -> list[ResolvedRelease]:
    """Find and validate the Discogs release(s) a track sits on.

    Owns the probe set (the Wave A/B `search_releases_by_track` calls + the
    conditional V/A-merge, lifted wholesale from search_compilations_for_track
    :1481-1519) and per-candidate `validate_track_on_release` (which matches the
    per-track credit, not the release credit). Does NOT do library-row matching.

    Returns releases ranked by title match to `album` (via `score_match` on
    TITLE ONLY — deliberately not find_best_typed_match, whose artist floor is
    what we are bypassing; artist is settled by track-credit validation), with a
    stable `release_id` tie-break. Empty when nothing validates.
    """
```

- **Boundary: release-only.** Library-row matching (`search_album_fuzzy` + the artist/compilation filter) stays in the strategies; the binding step already has its row.
- **Return: ranked list.** Ranking is owned by the module (title-match primary, stable `release_id` tie-break). The binding step takes `[0]`; strategies that want per-release provenance iterate. This is what kills the divergent-pressing bug — instead of taking Discogs's top-1 from a re-search (which can be a deluxe reissue), the module deterministically prefers the release whose title matches the requested album.

### The seam

`SearchState.discogs_titles: dict[int, str]` (`core/search.py:333`) becomes `dict[int, ResolvedRelease]`, threaded through `Outcome.compilation` (`core/search.py:491`), `_apply` (`:570`), and into `fetch_artwork_for_items`. Internal to LML — never on the wire.

### The binding step

`fetch_one` gains two paths:
- **Carried release present** (strategy already resolved it): trust-and-bind — synthesize `DiscogsSearchResult` from `release_id`/`release_url`, fetch art via `_resolve_fallback_artwork(release_id)` (`:2096`), skip `search()` + floor. Downstream `enrich_artwork_results` fills year/label/genres via `get_release` as today.
- **No carried release AND floor search returned `None` AND `song` present**: lazily call `resolve_release_for_track`, take `[0]`, trust-and-bind. This is the path that fixes a directly-found album row.

Trust is sound: a carried release was validated this same request; a lazily-resolved release was just validated by the module. The floor was never a validation backstop — it is a search disambiguator, and validation already settled the artist.

## Decisions (locked)

| # | Decision | Resolution |
|---|---|---|
| 1 | Channel | Internal `ResolvedRelease` on the upgraded `discogs_titles` dict. Not on `LibraryItem`, not on the wire. |
| 2 | Binding behavior | Trust-and-bind (skip search + floor; `get_release(id)` for art/year). |
| 3 | Shape | Extract one `resolve_release_for_track` module; strategies delegate; binding calls lazily on floor-reject. |
| 4 | Module boundary | Release-only (probe + track-credit validation). Row-matching stays in strategies. |
| 5 | Return | Ranked `list[ResolvedRelease]`; title-match primary, stable `release_id` tie-break; binding takes `[0]`. |
| 6 | Cost posture | Flagged (default off), **negative-cached**, **bulk path excluded**. Live worker is first consumer. |
| 7 | Sequencing | PR1 behavior-neutral extraction **+ seam-widen** → PR2 flagged lazy bind. |
| 8 | Wire scope | Invisible; internal telemetry only. No `api.yaml` change. |

## PR1 — Extract the module + widen the seam (behavior-neutral)

**Goal:** pay down the duplication and do all the structural plumbing at once, with zero *observable* behavior change. The existing pinned suites (LML#536 / #301 / #400 / #478) are the proof — if a behavior test (which rows surface, which `discogs_url` binds) moves, the extraction is wrong.

- New `lookup/release_resolution.py`: module docstring (above) + `ResolvedRelease` + `resolve_release_for_track`. Lift the probe set + V/A-merge from `search_compilations_for_track` (`:1481-1519`) and the `validate_track_on_release` loop.
- `search_compilations_for_track.process_release` (`:1537-1633`) delegates its probe+validate to the module; keeps its own `search_album_fuzzy` + row filter.
- `SONG_AS_TRACK._validate_one` (`:1006-1043`) delegates likewise.
- **Widen the seam here** (not PR2 — doing it later would re-touch these exact call sites): `SearchState.discogs_titles: dict[int, str]` → `dict[int, ResolvedRelease]`, through `Outcome.compilation` (`core/search.py:491`), `_apply` (`:570`), and the `fetch_artwork_for_items` signature (`:2137`). **The binding step stays behavior-identical** — it extracts `release.album_title` from the carried `ResolvedRelease` and re-searches exactly as today. The richer type is produced but not yet *consumed* differently. That consumption is PR2's flagged change.
- Tests: characterization on observable behavior (rows, URLs). Internal-shape assertions that referenced `discogs_titles[id] == "<title>"` update mechanically to `discogs_titles[id].album_title == "<title>"` — part of the refactor, not a behavior change. Run the full marker matrix (`pg`, `external_api`) green.

**Risk:** hottest matching code. Mitigation: behavior-identical extraction + widen; green pinned behavior-tests as the contract; ranking helper reuses existing `score_match`, and because the binding step still re-searches in PR1, scoring/selection is unchanged until PR2.

## PR2 — Lazy bind (flagged behavior)

**Goal:** the symptom fix. All structural plumbing (module, widened seam) already landed in PR1; PR2 changes only how the binding step *consumes* the seam, behind a flag.

- `fetch_one` (now `lookup/artwork.py`; `lookup/orchestrator.py:2145+` at writing): add two paths — (a) **carried-release trust-and-bind** (a `ResolvedRelease` is present in the seam → synthesize `DiscogsSearchResult` from `release_id`/`release_url`, art via `_resolve_fallback_artwork(release_id)`, skip `search()`+floor); (b) **lazy fallback** — when no carried release AND the floor search returned `None` AND `song` is present AND the flag is on AND `allow_release_resolution_fallback`, call `resolve_release_for_track`, take `[0]`, trust-and-bind.
- **Flag** — add to `config/settings.py` (mirror `lml_resolve_artist_canonical`):
  ```python
  lml_resolve_compilation_release: bool = Field(
      default=False,
      description=(
          "When True, the artwork binding step lazily runs "
          "resolve_release_for_track() as a fallback when its floor search "
          "returns None and a song is present — binding the validated release "
          "(typically a Various-Artists compilation) without the artist floor. "
          "When False, a floor-rejected release stays unbound (existing "
          "behavior). Default False; roll staging -> prod, watch Discogs call "
          "rate. Never enabled on the /lookup/bulk path. See LML#<issue>."
      ),
  )
  ```
- **Negative cache:** wrap `resolve_release_for_track` with the L1 `@async_cached` null-pinning pattern, keyed `(song, artist, album)`, so an unresolvable row does not re-probe every poll. Mandatory — without it this is the 2026-05-21 backfill-monopolization / LML#370-#372 cascade shape.
- **Bulk exclusion:** add `allow_release_resolution_fallback: bool = True` to `perform_lookup`'s signature. `handle_lookup` (`lookup/router.py:172`) uses the default (`True`); `handle_bulk_lookup`'s `_run_one` (`lookup/router.py:333`) passes `False`. So the 35k-album bulk drain can never trigger the fan-out. Historical compilation backfill, if wanted, is a separate paced job.
- **Telemetry:** a `_log_track_validation`-style counter for "lazy fallback fired / bound" so we can prove adoption and watch cost. No wire field.

### Test matrix (PR2)

| Test | Asserts |
|---|---|
| **Red→green spine** | Stub Discogs so `search_releases_by_track("Message to Black Youth","A Guy Called Gerald")` returns `ReleaseInfo(album="When There Is No Sun", artist="Various", release_id=36907527, release_url="https://www.discogs.com/release/36907527", is_compilation=True)`, and `get_release(36907527)` returns a tracklist whose "Message to Black Youth" row credits "A Guy Called Gerald" (so `validate_track_on_release` passes on the per-track credit). Request `(artist="A Guy Called Gerald", album="When There Is No Sun", song="Message to Black Youth")` with the flag on; assert `discogs_url == "https://www.discogs.com/release/36907527"`. Fails today (floor rejects "Various"). |
| Negative cache | A second identical request issues no new Discogs probe when the first resolved to empty. |
| Bulk exclusion | `/lookup/bulk` does not invoke the lazy fallback. |
| Ranking | Two validated pressings (original + reissue) → the title-matched original is bound, deterministically across runs. |
| Carried-release trust | A `TRACK_ON_COMPILATION` hit binds by id without a re-search (assert no `search()` call). |
| Flag off | Flag disabled → behavior identical to pre-PR2 (no fallback, floor `None` stays `None`). |

## Out of scope

- Wire provenance (`TrackMatchHint.release_id` / `matched_via` for the bound comp) — the optional follow-on if a real consumer (e.g. dj-site "appears on compilation X") appears.
- The consolidation of the 80-floor constants and the V/A gates into one verdict module (review Candidate #2) — separate, calibration-sensitive work.
- Persisting `discogs_release_id` as a first-class column / `entity.release_identity` join (review Candidate #3) — crosses the discogs-cache schema-ownership seam.
- Backend `album_metadata` projection consolidation (review Candidate #4) — BS-side, project #32 / Epic D.

## Rollout

1. Merge PR1 (no behavior change; auto-deploys to staging on `main`).
2. Merge PR2 with `lml_resolve_compilation_release` **off**.
3. Enable on staging; verify the spine case binds and watch the Discogs call-rate metric.
4. Enable on prod (`prod` branch). Spot-check the screenshot case in the app; confirm no cascade signature on the Discogs semaphore.

## CONTEXT.md

This branch adds the "Release resolution (compilation cross-reference)" language section to `CONTEXT.md` (Release resolution, ResolvedRelease, track-credit validation, artist floor, binding step, compilation/V-A) and an example dialogue. It travels with PR1.
