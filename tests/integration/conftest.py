"""Integration test fixtures.

Provides a real LibraryDB backed by in-memory SQLite with FTS5,
seeded with representative catalog items.
"""

import os
from collections.abc import Iterable
from unittest.mock import AsyncMock

import aiosqlite
import asyncpg
import pytest
import pytest_asyncio

from config.settings import Settings
from discogs.models import DiscogsSearchResponse
from library.db import LibraryDB
from tests.factories import make_discogs_result

# ---------------------------------------------------------------------------
# PostgreSQL test pool (shared by the ``@pytest.mark.pg`` integration suite)
# ---------------------------------------------------------------------------

# Centralized DSN for the real PostgreSQL container the ``pg`` suite runs
# against. Modules that also need the DSN directly -- for ``source._dsn``
# wiring or ``monkeypatch.setenv("DATABASE_URL_DISCOGS", ...)`` -- import it
# from here instead of re-deriving the ``os.getenv`` default.
DATABASE_URL = os.getenv(
    "DATABASE_URL_TEST",
    "postgresql://discogs:discogs@localhost:5433/discogs",
)


async def _make_pg_pool(max_size: int):
    """Yield a real asyncpg pool, or ``pytest.skip`` when PostgreSQL is absent.

    Async-generator helper backing :func:`pg_pool` and :func:`pg_pool_large`.
    The skip-on-connect-failure contract keeps the ``pg`` suite green
    (all-skipped) on a host with no test PostgreSQL -- exactly what the
    previously-per-file copies did.
    """
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=max_size)
    except Exception as e:
        pytest.skip(f"Cannot connect to test PostgreSQL: {e}")
        return
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def pg_pool():
    """Real asyncpg pool (``max_size=3``); skips on connect failure.

    The common case across the ``pg`` integration suite. Modules needing the
    larger cap depend on :func:`pg_pool_large` instead.
    """
    async for pool in _make_pg_pool(max_size=3):
        yield pool


# ---------------------------------------------------------------------------
# Shared discogs-cache stubbing helpers (reconciler / #759 candidate-set SQL)
# ---------------------------------------------------------------------------

# IMMUTABLE f_unaccent wrapper mirroring discogs-cache, so trigram operators
# can ride a functional GIN index. Idempotent. Shared by every fixture that
# stubs discogs-cache tables on a bare PG (``test_entity_resolution.py``,
# ``test_artist_candidate_sets.py``) so the schema mirror can't drift
# per-file — the suites it backs exist to prove SQL that must stay
# predicate-compatible across two modules.
F_UNACCENT_WRAPPER_SQL = (
    "CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text "
    "AS $$ SELECT unaccent('unaccent', $1) $$ "
    "LANGUAGE sql IMMUTABLE PARALLEL SAFE"
)

# Stub DDL for the four discogs-cache tables read by the reconciler cascade
# legs (``scripts/entity_resolution/discogs.py``) and the #759 candidate-set
# queries (``discogs/cache_service.py``). One shared map so the stub shape
# the two suites test against can't silently diverge.
RECONCILER_TABLE_DDL: dict[str, str] = {
    "release_artist": (
        "release_id INTEGER, artist_id INTEGER, artist_name TEXT, extra INTEGER DEFAULT 0"
    ),
    "artist_member": "artist_id INTEGER, member_id INTEGER, member_name TEXT",
    "artist_alias": "artist_id INTEGER, alias_name TEXT",
    "artist_name_variation": "artist_id INTEGER, name TEXT",
}

# ``entity.identity`` stub DDL — mirrors discogs-cache alembic (the schema's
# owner; see CLAUDE.md "PostgreSQL schema ownership": LML never CREATEs in
# ``entity.*`` outside tests). One shared literal so per-file copies can't
# silently diverge. Five older files still carry inline copies pending
# LML#768; until that lands, a discogs-cache identity-column change must
# update this constant AND those copies.
ENTITY_IDENTITY_DDL = """
    CREATE TABLE entity.identity (
        id SERIAL PRIMARY KEY,
        library_name TEXT NOT NULL UNIQUE,
        discogs_artist_id INTEGER,
        wikidata_qid TEXT,
        musicbrainz_artist_id TEXT,
        spotify_artist_id TEXT,
        apple_music_artist_id TEXT,
        bandcamp_id TEXT,
        reconciliation_status TEXT NOT NULL DEFAULT 'unreconciled',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
"""


