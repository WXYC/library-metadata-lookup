"""Unit tests for discogs/cache_service.py."""

from unittest.mock import AsyncMock

import pytest

from discogs.cache_service import CacheUnavailableError, DiscogsCacheService
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    LabelCredit,
    MemberRef,
    ReleaseMetadataResponse,
    TrackItem,
)


def make_fetch_router(**table_results):
    """Create a mock side_effect that routes by table name in query.

    With asyncio.gather, the consumption order of side_effect entries is
    non-deterministic. This routes by matching table names in the SQL query.
    More specific table names (e.g., release_track_artist) must be checked
    before less specific ones (e.g., release_track).
    """
    # Sort keys longest-first so "release_track_artist" matches before "release_track"
    sorted_tables = sorted(table_results.keys(), key=len, reverse=True)

    async def route(query, *args):
        for table_name in sorted_tables:
            if table_name in query:
                return table_results[table_name]
        return []

    return route


@pytest.fixture
def cache_service(mock_asyncpg_pool):
    return DiscogsCacheService(mock_asyncpg_pool)


# ---------------------------------------------------------------------------
# is_available
# ---------------------------------------------------------------------------


class TestIsAvailable:
    @pytest.mark.asyncio
    async def test_healthy(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=1)
        assert await cache_service.is_available() is True

    @pytest.mark.asyncio
    async def test_exception(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(side_effect=Exception("down"))
        assert await cache_service.is_available() is False


# ---------------------------------------------------------------------------
# search_releases_by_track
# ---------------------------------------------------------------------------


class TestSearchReleasesByTrack:
    @pytest.mark.asyncio
    async def test_returns_results(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "track_title": "Song",
                    "is_compilation": False,
                }
            ]
        )

        results = await cache_service.search_releases_by_track("Song", "Artist")
        assert len(results) == 1
        assert results[0].album == "Album"

    @pytest.mark.asyncio
    async def test_deduplicates(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "A",
                    "track_title": "S",
                    "is_compilation": False,
                },
                {
                    "release_id": 2,
                    "title": "Album",
                    "artist_name": "A",
                    "track_title": "S",
                    "is_compilation": False,
                },
            ]
        )

        results = await cache_service.search_releases_by_track("S")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_respects_limit(self, cache_service, mock_asyncpg_pool):
        rows = [
            {
                "release_id": i,
                "title": f"Album{i}",
                "artist_name": "A",
                "track_title": "S",
                "is_compilation": False,
            }
            for i in range(10)
        ]
        mock_asyncpg_pool.fetch = AsyncMock(return_value=rows)

        results = await cache_service.search_releases_by_track("S", limit=3)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_returns_va_compilation_via_track_artist(self, cache_service, mock_asyncpg_pool):
        """VA compilation is returned when the track-level artist matches."""
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 99,
                    "title": "Nao Wave- Brazilian Punk 82-88",
                    "artist_name": "Various Artists",
                    "track_title": "Ciencias Sensuais",
                    "is_compilation": True,
                }
            ]
        )

        results = await cache_service.search_releases_by_track("Ciencias Sensuais", "Azul 29")
        assert len(results) == 1
        assert results[0].album == "Nao Wave- Brazilian Punk 82-88"
        assert results[0].is_compilation is True

    @pytest.mark.asyncio
    async def test_query_includes_track_artist_join(self, cache_service, mock_asyncpg_pool):
        """SQL query joins release_track_artist for track-level artist matching."""
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])

        await cache_service.search_releases_by_track("Song", "Artist")

        sql = mock_asyncpg_pool.fetch.call_args[0][0]
        assert "release_track_artist" in sql
        assert "rta.artist_name" in sql

    @pytest.mark.asyncio
    async def test_deduplicates_multiple_track_artists(self, cache_service, mock_asyncpg_pool):
        """Multiple rows from LEFT JOIN (different track artists) are deduplicated."""
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 99,
                    "title": "Compilation",
                    "artist_name": "Various Artists",
                    "track_title": "Track",
                    "is_compilation": True,
                },
                {
                    "release_id": 99,
                    "title": "Compilation",
                    "artist_name": "Various Artists",
                    "track_title": "Track",
                    "is_compilation": True,
                },
            ]
        )

        results = await cache_service.search_releases_by_track("Track", "Artist A")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_error_raises_cache_unavailable(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=Exception("db error"))

        with pytest.raises(CacheUnavailableError):
            await cache_service.search_releases_by_track("S")


