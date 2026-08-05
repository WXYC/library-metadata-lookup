# Decompose lookup/orchestrator.py (4,485 lines → ~450-line spine + 12 concern modules)

## Context

`lookup/orchestrator.py` is a 4,485-line god module: the `/lookup` pipeline spine plus nine distinct concerns that accreted around it. Its two core functions are `enrich_artwork_results` (769 lines, two nested closures over ~15 variables — untestable in pieces) and `perform_lookup` (~440 lines threading five mutable locals through 13 implicitly-ordered steps). Every recent feature (#699, #684, #663, #652, #718) landed inside this file because it was already where everything lived. The module's public surface is tiny — production code (`lookup/router.py`) imports exactly `perform_lookup` and `NONLIBRARY_RELEASE_SURFACED_STAT_KEY` — so the decomposition is almost entirely internal motion. The `lookup/strategies/` subpackage already exists with five per-strategy shell modules (gating predicates + type aliases); the execute implementations they wire via `build_strategies(...)` partials still live in the monolith. This plan finishes that half-built structure.

Decisions below were resolved in a design interview 2026-07-05; this file is the implementation manifest.

## Goal and non-goals

**Goal:** regression-resistance for future feature work in the hot path. Each concern becomes an independently ownable, independently testable module; the spine's data flow becomes explicit; a guardrail prevents regrowth.

**Non-goals (explicitly out of scope):**
- No behavior changes anywhere. If a PR discovers it needs one, stop the line: file it, land it separately red-green, rebase.
- `perform_lookup`'s public signature is FROZEN (85 call sites across 12 test files + router). The services bundle is internal only.
- The `build_strategies(...)` injection seam stays as-is (it is runtime dependency binding, not test plumbing).
- No internal decomposition of `search_compilations_for_track` (443 lines) — it moves intact; splitting it is a candidate follow-up after the program.
- No re-export layer, ever — see Mechanics.

## Sequencing gates

- ~~**#687** must land before PR 5~~ — gate dropped 2026-07-05 (Jake): PR 5 moves `_resolve_fallback_artwork` as-is; #687 implements against `lookup/artwork.py` afterward.
- **#685** (bulk per-item `extended` verify-and-guarantee) must land before PR 6 (its tests become extra pinning for the enrichment move).
- **#504/#505/#507** (`enrich_one` chain): if still unstarted when PR 5 merges, PR 6 goes first and the chain implements on the decomposed structure. If the chain lands first, PR 6a/6b rebase over it (motion PRs rebase cheaply). Whichever path is taken, record the decision in the epic when PR 5 merges.
- #717/#718: already landed (2026-07-04) — its tests in `test_album_title_fallback.py` are part of the pinning suite.

## Target layout

```
lookup/
  orchestrator.py          # spine only: perform_lookup + step functions + LookupState/LookupServices
                           # + resolve_albums_for_track, build_context_message, _identity_to_reconciled,
                           #   _resolve_identities (spine-scoped helpers)   ~450 lines
  matching.py              # pure predicates, filters, floors — no I/O, no async defs with I/O
  concurrency.py           # _chunked_gather + API-call-budget machinery
  artist_resolution.py     # ResolverOutcome, resolve_canonical_artist, flag gates, log projections
  rowless.py               # non-library release synthesis + credit recovery (#628/#632/#652 cluster)
  validation.py            # track validation + cached-track safety net
  artwork.py               # fetch_artwork_for_items + release binding + fallback artwork
  strategies/
    artist_plus_album.py       # (existing shell) + search_library_with_fallback
    swapped_interpretation.py  # (existing shell) + search_with_alternative_interpretation, _narrow_swapped_by_track
    track_on_compilation.py    # (existing shell) + search_compilations_for_track, _log_album_title_fallback, _ProbeResult
    song_as_artist.py          # (existing shell) + search_song_as_artist
    song_as_track.py           # (existing shell) + search_song_as_track
    track_release_matching.py  # NEW shared kernel: _match_track_releases_to_library, search_album_fuzzy
    library_miss.py            # NEW: _library_miss_discogs_search (#583 step-3a probe)
  enrichment/
    __init__.py            # enrich_artwork_results coordinator
    context.py             # EnrichmentContext (frozen dataclass)
    top1.py                # top-1 release/artist/bio fetch (was nested fetch_top1_release_details)
    item.py                # per-item gates + streaming-URL assignment + extended fields (was nested enrich_one)
    background.py          # _background_tasks, _warm_bio_cache, warm-cache semaphore
core/
  thresholds.py            # NEW leaf: CANONICAL_ARTIST_SIMILARITY_FLOOR (shared with release/musicbrainz_resolver.py)
tests/unit/
  test_module_budgets.py   # NEW (PR 7): per-file line-budget guardrail
```

Dependency direction is strictly downward: orchestrator → {strategies, enrichment, validation, artwork, rowless} → {artist_resolution, matching, concurrency} → core. `core/thresholds.py` is a leaf importable from both `lookup/` and `release/` (release must not import lookup — see the circular-import note at `release/musicbrainz_resolver.py:60-66`, which this plan deletes).

## Symbol manifest (line numbers = current origin/main, 4,485-line file)

### PR 1 — `lookup/matching.py` + `lookup/concurrency.py`

**matching.py** (pure; no service handles): `MAX_SEARCH_RESULTS` (106), `SELF_TITLED_PATTERNS` (109), `is_self_titled` (702), `map_library_format_to_discogs` (714), `_FETCH_LIMIT` (740), `limit_results` (750), `artist_matches_item` (755), `library_artist_for` (783), `filter_results_by_artist` (839), `_release_matches_library_row` (1671), `_ALBUM_MATCH_FLOOR` (1699), `_FALLBACK_ARTIST_SIMILARITY_FLOOR` (1713), `_filter_results_by_album_match` (1716), `_SONG_AS_ALBUM_TITLE_FLOOR` (1805), `_filter_results_by_song_as_album_title` (1808), `_VA_VOLUME_SUFFIX_RE` (2450), `_va_series_base` (2453), `_va_series_title_match` (2468), `album_title_acceptable` (2510), `_TRAILING_PARENTHETICAL_RE` (2566).

**concurrency.py**: `_SEARCH_MAX_API_CALLS_DEFAULT` (141), `_SEARCH_MAX_API_CALLS_ENV_VAR` (151), `_chunked_gather` (154), `_record_search_api_call_cap_fired` (229).

Tests to rewire: `test_matching.py`, `test_orchestrator_helpers.py` (`MAX_SEARCH_RESULTS`, `_chunked_gather`, `_record_search_api_call_cap_fired` imports), `test_album_match_floor.py`, `test_alternate_artist.py` (`filter_results_by_artist`).

### PR 2 — `lookup/artist_resolution.py` + `core/thresholds.py`

**core/thresholds.py**: `CANONICAL_ARTIST_SIMILARITY_FLOOR` (362). Also: delete the duplicated `_SIMILARITY_FLOOR` in `release/musicbrainz_resolver.py:66` and its keep-in-sync comment; both files import the leaf. The module carries a header comment declaring the leaf constraint: it must never import from `lookup/`, `release/`, or `discogs/` (that is what makes it cycle-proof — `lookup/` already imports `release.musicbrainz_resolver`, so the shared constant cannot live in either package). PR 2 exit check: `core/thresholds.py` has zero non-stdlib imports.

**artist_resolution.py**: `_ARTIST_IDENTITY_SPLIT_GATE_ENV_VAR` (286), `_FALSE_FLAG_VALUES` (293), `_artist_identity_split_gate_enabled` (299), `_MB_RESCUE_REQUIRE_SONG_MATCH_ENV_VAR` (309), `_mb_rescue_song_match_required` (317), `_artist_pair_verified` (326), `_resolver_cache` (372), `ResolverOutcome` (380), `resolve_canonical_artist` (400), `_log_release_resolution_bind` (505), `_project_mb_rescue_attrs` (546), `_log_resolver_pre_pass` (594), `_log_artist_identity_split_gate` (634).

Note: `_log_album_title_fallback` (467) does NOT move here despite sitting in this block — its only consumers are inside `search_compilations_for_track` (2349, 2381); it moves in PR 4b.

Tests to rewire: `test_resolver_pre_pass.py`, `test_orchestrator_helpers.py` (`_log_release_resolution_bind`), `test_album_title_fallback.py` (`CANONICAL_ARTIST_SIMILARITY_FLOOR` import line), `test_rowless_flag_observability.py` (imports flag-gate names — verify at PR time).

### PR 3 — `lookup/rowless.py`

`_PACKED_TITLE_SEPARATOR` (1080), `_own_release_credit` (1083), `_select_rowless_artist_release` (1111), `ROWLESS_LIBRARY_ID` (1205), `NONLIBRARY_RELEASE_SURFACED_STAT_KEY` (1216), `_make_rowless_item` (1219), `_resolve_nonlibrary_release` (1237), `_rehydrate_resolved_release` (1329), `_MAX_CREDIT_RECOVERY_FETCHES` (1362), `_recover_track_credit` (1365).

Production import update: `lookup/router.py:58` (`NONLIBRARY_RELEASE_SURFACED_STAT_KEY`).

Tests to rewire: `test_nonlibrary_release_resolution.py` (incl. `setattr(orchestrator, "_MAX_CREDIT_RECOVERY_FETCHES", ...)` → new module), `test_rowless_flag_observability.py`, `test_rowless_enrichment.py` (`ROWLESS_LIBRARY_ID` import line).

### PR 4a — strategies: kernel + its two consumers

**strategies/track_release_matching.py** (new): `_match_track_releases_to_library` (1397), `search_album_fuzzy` (2569).
**strategies/song_as_track.py**: `search_song_as_track` (1628).
**strategies/swapped_interpretation.py**: `_narrow_swapped_by_track` (864), `search_with_alternative_interpretation` (905).

Tests to rewire: `test_search_album_fuzzy.py`, `test_search.py` (verify import lines at PR time), song-as-track/SWAPPED suites.

### PR 4b — strategies: remaining three + library-miss probe

**strategies/track_on_compilation.py**: `_ProbeResult` (264), `search_compilations_for_track` (1973), `_log_album_title_fallback` (467).
**strategies/song_as_artist.py**: `search_song_as_artist` (975).
**strategies/artist_plus_album.py**: `search_library_with_fallback` (1845).
**strategies/library_miss.py** (new): `_library_miss_discogs_search` (1741).

Tests to rewire: `test_compilation_wave_merge.py`, `test_album_title_fallback.py` (`search_compilations_for_track`), `test_library_miss_discogs.py` (import line).

### PR 5 — `lookup/validation.py` + `lookup/artwork.py`

**validation.py**: `filter_results_by_track_validation` (2676), `find_library_albums_with_cached_track` (2742).
**artwork.py**: `COMPILATION_ARTIST_SEARCH_FORM` (112), `COMPILATION_ARTIST_CANONICAL_FORM` (115), `_resolve_fallback_artwork` (2866, post-#687 shape), `_bind_resolved_release` (2904), `fetch_artwork_for_items` (2953).

Tests to rewire: `test_cached_track_safety_net.py`, artwork-fetch suites (enumerate by grep at PR time).

### PR 6a — enrichment, pure motion

Move intact to `lookup/enrichment/__init__.py`: `_WARM_CACHE_CONCURRENCY` (126), `_warm_cache_semaphore` (137), `_background_tasks` (275), `_build_streaming_search_url` (3177), `enrich_artwork_results` (3183), `_warm_bio_cache` (3952).

Tests to rewire: `test_enrichment.py` (incl. all `lookup.orchestrator.sentry_sdk` / `.asyncio` / `.apple_music_lookup_timeout_s` / `.set_cached_release_id` patches whose consuming code moves), `test_rowless_enrichment.py`, `test_fd_leak_regression_241.py` (patches `lookup.orchestrator` module attrs — retarget).

### PR 6b — enrichment, decomposition (shape change #1)

Un-nest the two closures into module functions: `context.py` gets a frozen `EnrichmentContext` (service handles: discogs_service, discogs_cache, mb_pg, apple_music, spotify, bandcamp, entity_store, discogs_cache_pg, library_db; request scalars: song, album, artist, extended, warm_cache, found_on_compilation). `top1.py` gets the former `fetch_top1_release_details`; `item.py` gets the former `enrich_one` (per-item gates + streaming-URL assignment + extended fields); `background.py` gets `_background_tasks`, `_warm_bio_cache`, warm-cache semaphore. `__init__.py` keeps `enrich_artwork_results` as a thin coordinator with an unchanged signature.

Pre-flight: run the enrichment suites with `--cov-branch`; backfill a pinning test only if a fully-dark branch arm exists (statement coverage measured 2026-07-05: 93.4%, no uncovered block ≥5 lines).

### PR 7 — respine (shape change #2) + guardrail

- `LookupState` mutable dataclass (fields: `library_results`, `items_with_artwork`, `song_not_found`, `found_on_compilation`, `discogs_titles`, `search_type`, `library_miss_outcome`, `corrected_artist`), created at the top of `perform_lookup`; each field documented.
- Named step functions inside `orchestrator.py`, each mutating the state and documenting which fields it reads/writes; the ordering invariants (step-3a validation bypass; step 9 appends after step 4) stated where enforced.
- Internal-only `LookupServices` dataclass bundling the service handles for step-function signatures. Public `perform_lookup` signature unchanged.
- The #626 two-channel seam (`parsed.library_artist = corrected` + its comment block, orchestrator.py:4099-4108) preserved verbatim.
- `tests/unit/test_module_budgets.py`: per-file line ceilings for `lookup/**/*.py`, calibrated with ~30% headroom over post-PR-7 actuals (spine target ~450 → ceiling 700; `track_on_compilation.py` ~550 → 800; others proportional). These numbers are a draft — measure actuals after PR 6b, recalibrate, and record the final table in PR 7's body.
- Spine-scoped helpers stay: `resolve_albums_for_track` (795, called only from `perform_lookup`), `build_context_message` (3975), `_identity_to_reconciled` (4003), `_resolve_identities` (4015).
- Final sweep: assert zero remaining `from lookup.orchestrator import` of moved names; `docs/architecture.md` key-files section final pass.

## Mechanics (every PR)

1. **Two-commit structure**: a *move* commit (code relocated verbatim; new module docstrings + loggers only) then a *rewire* commit (imports + test patch targets). Reviewer verifies the move commit with `git diff --color-moved=zebra`.
2. **No re-exports.** Every import of a moved name is rewritten in the same PR. A stale `from lookup.orchestrator import X` must fail loudly.
3. **Stale-patch grep audit (exit criterion)**: after the rewire, `grep -rn "lookup\.orchestrator\." tests/` — every surviving hit must reference a name whose *consuming code still lives in* `orchestrator.py`. This catches the silent case: module-attr patches (e.g. `lookup.orchestrator.sentry_sdk`) that still resolve but no longer affect the moved code. Each PR body records the patch targets it retargeted (the ledger under Risks is the running ground truth; update it as PRs land).
4. **Local gate before push**: full unit suite (`uv run --no-sync python -m pytest`), `ruff check`, `ruff format --check`. Integration `-m pg` suite when the PR touches code with pg-marked coverage.
5. **Docs ride along**: `docs/architecture.md` file references updated in whichever PR moves the code they cite.
6. **TDD waiver** (agreed 2026-07-05): motion and shape PRs carry no new behavior; the existing suite passing unmodified (except import/patch rewires) is the correctness criterion. The interface is stable, so the suite asserts behavior regardless of implementation. Any needed behavior change stops the line and lands separately red-green.
7. Serial execution: one PR at a time, own worktree branched from origin/main, `/code-review` loop (fix significant, sweep nits), rebase-merge, then the next PR starts.
8. **Prod promotion: once, after PR 7 merges** and staging soaks. No intermediate prod pushes for this program.

## Risks

- **Silent monkeypatch drift** — mitigated by mechanic 3; the known patch surface is ~9 distinct targets (`sentry_sdk` ×8 sites, `asyncio` ×4, `set_cached_release_id` ×3, `apple_music_lookup_timeout_s` ×3, `resolve_release_for_track` ×2, `_MAX_CREDIT_RECOVERY_FETCHES` ×2, `logger`, `get_cached_release_id`, `_log_album_title_fallback`).
- **Hidden coupling discovered mid-move** (a "pure" helper reading a module global that stays behind) — the move commit fails imports/tests immediately; resolve by moving the global with it or threading a parameter, never by a back-import from `orchestrator.py`.
- **Conflict with the #504/#507 chain** — governed by the sequencing gate above.
- **`core/search.py` docstrings** reference `_chunked_gather` locations (core/search.py:105,121,905) — update the references in PR 1's rewire commit.

## Success criteria

- `lookup/orchestrator.py` ≤ ~450 lines; no `lookup/` module exceeds its budget; budget test enforcing both. *(Recalibrated 2026-07-06 at PR 7: the respine's executable delta over the pre-PR-7 spine is pure plumbing — verified token-level by the independent audit — but the mandated per-field and per-step READS/WRITES documentation costs ~350 physical lines, so the spine landed at 991 with a 1300 ceiling per the test's smallest-multiple-of-50 ≥ 1.3× convention. The ~450 draft predates knowing the documentation cost; disclosed in PR #749's body and accepted at merge. Follow-ups #750/#751 thin the spine further and retighten the ceiling deliberately.)*
- Zero re-exports; zero stale `lookup.orchestrator.*` patch targets.
- Full suite green at every PR; no test deleted or weakened (import lines and patch targets are the only permitted test edits in motion PRs; PR 6b/7 may restructure test arrangement but not assertions).
- `docs/architecture.md` strategy table's file column is accurate (it already names the per-strategy paths — the code finally matches the docs).
- Epic + 9 sub-issues closed; single prod promotion at the end.

## Tracking

Epic on the Post-launch service hardening board (org project 32), related to epic H (BS#882); LML#608 referenced as prose "Related". Nine sub-issues (PR 1, 2, 3, 4a, 4b, 5, 6a, 6b, 7), each blocked-by its predecessor.