async def skip_if_drop_targets_populated(conn, public_tables: "Iterable[str]") -> None:
    """``pytest.skip`` when anything a schema-dropping fixture would destroy holds rows.

    The default ``DATABASE_URL_TEST`` may point at a real discogs-cache whose
    ``entity.*`` rows took hours of rate-limited reconciliation to build.
    Fixtures that run ``DROP SCHEMA entity CASCADE`` and drop named public
    tables must call this FIRST.

    The entity side is enumerated dynamically from ``pg_class`` (relkinds
    r/p/m: ordinary + partitioned tables + matviews — everything row-bearing
    the CASCADE destroys; matviews are invisible to ``information_schema``,
    hence ``pg_class``). A hand-maintained list rots the day discogs-cache's
    alembic adds a table — this guard's per-file precursors had to be widened
    for ``release_identity`` after the fact. The public side stays an explicit
    parameter: those fixtures drop only the named tables, never the schema.

    No false veto: the calling fixtures drop the whole entity schema in
    teardown, so between tests the sweep sees zero entity relations; residue
    from a crashed setup SHOULD veto until an operator looks. Probes use
    ``EXISTS``, not ``COUNT(*)`` — the veto needs has-rows only, and a COUNT
    against a real discogs-cache would table-scan millions of rows in exactly
    the mispointed-DSN case the guard exists for. A probe error (e.g. a
    never-refreshed matview) fails toward veto.
    """
    entity_rels = await conn.fetch(
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'entity' AND c.relkind IN ('r', 'p', 'm')"
    )
    targets: list[tuple[str, str]] = [("entity", r["relname"]) for r in entity_rels]
    for table in public_tables:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = $1)",
            table,
        )
        if exists:
            targets.append(("public", table))
    await _veto_if_any_rows(conn, targets)


async def skip_if_named_tables_populated(conn, tables: "Iterable[tuple[str, str]]") -> None:
    """``pytest.skip`` when any of the named ``(schema, table)`` targets holds rows.

    The table-scoped companion to :func:`skip_if_drop_targets_populated` (no
    ``entity.*`` sweep): for fixtures that drop only tables their own suite
    owns — e.g. the ``lml_cache.*`` cache suites — but whose targets on a
    mispointed ``DATABASE_URL_TEST`` would be real collected data (streaming
    URLs, resolution caches). Any-rows veto, same fail-toward-veto probe
    semantics; a missing table is not a veto (``to_regclass`` filters it out
    so first-run bootstraps pass).
    """
    targets: list[tuple[str, str]] = []
    for schema, table in tables:
        try:
            regclass = await conn.fetchval("SELECT to_regclass($1)", f"{schema}.{table}")
        except Exception:
            # Fail toward veto: an unprobeable existence check (connection
            # loss mid-sweep) must not read as "table absent, safe to drop".
            # _veto_if_any_rows' own probe will fail the same way and report
            # it as unprobeable rather than silently passing.
            targets.append((schema, table))
            continue
        if regclass is not None:
            targets.append((schema, table))
    await _veto_if_any_rows(conn, targets)


async def _veto_if_any_rows(conn, targets: "Iterable[tuple[str, str]]") -> None:
    """Shared probe/veto core: ``pytest.skip`` if any target has rows (or can't be probed)."""
    populated: list[str] = []
    unprobeable: list[str] = []
    for schema, table in targets:
        try:
            has_rows = await conn.fetchval(f'SELECT EXISTS(SELECT 1 FROM "{schema}"."{table}")')
        except Exception:
            # Fail toward veto, but say so — a mid-sweep connection loss (or
            # a never-refreshed matview) must not be reported as "holds
            # data", or the operator repoints DATABASE_URL_TEST instead of
            # looking at the actual infra failure.
            unprobeable.append(f"{schema}.{table}")
            continue
        if has_rows:
            populated.append(f"{schema}.{table}")

    if populated or unprobeable:
        parts = []
        if populated:
            parts.append(f"tables that hold data ({', '.join(populated)})")
        if unprobeable:
            parts.append(
                f"tables whose row-probe FAILED ({', '.join(unprobeable)}) — "
                "possible connection loss; vetoing because their contents are unknown"
            )
        pytest.skip(
            f"Refusing to DROP: {'; '.join(parts)}. Point DATABASE_URL_TEST "
            "at a clean, reachable PG before running this suite."
        )


