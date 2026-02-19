"""Unit tests for routers/admin.py -- library.db upload endpoint."""

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from tests.unit.conftest import override_deps


def _make_valid_sqlite_db(path) -> int:
    """Create a minimal valid SQLite library database at ``path``. Returns row count."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE library ("
        "id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
        "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
        "genre TEXT, format TEXT)"
    )
    conn.execute(
        "INSERT INTO library (id, title, artist, call_letters) VALUES (1, 'OK Computer', 'Radiohead', 'R')"
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
