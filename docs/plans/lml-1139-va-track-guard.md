# LML#1139 — V/A↔V/A Apple track matches must clear the album axis

Issue: https://github.com/WXYC/library-metadata-lookup/issues/1139
Branch: `lml-1139-va-track-guard` (worktree of the same name, off `origin/main` @ 83190be)
Downstream: WXYC/Backend-Service#2000 (blocked-by this; must run only after guard **and** purge deploy)

> **Revision 3** — incorporates two rounds of plan review. Two corrections to the issue body itself are recorded in "Deviations from the issue body" below; both were verified empirically, not argued. Round 3 adds the LML#904 probe-throttle interaction (which materially changes the purge's rollout), fixes the integration-test tier, and switches the purge to asyncpg.

## Problem (established by the issue; not re-litigated here)

`Various Artists - ` is a constant ~19-char prefix, so any two WXYC V/A credits score ~85 on the artist axis via `token_set_ratio` — over the 80 floor, carrying zero identifying information. All 21 measured marginal V/A clears in the trailing 30d are on the **Apple Music track path** (`_select_best_track_candidate`), not `find_best_match`/`find_best_typed_match`. Title scores on the false positives are **95–100** (generic blues/latin standards whose titles genuinely agree), so no title-axis rule and no floor move separates them — the FP artist-score band (83.87–85.71) is entirely *contained* inside the TP band (80.0–89.55). LML#638's no-change decision on the floor is confirmed, not revisited.

The dominant surface is the LML#782 album-dropped fallback: the album-constrained pass rejected every candidate, then the fallback re-admitted a winner on vacuous-artist + generic-title.

## Desired end state

On the Apple track path, a V/A↔V/A pair is acceptable **only when the album-constrained pass supplied the winner**. The #782 fallback pass and album-less-from-the-start track queries reject V/A↔V/A candidates. Album path (all 5 services), `find_best_typed_match`, `title_subset_is_degenerate`, `album_subset_is_degenerate`, and `SCORE_MATCH_ACCEPTANCE_FLOOR` are untouched.

## Deviations from the issue body (both verified, not assumed)

**1. The guard keys on the NORMALIZED artist, not the raw string.** The issue instructs "Feed it **raw** strings, not `normalize_for_comparison` output". That rationale does not hold: `to_match_form` (= `normalize_for_comparison`) is NFKC + lowercase + combining-strip + folds + ASCII-space-collapse (`wxyc-etl/src/text/forms.rs:72-80`) — it **does not strip punctuation**, so the `v/a` and `v.a` prefixes survive intact. Measured across the full plan matrix:

| input | `to_match_form` | raw | normalized |
|---|---|---|---|
| `Various Artists - Blues` | `various artists - blues` | True | True |
| `V/A` / `V.A.` | `v/a` / `v.a.` | True | True |
| `Various Production` | `various production` | False | **False** |
| `The Various` | `the various` | False | **False** |
| `CAGAYANO VARIOUS ARTISTS` | `cagayano various artists` | False | **False** |
| `Ice-T` / `a-ha` | `ice-t` / `a-ha` | False | **False** |
| `  Various Artists` | `various artists` | **False** | True |
| `Various  Artists - Blues` | `various artists - blues` | **False** | True |
| `Vàrious Artists` | `various artists` | **False** | True |

Normalizing preserves **every** positive and **every** negative and additionally catches three real variants raw misses (leading whitespace, doubled space, diacritics). Decisively: `norm_artist` in `_select_best_track_candidate` is `to_match_form(row_artist)`, which is **literally the L1 cache key** (`entity/track_streaming_url_cache.py` keys on `to_match_form(artist)`). Keying the guard on the same string makes "the purged set == the guarded set" a provable identity rather than an aspiration; keying on raw would purge rows the guard never strikes, and they would immediately re-cache the same wrong URL. This also removes the issue's proposed `raw_artist` param and both call-site edits.

