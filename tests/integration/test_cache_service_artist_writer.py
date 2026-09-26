"""Integration tests for ``DiscogsCacheService.write_artist_details``'s
``fetched_at`` invariant against real PostgreSQL.

The cache-hit discriminator in ``DiscogsService.get_artist_details``
(discogs/service.py) keys on ``ArtistDetails.fetched_at`` -- any row written
by LML has a non-NULL ``fetched_at``; rows with ``fetched_at IS NULL`` are
rebuild-created stubs that need to be re-fetched. The unit-level pin in
``tests/unit/test_cache_service.py::TestWriteArtistDetailsFetchedAtInvariant``
catches the column being dropped from the SQL text, but a future writer
that binds the value as a parameter (and lets a caller pass ``None``) or
swaps ``now()`` for a functionally-identical-but-textually-different
alternative would slip past a substring check.

This module asserts the post-write database state instead:

* INSERT path: a fresh id is written with a non-NULL ``fetched_at``.
* ON CONFLICT UPDATE path: a pre-seeded row with ``fetched_at = '2000-01-01'``
  has its timestamp advanced past the seed value after the write.

It also covers the WXYC/discogs-etl#433 prerequisite: the child tables here
carry the ``UNIQUE`` constraints that ticket adds to production, so a Discogs
response listing the same URL / alias / member / name variation twice would
abort the whole artist write unless the child inserts say
``ON CONFLICT DO NOTHING``.

Run with: ``pytest -m pg -v tests/integration/test_cache_service_artist_writer.py``
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from discogs.models import ArtistDetails, ArtistRef, MemberRef
from tests.integration.conftest import skip_if_named_tables_populated

# ``pg_pool`` (max_size=3) is provided by tests/integration/conftest.py.


@pytest_asyncio.fixture(autouse=True)
async def fresh_artist_schema(pg_pool):
    """Bring up the artist parent + child tables LML writes through.

    Mirrors the production discogs-cache shape in every respect **except
    uniqueness** (see below): ``fetched_at`` is nullable (rebuild stubs land
    here as NULL) and ``not_found`` defaults to FALSE (LML#510 tombstone
    column). The writer is responsible for populating ``fetched_at`` on every
    write -- this fixture deliberately does NOT default it server-side so the
    test catches any writer that fails to stamp it.

    The four child tables carry the ``UNIQUE`` constraints that
    WXYC/discogs-etl#433 adds to production, **which prod does not have
    yet**. That is the one deliberate divergence: this fixture is how LML
    finds out whether its writer survives the constraint before the
    constraint exists, and without them the duplicate-collapse test below
    passes vacuously. The cost of the divergence, stated so nobody is
    surprised by it: a regression that manifests only *without* uniqueness --
    i.e. against today's actual production -- is not reproducible here.

    Guarded by ``skip_if_named_tables_populated``, which this fixture was
    missing: it drops five real discogs-cache table names, and on a
    ``DATABASE_URL_TEST`` pointed at the actual cache those hold the artist
    catalogue (~393 MB as of 2026-09-25) that a monthly rebuild takes hours
    to reproduce. The conftest helper's own docstring says dropping fixtures
    must call it first, and most of the ``pg`` suite does --
    ``test_cache_service_tombstones.py`` and
    ``test_cache_lean_json_agg_parity.py`` still do not, and
    ``test_pg_fixture_guard_adoption.py``'s discovery sweep only greps for
    ``lml_cache``, so nothing catches a ``public.*`` dropper. Tracked in
    LML#1363; not fixed here because it needs shared table-name constants and
    a widened sweep, which is its own change.

    One consequence of the guard worth knowing: this fixture scratches under
    the *real* table names in ``public``, so if a run is interrupted between
    an insert and the teardown drop (Ctrl-C, SIGKILL), ``public.artist`` holds
    a row and every test in this module then skips permanently -- and a
    skipped check satisfies branch protection, so that reads green. If you see
    this module skipping, drop the leftovers by hand rather than assuming it
    passed. Scratching in a dedicated schema is the real fix, also LML#1363.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(
            conn,
            (
                ("public", "artist"),
                ("public", "artist_alias"),
                ("public", "artist_name_variation"),
                ("public", "artist_member"),
                ("public", "artist_url"),
            ),
        )
        for child in (
            "artist_alias",
            "artist_name_variation",
            "artist_member",
            "artist_url",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {child} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist CASCADE")
        await conn.execute(
            """
            CREATE TABLE artist (
                id         integer PRIMARY KEY,
                name       text NOT NULL,
                profile    text,
                image_url  text,
                fetched_at timestamptz,
                not_found  boolean NOT NULL DEFAULT FALSE
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_alias (
                artist_id  integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                alias_id   integer,
                alias_name text NOT NULL,
                UNIQUE (artist_id, alias_name)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_name_variation (
                artist_id integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                name      text NOT NULL,
                UNIQUE (artist_id, name)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_member (
                artist_id   integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                member_id   integer NOT NULL,
                member_name text NOT NULL,
                active      boolean DEFAULT true,
                UNIQUE (artist_id, member_id)
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE artist_url (
                artist_id integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                url       text NOT NULL,
                UNIQUE (artist_id, url)
            )
            """
        )
    yield
    async with pg_pool.acquire() as conn:
        for t in (
            "artist_alias",
            "artist_name_variation",
            "artist_member",
            "artist_url",
            "artist",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


@pytest.mark.pg
@pytest.mark.asyncio
async def test_insert_path_stamps_fetched_at_non_null(pg_pool):
    """A fresh artist write lands with ``fetched_at`` non-NULL.

    Catches any writer that omits the column or binds it as a nullable
    parameter -- the substring unit test would still see ``fetched_at``
    in the SQL, but the row would have ``NULL`` in the column.
    """
    cache = DiscogsCacheService(pg_pool)
    await cache.write_artist_details(ArtistDetails(artist_id=2154, name="Stereolab"))

    async with pg_pool.acquire() as conn:
        fetched_at = await conn.fetchval("SELECT fetched_at FROM artist WHERE id = 2154")

    assert fetched_at is not None, (
        "INSERT branch of write_artist_details must stamp fetched_at; the "
        "cache-hit discriminator in DiscogsService.get_artist_details (#503) "
        "treats fetched_at IS NULL as a rebuild stub and re-fetches every call."
    )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_on_conflict_path_advances_fetched_at(pg_pool):
    """An UPDATE against a stale row advances ``fetched_at`` past the seed.

    This is the assertion the substring unit test can't make. A writer that
    binds ``fetched_at`` to a parameter and lets the caller pass an old
    timestamp (or ``None``) would still emit ``fetched_at = $5`` in the SQL,
    passing the substring check while leaving the column stuck at the seed.
    Asserting strict-greater-than against a wall-clock-old seed catches that
    class of regression.
    """
    seed = datetime(2000, 1, 1, tzinfo=UTC)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO artist (id, name, fetched_at) VALUES (305253, 'Juana Molina', $1)",
            seed,
        )

    cache = DiscogsCacheService(pg_pool)
    await cache.write_artist_details(
        ArtistDetails(artist_id=305253, name="Juana Molina", profile="Argentinian artist")
    )

    async with pg_pool.acquire() as conn:
        fetched_at = await conn.fetchval("SELECT fetched_at FROM artist WHERE id = 305253")

    assert fetched_at is not None, "ON CONFLICT branch must stamp fetched_at non-NULL on update"
    assert fetched_at > seed, (
        "ON CONFLICT branch must advance fetched_at past the previous value; "
        f"got {fetched_at!r} <= seed {seed!r}. A writer that binds fetched_at "
        "as a parameter and is passed a stale value (or None) would pass the "
        "substring SQL check but leave the row stuck in the past."
    )


# ---------------------------------------------------------------------------
# WXYC/discogs-etl#433 prerequisite: survive the UNIQUE constraints
# ---------------------------------------------------------------------------


def _artist_with_duplicated_children() -> ArtistDetails:
    """An ``ArtistDetails`` whose four child lists each repeat one entry.

    This is what a Discogs response looks like when the API lists the same
    URL, alias, member or name variation twice -- which it does. Nothing
    upstream of the writer de-duplicates these lists.
    """
    return ArtistDetails(
        artist_id=2154,
        name="Stereolab",
        name_variations=["Stereolab", "Stereolab"],
        aliases=[
            ArtistRef(id=53199, name="Groop Played Space Age Bachelor Pad Music"),
            ArtistRef(id=53199, name="Groop Played Space Age Bachelor Pad Music"),
        ],
        members=[
            MemberRef(id=246559, name="Lætitia Sadier", active=True),
            MemberRef(id=246559, name="Lætitia Sadier", active=True),
        ],
        urls=[
            "https://www.discogs.com/artist/2154",
            "https://www.discogs.com/artist/2154",
        ],
    )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_within_response_duplicates_collapse_instead_of_aborting(pg_pool):
    """A Discogs response listing the same child twice must not fail the write.

    WXYC/discogs-etl#433 adds ``UNIQUE`` constraints to the four ``artist_*``
    child tables. ``write_artist_details`` inserts those children with plain
    ``INSERT`` inside one transaction, so under the constraint a duplicated
    entry raises ``unique_violation``, rolls the **whole** artist write back,
    and leaves that artist a permanent cache miss -- re-burning Discogs rate
    budget on every subsequent lookup, forever. That is why LML must ship
    ``ON CONFLICT DO NOTHING`` on all four child inserts, and be live on both
    staging and production, *before* the constraint exists anywhere. Staging
    and production share the one discogs-cache database, so there is no
    environment in which the constraint lands against an unhardened writer.

    The clause is deliberately target-less (``ON CONFLICT DO NOTHING``, no
    column list). A target requires a matching unique index to already exist,
    which would make this change unshippable before the migration -- exactly
    the ordering deadlock it exists to avoid. Target-less is valid with no
    constraint present at all, so the same code is correct on both sides of
    the migration.
    """
    cache = DiscogsCacheService(pg_pool)

    # Must not raise. Before the fix this aborts the transaction.
    await cache.write_artist_details(_artist_with_duplicated_children())

    async with pg_pool.acquire() as conn:
        fetched_at = await conn.fetchval("SELECT fetched_at FROM artist WHERE id = 2154")
        counts = {
            table: await conn.fetchval(f"SELECT count(*) FROM {table} WHERE artist_id = 2154")
            for table in (
                "artist_alias",
                "artist_name_variation",
                "artist_member",
                "artist_url",
            )
        }

    assert fetched_at is not None, (
        "the artist row must be committed, not rolled back. A NULL/absent row "
        "here means the child INSERT raised and took the whole transaction "
        "with it -- the permanent-cache-miss failure mode this test exists for."
    )
    assert counts == {
        "artist_alias": 1,
        "artist_name_variation": 1,
        "artist_member": 1,
        "artist_url": 1,
    }, f"each duplicated child must collapse to exactly one row; got {counts}"


def _artist_after_upstream_corrections() -> ArtistDetails:
    """The same artist as above, re-fetched after Discogs data changed.

    Every child differs from the first write in a way that a missing
    ``DELETE`` would strand:

    * ``artist_member`` keeps ``member_id`` 246559 but flips ``active`` to
      ``False``. The key is ``(artist_id, member_id)``, so this row *conflicts*
      -- ``DO NOTHING`` without a preceding DELETE keeps the stale
      ``active = True``. This is the value-level discriminator.
    * the alias is renamed, the name variation changed, and one URL dropped.
      Those keys no longer match, so without the DELETE the stale rows simply
      remain alongside the new ones and the counts go up. Count-level
      discriminators.
    """
    return ArtistDetails(
        artist_id=2154,
        name="Stereolab",
        name_variations=["Stereolab (UK)"],
        aliases=[
            ArtistRef(id=53199, name="Groop Played Space Age Bachelor Pad Music (Remastered)")
        ],
        members=[MemberRef(id=246559, name="L\u00e6titia Sadier", active=False)],
        urls=["https://www.discogs.com/artist/2154-Stereolab"],
    )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_rehydration_applies_upstream_corrections(pg_pool):
    """A re-fetch must replace cached children, not be swallowed by DO NOTHING.

    This is the test that pins the ``DELETE`` before each child insert. The
    delete is what makes re-hydration a *replace*; ``ON CONFLICT DO NOTHING``
    on its own keeps the **existing** row, so dropping the delete and leaning
    on the clause would silently stop applying upstream corrections -- a
    renamed alias, a member who left the band, a URL Discogs removed would all
    be frozen at their first-seen values, forever, with no error anywhere.

    An earlier version of this test wrote byte-identical details twice and
    asserted the counts stayed at one. That passes with the deletes removed
    (every row conflicts and is skipped, so the counts are unchanged), which
    means it pinned nothing. Verified: deleting all four ``DELETE FROM
    artist_*`` statements left that version green. Hence the mutated second
    payload -- it fails on both counts and values without the deletes.
    """
    cache = DiscogsCacheService(pg_pool)

    await cache.write_artist_details(_artist_with_duplicated_children())
    await cache.write_artist_details(_artist_after_upstream_corrections())

    async with pg_pool.acquire() as conn:
        counts = {
            table: await conn.fetchval(f"SELECT count(*) FROM {table} WHERE artist_id = 2154")
            for table in (
                "artist_alias",
                "artist_name_variation",
                "artist_member",
                "artist_url",
            )
        }
        member_active = await conn.fetchval(
            "SELECT active FROM artist_member WHERE artist_id = 2154 AND member_id = 246559"
        )
        alias_name = await conn.fetchval(
            "SELECT alias_name FROM artist_alias WHERE artist_id = 2154"
        )
        variation = await conn.fetchval(
            "SELECT name FROM artist_name_variation WHERE artist_id = 2154"
        )
        url = await conn.fetchval("SELECT url FROM artist_url WHERE artist_id = 2154")

    assert counts == {
        "artist_alias": 1,
        "artist_name_variation": 1,
        "artist_member": 1,
        "artist_url": 1,
    }, (
        "a re-fetch must REPLACE children, not accumulate alongside them. Higher "
        f"counts mean the DELETE before each insert is missing. Got {counts}"
    )
    assert member_active is False, (
        "the corrected `active = False` must land. `True` here means the row "
        "conflicted and ON CONFLICT DO NOTHING kept the stale value -- i.e. the "
        "DELETE is gone and re-hydration has silently stopped correcting data."
    )
    assert alias_name == "Groop Played Space Age Bachelor Pad Music (Remastered)", (
        f"renamed alias must replace the old one; got {alias_name!r}"
    )
    assert variation == "Stereolab (UK)", f"changed name variation must replace; got {variation!r}"
    assert url == "https://www.discogs.com/artist/2154-Stereolab", (
        f"replacement URL must be the only one; got {url!r}"
    )
