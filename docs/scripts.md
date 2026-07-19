# Scripts

## Streaming Report Stats Regenerator (`scripts/regenerate_report_stats.py`)

Refreshes the data-driven values in `streaming_analysis_report.md`. The report contains `<!-- gen:KEY -->VALUE<!-- /gen -->` markers, each bound to a query in `QUERIES`. The script runs every query, replaces each marker's value, and errors loudly on any unknown or unresolved marker.

**When to run:** After any change to `streaming_availability.db` (pipeline runs, validation passes, etc.) or after adding a new method to the report. Re-run before committing to keep the doc honest.

**Workflow for a new method:** (1) write the prose for the new Method block in the report by hand, (2) if it produces a number worth tracking in a headline table, register a `Query` in `QUERIES` and reference it via a marker, (3) run the regenerator. The script will fail if any marker has no registered query, or any query has no marker — preventing silent drift between the registry and the doc.

**Usage:**
```bash
TUBAFRENZY_DB_PASSWORD=... uv run python -m scripts.regenerate_report_stats [--dry-run]
```

Markers backed by tubafrenzy MySQL are skipped (with a warning) if `TUBAFRENZY_DB_PASSWORD` is not set; SQLite-backed markers always run.

## Discogs Cache Benchmark (`scripts/benchmark_cache.py`)

Benchmarks PG cache vs Discogs API response times for `search()`. Useful for evaluating cache effectiveness after discogs-cache ETL runs.

**Usage:**
```bash
.venv/bin/python scripts/benchmark_cache.py --iterations 3
```

Loads `DISCOGS_TOKEN` and `DATABASE_URL_DISCOGS` from `.env` or the Railway CLI linked project.

## API Model Generation (`scripts/generate_api_models.sh`)

Generates Pydantic v2 models from `wxyc-shared/api.yaml`. Uses a local sibling `wxyc-shared` directory if available, otherwise downloads from GitHub. The generated file (`generated/api_models.py`) is committed to git. Re-run after api.yaml changes.

**Usage:**
```bash
bash scripts/generate_api_models.sh
```

Requires `datamodel-code-generator` (included in dev dependencies).

## VA Disambiguation (`scripts/va_disambiguate/`)

Disambiguates "Various Artists" entries in the WXYC flowsheet and populates the `COMPILATION_TRACK_ARTIST` table with per-track artist credits from the Discogs cache.

**Usage:**
```bash
uv run python -m scripts.va_disambiguate [OPTIONS]
```

Options: `--dry-run` (extract only), `--stats` (show progress), `--apply` (execute SQL), `--confidence-threshold FLOAT` (default 0.70), `--verbose`.

Generates two SQL files for review before application: `va_flowsheet_updates.sql` (updates `ARTIST_NAME`/`ARTIST_ID` on flowsheet entries) and `va_catalog_inserts.sql` (inserts into `COMPILATION_TRACK_ARTIST`). Requires `DATABASE_URL_DISCOGS` and `TUBAFRENZY_DB_PASSWORD` environment variables.

## Bandcamp Pipeline (`scripts/bandcamp_pipeline.py`)

Unified pipeline for discovering Bandcamp artist slugs and matching them to WXYC albums. Connects two phases via `asyncio.Queue` so album matching begins as soon as slugs are discovered.

**Phase 1 (Search):** Queries Bandcamp's autocomplete API to discover artist slugs. Writes to `bandcamp_slug` column in `streaming_availability.db`.

**Phase 2 (Lookup):** For artists with known slugs, scrapes the Bandcamp catalog page and fuzzy-matches album titles using `score_match()`. Writes album-specific URLs to `bandcamp_url`.