**2. The purge's service literal is `apple_music_track`, not `apple_music`.** The issue's purge SQL says `WHERE service = 'apple_music'`. That value **cannot exist** in the table: `streaming/service.py:124-126` sets `TRACK_CACHE_KEYS[StreamingService.APPLE_MUSIC] = "apple_music_track"`, and `entity/track_streaming_url_cache.py:58` pins it behind a named CHECK constraint. As written the DELETE would match zero rows while the dry-run reported a clean zero — the ordering hazard the issue itself flags (stale wrong deep-links serving forever, BS#2000 reading them back) would silently materialize. The script imports `APPLE_MUSIC_TRACK_SERVICE` from `entity.track_streaming_url_cache` rather than writing any literal (the LML#1037 anti-drift pattern).

## Mechanism

### 1. Predicate — `clients/streaming/matching.py`

Beside `title_subset_is_degenerate` / `album_subset_is_degenerate`:

```python
def va_artist_axis_is_uninformative(query_artist: str, candidate_artist: str) -> bool:
    """True when both sides are V/A credits, so the artist score is carried
    entirely by the shared 'Various Artists' prefix and identifies nothing."""
    return is_compilation_artist(query_artist or "") and is_compilation_artist(candidate_artist or "")
```

- Imports `is_compilation_artist` from `wxyc_etl.text` — the org's canonical V/A detector, already imported at `lookup/matching.py:13` and `identity/router.py:217`. The module already imports `to_match_form as normalize_for_comparison` from `wxyc_etl.text`, so the import is additive.
- Callers pass **`to_match_form` output on both sides** (see Deviation 1). The docstring states this contract explicitly, since the leading-anchored rule is normalization-sensitive.
- `or ""` on both sides: `is_compilation_artist` raises on `None` (pinned by `tests/unit/test_matching.py:155`).

### 2. Guard — `clients/streaming/apple_music.py`

**No new parameter.** `_select_best_track_candidate` already has `norm_artist` in scope, and already computes the candidate's normalized artist inline for `artist_score`. Hoist that into a local and reuse it:

```python
cand_artist_norm = normalize_for_comparison(attrs.get("artistName") or "")
artist_score = fuzz.token_set_ratio(norm_artist, cand_artist_norm)
```

Then, in the candidate loop immediately after the LML#719 title guard and before the `if norm_album is not None:` block:

```python
if norm_album is None and va_artist_axis_is_uninformative(norm_artist, cand_artist_norm):
    continue
```

- `norm_album is None` covers both the #782 fallback pass and album-less-from-the-start queries — the whole rule in one clause.
- **Per-candidate**, not a winner veto and not query-side fallback suppression: only V/A↔V/A candidates are struck; a non-V/A candidate stays eligible on its own merits. This matters because the artist axis is deliberately `token_set_ratio` for leader→ensemble subsets.
- Placement after the floor checks is load-bearing: an empty candidate `artistName` already fails the artist floor and `continue`s before this line, so the guard never sees the degenerate-empty case.
- Zero extra normalization cost (the hoist reuses a value the loop already computed).

### 3. No feature flag

Ships unconditional, matching the LML#719 precedent named in the issue.

### 3b. Make the album-less arm measurable (new in rev 3)

Review raised that the guard's blast radius is wider than the evidence base: the 21 measured clears are dominated by the #782 fallback, but the rule also strikes **album-less-from-the-start** track queries, and `clients/streaming/apple_music.py:540` records that "~40% of request-o-matic traffic is artist+song-only". That population's V/A slice is currently **unmeasurable** — `record_match_telemetry` emits `surface="track"` both when the album-constrained pass won *and* when the query was album-less from the start, so the two are indistinguishable in the 30d sample.

The issue makes the scope decision explicitly ("the LML#782 fallback pass **and album-less track queries** reject V/A↔V/A candidates", plus AC4 pinning `Jombang Jet` as a deliberate kill), so this plan does **not** re-litigate it. But it does fix the observability gap that made the concern unanswerable: emit `surface="track_album_less"` when `norm_album is None` on the first (non-fallback) pass, leaving `"track"` to mean strictly "album-constrained pass won" and `"track_album_fallback"` unchanged.

One-line change at the existing `record_match_telemetry` call; no new dependency, no cardinality explosion (three values instead of two). It converts the residual into a series that can be watched from the moment of deploy, and gives a later decision-to-narrow real numbers to work from. Listed as a post-deploy watch item below.

### 4. Cache purge — `scripts/purge_va_apple_track_cache.py`

`lml_cache.track_streaming_url_cache` (LML#893 L1) is **hit-only and TTL-less**, peeked in `lookup/enrichment/item.py:278` *before* the live probe. Pre-guard wrong V/A deep-links would otherwise serve forever on exactly the repeat-play traffic that recurs most, and the guard would never re-adjudicate them.

**Structural donor: `scripts/audit_va_writeback_pollution.py`** — the same coarse-net + pure-arbiter idiom already exists in this repo against `entity.identity` (`SUPERSET_REGEX` line 31, pure `classify()` line 53, lazy `psycopg` import at line 97 specifically so the arbiter stays unit-testable without a DB driver). This script reuses its regex shape and its `classify()` split.

1. `SELECT DISTINCT artist_normalized, album_normalized, song_normalized, url` for `service = APPLE_MUSIC_TRACK_SERVICE` matching the **unanchored superset** net. Unanchored on purpose: it must be a genuine superset, so real-artist strings like `the various` and `cagayano various artists` *do* reach the arbiter and are *rejected by it* — which is what makes the arbiter load-bearing rather than decorative.
2. Filter through `is_compilation_artist` — the exact predicate the guard uses, applied to the exact string the guard sees (Deviation 1). The coarse net only bounds the scan; this is the true arbiter.
3. Write every to-be-purged row to a recovery file (below), **then** `DELETE ... WHERE service = $1 AND artist_normalized = ANY($2)` — parameterized on both the derived service constant and the arbitrated key list; no interpolation.

**The net regex is widened from the donor's (rev 4).** The donor's `SUPERSET_REGEX` (`scripts/audit_va_writeback_pollution.py:31`) is `(various|soundtrack|compilation|v/a|v\.a\.)`. Its `v\.a\.` alternative requires a **trailing dot**, but `is_compilation_artist` accepts the dotless form — so reusing it verbatim would *not* be a superset, and the "purged set == guarded set" identity that Deviation 1 rests on would be false. Measured against the installed crate:

| `artist_normalized` | arbiter | donor net | widened net |
|---|---|---|---|
| `v.a` | True | **False** | True |
| `v.a - jazz` | True | **False** | True |
| `v.a 1998` | True | **False** | True |
| `v.a.` / `v/a` / `various artists - blues` | True | True | True |
| `various production` / `the various` | False | True | True |

Those three key shapes are guard-struck but would never be purged — wrong pre-guard deep-links serving forever from a hit-only, TTL-less table: the same silent form as Deviation 2. The net therefore uses `(various|soundtrack|compilation|v[./]\s*a\.?)`, and the test matrix adds `v.a` / `v.a - jazz` as **regex-and-arbiter positives** (rev 3's matrix only asserted that the *negatives* reach the net, which would not have caught this).

**Driver: asyncpg, not psycopg.** The donor is psycopg (sync, `%s` placeholders), but every `lml_cache` module and the large majority of `scripts/` use asyncpg, and the `$n` placeholders above are asyncpg's. What this script borrows from the donor is the *idiom* — a widened `SUPERSET_REGEX` plus a pure `classify()` split, which alone is what buys the arbiter's unit-testability. (Rev 3 also cited the donor's lazy driver import; that rationale does **not** transfer — `psycopg` is optional there, whereas `asyncpg` is a hard dependency at `pyproject.toml:24` and `entity/sources.py` imports it at module scope, so there is nothing to defer.) Connection comes from `PgSource(dsn=get_settings().database_url_discogs)` (env `DATABASE_URL_DISCOGS`), and the script opens with the shared `scripts/_lib/runtime.set_up_script_runtime` preamble (`load_dotenv` + logging + shutdown flag).

**Recovery artifact (rev 4).** Before any DELETE — on the dry-run path *and* the `--execute` path — write every purged row's full tuple (`service, artist_normalized, album_normalized, song_normalized, url`) to a CSV, mirroring the donor's `DELETE_CSV` / `KEEP_CSV`. This matters more here than in the donor: the purge knowingly over-deletes correct album-cleared V/A links, and the probe-throttle section below establishes that those rows mostly **re-null rather than re-fill**. The deleted URLs are therefore expensive-to-replace API-derived data, not cheap cache — org data-safety rules treat that as something you do not destroy without a way back. The file makes the over-deletion reversible, lets the staged waves be reconciled after the fact, and gives a human a way to inspect what a wave actually removed. Path via **`--out`** (default `/tmp/lml-1139-purged-<timestamp>.csv`, the donor's `/tmp` convention but per-wave rather than a fixed name that later waves would clobber); the path is named in the `docs/scripts.md` entry and recorded on the PR alongside the counts.

**Wave mechanism (rev 5).** The rollout calls for a staged purge, so the script needs an actual lever for it: **`--limit N`** over a stable `ORDER BY artist_normalized`, so each invocation takes the next N distinct keys deterministically and a re-run resumes where the last stopped (already-purged keys no longer match the SELECT, so the ordering alone is a sufficient cursor — the table is the state). Rev 4 prescribed waves while specifying a single `SELECT DISTINCT` + one `DELETE`, i.e. an all-or-nothing run; that gap is closed here. Wave behavior is pinned in the unit tests (`--limit` bounds the key set; ordering is stable; a second wave picks up disjoint keys).

Flag naming: the donor uses `--apply`; `seed_library_release_overrides.py` uses `--execute`. This script takes **`--execute`** to match the downstream BS#2000 job and the more recent convention; the divergence is deliberate and noted in the module docstring so the next reader doesn't read it as drift.

Other notes:
- **Dry-run by default.** Dry-run reports the distinct-key count, the row count those keys cover, and a sample of kept-vs-deleted keys.
- Scoped to the derived track-service constant only — the sole service in `TRACK_CACHE_KEYS` today, but explicit so a future Spotify track deep-link isn't collaterally purged.
- Accepted over-deletion (module docstring): correct album-cleared V/A deep-links are purged too. Deleting is safe *for correctness* because the table is hit-only — a missing row is a miss, never a wrong answer. The **cost** of that over-deletion is larger than the issue assumed; see the next section.

#### The purge's real cost: the LML#904 probe-throttle interaction (new in rev 3)

The issue (and this plan's rev 2) assumed purged keys "re-fill via the album-constrained pass on next play (~4.8 s warm probe each)". `docs/env-vars.md:56` (LML#904) contradicts the benign reading: at the default `LML_APPLE_MUSIC_RATE_PER_MIN=60` (= 1 req/s), **~56% of `find_track_url` probes time out at the 4 s `LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS` ceiling and return null**, with **zero 429s** — the wait is LML's own acquire-time, not Apple latency (the raw HTTP GET is ~338 ms). Because the L1 table is `url NOT NULL`, a nulled probe caches nothing, so a purged key does not re-fill once — it **re-probes and mostly re-nulls on every play** until it happens to win the throttle.

Consequences, both recorded as rollout risks rather than discovered during the run:
- A blanket purge converts a bounded set of *wrong* cached links into an unbounded stream of *missing* links plus sustained probe load. Still the fail-safe direction (missing beats wrong), but it is not free and it is not self-healing at the default rate.
- **This is why the purge and BS#2000's re-verify must be paired with a rate-limit roll-up**, not run at the default. Per the LML#904 guidance in the same doc: step `LML_APPLE_MUSIC_RATE_PER_MIN` 60 → 300 → 600 (1 → 5 → 10 req/s), staying under the ~15 req/s `_SEMAPHORE_LIMIT` ceiling, watching the 429 count + null rate after each step. The knob is read once at client construction, so setting the Railway var restarts the service — no redeploy, and rollback is resetting the var.

Rejected alternative: narrowing the purge to `album_normalized = ''` (the album-less-query writes, which are unambiguously guard-struck). Those are provably wrong under the new guard, but they are the *minority* surface — the measured FPs are dominated by `track_album_fallback`, whose cache rows carry a **non-empty** `album_normalized` (the query had an album; only the scoring dropped it). Narrowing that way would leave the dominant pollution in cache, which defeats the purge. So: full purge, staged, behind the rate-limit bump.

## Files

| File | Change |
|---|---|
| `clients/streaming/matching.py` | + `va_artist_axis_is_uninformative`; + `is_compilation_artist` import; **+ `track_album_less` in the `surface:` arg docstring at `:570`** (currently enumerates `"album", "track", "track_album_fallback"`) |
| `clients/streaming/apple_music.py` | hoist `cand_artist_norm`; guard in loop; `surface="track_album_less"` split; docstring |
| `scripts/purge_va_apple_track_cache.py` | new one-shot purge (asyncpg) |
| `docs/architecture.md` | + "Artist-axis V/A guard (LML#1139)" beside the LML#719 section at line 203; **+ fix the `matcher.surface` enumeration at `:196`** (`"album" \| "track"` — already stale, missing `track_album_fallback`; add both it and `track_album_less` while in the section) |
| `docs/env-vars.md` | **one-line cross-reference** to the purge from the `LML_APPLE_MUSIC_RATE_PER_MIN` entry — not a restatement. That entry already carries the 60 → 300 → 600 roll-up, the 429/null-rate watch, and the don't-bump-during-a-backfill caveat |
| `docs/scripts.md` | + `## Track-Cache V/A Purge (scripts/purge_va_apple_track_cache.py)` — not because every script is documented there (many, incl. the donor, are not), but because this one **mutates prod and carries an ordering hazard**. Follows the `seed_library_release_overrides.py` entry's form (`docs/scripts.md:232`) incl. the `uv run python -m …` invocation |
| `docs/plans/lml-1139-va-track-guard.md` | this plan, committed (CI `scripts/check_plan_links.sh` fails on citations to untracked plans) |
| `tests/unit/test_streaming_matching.py` | predicate unit matrix |
| `tests/unit/test_apple_music_client.py` | + `TestFindTrackMetadataVaArtistAxis`; **+ update `test_whitespace_album_emits_plain_track_surface` (`:1302`)** — it asserts `surface == "track"` for `album="   "`, which §3b relabels `track_album_less`. Its docstring *argues for* the old label, so it needs rewriting, not just re-asserting; the new label in fact serves that test's own intent (not mislabeling a never-constrained request) more precisely. Update it **before** the §3b change lands |
| `tests/unit/test_purge_va_apple_track_cache.py` | new — arbiter, superset-property, and wave-shape tests |

## Test plan (TDD — failing tests first, per `docs/testing.md`)

`tests/unit/test_streaming_matching.py` — direct predicate matrix, **normalized inputs** (the contract):
- both-sides V/A → True: (`various artists - blues`, `various artists - document records`), (`various artists - latin`, `various artists - azzurra music`), (`various artists - asia`, `various artists & pan ron`), (`v/a`, `various`), (`soundtracks`, `compilation`).
- one-sided → False, both orders.
- trailing-credit negative (the documented residual): (`various artists - xmas`, `cagayano various artists`) → False — leading-anchored.
- real-artist negatives: `various production`, `the various`, `the soundtrack of our lives`.
- empty/None-ish: `("", "various")` → False; `(None, None)` must not raise (the `or ""` guard).
- **Normalization-contract pins**: `to_match_form` of `  Various Artists`, `Various  Artists - Blues`, `Vàrious Artists` all → True, documenting why callers must pass normalized strings.

`tests/unit/test_apple_music_client.py`, new `TestFindTrackMetadataVaArtistAxis` (follows the `TestFindTrackMetadataTitleSubsetInflation` idiom — `_make_song_data` / `_songs_response` / mocked `httpx`):
1. **AC1 regression** — (`Various Artists - Blues`, `I'm On My Way`, album=`<catalog album>`) vs a `Various Artists - Document Records` candidate whose album fails the constrained pass and would previously win the #782 fallback → `find_track_metadata` returns `None`.
2. **AC2** — `Various Artists - Latin` / `Various Artists - Azzurra Music` / `Aguas de Março`, same shape → `None`.
3. **AC3** — a V/A↔V/A candidate that *clears* the album-constrained pass still resolves, keeps its URL, and stays `album_verified=True`.
4. **AC4** — album-less V/A↔V/A: (`Various Artists - Asia`, `Jombang Jet`, album=`None`) → (`Various Artists & Pan Ron`, title 100) returns `None`, with an inline comment documenting the accepted recall cost (LML#719 idiom — a deliberate kill of an arguably-correct match).
5. **AC5 no-regression corpus** — labeled true positives still match: `Ice-T`→`Ice T` (80.0), `Aha`→`a-ha` (85.71), `Altin Gun`→`Altın Gün` (88.89), `N.E.R.D.*`→`N.E.R.D` (87.5). Kept as cheap pins with the honest caveat that they are **non-V/A on both sides, so the predicate short-circuits False before scores matter** — they cannot regress from this change and prove only that the guard didn't leak. The case that actually exercises the boundary is (6b).
6. **Per-candidate, not winner-veto** — a response holding both a V/A↔V/A candidate and a legitimate non-V/A candidate on an album-less query still returns the non-V/A winner.
6b. **The real boundary** — a **V/A query** against a **non-V/A candidate** on an album-less probe must still resolve (one side V/A ⇒ predicate False ⇒ the artist axis still carries real information). This is the assertion that fails if the predicate is ever loosened from AND to OR.
7. **Constrained pass unaffected** — a V/A row whose album clears is returned by the constrained pass even when the response also holds a struck fallback-only candidate.
8. `SCORE_MATCH_ACCEPTANCE_FLOOR == 80.0` unchanged (AC).
9. **Telemetry split** — an album-less-from-the-start win records `surface="track_album_less"`; an album-constrained win still records `"track"`; a #782 fallback win still records `"track_album_fallback"`.

`tests/unit/test_purge_va_apple_track_cache.py`:
- Arbiter keeps `various artists - blues`, `v/a`, `v.a.`, `soundtracks`, `various`, `compilation`; drops `various production`, `the various`, `cagayano various artists`, `the soundtrack of our lives`. Every one of those negatives **does** match the superset regex — asserted explicitly — so the test proves the arbiter is what excludes them.
- **Superset property, both directions** (the rev-4 regression): `v.a`, `v.a - jazz`, `v.a 1998` are asserted as **arbiter-positive AND net-positive**. Rev 3's matrix asserted only that negatives reach the net, which is why it did not catch the donor regex's dotless-`v.a` gap.
- The DELETE targets `APPLE_MUSIC_TRACK_SERVICE` (imported, not a literal) and is parameterized on the key array.
- Dry-run path performs no DELETE but **does** write the recovery CSV.
- The recovery CSV is written before the DELETE, not after (pin the ordering — a crash mid-DELETE must still leave the artifact).

**Integration tier — deviation from the Bug Fix Protocol, stated rather than silently skipped.** `docs/testing.md:21-27` asks for a unit test *and* an integration test "against real APIs". No such test is added here, for two verified reasons:

1. **CI cannot run it.** `docs/testing.md:50` defines `external_api` as "needs a real third-party API key (**Discogs**)" and the job provisions only `DISCOGS_TOKEN`. Apple Music needs an ES256 developer token assembled from `apple_music_team_id` / `apple_music_key_id` / `apple_music_private_key` (`config/settings.py:50-56`, signed at `clients/streaming/apple_music.py:201-226`), which CI does not hold. An `external_api`-marked Apple test would never execute; every Apple-touching file under `tests/integration/` already mocks the client.
2. **The unit tests already are the composition test.** `TestFindTrackMetadataVaArtistAxis` mocks only `httpx` — it drives the real `find_track_metadata` → `_select_best_track_candidate` → `matching.py` → real `wxyc_etl.is_compilation_artist` path end to end. A mocked-`httpx` file under `tests/integration/` would be a near-duplicate requiring a shared-fixture refactor (`es256_keypair` lives at `tests/unit/conftest.py:224`; the song builders are module-local) for zero additional coverage.

What the protocol is actually protecting — "false positives excluded AND correct results retained" — is covered: the two measured FP pairs are pinned as rejections, the album-cleared V/A pair and the one-sided `CAGAYANO` pair are pinned as retained, and the labeled true-positive corpus is pinned unaffected. The purge script's live-DB behavior (wave cursoring, `ANY($2)` array bind, the `DELETE ... RETURNING` shape and its transaction) is the part genuinely not unit-testable; it is covered by `tests/integration/test_purge_va_apple_track_cache_pg.py` (`@pytest.mark.pg`) rather than by an Apple-hitting one.

## Verification & rollout

1. `ruff format` + `ruff check` + full unit suite green locally before push (pre-commit hook runs the first two on staged `.py`).
2. PR → CI green → merge to `main` (staging) → verify → promote to `prod`.
3. **Raise the Apple probe ceiling first, and confirm it actually took.** Step `LML_APPLE_MUSIC_RATE_PER_MIN` 60 → 300 via the sanctioned `.github/workflows/set-railway-var.yml` (it waits for the auto-redeploy and health-probes, `:143-147`) — **not** a bare var-set: `docs/env-vars.md:65` records the failure mode where a var-set returns `SKIPPED` and leaves the running process untouched, which would silently land the purge's churn on the 1 req/s probe this step exists to avoid. Confirm the new rate is live before proceeding to step 4. Then watch the 429 count + null rate, then → 600 if clean. Without this the purge's churn lands on a 1 req/s probe where ~56% of calls null out (LML#904). Not during any active BS#1631-style backfill. **The Apple token is shared** with LML staging and the `docs/scripts.md` resolver scripts, so the safe ceiling is the aggregate across all consumers — and the purge's own re-probe churn is a second consumer on that same bucket, on top of live `/lookup` traffic.
4. **Same deploy as the guard**, following the repo's prod-write contract for one-shots (`docs/scripts.md:236` — "**Prod writes require explicit authorization and run staging-first**"): get explicit authorization, run `--execute` on **staging** first and confirm the recovery CSV + counts look right, then prod dry-run → prod `--execute`, **staged in waves** rather than one shot so the re-probe load arrives gradually. Counts and the recovery-file path recorded on the PR (AC).
5. Post-deploy telemetry (AC, absence-vs-baseline — the LML#719 verification): the `matcher.match` marginal-clear query keeps firing and the V/A class disappears from the marginal band. Baseline: ~0.9% of matches marginal (22 vs 2,414 in 24h), ~17.5% FP rate by occurrence in-band; expect ~6–7% after, band size unchanged. **Query with `surface IN ('track','track_album_less','track_album_fallback')`, not `surface = 'track'`** — §3b makes `"track"` a strictly smaller population than it was when the baseline was measured, so a naive before/after on that filter would conflate the guard's effect with the relabel. The new value is for the §6 volume watch only.
6. **Watch the new `track_album_less` surface** (§3b) for the album-less V/A volume the 30d sample could not separate. If it is large, that is the input to a follow-up decision about narrowing the rule — it does not block this ship.
7. Only then unblock BS#2000's run, handing it the deploy SHA. Keep the raised rate limit in place through BS#2000's re-verify pass (that job depends on it for a different reason — a throttle-null there is indistinguishable from a guard-strike and would destroy correct URLs), then restore.

## Risks

- **Ordering**: guard deployed but cache not purged ⇒ wrong URLs keep serving from L1 and BS#2000's re-verify reads them straight back. Purge is part of the same deploy; BS#2000's README gates on both. Deviation 2 is precisely this risk's silent form.
- **Purge churn under the default probe throttle** (rev 3, LML#904): purged keys mostly re-null rather than re-fill until the rate ceiling is raised; nulls are never cached, so the churn repeats per play. Mitigated by step 3 + staging; the direction stays fail-safe (missing beats wrong).
- **Over-rejection**: an album-less V/A↔V/A query that was genuinely right now returns `None` (AC4's `Jombang Jet`). Accepted and pinned as a deliberate kill; the failure mode is a missing Apple link, never a wrong one.
- **Album-less arm blast radius is wider than the evidence base.** Raised in review and left as the issue scoped it, but recorded honestly: the 21 measured clears are dominated by the #782 fallback, while the rule also strikes album-less-from-the-start queries (~40% of request-o-matic traffic is artist+song-only overall; its V/A slice was unmeasurable pre-§3b). §3b makes it measurable from day one; §6 above is the watch item.
- **Residuals out of scope** (per the issue): album-axis-fooled V/A pairs (2 occurrences), one-sided/trailing V/A credits (`CAGAYANO`, n=1), generational suffix (`Hank Williams, Jr.`), surname-only collisions, lineup supersets.