# ---------------------------------------------------------------------------
# get_release
# ---------------------------------------------------------------------------


class TestGetRelease:
    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        result = await cache_service.get_release(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_full_metadata(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 123,
                "title": "The Game",
                "release_year": 1980,
                "artwork_url": "https://img.com/a.jpg",
                "released": None,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[
                    {"position": "1", "title": "Play the Game", "duration": "3:30", "sequence": 1}
                ],
                release_artist=[
                    {"artist_id": None, "artist_name": "Queen", "extra": 0, "role": None}
                ],
                release_label=[],
                release_genre=[{"genre": "Rock"}],
                release_style=[{"style": "Arena Rock"}, {"style": "Pop Rock"}],
            )
        )

        result = await cache_service.get_release(123)
        assert result is not None
        assert result.title == "The Game"
        assert result.artist == "Queen"
        assert result.genres == ["Rock"]
        assert result.styles == ["Arena Rock", "Pop Rock"]
        assert len(result.tracklist) == 1
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_with_track_artists(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "title": "Compilation",
                "release_year": 2000,
                "artwork_url": None,
                "released": None,
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[{"track_sequence": 1, "artist_name": "Some Artist"}],
                release_track=[
                    {"position": "1", "title": "Track1", "duration": None, "sequence": 1}
                ],
                release_artist=[
                    {"artist_id": None, "artist_name": "Various Artists", "extra": 0, "role": None}
                ],
                release_label=[],
                release_genre=[],
                release_style=[],
            )
        )

        result = await cache_service.get_release(1)
        assert result.tracklist[0].artists == ["Some Artist"]

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.get_release(1)


# ---------------------------------------------------------------------------
# write_release
# ---------------------------------------------------------------------------


