"""Integration tests for cache_service tombstone read + write paths (LML#510).

These tests bring up a fresh `release` / `artist` schema (mirroring the
discogs-etl dual-write contract — the `not_found` column lives in both
`schema/create_database.sql` and `alembic/versions/0010_release_not_found.py`
in the sibling repo). The tests pin:

* `get_release` / `get_artist_details` SELECT the `not_found` column and
  short-circuit before fetching child tables when it's `TRUE`. The
  tombstone-shaped model returned from the short-circuit carries empty
  identifier columns (`title = ''` / `name = ''`).
* `write_release` / `write_artist_details` distinguish tombstone payloads
  (`not_found = True`) from hydrated payloads:
  - Tombstone path: narrow UPSERT only touches the parent row's
    `not_found` flag + `_checked_at` / `fetched_at` stamp; the child
    cascade is skipped entirely so a 404 after a 200 doesn't wipe rich
    catalog metadata that's expensive to repopulate.
  - Hydrated path: the existing UPSERT plus `not_found = FALSE` in the
    `ON CONFLICT DO UPDATE SET` clause clears any prior tombstone in
    one statement.
* `get_artist_details_bulk` is intentionally untouched and returns
  tombstones as empty-fields `ArtistDetails`, parallel to the
  pre-LML#510 stub policy from PR #503.

Run with: `pytest -m pg -v tests/integration/test_cache_service_tombstones.py`
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    LabelCredit,
    MemberRef,
    ReleaseMetadataResponse,
    TrackItem,
)

# ``pg_pool`` (max_size=3) is provided by tests/integration/conftest.py.


@pytest_asyncio.fixture(autouse=True)
async def fresh_release_and_artist_schema(pg_pool):
    """Bring up the parent + child tables LML reads/writes.

    Mirrors the relevant pieces of `discogs-etl/schema/create_database.sql`
    at HEAD — once the LML PR pins the new wxyc-shared release and the
    discogs-etl alembic migration is live in staging, these inline
    definitions are the contract this test pins.
    """
    async with pg_pool.acquire() as conn:
        # Child tables first (FK ON DELETE CASCADE)
        for child in (
            "release_artist",
            "release_label",
            "release_genre",
            "release_style",
            "release_track",
            "release_track_artist",
            "release_video",
            "artist_alias",
            "artist_name_variation",
            "artist_member",
            "artist_url",
            "cache_metadata",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {child} CASCADE")
        await conn.execute("DROP TABLE IF EXISTS release CASCADE")
        await conn.execute("DROP TABLE IF EXISTS artist CASCADE")

        await conn.execute("""
            CREATE TABLE release (
                id                  integer PRIMARY KEY,
                title               text NOT NULL,
                release_year        smallint,
                country             text,
                artwork_url         text,
                released            text,
                format              text,
                master_id           integer,
                artwork_checked_at  timestamptz,
                not_found           boolean NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE release_artist (
                release_id  integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                artist_id   integer,
                artist_name text NOT NULL,
                extra       integer DEFAULT 0,
                role        text
            )
        """)
        await conn.execute("""
            CREATE TABLE release_label (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                label_id   integer,
                label_name text NOT NULL,
                catno      text
            )
        """)
        await conn.execute("""
            CREATE TABLE release_genre (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                genre      text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE release_style (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                style      text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE release_track (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                sequence   integer NOT NULL,
                position   text,
                title      text NOT NULL,
                duration   text
            )
        """)
        await conn.execute("""
            CREATE TABLE release_track_artist (
                release_id     integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                track_sequence integer NOT NULL,
                artist_name    text NOT NULL,
                extra          integer DEFAULT 0,
                role           text
            )
        """)
        await conn.execute("""
            CREATE TABLE release_video (
                release_id integer NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                sequence   integer NOT NULL,
                src        text NOT NULL,
                title      text,
                duration   integer,
                embed      boolean DEFAULT true
            )
        """)

        await conn.execute("""
            CREATE TABLE artist (
                id         integer PRIMARY KEY,
                name       text NOT NULL,
                profile    text,
                image_url  text,
                fetched_at timestamptz NOT NULL DEFAULT now(),
                not_found  boolean NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_alias (
                artist_id  integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                alias_id   integer,
                alias_name text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_name_variation (
                artist_id integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                name      text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_member (
                artist_id   integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                member_id   integer NOT NULL,
                member_name text NOT NULL,
                active      boolean DEFAULT true
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_url (
                artist_id integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                url       text NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE cache_metadata (
                release_id     integer PRIMARY KEY REFERENCES release(id) ON DELETE CASCADE,
                cached_at      timestamptz NOT NULL DEFAULT now(),
                source         text NOT NULL,
                last_validated timestamptz
            )
        """)
    yield
    async with pg_pool.acquire() as conn:
        for t in (
            "release_artist",
            "release_label",
            "release_genre",
            "release_style",
            "release_track",
            "release_track_artist",
            "release_video",
            "artist_alias",
            "artist_name_variation",
            "artist_member",
            "artist_url",
            "cache_metadata",
            "release",
            "artist",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


# ---------------------------------------------------------------------------
# Read-path short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.pg
@pytest.mark.asyncio
async def test_get_release_returns_tombstone_for_not_found_row(pg_pool):
    """A `not_found = TRUE` row reads back as a tombstone-shaped
    `ReleaseMetadataResponse`. Child tables are NOT queried."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, not_found, artwork_checked_at) "
            "VALUES (42, '', TRUE, now())"
        )

    cache = DiscogsCacheService(pg_pool)
    result = await cache.get_release(42)

    assert result is not None
    assert result.not_found is True
    assert result.title == ""
    assert result.artist == ""
    assert result.release_url == "https://www.discogs.com/release/42"
    # Child fields are empty per the short-circuit
    assert result.tracklist == []
    assert result.artists == []


@pytest.mark.pg
@pytest.mark.asyncio
async def test_get_release_real_row_not_found_false(pg_pool):
    """Real rows return with `not_found = False`."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, artwork_checked_at) "
            "VALUES (101, 'Aluminum Tunes', now())"
        )

    cache = DiscogsCacheService(pg_pool)
    result = await cache.get_release(101)
    assert result is not None
    assert result.not_found is False
    assert result.title == "Aluminum Tunes"


@pytest.mark.pg
@pytest.mark.asyncio
async def test_get_artist_details_returns_tombstone_for_not_found_row(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("INSERT INTO artist (id, name, not_found) VALUES (99, '', TRUE)")

    cache = DiscogsCacheService(pg_pool)
    result = await cache.get_artist_details(99)
    assert result is not None
    assert result.not_found is True
    assert result.name == ""
    assert result.aliases == []
    assert result.name_variations == []
    assert result.members == []
    assert result.urls == []


# ---------------------------------------------------------------------------
# Write-path: tombstone branch
# ---------------------------------------------------------------------------


@pytest.mark.pg
@pytest.mark.asyncio
async def test_write_release_tombstone_preserves_hydrated_parent_columns(pg_pool):
    """A tombstone write against a previously-hydrated row preserves the
    parent identifier columns and only flips `not_found` to TRUE. Without
    this, a 404 after a 200 would wipe the title."""
    cache = DiscogsCacheService(pg_pool)

    # First write: a real, hydrated row.
    hydrated = ReleaseMetadataResponse(
        release_id=200,
        title="Aluminum Tunes",
        artist="Stereolab",
        release_url="https://www.discogs.com/release/200",
        year=1998,
        artwork_url="https://img.discogs.com/aluminum.jpg",
        artists=[ArtistCredit(artist_id=2154, name="Stereolab")],
        labels=[LabelCredit(label_id=1, name="Duophonic", catno="UHF1")],
        tracklist=[TrackItem(position="A1", title="Cybele's Reverie", duration="3:30")],
        genres=["Electronic"],
        styles=["Krautrock"],
    )
    await cache.write_release(hydrated)

    # Second write: tombstone.
    tombstone = ReleaseMetadataResponse(
        release_id=200,
        title="",
        artist="",
        release_url="https://www.discogs.com/release/200",
        not_found=True,
    )
    await cache.write_release(tombstone)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, release_year, artwork_url, not_found, artwork_checked_at "
            "FROM release WHERE id = 200"
        )
    assert row["not_found"] is True
    assert row["title"] == "Aluminum Tunes", (
        "Tombstone write must NOT overwrite the parent's identifier columns; "
        "the EXCLUDED set in the tombstone branch intentionally omits title."
    )
    assert row["release_year"] == 1998
    assert row["artwork_url"] == "https://img.discogs.com/aluminum.jpg"
    assert row["artwork_checked_at"] is not None


@pytest.mark.pg
@pytest.mark.asyncio
async def test_write_release_tombstone_preserves_child_cascade(pg_pool):
    """A tombstone write must NOT wipe the child tables (release_artist /
    release_label / release_track / etc.) — those rows are expensive to
    repopulate and a tombstoned row that later recovers via the admin
    endpoint should re-emerge with intact catalog metadata."""
    cache = DiscogsCacheService(pg_pool)

    hydrated = ReleaseMetadataResponse(
        release_id=201,
        title="Aluminum Tunes",
        artist="Stereolab",
        release_url="https://www.discogs.com/release/201",
        artists=[ArtistCredit(artist_id=2154, name="Stereolab")],
        labels=[LabelCredit(label_id=1, name="Duophonic", catno="UHF1")],
        tracklist=[TrackItem(position="A1", title="Cybele's Reverie")],
        genres=["Electronic"],
        styles=["Krautrock"],
    )
    await cache.write_release(hydrated)

    tombstone = ReleaseMetadataResponse(
        release_id=201,
        title="",
        artist="",
        release_url="https://www.discogs.com/release/201",
        not_found=True,
    )
    await cache.write_release(tombstone)

    async with pg_pool.acquire() as conn:
        # Pin every child table from the issue's cascade list.
        counts = {}
        for tbl in (
            "release_artist",
            "release_label",
            "release_genre",
            "release_style",
            "release_track",
        ):
            counts[tbl] = await conn.fetchval(f"SELECT count(*) FROM {tbl} WHERE release_id = 201")
    for tbl, c in counts.items():
        assert c >= 1, (
            f"Tombstone write wiped {tbl} (count={c}); the tombstone branch "
            "must early-return before the child-table DELETE+INSERT cascade."
        )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_write_artist_details_tombstone_preserves_hydrated_parent(pg_pool):
    cache = DiscogsCacheService(pg_pool)

    hydrated = ArtistDetails(
        artist_id=300,
        name="Stereolab",
        profile="French/British band formed in 1990",
        image_url="https://img.discogs.com/stereolab.jpg",
    )
    await cache.write_artist_details(hydrated)

    tombstone = ArtistDetails(
        artist_id=300,
        name="",
        not_found=True,
    )
    await cache.write_artist_details(tombstone)

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT name, profile, image_url, not_found FROM artist WHERE id = 300"
        )
    assert row["not_found"] is True
    assert row["name"] == "Stereolab", (
        "Tombstone write must NOT overwrite name; the EXCLUDED set in the "
        "tombstone branch intentionally omits name/profile/image_url."
    )
    assert row["profile"] == "French/British band formed in 1990"
    assert row["image_url"] == "https://img.discogs.com/stereolab.jpg"


@pytest.mark.pg
@pytest.mark.asyncio
async def test_write_artist_details_tombstone_preserves_child_cascade(pg_pool):
    cache = DiscogsCacheService(pg_pool)

    hydrated = ArtistDetails(
        artist_id=301,
        name="Stereolab",
        aliases=[ArtistRef(id=999, name="Monade")],
        name_variations=["The Stereolab"],
        members=[MemberRef(id=200, name="Laetitia Sadier", active=True)],
        urls=["https://stereolab.com"],
    )
    await cache.write_artist_details(hydrated)

    tombstone = ArtistDetails(
        artist_id=301,
        name="",
        not_found=True,
    )
    await cache.write_artist_details(tombstone)

    async with pg_pool.acquire() as conn:
        counts = {}
        for tbl in ("artist_alias", "artist_name_variation", "artist_member", "artist_url"):
            counts[tbl] = await conn.fetchval(f"SELECT count(*) FROM {tbl} WHERE artist_id = 301")
    for tbl, c in counts.items():
        assert c >= 1, (
            f"Tombstone write wiped {tbl} (count={c}); the tombstone branch "
            "must early-return before the child-table DELETE+INSERT cascade."
        )


# ---------------------------------------------------------------------------
# Tombstone clears on recovered 200
# ---------------------------------------------------------------------------


@pytest.mark.pg
@pytest.mark.asyncio
async def test_recovered_200_clears_release_tombstone(pg_pool):
    """A hydrated write following a tombstone write clears `not_found` to
    FALSE in the same statement. Without this, a tombstoned id whose
    Discogs upstream recovers stays unreachable forever."""
    cache = DiscogsCacheService(pg_pool)

    # Tombstone first.
    await cache.write_release(
        ReleaseMetadataResponse(
            release_id=400,
            title="",
            artist="",
            release_url="https://www.discogs.com/release/400",
            not_found=True,
        )
    )
    # Now recovered hydrated payload.
    await cache.write_release(
        ReleaseMetadataResponse(
            release_id=400,
            title="Moon Pix",
            artist="Cat Power",
            release_url="https://www.discogs.com/release/400",
            year=1998,
        )
    )

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT title, not_found FROM release WHERE id = 400")
    assert row["title"] == "Moon Pix"
    assert row["not_found"] is False, (
        "Non-tombstone write must clear not_found in `ON CONFLICT DO "
        "UPDATE SET` — otherwise a tombstoned id with a recovered "
        "Discogs upstream is unreachable forever."
    )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_recovered_200_clears_artist_tombstone(pg_pool):
    cache = DiscogsCacheService(pg_pool)

    await cache.write_artist_details(ArtistDetails(artist_id=401, name="", not_found=True))
    await cache.write_artist_details(ArtistDetails(artist_id=401, name="Stereolab"))

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT name, not_found FROM artist WHERE id = 401")
    assert row["name"] == "Stereolab"
    assert row["not_found"] is False


# ---------------------------------------------------------------------------
# Bulk path leaves tombstones alone
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Admin recovery: DELETE tombstone surface
# ---------------------------------------------------------------------------


@pytest.mark.pg
@pytest.mark.asyncio
async def test_delete_tombstone_release_returns_deleted(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, not_found, artwork_checked_at) "
            "VALUES (600, '', TRUE, now())"
        )
    cache = DiscogsCacheService(pg_pool)
    outcome = await cache.delete_tombstone("release", 600)
    assert outcome == "deleted"
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM release WHERE id = 600")
    assert row is None, "Tombstoned row must be physically deleted."


@pytest.mark.pg
@pytest.mark.asyncio
async def test_delete_tombstone_artist_returns_deleted(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("INSERT INTO artist (id, name, not_found) VALUES (601, '', TRUE)")
    cache = DiscogsCacheService(pg_pool)
    outcome = await cache.delete_tombstone("artist", 601)
    assert outcome == "deleted"


@pytest.mark.pg
@pytest.mark.asyncio
async def test_delete_tombstone_non_tombstone_row_refuses(pg_pool):
    """A real (non-tombstone) row must be physically untouchable via this
    endpoint. The `AND not_found = TRUE` guard is the safety floor."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, not_found, artwork_checked_at) "
            "VALUES (602, 'Aluminum Tunes', FALSE, now())"
        )
    cache = DiscogsCacheService(pg_pool)
    outcome = await cache.delete_tombstone("release", 602)
    assert outcome == "exists_not_tombstone"
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, title FROM release WHERE id = 602")
    assert row is not None, "Real row must survive the admin delete probe."
    assert row["title"] == "Aluminum Tunes"


@pytest.mark.pg
@pytest.mark.asyncio
async def test_delete_tombstone_missing_row_returns_not_found(pg_pool):
    cache = DiscogsCacheService(pg_pool)
    outcome = await cache.delete_tombstone("release", 9_999_999)
    assert outcome == "not_found"


@pytest.mark.pg
@pytest.mark.asyncio
async def test_get_artist_details_bulk_returns_tombstone_as_empty_fields(pg_pool):
    """`get_artist_details_bulk` is cache-only and (per the LML#503 stub
    policy that LML#510 inherits) does not filter tombstones. It returns
    them as empty-fields `ArtistDetails` — same shape as a stub row,
    observationally equivalent to "missing from bundle" for current
    callers."""
    cache = DiscogsCacheService(pg_pool)
    async with pg_pool.acquire() as conn:
        await conn.execute("INSERT INTO artist (id, name, not_found) VALUES (500, '', TRUE)")
        await conn.execute("INSERT INTO artist (id, name) VALUES (501, 'Cat Power')")

    bundle = await cache.get_artist_details_bulk([500, 501])
    assert 500 in bundle
    assert 501 in bundle
    # Tombstone returns as empty-fields per the bulk-path policy.
    assert bundle[500].name == ""
    assert bundle[500].aliases == []
    assert bundle[501].name == "Cat Power"
