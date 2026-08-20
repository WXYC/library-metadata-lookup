# Testing

## Unit Tests

All external services (LibraryDB, DiscogsService) are mocked. Run frequently:

```bash
uv run pytest tests/unit/ -v
```

## Test Patterns

- Use factories from `tests/factories.py`: `make_library_item()`, `make_discogs_result()`, `LOOKUP_BODY`
- Mock `discogs_service` with `AsyncMock` and construct `DiscogsSearchResponse`/`DiscogsSearchResult` models
- `DiscogsSearchResult` requires `release_id: int` and `release_url: str` (no defaults)
- Mock `LibraryDB` with `AsyncMock` including `search`, `find_similar_artist`, `connect`, `close`
- Use `unittest.mock.patch` for `lookup.orchestrator.lookup_releases_by_track` in pipeline tests
- `test_orchestrator.py` tests `perform_lookup()` end-to-end with mocked dependencies
- `test_orchestrator_helpers.py` tests individual helper functions in isolation

## Bug Fix Protocol

For every lookup bug where a search fails to find the correct release:

1. Create a **unit test** in `tests/unit/` that reproduces the bug with mocked data
2. Create an **integration test** in `tests/integration/` that verifies the fix against real APIs
3. Integration test should assert that false positives are excluded AND correct results are included

## TDD (Required)

All code changes in this repo follow test-driven development. This is not optional.

1. **Red**: Write a failing test that describes the desired behavior. Run it and confirm it fails.
2. **Green**: Write the minimum implementation to make the test pass.
3. **Refactor**: Clean up the implementation while keeping tests green.

Concretely this means:
- New features: write tests for the new behavior first, watch them fail, then implement.
- Bug fixes: write a test that reproduces the bug first, confirm it fails, then fix.
- Refactors: ensure existing tests pass before and after. Add tests for any behavior not already covered.
- Do not write implementation code without a corresponding failing test preceding it.

## Pytest markers (architecture A)

