"""Unit tests for discogs/service.py."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from discogs.service import DiscogsService


@pytest.fixture
def service():
    svc = DiscogsService(token="test-token")
    return svc


@pytest.fixture
def service_with_cache(mock_asyncpg_pool):
    cache_svc = AsyncMock()
    svc = DiscogsService(token="test-token", cache_service=cache_svc)
    return svc


# ---------------------------------------------------------------------------
# Init / Client / Close
# ---------------------------------------------------------------------------


class TestDiscogsServiceInit:
    def test_init(self, service):
        assert service.token == "test-token"
        assert service.cache_service is None
        assert service._client is None

    @pytest.mark.asyncio
    async def test_get_client_creates_once(self, service):
        client = await service._get_client()
        assert client is not None
        client2 = await service._get_client()
        assert client is client2
        await service.close()

    @pytest.mark.asyncio
    async def test_close(self, service):
        await service._get_client()
        await service.close()
        assert service._client is None

    @pytest.mark.asyncio
    async def test_close_without_client(self, service):
        await service.close()  # Should not raise


# ---------------------------------------------------------------------------
# check_api
# ---------------------------------------------------------------------------


class TestCheckApi:
    @pytest.mark.asyncio
    async def test_check_api_200(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        assert await service.check_api() is True

    @pytest.mark.asyncio
    async def test_check_api_non_200(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_client.get = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        assert await service.check_api() is False

    @pytest.mark.asyncio
    async def test_check_api_exception(self, service):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
        service._client = mock_client

        assert await service.check_api() is False


# ---------------------------------------------------------------------------
# _request_with_retry
# ---------------------------------------------------------------------------


class TestRequestWithRetry:
    @pytest.mark.asyncio
    async def test_success(self, service):
        mock_client = AsyncMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_client.request = AsyncMock(return_value=mock_resp)
        service._client = mock_client

        resp = await service._request_with_retry("GET", "/test", max_retries=0)
        assert resp is mock_resp

    @pytest.mark.asyncio
    async def test_429_retry(self, service):
        mock_client = AsyncMock()

        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}

        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.headers = {}

        mock_client.request = AsyncMock(side_effect=[resp_429, resp_200])
        service._client = mock_client

        with patch("discogs.service.asyncio.sleep", new_callable=AsyncMock):
            resp = await service._request_with_retry("GET", "/test", max_retries=1)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_max_retries_exhausted(self, service):
        mock_client = AsyncMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {}
        mock_client.request = AsyncMock(return_value=resp_429)
        service._client = mock_client

        with patch("discogs.service.asyncio.sleep", new_callable=AsyncMock):
            resp = await service._request_with_retry("GET", "/test", max_retries=1)
        assert resp is None

    @pytest.mark.asyncio
    async def test_request_error(self, service):
        mock_client = AsyncMock()
        mock_client.request = AsyncMock(side_effect=httpx.RequestError("fail"))
        service._client = mock_client

        resp = await service._request_with_retry("GET", "/test", max_retries=0)
        assert resp is None


# ---------------------------------------------------------------------------
# _parse_title
# ---------------------------------------------------------------------------


class TestParseTitle:
    def test_artist_album(self):
        service = DiscogsService("t")
        assert service._parse_title("Queen - The Game") == ("Queen", "The Game")

    def test_no_separator(self):
        service = DiscogsService("t")
        assert service._parse_title("The Game") == ("", "The Game")


# ---------------------------------------------------------------------------
# _process_search_result
# ---------------------------------------------------------------------------


class TestProcessSearchResult:
    def test_valid_result(self, service):
        seen = set()
        result = service._process_search_result({"title": "Queen - The Game", "id": 123}, seen)
        assert result is not None
        assert result.album == "The Game"
        assert result.artist == "Queen"
        assert "the game" in seen

    def test_empty_title_returns_none(self, service):
        result = service._process_search_result({"title": "", "id": 1}, set())
        assert result is None

    def test_duplicate_skipped(self, service):
        seen = {"the game"}
        result = service._process_search_result({"title": "Queen - The Game", "id": 123}, seen)
        assert result is None

    def test_no_id_returns_none(self, service):
        result = service._process_search_result({"title": "Queen - The Game"}, set())
        assert result is None

    def test_compilation_detection(self, service):
        seen = set()
        result = service._process_search_result(
            {"title": "Various Artists - Compilation Album", "id": 1}, seen
        )
        assert result is not None
        assert result.is_compilation is True


# ---------------------------------------------------------------------------
# search_releases_by_track
# ---------------------------------------------------------------------------


class TestSearchReleasesByTrack:
    @pytest.mark.asyncio
    async def test_api_returns_results(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": [{"title": "Queen - The Game", "id": 123}]}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.search_releases_by_track("Crazy Little Thing", "Queen")

        assert isinstance(result, TrackReleasesResponse)
        assert len(result.releases) >= 1

    @pytest.mark.asyncio
    async def test_cache_hit(self, service_with_cache):
        from discogs.models import ReleaseInfo

        service_with_cache.cache_service.search_releases_by_track = AsyncMock(
            return_value=[
                ReleaseInfo(
                    album="The Game",
                    artist="Queen",
                    release_id=123,
                    release_url="https://discogs.com/release/123",
                )
            ]
        )

        result = await service_with_cache.search_releases_by_track("Song", "Queen")
        assert result.cached is True
        assert len(result.releases) == 1

    @pytest.mark.asyncio
    async def test_cache_error_falls_back_to_api(self, service_with_cache):
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(
            side_effect=Exception("cache down")
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search_releases_by_track("Song", "Queen")
        assert isinstance(result, TrackReleasesResponse)

    @pytest.mark.asyncio
    async def test_supplement_search_when_few_results(self, service):
        """When fewer than 3 results, a supplementary keyword search runs."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.raise_for_status = MagicMock()
        resp1.json.return_value = {"results": [{"title": "Queen - Album1", "id": 1}]}

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.raise_for_status = MagicMock()
        resp2.json.return_value = {"results": [{"title": "Queen - Album2", "id": 2}]}

        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=[resp1, resp2],
        ):
            result = await service.search_releases_by_track("Song", "Queen")

        assert len(result.releases) == 2

    @pytest.mark.asyncio
    async def test_api_exception_returns_empty(self, service):
        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await service.search_releases_by_track("Song")
        assert result.releases == []

    @pytest.mark.asyncio
    async def test_artist_as_keyword_skips_cache(self, service_with_cache):
        """When artist_as_keyword=True, the PG cache must be bypassed.

        The PG cache filters by release-level artist (release_artist table),
        which excludes VA compilations where the artist is credited on
        individual tracks. The artist_as_keyword flag tells the Discogs API
        to use keyword search + format=Compilation instead, so the cache
        must be skipped to let the API handle it.
        """
        from discogs.models import ReleaseInfo

        # Set up cache to return non-VA results (simulating the bug)
        service_with_cache.cache_service.search_releases_by_track = AsyncMock(
            return_value=[
                ReleaseInfo(
                    album="No Way Back",
                    artist="Adonis",
                    release_id=100,
                    release_url="https://discogs.com/release/100",
                )
            ]
        )

        # Set up API to return the VA compilation we actually want
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [
                {
                    "title": "Various - Trax Records 20th Anniversary Collection",
                    "id": 456,
                }
            ]
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search_releases_by_track(
                "No Way Back", "Adonis", artist_as_keyword=True
            )

        # Cache should NOT have been consulted
        service_with_cache.cache_service.search_releases_by_track.assert_not_called()
        # API result should come through
        assert len(result.releases) >= 1
        assert result.releases[0].is_compilation is True
        assert result.cached is False


