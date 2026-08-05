# LML#802 — `search_releases_by_track`: push the artist predicate into the `matching_tracks` CTE so the `LIMIT` doesn't prune the artist's own release

Issue: https://github.com/WXYC/library-metadata-lookup/issues/802 (parent WXYC/library-metadata-lookup#801). Root cause verified against deployed code at `1ce136b` — `discogs/cache_service.py:388-416`.

## Problem recap (verified against code)

`DiscogsCacheService.search_releases_by_track` (`discogs/cache_service.py:357-446`) runs a single query whose `matching_tracks` CTE selects releases whose **track title** trigram-matches `$1`, orders by `sim DESC`, and truncates with `LIMIT $2` (`$2 = limit*2`, i.e. 40 at the default `limit=20`). The artist argument `$3` is referenced **only in the outer `WHERE`**, after the CTE has already truncated (`rows = await self.pool.fetch(query, track, limit * 2, artist)`, `cache_service.py:416`).

For a **common track title** (e.g. "Milkman"), many releases carry an exact match and all tie at `sim = 1.0`; `ORDER BY sim DESC LIMIT 40` then keeps a **non-deterministic** 40. If the queried artist's release is not in that arbitrary set, it is pruned *before* the `ra.artist_name % $3 OR rta.artist_name % $3` filter runs, and the query returns zero rows for that artist even though the release is in the cache.

Confirmed call path for the parent #801: `lookup/strategies/track_on_compilation.py:303` calls `search_releases_by_track(song_search, artist_for_probes)` with `artist_for_probes = parsed.artist` ("Aphex Twin"); the service wrapper `DiscogsService.search_releases_by_track` (`discogs/service.py:602`, PG-skip rationale in its docstring at `:611-616`) issues the PG read for exactly this `artist_as_keyword=False` probe (it skips PG only for the `artist_as_keyword=True` V/A probe). So the artist-scoped cache read is the path that should surface *Richard D. James Album* and does not — this fix targets it directly.

## Scope boundary (what this does NOT fix)