**Phase 2 resumability (#661):** every album carries a `bandcamp_status` marker (`pending` → `found` / `not_found`) plus `bandcamp_checked_at`, mirroring `spotify_status`/`spotify_checked_at`. A match (or an `--artist-fallback` URL) marks `found`; without `--artist-fallback`, an album scraped against a real catalog with no title match — including a successful fetch of a genuinely empty artist page — is marked `not_found` rather than left at `bandcamp_url = NULL` (with `--artist-fallback` those same cases instead write the artist-level URL and mark `found`). **Transient fetch failures are not terminal:** `fetch_artist_catalog` returns `None` (distinct from an empty `[]`) on a network error / timeout / non-200 / 429-after-retries, and Phase 2 leaves those slugs `pending` so a re-run retries them — a network blip during the multi-hour drain never permanently drops a slug. `get_pending_bandcamp_lookup` only returns `pending` rows, so a re-run skips already-attempted slugs and the drain is restartable. The lookup log reports the split (`N matches, M not-found, K fetch-failed (left pending)`), and `get_stats` surfaces the `bandcamp` status breakdown. The columns are added by the idempotent `_migrate()` ALTER path in `results_db.py`, which also backfills `found` (once, when the column is first added) for rows that already had a resolved `bandcamp_url`.

**Usage:**
```bash
python -m scripts.bandcamp_pipeline [--phase {search,lookup,both}] [--include-streaming] [--artist-fallback] [--dry-run] [--limit N] [--db-path PATH]
```

Options: `--phase` (default: both, runs concurrently), `--include-streaming` (search all artists, not just not-on-streaming), `--artist-fallback` (write artist-level URL when no album match), `--dry-run` (report what would happen without changes), `--limit N` (max artists/slugs to process).

Uses `BandcampClient` (`clients/bandcamp.py`) extending `BaseStreamingClient` (`clients/streaming/base.py`) with rate limiting (1 req/s, semaphore 2) and 429 retry with exponential backoff. Optionally loads Wikidata slugs via `DATABASE_URL_WIKIDATA`.

## Resolver Calibration (`scripts/resolver_calibration/`)

Sweeps the trigram-similarity floor used by `lookup/artist_resolution.resolve_canonical_artist` (the pre-pass for `search_compilations_for_track` — see WXYC/library-metadata-lookup#318). Builds labeled datasets — positives from `artist_name_variation`, `entity.identity`, and (optionally) synthetic listener-typo corruptions; negatives from sampled close-but-distinct `artist` pairs — and writes a precision/recall sweep + a borderline-band CSV.

The synthetic-typo class targets the actual production failure mode (listener-typed names like "Robotnik" → "Robotnick"). The Discogs `artist_name_variation` class covers aliases / AKAs ("Bob Dylan" / "Robert Zimmerman") which is a different distribution. Synthetic class is opt-in via `--synthetic-positive-size N` (default 0 = disabled); set ~1000 for a typical calibration run. See `scripts/resolver_calibration/synthetic_typos.py` for the four corruption classes (drop, transpose, duplicate, ASCII-fold).

**Output** (defaults to `docs/resolver-calibration/`):
- `calibration_sweep.csv` — threshold, TP rate, FP rate, sample sizes, swap count.
- `borderline.csv` — pairs whose score sits in `[chosen_floor − 0.05, chosen_floor + 0.05]`, sorted by score, for eyeball QA of the decision boundary.

After running, update `CANONICAL_ARTIST_SIMILARITY_FLOOR` in `core/thresholds.py` if the chosen floor differs from the in-tree value, and commit the CSVs alongside a one-page `docs/resolver-calibration/README.md` documenting the FP-rate tolerance that drove the choice. The same script is used to re-validate after large discogs-cache refreshes; commit fresh CSVs each run so the calibration history stays in version control.

**Usage:**
```bash
DATABASE_URL_DISCOGS=postgresql://... \
  uv run python -m scripts.resolver_calibration \
    --output-dir docs/resolver-calibration/ \
    --positive-sample-size 5000 \
    --negative-sample-size 5000 \
    --synthetic-positive-size 1000 \
    --fp-rate-target 0.005
```

## Cache Investigation (LML#537)

Three one-shot diagnostic scripts that quantify why the Discogs cache hit ratio plateaued at ~50% (`search_releases_by_track` at 34%) after the `write_release` ON CONFLICT failure was resolved by Alembic 0009. All read-only; none modify state.

### Cache miss provenance sample (`scripts/cache_miss_provenance.py`)

For a sample of cache-miss → API-call events, classifies each by whether the `release` row already existed at miss time and whether it carried `release_track` rows. Separates first-time misses (H1 — ETL coverage gap) from release-row-exists-with-tracks misses (H3' — validate-as-hit poisoning).

**Usage:**
```bash
DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_miss_provenance.py --log railway-export.log
DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_miss_provenance.py --ids sentry-traces.csv --limit 50
```

Outputs `/tmp/lml-537-cache-miss-provenance.csv` and a summary table to stdout. Accepts either a Railway log file (`--log`) parsed by built-in regex patterns or a CSV of `release_id,method` (`--ids`) from a Sentry trace export.

### Cache warm-up histogram (`scripts/cache_warm_histogram.py`)

Buckets `release.artwork_checked_at` by day and contrasts the pre-Alembic-0009 ETL fill with the post-deploy dynamic warm-up daily mean. A small post-deploy mean confirms the warm-up channel is structurally limited (H1).

**Usage:**
```bash
DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_warm_histogram.py
DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_warm_histogram.py --csv > histogram.csv
DATABASE_URL_DISCOGS=postgresql://... python scripts/cache_warm_histogram.py --since 2026-06-07
```

### Discogs ranking jitter (`scripts/discogs_ranking_jitter.py`)

Issues two identical `/database/search?track=...&artist=...` calls separated by a short delay, measures jaccard overlap of returned release IDs and average rank-delta for shared IDs. Low jaccard confirms H2 — Discogs returns different IDs across calls, so warming the prior set doesn't help the next one.

**Usage:**
```bash
DISCOGS_TOKEN=... python scripts/discogs_ranking_jitter.py \
  --track "Moments of Soft Persuasion" --artist Yoshimura
DISCOGS_TOKEN=... python scripts/discogs_ranking_jitter.py \
  --track Coastin --artist "Quiet Force" --repeat 3 --delay-seconds 45 --json
```

Bypasses `DiscogsService` (no L1 LRU, no semaphore) to surface Discogs-side ranking behavior directly. Default delay is 30s (within Discogs's 60req/60s window); the `X-Discogs-Ratelimit-Remaining` header is logged after each call.

## Artist Name Variation Audit (`scripts/variation_audit/`)

Cross-references WXYC library catalog artist name variations against local Discogs and MusicBrainz datasets. Classifies each relationship (ALIAS, MEMBER_OF_GROUP, SEPARATE_ARTIST, COLLABORATION, SPELLING_VARIANT, SPLIT_RELEASE) and identifies artists that should have their own library code. Output includes flowsheet play counts and own-release counts for prioritization.

**Pre-step** (extract Discogs artist CSVs, ~7 seconds):
```bash
mkdir -p /tmp/discogs_artists
ln -sf /path/to/discogs_artists.xml /tmp/discogs_artists/artists.xml
discogs-xml-converter /tmp/discogs_artists/ --output-dir /tmp/discogs_artists/
```

**Usage:**
```bash
uv run python -m scripts.variation_audit \
  --library-db library.db \
  --graph-db ../semantic-index/data/wxyc_artist_graph.db \
  --sql-dump ../tubafrenzy/wxycmusic-full-2026-03-28.sql \
  --discogs-csv-dir /tmp/discogs_artists/ \
  --mb-alias-tsv ../musicbrainz-cache/data/mbdump/artist_alias \
  --mb-artist-tsv ../musicbrainz-cache/data/mbdump/artist \
  --output-dir ../docs/variation-audit/
```

Discogs CSVs and MusicBrainz TSVs are optional; the script gracefully degrades using only the semantic-index entity resolution and member-of data when external files are missing.

## Bulk Artist-Resolve Drain (`scripts/artist_resolve_drain/`)

Drains a bare-name set — clean touring-artist names Backend-Service exports for its concerts pipeline (WXYC/Backend-Service#1614) — through the prod `POST /api/v1/artists/resolve/bulk` endpoint (LML#759), minting `entity.identity` rows for the exact-form-unique names and producing a yield report + a wrong-mint spot-check table. Backend-Service owns the name-set export (the clean-name predicate is the same code as its `extractHeadliner` gate); the handoff is a names file (JSON array or newline-delimited).

The drain **always runs against production**, not staging: prod is the only place all Discogs traffic coordinates through one 50/min limiter + the LML#755 saturation breaker, so a drain there cannot 429 live lookups the way a staging drain (shared token, uncoordinated limiter) would. Run it off-peak, outside the 06:00 UTC flowsheet-backfill window.

It pages 25 names at a time (the endpoint cap), appends every verdict to a JSONL log, and resumes from that log on restart — a crash at batch 8 re-pays nothing already settled. The one retryable verdict, `escalation_unavailable` (breaker open / Discogs outage / 429 / 5xx-after-retries), is re-paged after a cool-down with bounded retries (default 2), then reported as residual. Dry and live records may share one JSONL file; resume and reporting are mode-scoped, so a prior dry drain never makes a `--live` run skip the mint.

**Runbook** (dry drain → human spot-check → live drain → BS writer unblocks):
```bash
# 1. dry drain (default; no write-back)
LML_API_KEY=... LML_BASE_URL=https://<prod-lml> \
  uv run python -m scripts.artist_resolve_drain names.txt \
    --out drain.jsonl --report report.md

# 2. a human eyeballs report.md's spot-check table (discogs.com/artist/<id> links)
#    for wrong mints — the only defense against a bare name colliding with a
#    single obscure Discogs artist. THEN authorize the live run:

# 3. live drain — mints DURABLE entity.identity rows (COALESCE never-clobber,
#    so a wrong mint is un-self-correcting)
LML_API_KEY=... LML_BASE_URL=https://<prod-lml> \
  uv run python -m scripts.artist_resolve_drain names.txt \
    --live --out drain.jsonl --report report-live.md
```

`--base-url` defaults to `$LML_BASE_URL` then `$PRODUCTION_URL`; `--api-key` to `$LML_API_KEY`. Other flags: `--page-size` (default/cap 25), `--max-retries` (default 2), `--cooldown` (seconds between retry rounds, default 60), `--spot-check` (sample size, default 20), `--seed` (spot-check RNG seed — the sample is reproducible from the JSONL), `--timeout` (per-request seconds, default 120; a fully-escalating page can take ~30s). The report is printed to stdout and, with `--report`, written to a markdown file for pasting into WXYC/Backend-Service#1614.

## Library-Release Override Seeder (`scripts/seed_library_release_overrides.py`)

Seeds `lml_cache.library_release_override` from WXYC DJ Alex L.'s hand-verified `card_catalog_id -> discogs_release_id` CSV (LML#850). Each pin makes a library-album `/lookup` return the verified Discogs release (and its correct tracklist) instead of the per-request fuzzy pick — gated behind the `LML_LIBRARY_RELEASE_OVERRIDE` flag (see [`env-vars.md`](env-vars.md)). The verified CSV lives in the workspace meta-repo (`plans/alex-discogs-import/data/phase1_release_links.csv`) and is **not** committed here — pass its path via `--input`.

**Safety:** dry-run by default — parses + validates the CSV and reports, making **no DB connection** (so it can't create the schema either); pass `--execute` to bootstrap the schema and write. Every row is stamped `--source` (default `alex-l-2026`) so a run is reversible by `DELETE FROM lml_cache.library_release_override WHERE source = '<source>'`. The upsert is `ON CONFLICT (library_id) DO UPDATE` guarded by `source = EXCLUDED.source`, so a re-run is idempotent **and** never clobbers a pin later hand-corrected under a different `source`. Targets the shared discogs-cache PG via `DATABASE_URL_DISCOGS`. **Prod writes require explicit authorization and run staging-first.**

```bash
# 1. dry-run (default): parse + validate + report, no DB connection
uv run python -m scripts.seed_library_release_overrides \
  --input ../plans/alex-discogs-import/data/phase1_release_links.csv

# 2. perform the upsert (staging first)
uv run python -m scripts.seed_library_release_overrides \
  --input <path> --execute
```

`load_rows` opens the CSV `utf-8-sig` (BOM-tolerant), drops rows with a missing/non-integer/non-positive id, and dedupes by `library_id` (last row wins — a later hand-correction supersedes an earlier automated entry). The seeder writes the override table only; minting `entity.release_identity` for the pinned releases and warming the release cache are separate optional operational steps via the existing HTTP surfaces (`POST /api/v1/identity/resolve`, `POST /api/v1/cache/refresh-for-identities`) against a running service.

## Master → Release Resolver (`scripts/resolve_master_overrides.py`)

Phase 2 of the Alex L. import (LML#858). The bulk of Alex's dataset links each card to a Discogs **master** id, not a release — and a master yields no single tracklist. This read-only pre-step converts the master-typed rows of `merged_discogs_links.csv` into concrete release ids and emits CSVs the [seeder](#library-release-override-seeder-scriptsseed_library_release_overridespy) consumes **unchanged** (it reads `card_catalog_id` + `discogs_release_id` by name; the extra `master_id,tier,confidence,reason` audit columns are ignored). It only reads the shared discogs-cache (`DATABASE_URL_DISCOGS`) — it never writes.

For each card the choice among the master's cached versions (grouped via `release.master_id`, each release's order-insensitive tracklist normalized with `wxyc_etl.text.to_match_form`) is **tiered**:

- **Tier A** (high) — one cached version, or several with identical tracklists: the choice is forced or provably irrelevant.
- **Tier B** (high) — versions diverge and the flowsheet's played titles pick a unique **strict-superset** winner. The signal is asymmetric — a played title present on version A and absent from B is evidence *for* A, but an unplayed title proves nothing — so a strict-superset margin is required, not raw-count scoring.
- **Tier C** (low) — versions diverge, no distinguishing flowsheet signal: pin a **cached** version (the master's `main_release` when it is itself cached, else a format-matched / lowest-id cached edition). An uncached `main_release` is deliberately never pinned — it carries no cached tracklist, and a format-matched cached edition is the better shelf-copy approximation. Format matching is by **token overlap** (e.g. a `vinyl - 7"` shelf copy prefers the cached 7" pressing over the LP), not whole-string equality.
- **no_cached** / **unresolved** — the master has no cached release: pin `main_release_id` if the `master` table supplies one (rare — the masters import is library-scoped, so a master with no cached release usually has no row either), else leave unresolved (the Discogs-API tail, reported, never silently dropped).

Tier A/B land in `--out-high` (seed as `--source alex-l-2026-masters`); Tier C / no_cached land in `--out-low` (seed as `--source alex-l-2026-masters-lowconf`) so the two confidence buckets stay separable and the seeder's `source`-guarded upsert treats each as its own set. **Every Tier A/B/C pin resolves to a cached, non-empty tracklist** — `fetch_candidates` inner-joins `release_track`, candidates whose titles all normalize to empty are dropped, and the choosers only ever return a surviving candidate id. The one exception is the rare `no_cached` tier: when a master has a `main_release_id` but no cached release at all, it pins that uncached main_release under the low-confidence tag — a concrete release identity whose tracklist LML fetches from the Discogs API on demand rather than from cache.

The `master.main_release_id` join requires the discogs-cache `master` table, populated by [WXYC/discogs-etl#317](https://github.com/WXYC/discogs-etl/issues/317). Tier A/B resolve off `release.master_id` alone and work without it; Tier C/no_cached degrade gracefully (deterministic cached fallback) when it is empty.

```bash
# read-only: query the cache, resolve, write two seed CSVs + a per-tier report
uv run python -m scripts.resolve_master_overrides \
  --input ../plans/alex-discogs-import/data/merged_discogs_links.csv \
  --flowsheet ../plans/alex-discogs-import/data/flowsheet_titles_per_card.csv \
  --out-high phase2_master_links_highconf.csv \
  --out-low  phase2_master_links_lowconf.csv \
  --out-unresolved phase2_master_unresolved.csv \
  --report   phase2_resolve_report.json

# then seed each bucket with the existing seeder (staging-verify, then prod — gated)
uv run python -m scripts.seed_library_release_overrides \
  --input phase2_master_links_highconf.csv --source alex-l-2026-masters --execute
```

The flowsheet titles CSV (`card_catalog_id,title`, one raw title per row) is produced by `plans/alex-discogs-import/scripts/parse_flowsheet.py` in the workspace meta-repo; the resolver re-normalizes those titles with the same `to_match_form` it applies to candidate tracklists, so Tier-B matching compares like-for-like. Omitting `--flowsheet` disables Tier B (all divergent masters fall to Tier C).

`--out-unresolved` writes the `UNRESOLVED` tail (`card_catalog_id,master_id`) — masters with no cached release and no local `main_release_id` — as the work-list for the API-tail drain below. Omitting it drops those cards silently; pass it whenever the resolver's report shows a non-zero `unresolved` count.

## Master API-Tail Drain (`scripts/drain_master_api_tail.py`)

Resolves the `UNRESOLVED` tail from `resolve_master_overrides --out-unresolved` against the **live Discogs API** and emits a seed CSV the [seeder](#library-release-override-seeder-scriptsseed_library_release_overridespy) consumes unchanged (source tag `alex-l-2026-masters-api`). Per distinct master: `get_master(id)` → `.main_release_id`, then `get_release(main_release_id)` — which both **confirms a non-blank tracklist** (never pin a trackless release, matching the Phase-2 invariant) and **warms the PG cache** via the fallthrough write-back, so the first user lookup is hot rather than a cold-tail API hit ([LML#706](https://github.com/WXYC/library-metadata-lookup/issues/706)). Only masters that clear both steps are pinned, one row per referencing card.

**Shared-token safety.** `get_master`/`get_release` route through `DiscogsService._request_with_retry`, so every call is bounded by the service's own (per-process) 50/min rate limiter and shielded by the [LML#755](https://github.com/WXYC/library-metadata-lookup/issues/755) saturation breaker. That limiter is per-process — the drain runs as a standalone script, so it does **not** coordinate with the live service's limiter and shares the prod Discogs token uncoordinated: run **off-peak** and **never** concurrently with a bulk backfill campaign (BS#1631-class). Progress is checkpointed per master to a JSONL file: terminal states (`resolved`, `no_main_release`, `trackless`) are never retried, while `dead`/`error` (ambiguous/transient) and un-checkpointed raises (e.g. a breaker shed) are re-attempted on the next run — so an interrupted or re-run drain never re-spends API budget on a settled master. `--limit` caps masters per run for a smoke batch; `--concurrency` bounds in-flight masters on top of the service limiter.

```bash
# resolve the API tail (writes/updates the checkpoint + a seed CSV); off-peak only
uv run python -m scripts.drain_master_api_tail \
  --unresolved phase2_master_unresolved.csv \
  --checkpoint drain_checkpoint.jsonl \
  --out-seed  phase2_master_links_api.csv \
  --concurrency 4 --limit 200   # drop --limit for the full run

# then seed the API-derived pins (staging-verify, then prod — gated)
uv run python -m scripts.seed_library_release_overrides \
  --input phase2_master_links_api.csv --source alex-l-2026-masters-api --execute
```

## Apple-Music-URL Resolver (`scripts/resolve_apple_urls.py`)

Off-prod resolver for the [BS#1631](https://github.com/WXYC/Backend-Service/issues/1631) Apple-Music-URL backfill's still-null `album_metadata` tail. The in-service backfill drives LML `/api/v1/lookup`, but almost none of a full lookup's work is needed for `apple_music_url`: the persistent album-level URL comes *solely* from the async warm calling `AppleMusicClient.find_album_match(artist, album)` (`entity/streaming_url_cache.py`). This script calls that exact method directly — **exact match parity** with the album-phase capture path — at a fraction of the cost, and, run off-prod, sidesteps both the LML health watchdog and the synchronous-enrichment latency wall ([LML#706](https://github.com/WXYC/library-metadata-lookup/issues/706)) that make the in-service backfill trip and under-capture.

It is the **resolve** half of a two-phase, clean-ownership design: this script reads a read-only candidate export `(album_id, artist, album)` and emits `(album_id, apple_music_url)` for accepted matches to a TSV; the **apply** half (Backend-Service `jobs/apple-music-url-backfill/resolve.ts::applyUpdate`, fill-only `WHERE apple_music_url IS NULL`) writes it back. LML never writes BS RDS. Scope is **album-level only** — `find_album_match` is the album-phase warm path; the flowsheet phase's track-level probe is out of scope.

**Shared-token safety.** The Apple JWT creds (`APPLE_MUSIC_TEAM_ID`/`KEY_ID`/`PRIVATE_KEY`, from `os.environ` or a local `.env`) are prod's and their per-process limiter is not coordinated with the live service's. `AppleMusicClient` already enforces its own internal ceiling — an `asyncio.Semaphore(5)` plus an `AsyncLimiter` of ~60 calls/min — so `--concurrency` / `--rate-per-min` can only make this resolver **more** conservative than that built-in 5-concurrent / ~60-per-minute cap (they bind only when set *below* it); they cannot make it faster. Run **off-peak** and set them below the ceiling to leave headroom for prod's own enrichment (a 403/429 reuses the client's backoff). Progress is checkpointed per album to a JSONL file: terminal states (`matched`, `no_match`) are never retried, while `api_error` is re-attempted on the next run — so an interrupted or re-run resolve never re-spends Apple budget on a settled candidate. Note the authenticated client *swallows* transient HTTP errors (exhausted 429/5xx, transport, bad JSON) to an empty result, so those return `None` and settle as `no_match` (terminal), **not** `api_error`; re-probing a candidate frozen by transient contention needs a fresh checkpoint (hence off-peak). `--limit` caps candidates per run for a smoke batch; `--dry-run` resolves + tallies + checkpoints but emits no output TSV.

```bash
# dry-run tally first (no TSV emitted); off-peak only. --concurrency/--rate-per-min
# below the client's built-in 5-concurrent / ~60-per-min ceiling to leave prod headroom.
uv run python -m scripts.resolve_apple_urls \
  --candidates candidates.tsv \
  --checkpoint apple_resolve.jsonl \
  --out apple_urls.tsv \
  --concurrency 3 --rate-per-min 30 --dry-run

# then the real resolve (writes/updates the checkpoint + the (album_id, url) TSV)
uv run python -m scripts.resolve_apple_urls \
  --candidates candidates.tsv \
  --checkpoint apple_resolve.jsonl \
  --out apple_urls.tsv \
  --concurrency 3 --rate-per-min 30
```

The emitted `apple_urls.tsv` is applied back via the Backend-Service fill-only write (SELECT-before / count-after; never overwrites a non-null URL).
