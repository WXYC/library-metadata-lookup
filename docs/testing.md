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
| `external_api` | needs a real third-party API key (Discogs) | `DISCOGS_TOKEN` secret |

Default `pytest` (no `-m`) runs every unmarked test across `tests/unit/`, `tests/integration/`, and `tests/e2e/`. Tier directories are documentation; CI routes by markers.

**Default Tests** runs `pytest -v --cov=...` -- pyproject's `addopts = "-m 'not pg and not external_api'"` excludes the infra-tagged tests.

**External API Tests** runs `pytest -v -m external_api`. The `tests/e2e/discogs/*` suite and the `TestDiscogsApiSearch` / `TestEntityResolution` classes in `tests/integration/test_api_discogs.py` hit the real Discogs API; they self-skip at collection if `DISCOGS_TOKEN` is unset (PR runs from forks may have no secret access).

**PG Tests** runs `pytest -v -m pg` against a `postgres:16-alpine` service container on port 5433. The `EntityStore` CRUD tests in `tests/integration/test_entity_resolution.py` run end-to-end against a fresh `entity` schema. The Discogs reconciliation tests skip themselves when the `release_artist` table is missing -- that table is part of the discogs-cache fixture and is too large to load in CI. `tests/integration/test_va_discogs_lookup.py` self-skips without `DATABASE_URL_DISCOGS`, which is intentional in CI.

**CI Marker Sync** invokes the reusable workflow at `WXYC/wxyc-etl/.github/workflows/check-ci-marker-sync.yml` to guarantee that every `@pytest.mark.<X>` actually used by a test is either re-selected by some CI `pytest -m` invocation or explicitly opted out via a `# ci-sync-skip: <marker> reason: <text>` comment in `pyproject.toml`. This guards against the silent-deselection bug pattern (WXYC/discogs-etl#103, WXYC/library-metadata-lookup#159).