- **Not #237's mechanism.** #237 (CLOSED/COMPLETED) is the same "release never enters the candidate list" family but its cause is trigram (`%`) vs `token_set_ratio` matching for reordered trio names — the artist predicate itself doesn't fire. This plan keeps the `%` trigram artist predicate; it only moves *where* that predicate runs relative to the `LIMIT`. #802 is **necessary-but-not-sufficient** for the broader class #801 tracks and will not subsume #237.
- **Not a ranking/merge change.** Whether the surfaced release becomes the lookup's *answer* is downstream (`validate_track_on_release` + orchestrator merge). This plan makes the cache **retrieve** it; the end-to-end #801 assertion is a staging replay, not this PR's merge gate (see AC#4).

## Constraints (from the ticket + repo)

- No schema change; read-only query; cache-only method (no API leg added here).
- Keep query rationale in **Python comments, not `--` SQL comments** (existing note at `cache_service.py:384-386` — keeps it out of `pg_stat_statements` / PG logs).
- Preserve the `ra.extra = 0` / `rta.extra = 0` main-artist-credit semantics and the documented #333 recall trade-off (the existing docstring at `cache_service.py:375-386` stays).
- Preserve both legs of the artist predicate: `ra` (release-level credit) **and** `rta` (track-level featured credit, correlated to the matched track's `sequence`).
- **The `artist=None` path (VA / SONG_AS_TRACK) must execute the identical query it does today** — it is the hot path and is under active perf scrutiny (#706 cold-tail, the Post-launch service-hardening project). "Unchanged behavior" is not enough; the None path must keep the same query *structure* (same parse tree) so its `pg_stat_statements` entry and generic plan are untouched. (`pg_stat_statements` keys on a normalization of the post-parse tree, not raw text — indentation/whitespace are irrelevant; what must not change is the shape of the query the None path runs. See "Why two strings" below.)
- TDD required (`docs/testing.md`): failing `pg`-marked integration test first, then the fix. Bug-fix protocol: the RED test reproduces the prune.
- This is a **bug fix**, so it is in scope despite the "stop adding features in these files" hardening-project freeze — but the perf-sensitive surface means the artist-branch plan must be measured (EXPLAIN), not assumed.

## Design — single PR, branch `802-search-by-track-artist-prefilter` off `origin/main`

**Worktree first (before any code).** The default tree is on `prod` (`1ce136b`, lags `origin/main` — MEMORY `prod-tree-lags-main`) and is dirty (`plans/`, `docs/repo-graph.*`, `.worktrees/`, etc. uncommitted). Per global CLAUDE.md, create a worktree off `origin/main` in the repo's existing `.worktrees/` convention before writing code, so this fix can't collide with that uncommitted state: `git fetch origin && git worktree add .worktrees/802-search-by-track-artist-prefilter -b 802-search-by-track-artist-prefilter origin/main`. Run the fresh-worktree pytest setup from MEMORY `project-worktree-pytest-setup` (`uv sync --extra dev`, then `uv run --no-sync python -m pytest`).

### Core change: split into two query strings, gated on `artist is None`

Replace the single `query` string in `search_releases_by_track` with **two** module- or method-level SQL constants, selected by whether an artist was supplied. This is the mechanism that isolates the hot path:

**`_SEARCH_BY_TRACK_SQL` (artist absent) — today's query, structurally unchanged.** Same query as today's inline `query` string (including the now-always-`NULL` `$3` outer-WHERE leg and its three bind params). Hoisting the string to a module constant will de-indent it — that is fine: `pg_stat_statements` normalizes away whitespace, so the entry and generic plan are preserved as long as the *structure* is unchanged. Called as today: `self.pool.fetch(_SEARCH_BY_TRACK_SQL, track, limit * 2, artist)` with `artist is None`.

**`_SEARCH_BY_TRACK_ARTIST_SQL` (artist supplied) — artist predicate pushed into the CTE `WHERE`, before the `LIMIT`.** The artist columns live on `release_artist` / `release_track_artist`, not `release_track`, so the predicate enters the CTE as two correlated `EXISTS` subqueries (EXISTS, not JOIN — it cannot multiply `release_track` rows, so the existing `SELECT DISTINCT` stays clean and the `LIMIT` counts distinct candidate tracks):

```
WITH matching_tracks AS (
    SELECT DISTINCT rt.release_id, rt.sequence,
           rt.title as track_title,
           similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) as sim
    FROM release_track rt
    WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
      AND (
          EXISTS (SELECT 1 FROM release_artist ra
                  WHERE ra.release_id = rt.release_id AND ra.extra = 0
                    AND lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3)))
          OR
          EXISTS (SELECT 1 FROM release_track_artist rta
                  WHERE rta.release_id = rt.release_id
                    AND rta.track_sequence = rt.sequence AND rta.extra = 0
                    AND lower(f_unaccent(rta.artist_name)) % lower(f_unaccent($3)))
      )
    ORDER BY sim DESC
    LIMIT $2
)
SELECT ... FROM matching_tracks mt
JOIN release r ON r.id = mt.release_id
JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
LEFT JOIN release_track_artist rta
    ON rta.release_id = mt.release_id AND rta.track_sequence = mt.sequence AND rta.extra = 0
WHERE ($3::text IS NULL
       OR lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3))
       OR lower(f_unaccent(rta.artist_name)) % lower(f_unaccent($3)))
ORDER BY mt.sim DESC
```

- The outer `SELECT ... WHERE (...)` is **kept unchanged** from today's query. For the artist branch it is now redundant with the CTE (every surviving release already satisfies it), but keeping it (a) minimizes the diff, (b) preserves the existing display-artist / `is_compilation` selection behavior for multi-credit releases, and (c) is a pure backstop — it can only *remove* rows the CTE kept, never admit a wrong one, so it cannot introduce a precision regression.
- Dispatch in Python: `sql = _SEARCH_BY_TRACK_SQL if artist is None else _SEARCH_BY_TRACK_ARTIST_SQL`, then `self.pool.fetch(sql, track, limit * 2, artist)`. Everything below `rows = ...` (the `seen_albums` dedup, `limit` cutoff, `ReleaseInfo` build, `CacheUnavailableError` wrap) is untouched.
- A Python comment above the two constants explains: why two strings (hot-path `pg_stat_statements`/plan isolation), why `EXISTS` not `JOIN` (no row multiplication vs the `DISTINCT`/`LIMIT`), and that the `rta` leg is correlated to `rt.sequence` so a featured-credit release still matches on its matching track. No `--` comments in the SQL.

### Why two strings and not one guarded CTE (`$3 IS NULL OR EXISTS(...)`)

A single guarded string would change the query *structure* the `artist=None` path executes (new parse tree → new `pg_stat_statements` entry) and, worse, force the planner to build one generic plan covering both branches — it cannot constant-fold an unknown `$3` bind at plan time, so the common VA/SONG_AS_TRACK path could inherit a worse plan than today's. Two strings keep the hot path's structure and plan identical and confine all risk to the artist-supplied branch. This planner argument — not raw-text identity — is the load-bearing justification for the split.

### Performance verification (required before merge, not optional)

The artist branch now evaluates the artist `EXISTS` predicate over the **full** track-title match set before the `LIMIT`, rather than over a bounded 40. For a common title that set can be large. This is inherent to answering correctly (you cannot filter-after-limit and stay correct), and it is the *organic artist+track* branch, not the hot VA path — but it must be measured:

- Capture `EXPLAIN (ANALYZE, BUFFERS)` for `_SEARCH_BY_TRACK_ARTIST_SQL` on a common-title case against the prod discogs-cache (read-only; creds via Railway per MEMORY `reference-lml-railway-access` / the EC2 recipe), and confirm which index each leg uses.
- Verify a trigram (GIN/GiST `gin_trgm_ops`) index exists on `release_track.title`, `release_artist.artist_name`, and `release_track_artist.artist_name`. If the artist-name indexes exist, the planner may lead with the (far more selective) artist predicate and the branch can be *faster* than today; if they don't, record the cost and whether an index belongs in discogs-etl (schema is discogs-cache-owned — file there, do not `CREATE INDEX` from LML).
- Record the numbers on the ticket. If the common-title case regresses unacceptably and no index closes it, fall back to the `LML#543`-style bounded interim discussion on-ticket before merging — do not ship an unmeasured tail.

## Test matrix (TDD — write failing-first, `pg`-marked integration)

New file `tests/integration/test_search_by_track_artist_prefilter.py`, `pytestmark = pytest.mark.pg`, self-seeding fixture modeled on `tests/integration/test_search_releases_credits.py`: `CREATE EXTENSION pg_trgm/unaccent`, `F_UNACCENT_WRAPPER_SQL`, and `skip_if_drop_targets_populated(conn, ("release", "release_track", "release_artist", "release_track_artist"))` as the data-safety guard, then DROP/CREATE the four tables (`release(id,title)`, `release_track(release_id,sequence,title)`, `release_artist(release_id,artist_name,extra)`, `release_track_artist(release_id,track_sequence,artist_name,extra)`) and seed. Teardown drops them. (`search_releases_by_track` is skipped in `test_song_as_track.py` when the real fixture lacks `release_track`; self-creating the tables avoids that dependency.)

**Determinism note (this is the crux of the RED test):** seed **N = 42** distractor releases (> `limit*2 = 40`) by *other* artists, each with a track titled **exactly** `"Common Track"` → every distractor scores `sim = 1.0`. Seed the target release by `"Target Artist"` with a track titled `"Common Track (Reprise)"` → `0.3 < sim < 1.0`, so it deterministically sorts **below all 42** and is reliably excluded by `LIMIT 40` on current code, regardless of tie ordering. (If the target shared the exact title, all 43 would tie at 1.0 and the prune would be non-deterministic → flaky RED. The fuzzier target title is what makes the reproduction deterministic.)

**Pin the trigram floor, don't inherit it.** Unlike `_artist_trigram_candidates` (`cache_service.py:600`), `search_releases_by_track` runs on a raw pooled connection and never `SET LOCAL`s `pg_trgm.similarity_threshold`, so the `%` operator uses the server default (0.3). The RED test's `0.3 < sim < 1.0` claim for `"Common Track (Reprise)"` is hostage to that default. The fixture must make the boundary explicit rather than rely on it: either `SET pg_trgm.similarity_threshold = 0.3` on the connection, or assert the seeded `similarity()` value directly (as `test_search_releases_credits.py` pins its 0.786 decoy) so the test fails loudly if a server default or a future title tweak moves the target's `sim` out of range.

| # | Test | Acceptance criterion | Expectation |
|---|---|---|---|
| 1 | 42 distractor releases (other artists) with track `"Common Track"` + 1 `"Target Artist"` release with track `"Common Track (Reprise)"`; `search_releases_by_track("Common Track", "Target Artist", 20)` | AC#1 (release-level `ra`) | **RED** on current code (returns `[]`); **GREEN** after (returns the Target Artist release) |
| 2 | Same seed; `search_releases_by_track("Common Track", None, 20)` returns the top-by-`sim` distractors exactly as today (assert the returned set + `sim DESC` order for a small deterministic seed) | AC#2 (VA/None no-regression) | Passes before **and** after (path unchanged) |
| 3 | Snapshot change-detector unit test: assert the `artist=None` branch's SQL constant equals a committed snapshot, whitespace-normalized (not byte-for-byte — the hoist de-indents it) | AC#2 (hot-path structure frozen) | Guards against accidental *semantic* edits to `_SEARCH_BY_TRACK_SQL`; lives in `tests/unit/test_cache_service.py`, no marker |
| 3b | Structural unit test (bug-fix-protocol analog): call `search_releases_by_track("Song", "Artist")` against a capture-only mock pool; assert the emitted SQL pushes the artist predicate **into the CTE** — an `EXISTS ... artist_name % $3` appears **before** `LIMIT` | AC#1 (fix mechanism, unit level) | The unit-level reproduction the prune bug can't express against a mock (no SQL runs); RED if the predicate stays outer-only |
| 4 | Featured-credit shape: `release_artist` = `"Various"` (extra=0), `release_track_artist` = `"Featured Artist"` (extra=0) on the matched track's sequence, + 42 distractors sharing the track title; `search_releases_by_track("Common Track", "Featured Artist", 20)` | AC#3 (`rta` track-level leg) | **RED** on current code; **GREEN** after (surfaces via the `rta` EXISTS leg) |
| 5 | `rta` leg respects `extra = 1` exclusion: same as #4 but the featured credit is `extra = 1` → not returned (mirrors the #333 trade-off; documents that this fix does not change `extra` semantics) | precision guard | Passes before and after (no new recall from guest/remixer credits) |
| 6 | #801 concrete pin: seed *Richard D. James Album* (id 87-analog) with track `"Milkman"` + 42 other releases each with a `"Milkman"`-ish track; `search_releases_by_track("Milkman", "Aphex Twin", 20)` returns RDJ Album | AC#4 (cache-retrieval half of #801) | **RED** on current code; **GREEN** after |

**Bug-fix-protocol note.** `docs/testing.md` step 1 wants a `tests/unit/` reproduction with mocked data. The prune bug is a SQL-execution artifact — it cannot be reproduced against a mock pool (no SQL runs), so the behavioral reproductions are the `pg` tests #1/#4/#6. Test #3b is the required unit-level analog (it pins the fix *mechanism* — predicate-in-CTE — structurally). Call out this deviation explicitly in the PR description so the protocol gap is intentional, not an oversight.

**Existing-test regression check.** `tests/unit/test_cache_service.py` already inspects this method's SQL: `test_query_includes_track_artist_join` (`:156`) and `test_query_filters_rta_to_extra_zero` (`:167`) call `search_releases_by_track("Song", "Artist")` and assert on captured SQL substrings. After the split those capture `_SEARCH_BY_TRACK_ARTIST_SQL`; they should still pass (the artist SQL keeps `release_track_artist`, `rta.artist_name`, `rta.extra = 0`), but this is a load-bearing assumption — run the full `TestSearchReleasesByTrack` suite and confirm both hold before relying on the "everything below `rows = ...` is untouched" framing.

**AC#4 end-to-end** (`/lookup` returns *Richard D. James Album* for `Milkman Aphex Twin`) is an orchestrator + healthy-cache assertion, not a unit gate. Verify by staging replay post-deploy (`main` auto-deploys to staging) against the real cache, and record on #801. Test #6 above is the LML-level proof that the cache method now retrieves it; the merge gate is tests #1-#6, not the live `/lookup`.

Run locally per MEMORY `project-pg-integration-local`: throwaway homebrew `postgresql@16` on 5433 with a `discogs`/`discogs` superuser + `discogs` db; `uv run --no-sync python -m pytest -m pg tests/integration/test_search_by_track_artist_prefilter.py -v`.

## Acceptance-criteria mapping (ticket ⇄ tests)

- AC#1 (artist's own release returned despite N > `limit*2` same-title others) → test #1.
- AC#2 (`artist=None` unchanged) → test #2 (behavior) + test #3 (frozen SQL text).
- AC#3 (featured-credit `rta.extra = 0` path still works) → test #4, bounded by test #5.
- AC#4 (#801 Milkman → RDJ Album against a healthy cache) → test #6 (cache retrieval) + staging `/lookup` replay recorded on #801.

## Risks & mitigations

- **Hot-path (`artist=None`) regression** — the whole point of the two-string split; frozen by test #3 (golden string) + test #2 (behavior). Zero SQL-text change ⇒ zero plan/`pg_stat_statements` change.
- **Artist-branch latency for common titles** — full-set predicate evaluation before `LIMIT`. Mitigation: mandatory `EXPLAIN (ANALYZE, BUFFERS)` on prod cache + trigram-index confirmation before merge; index gap (if any) filed to discogs-etl, not patched from LML. Branch is organic artist+track, not the VA hot path.
- **Multi-credit display-artist selection** — the kept outer `WHERE`/JOIN preserve today's `artist_name` + `is_compilation` selection for releases with several `extra = 0` credits; no change vs current behavior, and the outer WHERE can only subtract, never add a wrong row.
- **Over-claiming #801 closure** — framed explicitly as necessary-not-sufficient; end-to-end left to staging replay; #237's distinct mechanism called out so a future recurrence isn't mis-triaged as a #802 regression.
- **PR size** — one method + one test file, well under the 1000-line preference; single PR, no stacking.

## Ops / rollout

0. Create the worktree off `origin/main` (per "Worktree first" above) before writing code.
1. Pre-merge: EXPLAIN + index check on prod discogs-cache (read-only), recorded on #802.
2. Local `pg` suite green (tests #1-#6) + full `ruff check`/`ruff format --check` + unit suite before push (per global CI-locally rule).
3. Merge to `main` → staging auto-deploy → replay `Milkman Aphex Twin` (and one featured-credit case) against staging `/lookup`; record release IDs on #801/#802.
4. Prod is a `prod`-branch push — coordinate, do not push unilaterally; watch the #706 wait-time / p95 histograms for the artist branch post-deploy.