async def skip_unless_wxyc_identity_match_artist(conn) -> None:
    """``pytest.skip`` unless ``wxyc_identity_match_artist`` is deployed.

    The function family ships via alembic 0004 from WXYC/discogs-etl and
    depends on the ``wxyc_unaccent`` text-search dictionary (a server-side
    rules file), so CI's plain ``postgres:16-alpine`` service container
    doesn't have it. See ``TestDiscogsReconciliationSQLSmoke`` for the
    provisioning TODO.
    """
    exists = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_proc WHERE proname = $1)",
        "wxyc_identity_match_artist",
    )
    if not exists:
        pytest.skip(
            "wxyc_identity_match_artist not deployed -- needs alembic 0004 "
            "from WXYC/discogs-etl. CI's plain postgres-16 service container "
            "doesn't have it."
        )


@pytest_asyncio.fixture
async def pg_pool_large():
    """Real asyncpg pool (``max_size=4``); skips on connect failure.

    Identical to :func:`pg_pool` apart from the connection cap. Modules whose
    fixtures historically created a ``max_size=4`` pool alias their local
    ``pg_pool`` to this one, so downstream consumers keep the larger cap
    without re-typing every request.
    """
    async for pool in _make_pg_pool(max_size=4):
        yield pool


# ---------------------------------------------------------------------------
# Seed data -- representative catalog items
# ---------------------------------------------------------------------------

