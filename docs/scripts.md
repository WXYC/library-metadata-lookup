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

## Gate 0 Burst Harness (`scripts/gate0_burst.py`)

The measurement mechanism for [LML#983](https://github.com/WXYC/library-metadata-lookup/issues/983) Gate 0: fires a concurrent `POST /api/v1/lookup` burst against a target host and reports p50/p95/p99 of the `lml_wall` and `event_loop_lag` `Server-Timing` legs, plus a warm/cold bimodality split — the direct signal for the cross-worker in-memory cache fragmentation the issue describes (each `UVICORN_WORKERS` process holds its own `TTLCache` heap with no cross-process sharing, so an identical query oscillates warm/cold depending on which worker the kernel hands the connection to). Run it once against `UVICORN_WORKERS=1` and once against `=3` on staging to decide whether the 3-worker burst headroom LML#747 bought is still needed now that #949/PR#899 removed its main p50 justification. Full human-supervised procedure (Railway CLI commands, the `DISCOGS_RATE_LIMIT` coupled-knob unwind, safe-burst-size guidance) is the [Gate 0 runbook in `docs/deployment.md`](deployment.md#gate-0-lml983-measuring-whether-uvicorn_workers3-is-still-needed).

**Query set:** three representative WXYC queries, including the exact compilation-track query from the issue trace (`{"artist": "c spencer yeh", "song": "in the blink of an eye"}` — the Wave B `search_releases_by_track` path, which has no PG tier per LML#393 and is therefore the highest-value cold-worker probe) plus two ordinary artist+album lookups (`GATE0_QUERIES` in the script).

**Safety.** Staging shares prod's Discogs token, LML#755 saturation breaker, and (as of this writing) a differently-keyed LML#841 rate bucket -- see the runbook's cautions. The harness itself refuses to run a burst against `/lookup` above a modest default `--concurrency`/`--total` ceiling without `--force`, and aborts mid-burst (stops issuing new requests, exits 2) the instant it observes a shed response: an HTTP 429/5xx, or a `200` body with `degraded: true` and `degraded_reason: "upstream_unavailable"` (the client-visible signal for a breaker shed or exhausted rate bucket -- `deadline_exceeded` is excluded, since that's a caller-budget shed unrelated to Discogs saturation). `--smoke` points the burst at `GET /health` instead -- zero Discogs risk, no API key required -- for validating the concurrency + request-firing plumbing before ever touching the real query set. `/health` is outside the Server-Timing middleware's `/api/v1/lookup` scope, so a smoke run emits no `lml_wall`/`event_loop_lag` legs and reports `server_timing_present: false` **by design** -- it does not exercise the header parser (the harness prints an explicit note rather than the LML_EMIT_SERVER_TIMING warning in that case).

**Usage:**
```bash
# Plumbing smoke test (no Discogs risk, no auth) -- run this first
python scripts/gate0_burst.py --host https://<staging-domain> --smoke

# The real Gate 0 measurement (human-supervised; run once per worker count)
LML_API_KEY=... python scripts/gate0_burst.py \
  --host https://<staging-domain> --concurrency 3 --total 12 --warm --json
```

Options: `--host` (required), `--api-key` (default `$LML_API_KEY`), `--concurrency` (default 3), `--total` (default 12), `--warm`/`--no-warm` (sequential one-hit-per-query prewarm pass before the concurrent burst, default on -- mirrors the issue trace's "run 1" that warms exactly one worker), `--smoke` (target `/health` instead of `/lookup`), `--warm-threshold-ms` (warm/cold split point, default 500.0), `--timeout` (per-request seconds, default 10.0), `--force` (override the burst-size safety rail), `--json` (machine-readable output).

If `server_timing_present` comes back `false` in a **real `/lookup` run** (not `--smoke`, which is always false as noted above), `LML_EMIT_SERVER_TIMING` (or the LML#907 `lml_event_loop_lag_gauge` flag) is likely off on the target -- the harness falls back to client-measured wall time only and warns loudly in the human-readable table.

## API Model Generation (`scripts/generate_api_models.sh`)

Generates Pydantic v2 models from `wxyc-shared/api.yaml`. Uses a local sibling `wxyc-shared` directory if available, otherwise downloads from GitHub. The generated file (`generated/api_models.py`) is committed to git. Re-run after api.yaml changes. Note that the committed file is a snapshot, not a live view: the Codegen Freshness CI job regenerates against wxyc-shared `main` at run time and diffs against what's committed here, so it can go red purely because upstream `api.yaml` moved since the last regen here, with no defect in this repo -- see [#1117](https://github.com/WXYC/library-metadata-lookup/issues/1117) for the currently-tracked instance of that drift.

**Usage:**
```bash
bash scripts/generate_api_models.sh                  # sibling checkout, else wxyc-shared main
bash scripts/generate_api_models.sh --ref <sha>      # pin to an upstream revision
bash scripts/generate_api_models.sh --download-only  # stop after fetching api.yaml (test hook)
WXYC_SHARED_REF=<sha> bash scripts/generate_api_models.sh
```

**Authenticated download.** When an ambient `GH_TOKEN` / `GITHUB_TOKEN` is set (`GH_TOKEN` wins, matching `gh`'s own precedence), the GitHub download sends it as a Bearer `Authorization` header — anonymous raw.githubusercontent.com fetches share a per-IP rate budget across the Actions runner pool and intermittently 429, which is why the Codegen Freshness job exports `github.token` for this step ([#1205](https://github.com/WXYC/library-metadata-lookup/issues/1205)). Three properties matter to a caller: the header rides curl's stdin so the token never reaches argv or the log; a failed authenticated attempt retries once anonymously, so pre-#1205 behavior is the floor; and both variables are unset at the top of the script, before either source-resolution arm, so no child process on any path inherits the token. The script's header comment is the canonical rationale — read it before changing the token flow. `--download-only` stops after fetching api.yaml and exists for the unit tests covering this branch (`tests/unit/test_generate_api_models_auth.py`).

Requires `datamodel-code-generator` and `ruff` (both in dev dependencies). Neither is optional: `ruff format` + `ruff check --fix` run over the generator's output, and byte-equality against that formatted result is exactly what the Codegen Freshness gate diffs, so the script fails loudly rather than emitting unformatted models if either binary is missing.

### Pinning the upstream revision

`--ref` (or `WXYC_SHARED_REF`) downloads one exact wxyc-shared revision and bypasses any sibling checkout, which is what you want when reproducing a past regen or bisecting a contract change. Without it the script prefers a local sibling `wxyc-shared/api.yaml` and otherwise tracks `main` at run time; it prints the sibling's branch and `git describe` so a stale or unpulled checkout is visible in the log rather than showing up later as a phantom CI drift failure.

**CI deliberately stays unpinned.** The Codegen Freshness job exists to diff the committed snapshot against whatever upstream `main` currently says, so pinning it would silence the very signal it is there to raise. The cost is that the job goes red on every open PR the moment wxyc-shared merges an api.yaml change, with no defect in the PR that tripped it -- see [#1117](https://github.com/WXYC/library-metadata-lookup/issues/1117). Reach for `--ref` locally, not in the workflow. Adopting wxyc-shared's consolidated generator (which carries `--ref` plus a `uvx`-pinned toolchain, an ancestor-walking venv probe, and empty-argument guards) is tracked at [#1159](https://github.com/WXYC/library-metadata-lookup/issues/1159); until that lands, a codegen fix made upstream has to be re-applied to this copy by hand.

### Schema descriptions become class docstrings

The generator runs with `--use-schema-description`, so an `api.yaml` schema-level `description` lands as the generated class's docstring. This is load-bearing rather than cosmetic: upstream field descriptions cross-reference their schema's prose (`AlbumMetadataResponse.recordLabel` says "see this schema's freshness caveat", and the caveat -- base fields are memoized for 1h, so a librarian's label correction can be shadowed for that window -- exists *only* at the schema level). Without the flag every such pointer dangles, and a Codegen Freshness pass certifies only that field *shapes* match upstream, not that the documented contract came with them.

### Enum fields are a closed set in Python

The org treats adding a response-enum value as non-breaking (oasdiff WARN, minor version bump). **That holds for the generated TypeScript and not for the generated Python.** Enum-typed fields generate as `StrEnum` subclasses, and pydantic raises `ValidationError` on an unrecognized member -- so a purely additive upstream enum value breaks every Python consumer pinned to the committed `api_models.py` until it regenerates, on responses the contract classifies as compatible. `MetadataStatus` currently reaches LML on three fields -- `AlbumMetadataResponse.metadataStatus`, `FlowsheetEntryResponse.metadata_status`, and `FlowsheetV2TrackEntry.metadata_status` -- and `tests/unit/test_generated_models.py` pins both the closed-set behavior and the coupling to the shared enum (api.yaml `$ref`s one schema across all three so they move together; a regen that widened any of them back to `str` would silently decouple them). When upstream adds an enum member, LML needs a regen PR, not just a version bump.

### Known contract residue

`AlbumMetadataResponse` declares three of its six local-first base fields. `artistName`, `releaseTitle` and `trackTitle` are on every Backend-Service response but declared nowhere in `api.yaml`, so pydantic's default `extra="ignore"` silently drops them on decode -- including `artistName`, the one field guaranteed present when every upstream call fails. Do not work around this by loosening `extra` on the generated models: the fix belongs in `api.yaml`, tracked upstream at [WXYC/wxyc-shared#324](https://github.com/WXYC/wxyc-shared/issues/324). `tests/unit/test_generated_models.py` characterizes today's dropping behavior so the regen that fixes it turns that test red on purpose.

### Strict-nullable field-shape semantics

The generator runs with `--strict-nullable` (adopted in PR #1154, merge commit `0bc5a40`, following [WXYC/wxyc-shared#302](https://github.com/WXYC/wxyc-shared/issues/302)). It reshapes two different kinds of `api.yaml` property in opposite directions, and both directions matter to anyone debugging an unexpected `ValidationError` or an inbound 422 on the `/lookup` hot path:

- **Required + `nullable: true` -> widened.** A property that is both `required` and `nullable: true` now generates as `X | None = Field(...)` -- the *value* is nullable, the *key* stays required. Read the trailing `...` as pydantic's required-field marker, not a default; there is no default here. `BulkResolveTrackIdentity.resolved_artist_name` is the canonical example in the generated tree: `None` is a real, constructible verdict ("the matcher ran and resolved nothing for this track"), not a validation failure.
- **Optional + schema default -> narrowed.** A property that is optional with a schema-level default (no `nullable: true`) now generates *without* `Optional` -- e.g. `LookupRequest.include_identity: bool = Field(False, ...)`. Explicitly passing `None` to one of these now raises a `ValidationError` at construction time, and an inbound request body with an explicit JSON `null` for that field now 422s where it previously validated silently. This was a byproduct of the pre-flag defaulting bug, not part of the contract.

The full enumerated list (36 widened, 36 narrowed, measured at api.yaml 1.30.0) lives on [WXYC/wxyc-shared#302](https://github.com/WXYC/wxyc-shared/issues/302); the canonical semantics writeup is wxyc-shared's `CLAUDE.md` under "Python codegen and `nullable` on required fields". This section covers only what's specific to LML.

**`generated/api_models.py` is generated and must never be hand-edited.** LML-local additions and overrides live one layer up instead. `lookup/models.py` subclasses the generated request/response models -- `class LookupRequest(_GeneratedLookupRequest)`, `class LookupResponse(_GeneratedLookupResponse)`, `class LookupResultItem(_GeneratedLookupResultItem)` -- to add fields and serialization behavior ahead of the next api.yaml regen. `discogs/models.py` takes the alias form for schemas that need no LML-local additions (`TrackItem = DiscogsTrackItem`, `ArtistCredit = DiscogsArtistCredit`, and similar). Either way, a `--strict-nullable` reshape in the generated layer propagates through: a subclass or alias built on a narrowed or widened generated field is exactly as narrow or wide as its parent, so the effect doesn't stop at `generated/api_models.py`.

**The failure mode to recognize:** a present-but-null value from an external API or a nullable PostgreSQL column reaching a model constructor. Discogs can send an explicit JSON `null` where it usually omits the key entirely, and `dict.get(key, default)` only applies its default on an *absent* key -- a present-but-null `genres`/`styles`/`join` used to sail straight through into the pre-narrowing model, which tolerated it. Post-`--strict-nullable`, the narrowed models don't.

`discogs/service.py` and `discogs/cache_service.py` guard some, not all, of these sites, and the line between guarded and not is not "wherever a null could theoretically arrive" -- it follows one rule: **a site is guarded if and only if the guard restores a documented schema default.** `DiscogsTrackItem.artists: list[str] = []`, `DiscogsArtistCredit.join: str = Field("", ...)`, `Member.active: bool = True`, and the `genres`/`styles`/`namevariations`/`urls` list fields all carry a real default, so a present-but-null value is normalized back to it -- with an `or` fallback for list/string fields (`data.get("genres") or []`) and an explicit `is None` check for booleans where an `or` guard would corrupt a legitimate `False` (`True if v.get("embed") is None else v["embed"]`; `discogs/cache_service.py` applies the same `is None` guard to the nullable `artist_member.active` PostgreSQL column). A required field with no default -- `DiscogsArtistCredit.name`, `DiscogsTrackItem.position`/`title`, `DiscogsReleaseMetadata.title`/`artist`, `Alias.id`/`name`, `Member.id`/`name` -- is deliberately left unguarded: there is no schema value to fall back to, so a null there means the payload is malformed, and letting construction fail rather than inventing a value is the intended outcome.

The reason is persistence, not style, and the same reasoning already governs `master_id` normalization earlier in `discogs/service.py`'s release parse: `_api_fetch`'s return value is written straight into the PG cache through the fallthrough seam (`pg_write=cache.write_release`, `pg_write=cache.write_artist_details`), so inventing a value for a no-default field wouldn't just paper over one response -- it would persist a fabricated value into the cache, turning a transient upstream null into a permanently wrong cached row that every later read then hits as a false success instead of a retryable miss. A new call site should apply the same test: restore the schema default if there is one; otherwise let it fail and stay a cache miss.

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

### Phase album-search: album-first discovery (LML#1069)

Phases 1/2 above are **artist-first**: they only ever find an album if it lives on the matched artist's own `*.bandcamp.com` subdomain catalog. `--phase album-search` is **album-first** instead — it searches the general Bandcamp autocomplete index (`param="a"`, album-type results) for `artist title` and binds by the returned `band_name`, so it also recovers releases hosted on a label/imprint/reissue subdomain that the artist's own page never carries (the measurement's golden repro: George Theodorakis's *The Rules of the Game* lives on `into-the-light.bandcamp.com`, not the artist's own `georgerakis.bandcamp.com`).

This phase calls `BandcampClient.find_album_match_via_search(artist, title)` — exactly one autocomplete request, matched via the same 80/80-floor `find_best_source_match` used by every other adapter (LML#592 telemetry included). It's a shared method by design: the runtime probe (`BandcampClient.find_album_match`, LML#573 tier) falls back to it whenever the artist-first path finds nothing, so a live `/lookup` or `/streaming-check` request also benefits from label/imprint-hosted discovery, at the cost of at most one extra rate-limited autocomplete request within Bandcamp's existing looser probe-timeout ceiling. It runs the floor check twice at most against that single result set: once on raw fields, and — only if that misses — once more with `normalize_bc_title` applied to both sides' titles (recovers catalog-tag/year-range/slash-spacing near-misses; see `clients/bandcamp.py` for the corpus this is parameterized against). `search_albums` distinguishes a transient fetch failure (`None` — network error, non-200, exhausted 429 backoff) from a genuine empty result set (`[]`), mirroring `fetch_artist_catalog`'s None/`[]` split (#661); `find_album_match_via_search` raises `BandcampSearchUnavailableError` on the former so this phase can avoid a durable `not_found` write on a blip.

**Candidates** come from `ResultsDB.get_pending_album_search()`: no `bandcamp_url`, non-compilation, `bandcamp_status = 'pending'` (or also `'not_found'` with `--include-not-found`), and `bandcamp_slug` is `NULL` or `''`. A row with a *real* recorded slug is always excluded — it's owned by the Phase 2 catalog backlog above, and album-search must not pre-empt a pending catalog scrape.

**Writes are opt-in and fill-only**, the opposite default of Phases 1/2: without `--execute` the phase only resolves and tallies (a `--report-json` of would-be writes); nothing is persisted. A hit writes via `update_bandcamp_url`; a miss on a `pending` row writes `not_found` via `mark_bandcamp_not_found` (album-first is now the primary mechanism, so a miss here is durable — no future artist-first sweep is preserved for it); a miss on an already-`not_found` row is a no-op (tallied `skipped`, keeps re-runs idempotent); a `BandcampSearchUnavailableError` is tallied `fetch_failed` and leaves the row untouched/re-runnable.

**Usage:**
```bash
# dry-run (default): resolve + tally, write nothing
python -m scripts.bandcamp_pipeline --phase album-search --limit 500 --report-json /tmp/report.json

# persist (opt-in, fill-only, with og:title verification before each write)
python -m scripts.bandcamp_pipeline --phase album-search --execute --db-path streaming_availability.db
```

Flags (album-search only): `--include-not-found` (also target rows already marked `not_found`), `--execute` (persist writes; only valid with `--phase album-search`, and an argparse error combined with `--dry-run`), `--verify-hits` / `--no-verify-hits` (fetch the matched album page and require its `og:title` — `"Title, by Artist"` — to clear the same 80/80 floor before writing; defaults to matching `--execute`, i.e. on when executing, off in dry-run), `--report-json PATH` (write the tallies + would-be-written rows). `--dry-run` is accepted but a no-op for this phase (it's already dry by default); `--limit`/`--db-path` are shared with the legacy phases.

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

## Wikipedia URL Extractor Validation (`scripts/wikipedia_url_validation.py`)

LML#513's empirical gate for the `LML_WIKIPEDIA_SLUG_MATCH` flag (`docs/plans/lml-1192-wikipedia-artist-bio.md` Phase A): samples discogs-cache artists carrying a `wikipedia.org` `artist_url` row, runs both the legacy first-match heuristic and the slug-scored extractor (`lookup.wikipedia_url.compare_wikipedia_extractors`) over each, and writes a CSV for hand classification. Two phases, mirroring `scripts/resolver_calibration`'s sweep-then-hand-review split.

**Usage:**
```bash
# 1. sample phase: query discogs-cache, write a CSV with blank
#    heuristic_correct / slug_correct columns
DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.wikipedia_url_validation \
  --sample-size 300 --seed 513 --out lml-513-sample.csv

# 2. a human opens each URL in the CSV and fills in heuristic_correct /
#    slug_correct with TRUE/FALSE, then:
uv run python -m scripts.wikipedia_url_validation --report lml-513-sample.csv
```

`--report` mode makes no DB connection — it only reads the already-classified CSV back and computes the regression rate (heuristic right, slug wrong) and improvement rate (heuristic wrong, slug right); rows with either column left blank are excluded from the denominator (not yet classified). The gate: a regression rate under 2% before flipping `LML_WIKIPEDIA_SLUG_MATCH` on staging, then prod. `--sample-size` (default 300), `--seed` (default 513, for a reproducible sample), `--out` (default `wikipedia_url_validation_sample.csv`). The sample phase intersects its seed with WXYC library artists by default (`--library-db`, same flag and default as the drain, applying the drain's own normalization predicate) so the gate measures exactly the population the drain serves; `--skip-library-intersection` samples the raw `artist_url` superset instead — only for deliberate population comparisons, since the raw tables include resolve-minted non-library artists. Read-only; never writes to PG.

## Wikipedia Bio Cache Drain (`scripts/warm_wikipedia_bios.py`)

The **primary population mechanism** for `lml_cache.artist_wikipedia_bio` (LML#513/#1192 Phase C; `docs/plans/lml-1192-wikipedia-artist-bio.md`). The lazy background miss-warm (`lookup/enrichment/wikipedia_warm.py`) only extends coverage to artists newly entering the flowsheet going forward — this drain is what covers the *existing* catalog, and per BS#1747 (Backend-Service freezes the first LML answer for an album permanently) it must run to completion **before** `LML_BIO_PREFER_WIKIPEDIA` flips in production, or a cold-cache first ask after the flip freezes the Discogs fallback for that artist instead of self-healing.

Seed: distinct discogs-cache artist ids carrying a `wikipedia.org` `artist_url` row (same candidate universe as `scripts/wikipedia_url_validation.py`), intersected with WXYC library artists (see the seed-intersection note below — the discogs-cache `artist`/`artist_url` tables are NOT library-exclusive in prod). Each candidate runs through `lookup.wikipedia_pick_validation.resolve_and_validate_pick` (LML#1192 review round 4, P0-2) — NOT the flag-independent `lookup.wikipedia_url.compare_wikipedia_extractors` a caller without a live fetch available uses: this drain pays for one, so it ranks candidates deterministically (`lookup.wikipedia_candidates.candidate_sort_key`) and then tries each in turn against a real fetch, using the first one whose fetch validates. This exists because no string-only rule can tell "the bare slug is the disambiguation page, the qualified one is correct" (Low, Sade) from "the bare slug is correct, the qualified one doesn't exist" (Sun Ra, Stereolab, Cat Power) — the deciding signal only lives in the fetched payload. A below-floor pick is declined without a live fetch (same "never fetch text for an unconfident pick" rule the request path follows) but still writes an `extract IS NULL` row so the artist counts as attempted. Since LML#1192 review round 5 removed the cache's URL-match read predicate (the row is now authoritative, read by `discogs_artist_id` alone — see `entity/artist_wikipedia_bio.py`'s module docstring), the written `wikipedia_url` no longer needs to match anything the request path would independently re-derive; it can legitimately be a better, fetch-validated pick than the request path's own unvalidated sync guess.

**Usage:**
```bash
# default: incremental -- only artist ids with no existing row
DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios

# retry rows that previously found nothing (a fetch miss OR a below-floor decline)
DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios --retry-misses --limit 5000

# operator lever: re-validate every already-successful row against a fresh live
# fetch, regardless of content age (e.g. after a Phase-A extractor recalibration)
DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios --repick --limit 500

# routine content upkeep: re-fetch already-successful rows whose content has aged
# past its 30-day TTL, oldest first -- unlike --repick, skips rows still fresh
DATABASE_URL_DISCOGS=postgresql://... uv run python -m scripts.warm_wikipedia_bios --refresh-stale --limit 500
```

Four mutually exclusive modes (`DrainMode = Literal["incremental", "retry_misses", "repick", "refresh"]`, collapsed into one `_MODE_SQL` + `fetch_candidates(pg, *, mode, ...)`, LML#1192 review round 3): default/`incremental` (the no-flag mode, never-attempted artist ids only), `--retry-misses` (only `extract IS NULL` rows plus any id recorded in `lml_cache.artist_wikipedia_bio_attempt` — never touches a positive row, the data-safety rule), `--repick` (every already-successful row, re-validated against a fresh live fetch regardless of whether the pick or the content has changed — LML#1192 review round 4, P0-2/P0-3 removed the earlier "only rewrite divergent rows" short-circuit, since knowing a pick is unchanged requires the same live fetch that also refreshes content; still never turns a positive `extract` into `NULL`, even when the fresh pick diverges into a negative — see the module docstring's data-safety section), `--refresh-stale` (LML#1192 review round 4, P0-3 — the same fetch-validate-and-write machinery as `--repick`, but scoped to already-successful rows whose `fetched_at` has aged past `DEFAULT_SUCCESS_TTL`, oldest-content-first; closes the gap `--repick`'s full-sweep cost and `incremental`/`--retry-misses`'s row-already-exists exclusion both leave open for routine upkeep of otherwise-healthy rows). Every mode's seed is intersected with WXYC library artists (`--library-db`, default `library.db`, reusing `scripts/build_filtered_discogs.py`'s `extract_library_artists`) — the discogs-cache `artist`/`artist_url` tables are NOT library-exclusive in prod (`LML_RESOLVE_NONLIBRARY_RELEASE` and the bulk artist-resolve drain both accumulate non-library artists into them), so an un-intersected seed would silently grow past the plan's target population. `--limit` caps candidates selected per session; `--rate` (default 3, correct for any positive rate including fractional; `--rate-per-second` kept as an alias) and `--max-retries` (default 5, honoring `Retry-After` on a 429 via `clients.wikipedia.WikipediaClient`'s own retry loop, which now acquires the rate limiter once per actual HTTP attempt rather than once per candidate) throttle live fetches. `--dry-run` runs the full picker + live fetches and tallies outcomes while writing nothing (neither content rows nor attempt records) — preview a mode's outcome mix, especially before a `--repick`/`--refresh-stale` pass, before committing to writes. Ctrl-C is the house two-stage graceful shutdown (`scripts/_lib`): the first signal stops at the next candidate boundary, prints the summary, and exits resumable; the second force-quits. A transient `WikipediaFetchError` (timeout, network error, exhausted retries) writes **nothing** — the row stays selectable by a future default/`--retry-misses` pass rather than being poisoned as a confirmed negative. A session aborts (non-zero exit) after too many consecutive failure-ish outcomes in a row, rather than grinding through the rest of the candidates making zero progress. Rough scale: bounded by LIBRARY artists with a Discogs-known Wikipedia URL — expect low-tens-of-thousands; at 3 rps that's a few hours across 1-2 sessions. Read-only against the WXYC catalog; the writes are to `lml_cache.artist_wikipedia_bio` (content rows) **and** `lml_cache.artist_wikipedia_bio_attempt` (a durable per-artist attempt record on `fetch_error`/`unresolvable`/`unexpected_error` outcomes — the cursor `--retry-misses` ordering reads, so those writes are load-bearing for resumability, not incidental).

**Not yet run against real data.** Like `scripts/wikipedia_url_validation.py`, the candidate-selection SQL (JOINs against the discogs-cache `artist`/`artist_url` tables) was written against `discogs/cache_service.py`'s known schema usage but has not been exercised end-to-end — the local discogs-cache PostgreSQL instances available in development are empty of that schema. Unit-tested throughout (mocked PG); the operator's first `--limit`-bounded run against a populated cache is the de facto integration test.

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

## Compilation Track Identity Backfill (`scripts/backfill_compilation_track_identity.py`)

Populates `lml_cache.compilation_track_identity` (LML#1020, Layer 2 of #271's Option B split): for each `library.db` `compilation_track_artist` (CTA) credit, resolves the credited artist to a Discogs identity — and, where `DATABASE_URL_MUSICBRAINZ` is configured, a MusicBrainz identity — and writes an attempt row per source, misses included, so `--retry-misses` is a `WHERE external_id IS NULL` predicate rather than a separate failure-tracking table. Full design (the F1–F4 findings that corrected the ticket's stated premises, the D1–D6 decisions) is in `docs/plans/lml-1020-per-track-identity-matcher.md`.

The Discogs leg reuses `BareNameArtistResolver` — but over HTTP, via `POST /api/v1/artists/resolve/bulk` against the running LML service, never by importing `DiscogsService`/`DiscogsCacheService` directly: the shared Discogs rate bucket, the LML#927 bulk-reservation semaphore, and the LML#755 saturation breaker all live inside that process, so a standalone script holding its own Discogs client would be an uncoordinated N+1th limiter against the same token. A joint credit ("The Bug feat. Flowdan") is attempted whole first (joint credits are frequently real Discogs entities) and only split on a miss. The MusicBrainz leg is cache-local trigram (`lookup/external_search.py`'s `fetch_mb_artist_candidates`), never a live MB API call, gated by a similarity floor and an ambiguity band so a near-tied pair records a miss rather than a coin-flip.

Position recovery (D5) is a separate deterministic `LATERAL` join against the LML#1019 recall index (`lml_cache.compilation_track_location`) — CTA carries no position column at all (F1), so 100% of positions come from this join, never from CTA. Yield is reported along two independent axes: recall-index coverage (did the comp match a Discogs release) and CTA↔Discogs credit-string agreement within a covered comp.

**Two independent switches, two distinct names**:

| Flag | Controls | Default |
|---|---|---|
| `--dry-run` | Whether the run writes `lml_cache.compilation_track_identity` rows | off (rows **are** written) |
| `--live` | Whether the resolve endpoint mints into `entity.identity` | off (**no** minting) |

`--live` matches the sibling artist-resolve drain's spelling. A default run populates this backfill's own table and mints nothing into the discogs-cache-owned `entity.identity` — the useful default, and the safe one, since a wrong mint is COALESCE-never-clobber and does not self-correct. **The production drain is a human-triggered post-merge step**, run off-peak against prod for the same reason the sibling drain is (`docs/scripts.md`'s Bulk Artist-Resolve Drain section above): prod is the only place all Discogs traffic coordinates through the shared limiter + breaker.

```bash
# Daily cadence: only credits with no existing row for a source yet.
LML_API_KEY=... LML_BASE_URL=https://<prod-lml> DATABASE_URL_DISCOGS=... \
  uv run python -m scripts.backfill_compilation_track_identity --incremental

# Monthly cadence: reprocess every CTA credit against both sources.
uv run python -m scripts.backfill_compilation_track_identity --full

# Re-attempt only credits whose existing row is a miss.
uv run python -m scripts.backfill_compilation_track_identity --retry-misses

# Re-run ONLY the D5 position-recovery join (zero external calls, no library.db
# needed) after the recall index grows:
uv run python -m scripts.backfill_compilation_track_identity --recover-positions

# Authorize minting after a dry-HTTP run's spot-check:
uv run python -m scripts.backfill_compilation_track_identity --incremental --live
```

Exactly one of `--incremental` / `--full` / `--retry-misses` / `--recover-positions` is required. Other flags: `--library-db` (path to `library.db`, default `library.db`), `--limit` (per-session work budget: cap the SELECTED credits processed per source this run — applied after population selection, so `--incremental --limit 5000` does the next 5,000 unattempted credits), `--base-url`/`--api-key` (default to `$LML_BASE_URL`/`$PRODUCTION_URL` and `$LML_API_KEY`, matching the sibling drain), `--timeout` (per-request seconds, default 120). Every run ends by logging the sibling drain's seeded `sample_spot_check` table over this run's `api_search` resolutions — that emitted sample is what the human review before a `--live` run reviews. Unlike the sibling artist-resolve drain, this script does not drive `run_drain`'s JSONL-logged resume: every attempt is already a durable PG row written page-by-page (D2), so a crash loses at most one in-flight page (≤25 credits) and the next `--incremental` invocation picks up exactly where the process left off — see the module docstring for the full reasoning.

Retires `scripts/match_compilations.py` (its matching cascade moved to `scripts/_lib/release_matching.py`, a genuine shared-script helper once `scripts/build_compilation_track_location.py` became its second consumer; its standalone JSON-writing CLI had no remaining caller) and `scripts/merge_cta.py` outright (it SSH-wrote to tubafrenzy MySQL, which the cross-cache-identity pivot's no-cross-service-writes rule forbids).

## Track-Cache V/A Purge (`scripts/purge_va_apple_track_cache.py`)

Clears pre-LML#1139 Various-Artists rows from `lml_cache.track_streaming_url_cache` (the LML#893 L1 track-URL cache). **This runs in the same deploy as the LML#1139 guard, not after it.** The table is hit-only and TTL-less and is peeked in `lookup/enrichment/apple_probe.py` *before* the live Apple probe, so a wrong V/A deep-link cached before the guard shipped would serve forever — on exactly the repeat-play traffic that recurs most — and the guard would never get a chance to re-adjudicate it. Ship the guard without the purge and the bug stays fully live from cache.

Selection is coarse-net-plus-pure-arbiter (donor: `scripts/audit_va_writeback_pollution.py`): an intentionally unanchored superset regex bounds the SQL scan, then `wxyc_etl.text.is_compilation_artist` — the exact predicate the guard uses, on the exact string the guard sees — decides. Real artists like `various production` and `the various` do match the regex and are rejected by the arbiter. The service is imported as `APPLE_MUSIC_TRACK_SERVICE` (`"apple_music_track"`), never spelled as a literal.

**Safety:** dry-run by default; `--execute` performs the DELETE. Every purged row's full tuple is written to a recovery CSV (`--out`), because the purge knowingly over-deletes (it keys on the query artist alone, while the guard also requires a V/A candidate and an album-less pass — so correct album-cleared V/A links go too) and those URLs are expensive-to-replace API-derived data. Two properties make that recoverable in practice:

- **The CSV is the deleted set, not an estimate of it.** The execute path is a single `DELETE ... RETURNING` inside one transaction, with the CSV written before commit. A pre-DELETE `SELECT` followed by a separate `DELETE` would be two autocommit statements on two pooled connections, and the guard does *not* block album-constrained V/A cache writes — so live `/lookup` traffic could insert a row for a purge key in the gap and have it deleted unrecorded. It also means the row count in the log is the DELETE's own count; that is the number to record on the PR for wave reconciliation.
- **Waves cannot clobber each other.** `--out` defaults to a timestamped per-invocation path, and an existing path is refused rather than truncated. Copy each wave's CSV somewhere durable anyway.

Targets the shared discogs-cache PG via `DATABASE_URL_DISCOGS`. **Prod writes require explicit authorization and run staging-first.**

**Run it in waves, after raising the probe ceiling.** `docs/env-vars.md` (LML#904) records that at the default `LML_APPLE_MUSIC_RATE_PER_MIN=60` roughly **56% of `find_track_url` probes time out** on LML's own self-throttle and return null — and nulls are never cached (`url NOT NULL`), so a purged key does not reliably re-fill on next play; it re-probes and mostly re-nulls until it wins the throttle. Roll the rate up (60 → 300 → 600) first, then use `--limit` so the re-probe load arrives gradually. `--after` is the resume cursor (an exclusive lower bound on `artist_normalized`); each wave logs the value to pass next. The cursor advances past every key *examined*, not just the purged ones, so a wave whose keys are all rejected by the arbiter still makes progress.

```bash
# 1. dry-run (default): report + write the recovery CSV, delete nothing
uv run python -m scripts.purge_va_apple_track_cache

# 2. first wave (staging first), 500 artist keys
uv run python -m scripts.purge_va_apple_track_cache \
  --limit 500 --out /tmp/lml-1139-wave1.csv --execute

# 3. next wave — resume past the last key the previous wave examined
uv run python -m scripts.purge_va_apple_track_cache \
  --limit 500 --after 'various artists - jazz' \
  --out /tmp/lml-1139-wave2.csv --execute
```

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

## Per-Consumer API Key Admin (`scripts/api_keys/`)

Seed / revoke / list CLI for `lml_cache.api_keys` (the [LML per-consumer API keys plan](plans/lml-per-consumer-api-keys.md)), which replaces the single shared `LML_API_KEY` with one row per consumer (tubafrenzy, backend-service, wxyc-canary, the operator drain script) so rotation is issue-new -> confirm -> revoke-old instead of a synchronized cutover. `core/auth.py:require_lml_key` dual-accepts a resolving table-backed key alongside the still-live legacy `LML_API_KEY` for the length of the migration.

**Safety:** `seed` prints the plaintext token to stdout **exactly once**, with a "will not be shown again" warning, and never logs it -- only its SHA-256 hash reaches `lml_cache.api_keys`. Hand the plaintext to the consumer's own secret store immediately. `revoke` is idempotent (a second revoke of the same id logs a WARNING, not an error). `list` never prints `key_hash` or the plaintext -- it is the direct operational answer to "can we delete this key yet," via `last_used_at`. Targets the shared discogs-cache PG via `DATABASE_URL_DISCOGS`; each subcommand bootstraps the schema (idempotent `IF NOT EXISTS`) before its query.

```bash
# mint a new key for a consumer -- prints the plaintext EXACTLY ONCE
uv run python -m scripts.api_keys seed --caller wxyc-canary --note "initial rollout"

# revoke by id (idempotent)
uv run python -m scripts.api_keys revoke --id 4

# list operational metadata (never key_hash or plaintext)
uv run python -m scripts.api_keys list
```

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

**Shared-token safety.** `get_master`/`get_release` route through `DiscogsService._request_with_retry`, so every call is bounded by the service's own (per-process) 50/min rate limiter and shielded by the [LML#755](https://github.com/WXYC/library-metadata-lookup/issues/755) saturation breaker. That limiter is per-process — the drain runs as a standalone script with its **own** breaker (driven only by its own requests), so it does **not** coordinate with the live service's limiter and shares the prod Discogs token uncoordinated: run **off-peak** and **never** concurrently with a bulk backfill campaign (BS#1631-class). Progress is checkpointed per master to a JSONL file: terminal states (`resolved`, `no_main_release`, `trackless`) are never retried, while `dead`/`error` (ambiguous/transient) are re-attempted on the next run — so an interrupted or re-run drain never re-spends API budget on a settled master.

**Pause on breaker shed** ([LML#874](https://github.com/WXYC/library-metadata-lookup/issues/874)). Because the drain owns its own breaker, that breaker *will* trip mid-run when a competing prod burst pushes the shared token's `X-Discogs-Ratelimit-Remaining` to the floor. On a shed the drain does **not** mark the master `dead` and march on (the pre-fix bug: one proactive trip shed the whole backlog to `dead` in under a second and exited). Instead every worker **pauses one cool-down and retries the shed master**, so a transient blip is ridden out. Only after `--max-breaker-pauses` *consecutive* pauses with no master settling in between (a sustained flood) does the run stop cleanly, leaving the remainder non-terminal for a later run. The end-of-run summary reports a `_paused` count alongside the outcome buckets.

`--limit` caps masters per run for a smoke batch; `--concurrency` bounds in-flight masters on top of the service limiter; `--max-breaker-pauses` (default 8) caps consecutive breaker pauses before the run bails; `--pause-seconds` (default: the breaker cool-down + 2s) sets the pause length.

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

**Shared-token safety.** The Apple JWT creds (`APPLE_MUSIC_TEAM_ID`/`KEY_ID`/`PRIVATE_KEY`, from `os.environ` or a local `.env`) are prod's and their per-process limiter is not coordinated with the live service's. `AppleMusicClient` already enforces its own internal ceiling — an `asyncio.Semaphore(5)` plus an `AsyncLimiter` of ~60 calls/min — so `--concurrency` / `--rate-per-min` can only make this resolver **more** conservative than that built-in 5-concurrent / ~60-per-minute cap (they bind only when set *below* it); they cannot make it faster. Run **off-peak** and set them below the ceiling to leave headroom for prod's own enrichment (a 429 or 5xx reuses the client's retry/backoff; a 403 — e.g. an auth/egress failure — is not retried and settles as `no_match`). Progress is checkpointed per album to a JSONL file: terminal states (`matched`, `no_match`) are never retried, while `api_error` is re-attempted on the next run — so an interrupted or re-run resolve never re-spends Apple budget on a settled candidate. Note the authenticated client *swallows* transient HTTP errors (exhausted 429/5xx, transport, bad JSON) to an empty result, so those return `None` and settle as `no_match` (terminal), **not** `api_error`; re-probing a candidate frozen by transient contention needs a fresh checkpoint (hence off-peak). `--limit` caps candidates per run for a smoke batch; `--dry-run` resolves + tallies + checkpoints but emits no output TSV.

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

## YouTube Music Coverage Drain (`scripts/ytm_coverage_drain.py`)

Resolves verified `music.youtube.com/browse/<browseId>` album links for a set of canonical `(artist, title)` candidates (epic [LML#830](https://github.com/WXYC/library-metadata-lookup/issues/830), impl [LML#1056](https://github.com/WXYC/library-metadata-lookup/issues/1056), productionized [LML#1070](https://github.com/WXYC/library-metadata-lookup/issues/1070); GO from the [#833](https://github.com/WXYC/library-metadata-lookup/issues/833) spike). Uses `YouTubeMusicClient` (`clients/streaming/youtube_music.py`) — unauthenticated `ytmusicapi` album search, scored through the shared production matcher (`find_best_source_match`, 80/80 floor). Per-candidate exceptions are isolated (a malformed search becomes a miss, never aborts the run).

**Candidate source (DECIDED, #1070).** Exactly one of `--from-db` / `--sample-csv` is required. `--from-db` is the production source: a pending-scan over `streaming_availability.db.albums` (`ResultsDB.get_pending("youtube_music", limit)`), which already excludes both resolved (`found`) and previously-attempted (`not_found`) rows — the drain is **resumable for free**. Because `albums` already holds the canonical `(artist, title)`, the row's own `id` is carried as the write target, eliminating the names-join entirely on this path. `--sample-csv` remains the credential-free dry-run/sample harness; wiring `entity.release_identity` → discogs-cache as a candidate source was considered and rejected (same-or-narrower universe, needs prod credentials + a human go-ahead mid-run).

**Write path — Option A.** Persistence is fill-only into `streaming_availability.db`: `ResultsDB.update_youtube_music_url` (`WHERE youtube_music_url IS NULL`) for matches, and `ResultsDB.mark_youtube_music_not_found` (same guard) for misses — so a re-run never re-searches an already-attempted candidate and never downgrades a resolved row. The rest of the chain already exists: `scripts/export_streaming_links.py` carries `albums.youtube_music_url` into `library.db.streaming_links` and `/lookup` surfaces it — this drain is the producer. On `--from-db`, each outcome writes by the candidate's own `album_id` (no names-join); on `--sample-csv`, a match maps to its `albums` row by normalized `(artist, title)` (`get_album_id_by_names`) and an unmatched candidate is tallied and skipped, never wrong-written — a `--sample-csv` miss has no `album_id` and so is skipped (not markable).

`ytmusicapi` is the optional `drain` extra (not a runtime dep; lazy import) — run under `--extra drain`. `--execute` is opt-in; the default is dry-run (writes nothing). Persisting is the operator's action (off-peak, fill-only, one bulk consumer at a time), followed by a `POST /admin/upload-streaming-db` republish.

```bash
# production dry-run: pending-scan + coverage report, write nothing
uv run --extra drain python -m scripts.ytm_coverage_drain \
  --from-db --limit 200 --results-db streaming_availability.db

# production persist (opt-in, fill-only, resumable pagination): --limit is the
# get_pending page size; each --execute run marks found + not_found and shrinks
# the next run's pending set, so repeated invocations walk the whole backlog
uv run --extra drain python -m scripts.ytm_coverage_drain \
  --from-db --limit 200 --execute --results-db streaming_availability.db

# sample dry-run (credential-free, local CSV)
uv run --extra drain python -m scripts.ytm_coverage_drain \
  --sample-csv resolved_names.csv --limit 200 --concurrency 4 \
  --report-json /tmp/ytm_drain_report.json
```

## Track-Recall Gap Census (`scripts/measure_track_recall_gap.py`)

Measurement-only coverage census for [LML#1264](https://github.com/WXYC/library-metadata-lookup/issues/1264)'s acceptance criterion #1: "the gap is measured before it is fixed." LML has track-level recall for exactly two cases — V/A compilations (`lml_cache.compilation_track_location`) and songs with no artist (`SONG_AS_TRACK`) — so a track on a single-artist release, requested with its artist, is unreachable. This script writes nothing and touches no matcher, strategy, or `/lookup` behavior; it answers "how big is the hole."

Four numbers, in order:

1. Comp-shelf vs artist-shelf split of `library.db`, via the same `wxyc_etl.text.is_compilation_artist` classifier `scripts/build_compilation_track_location.py` gates on — not a hand-rolled `LIKE 'Various Artists%'` guess, which the script also reports side by side (`comp_shelf_naive_like_count`) because the two counts diverge: the classifier catches shelf forms like bare `"V/A"` and lowercase `"various"` the naive heuristic misses.
2. **Admission** — of the artist-shelf rows, how many can the Discogs cache hold a release for *at all*, by simulating the production cache filter (`LibraryPairIndex`) against the Discogs source. This is the structural question, and it is asked before any matcher is consulted.
3. **Resolution** — of the releases that filter admits, how many artist-shelf rows resolve to a specific one at the same 80/80 floor `clients/streaming/matching.py::find_best_typed_match` already applies for artwork resolution (LML#478), reused read-only through its existing `query_artist` variant-iterable support with no change to the floor; and how many of those carry a cached tracklist (`release_track` rows). **The tracklist check is non-discriminating against a full Discogs source — never read as coverage; see the caveats below.**
4. The headline — artist-shelf rows that could gain track-level recall with **no new data collection** vs. rows that would need some, with the latter **partitioned** by which remedy moves it: no cached release reaches the row at all (structural — no candidate exists to score, so nothing inside LML reaches it); a release *is* admitted but cleared no title floor (a matcher change moves this); the row resolves but its release carries no tracklist. The three sum to "would need new collection" exactly, and are pinned to. Reported alongside but deliberately *outside* the partition: rows with no release under their **own** pair (the figure usually quoted, which overlaps "could gain recall" by the handful of rows a *sibling* shelf row's admission rescues), and that rescued count itself — the only measure of whether the two legs' asymmetry below actually pays off.

### Which filter this mirrors — and the decoy in this repo

Every Discogs-side number is downstream of one question: which releases does the cache admit? The rule that governs production lives in **another repo** — discogs-etl's `rebuild-cache.sh` forwards `--library-db` to `discogs-xml-converter`, whose [`src/library_pairs.rs::LibraryPairs`](https://github.com/WXYC/discogs-xml-converter/blob/main/src/library_pairs.rs) admits a release when its normalized title is a library title **and** one of its credited artists is in that title's artist set. Pair-wise; both sides folded through `wxyc_etl::text::to_match_form`; built from `SELECT artist, title FROM library`, so primary `library.artist` **only**; probed against both credit tiers (`release.artists` chained with `release.extra_artists`, i.e. `extra = 0` and `extra = 1`).

`scripts/build_filtered_discogs.py`, in *this* repo, is **not** that filter. It is a local dev tool: artist-only, unions `alternate_artist_name`, and writes a `wxyc.*` schema LML never queries (LML's runtime SQL is unqualified `public.*`). It is in-repo, importable, and reads exactly like the thing to mirror — and the census's first version mirrored it, through a code review that pushed it *further* toward the decoy. Mirroring it measures a database nobody runs and overstates coverage by roughly the difference between a ~4M-release cache and a ~50K one. `tests/unit/test_measure_track_recall_gap_filter_parity.py` holds the distinction in place behaviourally, on a fixture where the two rules give opposite answers, because prose did not survive one review cycle.

### The one place the two legs deliberately disagree

Admission reads `library.artist` alone, because the production filter does. Resolution reads `artist_variants` — the union with `library.alternate_artist_name` — because LML's own runtime matcher does (`artist_matches_item`). That asymmetry is not an oversight to be tidied away: **it is the census's finding.** The substrate that decides what LML can see folds names more weakly than the consumer that searches it, which is the wrong way round, and keeping the two legs on different artist sets is how the script states it rather than hides it.

### Caveats

**The methodology caveats live in the script's module docstring, and the script prints them alongside the numbers they qualify — that is the canonical copy.** In summary: the source is a **full, unscoped** Discogs dump, so running the production filter over it yields what that filter *would* admit from this dump vintage — an **upper bound** on the real prod cache, which is built from a different vintage and pruned afterwards by `verify_cache`. The error direction is safe for the structural finding: a row the filter admits nothing for from the full dump cannot be in any cache built from any vintage of it. The tracklist check **cannot discriminate** against a full dump (19,341,286 of 19,341,287 releases in the 2026-08 local dump carry `release_track` rows). The `lml_cache.library_release_override` pin-coverage figure is quoted from the `identity/bulk_resolve.py` code comment rather than derived, and is labeled as such. `ADMISSION_MODEL_NOTE`, `TRACKLIST_CHECK_CAVEAT` and `DOCUMENTED_PIN_COVERAGE_NOTE` travel with the figures in both the console report and the `--out` JSON, so a reader who meets a number without this doc still meets its caveat.

Omit `--discogs-url` (and unset `DATABASE_URL_DISCOGS`) to run the library-only half of the census with the Discogs-side measurement cleanly skipped, rather than pointed at nothing. Such a run reports `discogs_measurement: "skipped"`, `null` headline figures, and **no Discogs field at all** in the `--out` JSON — the leg is one optional structure rather than a handful of fields that go to zero, so an unmeasured zero cannot be serialized as though it were a finding.

The admission sweep streams the whole `release` table (~19.3M rows, ~60s against the 2026-08 local dump) and folds each title in Python. That is deliberate: no SQL expression is equivalent to `to_match_form` — `unaccent()` is neither a superset nor a subset of NFKD-plus-strip-combining — so any server-side prefilter would silently drop exactly the diacritic-differing pairs the fold exists to make collide.

```bash
# library-only census (no Discogs access needed)
uv run python -m scripts.measure_track_recall_gap --library-db library.db

# full census against a local unscoped Discogs dump (upper bound; read the caveats above)
uv run python -m scripts.measure_track_recall_gap \
  --library-db /path/to/prod-snapshot/library.db \
  --discogs-url postgresql://postgres@localhost:5432/discogs_full \
  --out /tmp/lml_1264_census.json
```
## Golden Corpus Builder + Re-baseliner (`scripts/build_golden_corpus.py`, `scripts/rebaseline_golden_corpus.py`)

Offline maintenance tools for the LML#1233 Layer 3 golden corpus in `tests/e2e/golden/`. Neither runs in CI, and the corpus itself reads only the three checked-in JSON files they write — see [`testing.md`](testing.md#golden-corpus-lml1233-layer-3) for what a case is, what it asserts, and the re-baselining rule.

The split is deliberate: **regenerating data** and **recording behavior** are different acts, and only the second can move a baseline.

`build_golden_corpus.py` writes `library.json`, `discogs.json` and case *skeletons*. It needs two local, read-only sources, neither available in CI: a production-shaped `library.db` (gitignored) and a full Discogs dump in PostgreSQL (the `release` / `release_artist` / `release_track` / `release_track_artist` shape discogs-cache builds — `discogs_full` on a local `:5432` is the usual one). Sampling is deterministic (`SAMPLE_SEED`), so a re-run against the same sources reproduces the same corpus; a catalog refresh moves rows, which is exactly the kind of change that should appear in a reviewed diff. It **never writes an expectation**: existing ones are carried forward verbatim by id and new cases come out `"expect": null`, so a regeneration cannot launder a regression into the baseline.

The two `LIKE` predicates in `_RESOLVE_SQL` are redundant with the equalities beside them and are there only to hit the `gin_trgm` indexes — without them PostgreSQL sequential-scans a 179M-row `release_track` per lookup. Keep both halves.

`rebaseline_golden_corpus.py` drives every case through the real app (same env pins as the test tier, via the shared `corpus.pinned_environment`) and records the verdicts. It refuses to rewrite a `"frozen": true` case — those pin failures that already reached production once — printing the drift and exiting non-zero instead; accepting the new behavior means hand-editing that case and saying why. It never runs implicitly: there is no pytest plugin and no environment variable CI could trip.

```bash
# regenerate fixtures (developer machine with both sources)
uv run --with 'psycopg[binary]' python -m scripts.build_golden_corpus \
  --library-db library.db --discogs-dsn 'postgresql://localhost:5432/discogs_full'

# preview what would move, write nothing
uv run python -m scripts.rebaseline_golden_corpus --dry-run

# record; then commit tests/e2e/golden/cases.json on its own, with the reason
uv run python -m scripts.rebaseline_golden_corpus

# reformat a hand-edited cases.json through the canonical writer
uv run python -m scripts.rebaseline_golden_corpus --format-only
```

`psycopg` is not a project dependency — the builder imports it lazily and tells you to `uv run --with 'psycopg[binary]'` if it is missing. The re-baseliner needs nothing beyond the dev extra.
