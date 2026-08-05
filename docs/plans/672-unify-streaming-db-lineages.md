# Plan: Unify the two `streaming_availability.db` lineages (LML#672)

## Goal

Make the Railway **volume** copy of `streaming_availability.db` the single authoritative lineage feeding production `library.db`, so Apple Music + track-level data reach prod automatically and the release/volume can't silently diverge. This implements **option 2** (volume canonical), the recommended option in #672.

Scope boundary (from the issue): this unifies **propagation** (whatever the rich copy holds reaches prod with no manual merge), not Apple **acquisition cadence**. Automatic Apple *refresh* is explicitly deferred.

## Background (current state, verified)

- `refresh-streaming.yml` (LML): weekly CI. `gh release download streaming-data-v1 --dir . || echo "..."` (enriches from empty on a flaked download), runs Spotify/Deezer incremental (CI has only Spotify creds — Apple gate at `scripts/streaming_availability/__main__.py:842-851` logs-and-skips), then `gh release upload streaming-data-v1 --clobber`. Never runs `track_streaming`.
- `routers/admin.py`:
  - `upload_streaming_db` (`POST /admin/upload-streaming-db`): full-file replace; validates only "SQLite + `albums` table + rowcount" before `os.replace`. **No coverage check.**
  - `download_streaming_db` (`GET /admin/download-streaming-db`): FileResponse of the volume copy, 404 if missing. Already exists (#238).
  - `_get_streaming_ids`/`_compute_streaming_diff`: read the `streaming_links` table from the **library.db** upload path — they do **not** apply to streaming_availability.db.
- `scripts/export_streaming_links.py`: enriches library.db `streaming_links` from **both** `albums.apple_url` *and* the `track_results` table.
- `streaming_availability.db` schema (`scripts/streaming_availability/results_db.py`): `albums` has `apple_url`, `spotify_url`, `deezer_url` (always present, base DDL); `track_results` table may or may not be present.
- discogs-etl `.github/workflows/sync-library.yml`: `gh release download streaming-data-v1 --repo WXYC/library-metadata-lookup` (hard-fails on download failure). Already has `ADMIN_TOKEN` + `PRODUCTION_URL` in the run-sync step env.
- discogs-etl `scripts/sync-library.sh:148-163`: treats a missing `streaming_availability.db` as **optional** — continues and uploads a zero-streaming-link library.db.

## Defense-in-depth: three layers (the spine of the change)

| Layer | Where | Change |
|---|---|---|
| 1. Generation | LML `refresh-streaming.yml` | Round-trip the volume (download→enrich→upload); **drop** the `\|\| echo` empty-db fallback so the fetch hard-fails. |
| 2. Publication | LML `routers/admin.py` `upload_streaming_db` | **Coverage-regression guard** comparing the upload against the on-disk file at replace time. |
| 3. Consumption | discogs-etl `sync-library.yml` + `sync-library.sh` | Read `GET /admin/download-streaming-db` with hard-fail (HTTP status + non-empty-SQLite); post-enrichment `apple_music_url` floor assertion. |

---

## Work item A — LML: coverage-regression guard (Layer 2)  [TDD]

File: `routers/admin.py`, function `upload_streaming_db`.

### Behaviour
1. After writing the upload to `tmp_path` and validating it (SQLite + `albums` + rowcount), compute **coverage metrics** for both `tmp_path` (new) and `db_path` (current on-disk, the live file at replace time):
   - `apple_url` = `COUNT(apple_url)` over `albums`
   - `spotify_url` = `COUNT(spotify_url)` over `albums`
   - `deezer_url` = `COUNT(deezer_url)` over `albums`
   - `albums` = `COUNT(*)` over `albums`
   - `track_results` = `COUNT(*)` over `track_results` (0 if the table is absent)
2. **Reject** (HTTP 409) if, for any metric, the new value:
   - drops below `prior × (1 − TOLERANCE)` (TOLERANCE = `0.05`), **or**
   - goes non-zero → zero (the minimum rule that catches the 288→0 incident).
3. **Override**: `force: bool = False` query param (`?force=true`). When set, the guard is skipped and a **loud `logger.warning`** records every regressed metric + old/new values.
4. **First upload** (no prior file on disk, or prior has 0 albums) → no prior to regress against → allow.
5. Reject response includes the offending metrics (old, new, floor) so CI logs are actionable; the tmp file is `unlink`ed on rejection (no partial state).

### Tolerance constant
`STREAMING_COVERAGE_TOLERANCE = 0.05` as a **module-level constant** in `routers/admin.py` (not a setting — there's no operational reason to vary it per-deploy, and a constant keeps the call site simple). `_check_streaming_regression` takes `tolerance` as a **parameter** (defaulting to the constant at the call site), so unit tests can drive it across multiple tolerance values without monkeypatching.

### New helpers
`_streaming_coverage(db_path: Path) -> dict[str, int]` — **always returns all five keys** (`apple_url`, `spotify_url`, `deezer_url`, `albums`, `track_results`), each mapping to its count, with **`0` for a missing file, missing `albums` table, or missing `track_results` table**. Fixed-key contract (never omits a key) so `_check_streaming_regression` can iterate without `KeyError`. Mirrors the defensive style of `_get_streaming_ids` (try/except, log-and-degrade).

`_check_streaming_regression(old: dict, new: dict, tolerance: float) -> list[dict]` — iterates the five fixed metric keys; for each returns a record `{metric, old, new, floor}` when `new < floor` (`floor = old × (1 − tolerance)`) **or** `old > 0 and new == 0`. Empty list = OK. Pure function, trivially unit-testable across tolerance values. Because `_streaming_coverage` guarantees all five keys, a `track_results` table that disappears reads as `N → 0` and is caught by the non-zero→zero rule.

### Tests (write first, `tests/unit/test_admin_router.py` or new `test_admin_streaming_guard.py`)
- regression below tolerance on each guarded metric → 409, file unchanged on disk.
- non-zero→zero on `apple_url` → 409 (the incident).
- within-tolerance shrink (e.g. −3%) → 200.
- growth on all metrics → 200.
- `force=true` over a regressing upload → 200 + warning logged; on-disk file replaced.
- first upload (no prior on disk) → 200.
- prior present **with** `track_results`, upload **missing** the `track_results` table → no `KeyError`; coverage reads `track_results: 0`; non-zero→zero rule fires → 409.
- pure-function unit tests for `_check_streaming_regression`: parameterized over tolerance (e.g. 0.05 and 0.10) crossing the floor boundary; the non-zero→zero rule independent of tolerance; old==0 (first-upload) never regresses regardless of new.

---

## Work item B — LML: `refresh-streaming.yml` round-trips the volume (Layer 1)

Transitional (dual-write) form for the cutover:
1. **Download step**: replace `gh release download ... || echo "..."` with a `curl GET $PRODUCTION_URL/admin/download-streaming-db` that **hard-fails** — assert HTTP 200 *and* the result is a non-empty SQLite file with an `albums` table. **No `|| echo` fallback.** This curl hard-fail snippet is **net-new** (today both repos use `gh release download`); **Work item B is its first implementation and Work item D mirrors it byte-for-byte** so it's reviewed once. Still `gh release download` the **library.db** artifact (needed as the enrichment input; the release stays the home of `library.db`).
2. Spotify/Deezer incremental pipeline (unchanged).
3. **Upload step (volume)**: `curl POST $PRODUCTION_URL/admin/upload-streaming-db` with the enriched db (passes through the Layer-2 guard).
4. **Dual-write (transitional)**: also keep `gh release upload streaming-data-v1 streaming_availability.db --clobber` so the release stays warm until discogs-etl has cut over and one sync cycle is verified. Removed in Work item E.
5. Secrets: add `ADMIN_TOKEN` + `PRODUCTION_URL` (already org secrets used by discogs-etl) to this workflow's env.

Note: `refresh-streaming.yml` is a workflow file — not unit-testable in-repo. Validate by `actionlint` + careful review. The hard-fail curl logic is the load-bearing part; keep it identical to the discogs-etl snippet so it's reviewed once.

---

## Work item C — LML: `docs/deployment.md`

**Current state correction (from review):** `docs/deployment.md:62-71` *already* asserts "making the volume the canonical source" — but that is **aspirational**: the download endpoint exists (#238), yet nothing reads it (sync-library.yml still reads the release) and nothing enforces it (no guard, refresh writes the release). So C does **not** re-state "volume is canonical" — it replaces the aspirational claim with the now-*enforced* reality and adds the new mechanics:
- Reframe "making the volume the canonical source" → describe the **enforced** single lineage: `refresh-streaming.yml` writes the volume, `sync-library.yml` reads the volume, both pass through the upload chokepoint. (Transitional: note the release is still dual-written until Work item E.)
- Document the **coverage-regression guard** on `POST /admin/upload-streaming-db` (the five metrics, 5% tolerance, non-zero→zero rule, on-disk comparison, `force` override).
- Document the **Railway-uptime failure mode**: the daily prod sync now depends on LML/Railway being up at sync time; a download failure hard-aborts the sync and prod keeps **yesterday's** `library.db` (which still has links) — the correct failure mode.
- Update L41's description of `refresh-streaming.yml` (it now round-trips the volume; release write dropped post-cutover in Work item E).

---

## Work item D — discogs-etl: consumption hard-fail (Layer 3)  [shell, no unit harness]

Files: `.github/workflows/sync-library.yml`, `scripts/sync-library.sh`.

1. **`sync-library.yml`** — replace the `gh release download streaming-data-v1 ... --dir /tmp/library-metadata-lookup` line in the "Set up streaming links enrichment" step with a `curl GET $PRODUCTION_URL/admin/download-streaming-db` that:
   - sends `Authorization: Bearer $ADMIN_TOKEN`;
   - captures HTTP status with `-w '%{http_code}' -o file`; **fail the step** if status ≠ 200 (a naive `curl -o` exits 0 on 404/500 and writes the error body — this is the regression trap the issue calls out);
   - asserts the downloaded file is a **non-empty SQLite** file with a populated `albums` table (`sqlite3 file "SELECT count(*) FROM albums"` > 0);
   - keeps `git clone` of LML for `export_streaming_links.py`.
   - Add `ADMIN_TOKEN` + `PRODUCTION_URL` to **this step's** `env` (they currently live only on the run-sync step).
2. **`sync-library.sh`** — after the enrichment block (`:148-163`), add a **post-enrichment floor assertion**: query `SELECT COUNT(apple_music_url) FROM streaming_links` in `$DB_PATH`; if it's below a floor (`STREAMING_APPLE_FLOOR`, default e.g. `100`), `notify_error` + `exit 1` **before** the upload, so a thin db never reaches prod. Keep the existing "missing streaming db → skip" branch as belt-and-suspenders, but the floor assertion makes a zero-apple library.db a hard failure.

   **Relationship to the LML guard (independent, complementary):** discogs-etl's `STREAMING_APPLE_FLOOR` is an **absolute** floor at the *consumption* layer — it catches a thin `library.db` (zero/low apple links) just before it would overwrite prod, regardless of *why* it's thin (download flake, export bug). LML's `STREAMING_COVERAGE_TOLERANCE` is a **relative** regression guard at the *publication* layer — it catches a writer trying to shrink the volume. They use different knobs by design (absolute vs relative, different repos, different failure they prevent); they are not synced and should not be. Both are needed for genuine defense-in-depth.

(No automated tests in discogs-etl for shell workflow steps; validate with `actionlint` + `bash -n` + manual trace. Mirror LML's hard-fail snippet exactly.)

---

## Work item E — LML: drop the release write (final cutover)  [separate PR, gated on verification]

After discogs-etl is reading the volume **and** one real sync cycle has been verified to yield a non-zero `apple_music_url` count in prod `library.db`:
- Remove the `gh release upload streaming_availability.db` dual-write from `refresh-streaming.yml`.
- Retire the `streaming_availability.db` release asset (keep the release for `library.db`).
- Finalize the `docs/deployment.md` wording (drop "transitional / dual-write").

This step **cannot be merged until the live verification passes** — it depends on a prod sync cycle I can't run. It ships as a clearly-marked follow-up PR, not merged with A–D.

---

## Rollout / PR sequencing (prod never loses links mid-cutover)

1. **PR-1 (LML)** — Work items A + B (dual-write) + C. Lands first: the guard must exist before any upload, and the volume must be written before discogs-etl reads it.
2. **PR-2 (discogs-etl)** — Work item D. Lands second; now reads the volume (which PR-1 keeps current) while the release is still dual-written as a safety net.
3. **Verify** one sync cycle → non-zero `apple_music_url` in prod `library.db`.
4. **PR-3 (LML)** — Work item E. Drops the release write; retires the asset.

Chain PR-1 → PR-2 → PR-3 in that order. PR-1 and PR-2 are independently safe to merge (PR-1 dual-writes; PR-2's reads hard-fail safely → prod keeps yesterday's links).

## Acceptance criteria mapping (#672)

- Apple non-zero & current without manual merge → A (guard preserves Apple) + B (volume kept current weekly) + D (apple reaches prod) + E (single source).
- Release/volume can't silently diverge → B + D + E (single writer/reader on the volume).
- Upload guard rejects regressions on the 5 metrics vs on-disk, with override → A.
- `refresh-streaming.yml` no longer enriches from empty on flaked fetch → B.
- `sync-library.yml` hard-fails fetch + asserts apple floor → D.
- `docs/deployment.md` updated (single lineage + Railway-uptime failure mode) → C.
- No regression in weekly refresh wallclock → B (download + Spotify/Deezer incremental + upload is cheaper than today's release create; volume transfer is negligible).

## Concurrency note (one file, two writers)

Closed by: (a) every writer round-trips the volume (download→modify→upload) so each upload is a superset on the axes it touched and carries the rest forward — incl. the occasional manual Apple/`track_streaming` leg; (b) the Layer-2 guard compares against **live on-disk state at replace time**, so a stale Apple-less writer is rejected on the Apple regression and re-applied next cycle. Guard does double duty: regression protection + serialization, no permanent loss.

## Out of scope (file separately if wanted)

- Automatic Apple **refresh** in CI (option 1's deferred cost; needs Apple key in CI + `track_streaming` wallclock).
- Running `track_streaming` weekly in CI.