SEED_ITEMS = [
    # Stereolab: multi-album artist (3 albums)
    (1, "Aluminum Tunes", "Stereolab", "RO", 87, 1, "Rock", "CD"),
    (2, "Dots and Loops", "Stereolab", "RO", 87, 2, "Electronic", "CD"),
    (3, "Emperor Tomato Ketchup", "Stereolab", "RO", 87, 3, "Electronic", "CD"),
    # Jessica Pratt + Cat Power: multiple genres
    (4, "On Your Own Love Again", "Jessica Pratt", "RO", 112, 1, "Rock", "Vinyl"),
    (5, "Moon Pix", "Cat Power", "RO", 23, 1, "Rock", "CD"),
    # Duke Ellington & John Coltrane: artist name with "&" (2 albums)
    (
        6,
        "Duke Ellington & John Coltrane",
        "Duke Ellington & John Coltrane",
        "JA",
        7,
        1,
        "Jazz",
        "CD",
    ),
    (
        7,
        "In a Sentimental Mood (Single)",
        "Duke Ellington & John Coltrane",
        "JA",
        7,
        2,
        "Jazz",
        "Vinyl",
    ),
    # Various Artists: compilation handling
    (10, "Now That's What I Call Music 47", "Various Artists", "V", 1, 1, "Compilation", "CD"),
    (11, "Rock Classics", "Various Artists", "V", 1, 2, "Compilation", "CD"),
    # Juana Molina: non-English artist name
    (12, "DOGA", "Juana Molina", "RO", 42, 1, "Rock", "CD"),
    (13, "Wed 21", "Juana Molina", "RO", 42, 2, "Rock", "CD"),
    # Preserved from original: intentionally swapped artist/title for regression tests
    (14, "Living Colour", "Vivid", "L", 1, 1, "Rock", "CD"),
    (15, "Vivid", "Living Colour", "L", 1, 1, "Rock", "CD"),
    # Preserved: self-titled artist/album edge case
    (18, "Laid Back", "Laid Back", "L", 2, 1, "Electronic", "CD"),
    (19, "Joni Mitchell", "Joni Mitchell", "MI", 8, 1, "Rock", "Vinyl"),
    (20, "Court and Spark", "Joni Mitchell", "MI", 8, 6, "Rock", "Vinyl"),
    # Preserved: "S/t" self-titled abbreviation
    (21, "S/t", "The Bird and the Bee", "BI", 125, 1, "Rock", "CD"),
    (22, "Please Clap Your Hands", "The Bird and the Bee", "BI", 125, 2, "Rock", "CD"),
    # Preserved: quoted Discogs artist name regression
    (23, "Weird Al Yankovic", "Weird Al Yankovic", "YA", 1, 1, "Rock", "Vinyl"),
    (24, "Dare to Be Stupid", "Weird Al Yankovic", "YA", 1, 2, "Rock", "Vinyl"),
    (25, "Even Worse", "Weird Al Yankovic", "YA", 1, 3, "Rock", "Vinyl"),
    # Preserved: track on both artist album and VA compilation
    (26, "London Zoo", "The Bug", "B", 2, 1, "Electronic", "CD"),
    (27, "Pressure", "The Bug", "B", 2, 2, "Electronic", "CD"),
    (28, "The Sound of Dub", "Various Artists - Reggae", "V", 2, 1, "Reggae", "CD"),
    # Preserved: Grimes cluster (multiple artists/albums with "Grimes" in them)
    (29, "Tiny Grimes", "Tiny Grimes", "G", 1, 1, "Jazz", "CD"),
    (30, "Report from Grimes Creek", "Rosalie Sorrels", "S", 3, 1, "Folk", "CD"),
    (31, "Grimes Golden", "Further", "F", 1, 1, "Rock", "CD"),
    (32, "Live at the Kerava Jazz Festival", "Henry Grimes", "G", 2, 1, "Jazz", "CD"),
    (33, "Spirits Aloft", "Henry Grimes", "G", 2, 2, "Jazz", "CD"),
    (34, "Visions", "Grimes", "G", 3, 1, "Rock", "CD"),
    (35, "Halfaxa", "Grimes", "G", 3, 2, "Rock", "Vinyl"),
    (36, "Art Angels", "Grimes", "G", 3, 3, "Rock", "Vinyl"),
    (37, "Miss Anthropocene", "Grimes", "G", 3, 4, "Rock", "CD"),
    # Additional WXYC example artists
    (38, "Edits", "Chuquimamani-Condori", "EL", 15, 1, "Electronic", "Vinyl"),
    (39, "Confield", "Autechre", "EL", 16, 1, "Electronic", "CD"),
    (40, "1st Class", "Large Professor", "HH", 1, 1, "Hiphop", "CD"),
    (41, "...Destroys The Space Invaders", "Prince Jammy", "RE", 1, 1, "Reggae", "Vinyl"),
    # Nina Simone: top-2 prod offender in the #400 evidence table
    # (/release/1402136 → 150 distinct DJ-typed albums across 419 rows). The
    # library carries Pastel Blues but NOT every Nina Simone album a DJ might
    # type — the artist-fallback contamination floor (#400) must drop
    # candidates whose titles don't clear 80 against the typed album.
    (42, "Pastel Blues", "Nina Simone", "SI", 1, 1, "Jazz", "Vinyl"),
    # Junior Kimbrough: title-album and same-artist comp that both contain the
    # song "Meet Me in the City". Exercises song-title-vs-primary-album
    # ranking when several library candidates by the same artist all pass
    # track validation against Discogs.
    (51752, "Meet Me in the City", "Junior Kimbrough", "KI", 6, 4, "Blues", "CD"),
    (
        51753,
        "You Better Run (The essential Junior Kimbrough)",
        "Junior Kimbrough",
        "KI",
        6,
        5,
        "Blues",
        "CD",
    ),
    # Lee 'Scratch' Perry: cluster used by the cache-promotion regression test.
    # Five fallback albums with low-id slots, plus "Live at Maritime Hall" deeper
    # in the catalog -- mirrors the production layout where the song-bearing
    # album sits past the artist-only FTS5 top-N slice.
    (12663, "Chicken Scratch", "Lee 'Scratch' Perry", "Pe", 1, 1, "Reggae", "Vinyl"),
    (12664, 'Ooh! Wah! 12"', "Lee 'Scratch' Perry", "Pe", 1, 2, "Reggae", "Vinyl"),
    (12665, "History Mystery Prophecy", "Lee 'Scratch' Perry", "Pe", 1, 3, "Reggae", "Vinyl"),
    (12680, "Arkology", "Lee 'Scratch' Perry", "Pe", 1, 17, "Reggae", "CD"),
    (12684, "Dub Fire", "Lee 'Scratch' Perry", "Pe", 1, 21, "Reggae", "CD"),
    (12682, "Live at Maritime Hall", "Lee 'Scratch' Perry", "Pe", 1, 20, "Reggae", "CD"),
    # V/A series catalogued under the librarian's <base>, vol. N convention,
    # against which Discogs returns the canonical release with a long
    # parenthetical subtitle. The repro shape for WXYC#531.
    (58610, "Disco Not Disco, vol. 1", "Various Artists - Rock - D", "V", 3, 1, "Rock", "CD"),
    (58611, "Disco Not Disco, vol. 2", "Various Artists - Rock - D", "V", 3, 2, "Rock", "CD"),
    # Non-V/A with the same shape — regression guard so the gate stays narrow.
    (60001, "Live Sessions, vol. 2", "Some Band", "S", 4, 2, "Rock", "CD"),
    # Trio / collaboration filed under one member (LML#684). The Discogs release
    # crediting the full trio fails the artist floor in the Step-4 artwork
    # re-search, so the found_on_compilation result must trust-bind the carried
    # (already-validated) release instead of dropping its artwork.
    (70001, "Orcutt-Shelley-Miller", "Bill Orcutt", "OR", 1, 1, "Jazz", "Vinyl"),
]


