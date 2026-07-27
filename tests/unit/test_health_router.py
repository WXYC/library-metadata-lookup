"""Unit tests for routers/health.py.

The health route is a thin local handler that uses ``wxyc_fastapi.healthcheck``
primitives (``Check`` dataclass + ``DEFAULT_TIMEOUT_SECONDS``) but preserves
LML's contracted response shape:

* URL stays ``GET /health`` (Railway healthcheck depends on it).
* Response body carries ``version`` (operator-facing).
* ``services.discogs_api`` may carry granular values like ``"auth-error"``,
  ``"rate-limited"``, ``"upstream-error"`` — see ``DiscogsApiCheckResult``.
  The shared ``readiness_router`` flattens those to ``"unavailable"``, which is
  why we don't mount it directly.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from discogs.breaker import BreakerState, DiscogsCircuitBreaker
from discogs.service import DiscogsApiCheckResult, DiscogsService
from library.db import LibraryDB
from routers.health import (
    _check_database,
    _check_discogs_api,
    _check_discogs_cache,
)
from tests.unit.conftest import override_deps

# ---------------------------------------------------------------------------
# Probe helpers (still local — they encode LML-specific contract values)
# ---------------------------------------------------------------------------


class TestCheckDatabase:
    @pytest.mark.asyncio
    async def test_ok(self):
        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=True)
        assert await _check_database(db) == "ok"

    @pytest.mark.asyncio
    async def test_error(self):
        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=False)
        assert await _check_database(db) == "error"


class TestCheckDiscogsApi:
    """The probe surfaces the enum's string value verbatim into /health."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result,expected",
        [
            (DiscogsApiCheckResult.OK, "ok"),
            (DiscogsApiCheckResult.AUTH_ERROR, "auth-error"),
            (DiscogsApiCheckResult.RATE_LIMITED, "rate-limited"),
            (DiscogsApiCheckResult.UPSTREAM_ERROR, "upstream-error"),
            (DiscogsApiCheckResult.NETWORK_ERROR, "network-error"),
            (DiscogsApiCheckResult.ERROR, "error"),
        ],
    )
    async def test_renders_each_enum_value(self, result, expected):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=result)
        assert await _check_discogs_api(svc) == expected

    @pytest.mark.asyncio
    async def test_none_service(self):
        assert await _check_discogs_api(None) == "unavailable"

    # -----------------------------------------------------------------
    # LML#757: the LML#755 saturation breaker's state is authoritative over
    # the independent live probe. An OPEN or HALF_OPEN breaker is already
    # shedding live Discogs traffic, so the probe must not report "ok" just
    # because its own live call happened to land in a momentary token-bucket
    # refill -- that disagreement is exactly what let the 2026-07-13/14
    # incident shed 100% of live Discogs calls for ~8h while /health stayed
    # green. The probe is skipped entirely (not merely overridden) when the
    # breaker is shedding, so /health adds no extra live Discogs load during
    # an active shed.
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_breaker_open_overrides_live_probe_to_rate_limited(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)

        result = await _check_discogs_api(svc, BreakerState.OPEN)

        assert result == "rate-limited"
        svc.check_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_breaker_half_open_overrides_live_probe_to_rate_limited(self):
        """HALF_OPEN still has only a single trial slot admitted -- every
        other caller is shed, so it must be reported as degraded, not ok."""
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)

        result = await _check_discogs_api(svc, BreakerState.HALF_OPEN)

        assert result == "rate-limited"
        svc.check_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_breaker_closed_defers_to_live_probe(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)

        result = await _check_discogs_api(svc, BreakerState.CLOSED)

        assert result == "ok"
        svc.check_api.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_breaker_state_defers_to_live_probe(self):
        """Omitting ``breaker_state`` preserves the pre-LML#757 behavior of
        trusting the live probe alone -- keeps direct callers backward
        compatible."""
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)

        result = await _check_discogs_api(svc)

        assert result == "ok"


