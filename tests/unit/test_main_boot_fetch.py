"""Boot-fetch of library.db from the object store (WXYC#837, volume-eviction PR 3).

In bucket mode the FastAPI lifespan fetches ``library.db`` from the store to the
local ``LIBRARY_DB_PATH`` before serving, with a small bounded retry under an
overall wall-clock deadline. The fetch is **non-fatal**: on final failure
(transient error exhausted, the object absent, or the deadline exceeded) the app
logs + Sentry-captures and boots degraded — ``/health`` then 503s via the
missing-DB path while the ``POST /admin/upload-library-db`` recovery endpoint
stays available (epic #834 decision 2). Local mode never fetches.

moto (``mock_aws``) intercepts botocore in-process, so no network or real bucket
is touched and no pytest marker is needed — the same approach the #835/#836
store tests use.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import boto3
import pytest
from moto import mock_aws

from config.settings import Settings
from routers.admin import LIBRARY_DB_FILENAME
from storage.object_store import ObjectNotFoundError, S3ObjectStore

BUCKET = "lml-library-test"
ENDPOINT = "https://s3.amazonaws.com"
PAYLOAD = b"SQLite format 3\x00-boot-fetched-library-db-bytes"


@pytest.fixture
def aws_creds(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _bucket_settings(tmp_path) -> Settings:
    return Settings(
        library_db_path=tmp_path / "library.db",
        lml_bucket_name=BUCKET,
        lml_bucket_endpoint=ENDPOINT,
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
    )


class _RaisingStore:
    """Stub store whose ``get`` always raises the given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.calls = 0

    async def get(self, key: str) -> bytes:
        self.calls += 1
        raise self._exc


class _HangingStore:
    """Stub store whose ``get`` blocks longer than the boot-fetch deadline.

    Models a partial S3 outage where the connection is accepted but the read
    hangs — the failure mode that, without a wall-clock bound, compounds with
    boto3's own ``read_timeout`` x ``max_attempts`` and can stall startup for
    minutes.
    """

    async def get(self, key: str) -> bytes:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable: the deadline must fire first")


class TestBootFetchHelper:
    @pytest.mark.asyncio
    async def test_success_writes_local_file(self, tmp_path, aws_creds):
        """A present object is fetched and written atomically to the local path."""
        from main import _boot_fetch_library_db

        settings = _bucket_settings(tmp_path)
        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
            store = S3ObjectStore(bucket=BUCKET, endpoint_url=ENDPOINT, region="us-east-1")
            await store.put(LIBRARY_DB_FILENAME, PAYLOAD)

            ok = await _boot_fetch_library_db(settings, store, base_delay=0)

        assert ok is True
        assert settings.resolved_library_db_path.read_bytes() == PAYLOAD

    @pytest.mark.asyncio
    async def test_success_overwrites_existing_local_file(self, tmp_path, aws_creds):
        """A stale local file (e.g. an old volume copy) is replaced by the bucket copy."""
        from main import _boot_fetch_library_db

        settings = _bucket_settings(tmp_path)
        # A pre-existing local library.db — the realistic transition-window state
        # where a volume still holds an older copy the boot-fetch must supersede.
        settings.resolved_library_db_path.write_bytes(b"stale-local-library-db")

        with mock_aws():
            boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
            store = S3ObjectStore(bucket=BUCKET, endpoint_url=ENDPOINT, region="us-east-1")
            await store.put(LIBRARY_DB_FILENAME, PAYLOAD)

            ok = await _boot_fetch_library_db(settings, store, base_delay=0)

        assert ok is True
        assert settings.resolved_library_db_path.read_bytes() == PAYLOAD

    @pytest.mark.asyncio
    async def test_deadline_exceeded_degrades(self, tmp_path):
        """A hung fetch is bounded by the deadline and degrades (non-fatal)."""
        from main import _boot_fetch_library_db

        settings = _bucket_settings(tmp_path)
        store = _HangingStore()

        with pytest.MonkeyPatch.context() as mp:
            captured = []
            mp.setattr("sentry_sdk.capture_message", lambda *a, **k: captured.append(a))
            ok = await asyncio.wait_for(
                _boot_fetch_library_db(settings, store, base_delay=0, deadline=0.05),
                timeout=5,  # test-level guard: the 0.05s deadline must fire well before this
            )

        assert ok is False
        assert captured, "a deadline-exceeded boot-fetch must be Sentry-captured"
        assert not settings.resolved_library_db_path.exists()

    @pytest.mark.asyncio
    async def test_transient_failure_retries_then_degrades(self, tmp_path):
        """A transient error is retried up to ``attempts`` times, then non-fatal."""
        from main import _boot_fetch_library_db

        settings = _bucket_settings(tmp_path)
        store = _RaisingStore(RuntimeError("connection reset"))

        with pytest.MonkeyPatch.context() as mp:
            captured = []
            mp.setattr("sentry_sdk.capture_exception", lambda *a, **k: captured.append(a))
            ok = await _boot_fetch_library_db(settings, store, attempts=3, base_delay=0)

        assert ok is False
        assert store.calls == 3  # retried, not one-shot
        assert captured, "final failure must be Sentry-captured"
        assert not settings.resolved_library_db_path.exists()

    @pytest.mark.asyncio
    async def test_missing_object_degrades_without_retry(self, tmp_path):
        """A 404 (ObjectNotFoundError) is non-fatal and not retried (retry can't help)."""
        from main import _boot_fetch_library_db

        settings = _bucket_settings(tmp_path)
        store = _RaisingStore(ObjectNotFoundError(LIBRARY_DB_FILENAME))

        with pytest.MonkeyPatch.context() as mp:
            captured = []
            mp.setattr("sentry_sdk.capture_message", lambda *a, **k: captured.append(a))
            ok = await _boot_fetch_library_db(settings, store, attempts=3, base_delay=0)

        assert ok is False
        assert store.calls == 1
        assert captured, "a missing object at boot must be Sentry-captured"
        assert not settings.resolved_library_db_path.exists()


