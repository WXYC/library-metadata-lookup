"""E2E fixtures for tests that hit the real Discogs API.

Provides an httpx AsyncClient wired to the FastAPI app with a real
DiscogsService backed by actual credentials. Supports two auth methods:

  1. ``DISCOGS_TOKEN`` -- personal access token (preferred when both are set)
  2. ``DISCOGS_API_KEY`` + ``DISCOGS_API_SECRET`` -- OAuth consumer pair

Tests in this package are skipped at collection time when no credentials
are available, so CI without secrets won't fail spuriously.
"""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from discogs.service import DiscogsService
from main import app

_token = os.environ.get("DISCOGS_TOKEN")
_key = os.environ.get("DISCOGS_API_KEY")
_secret = os.environ.get("DISCOGS_API_SECRET")

if not _token and not (_key and _secret):
    pytest.skip(
        "Set DISCOGS_TOKEN or DISCOGS_API_KEY + DISCOGS_API_SECRET -- skipping",
        allow_module_level=True,
    )


@pytest.fixture(scope="module")
def e2e_settings() -> Settings:
    """Settings with real Discogs credentials, telemetry/cache disabled."""
    return Settings(
        discogs_token=_token,
        discogs_api_key=_key,
        discogs_api_secret=_secret,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest_asyncio.fixture
async def real_discogs_client(e2e_settings: Settings):
    """httpx AsyncClient with a real DiscogsService hitting the Discogs API."""
    if _token:
        service = DiscogsService(token=_token, cache_service=None)
    else:
        service = DiscogsService(api_key=_key, api_secret=_secret, cache_service=None)

    app.dependency_overrides[get_discogs_service] = lambda: service
    app.dependency_overrides[get_library_db] = lambda: None
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: e2e_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    await service.close()
    app.dependency_overrides.clear()
