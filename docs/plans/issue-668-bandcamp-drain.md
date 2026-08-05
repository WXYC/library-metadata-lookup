# Plan — #668: run the offline Phase-2 Bandcamp drain to publish ~7.9K backlog URLs

## Principle

LML owns and produces `streaming_availability.db`. It does **not** own `library.db` — that file is rebuilt daily from authoritative Kattare MySQL by discogs-etl's `sync-library.yml`, which enriches it with streaming links read from the **`streaming-data-v1` GitHub Release** copy of `streaming_availability.db`. So the drain's job is: add Bandcamp URLs to `streaming_availability.db`, publish that file to the release (and the volume backups), and let the existing daily pipeline regenerate and publish `library.db`. Do not hand-build and upload `library.db`.

This plan executes the [#668](https://github.com/WXYC/library-metadata-lookup/issues/668) runbook, correcting two operational inaccuracies and one data-safety gap found while verifying it against the code on `origin/main`.

## What the issue gets right (verified)

- **Prereqs merged.** #665 (`fix(#125)` slug-scoped lookup, `cde7a98`) and #667 (`feat(#661)` resumability marker, `ed3712a`) are both merged to `main`.
- **Resumability is real.** `phase_lookup` ([scripts/bandcamp_pipeline.py](https://github.com/WXYC/library-metadata-lookup/blob/main/scripts/bandcamp_pipeline.py)) leaves a slug `pending` when `fetch_artist_catalog` returns `None` (transient: network / timeout / non-200 / 429-exhausted), and only marks `not_found` on a *successful* fetch with no title match or a genuinely empty catalog. `get_pending_bandcamp_lookup` ([scripts/streaming_availability/results_db.py:392-412](https://github.com/WXYC/library-metadata-lookup/blob/main/scripts/streaming_availability/results_db.py)) filters `bandcamp_status = 'pending'`, so re-runs skip attempted slugs and retry only the fetch-failed remainder. The `_migrate` backfill marks existing `bandcamp_url`-having rows as `found` (idempotent).
- **Reported split exists.** The log line the runbook says to inspect is emitted verbatim: `"Lookup complete: N album matches, M marked not-found, K fetch-failed (left pending), from P catalogs"`.
- **Admin endpoints + scripts exist.** `POST/GET /admin/{upload,download}-streaming-db`, `POST /admin/upload-library-db` ([routers/admin.py](https://github.com/WXYC/library-metadata-lookup/blob/main/routers/admin.py)), and `scripts/export_streaming_links.py` are all present and documented in [docs/deployment.md](https://github.com/WXYC/library-metadata-lookup/blob/main/docs/deployment.md).
- **Drain is non-destructive.** `update_bandcamp_url` only fires for `bandcamp_url IS NULL` pending rows; it never overwrites a resolved URL.

## Corrections to the issue's runbook

1. **Run from a `main` checkout — not the default working tree.** The default LML working tree sits on `prod` (currently `af2e673`, 19 commits behind `origin/main`) and has **no `bandcamp_status` column anywhere in it** — #667 only landed on `main`. Running the drain from the default tree would execute the pre-#667 pipeline with no resumability marker, exactly the un-resumable multi-hour drain #667 was built to prevent. → Execute from a fresh worktree off `origin/main`.

2. **Drop `--include-streaming` from the lookup command — it's a no-op.** The `--phase lookup` dispatch calls `phase_lookup(client, db, artist_fallback=..., limit=...)` and never threads `include_streaming` through; that flag only changes which *artists* `phase_search` discovers slugs for. The correct command is `python -m scripts.bandcamp_pipeline --phase lookup`. Likewise the issue's "prioritize the ~2,151 not-on-Spotify subset" is moot: `get_pending_bandcamp_lookup` has no `spotify_status` filter, and since Bandcamp outranks Spotify in the streaming-link priority order, the full drain (every pending slug-known row) is the correct target anyway.

3. **Publish via the release + the daily sync, not a hand-built `library.db` (the data-safety gap).** The issue's step 4–5 (run `export_streaming_links.py` locally, then re-upload `library.db`) is unsafe: `export_streaming_links.py` does `DROP TABLE IF EXISTS streaming_links; CREATE TABLE ...` *inside* `library.db`, so it must run against a `library.db` whose **catalog** is current. The current catalog lives only in Kattare MySQL and is rebuilt daily by `sync-library.sh`; uploading a `library.db` built from a stale local copy would roll back the catalog sync. Instead, the drained `streaming_availability.db` must become the **`streaming-data-v1` release asset**, and `library.db` production is left to `sync-library.yml` (fresh MySQL pull + enrichment + upload to prod/staging).

## The actual data flow (verified, load-bearing)

```
refresh-streaming.yml  (LML, weekly Sun 00:00 UTC + dispatch)
   download release  ->  streaming pipeline (incremental, retry-errors; bandcamp cols preserved)  ->  gh release upload streaming_availability.db --clobber
                                                                                                          |
streaming-data-v1 RELEASE  <-- canonical integration copy of streaming_availability.db ------------------+
   |                                                                                                       \
   | gh release download (streaming_availability.db + library.db)                                          (also Railway volume backup,
   v                                                                                                         maintained manually via
sync-library.yml  (discogs-etl, daily 12:00 UTC + dispatch)                                                 POST /admin/upload-streaming-db;
   fresh MySQL -> library.db  ->  export_streaming_links.py(library.db, release's streaming_availability.db)  served by GET /admin/download-streaming-db)
                                 ->  POST /admin/upload-library-db  (staging + production volumes)
                                 ->  gh release upload library.db --clobber
```

Observed release asset state confirms the cadence: `streaming_availability.db` updated 2026-06-21 02:50Z (weekly refresh, Sun), `library.db` updated 2026-06-21 14:23Z (daily sync, after 12:00 cron).

## Steps

### 0. Set up — and a hard gate that **every** later step runs here

**All of steps 1–6 run from a worktree on `origin/main`, never the default `prod`-based tree.** The default tree (`af2e673`) predates #667 and has no `bandcamp_status` column: its `get_pending_bandcamp_lookup` has no status filter (defeats resumability — re-runs re-process already-attempted slugs) and its `phase_lookup` emits the old single-count `"Lookup complete: N album matches from P catalogs"` log without the not-found / fetch-failed split that step 6 needs. Running the drain from there is the exact un-resumable failure #667 prevents.

- `git worktree add .worktrees/issue-668 origin/main` (or use an existing clean main worktree); `cd` into it for all subsequent steps.
- **Verification gate (fail closed):** confirm `git rev-parse HEAD` matches `origin/main`, and that the marker code is present:
  - `grep -n "bandcamp_status\|bandcamp_checked_at" scripts/streaming_availability/results_db.py` returns the column definitions, the index, the backfill, and the `bandcamp_status = 'pending'` filter in `get_pending_bandcamp_lookup`.
  - `grep -n "fetch-failed (left pending)" scripts/bandcamp_pipeline.py` returns the modern `Lookup complete: … M marked not-found, K fetch-failed (left pending), from P catalogs` log line; the pre-#667 tree omits the breakdown and emits only `… from P catalogs`, so an empty grep here means the wrong tree.
  - If either grep is empty, you are on the wrong tree — stop and re-checkout `origin/main` before going further.
- `uv sync --extra dev` in the worktree. The drain needs network only (no Spotify creds — it uses `BandcampClient`, 1 req/s, semaphore 2).
- Have `ADMIN_TOKEN`, prod URL, and staging URL available (for the download/upload endpoints), and `gh` authed with release-write scope on LML.

### 1. Acquire the freshest `streaming_availability.db` and establish a baseline

- Download **both** candidate copies and compare — they are maintained by different processes and may diverge:
  - release asset: `gh release download streaming-data-v1 --repo WXYC/library-metadata-lookup --pattern streaming_availability.db --output sa.release.db`
  - volume copy: `GET /admin/download-streaming-db` → `sa.volume.db`
- For each, record `get_stats()` bandcamp breakdown plus: row count, `count(bandcamp_url NOT NULL)`, `count(bandcamp_status='pending' AND bandcamp_slug IS NOT NULL AND bandcamp_url IS NULL)` (the true backlog), and `max(bandcamp_checked_at)` / max spotify/apple checked timestamps.
- **Drain the fresher/more-complete copy.** Expectation: the release asset (read by the daily sync) is canonical and at least as fresh. If the volume copy is materially fresher (newer Spotify/Apple results), note the divergence — it means an `/admin/upload-streaming-db` happened after the last weekly refresh — and reconcile by draining the volume copy, then publishing to both. Capture the chosen baseline's backlog number; the issue's ~7,878 / ~2,151 came from a stale 2026-04-23 local snapshot and will differ.
- **Document any divergence before draining.** Treat the two copies as divergent if **any** of: the volume copy's `max(bandcamp_checked_at)` (or max spotify/apple `checked_at`) is >12h ahead of the release asset's; total `bandcamp_url` coverage differs by >5%; or the `bandcamp_status='pending'` backlog differs by >10%. On divergence, write down which copy is fresher and *why* (likely an out-of-band `/admin/upload-streaming-db`) and which you chose, so the publish step (4) re-converges both stores deliberately rather than silently picking a loser. A large unexplained gap is a signal to stop and reconcile, not to drain blindly.

### 2. Run the drain

```
python -m scripts.bandcamp_pipeline --phase lookup     # against the chosen streaming_availability.db
```

- Multi-hour at ~1 req/s; safe to stop/restart. Run under `nohup`/`tmux` with logging.
- Capture the final `"Lookup complete: N album matches, M marked not-found, K fetch-failed (left pending), from P catalogs"` line.
- **Wrong-tree guard:** if the final line instead reads `"Lookup complete: N album matches from P catalogs"` (no not-found / fetch-failed split), you ran the pre-#667 pipeline from the wrong tree — discard the run, re-verify step 0's gate, and re-run from the `origin/main` worktree.

### 3. Drain the transient remainder

- Re-run step 2 to retry the `K` fetch-failed (still-`pending`) slugs. Repeat until `K` stabilizes (a persistent residual is expected — slugs that 404/error every time stay `pending` by design; do **not** force-mark them).
- Confirm via `get_stats` / direct SQL: `slug_known AND url_missing AND bandcamp_status='pending'` ≈ 0 modulo the persistent residual.

### 4. Publish `streaming_availability.db`

- **Primary (drives library.db enrichment):** `gh release upload streaming-data-v1 streaming_availability.db --clobber --repo WXYC/library-metadata-lookup`.
  - **Timing guard:** the weekly `refresh-streaming.yml` runs Sun 00:00 UTC (Sat 19:00–20:30 US Eastern in EDT) and clobbers the release asset; a concurrent refresh reads the *old* asset and would re-clobber, dropping the drain. Before uploading, check it isn't mid-run: `gh run list --workflow refresh-streaming.yml --repo WXYC/library-metadata-lookup --limit 1 --json status,startedAt` — if `in_progress`, wait for completion. `refresh-streaming` is otherwise additive (it preserves bandcamp columns), so off-window there's no conflict.
- **Secondary (keep volume backup in parity):** `POST /admin/upload-streaming-db` to **production and staging**, so the backup the download endpoint serves matches the release.

### 5. Produce and publish `library.db` via the daily pipeline (do not hand-build)

- Manually dispatch discogs-etl `sync-library.yml` (`workflow_dispatch`) so `library.db` is rebuilt from fresh MySQL + the just-published `streaming_availability.db` and uploaded to prod + staging (and the release) — rather than waiting up to ~24h for the 12:00 UTC cron.
- Watch the run to success.

### 6. Verify (acceptance criteria)

- [ ] Drain ran against the **current** `streaming_availability.db`; afterward `slug_known AND url_missing AND bandcamp_status='pending'` ≈ 0 (modulo persistent fetch-failures).
- [ ] Recorded attempted vs newly-resolved (and fetch-failed) counts from the run logs.
- [ ] `streaming_availability.db` re-published to the `streaming-data-v1` release and re-uploaded to prod + staging volumes.
- [ ] `sync-library.yml` dispatch succeeded; spot-check `library.db.streaming_links.bandcamp_url` count rose by ≈ the newly-resolved figure.
- [ ] Hit prod `/lookup` for 2–3 drained albums and confirm `bandcamp_url` surfaces via the priority-1 override (no read-path change). Use Stereolab / Cat Power / Juana Molina style fixtures from the drained set.

## Risks & mitigations

- **Stale-catalog clobber of `library.db`** — mitigated by never hand-uploading `library.db`; only `sync-library.yml` (fresh MySQL) produces it.
- **Release/volume divergence** — mitigated by step 1's compare-both and publishing to both in step 4.
- **Concurrent `refresh-streaming` clobber** — mitigated by the step-4 timing guard.
- **Drain interrupted mid-run** — safe by design (#667); resume by re-running step 2.
- **`/lookup` hot-path** — untouched. This is an offline pipeline + file publish; the #651 cold-start regression cannot recur.

## Out of scope

- Adding a `spotify_status` filter to `get_pending_bandcamp_lookup` (the not-on-Spotify prioritization) — unnecessary; the full drain is the correct target and Bandcamp outranks Spotify regardless.
- Migrating `sync-library.yml` to read `streaming_availability.db` from the Railway volume instead of the release (the deployment.md "volume is canonical" intent is not yet reflected in the workflow). File separately if desired.
- Any change to the `/lookup` read path.

## Suggested follow-up (optional, not blocking)

- Edit the #668 body to reflect corrections 1–3 (run from `main`; drop `--include-streaming`; publish via release + `sync-library.yml`, not a hand-built `library.db`), so the next operator inherits the corrected runbook.
