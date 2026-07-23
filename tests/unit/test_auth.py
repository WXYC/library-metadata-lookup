"""Unit tests for core/auth.py — bearer token enforcement on tubafrenzy/Backend-Service routes."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from config.settings import Settings
from core.api_key_cache import ApiKeyCache, hash_token, reset_api_key_cache


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


def _seeded_cache(token_to_caller: dict[str, str]) -> ApiKeyCache:
    """Build an ``ApiKeyCache`` whose snapshot is seeded directly (no PG).

    Reaches into the "private" ``_hash_to_caller`` attribute the same way the
    existing ``core/event_loop_lag.py`` tests reach into ``_gauge`` — no PG
    connection or event loop is needed to exercise ``resolve()``/``is_empty``.
    """
    # pg is never used: resolve()/is_empty do no I/O.
    cache = ApiKeyCache(pg=None)  # type: ignore[arg-type]
    cache._hash_to_caller = {hash_token(t): caller for t, caller in token_to_caller.items()}
    return cache


@pytest.fixture(autouse=True)
def _isolate_api_key_cache():
    """Every test starts with an unstarted (``None``) singleton.

    Mirrors ``core/event_loop_lag.py``'s ``TestRouterStamp._isolate`` fixture.
    A test that needs a populated cache calls ``reset_api_key_cache(_seeded_cache(...))``
    itself.
    """
    reset_api_key_cache()
    yield
    reset_api_key_cache()


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
    async def test_flag_on_non_ascii_token_is_rejected_not_500(self):
        # The legacy-key comparison uses secrets.compare_digest for constant-
        # time matching. compare_digest on *str* raises TypeError for non-ASCII
        # input, which would surface as a 500; the bytes form does not, so a
        # non-ASCII bearer token is a clean 403 rather than a crash.
        from core.auth import require_lml_key

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key="secret"),
                authorization="Bearer nördic-tøken",
            )
        assert exc.value.status_code == 403

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


class TestRequireLmlKeyTableBackedKeys:
    """Dual-accept behavior added by the LML per-consumer API keys plan.

    A resolving table-backed hash passes and attributes the caller; a miss
    falls through to the legacy shared key (still-live throughout the
    migration); a miss on both is a 403 identical to today's. The distinction
    between "revoked" and "never existed" must never leak past ``resolve()``.
    """

    @pytest.mark.asyncio
    async def test_table_backed_key_passes_with_no_legacy_key_configured(self):
        from core.auth import require_lml_key

        reset_api_key_cache(_seeded_cache({"secret-canary-token": "wxyc-canary"}))

        await require_lml_key(
            request=_request(),
            settings=_settings(require_auth=True, key=None),
            authorization="Bearer secret-canary-token",
        )

    @pytest.mark.asyncio
    async def test_table_backed_key_touches_last_used(self):
        # require_lml_key hashes the bearer token exactly once and reuses the
        # digest for both the lookup and the touch, so it drives the
        # hash-based touch_by_hash (not the token-based touch_last_used).
        from core.auth import require_lml_key

        cache = _seeded_cache({"secret-canary-token": "wxyc-canary"})
        with patch.object(cache, "touch_by_hash") as touch:
            reset_api_key_cache(cache)
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer secret-canary-token",
            )
        touch.assert_called_once_with(hash_token("secret-canary-token"))

    @pytest.mark.asyncio
    async def test_table_backed_key_tags_caller_on_sentry(self):
        from core.auth import require_lml_key

        reset_api_key_cache(_seeded_cache({"secret-canary-token": "wxyc-canary"}))

        with patch("core.auth.sentry_sdk.set_tag") as set_tag:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer secret-canary-token",
            )
        set_tag.assert_called_once_with("lml.auth.caller_name", "wxyc-canary")

    @pytest.mark.asyncio
    async def test_hash_miss_falls_through_to_legacy_key_and_passes(self):
        from core.auth import require_lml_key

        reset_api_key_cache(_seeded_cache({"secret-canary-token": "wxyc-canary"}))

        # "legacy-secret" is not in the table-backed cache, but matches the
        # still-configured legacy LML_API_KEY.
        await require_lml_key(
            request=_request(),
            settings=_settings(require_auth=True, key="legacy-secret"),
            authorization="Bearer legacy-secret",
        )

    @pytest.mark.asyncio
    async def test_hash_miss_with_no_legacy_match_returns_403(self):
        from core.auth import require_lml_key

        reset_api_key_cache(_seeded_cache({"secret-canary-token": "wxyc-canary"}))

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key="legacy-secret"),
                authorization="Bearer neither-table-nor-legacy",
            )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_revoked_key_fails_identically_to_a_key_that_never_existed(self):
        # A revoked row is simply absent from the active-key SELECT (entity/
        # api_keys.py's _SELECT_ACTIVE_SQL), so from ApiKeyCache's point of
        # view a "revoked" token and one that never existed are the same
        # thing: both miss the dict. Both must land on the same 403 reason.
        from core.auth import require_lml_key

        # No legacy key configured, so a resolve() miss on either token has
        # nothing else to fall through to.
        reset_api_key_cache(_seeded_cache({"still-active-token": "backend-service"}))

        never_existed_exc = None
        revoked_exc = None
        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer some-revoked-token",
            )
        revoked_exc = exc.value
        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer some-never-issued-token",
            )
        never_existed_exc = exc.value

        assert revoked_exc.status_code == never_existed_exc.status_code == 403
        assert revoked_exc.detail == never_existed_exc.detail

    @pytest.mark.asyncio
    async def test_empty_cache_and_no_legacy_key_returns_500(self):
        # A cache that started (unlike the bare-None case already covered by
        # test_flag_on_key_unset_returns_500) but has zero active rows must
        # still 500 when there is no legacy key either.
        from core.auth import require_lml_key

        reset_api_key_cache(_seeded_cache({}))

        with pytest.raises(HTTPException) as exc:
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer anything",
            )
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_nonempty_cache_with_no_legacy_key_does_not_misconfigure_500(self):
        # The 500 only fires when BOTH the legacy key is unset AND the cache
        # is empty/unstarted -- a populated cache alone is a valid config.
        from core.auth import require_lml_key

        reset_api_key_cache(_seeded_cache({"secret-canary-token": "wxyc-canary"}))

        await require_lml_key(
            request=_request(),
            settings=_settings(require_auth=True, key=None),
            authorization="Bearer secret-canary-token",
        )


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
        # Healthy requests must not pollute the log stream. A table-backed
        # key hit is the fully-migrated, silent-success shape (unlike the
        # transitional legacy-key hit below, which deliberately DOES log).
        from core.auth import require_lml_key

        reset_api_key_cache(_seeded_cache({"secret": "wxyc-canary"}))

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            await require_lml_key(
                request=_request(),
                settings=_settings(require_auth=True, key=None),
                authorization="Bearer secret",
            )

        assert [r for r in caplog.records if r.name == "core.auth"] == []

    @pytest.mark.asyncio
    async def test_legacy_key_success_logs_warning_with_reason(self, caplog):
        # Transitional: a still-live legacy LML_API_KEY hit succeeds but is
        # deliberately NOT silent -- so not-yet-migrated traffic stays
        # visible for the length of the per-consumer-keys rollout.
        from core.auth import require_lml_key

        with caplog.at_level(logging.WARNING, logger="core.auth"):
            await require_lml_key(
                request=_request(
                    client=("10.0.0.9", 4321),
                    headers={"user-agent": "tubafrenzy/1.0"},
                ),
                settings=_settings(require_auth=True, key="secret"),
                authorization="Bearer secret",
            )

        records = [r for r in caplog.records if r.name == "core.auth"]
        assert len(records) == 1, f"expected 1 auth log, got {len(records)}: {records}"
        rec = records[0]
        assert rec.levelno == logging.WARNING
        assert rec.client_ip == "10.0.0.9"
        assert rec.user_agent == "tubafrenzy/1.0"
        assert rec.path == "/api/v1/lookup"
        assert rec.method == "POST"
        assert rec.reason == "legacy_shared_key_used"

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
        ("POST", "/api/v1/artists/resolve/bulk"),
        ("POST", "/api/v1/artists/search-aliases/bulk"),
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
