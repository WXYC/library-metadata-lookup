"""Unit tests for core/auth.py — bearer token enforcement on tubafrenzy/Backend-Service routes."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from config.settings import Settings


def _settings(*, require_auth: bool = False, key: str | None = None) -> Settings:
    """Settings with auth-related fields set and unrelated config quieted."""
    return Settings(
        lml_require_auth=require_auth,
        lml_api_key=key,
        discogs_token=None,
        database_url_discogs=None,
        sentry_dsn=None,
        posthog_api_key=None,
        enable_telemetry=False,
        library_db_path="test.db",
    )


class TestRequireLmlKey:
    """Direct unit tests for the FastAPI dep."""

    @pytest.mark.asyncio
    async def test_flag_off_no_header_passes(self):
        from core.auth import require_lml_key

        await require_lml_key(settings=_settings(require_auth=False), authorization=None)

    @pytest.mark.asyncio
    async def test_flag_off_with_garbage_header_passes(self):
        # When auth is disabled, the dep does not even inspect the header.
        from core.auth import require_lml_key

        await require_lml_key(
            settings=_settings(require_auth=False),
            authorization="not even bearer-shaped",
        )

    @pytest.mark.asyncio
    async def test_flag_on_no_header_returns_401(self):
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                settings=_settings(require_auth=True, key="secret"),
                authorization=None,
            )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_flag_on_wrong_token_returns_403(self):
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                settings=_settings(require_auth=True, key="secret"),
                authorization="Bearer wrong",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_flag_on_malformed_header_returns_403(self):
        # Header present but not in "<scheme> <token>" form.
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                settings=_settings(require_auth=True, key="secret"),
                authorization="secret",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_flag_on_wrong_scheme_returns_403(self):
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                settings=_settings(require_auth=True, key="secret"),
                authorization="Basic c2VjcmV0",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_flag_on_correct_bearer_passes(self):
        from core.auth import require_lml_key

        await require_lml_key(
            settings=_settings(require_auth=True, key="secret"),
            authorization="Bearer secret",
        )

    @pytest.mark.asyncio
    async def test_flag_on_lowercase_scheme_passes(self):
        # RFC 7235 says the scheme is case-insensitive.
        from core.auth import require_lml_key

        await require_lml_key(
            settings=_settings(require_auth=True, key="secret"),
            authorization="bearer secret",
        )

    @pytest.mark.asyncio
    async def test_flag_on_key_unset_returns_500(self):
        # Misconfiguration: enforcement on but no key configured. Fail loudly.
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer anything",
            )
        assert exc.value.status_code == 500


class TestProtectedRouteRegistration:
    """Smoke test: verify each route's dep tree includes (or excludes) require_lml_key.

    This guards against the easy mistake of adding a new tubafrenzy/Backend-Service-facing
    endpoint and forgetting to attach the dep, or — conversely — accidentally locking
    down /health or an /identity/* route.
    """

    EXPECTED_PROTECTED = [
        ("POST", "/api/v1/streaming-check"),
        ("POST", "/api/v1/releases/resolve"),
        ("GET", "/api/v1/library/search"),
        ("POST", "/api/v1/lookup"),
        ("GET", "/api/v1/discogs/track-releases"),
        ("GET", "/api/v1/discogs/release/{release_id}"),
        ("GET", "/api/v1/discogs/artist/{artist_id}"),
        ("GET", "/api/v1/discogs/entity/{entity_type}/{entity_id}"),
        ("GET", "/api/v1/discogs/tracks/autocomplete"),
    ]

    EXPECTED_UNPROTECTED = [
        ("GET", "/health"),
        ("GET", "/identity/resolve"),
        ("POST", "/identity/bulk"),
        # /admin/* has its own ADMIN_TOKEN validation, not require_lml_key.
        ("POST", "/admin/upload-library-db"),
    ]

    @pytest.mark.parametrize("method,path", EXPECTED_PROTECTED)
    def test_route_has_require_lml_key(self, method, path):
        from core.auth import require_lml_key
        from main import app

        route = _find_route(app, method, path)
        assert route is not None, f"Route {method} {path} not found in app.routes"
        deps = _collect_dep_callables(route)
        assert require_lml_key in deps, (
            f"Route {method} {path} is missing require_lml_key dep. "
            f"Resolved deps: {sorted(d.__name__ for d in deps)}"
        )

    @pytest.mark.parametrize("method,path", EXPECTED_UNPROTECTED)
    def test_route_does_not_have_require_lml_key(self, method, path):
        from core.auth import require_lml_key
        from main import app

        route = _find_route(app, method, path)
        assert route is not None, f"Route {method} {path} not found in app.routes"
        deps = _collect_dep_callables(route)
        assert require_lml_key not in deps, (
            f"Route {method} {path} should NOT have require_lml_key. "
            f"Resolved deps: {sorted(d.__name__ for d in deps)}"
        )


def _find_route(app, method, path):
    for r in app.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set()):
            return r
    return None


def _collect_dep_callables(route):
    """Walk the FastAPI dependant tree and return all callables reached via Depends()."""
    deps: set = set()
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return deps
    stack = [dependant]
    seen = set()
    while stack:
        d = stack.pop()
        if id(d) in seen:
            continue
        seen.add(id(d))
        if d.call is not None:
            deps.add(d.call)
        stack.extend(d.dependencies)
    return deps
