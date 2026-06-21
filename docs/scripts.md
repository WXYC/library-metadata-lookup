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

Sweeps the trigram-similarity floor used by `lookup/orchestrator.resolve_canonical_artist` (the pre-pass for `search_compilations_for_track` — see WXYC/library-metadata-lookup#318). Builds labeled datasets — positives from `artist_name_variation`, `entity.identity`, and (optionally) synthetic listener-typo corruptions; negatives from sampled close-but-distinct `artist` pairs — and writes a precision/recall sweep + a borderline-band CSV.

The synthetic-typo class targets the actual production failure mode (listener-typed names like "Robotnik" → "Robotnick"). The Discogs `artist_name_variation` class covers aliases / AKAs ("Bob Dylan" / "Robert Zimmerman") which is a different distribution. Synthetic class is opt-in via `--synthetic-positive-size N` (default 0 = disabled); set ~1000 for a typical calibration run. See `scripts/resolver_calibration/synthetic_typos.py` for the four corruption classes (drop, transpose, duplicate, ASCII-fold).

**Output** (defaults to `docs/resolver-calibration/`):
- `calibration_sweep.csv` — threshold, TP rate, FP rate, sample sizes, swap count.
- `borderline.csv` — pairs whose score sits in `[chosen_floor − 0.05, chosen_floor + 0.05]`, sorted by score, for eyeball QA of the decision boundary.

After running, update `CANONICAL_ARTIST_SIMILARITY_FLOOR` in `lookup/orchestrator.py` if the chosen floor differs from the in-tree value, and commit the CSVs alongside a one-page `docs/resolver-calibration/README.md` documenting the FP-rate tolerance that drove the choice. The same script is used to re-validate after large discogs-cache refreshes; commit fresh CSVs each run so the calibration history stays in version control.

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
