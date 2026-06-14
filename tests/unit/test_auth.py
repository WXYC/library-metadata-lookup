"""Unit tests for core/auth.py — bearer token enforcement on tubafrenzy/Backend-Service routes."""

from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from starlette.requests import Request

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


def _request(
    *,
    method: str = "POST",
    path: str = "/api/v1/lookup",
    client: tuple[str, int] | None = ("10.0.0.7", 51234),
    headers: dict[str, str] | None = None,
) -> Request:
    """Build a minimal Starlette ``Request`` for direct dep tests.

    The dep only reads ``request.client``, ``request.headers``, ``request.url.path``,
    and ``request.method``, so we construct a scope with just those fields populated.
    """
    encoded_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in (headers or {}).items()
    ]
    scope: dict = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": encoded_headers,
        "client": client,
        "server": ("testserver", 80),
    }
    return Request(scope)


class TestRequireLmlKey:
    """Direct unit tests for the FastAPI dep."""

    @pytest.mark.asyncio
    async def test_flag_off_no_header_passes(self):
        from core.auth import require_lml_key

        await require_lml_key(
            request=_request(),
            settings=_settings(require_auth=False),
            authorization=None,
        )

    @pytest.mark.asyncio
    async def test_flag_off_with_garbage_header_passes(self):
        # When auth is disabled, the dep does not even inspect the header.
        from core.auth import require_lml_key

        await require_lml_key(
            request=_request(),
            settings=_settings(require_auth=False),
            authorization="not even bearer-shaped",
        )

    @pytest.mark.asyncio
    async def test_flag_on_no_header_returns_401(self):
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key="secret"),
                authorization=None,
            )
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_flag_on_wrong_token_returns_403(self):
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
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
                request=_request(),
                settings=_settings(require_auth=True, key="secret"),
                authorization="secret",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_flag_on_wrong_scheme_returns_403(self):
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key="secret"),
                authorization="Basic c2VjcmV0",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_flag_on_correct_bearer_passes(self):
        from core.auth import require_lml_key

        await require_lml_key(
            request=_request(),
            settings=_settings(require_auth=True, key="secret"),
            authorization="Bearer secret",
        )

    @pytest.mark.asyncio
    async def test_flag_on_lowercase_scheme_passes(self):
        # RFC 7235 says the scheme is case-insensitive.
        from core.auth import require_lml_key

        await require_lml_key(
            request=_request(),
            settings=_settings(require_auth=True, key="secret"),
            authorization="bearer secret",
        )

    @pytest.mark.asyncio
    async def test_flag_on_key_unset_returns_500(self):
        # Misconfiguration: enforcement on but no key configured. Fail loudly.
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer anything",
            )
        assert exc.value.status_code == 500


