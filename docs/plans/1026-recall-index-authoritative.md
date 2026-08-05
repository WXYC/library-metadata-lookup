# Plan — LML#1026: make the recall index authoritative on the /lookup comp-track path

**Written:** 2026-08-01 PT
**Issue:** WXYC/library-metadata-lookup#1026 (follow-up to #1022, merged in #1025; post-fold context from #1029/#1030 and `docs/plans/location-union-transparent-results.md` §6/§7)
**Worktree:** `library-metadata-lookup-worktrees/1026-recall-index-authoritative` (branch `feat/1026-recall-index-authoritative` off `origin/main` = `67e3fe2`)

## 1. Context and post-fold translation

The issue predates the transparent fold (#1030): it says "when `include_locations` is set" and names `resolve_also_available_on`. Both are gone. The up-to-date translation, per plan §6 ("survives and becomes more central"): the union now runs **default-on** for every interactive song-bearing lookup (`should_run_location_union(request) and not is_discogs_low_priority()`, `orchestrator.py:1116-1122`), and the probe fn is `resolve_track_shelf_locations`. "The flagged path" therefore means "the union-active path" — exactly the requests for which `location_union_task` is created.

What #1026 changes: on the union-active path, `TRACK_ON_COMPILATION`'s ~6-call live-Discogs comp-discovery pass (Wave A + Wave B searches + per-release `validate_release_for_track` calls) is removed; the recall index `lml_cache.compilation_track_location` is the single source for comp-track resolution (Case A + Case B). The #271 grill locked "removed, not augmented" — an index miss does NOT fall back to live comp discovery (live discovery is title-distinctiveness-dependent and silently partial; keeping it as a fallback reintroduces exactly that nondeterminism).

What #1026 must NOT remove (co-located in `search_compilations_for_track` but not comp-location discovery):

- **The album-title fallback** (#319/#237, the trio-collaboration case — `_apply_album_title_fallback` + its speculative probe). It matches the artist's *own* collaborative release by typed album title; the recall index (V/A comp tracks only) cannot answer it. Removing it would regress trio lookups with zero replacement.
- **The keyword last-resort** (`_search_by_keyword`) — local SQLite, no Discogs.
- **The rowless non-library carry-through** (#628, `_carry_through_nonlibrary_release`) — resolution of a *non-library* release, governed by its own `lml_resolve_nonlibrary_release` kill switch. The index cannot answer "not in the library at all".

## 2. Core mechanism

**Threading:** `perform_lookup` already holds `location_union_task` before step 3 runs. `_step_search_pipeline` gains a `location_union_task` parameter, and the `search_compilations_func` partial binds it: `partial(search_compilations_for_track, …, location_union_task=location_union_task)`. `search_compilations_for_track` gains keyword-only `location_union_task: asyncio.Task[list[ResolvedLocation]] | None = None`. The `TrackOnCompilationExecute` alias is unchanged (the partial erases the kwarg). No `perform_lookup` signature change (LML#722 freeze respected). Import direction `lookup/strategies/track_on_compilation.py` → `lookup/location_union.py` adds no cycle (`location_union` imports no strategy module).

**Tri-state behavior in `search_compilations_for_track`:**

1. **`location_union_task is None`** (kill switch off / low-priority caller / no song): byte-identical legacy behavior — full live pass. This makes `LML_LOCATION_UNION_ENABLED=false` a complete rollback lever for #1026 too, and keeps class-5 backfill/enrichment semantics (BS canonical-entity + artwork backfills, `/lookup/bulk`) untouched.
2. **Task present, index hit** (awaited task returns ≥1 location): return `([], {})` immediately — no keyword search, no probes, no album fallback, no rowless carve. `Outcome.empty()` writes nothing, so prior results survive; the fold appends the locations after the spine. Case B (no prior results): the ranked-first location becomes `results[0]` — the primary is resolved from the index. Case A′ (artist's own release matched but `song_not_found`): the prior artist-release rows stay primary and the comp location is appended — a deliberate semantic change from the live pass's replace+stash (locations are appended results, per the fold design; `found_on_compilation`/`search_type`/`context_message` are already reconciled post-fold by #1030's `_reconcile_post_fold_signals`). The rowless carve is correctly skipped: an index hit proves the track IS in the library, so a "not in your library" rowless row would be wrong and would preempt the shelf location as primary (breaking the `ikebana` acceptance criterion).
3. **Task present, index miss** (returns `[]`, or awaiting it raises): degraded pass — `_search_by_keyword` + album-title probe (when `parsed.album` typed and its existing preconditions hold) + `_apply_album_title_fallback` + rowless carve, but **no Wave A/Wave B comp discovery and no per-release validates**. Implemented by adding an explicit `suppress_comp_discovery: bool = False` keyword to `_run_discogs_probes` (threaded from `search_compilations_for_track`): when True, skip the memo-seed/fresh-gather paths entirely, fire only `_album_title_probe_safe` (when `album_fallback_should_fire`), and return a `_ProbeBundle` with `raw_releases=[]` and the album probe's response/error populated (so `_collect_release_matches` is a no-op and `_apply_album_title_fallback` consumes the probe unchanged). The resolver pre-pass still runs (its `outcome.swapped` gates the album probe, unchanged).

**Await cost:** the task starts before step 3 and is a single indexed btree probe; by TRACK_ON_COMPILATION time (after ARTIST_PLUS_ALBUM + SWAPPED_INTERPRETATION) it is effectively always done, and awaiting a completed task is free. The worst case for the PG leg is bounded by the new probe timeout (below); the task body's second leg — the `db.get_items_by_ids` shelf join — remains unbounded but is a local-SQLite `IN` query (single-digit ms, per plan §7 of the parent plan), which is acceptable versus the multi-second live pass this branch replaces. The strategy's await is wrapped `except Exception → treat as miss` (and never catches `CancelledError`).

## 3. §7 belt-and-suspenders: probe timeout

`entity/compilation_track_location.py::get_compilation_track_locations` wraps its `pg.fetchall(...)` in `asyncio.wait_for(..., timeout=_PROBE_TIMEOUT_S)` (module constant, 2.0s — no new env var). Timeout and any other failure degrade to `[]` via the existing `except Exception` (Python ≥3.11: `asyncio.TimeoutError is TimeoutError`, an `Exception`). Also move the `CompilationTrackLocationRow(**row)` list-build **inside** the try, so a future column addition/rename degrades to `[]` instead of propagating a `TypeError`. This matters more after #1026 because the strategy awaits the task mid-spine.

## 4. The four issue nits, post-fold

1. **`lookup/location_union.py` fail-safe:** wrap `resolve_track_shelf_locations`'s rank/sort + final construction (everything after the probe's early returns) in an outer `try/except Exception → log + []`, keeping the inner shelf-join guard (which preserves partial rank-only degradation). A future recall-index schema relaxation (e.g. a `credit_role` outside the tier map is already tolerated, but a `None` title reaching `fuzz.ratio` is not) degrades to no locations, never a 500.
2. **Bulk amplification guard:** post-fold this is structural — `/lookup/bulk` calls `set_discogs_low_priority(True)` unconditionally for the whole batch (`router.py:855`), and the task-creation gate checks `not is_discogs_low_priority()`, so no bulk item can ever fan out a probe + shelf join. Action: verify existing test coverage pins the bulk contextvar; add a pinning test only if missing. No code change expected.
3. **Kill-switch wording:** `config/settings.py`'s `lml_location_union_enabled` docstring was already rewritten by #1030 (no stale "omitted" language). #1026 changes its blast radius: False now also **restores the legacy live-Discogs comp pass** (because the strategy bypass keys off the task existing). Update the docstring + `docs/env-vars.md` + `docs/architecture.md` (strategy-table row for TRACK_ON_COMPILATION) to say so.
4. **End-to-end fold test:** add a `perform_lookup` test that exercises the real `resolve_track_shelf_locations` → rank → shelf-join → `build_location_result_items` chain, mocking only `lookup.location_union.get_compilation_track_locations` (rows in) and `db.get_items_by_ids` (shelf items out), with a mock `discogs_cache_pg` passed so the probe actually runs.

## 5. TDD test list (failing first, in this order)

`tests/unit/test_compilation_wave_merge.py` (the existing harness that mocks the Discogs service and asserts on Wave A/B calls — do NOT create a new `test_track_on_compilation.py`):
- T1 index hit → `([], {})`; `discogs_service.search_releases_by_track` never called; `db.search` (keyword) never called; rowless resolver never called.
- T2 index miss → Wave A/B never called; album-title probe fires when album typed; keyword last-resort runs; rowless carve unchanged.
- T3 `location_union_task=None` → legacy: probes fire exactly as before (regression pin).
- T4 task raises → treated as miss (T2 shape), no exception escapes.

`tests/unit/test_orchestrator.py`:
- T5 Case-B repro extended: union-active `perform_lookup` never issues live comp discovery (assert on the discogs service mock) and LiT id 60654 is `results[0]`.
- T6 Case-A′: artist release matched, song not found, index hit → artist release stays `results[0]`, location appended, `found_on_compilation=True` (pins the deliberate replace→append change).
- T7 the nit-4 end-to-end real-probe test.
- T8 low-priority caller → task None → strategy still uses the live pass (class-5 unchanged).

`tests/unit/test_location_union.py`:
- T9 nit-1 fail-safe: a poison row (e.g. `track_title=None`) → `[]`, no raise.

`tests/unit/test_compilation_track_location_read.py` (existing home of `TestGetCompilationTrackLocations`):
- T10 probe timeout: hanging `fetchall` → `[]` within the bound (patch `entity.compilation_track_location._PROBE_TIMEOUT_S` small).
- T11 malformed row shape → `[]`.

Bulk (nit 2): add T12 only if no existing test pins the unconditional bulk low-priority set.

## 6. Budgets, checks, docs

- `tests/unit/test_module_budgets.py`: `lookup/location_union.py` is at 249/250 and `lookup/orchestrator.py` at 1497/1500 — recalibrate both deliberately (smallest multiple of 50 ≥ new measured size, dated comment, per the file's own formula). `track_on_compilation.py` has headroom (897/1150).
- Local CI parity before push: `ruff check` + `ruff format --check`, `uv run --no-sync mypy <touched files> --ignore-missing-imports`, full `uv run --no-sync pytest tests/unit`, marker-sync unaffected (no new markers).
- Docs: `docs/architecture.md` strategy table + lookup-flow prose (union-active bypass), `docs/env-vars.md` kill-switch entry, `config/settings.py` docstring.

## 7. Validation constraints from the issue

- **Recall:** T5/T6 pin the live-repro shapes with the index as the primary source.
- **Latency:** the live pass's removal is the improvement lever; a meaningful before/after measurement requires a populated index, which is #1027 step 1 — the measurement lands with #1027's live validation (step 6). Staging behavior post-merge with the still-empty shared index: Case-B interactive queries run the degraded pass (keyword + album fallback + rowless only) — deliberate, kill-switch-revertible, and the reason #1027's population step should follow promptly.
- **Data safety:** no schema changes, no writes; the probe/read path only. A recall-index miss or probe error degrades to no locations (T4/T9/T10/T11), never a `/lookup` failure.

## 8. Out of scope

- Populating the index and `main`→`prod` promotion (LML#1027).
- The deferred identity matcher (LML#1020/#1021).
- `SONG_AS_TRACK`'s live path (song-only queries have no `parsed.artist`, so the probe returns `[]` by contract; its strategy is untouched).
