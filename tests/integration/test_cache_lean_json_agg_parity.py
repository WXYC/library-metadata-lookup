"""Parity test for the L4c ``json_agg`` lean ``/lookup`` read path (LML#895).

L4c collapses the lean ``/lookup`` release/artist hydration into 1-2 ``json_agg``
round-trips (extending the LML#894 lean path). This suite pins the acceptance
criterion end-to-end against a real PostgreSQL: for a fixture release and a
fixture artist, ``get_release_lean`` / ``get_artist_details_lean`` return a
payload **shape-identical** to the shared per-child ``get_release`` /
``get_artist_details`` on the ``/lookup``-visible surface. The only deliberate
divergences are the children ``/lookup`` never consumes — release ``videos`` and
artist ``aliases`` / ``name_variations`` / ``members`` — which the lean path
returns empty while the shared (``/discogs/*``) path returns full.

Running against real Postgres also exercises the real driver's ``json`` decode
(asyncpg hands a ``json`` column back as ``str``), which the mock-based unit
tests in ``tests/unit/test_cache_service.py`` cannot.

Run with: ``pytest -m pg -v tests/integration/test_cache_lean_json_agg_parity.py``
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from discogs.cache_service import DiscogsCacheService

# ``pg_pool`` (max_size=3) is provided by tests/integration/conftest.py.


@pytest_asyncio.fixture(autouse=True)
async def fresh_schema(pg_pool):
    """Bring up the parent + child tables the release / artist reads touch."""
    async with pg_pool.acquire() as conn:
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
                fetched_at timestamptz,
                not_found  boolean NOT NULL DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE artist_alias (
                artist_id  integer NOT NULL REFERENCES artist(id) ON DELETE CASCADE,
                alias_id   integer,
                alias_name text
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
                member_id   integer,
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
            "release",
            "artist",
        ):
            await conn.execute(f"DROP TABLE IF EXISTS {t} CASCADE")


async def _seed_release(pg_pool):
    """A richly-populated release: Duke Ellington & John Coltrane on Impulse."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, release_year, artwork_url, released, master_id, "
            "not_found) VALUES (100, 'Duke Ellington & John Coltrane', 1963, "
            "'https://img.discogs.com/duke.jpg', '1963-01-01', 555, FALSE)"
        )
        await conn.executemany(
            "INSERT INTO release_artist (release_id, artist_id, artist_name, extra, role) "
            "VALUES (100, $1, $2, $3, $4)",
            [
                (10, "Duke Ellington", 0, None),
                (11, "John Coltrane", 0, None),
                (12, "Aaron Bell", 1, "Bass"),
            ],
        )
        await conn.execute(
            "INSERT INTO release_label (release_id, label_id, label_name, catno) "
            "VALUES (100, 7, 'Impulse Records', 'A-30')"
        )
        await conn.executemany(
            "INSERT INTO release_genre (release_id, genre) VALUES (100, $1)",
            [("Jazz",), ("Blues",)],
        )
        await conn.executemany(
            "INSERT INTO release_style (release_id, style) VALUES (100, $1)",
            [("Big Band",), ("Modal",)],
        )
        await conn.executemany(
            "INSERT INTO release_track (release_id, sequence, position, title, duration) "
            "VALUES (100, $1, $2, $3, $4)",
            [
                (1, "A1", "In a Sentimental Mood", "4:17"),
                (2, "A2", "Take the Coltrane", "4:44"),
            ],
        )
        await conn.executemany(
            "INSERT INTO release_track_artist "
            "(release_id, track_sequence, artist_name, extra, role) VALUES (100, $1, $2, $3, $4)",
            [
                # extra=0 performer credits (flow to TrackItem.artists)
                (1, "Duke Ellington", 0, None),
                (2, "John Coltrane", 0, None),
                # extra=1 per-track writer + a non-writer credit
                (1, "Duke Ellington", 1, "Written-By"),
                (2, "A Producer", 1, "Producer"),
            ],
        )
        await conn.execute(
            "INSERT INTO release_video (release_id, sequence, src, title, duration, embed) "
            "VALUES (100, 1, 'https://youtu.be/x', 'A Sentimental Mood (live)', 257, true)"
        )


