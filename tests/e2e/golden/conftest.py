"""Fixtures for the LML#1233 golden corpus.

Deliberately separate from `tests/e2e/conftest.py`. That one wires ten
hand-written seed rows and a mock Discogs whose `if "doga" in album_lower`
branches are matching logic living in the test double; this one wires 380+ real
catalog rows and a routing-table fake, because a corpus that asserts recall
cannot have a double that decides recall.

**xdist safety.** Everything here is either per-worker (the SQLite file, built
under `tmp_path_factory`, which hands each worker its own directory) or
per-test (the connection, the fake, the env pins, the dependency overrides).
Nothing is shared across processes and nothing is written outside `tmp_path`.

**Determinism.** Two pieces of process-global state would otherwise make a
verdict depend on what ran before it, and both are reset around every case:
the module-level TTLCaches in `library/db.py` (keyed on query text alone, so a
query shared with `tests/e2e/test_lookup_e2e.py`'s *different* catalog would be
served that catalog's answer), and the `lru_cache`d `Settings`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient

from config.settings import get_settings
from library.db import LibraryDB, clear_library_caches
from tests.e2e.golden import corpus


def _case_of(request: pytest.FixtureRequest) -> corpus.Case | None:
    """The `Case` this test is parameterized with, if any.

    Read off the callspec rather than taken as a fixture argument because
    `case` is a `parametrize` value on the test, not a fixture -- and the
    settings pin has to be in place before the app is built.
    """
    callspec = getattr(request.node, "callspec", None)
    if callspec is None:
        return None
    return callspec.params.get("case")


@pytest.fixture(scope="session")
def golden_library_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The seeded SQLite catalog, built once per xdist worker.

    Seeding is the only part of a case that is not cheap (an FTS rebuild over
    the whole fixture), so it is hoisted to session scope. Opening a fresh
    connection per case costs microseconds and keeps every case's connection
    inside its own event loop.
    """
    path = tmp_path_factory.mktemp("golden") / "library.db"
    return corpus.build_library_db(path, corpus.load_library_rows())


@pytest.fixture(scope="session")
def golden_discogs_fixture() -> dict[str, Any]:
    """Raw parsed `discogs.json`, read once per worker. Never mutated.

    Kept for the tests that need the raw shape (route tables as plain dicts);
    the per-case fake takes the parsed `DiscogsUniverse` instead.
    """
    return corpus.load_discogs_fixture()


@pytest.fixture(scope="session")
def golden_discogs_universe() -> corpus.DiscogsUniverse:
    """`discogs.json` parsed into models once per worker. Never mutated."""
    return corpus.load_discogs_universe()


@pytest.fixture
def golden_discogs(golden_discogs_universe: corpus.DiscogsUniverse) -> corpus.FakeDiscogsService:
    """A fresh fake per case, so its call log describes only that case.

    Fresh fake, shared universe: the call log is what has to be per-case, and
    the 583 parsed models behind it are identical for every one.
    """
    return corpus.FakeDiscogsService(golden_discogs_universe)


@pytest.fixture(autouse=True)
def _pinned_settings(request: pytest.FixtureRequest) -> Iterator[None]:
    """Pin every feature flag, plus the credentials, for the duration of a case.

    The corpus records behavior under the repo's own checked-in defaults, not
    under production's Railway variables: CI cannot read those, and a baseline
    that depends on an environment it cannot observe is not a baseline. A flag
    default that flips therefore fails the corpus, which is the intended
    signal -- the change is real and belongs in a reviewed rebaseline.
    """
    case = _case_of(request)
    monkeypatch = pytest.MonkeyPatch()
    for name, value in corpus.pinned_environment(case.settings if case else None).items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    clear_library_caches()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        clear_library_caches()


@pytest_asyncio.fixture
async def golden_library_db(golden_library_path: Path) -> AsyncIterator[LibraryDB]:
    """A real `LibraryDB` over the seeded file, opened through `connect()`.

    `connect()` rather than assigning `_conn` directly: it is what performs the
    `PRAGMA table_info` column detection and the optional-table probes, so
    going around it would run the corpus against a `LibraryDB` that believes
    the catalog has no `alternate_artist_name` and no `streaming_links`.
    """
    database = LibraryDB(golden_library_path)
    await database.connect()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def golden_client(
    golden_library_db: LibraryDB,
    golden_discogs: corpus.FakeDiscogsService,
) -> AsyncIterator[AsyncClient]:
    """The real FastAPI app, wired to the fixture catalog and the fake Discogs.

    The wiring itself lives in `corpus.golden_app_client`, shared with
    `scripts/rebaseline_golden_corpus.py` so the tool that *records* a verdict
    and the tier that *asserts* it cannot drift apart in what they wire up.
    """
    async with corpus.golden_app_client(golden_library_db, golden_discogs) as client:
        yield client