async def _create_schema(conn: aiosqlite.Connection):
    """Create the library table and FTS5 virtual table."""
    await conn.execute("""
        CREATE TABLE library (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            call_letters TEXT,
            artist_call_number INTEGER,
            release_call_number INTEGER,
            genre TEXT,
            format TEXT
        )
    """)
    await conn.execute("""
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title, artist,
            content=library,
            content_rowid=id
        )
    """)
    await conn.commit()


async def _seed_data(conn: aiosqlite.Connection):
    """Insert seed catalog items and sync FTS index."""
    await conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        SEED_ITEMS,
    )
    # Rebuild FTS index from library table
    await conn.execute("INSERT INTO library_fts(library_fts) VALUES('rebuild')")
    await conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def library_db():
    """Real LibraryDB backed by in-memory SQLite with FTS5 and seed data."""
    db = LibraryDB()
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row

    await _create_schema(conn)
    await _seed_data(conn)

    # Bypass connect() path-checking by directly setting the connection
    db._conn = conn

    yield db

    await conn.close()


@pytest.fixture
def test_settings():
    """Settings with no real tokens, telemetry disabled."""
    return Settings(
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest_asyncio.fixture
async def app_client(library_db, test_settings):
    """httpx AsyncClient with real LibraryDB but mocked Discogs/PostHog."""
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: None
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def app_client_with_discogs(library_db, test_settings):
    """httpx AsyncClient with real LibraryDB and a mock DiscogsService.

    The mock DiscogsService provides realistic track validation behavior:
    - validate_track_on_release returns True/False based on album title
    - search returns Discogs results for known albums
    """
    from httpx import ASGITransport, AsyncClient

    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
    from main import app

    mock_discogs = AsyncMock()
    mock_discogs.cache_service = None

    # Track validation: "Help Me" is on "Court and Spark" but NOT "Joni Mitchell" (self-titled)
    async def validate_track(release_id, song, artist):
        # release_id 1001 = Court and Spark, 1002 = self-titled
        return release_id == 1001

    mock_discogs.validate_track_on_release = AsyncMock(side_effect=validate_track)

    # Search returns matching Discogs results for known albums
    court_and_spark = make_discogs_result(
        release_id=1001, album="Court and Spark", artist="Joni Mitchell"
    )
    joni_self_titled = make_discogs_result(
        release_id=1002, album="Joni Mitchell", artist="Joni Mitchell"
    )

    async def search_discogs(request):
        album = request.album if hasattr(request, "album") else ""
        if album and "court" in album.lower():
            return DiscogsSearchResponse(results=[court_and_spark])
        if album and "joni" in album.lower():
            return DiscogsSearchResponse(results=[joni_self_titled])
        return DiscogsSearchResponse(results=[])

    mock_discogs.search = AsyncMock(side_effect=search_discogs)
    mock_discogs.get_release = AsyncMock(return_value=None)

    app.dependency_overrides[get_library_db] = lambda: library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: test_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()