# ---------------------------------------------------------------------------
# get_release
# ---------------------------------------------------------------------------


class TestGetRelease:
    @pytest.mark.asyncio
    async def test_api_success(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "The Game",
            "artists": [{"name": "Queen"}],
            "year": 1980,
            "labels": [{"name": "EMI"}],
            "genres": ["Rock"],
            "styles": ["Arena Rock"],
            "tracklist": [
                {"position": "1", "title": "Play the Game", "duration": "3:30", "artists": []}
            ],
            "images": [{"uri": "https://img.com/cover.jpg"}],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(12345)

        assert result is not None
        assert result.title == "The Game"
        assert result.artist == "Queen"
        assert result.year == 1980
        assert result.artwork_url == "https://img.com/cover.jpg"
        assert len(result.tracklist) == 1

    @pytest.mark.asyncio
    async def test_cached_release(self, service_with_cache):
        cached = ReleaseMetadataResponse(
            release_id=123,
            title="Cached Album",
            artist="Artist",
            release_url="https://discogs.com/release/123",
            cached=True,
        )
        service_with_cache.cache_service.get_release = AsyncMock(return_value=cached)

        result = await service_with_cache.get_release(123)
        assert result.title == "Cached Album"
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_404_returns_none(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_release(99999)
        assert result is None

    @pytest.mark.asyncio
    async def test_write_back_to_cache(self, service_with_cache):
        service_with_cache.cache_service.get_release = AsyncMock(return_value=None)
        service_with_cache.cache_service.write_release = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "tracklist": [],
            "images": [],
            "labels": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            await service_with_cache.get_release(456)

        service_with_cache.cache_service.write_release.assert_called_once()

    @pytest.mark.asyncio
    async def test_cache_write_error_still_returns(self, service_with_cache):
        service_with_cache.cache_service.get_release = AsyncMock(return_value=None)
        service_with_cache.cache_service.write_release = AsyncMock(
            side_effect=Exception("write fail")
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "tracklist": [],
            "images": [],
            "labels": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.get_release(789)

        assert result is not None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_api_success(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"title": "Queen - The Game", "id": 1, "thumb": "https://img.com/t.jpg"}]
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.search(DiscogsSearchRequest(artist="Queen", album="The Game"))

        assert isinstance(result, DiscogsSearchResponse)
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_fuzzy_fallback_on_empty(self, service):
        """When strict search returns empty, tries fuzzy query."""
        resp_empty = MagicMock()
        resp_empty.status_code = 200
        resp_empty.raise_for_status = MagicMock()
        resp_empty.json.return_value = {"results": []}

        resp_fuzzy = MagicMock()
        resp_fuzzy.status_code = 200
        resp_fuzzy.raise_for_status = MagicMock()
        resp_fuzzy.json.return_value = {
            "results": [{"title": "Queen - Game", "id": 2, "thumb": ""}]
        }

        with patch.object(
            service,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=[resp_empty, resp_fuzzy],
        ):
            result = await service.search(DiscogsSearchRequest(artist="Queen", album="Game"))

        assert len(result.results) >= 1

    @pytest.mark.asyncio
    async def test_no_search_fields_returns_empty(self, service):
        result = await service.search(DiscogsSearchRequest())
        assert result.results == []

    @pytest.mark.asyncio
    async def test_cache_hit(self, service_with_cache):
        service_with_cache.cache_service.search_releases = AsyncMock(
            return_value=[
                {
                    "release_id": 1,
                    "title": "Album",
                    "artist_name": "Artist",
                    "artwork_url": "https://img.com/a.jpg",
                }
            ]
        )

        result = await service_with_cache.search(DiscogsSearchRequest(artist="Artist"))
        assert result.cached is True
        assert len(result.results) == 1

    @pytest.mark.asyncio
    async def test_cache_error_falls_back_to_api(self, service_with_cache):
        service_with_cache.cache_service.search_releases = AsyncMock(
            side_effect=Exception("cache error")
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await service_with_cache.search(DiscogsSearchRequest(artist="Artist"))
        assert isinstance(result, DiscogsSearchResponse)

    @pytest.mark.asyncio
    async def test_spacer_gif_filtered(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"title": "Art - Alb", "id": 1, "thumb": "https://img.com/spacer.gif"}]
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.search(DiscogsSearchRequest(artist="Art"))
        assert result.results[0].artwork_url is None


# ---------------------------------------------------------------------------
# _build_search_params
# ---------------------------------------------------------------------------


class TestBuildSearchParams:
    def test_artist_and_album(self, service):
        params = service._build_search_params(
            DiscogsSearchRequest(artist="Queen", album="The Game")
        )
        assert params["artist"] == "Queen"
        assert params["release_title"] == "The Game"

    def test_artist_and_track(self, service):
        params = service._build_search_params(DiscogsSearchRequest(artist="Queen", track="Song"))
        assert params["release_title"] == "Song"

    def test_no_fields_returns_empty(self, service):
        params = service._build_search_params(DiscogsSearchRequest())
        assert params == {}


# ---------------------------------------------------------------------------
# validate_track_on_release
# ---------------------------------------------------------------------------


class TestValidateTrackOnRelease:
    @pytest.mark.asyncio
    async def test_per_track_artist_match(self, service):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Compilation",
            artist="Various Artists",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="My Song", artists=["The Artist"]),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "My Song", "The Artist")
        assert result is True

    @pytest.mark.asyncio
    async def test_release_artist_match(self, service):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Queen",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="Bohemian Rhapsody"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Bohemian Rhapsody", "Queen")
        assert result is True

    @pytest.mark.asyncio
    async def test_not_found(self, service):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Queen",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="Other Song"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Missing Song", "Queen")
        assert result is False

    @pytest.mark.asyncio
    async def test_release_not_found(self, service):
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=None):
            result = await service.validate_track_on_release(1, "Song", "Artist")
        assert result is False

    @pytest.mark.asyncio
    async def test_quoted_artist_name_matches(self, service):
        """Discogs formats some artist names with quotes, e.g. '"Weird Al" Yankovic'.

        validate_track_on_release must strip quotes before comparing so that
        the user-supplied 'Weird Al Yankovic' matches the Discogs-formatted
        '"Weird Al" Yankovic'.
        """
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Poodle Hat",
            artist='"Weird Al" Yankovic',
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(position="1", title="Couch Potato"),
                TrackItem(position="2", title="Hardware Store"),
                TrackItem(position="3", title="Trash Day"),
                TrackItem(position="4", title="Party At The Leper Colony"),
                TrackItem(position="5", title="Angry White Boy Polka"),
                TrackItem(position="6", title="Wanna B Ur Lovr"),
                TrackItem(position="7", title="A Complicated Song"),
                TrackItem(position="8", title="Why Does This Always Happen To Me?"),
                TrackItem(position="9", title="Ode To A Superhero"),
                TrackItem(position="10", title="Bob"),
                TrackItem(position="11", title="Genius In France"),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Bob", "Weird Al Yankovic")
        assert result is True

    @pytest.mark.asyncio
    async def test_quoted_per_track_artist_matches(self, service):
        """Per-track artists on compilations may also have Discogs quote formatting."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Compilation",
            artist="Various Artists",
            release_url="https://discogs.com/release/1",
            tracklist=[
                TrackItem(
                    position="1",
                    title="Bob",
                    artists=['"Weird Al" Yankovic'],
                ),
            ],
        )
        with patch.object(service, "get_release", new_callable=AsyncMock, return_value=release):
            result = await service.validate_track_on_release(1, "Bob", "Weird Al Yankovic")
        assert result is True

    @pytest.mark.asyncio
    async def test_cache_validated(self, service_with_cache):
        service_with_cache.cache_service.validate_track_on_release = AsyncMock(return_value=True)

        result = await service_with_cache.validate_track_on_release(1, "Song", "Artist")
        assert result is True

    @pytest.mark.asyncio
    async def test_cache_miss_falls_back_to_api(self, service_with_cache):
        service_with_cache.cache_service.validate_track_on_release = AsyncMock(return_value=None)

        release = ReleaseMetadataResponse(
            release_id=1,
            title="Album",
            artist="Queen",
            release_url="https://discogs.com/release/1",
            tracklist=[TrackItem(position="1", title="Song")],
        )
        with patch.object(
            service_with_cache, "get_release", new_callable=AsyncMock, return_value=release
        ):
            result = await service_with_cache.validate_track_on_release(1, "Song", "Queen")
        assert result is True


# ---------------------------------------------------------------------------
# get_artist_image
# ---------------------------------------------------------------------------


class TestGetArtistImage:
    @pytest.mark.asyncio
    async def test_returns_uri(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 77,
            "name": "Autechre",
            "images": [
                {"uri": "https://i.discogs.com/artist-primary.jpg", "type": "primary"},
                {"uri": "https://i.discogs.com/artist-secondary.jpg", "type": "secondary"},
            ],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_image(77)

        assert result == "https://i.discogs.com/artist-primary.jpg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_images(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": 77, "name": "Autechre", "images": []}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_image(77)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("Not Found"))
        mock_resp.json.return_value = {}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_image(77)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_rate_limit(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_artist_image(77)

        assert result is None


# ---------------------------------------------------------------------------
# get_label_image
# ---------------------------------------------------------------------------


class TestGetLabelImage:
    @pytest.mark.asyncio
    async def test_returns_uri(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 233,
            "name": "Warp Records",
            "images": [{"uri": "https://i.discogs.com/label-logo.jpg", "type": "primary"}],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_label_image(233)

        assert result == "https://i.discogs.com/label-logo.jpg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_images(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"id": 233, "name": "Warp Records", "images": []}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_label_image(233)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_api_failure(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status = MagicMock(side_effect=Exception("Not Found"))
        mock_resp.json.return_value = {}

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_label_image(233)

        assert result is None


# ---------------------------------------------------------------------------
# get_release extracts artist_id / label_id
# ---------------------------------------------------------------------------


class TestGetReleaseExtractsIds:
    @pytest.mark.asyncio
    async def test_extracts_artist_and_label_ids(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"id": 77, "name": "Autechre"}],
            "labels": [{"id": 233, "name": "Warp Records"}],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert result.artist_id == 77
        assert result.label_id == 233

    @pytest.mark.asyncio
    async def test_handles_missing_ids(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"name": "Autechre"}],  # no id
            "labels": [],  # no labels
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert result.artist_id is None
        assert result.label_id is None


# ---------------------------------------------------------------------------
# get_release parses enriched data (multi-artist, labels, extra artists)
# ---------------------------------------------------------------------------


class TestGetReleaseEnrichedParsing:
    @pytest.mark.asyncio
    async def test_parses_multiple_artists(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Duke Ellington & John Coltrane",
            "artists": [
                {"id": 100, "name": "Duke Ellington", "join": " & "},
                {"id": 101, "name": "John Coltrane", "join": ""},
            ],
            "labels": [{"id": 500, "name": "Impulse Records"}],
            "tracklist": [],
            "images": [],
            "genres": ["Jazz"],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(12345)

        assert result is not None
        assert len(result.artists) == 2
        assert result.artists[0].artist_id == 100
        assert result.artists[0].name == "Duke Ellington"
        assert result.artists[0].join == " & "
        assert result.artists[1].artist_id == 101
        assert result.artists[1].name == "John Coltrane"
        # Backward compat: scalar artist is first artist
        assert result.artist == "Duke Ellington"
        assert result.artist_id == 100

    @pytest.mark.asyncio
    async def test_parses_extra_artists(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"id": 77, "name": "Autechre"}],
            "extraartists": [
                {"id": 200, "name": "Rob Brown", "role": "Producer"},
                {"id": 201, "name": "Sean Booth", "role": "Producer"},
            ],
            "labels": [],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert len(result.extra_artists) == 2
        assert result.extra_artists[0].artist_id == 200
        assert result.extra_artists[0].name == "Rob Brown"
        assert result.extra_artists[0].role == "Producer"

    @pytest.mark.asyncio
    async def test_parses_multiple_labels(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Aluminum Tunes",
            "artists": [{"id": 1, "name": "Stereolab"}],
            "labels": [
                {"id": 233, "name": "Duophonic", "catno": "D-UHF-CD 19"},
                {"id": 400, "name": "Elektra", "catno": "62302-2"},
            ],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(55555)

        assert result is not None
        assert len(result.labels) == 2
        assert result.labels[0].label_id == 233
        assert result.labels[0].name == "Duophonic"
        assert result.labels[0].catno == "D-UHF-CD 19"
        assert result.labels[1].label_id == 400
        # Backward compat: scalar label is first label
        assert result.label == "Duophonic"
        assert result.label_id == 233

    @pytest.mark.asyncio
    async def test_parses_released_date(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Confield",
            "artists": [{"id": 77, "name": "Autechre"}],
            "labels": [],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
            "released": "2001-04-30",
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(28138)

        assert result is not None
        assert result.released == "2001-04-30"

    @pytest.mark.asyncio
    async def test_empty_artists_and_labels(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "title": "Untitled",
            "artists": [],
            "labels": [],
            "tracklist": [],
            "images": [],
            "genres": [],
            "styles": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_release(99999)

        assert result is not None
        assert result.artists == []
        assert result.extra_artists == []
        assert result.labels == []
        assert result.artist == ""
        assert result.artist_id is None


# ---------------------------------------------------------------------------
# get_artist_details
# ---------------------------------------------------------------------------


class TestGetArtistDetails:
    @pytest.mark.asyncio
    async def test_parses_full_artist_data(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 77,
            "name": "Autechre",
            "profile": "Electronic duo from Rochdale, England.",
            "namevariations": ["Ae", "Autechre."],
            "aliases": [{"id": 500, "name": "Gescom"}],
            "members": [
                {"id": 200, "name": "Rob Brown", "active": True},
                {"id": 201, "name": "Sean Booth", "active": True},
            ],
            "urls": ["https://autechre.ws", "https://warp.net/artists/autechre"],
            "images": [
                {"uri": "https://i.discogs.com/autechre.jpg", "type": "primary"},
            ],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_details(77)

        assert result is not None
        assert result.artist_id == 77
        assert result.name == "Autechre"
        assert result.profile == "Electronic duo from Rochdale, England."
        assert result.image_url == "https://i.discogs.com/autechre.jpg"
        assert result.name_variations == ["Ae", "Autechre."]
        assert len(result.aliases) == 1
        assert result.aliases[0].id == 500
        assert result.aliases[0].name == "Gescom"
        assert len(result.members) == 2
        assert result.members[0].name == "Rob Brown"
        assert result.urls == ["https://autechre.ws", "https://warp.net/artists/autechre"]

    @pytest.mark.asyncio
    async def test_handles_minimal_response(self, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 1,
            "name": "Unknown Artist",
            "images": [],
        }

        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=mock_resp
        ):
            result = await service.get_artist_details(1)

        assert result is not None
        assert result.artist_id == 1
        assert result.name == "Unknown Artist"
        assert result.profile is None
        assert result.image_url is None
        assert result.name_variations == []
        assert result.aliases == []
        assert result.members == []
        assert result.urls == []

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self, service):
        with patch.object(
            service, "_request_with_retry", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_artist_details(77)

        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit(self, service_with_cache):
        from discogs.models import ArtistDetails

        cached_details = ArtistDetails(
            artist_id=77, name="Autechre", cached=True
        )
        service_with_cache.cache_service.get_artist_details = AsyncMock(
            return_value=cached_details
        )

        result = await service_with_cache.get_artist_details(77)
        assert result is not None
        assert result.artist_id == 77
        assert result.cached is True

    @pytest.mark.asyncio
    async def test_writes_back_to_cache(self, service_with_cache):
        service_with_cache.cache_service.get_artist_details = AsyncMock(return_value=None)
        service_with_cache.cache_service.write_artist_details = AsyncMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "id": 77,
            "name": "Autechre",
            "images": [],
        }

        with patch.object(
            service_with_cache,
            "_request_with_retry",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            await service_with_cache.get_artist_details(77)

        service_with_cache.cache_service.write_artist_details.assert_called_once()


# ---------------------------------------------------------------------------
# get_artist_image delegates to get_artist_details
# ---------------------------------------------------------------------------


class TestGetArtistImageDelegation:
    @pytest.mark.asyncio
    async def test_delegates_to_get_artist_details(self, service):
        from discogs.models import ArtistDetails

        details = ArtistDetails(
            artist_id=77,
            name="Autechre",
            image_url="https://i.discogs.com/autechre.jpg",
        )
        with patch.object(
            service, "get_artist_details", new_callable=AsyncMock, return_value=details
        ):
            result = await service.get_artist_image(77)

        assert result == "https://i.discogs.com/autechre.jpg"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_details(self, service):
        with patch.object(
            service, "get_artist_details", new_callable=AsyncMock, return_value=None
        ):
            result = await service.get_artist_image(77)

        assert result is None
