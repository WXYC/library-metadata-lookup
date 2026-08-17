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

Several suites are *discovery nets*: they sweep the source tree (glob + regex/AST) and require every hit to be on a hand-maintained roster, so a new module can't sit outside a guard unnoticed — `tests/unit/test_lifespan_bootstrap_totality.py` (every `entity/*.py` bootstrap is lifespan-wired and sidecar-registered), `tests/integration/test_pg_fixture_guard_adoption.py` (every lml_cache-dropping fixture routes through the shared data-safety guard), and the fd-leak net in `tests/unit/test_fd_leak_regression_241.py` (no httpx construction on the lookup hot path). New nets are written by imitating old ones, so each should carry the family's three accessories: a **vacuity guard** (assert the sweep still finds known members — a drifted pattern must fail loudly, never pass vacuously; e.g. `test_discovery_finds_the_known_bootstraps`, `test_discovery_finds_known_lml_cache_dropping_suites`), a **reverse/stale check** (roster entries that no longer match anything real must be flagged, so the roster stays an accurate map — e.g. `test_no_stale_budget_entries` in the module-budget guardrail), and an **exemption roster** (deliberate opt-outs live in a named constant with a comment per entry — e.g. `_LIFESPAN_EXEMPT`, `_SIDECAR_GENERATOR_EXEMPT` — never as a weakened sweep pattern).