class TestAtomicWriteConcurrency:
    """LML#747: N uvicorn workers boot-fetch library.db in parallel, each calling
    ``_atomic_write_bytes`` against the SAME dest. The pre-fix fixed temp name
    (``dest + ".boot.tmp"``) made every worker share one temp path, so one
    worker's ``os.replace`` consumed the temp out from under another — whose own
    ``os.replace`` then raised ``FileNotFoundError``, and because uvicorn treats a
    child that fails startup as fatal, the whole process group crash-looped
    (observed on the staging ``UVICORN_WORKERS=3`` soak). Concurrent writers to
    one dest must each use a distinct temp and all land the bytes atomically.
    """

    def test_concurrent_writers_do_not_collide(self, tmp_path):
        import threading

        from main import _atomic_write_bytes

        dest = tmp_path / "library.db"
        # Identical bytes across writers (every worker fetches the same object),
        # long enough that the write/replace window is non-trivial.
        payload = b"SQLite format 3\x00" + b"x" * (256 * 1024)
        n = 16
        barrier = threading.Barrier(n)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _writer() -> None:
            barrier.wait()  # release all writers together to maximize overlap
            try:
                _atomic_write_bytes(dest, payload)
            except BaseException as exc:  # noqa: BLE001 - collected for assertion
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=_writer) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent boot-fetch writers collided: {errors!r}"
        assert dest.read_bytes() == payload  # atomic: never torn or partial
        # No temp file leaks: dest is the only artifact in the directory.
        assert list(tmp_path.iterdir()) == [dest]


class TestLifespanWiring:
    @pytest.mark.asyncio
    async def test_bucket_mode_invokes_boot_fetch(self, tmp_path, monkeypatch):
        """In bucket mode the lifespan builds the store and calls the boot-fetch."""
        import main

        monkeypatch.setattr(main, "settings", _bucket_settings(tmp_path))
        sentinel_store = object()
        monkeypatch.setattr(main, "get_object_store", AsyncMock(return_value=sentinel_store))

        seen = {}

        async def _fake_fetch(settings, store, **kwargs):
            seen["settings"] = settings
            seen["store"] = store
            return True

        monkeypatch.setattr(main, "_boot_fetch_library_db", _fake_fetch)

        async with main.lifespan(main.app):
            pass

        assert seen.get("store") is sentinel_store

    @pytest.mark.asyncio
    async def test_local_mode_skips_boot_fetch(self, tmp_path, monkeypatch):
        """Without bucket vars the lifespan does not fetch (current behavior)."""
        import main

        local_settings = Settings(
            library_db_path=tmp_path / "library.db",
            discogs_token=None,
            database_url_discogs=None,
            sentry_dsn=None,
            posthog_api_key=None,
            enable_telemetry=False,
        )
        monkeypatch.setattr(main, "settings", local_settings)

        called = False

        async def _fake_fetch(settings, store, **kwargs):
            nonlocal called
            called = True
            return True

        monkeypatch.setattr(main, "_boot_fetch_library_db", _fake_fetch)

        async with main.lifespan(main.app):
            pass

        assert called is False