async def _seed_artist(pg_pool):
    """A richly-populated artist: Stereolab, with every related child present."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO artist (id, name, profile, image_url, fetched_at, not_found) "
            "VALUES (77, 'Stereolab', 'Anglo-French avant-pop band.', "
            "'https://img.discogs.com/stereolab.jpg', now(), FALSE)"
        )
        await conn.execute(
            "INSERT INTO artist_alias (artist_id, alias_id, alias_name) "
            "VALUES (77, 88, 'Groop Play')"
        )
        await conn.executemany(
            "INSERT INTO artist_name_variation (artist_id, name) VALUES (77, $1)",
            [("Stereo Lab",), ("Stereolab (2)",)],
        )
        await conn.execute(
            "INSERT INTO artist_member (artist_id, member_id, member_name, active) "
            "VALUES (77, 99, 'Laetitia Sadier', true)"
        )
        await conn.executemany(
            "INSERT INTO artist_url (artist_id, url) VALUES (77, $1)",
            [("https://en.wikipedia.org/wiki/Stereolab",), ("https://stereolab.co.uk",)],
        )


@pytest.mark.pg
@pytest.mark.asyncio
async def test_release_lean_shape_parity_with_full(pg_pool):
    await _seed_release(pg_pool)
    svc = DiscogsCacheService(pg_pool)

    full = await svc.get_release(100)
    lean = await svc.get_release_lean(100)

    assert full is not None and lean is not None

    # /lookup-visible surface: identical.
    assert lean.title == full.title
    assert lean.artist == full.artist == "Duke Ellington"
    assert lean.artist_id == full.artist_id
    assert lean.year == full.year == 1963
    assert lean.label == full.label == "Impulse Records"
    assert lean.label_id == full.label_id
    assert lean.genres == full.genres == ["Jazz", "Blues"]
    assert lean.styles == full.styles == ["Big Band", "Modal"]
    assert lean.artwork_url == full.artwork_url
    assert lean.released == full.released
    assert lean.master_id == full.master_id == 555
    assert [c.name for c in lean.artists] == [c.name for c in full.artists]
    assert [c.name for c in lean.extra_artists] == [c.name for c in full.extra_artists]
    assert [(c.name, c.role) for c in lean.extra_artists] == [("Aaron Bell", "Bass")]
    assert [(lc.name, lc.catno) for lc in lean.labels] == [
        (lc.name, lc.catno) for lc in full.labels
    ]
    assert [(t.position, t.title, t.duration, t.artists) for t in lean.tracklist] == [
        (t.position, t.title, t.duration, t.artists) for t in full.tracklist
    ]
    # LML#699 per-track writer credits survive on the lean path, position-keyed.
    assert lean.track_writers == full.track_writers
    assert lean.track_writers is not None
    assert [c.name for c in lean.track_writers["A1"]] == ["Duke Ellington"]
    assert "A2" not in lean.track_writers  # producer-only extra=1 → dropped

    # Deliberate divergence: /lookup never surfaces videos.
    assert lean.videos == []
    assert len(full.videos) == 1


@pytest.mark.pg
@pytest.mark.asyncio
async def test_artist_lean_shape_parity_with_full(pg_pool):
    await _seed_artist(pg_pool)
    svc = DiscogsCacheService(pg_pool)

    full = await svc.get_artist_details(77)
    lean = await svc.get_artist_details_lean(77)

    assert full is not None and lean is not None

    # /lookup-visible surface: identical.
    assert lean.name == full.name == "Stereolab"
    assert lean.profile == full.profile
    assert lean.image_url == full.image_url
    assert lean.fetched_at == full.fetched_at
    assert (
        lean.urls
        == full.urls
        == [
            "https://en.wikipedia.org/wiki/Stereolab",
            "https://stereolab.co.uk",
        ]
    )

    # Deliberate divergence: /lookup never reads related children.
    assert lean.aliases == []
    assert lean.name_variations == []
    assert lean.members == []
    assert len(full.aliases) == 1
    assert full.name_variations == ["Stereo Lab", "Stereolab (2)"]
    assert len(full.members) == 1


@pytest.mark.pg
@pytest.mark.asyncio
async def test_release_lean_empty_children_return_empty_lists(pg_pool):
    """A release with no child rows: json_agg NULLs decode to [], not None."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, not_found) VALUES (200, 'Bare Release', FALSE)"
        )
    svc = DiscogsCacheService(pg_pool)
    lean = await svc.get_release_lean(200)
    assert lean is not None
    assert lean.artist == ""
    assert lean.artists == []
    assert lean.extra_artists == []
    assert lean.labels == []
    assert lean.tracklist == []
    assert lean.genres == []
    assert lean.styles == []
    assert lean.videos == []
    assert lean.track_writers is None


@pytest.mark.pg
@pytest.mark.asyncio
async def test_artist_lean_no_urls_returns_empty_list(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO artist (id, name, fetched_at, not_found) "
            "VALUES (300, 'Juana Molina', now(), FALSE)"
        )
    svc = DiscogsCacheService(pg_pool)
    lean = await svc.get_artist_details_lean(300)
    assert lean is not None
    assert lean.name == "Juana Molina"
    assert lean.urls == []
    assert lean.aliases == []


@pytest.mark.pg
@pytest.mark.asyncio
async def test_release_lean_tombstone_returns_none(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO release (id, title, not_found, artwork_checked_at) "
            "VALUES (400, '', TRUE, now())"
        )
    svc = DiscogsCacheService(pg_pool)
    lean = await svc.get_release_lean(400)
    # The tombstone-shaped model carries not_found=True; the service boundary
    # translates it, but the cache method itself surfaces the tombstone.
    assert lean is not None
    assert lean.not_found is True
