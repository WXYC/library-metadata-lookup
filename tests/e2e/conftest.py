"""E2E test fixtures.

Provides an httpx AsyncClient wired to the real FastAPI app with a real
DiscogsService backed by actual Discogs API credentials. Supports two
auth methods:

  1. DISCOGS_TOKEN -- personal access token (preferred)
  2. DISCOGS_API_KEY + DISCOGS_API_SECRET -- OAuth consumer key/secret pair

All tests in this package are skipped when neither auth method is available.
"""

import os

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
from discogs.service import DiscogsService
from main import app

# Resolve credentials: prefer personal token, fall back to key/secret.
_discogs_token = os.environ.get("DISCOGS_TOKEN")
_discogs_key = os.environ.get("DISCOGS_API_KEY")
_discogs_secret = os.environ.get("DISCOGS_API_SECRET")

if not _discogs_token and not (_discogs_key and _discogs_secret):
    pytest.skip(
        "Set DISCOGS_TOKEN or DISCOGS_API_KEY + DISCOGS_API_SECRET -- skipping e2e tests",
        allow_module_level=True,
    )


class KeySecretDiscogsService(DiscogsService):
    """DiscogsService variant that authenticates with key/secret instead of a personal token."""

    def __init__(self, key: str, secret: str, cache_service=None):
        super().__init__(token="unused", cache_service=cache_service)
        self._key = key
        self._secret = secret

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url="https://api.discogs.com",
                headers={
                    "Authorization": f"Discogs key={self._key}, secret={self._secret}",
                    "User-Agent": "LibraryMetadataLookupService/1.0",
                },
                timeout=10.0,
            )
        return self._client


def _create_discogs_service() -> DiscogsService:
    if _discogs_token:
        return DiscogsService(token=_discogs_token, cache_service=None)
    return KeySecretDiscogsService(key=_discogs_key, secret=_discogs_secret, cache_service=None)


# Use the token value for Settings (it expects a string; the key works here too).
_settings_token = _discogs_token or _discogs_key


@pytest.fixture(scope="module")
def e2e_settings() -> Settings:
    """Settings with real Discogs credentials, telemetry disabled."""
    return Settings(
        discogs_token=_settings_token,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test_library.db",
    )


@pytest_asyncio.fixture
async def real_discogs_client(e2e_settings: Settings):
    """httpx AsyncClient with a real DiscogsService hitting the Discogs API.

    The library DB dependency is stubbed out (not needed for Discogs-only
    endpoints), and telemetry is disabled.
    """
    service = _create_discogs_service()

    # Wire the real service into FastAPI's dependency overrides.
    app.dependency_overrides[get_discogs_service] = lambda: service
    app.dependency_overrides[get_library_db] = lambda: None
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_settings] = lambda: e2e_settings

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    await service.close()
    app.dependency_overrides.clear()
