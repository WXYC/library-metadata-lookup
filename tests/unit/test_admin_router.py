"""Unit tests for routers/admin.py -- library.db upload endpoint."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from tests.unit.conftest import override_deps


def _make_valid_sqlite_db(path, streaming_ids=None) -> int:
    """Create a minimal valid SQLite library database at ``path``.

    Args:
        streaming_ids: Optional list of library_ids to insert into streaming_links.

    Returns row count.
    """
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE library ("
        "id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
        "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
        "genre TEXT, format TEXT)"
    )
    conn.execute(
        "INSERT INTO library (id, title, artist, call_letters) "
        "VALUES (1, 'Aluminum Tunes', 'Stereolab', 'S')"
    )
    if streaming_ids is not None:
        conn.execute(
            "CREATE TABLE streaming_links ("
            "library_id INTEGER PRIMARY KEY, spotify_url TEXT, apple_music_url TEXT, "
            "deezer_url TEXT, bandcamp_url TEXT, tidal_url TEXT, "
            "youtube_music_url TEXT, soundcloud_url TEXT)"
        )
        for lib_id in streaming_ids:
            conn.execute(
                "INSERT INTO streaming_links (library_id, spotify_url) VALUES (?, ?)",
                (lib_id, f"https://open.spotify.com/album/{lib_id}"),
            )
    conn.commit()
    count = conn.execute("SELECT count(*) FROM library").fetchone()[0]
    conn.close()
    return count


@pytest.fixture
def admin_settings(tmp_path):
    """Settings configured for admin endpoint testing."""
    return Settings(
        admin_token="test-secret-token",
        library_db_path=tmp_path / "library.db",
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


@pytest.fixture
def no_token_settings(tmp_path):
    """Settings with no admin_token configured."""
    return Settings(
        admin_token=None,
        library_db_path=tmp_path / "library.db",
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


@pytest.fixture
def webhook_settings(tmp_path):
    """Settings configured for webhook testing."""
    return Settings(
        admin_token="test-secret-token",
        library_db_path=tmp_path / "library.db",
        streaming_webhook_urls="https://tubafrenzy.example.com/webhook",
        etl_notify_key="test-notify-key",
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


@pytest.fixture
def multi_webhook_settings(tmp_path):
    """Settings with multiple webhook URLs configured."""
    return Settings(
        admin_token="test-secret-token",
        library_db_path=tmp_path / "library.db",
        streaming_webhook_urls=(
            "https://tubafrenzy.example.com/webhook,https://backend.example.com/webhook"
        ),
        etl_notify_key="test-notify-key",
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


class TestUploadLibraryDB:
    @pytest.mark.asyncio
    async def test_successful_upload(self, tmp_path, admin_settings):
        """Upload a valid SQLite file and get 200 with row count."""
        from main import app

        db_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(db_file)

        mock_db = AsyncMock()
        mock_db.is_available = AsyncMock(return_value=True)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: admin_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with open(db_file, "rb") as f:
                    resp = await client.post(
                        "/admin/upload-library-db",
                        headers={"Authorization": "Bearer test-secret-token"},
                        files={"file": ("library.db", f, "application/octet-stream")},
                    )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["row_count"] == 1
        assert "timestamp" in body

    @pytest.mark.asyncio
    async def test_invalid_sqlite_file(self, tmp_path, admin_settings):
        """Upload a non-SQLite file and get 400."""
        from main import app

        bad_file = tmp_path / "bad.db"
        bad_file.write_text("this is not a database")

        mock_db = AsyncMock()

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: admin_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with open(bad_file, "rb") as f:
                    resp = await client.post(
                        "/admin/upload-library-db",
                        headers={"Authorization": "Bearer test-secret-token"},
                        files={"file": ("library.db", f, "application/octet-stream")},
                    )

        assert resp.status_code == 400
        assert "Invalid SQLite database" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_sqlite_missing_library_table(self, tmp_path, admin_settings):
        """Upload a valid SQLite file that lacks the 'library' table -> 400."""
        from main import app

        db_file = tmp_path / "no_table.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.close()

        mock_db = AsyncMock()

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: admin_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with open(db_file, "rb") as f:
                    resp = await client.post(
                        "/admin/upload-library-db",
                        headers={"Authorization": "Bearer test-secret-token"},
                        files={"file": ("library.db", f, "application/octet-stream")},
                    )

        assert resp.status_code == 400
        assert "Invalid SQLite database" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_auth_header(self, admin_settings):
        """No Authorization header -> 401."""
        from main import app

        mock_db = AsyncMock()

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: admin_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/upload-library-db",
                    files={"file": ("library.db", b"data", "application/octet-stream")},
                )

        assert resp.status_code == 401
        assert resp.json()["detail"] == "Missing authorization"

    @pytest.mark.asyncio
    async def test_wrong_bearer_token(self, admin_settings):
        """Wrong token -> 403."""
        from main import app

        mock_db = AsyncMock()

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: admin_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/upload-library-db",
                    headers={"Authorization": "Bearer wrong-token"},
                    files={"file": ("library.db", b"data", "application/octet-stream")},
                )

        assert resp.status_code == 403
        assert resp.json()["detail"] == "Invalid token"

    @pytest.mark.asyncio
    async def test_no_admin_token_configured(self, no_token_settings):
        """ADMIN_TOKEN not set -> endpoint returns 403."""
        from main import app

        mock_db = AsyncMock()

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: no_token_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/admin/upload-library-db",
                    headers={"Authorization": "Bearer anything"},
                    files={"file": ("library.db", b"data", "application/octet-stream")},
                )

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_upload_triggers_db_reconnection(self, tmp_path, admin_settings):
        """After upload, close_library_db is called so next request gets new data."""
        from main import app

        db_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(db_file)

        mock_db = AsyncMock()
        mock_db.is_available = AsyncMock(return_value=True)

        with (
            override_deps(
                app,
                {
                    get_library_db: mock_db,
                    get_discogs_service: None,
                    get_posthog_client: None,
                    get_settings: admin_settings,
                },
            ),
            patch("routers.admin.close_library_db", new_callable=AsyncMock) as mock_close,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with open(db_file, "rb") as f:
                    resp = await client.post(
                        "/admin/upload-library-db",
                        headers={"Authorization": "Bearer test-secret-token"},
                        files={"file": ("library.db", f, "application/octet-stream")},
                    )

        assert resp.status_code == 200
        mock_close.assert_called_once()


class TestGetStreamingIds:
    def test_missing_file(self, tmp_path):
        """Non-existent file returns empty set."""
        from routers.admin import _get_streaming_ids

        result = _get_streaming_ids(tmp_path / "nonexistent.db")
        assert result == set()

    def test_no_streaming_table(self, tmp_path):
        """DB without streaming_links table returns empty set."""
        from routers.admin import _get_streaming_ids

        db_path = tmp_path / "library.db"
        _make_valid_sqlite_db(db_path)
        result = _get_streaming_ids(db_path)
        assert result == set()

    def test_returns_ids(self, tmp_path):
        """DB with streaming_links returns the set of library_ids."""
        from routers.admin import _get_streaming_ids

        db_path = tmp_path / "library.db"
        _make_valid_sqlite_db(db_path, streaming_ids=[10, 20, 30])
        result = _get_streaming_ids(db_path)
        assert result == {10, 20, 30}


class TestComputeStreamingDiff:
    def test_added_and_removed(self):
        """Computes additions and removals correctly."""
        from routers.admin import _compute_streaming_diff

        changes = _compute_streaming_diff(old_ids={1, 2, 3}, new_ids={2, 3, 4})
        assert changes == [
            {"library_release_id": 4, "on_streaming": True},
            {"library_release_id": 1, "on_streaming": False},
        ]

    def test_identical(self):
        """Identical sets produce no changes."""
        from routers.admin import _compute_streaming_diff

        assert _compute_streaming_diff(old_ids={1, 2}, new_ids={1, 2}) == []

    def test_all_new(self):
        """Empty old set means everything is added."""
        from routers.admin import _compute_streaming_diff

        changes = _compute_streaming_diff(old_ids=set(), new_ids={5, 6})
        assert changes == [
            {"library_release_id": 5, "on_streaming": True},
            {"library_release_id": 6, "on_streaming": True},
        ]

    def test_all_removed(self):
        """Empty new set means everything is removed."""
        from routers.admin import _compute_streaming_diff

        changes = _compute_streaming_diff(old_ids={7, 8}, new_ids=set())
        assert changes == [
            {"library_release_id": 7, "on_streaming": False},
            {"library_release_id": 8, "on_streaming": False},
        ]


def _mock_httpx_client(response_status=200, side_effect=None):
    """Create a mock httpx.AsyncClient context manager.

    Returns (patcher, mock_post) where mock_post is the AsyncMock for client.post().
    """
    mock_post = AsyncMock(
        side_effect=side_effect,
        return_value=httpx.Response(response_status, request=httpx.Request("POST", "http://test")),
    )
    mock_client = AsyncMock()
    mock_client.post = mock_post
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    patcher = patch("routers.admin.httpx.AsyncClient", return_value=mock_client)
    return patcher, mock_post


class TestSendStreamingWebhook:
    @pytest.mark.asyncio
    async def test_success(self):
        """Successful POST returns sent status with changes_count."""
        from routers.admin import _send_streaming_webhook

        patcher, mock_post = _mock_httpx_client(200)
        changes = [{"library_release_id": 1, "on_streaming": True}]

        with patcher:
            result = await _send_streaming_webhook(
                "https://example.com/webhook", "secret-key", changes
            )

        assert result["url"] == "https://example.com/webhook"
        assert result["status"] == "sent"
        assert result["changes_count"] == 1

    @pytest.mark.asyncio
    async def test_failure(self):
        """Connection error returns failed status with error message."""
        from routers.admin import _send_streaming_webhook

        patcher, _ = _mock_httpx_client(side_effect=httpx.ConnectError("refused"))
        changes = [{"library_release_id": 1, "on_streaming": True}]

        with patcher:
            result = await _send_streaming_webhook(
                "https://example.com/webhook", "secret-key", changes
            )

        assert result["url"] == "https://example.com/webhook"
        assert result["status"] == "failed"
        assert "error" in result

    @pytest.mark.asyncio
    async def test_includes_bearer_token(self):
        """POST includes Authorization: Bearer header when notify_key is set."""
        from routers.admin import _send_streaming_webhook

        patcher, mock_post = _mock_httpx_client(200)
        changes = [{"library_release_id": 1, "on_streaming": True}]

        with patcher:
            await _send_streaming_webhook("https://example.com/webhook", "my-token", changes)

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert headers["Authorization"] == "Bearer my-token"

    @pytest.mark.asyncio
    async def test_no_auth_when_no_key(self):
        """POST omits Authorization header when notify_key is None."""
        from routers.admin import _send_streaming_webhook

        patcher, mock_post = _mock_httpx_client(200)
        changes = [{"library_release_id": 1, "on_streaming": True}]

        with patcher:
            await _send_streaming_webhook("https://example.com/webhook", None, changes)

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_http_error_status(self):
        """Non-2xx response returns failed status."""
        from routers.admin import _send_streaming_webhook

        patcher, _ = _mock_httpx_client(500)
        changes = [{"library_release_id": 1, "on_streaming": True}]

        with patcher:
            result = await _send_streaming_webhook("https://example.com/webhook", "key", changes)

        assert result["status"] == "failed"


class TestSendStreamingWebhooks:
    @pytest.mark.asyncio
    async def test_multiple_urls(self):
        """Sends to all comma-separated URLs and returns results for each."""
        from routers.admin import _send_streaming_webhooks

        patcher, mock_post = _mock_httpx_client(200)
        changes = [{"library_release_id": 1, "on_streaming": True}]
        urls = "https://a.example.com/webhook,https://b.example.com/webhook"

        with patcher:
            results = await _send_streaming_webhooks(urls, "key", changes)

        assert len(results) == 2
        assert all(r["status"] == "sent" for r in results)
        returned_urls = {r["url"] for r in results}
        assert "https://a.example.com/webhook" in returned_urls
        assert "https://b.example.com/webhook" in returned_urls

    @pytest.mark.asyncio
    async def test_partial_failure(self):
        """One URL fails, the other succeeds -- both are reported."""
        from routers.admin import _send_streaming_webhooks

        changes = [{"library_release_id": 1, "on_streaming": True}]
        urls = "https://good.example.com/wh,https://bad.example.com/wh"

        async def mock_send(url, key, ch):
            if "bad" in url:
                return {"url": url, "status": "failed", "error": "refused"}
            return {"url": url, "status": "sent", "changes_count": len(ch)}

        with patch("routers.admin._send_streaming_webhook", side_effect=mock_send):
            results = await _send_streaming_webhooks(urls, "key", changes)

        assert len(results) == 2
        statuses = {r["url"]: r["status"] for r in results}
        assert statuses["https://good.example.com/wh"] == "sent"
        assert statuses["https://bad.example.com/wh"] == "failed"


class TestUploadWebhookIntegration:
    """Tests for the webhook behavior wired into upload_library_db."""

    async def _upload(self, app, settings, db_file):
        """Helper: upload db_file via the admin endpoint."""
        mock_db = AsyncMock()
        mock_db.is_available = AsyncMock(return_value=True)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with open(db_file, "rb") as f:
                    return await client.post(
                        "/admin/upload-library-db",
                        headers={"Authorization": "Bearer test-secret-token"},
                        files={"file": ("library.db", f, "application/octet-stream")},
                    )

    @pytest.mark.asyncio
    async def test_sends_streaming_diff(self, tmp_path, webhook_settings):
        """Upload with streaming changes triggers background webhook with correct diff."""
        from main import app

        # Old DB at the configured path with streaming_ids [1, 2, 3]
        _make_valid_sqlite_db(webhook_settings.library_db_path, streaming_ids=[1, 2, 3])

        # New DB to upload with streaming_ids [2, 3, 4]
        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file, streaming_ids=[2, 3, 4])

        patcher, mock_post = _mock_httpx_client(200)
        with patcher:
            resp = await self._upload(app, webhook_settings, upload_file)
            await asyncio.sleep(0)  # let background task run

        assert resp.status_code == 200
        body = resp.json()
        assert body["webhook"]["status"] == "pending"
        assert body["webhook"]["changes_count"] == 2

        # Verify the POST payload sent in background
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        changes = payload["changes"]
        assert {"library_release_id": 4, "on_streaming": True} in changes
        assert {"library_release_id": 1, "on_streaming": False} in changes
        assert len(changes) == 2

    @pytest.mark.asyncio
    async def test_no_webhook_when_no_changes(self, tmp_path, webhook_settings):
        """Identical streaming_links means no webhook is sent."""
        from main import app

        _make_valid_sqlite_db(webhook_settings.library_db_path, streaming_ids=[1, 2])

        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file, streaming_ids=[1, 2])

        patcher, mock_post = _mock_httpx_client(200)
        with patcher:
            resp = await self._upload(app, webhook_settings, upload_file)

        assert resp.status_code == 200
        assert "webhook" not in resp.json()
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_webhook_when_not_configured(self, tmp_path, admin_settings):
        """Without streaming_webhook_urls, no webhook is attempted."""
        from main import app

        _make_valid_sqlite_db(admin_settings.library_db_path, streaming_ids=[1])

        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file, streaming_ids=[1, 2])

        patcher, mock_post = _mock_httpx_client(200)
        with patcher:
            resp = await self._upload(app, admin_settings, upload_file)

        assert resp.status_code == 200
        assert "webhook" not in resp.json()
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_webhook_failure_returns_200(self, tmp_path, webhook_settings):
        """Webhook failure does not fail the upload (fires in background)."""
        from main import app

        _make_valid_sqlite_db(webhook_settings.library_db_path, streaming_ids=[1])

        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file, streaming_ids=[1, 2])

        patcher, _ = _mock_httpx_client(side_effect=httpx.ConnectError("refused"))
        with patcher:
            resp = await self._upload(app, webhook_settings, upload_file)
            await asyncio.sleep(0)  # let background task run (and fail)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["webhook"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_first_ever_upload(self, tmp_path, webhook_settings):
        """No old DB file: all new streaming IDs are additions."""
        from main import app

        # Don't create old DB -- first-ever upload
        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file, streaming_ids=[5])

        patcher, mock_post = _mock_httpx_client(200)
        with patcher:
            resp = await self._upload(app, webhook_settings, upload_file)
            await asyncio.sleep(0)

        assert resp.status_code == 200
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        assert payload["changes"] == [
            {"library_release_id": 5, "on_streaming": True},
        ]

    @pytest.mark.asyncio
    async def test_old_db_without_streaming_table(self, tmp_path, webhook_settings):
        """Old DB lacks streaming_links: all new IDs are additions."""
        from main import app

        _make_valid_sqlite_db(webhook_settings.library_db_path)  # no streaming_ids

        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file, streaming_ids=[10, 20])

        patcher, mock_post = _mock_httpx_client(200)
        with patcher:
            resp = await self._upload(app, webhook_settings, upload_file)
            await asyncio.sleep(0)

        assert resp.status_code == 200
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        changes = payload["changes"]
        assert {"library_release_id": 10, "on_streaming": True} in changes
        assert {"library_release_id": 20, "on_streaming": True} in changes

    @pytest.mark.asyncio
    async def test_new_db_without_streaming_table(self, tmp_path, webhook_settings):
        """New DB lacks streaming_links: all old IDs become removals."""
        from main import app

        _make_valid_sqlite_db(webhook_settings.library_db_path, streaming_ids=[1, 2])

        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file)  # no streaming_ids

        patcher, mock_post = _mock_httpx_client(200)
        with patcher:
            resp = await self._upload(app, webhook_settings, upload_file)
            await asyncio.sleep(0)

        assert resp.status_code == 200
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1]["json"]
        changes = payload["changes"]
        assert {"library_release_id": 1, "on_streaming": False} in changes
        assert {"library_release_id": 2, "on_streaming": False} in changes

    @pytest.mark.asyncio
    async def test_sends_to_multiple_webhook_urls(self, tmp_path, multi_webhook_settings):
        """Multiple webhook URLs each receive the POST."""
        from main import app

        _make_valid_sqlite_db(multi_webhook_settings.library_db_path, streaming_ids=[1])

        upload_file = tmp_path / "upload.db"
        _make_valid_sqlite_db(upload_file, streaming_ids=[1, 2])

        patcher, mock_post = _mock_httpx_client(200)
        with patcher:
            resp = await self._upload(app, multi_webhook_settings, upload_file)
            await asyncio.sleep(0)

        assert resp.status_code == 200
        body = resp.json()
        assert body["webhook"]["status"] == "pending"
        assert body["webhook"]["changes_count"] == 1