class TestCheckDiscogsCache:
    @pytest.mark.asyncio
    async def test_ok(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=True)
        assert await _check_discogs_cache(svc) == "ok"

    @pytest.mark.asyncio
    async def test_error(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=False)
        assert await _check_discogs_cache(svc) == "error"

    @pytest.mark.asyncio
    async def test_none_service(self):
        assert await _check_discogs_cache(None) == "unavailable"

    @pytest.mark.asyncio
    async def test_no_cache_service(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.cache_service = None
        assert await _check_discogs_cache(svc) == "unavailable"


# ---------------------------------------------------------------------------
# commit_sha resolution (file → env → None)
# ---------------------------------------------------------------------------


class TestResolveCommitSha:
    """``_resolve_commit_sha`` resolves the deployed commit SHA for /health.

    The winning prod/staging deploy is a ``railway up`` CLI source-deploy that
    carries no git metadata, so ``RAILWAY_GIT_COMMIT_SHA`` is absent there and
    the SHA must come from a ``COMMIT_SHA`` file CI bakes into the image before
    ``railway up`` — see WXYC/library-metadata-lookup#509.
    """

    def test_reads_sha_from_commit_sha_file(self, tmp_path):
        """The CI-baked ``COMMIT_SHA`` file is the authoritative source.

        ``echo "$SHA" > COMMIT_SHA`` leaves a trailing newline, so the value
        must be stripped.
        """
        from routers.health import _resolve_commit_sha

        sha = "abc123def456abc123def456abc123def456abcd"
        commit_file = tmp_path / "COMMIT_SHA"
        commit_file.write_text(sha + "\n")

        assert _resolve_commit_sha(commit_file) == sha

    def test_falls_back_to_env_when_file_absent(self, tmp_path, monkeypatch):
        """No baked file (Railway git-native deploy, local dev) -> read the env var."""
        from routers.health import _resolve_commit_sha

        sha = "0123456789abcdef0123456789abcdef01234567"
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", sha)

        assert _resolve_commit_sha(tmp_path / "COMMIT_SHA") == sha

    def test_empty_env_and_no_file_returns_none(self, tmp_path, monkeypatch):
        """A set-but-empty env var with no file must coerce to ``None``.

        The documented contract is "null when unset"; downstream deploy-gate
        equality checks (WXYC/Backend-Service#1361) must not be fooled by ``""``.
        """
        from routers.health import _resolve_commit_sha

        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "")

        assert _resolve_commit_sha(tmp_path / "COMMIT_SHA") is None

    def test_file_takes_priority_over_env(self, tmp_path, monkeypatch):
        """When the CLI-deploy file and a git-native env var disagree, the baked
        file wins — it identifies the deploy actually serving traffic (#509)."""
        from routers.health import _resolve_commit_sha

        file_sha = "1111111111111111111111111111111111111111"
        env_sha = "2222222222222222222222222222222222222222"
        commit_file = tmp_path / "COMMIT_SHA"
        commit_file.write_text(file_sha + "\n")
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", env_sha)

        assert _resolve_commit_sha(commit_file) == file_sha

    def test_blank_file_falls_through_to_env(self, tmp_path, monkeypatch):
        """A whitespace-only file (e.g. ``echo "" > COMMIT_SHA``) is treated as
        absent and yields to the env var rather than masking it with ``""``."""
        from routers.health import _resolve_commit_sha

        env_sha = "3333333333333333333333333333333333333333"
        commit_file = tmp_path / "COMMIT_SHA"
        commit_file.write_text("\n")
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", env_sha)

        assert _resolve_commit_sha(commit_file) == env_sha

    def test_returns_none_when_neither_set(self, tmp_path, monkeypatch):
        """Local dev / CI / tests: no file, no env -> ``None`` (not an error)."""
        from routers.health import _resolve_commit_sha

        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)

        assert _resolve_commit_sha(tmp_path / "COMMIT_SHA") is None

    def test_non_utf8_file_degrades_instead_of_raising(self, tmp_path, monkeypatch):
        """A corrupt / non-UTF-8 ``COMMIT_SHA`` must not crash ``_resolve_commit_sha``.

        ``/health`` is the Railway healthcheck path; a 500 there fails the deploy.
        A malformed identity file must degrade to the env / ``None`` tiers, never
        raise. (Unreachable via the ``echo``-written ASCII SHA, but the health
        path must be total.) #509 review.
        """
        from routers.health import _resolve_commit_sha

        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        commit_file = tmp_path / "COMMIT_SHA"
        commit_file.write_bytes(b"\xff\xfe not valid utf-8 \x80")

        assert _resolve_commit_sha(commit_file) is None

    def test_utf8_bom_file_is_stripped(self, tmp_path, monkeypatch):
        """A ``COMMIT_SHA`` written with a UTF-8 BOM yields a clean SHA.

        ``str.strip()`` does not remove ``\\ufeff``; a leading BOM would otherwise
        break downstream exact-equality deploy gates (``commit_sha == sha``). #509
        review.
        """
        from routers.health import _resolve_commit_sha

        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        sha = "abcabcabcabcabcabcabcabcabcabcabcabcabca"
        commit_file = tmp_path / "COMMIT_SHA"
        commit_file.write_bytes(b"\xef\xbb\xbf" + (sha + "\n").encode("utf-8"))

        assert _resolve_commit_sha(commit_file) == sha

    def test_committed_placeholder_is_tracked_and_blank(self):
        """The ``COMMIT_SHA`` committed to git must be tracked and blank.

        CI overwrites it with the real SHA at deploy time; a real SHA committed
        into it would make local/CI ``/health`` advertise a deploy that isn't
        running, and a *deleted* placeholder would leave ``railway up`` nothing
        to upload (re-null-ing prod). It must stay tracked — not ``.gitignore``d —
        because ``railway up`` honors ``.gitignore``. See #509.

        Reads the committed blob (``git show HEAD:COMMIT_SHA``), not the working
        tree, so a locally-dirty ``COMMIT_SHA`` (e.g. after running CI's bake
        step by hand) never spuriously fails this.
        """
        import subprocess

        import routers.health as health_module

        repo_root = health_module.COMMIT_SHA_PATH.parent
        try:
            result = subprocess.run(
                ["git", "show", "HEAD:COMMIT_SHA"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            pytest.skip("git not available")

        assert result.returncode == 0, (
            "COMMIT_SHA must be tracked at HEAD so `railway up` uploads it (LML#509)"
        )
        assert result.stdout.strip() == "", (
            "committed COMMIT_SHA must be blank — CI overwrites it at deploy time"
        )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    @pytest.fixture
    def mock_db(self):
        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=True)
        return db

    @pytest.fixture
    def mock_discogs(self):
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=True)
        return svc

    @pytest.fixture(autouse=True)
    def _isolate_commit_sha(self, monkeypatch, tmp_path):
        """Resolve ``commit_sha`` against controlled state in every endpoint test.

        Without this, tests read the real on-disk ``COMMIT_SHA`` placeholder and
        an ambient ``RAILWAY_GIT_COMMIT_SHA`` (e.g. when the suite runs on a
        Railway shell), which would make ``commit_sha`` assertions — and even the
        non-asserting tests — depend on uncontrolled state. Default is "no file,
        no env" -> ``commit_sha`` is ``null``; individual tests opt into a baked
        file or an env value.
        """
        import routers.health as health_module

        monkeypatch.setattr(health_module, "COMMIT_SHA_PATH", tmp_path / "absent-COMMIT_SHA")
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)

    @pytest.mark.asyncio
    async def test_healthy(self, mock_db, mock_discogs, mock_settings):
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert "version" in body
        assert body["services"]["database"] == "ok"
        assert body["services"]["discogs_api"] == "ok"
        assert body["services"]["discogs_cache"] == "ok"

    @pytest.mark.asyncio
    async def test_commit_sha_surfaced_from_baked_file(
        self, mock_db, mock_discogs, mock_settings, monkeypatch, tmp_path
    ):
        """``GET /health`` surfaces the CI-baked ``COMMIT_SHA`` file end-to-end.

        This is the deploy path that actually serves prod/staging traffic — a
        ``railway up`` CLI source-deploy with no ``RAILWAY_GIT_COMMIT_SHA``
        (WXYC/library-metadata-lookup#509).
        """
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        sha = "feedfacefeedfacefeedfacefeedfacefeedface"
        commit_file = tmp_path / "COMMIT_SHA"
        commit_file.write_text(sha + "\n")
        monkeypatch.setattr(health_module, "COMMIT_SHA_PATH", commit_file)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["commit_sha"] == sha

    @pytest.mark.asyncio
    async def test_commit_sha_file_wins_over_env(
        self, mock_db, mock_discogs, mock_settings, monkeypatch, tmp_path
    ):
        """End-to-end, the baked file beats a present ``RAILWAY_GIT_COMMIT_SHA``.

        Locks the endpoint→helper wiring at the integration tier: a regression
        that resolved env before file (both present) would pass the unit test
        but is caught here. Distinct sentinels make the winning source explicit.
        """
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        file_sha = "f11ef11ef11ef11ef11ef11ef11ef11ef11ef11e"
        env_sha = "e0ee0ee0ee0ee0ee0ee0ee0ee0ee0ee0ee0ee0ee"
        commit_file = tmp_path / "COMMIT_SHA"
        commit_file.write_text(file_sha + "\n")
        monkeypatch.setattr(health_module, "COMMIT_SHA_PATH", commit_file)
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", env_sha)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["commit_sha"] == file_sha

    @pytest.mark.asyncio
    async def test_commit_sha_from_railway_env(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ):
        """Fallback tier: with no baked file, ``commit_sha`` mirrors Railway's
        auto-injected ``RAILWAY_GIT_COMMIT_SHA`` (git-native deploys).

        Cross-repo deploy gates (e.g. WXYC/Backend-Service#1361) need a programmatic
        way to ask "is commit X live?". ``settings.app_version`` is hardcoded and
        unbumped, so it cannot serve that role. (The ``_isolate_commit_sha``
        autouse fixture already points ``COMMIT_SHA_PATH`` at an absent file.)
        """
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        sha = "abc123def456abc123def456abc123def456abcd"
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", sha)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["commit_sha"] == sha

    @pytest.mark.asyncio
    async def test_commit_sha_null_when_unset(self, mock_db, mock_discogs, mock_settings):
        """Local dev / CI: no file and no env var -> ``commit_sha`` is ``null``,
        not an error. State is established by the ``_isolate_commit_sha`` fixture."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert "commit_sha" in body
        assert body["commit_sha"] is None

    @pytest.mark.asyncio
    async def test_commit_sha_null_when_env_var_is_empty_string(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ):
        """A set-but-empty ``RAILWAY_GIT_COMMIT_SHA`` (dev shell `export X=`, transient
        Railway state) with no baked file must surface as ``null`` — the documented
        contract is "null when unset", and downstream deploy-gate comparisons
        (``body['commit_sha'] == sha``) must not be fooled by an empty string that would
        equal nothing but also fail an ``is None`` check. (Absent file via the
        ``_isolate_commit_sha`` fixture.)
        """
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "")

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        assert resp.json()["commit_sha"] is None

    @pytest.mark.asyncio
    async def test_degraded_preserves_granular_discogs_value(self, mock_db, mock_settings):
        """Core (database) ok but optional service erroring -> degraded.

        The granular DiscogsApiCheckResult value (``auth-error``) must round-trip
        to the response body verbatim — operators rely on it to distinguish
        token rotation from rate limiting from upstream outages. The shared
        readiness_router would flatten this to ``unavailable``; the local
        handler must not.
        """
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.AUTH_ERROR)
        svc.cache_service = AsyncMock()
        svc.cache_service.is_available = AsyncMock(return_value=False)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: svc,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["services"]["discogs_api"] == "auth-error"
        assert body["services"]["discogs_cache"] == "error"

    # -----------------------------------------------------------------
    # LML#757: the breaker's shedding state must be surfaced on GET /health,
    # dominating the independent live probe, plus the raw state must be
    # exposed for drill-down. Reproduces the 2026-07-13/14 incident scenario
    # where the breaker was OPEN/HALF_OPEN (shedding 100% of live Discogs
    # calls) while the independent probe kept landing "ok".
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_discogs_api_degraded_when_breaker_open(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ):
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        open_breaker = MagicMock(spec=DiscogsCircuitBreaker)
        open_breaker.state = BreakerState.OPEN
        monkeypatch.setattr(health_module, "get_discogs_breaker", lambda: open_breaker)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["services"]["discogs_api"] == "rate-limited"
        assert body["discogs_breaker_state"] == "open"
        assert body["status"] == "degraded"
        mock_discogs.check_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_discogs_api_degraded_when_breaker_half_open(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ):
        """HALF_OPEN is still shedding (a single trial slot occupied) -- must
        not be reported as healthy even though it is one step from CLOSED."""
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        half_open_breaker = MagicMock(spec=DiscogsCircuitBreaker)
        half_open_breaker.state = BreakerState.HALF_OPEN
        monkeypatch.setattr(health_module, "get_discogs_breaker", lambda: half_open_breaker)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["services"]["discogs_api"] == "rate-limited"
        assert body["discogs_breaker_state"] == "half-open"
        assert body["status"] == "degraded"
        mock_discogs.check_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_discogs_api_ok_when_breaker_closed(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ):
        """CLOSED (normal operation) -- discogs_api reflects the live probe,
        and the raw breaker state rides along as "closed"."""
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        closed_breaker = MagicMock(spec=DiscogsCircuitBreaker)
        closed_breaker.state = BreakerState.CLOSED
        monkeypatch.setattr(health_module, "get_discogs_breaker", lambda: closed_breaker)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["services"]["discogs_api"] == "ok"
        assert body["discogs_breaker_state"] == "closed"

    @pytest.mark.asyncio
    async def test_breaker_state_null_when_discogs_unconfigured(
        self, mock_db, mock_settings, monkeypatch
    ):
        """LML#757: with Discogs unconfigured (``discogs_service is None``) there
        is no live probe for the breaker to dominate and no breaker worth
        materializing, so ``discogs_breaker_state`` reports ``null`` (the same
        "null when N/A" convention as ``commit_sha``) and ``get_discogs_breaker``
        is never called just to answer a health probe."""
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        breaker_probe = MagicMock()
        monkeypatch.setattr(health_module, "get_discogs_breaker", breaker_probe)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["discogs_breaker_state"] is None
        assert body["services"]["discogs_api"] == "unavailable"
        breaker_probe.assert_not_called()

    # -----------------------------------------------------------------
    # LML#940: a read-only, additive top-level field carrying the
    # process-global live-Discogs-request-attempt count -- the volume signal
    # the wxyc-canary needs to tell a real sustained breaker shed (this total
    # climbing across polls) apart from the idle-tail false positive (breaker
    # latched open/half-open but this total is flat, i.e. no live-Discogs
    # traffic has been attempted).
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_includes_discogs_live_requests_total(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ):
        """The field is read once up front (mirroring ``breaker_state`` at
        :220) and reflects the counter verbatim."""
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        monkeypatch.setattr(health_module, "get_discogs_live_requests_total", lambda: 42)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["discogs_live_requests_total"] == 42

    @pytest.mark.asyncio
    async def test_discogs_live_requests_total_kept_out_of_services(
        self, mock_db, mock_discogs, mock_settings, monkeypatch
    ):
        """Kept out of ``services`` -- that dict feeds the ``("ok",
        "unavailable")`` ``all_configured_ok`` check, and a number there would
        spuriously flip a healthy deploy to degraded (same reasoning as
        ``discogs_breaker_state``)."""
        import routers.health as health_module
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        monkeypatch.setattr(health_module, "get_discogs_live_requests_total", lambda: 7)

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: mock_discogs,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert "discogs_live_requests_total" not in body["services"]

    @pytest.mark.asyncio
    async def test_discogs_live_requests_total_present_when_discogs_unconfigured(
        self, mock_db, mock_settings
    ):
        """Unlike ``discogs_breaker_state``, no breaker needs to exist to
        answer this field -- it's a plain global-int read, so a fresh process
        with Discogs unconfigured still reports ``0`` (correct: no
        live-Discogs traffic has happened), not ``null`` or an absent key."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from discogs.live_request_counter import reset_discogs_live_requests_total
        from main import app

        reset_discogs_live_requests_total()

        with override_deps(
            app,
            {
                get_library_db: mock_db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["discogs_live_requests_total"] == 0

    @pytest.mark.asyncio
    async def test_unhealthy_returns_503(self, mock_settings):
        """Core service (database) down -> unhealthy + 503."""
        from config.settings import get_settings
        from core.dependencies import get_discogs_service, get_library_db, get_posthog_client
        from main import app

        db = AsyncMock(spec=LibraryDB)
        db.is_available = AsyncMock(return_value=False)

        with override_deps(
            app,
            {
                get_library_db: db,
                get_discogs_service: None,
                get_posthog_client: None,
                get_settings: mock_settings,
            },
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unhealthy"
        assert body["services"]["database"] == "error"

    @pytest.mark.asyncio
    async def test_uses_shared_check_dataclass(self):
        """The route declares its probes as ``wxyc_fastapi.healthcheck.Check``s.

        Locks in the shared-primitive contract: future drift back to bespoke
        local types should be flagged here.
        """
        from wxyc_fastapi.healthcheck import Check

        from routers.health import _build_checks

        db = AsyncMock(spec=LibraryDB)
        svc = AsyncMock(spec=DiscogsService)
        checks = _build_checks(db=db, discogs_service=svc)
        assert len(checks) == 3
        assert all(isinstance(c, Check) for c in checks)
        assert [c.name for c in checks] == ["database", "discogs_api", "discogs_cache"]
        assert checks[0].required is True  # database is core
        assert checks[1].required is False  # discogs_api is optional
        assert checks[2].required is False  # discogs_cache is optional

    @pytest.mark.asyncio
    async def test_build_checks_threads_breaker_state_into_discogs_api_probe(self):
        """LML#757: ``_build_checks`` must wire ``breaker_state`` into the
        ``discogs_api`` probe so an OPEN/HALF_OPEN breaker dominates the
        independent live probe end-to-end, not just when ``_check_discogs_api``
        is called directly."""
        from routers.health import _build_checks

        db = AsyncMock(spec=LibraryDB)
        svc = AsyncMock(spec=DiscogsService)
        svc.check_api = AsyncMock(return_value=DiscogsApiCheckResult.OK)

        checks = _build_checks(db=db, discogs_service=svc, breaker_state=BreakerState.OPEN)
        result = await checks[1].probe()

        assert result == "rate-limited"
        svc.check_api.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_helpers_removed(self):
        """``_run_check`` and ``CHECK_TIMEOUT`` were deleted in favour of the
        shared ``DEFAULT_TIMEOUT_SECONDS`` constant."""
        import routers.health as health_module

        assert not hasattr(health_module, "_run_check"), (
            "_run_check should have been removed; use shared primitives instead"
        )
        assert not hasattr(health_module, "CHECK_TIMEOUT"), (
            "CHECK_TIMEOUT should have been removed; "
            "use wxyc_fastapi.healthcheck.DEFAULT_TIMEOUT_SECONDS"
        )