class TestRequireLmlKeyLogging:
    """Pin the structured-logging surface added for WXYC/library-metadata-lookup#360.

    Each 401/403 branch must emit a WARNING with ``client_ip``, ``user_agent``, ``path``,
    ``method``, and ``reason`` so we can identify the dominant caller of
    ``/api/v1/lookup``'s 946 daily 401s without re-pulling Sentry traces. The token
    itself is never logged — neither raw nor hashed.
    """

    @pytest.mark.asyncio
    async def test_missing_header_logs_warning_with_request_context(self, caplog):
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            with pytest.raises(HTTPException):
                await require_lml_key(
                    request=_request(
                        method="POST",
                        path="/api/v1/lookup",
                        client=("10.0.0.7", 51234),
                        headers={"user-agent": "tubafrenzy/1.0"},
                    ),
                    settings=_settings(require_auth=True, key="secret"),
                    authorization=None,
                )

        records = [r for r in caplog.records if r.name == "core.auth"]
        assert len(records) == 1, f"expected 1 auth log, got {len(records)}: {records}"
        rec = records[0]
        assert rec.levelno == logging.WARNING
        assert rec.client_ip == "10.0.0.7"
        assert rec.user_agent == "tubafrenzy/1.0"
        assert rec.path == "/api/v1/lookup"
        assert rec.method == "POST"
        assert rec.reason == "missing_authorization"

    @pytest.mark.asyncio
    async def test_missing_header_uses_x_forwarded_for_first_hop(self, caplog):
        # Railway puts a proxy in front of the service; the true client IP comes via
        # X-Forwarded-For. The first hop is the original client.
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            with pytest.raises(HTTPException):
                await require_lml_key(
                    request=_request(
                        client=("172.16.0.1", 1234),  # proxy
                        headers={
                            "user-agent": "curl/8.0",
                            "x-forwarded-for": "198.51.100.42, 172.16.0.1",
                        },
                    ),
                    settings=_settings(require_auth=True, key="secret"),
                    authorization=None,
                )

        rec = next(r for r in caplog.records if r.name == "core.auth")
        assert rec.client_ip == "198.51.100.42"

    @pytest.mark.asyncio
    async def test_wrong_token_logs_warning_with_invalid_token_value_reason(self, caplog):
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            with pytest.raises(HTTPException):
                await require_lml_key(
                    request=_request(
                        client=("10.0.0.8", 9000),
                        headers={"user-agent": "BackendService/1.2"},
                    ),
                    settings=_settings(require_auth=True, key="secret"),
                    authorization="Bearer wrong",
                )

        rec = next(r for r in caplog.records if r.name == "core.auth")
        assert rec.levelno == logging.WARNING
        assert rec.client_ip == "10.0.0.8"
        assert rec.user_agent == "BackendService/1.2"
        assert rec.path == "/api/v1/lookup"
        assert rec.method == "POST"
        assert rec.reason == "invalid_token_value"

    @pytest.mark.asyncio
    async def test_wrong_scheme_logs_invalid_token_scheme_reason(self, caplog):
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            with pytest.raises(HTTPException):
                await require_lml_key(
                    request=_request(headers={"user-agent": "probe/1.0"}),
                    settings=_settings(require_auth=True, key="secret"),
                    authorization="Basic c2VjcmV0",
                )

        rec = next(r for r in caplog.records if r.name == "core.auth")
        assert rec.reason == "invalid_token_scheme"

    @pytest.mark.asyncio
    async def test_malformed_header_logs_invalid_token_scheme_reason(self, caplog):
        # No space between scheme and token — splits to one part, treated as bad scheme.
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            with pytest.raises(HTTPException):
                await require_lml_key(
                    request=_request(),
                    settings=_settings(require_auth=True, key="secret"),
                    authorization="secret",
                )

        rec = next(r for r in caplog.records if r.name == "core.auth")
        assert rec.reason == "invalid_token_scheme"

    @pytest.mark.asyncio
    async def test_success_emits_no_warning(self, caplog):
        # Healthy requests must not pollute the log stream.
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key="secret"),
                authorization="Bearer secret",
            )

        assert [r for r in caplog.records if r.name == "core.auth"] == []

    @pytest.mark.asyncio
    async def test_log_does_not_contain_token_value(self, caplog):
        # Defensive: even though we log a "reason" rather than the token, make sure
        # nothing in the record (message or any attribute) carries the raw token.
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            with pytest.raises(HTTPException):
                await require_lml_key(
                    request=_request(),
                    settings=_settings(require_auth=True, key="secret"),
                    authorization="Bearer hunter2-secret-token",
                )

        rec = next(r for r in caplog.records if r.name == "core.auth")
        # Check both the formatted message and every attribute on the record.
        formatted = rec.getMessage()
        assert "hunter2-secret-token" not in formatted
        for value in vars(rec).values():
            assert "hunter2-secret-token" not in str(value)

    @pytest.mark.asyncio
    async def test_missing_client_does_not_crash(self, caplog):
        # request.client can be None when the ASGI scope omits it (test harness, etc.).
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            with pytest.raises(HTTPException):
                await require_lml_key(
                    request=_request(client=None, headers={"user-agent": "x"}),
                    settings=_settings(require_auth=True, key="secret"),
                    authorization=None,
                )

        rec = next(r for r in caplog.records if r.name == "core.auth")
        assert rec.client_ip is None
        assert rec.user_agent == "x"


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


def _iter_effective_routes(app):
    """Yield route-like objects for every concrete endpoint reachable from ``app.routes``.

    FastAPI >= 0.137 wraps each ``include_router(...)`` call in an ``_IncludedRouter``
    so the underlying ``APIRoute`` objects no longer live directly in ``app.routes``;
    they are exposed via ``effective_route_contexts()`` with prefix + include-dependencies
    merged in. Older FastAPI flattens them and is handled by the else branch.
    """
    for r in app.routes:
        if hasattr(r, "effective_route_contexts"):
            yield from r.effective_route_contexts()
        else:
            yield r


def _find_route(app, method, path):
    for r in _iter_effective_routes(app):
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