class TestWriteRelease:
    @pytest.mark.asyncio
    async def test_writes_release(self, cache_service, mock_asyncpg_pool):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Artist",
            year=2020,
            artwork_url="https://img.com/a.jpg",
            tracklist=[TrackItem(position="1", title="Track1", artists=["ArtistA"])],
            release_url="https://discogs.com/release/1",
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn
        assert conn.execute.call_count >= 3  # insert release, artist, delete tracks, cache_metadata
        assert conn.executemany.call_count >= 1  # insert tracks

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=Exception("fail"))
        release = ReleaseMetadataResponse(
            release_id=1,
            title="A",
            artist="B",
            release_url="https://discogs.com/release/1",
        )
        with pytest.raises(CacheUnavailableError):
            await cache_service.write_release(release)

    @pytest.mark.asyncio
    async def test_writes_enriched_release(self, cache_service, mock_asyncpg_pool):
        """write_release persists all artists, extra artists, labels, and released."""
        release = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            year=2001,
            artwork_url="https://img.com/confield.jpg",
            release_url="https://discogs.com/release/28138",
            genres=["Electronic"],
            styles=["IDM", "Abstract"],
            artists=[
                ArtistCredit(artist_id=77, name="Autechre"),
            ],
            extra_artists=[
                ArtistCredit(artist_id=200, name="Rob Brown", role="Producer"),
                ArtistCredit(artist_id=201, name="Sean Booth", role="Producer"),
            ],
            labels=[
                LabelCredit(label_id=233, name="Warp Records", catno="WAP 159 CD"),
            ],
            released="2001-04-30",
            tracklist=[TrackItem(position="1", title="VI Scose Poise")],
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn

        # Check the release row includes 'released'
        release_call = conn.execute.call_args_list[0]
        release_sql = release_call[0][0]
        assert "released" in release_sql

        # Check release_artist rows were deleted then re-inserted (not just a single ON CONFLICT)
        all_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert any("DELETE FROM release_artist" in sql for sql in all_sql)

        # Check release_label rows were written
        assert any("release_label" in sql for sql in all_sql)

        # Check genre/style rows were written
        assert any("DELETE FROM release_genre" in sql for sql in all_sql)
        assert any("DELETE FROM release_style" in sql for sql in all_sql)
        all_executemany_sql = [call[0][0] for call in conn.executemany.call_args_list]
        assert any("release_genre" in sql for sql in all_executemany_sql)
        assert any("release_style" in sql for sql in all_executemany_sql)

    @pytest.mark.asyncio
    async def test_writes_multiple_artists_as_rows(self, cache_service, mock_asyncpg_pool):
        """write_release writes ALL artists (main + extra) as separate rows."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Duke Ellington & John Coltrane",
            artist="Duke Ellington",
            release_url="https://discogs.com/release/1",
            artists=[
                ArtistCredit(artist_id=100, name="Duke Ellington", join=" & "),
                ArtistCredit(artist_id=101, name="John Coltrane"),
            ],
            extra_artists=[
                ArtistCredit(artist_id=300, name="Engineer Bob", role="Engineer"),
            ],
        )

        await cache_service.write_release(release)
        conn = mock_asyncpg_pool._mock_conn

        # Should use executemany for artist inserts (3 total: 2 main + 1 extra)
        executemany_calls = conn.executemany.call_args_list
        artist_inserts = [c for c in executemany_calls if "release_artist" in c[0][0]]
        assert len(artist_inserts) >= 1
        # The data should contain all 3 artists
        artist_data = artist_inserts[0][0][1]
        assert len(artist_data) == 3


# ---------------------------------------------------------------------------
# get_release (enriched)
# ---------------------------------------------------------------------------


class TestGetReleaseEnriched:
    @pytest.mark.asyncio
    async def test_reads_enriched_metadata(self, cache_service, mock_asyncpg_pool):
        """get_release returns enriched artist, label, and released data."""
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 28138,
                "title": "Confield",
                "release_year": 2001,
                "artwork_url": "https://img.com/a.jpg",
                "released": "2001-04-30",
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[
                    {"position": "1", "title": "VI Scose Poise", "duration": "5:30", "sequence": 1}
                ],
                release_artist=[
                    {"artist_id": 77, "artist_name": "Autechre", "extra": 0, "role": None},
                    {"artist_id": 200, "artist_name": "Rob Brown", "extra": 1, "role": "Producer"},
                ],
                release_label=[
                    {"label_id": 233, "label_name": "Warp Records", "catno": "WAP 159 CD"},
                ],
            )
        )

        result = await cache_service.get_release(28138)
        assert result is not None
        assert result.released == "2001-04-30"
        assert len(result.artists) == 1
        assert result.artists[0].artist_id == 77
        assert result.artists[0].name == "Autechre"
        assert len(result.extra_artists) == 1
        assert result.extra_artists[0].name == "Rob Brown"
        assert result.extra_artists[0].role == "Producer"
        assert len(result.labels) == 1
        assert result.labels[0].label_id == 233
        assert result.labels[0].name == "Warp Records"
        assert result.labels[0].catno == "WAP 159 CD"
        # Backward compat scalars
        assert result.artist == "Autechre"
        assert result.label == "Warp Records"


# ---------------------------------------------------------------------------
# search_releases
# ---------------------------------------------------------------------------


class TestSearchReleases:
    @pytest.mark.asyncio
    async def test_no_params_returns_empty(self, cache_service):
        result = await cache_service.search_releases()
        assert result == []

    @pytest.mark.asyncio
    async def test_artist_and_album(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artwork_url": None,
                    "score": 0.8,
                }
            ]
        )
        result = await cache_service.search_releases(artist="Artist", album="Album")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_artist_only(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artwork_url": None,
                    "score": 0.8,
                }
            ]
        )
        result = await cache_service.search_releases(artist="Artist")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_album_only(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artwork_url": None,
                    "score": 0.8,
                }
            ]
        )
        result = await cache_service.search_releases(album="Album")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_deduplicates(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "A1",
                    "artwork_url": None,
                    "score": 0.8,
                },
                {
                    "release_id": 2,
                    "title": "Album",
                    "artist_name": "A2",
                    "artwork_url": None,
                    "score": 0.7,
                },
            ]
        )
        result = await cache_service.search_releases(artist="A1")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_artist_and_album_query_uses_and_logic(self, cache_service, mock_asyncpg_pool):
        """When both artist and album are provided, the SQL must require BOTH to match (AND)."""
        mock_asyncpg_pool.fetch = AsyncMock(return_value=[])
        await cache_service.search_releases(artist="High Rise", album="Disallow")

        query_sql = mock_asyncpg_pool.fetch.call_args[0][0]
        # Normalize whitespace for reliable matching
        normalized = " ".join(query_sql.split())
        assert "AND lower(f_unaccent(ra.artist_name))" in normalized, (
            f"Expected AND between title and artist_name conditions, got: {normalized}"
        )
        assert "OR lower(f_unaccent(ra.artist_name))" not in normalized, (
            f"Expected no OR between title and artist_name conditions, got: {normalized}"
        )

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetch = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.search_releases(artist="A")


# ---------------------------------------------------------------------------
# validate_track_on_release
# ---------------------------------------------------------------------------


class TestValidateTrackOnRelease:
    @pytest.mark.asyncio
    async def test_not_cached_returns_none(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=False)
        result = await cache_service.validate_track_on_release(999, "Song", "Artist")
        assert result is None

    @pytest.mark.asyncio
    async def test_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "Song"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "Artist"})
        result = await cache_service.validate_track_on_release(1, "Song", "Artist")
        assert result is True

    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[],
                release_track=[{"sequence": 1, "title": "Other Song"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "Artist"})
        result = await cache_service.validate_track_on_release(1, "Missing Song", "Artist")
        assert result is False

    @pytest.mark.asyncio
    async def test_diacritics_in_track_title(self, cache_service, mock_asyncpg_pool):
        """Track title with diacritics should match the unaccented search query."""
        mock_asyncpg_pool.fetchval = AsyncMock(return_value=True)
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                release_track_artist=[{"track_sequence": 1, "artist_name": "Azul 29"}],
                release_track=[{"sequence": 1, "title": "Ciências Sensuais"}],
            )
        )
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value={"artist_name": "Various Artists"})
        result = await cache_service.validate_track_on_release(1, "Ciencias Sensuais", "Azul 29")
        assert result is True

    @pytest.mark.asyncio
    async def test_error_raises(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchval = AsyncMock(side_effect=Exception("db error"))
        with pytest.raises(CacheUnavailableError):
            await cache_service.validate_track_on_release(1, "Song", "Artist")


# ---------------------------------------------------------------------------
# Artist detail caching
# ---------------------------------------------------------------------------


class TestGetArtistDetails:
    @pytest.mark.asyncio
    async def test_not_found(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(return_value=None)
        result = await cache_service.get_artist_details(999)
        assert result is None

    @pytest.mark.asyncio
    async def test_full_details(self, cache_service, mock_asyncpg_pool):
        mock_asyncpg_pool.fetchrow = AsyncMock(
            return_value={
                "id": 77,
                "name": "Autechre",
                "profile": "Electronic duo from Rochdale.",
                "image_url": "https://i.discogs.com/autechre.jpg",
            }
        )
        mock_asyncpg_pool.fetch = AsyncMock(
            side_effect=make_fetch_router(
                artist_alias=[{"alias_id": 500, "alias_name": "Gescom"}],
                artist_name_variation=[{"name": "Ae"}, {"name": "Autechre."}],
                artist_member=[{"member_id": 200, "member_name": "Rob Brown", "active": True}],
                artist_url=[{"url": "https://autechre.ws"}],
            )
        )

        result = await cache_service.get_artist_details(77)
        assert result is not None
        assert result.artist_id == 77
        assert result.name == "Autechre"
        assert result.profile == "Electronic duo from Rochdale."
        assert result.image_url == "https://i.discogs.com/autechre.jpg"
        assert len(result.aliases) == 1
        assert result.aliases[0].name == "Gescom"
        assert result.name_variations == ["Ae", "Autechre."]
        assert len(result.members) == 1
        assert result.members[0].name == "Rob Brown"
        assert result.urls == ["https://autechre.ws"]
        assert result.cached is True


class TestWriteArtistDetails:
    @pytest.mark.asyncio
    async def test_writes_artist_details(self, cache_service, mock_asyncpg_pool):
        details = ArtistDetails(
            artist_id=77,
            name="Autechre",
            profile="Electronic duo.",
            image_url="https://i.discogs.com/autechre.jpg",
            aliases=[ArtistRef(id=500, name="Gescom")],
            name_variations=["Ae"],
            members=[MemberRef(id=200, name="Rob Brown", active=True)],
            urls=["https://autechre.ws"],
        )

        await cache_service.write_artist_details(details)
        conn = mock_asyncpg_pool._mock_conn

        # Should upsert artist row
        artist_call = conn.execute.call_args_list[0]
        assert "artist" in artist_call[0][0].lower()

        # Should write child tables
        all_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert any("artist_alias" in sql for sql in all_sql)
        assert any("artist_name_variation" in sql for sql in all_sql)
        assert any("artist_member" in sql for sql in all_sql)
        assert any("artist_url" in sql for sql in all_sql)