Markers route CI by infrastructure, not taxonomy. See the WXYC test-patterns guide ([WXYC/wiki, plans/test-patterns.md, Section 3](https://github.com/WXYC/wiki/blob/main/plans/test-patterns.md#3-marker-conventions)) for the canonical vocabulary; LML uses two:

| Marker | Meaning | CI provisions |
|---|---|---|
| `pg` | needs a PostgreSQL service | `postgres:16-alpine` service container, `DATABASE_URL_TEST` |
| `external_api` | needs network access to a real third-party API (Discogs — credential-gated; Wikipedia — keyless, runtime-skip-on-unanswerable instead) | `DISCOGS_TOKEN` secret for the Discogs suites; no secret for the Wikipedia suite |

Default `pytest` (no `-m`) runs every unmarked test across `tests/unit/`, `tests/integration/`, and `tests/e2e/`. Tier directories are documentation; CI routes by markers.

**Default Tests** runs `pytest -v --cov=...` -- pyproject's `addopts = "-m 'not pg and not external_api'"` excludes the infra-tagged tests.

**External API Tests** runs `pytest -v -m external_api`. The `tests/e2e/discogs/*` suite, the `TestDiscogsApiSearch` / `TestEntityResolution` classes in `tests/integration/test_api_discogs.py`, and the `type=artist` payload-shape smoke in `tests/integration/test_search_artists_live.py` hit the real Discogs API. Skip behavior differs by tier (PR runs from forks may have no secret access): the e2e suite self-skips at collection (module-level `pytest.skip`) when no Discogs credentials are configured — `DISCOGS_TOKEN` or the `DISCOGS_API_KEY`/`DISCOGS_API_SECRET` pair; the integration classes and the live smoke check `DISCOGS_TOKEN` alone and skip at runtime inside the test body. The smoke also runtime-skips when the probe is unanswerable (rate-limited, network, LML#755 breaker shed) and goes red when Discogs answers but the contract moved: `id`/`title` payload-shape drift, or the pinned Popsicle overload family disappearing from page 1 (data drift — the assert message says to pick a new stable family).

`tests/integration/test_wikipedia_client_live.py` (LML#513/#1192) is the same `external_api` marker, a different skip shape: Wikipedia's REST `/page/summary` endpoint is **keyless**, so there is no credential to check — the CI External API lane runs `-m external_api` unconditionally in one serial job regardless, so this suite runtime-skips on an unanswerable probe (a caught `WikipediaFetchError` — timeout, network error, transient non-200) rather than gating on an env var no environment would ever set. Goes red only on contract drift: Wikipedia's REST API no longer returning `type: "standard"` + a non-empty `extract` for a known-stable artist page (Stereolab), no longer marking a known-ambiguous term `type: "disambiguation"`, or no longer 404ing a nonexistent page.

**PG Tests** runs `pytest -v -m pg` against a `postgres:16-alpine` service container on port 5433. The `EntityStore` CRUD tests in `tests/integration/test_entity_resolution.py` run end-to-end against a fresh `entity` schema. The Discogs reconciliation tests skip themselves when the `release_artist` table is missing -- that table is part of the discogs-cache fixture and is too large to load in CI. `tests/integration/test_va_discogs_lookup.py` self-skips without `DATABASE_URL_DISCOGS`, which is intentional in CI.

**CI Marker Sync** invokes the reusable workflow at `WXYC/wxyc-etl/.github/workflows/check-ci-marker-sync.yml` to guarantee that every `@pytest.mark.<X>` actually used by a test is either re-selected by some CI `pytest -m` invocation or explicitly opted out via a `# ci-sync-skip: <marker> reason: <text>` comment in `pyproject.toml`. This guards against the silent-deselection bug pattern (WXYC/discogs-etl#103, WXYC/library-metadata-lookup#159).

## Meta-test conventions (discovery nets)

Several suites are *discovery nets*: they sweep the source tree (glob + regex/AST) and require every hit to be on a hand-maintained roster, so a new module can't sit outside a guard unnoticed — `tests/unit/test_lifespan_bootstrap_totality.py` (every `entity/*.py` bootstrap is lifespan-wired and sidecar-registered), `tests/integration/test_pg_fixture_guard_adoption.py` (every lml_cache-dropping fixture routes through the shared data-safety guard), the fd-leak net in `tests/unit/test_fd_leak_regression_241.py` (no httpx construction on the lookup hot path), and `tests/unit/test_unsampled_counter_documented.py` (every `capture_unsampled_counter` event name is classified in `docs/env-vars.md`'s `POSTHOG_RATELIMIT_EXEMPT_EVENTS` entry -- note it runs from its own paths-filtered workflow as well as `ci.yml`, since its reverse/stale half guards doc-only PRs that `ci.yml` skips). New nets are written by imitating old ones, so each should carry the family's three accessories: a **vacuity guard** (assert the sweep still finds known members — a drifted pattern must fail loudly, never pass vacuously; e.g. `test_discovery_finds_the_known_bootstraps`, `test_discovery_finds_known_lml_cache_dropping_suites`), a **reverse/stale check** (roster entries that no longer match anything real must be flagged, so the roster stays an accurate map — e.g. `test_no_stale_budget_entries` in the module-budget guardrail), and an **exemption roster** (deliberate opt-outs live in a named constant with a comment per entry — e.g. `_LIFESPAN_EXEMPT`, `_SIDECAR_GENERATOR_EXEMPT` — never as a weakened sweep pattern).

## Golden corpus (LML#1233 Layer 3)

`tests/e2e/golden/` is a checked-in corpus of frozen lookups: 142 queries, each recorded against a seeded catalog and a seeded Discogs universe, asserted on every PR. It exists for **per-commit attribution** — the production miss-rate alert of [`lml-1233-lookup-miss-monitoring.md`](plans/lml-1233-lookup-miss-monitoring.md) Layer 2 is lagging and confounded and answers *did something break*; this tier is deterministic and answers *which change broke it*, before merge. It additionally catches the failure `results_count` structurally cannot see: a lookup returning the **wrong** rows rather than none.

It runs in the **default** pytest job. No marker, no PostgreSQL, no network, no `library.db` — `pyproject.toml` already collects `tests/e2e/`, so there is no workflow, marker, or `check-ci-marker-sync` interaction. It adds roughly a second.

### What a case asserts

The verdict is four fields, and deliberately not the response:

| field | why |
|---|---|
| `miss_kind` | the Layer 1/2 vocabulary from `lookup/miss_kind.py`, re-derived from the wire response's `results`/`timeout`/`degraded` — a failing case and a production alert then describe the same thing |
| `song_not_found` | a caller reads it to decide whether to caveat, so a flip is user-visible even when the rows are identical (LML#1225) |
| `found_on_compilation` | same |
| `results` | the **ordered** result identities, `#<library id> <artist> — <title>`, with `#0 row-less:` marking the LML#628/#631 shape |

Excluded on purpose: artwork, streaming URLs, enrichment, identities, timings, `context_message`, and `search_type`. `search_type` is a lane-derived label with no reference to what was found (LML#1233 Finding 1), so pinning it would fail on a strategy-ordering refactor that changed no answer. The rest are excluded because a corpus that fails on an unrelated enrichment change teaches its readers to rebaseline without looking, which is what makes a corpus worthless.

`miss_kind` alone would not be enough. Three of the four regressions this tier is calibrated against return rows both before and after their fix and differ only in *which* rows, in *what order* — LML#801 (five wrong albums vs the one carrying the track), LML#717 (a wrong-artist row instead of the right row-less one), LML#1184 (a row-less compilation silently replacing two shelved albums). All three are `hit` on both sides.

### Stratification

Sampled against the production query-shape mix — 21 days of alert-scope `lookup_completed` (PostHog 551103, `environment='production' AND endpoint_family='lookup' AND low_priority=false`, excluding `library-enrich-artwork`; 5,413 requests, pulled 2026-08-20):

| shape | production | corpus | note |
|---|---|---|---|
| artist+album | 76.1% (13.0% zero-result) | 81 | the dominant shape; a recall regression shows here first |
| artist+album+song | 17.9% (2.2%) | 19 | |
| artist+song | 3.8% (0.0%) | 20 | over-represented — LML#801/#1184's lane |
| artist only | 1.9% (24.0%) | 12 | over-represented — cheap, and pins shelf ordering |
| song only | 0.3% (73.3%) | 10 | over-represented — LML#1225's lane |

The small shapes are deliberately over-weighted: three of the four calibration regressions live in the two lanes that are 4% of traffic, and sampling them proportionally would put one or two cases on the code most likely to break.

**Misses are over-weighted too** — 16 of 142 (11%) against production's 10.5%, and all of them deliberate rather than incidental: eight artists with no shelf presence at all, four tracks that exist nowhere, four albums the station does not hold. Every sampled hit case is a hit by construction, so without those the corpus could only ever notice recall getting *worse*. A false positive and a recall win look identical from the outside — both turn a miss into a hit — and the miss cases are the only thing that tells them apart.

Note that the corpus does **not** stratify on the caller segments of the plan's Finding 3. Caller class arrives as a header and changes admission and budget policy, not matching, so it is not a property of a query a corpus can hold fixed.

### Deliberately not stratifying on config

Cases run under the repo's own checked-in `Settings` defaults, pinned into the environment (`corpus.pinned_environment`) so a developer's `.env` cannot move a verdict locally that CI records differently. Every boolean `Settings` field is pinned to its declared default, derived from the model, so a new flag joins the pin automatically. A case that only means something with a flag on says so inline:

```json
"settings": {"lml_resolve_nonlibrary_release": true}
```

which puts the dependency in the reviewed diff instead of in an environment. Note that settings must be pinned through the **environment**, not `app.dependency_overrides[get_settings]`: most of the pipeline calls `config.settings.get_settings()` directly rather than through `Depends`, so a dependency override reaches the router and nothing under it.

The corpus therefore records default-config behavior. Production's Railway flag values are out of its scope, by construction: CI cannot read them, and a baseline that depends on an environment it cannot observe is not a baseline.

### Frozen cases, and why they cannot be rebaselined

Six cases are `"frozen": true`. Each pins a failure that already reached production once, cites its issue, and declares the catalog rows it needs (`requires_rows`) so it cannot pass vacuously if those rows are ever dropped from the fixture:

| case | pins |
|---|---|
| `lml801-aphex-twin-milkman` | LML#801 against a healthy cache: the track-bearing album is surfaced and *confirmed* |
| `lml801-aphex-twin-fingerbib-cold-cache` | the same gap through the mechanism that broke — no track route at all (LML#802's CTE prune), so the artist-only leg returns the whole 18-row shelf and validation has to widen past the top five to reach the eighth row |
| `lml717-lone-galaxy-garden` | a wrong-*artist* library row matched purely on album-title fuzz must not surface and preempt the non-library resolution |
| `lml1225-space-lizzard-battle-star` | the query-coverage gate must reject a tracklist entry that is a sub-phrase of the typed query |
| `lml1184-arabian-prince-strange-life` | a row-less compilation hit must not erase the artist's shelved albums |
| `lml1184-arabian-prince-nonsense-track` | the control the issue itself used — with Discogs finding nothing, the artist fallback still returns both shelved rows |

Each of the six was verified by reverting its fix in a working tree and confirming the case goes red.

Frozen cases also declare `requires_routes` — the exact fixture routes that must have served a candidate during the run (`"track:milkman|aphex twin"`). `requires_discogs` alone is too weak for them: `lml801-aphex-twin-milkman` is reachable both through its track route and through per-row validation over the shelf routes, so deleting the track route left the case green while it silently stopped testing the healthy-cache path. Naming the route catches that.

### Recording a verdict, and re-baselining

Two tools, split so that *regenerating data* and *recording behavior* are different acts:

```bash
# 1. Regenerate the fixture data. Needs library.db and a full Discogs dump in
#    PostgreSQL; writes library.json + discogs.json + case skeletons.
uv run python -m scripts.build_golden_corpus \
    --library-db library.db --discogs-dsn 'postgresql://localhost:5432/discogs_full'

# 2. Record verdicts. Prints every change; --dry-run writes nothing.
uv run python -m scripts.rebaseline_golden_corpus --dry-run
uv run python -m scripts.rebaseline_golden_corpus
```

`build_golden_corpus.py` **never** writes an expectation. Existing ones are carried forward verbatim and new cases come out `"expect": null`, so a regeneration cannot launder a regression into the baseline.

`rebaseline_golden_corpus.py` is built around three refusals:

1. **It refuses frozen cases.** If one moved, it prints it and exits non-zero without writing. Accepting the new behavior means hand-editing that case and saying why — the friction that stops a regression from being re-recorded as an improvement.
2. **It never runs implicitly.** No pytest plugin, no `--rebaseline` flag on the suite, no environment variable CI could pick up.
3. **It prints every change before writing.** The `cases.json` diff is the review artifact; commit it on its own, with the reason each verdict moved.

An improvement fails a case too. That symmetry is deliberate: a corpus that silently absorbs improvements will silently absorb regressions dressed as improvements, and from the outside those are the same event.

### A recorded verdict is not a claim that the verdict is right

The corpus records what LML does, which is not always what it should do. A case whose verdict is believed wrong is marked `"suspect": true` with a note beginning `SUSPECT`, so the judgement lives next to the data and the day it changes reads as "the fix landed" rather than "something broke".

One case carries that mark today. `track-miss-stereolab-zzyzx-marginal-fanfare` asks for a track that exists nowhere and gets back a confident, uncaveated hit — `found_on_compilation: true`, `song_not_found: false`, context `Found "Zzyzx Marginal Fanfare" by Stereolab on:` — pointing at *Peng!*. `TRACK_ON_COMPILATION`'s last-resort branch (`if not results and keyword_matches and not discogs_found_releases`, `lookup/strategies/track_on_compilation.py`) surfaces the best library keyword hit whenever Discogs returns nothing, and the compilation `Outcome` labels it confirmed. That is the LML#1225 user-visible failure — a confident row for a song that does not exist — reached through a path #1225's tracklist-gate fix does not cover, because no tracklist is ever consulted.

### Fixture shape

- `library.json` — real WXYC catalog rows, verbatim from `library.db`, **production ids preserved**. LML#801 is a story about rowid ordering, so renumbering would rewrite the property the case exists to pin.
- `discogs.json` — real Discogs releases with real tracklists (from a local full dump), plus routing tables saying which release ids each search returns. Real rather than synthesized because LML's gates key on token overlap between a query and track titles, and synthesized tracklists (`"<album> (part 1)"`) have a degenerate token distribution that makes the coverage veto in `discogs/matching.py` behave in ways real data never produces.
- `cases.json` — queries plus recorded verdicts.

`corpus.FakeDiscogsService` fakes exactly the I/O boundary and nothing above it. The routing tables are the honest model of a Discogs search: whether served by the API keyword index or the cache's pg_trgm scan, it is a *candidate generator*, and being loose is its job (LML#1225 root cause 3). One method is not a lookup — `validate_track_on_release` delegates to the production `discogs.service._scan_tracklist_for_match`, so `discogs/matching.py`'s title, coverage and artist gates run for real. A fake that answered validation questions itself would test the fake.

Fixture drift against live Discogs is accepted (LML#1233, "Risks"): this tier tests *this repo's algorithm given fixed inputs*, which is what per-commit attribution requires. Live-Discogs behavior stays covered by the `external_api` tier.

### What this tier cannot cover

- **LML#802 itself.** The bug is a `matching_tracks` CTE in `discogs/cache_service.py` that truncates before applying the artist filter — PostgreSQL, reachable only from the `pg` tier. The corpus reproduces its *effect* (an empty track search) in `lml801-aphex-twin-fingerbib-cold-cache`, not its cause. `tests/integration/test_entity_resolution.py`-style `pg` coverage is where the CTE itself is pinned.
- Anything whose behavior depends on a production-only flag value (see "Deliberately not stratifying on config").
- Timing, concurrency, and budget behavior — `miss_timeout` and `miss_degraded` are in the verdict vocabulary but no case produces them, because a corpus that raced a deadline would not be deterministic.
