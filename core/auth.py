"""Bearer-token auth for tubafrenzy / Backend-Service -> LML calls, plus /admin/*.

``require_lml_key`` is a separate auth mechanism -- deliberately, a separate
secret -- from ``require_admin_token``'s ``ADMIN_TOKEN`` (used for
library.db / streaming_availability.db uploads and tombstone clearing) and
from the ``ETL_NOTIFY_KEY`` LML uses when *pushing* the streaming-status
webhook to tubafrenzy. ``LML_API_KEY`` covers the *inbound* direction:
callers like tubafrenzy (servlets) and Backend-Service include
``Authorization: Bearer <LML_API_KEY>`` on every request to a protected
endpoint. The two deps share their bearer-parsing and constant-time-compare
core (``_parse_bearer_token`` / ``_token_matches``) but keep independent
policy -- most visibly, what happens when the expected secret itself isn't
configured (see ``require_admin_token``'s docstring for the asymmetry).

Enforcement is gated by ``LML_REQUIRE_AUTH`` so the dep can be deployed and
attached to routes before consumers are updated. Roll out: deploy with the
flag off, update each consumer to send the bearer header, then flip the flag
on. See ``docs/lml-release-resolve-plan.md`` (in tubafrenzy) for the full
phasing.

Structured logging on the 401 / 403 branches (WXYC/library-metadata-lookup#360):
prod sees ~946 unauthenticated ``/api/v1/lookup`` calls per day and we can't
identify the caller from Sentry span attributes (no work happens on the
unauth path, no useful tags get set). Each rejection emits a single
``logger.warning`` with the request context (client IP, user-agent, path,
method, reason) so we can grep Railway logs to find the dominant caller.
The token itself is never logged — the reason code distinguishes the
failure modes.

Dual-accept, transitional (LML per-consumer API keys plan,
``docs/plans/lml-per-consumer-api-keys.md``): a bearer token whose SHA-256
hash resolves through the in-process ``ApiKeyCache`` (``core/api_key_cache.py``,
backed by ``lml_cache.api_keys``) identifies a specific consumer and is the
long-term mechanism — it attributes the caller (a Sentry span tag) and
touches ``last_used_at`` (fire-and-forget). The single shared ``LML_API_KEY``
value keeps working alongside it for the length of the migration, logged at
WARNING with a distinct ``reason=legacy_shared_key_used`` so not-yet-migrated
traffic stays visible. Plan PR 4 removes the legacy branch once every
consumer shows zero of those log lines for 7 consecutive days.
"""

from __future__ import annotations

import logging
import secrets

import sentry_sdk
from fastapi import Depends, Header, HTTPException, Request

from config.settings import Settings, get_settings
from core.api_key_cache import get_api_key_cache, hash_token

logger = logging.getLogger(__name__)


def _client_ip(request: Request) -> str | None:
    """Return the originating client IP, honoring ``X-Forwarded-For`` first hop.

    Railway terminates TLS at its edge and forwards via a proxy, so
    ``request.client.host`` is the proxy's address. ``X-Forwarded-For`` carries
    the original client as its leftmost entry (rest of the chain is proxies).
    Fall back to ``request.client.host`` when the header is absent.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first_hop = forwarded.split(",", 1)[0].strip()
        if first_hop:
            return first_hop
    if request.client is not None:
        return request.client.host
    return None


def _auth_log_fields(request: Request, *, reason: str) -> dict[str, str | None]:
    """Build the minimal, stable field set both auth log helpers emit.

    Kept as one helper so the "who is the dominant caller" Railway-log query
    stays joinable: a future change to the field set (adding a trace id, say)
    lands in both the rejection log and the legacy-key-used log at once.
    """
    return {
        "client_ip": _client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "path": request.url.path,
        "method": request.method,
        "reason": reason,
    }


def _log_auth_rejection(request: Request, *, reason: str) -> None:
    """Emit a structured WARNING for an unauthenticated/unauthorized call.

    Fields land on the log record both as ``extra`` kwargs (so a JSON
    formatter can lift them) and as ``%(...)s`` substitutions in the message
    (so the default text formatter still surfaces them).
    """
    fields = _auth_log_fields(request, reason=reason)
    logger.warning("lml.auth.rejected %s", fields, extra=fields)


def _log_legacy_key_used(request: Request) -> None:
    """WARNING on a still-live legacy ``LML_API_KEY`` hit (``reason=legacy_shared_key_used``).

    Distinct from ``_log_auth_rejection``'s reject reasons — this call
    SUCCEEDS (the request proceeds); it exists so not-yet-migrated traffic
    stays visible for the length of the LML per-consumer API keys rollout.
    """
    fields = _auth_log_fields(request, reason="legacy_shared_key_used")
    logger.warning("lml.auth.legacy_key_used %s", fields, extra=fields)


def _parse_bearer_token(authorization: str) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header value.

    Returns ``None`` when the header isn't in "<scheme> <token>" form, or the
    scheme isn't ``bearer`` (case-insensitive per RFC 7235). Assumes the header
    is present -- callers check that first, since ``require_lml_key`` and
    ``require_admin_token`` both distinguish "missing" (401) from "malformed"
    (403) and this helper only covers the latter.
    """
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def _token_matches(token: str, expected: str) -> bool:
    """Constant-time comparison of a bearer token against an expected secret.

    Shared by ``require_lml_key`` (``LML_API_KEY``) and ``require_admin_token``
    (``ADMIN_TOKEN``) -- two distinct secrets, same timing-safe comparison
    core. Compares the bytes form (not the str form) deliberately:
    ``secrets.compare_digest`` raises ``TypeError`` on a non-ASCII str, which
    would turn a wrong-token 403 into a 500.
    """
    return secrets.compare_digest(token.encode(), expected.encode())


def _tag_caller(caller_name: str) -> None:
    """Stamp the resolved caller name onto the request's Sentry span.

    The direct answer to "who is calling us" (LML per-consumer API keys plan,
    "Observability"): a span tag rather than a per-request log line, since the
    dominant caller (Backend-Service's live proxy path) is high-volume enough
    that a bare ``logger.info`` on every hit would rival authenticated traffic
    itself. Observability must never break the request path — a failure here
    logs and the request proceeds (mirrors
    ``core.bulk_concurrency._project_global_capped``'s try/except-and-continue
    posture).
    """
    try:
        sentry_sdk.set_tag("lml.auth.caller_name", caller_name)
    except Exception:
        logger.warning("Failed to tag lml.auth.caller_name onto Sentry scope", exc_info=True)


async def require_lml_key(
    request: Request,
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
) -> None:
    """Validate the ``Authorization: Bearer <token>`` header.

    Behavior matrix:

    - ``LML_REQUIRE_AUTH=false`` -> no-op (the deploy-then-flip rollout window).
    - ``LML_REQUIRE_AUTH=true`` and neither a legacy ``LML_API_KEY`` nor any active
      table-backed key is configured -> 500 (misconfig; fail loudly so we don't
      accept all requests by accident).
    - ``LML_REQUIRE_AUTH=true`` and header missing -> 401 + WARNING log
      (``reason=missing_authorization``).
    - ``LML_REQUIRE_AUTH=true`` and header present but malformed scheme -> 403
      + WARNING log (``reason=invalid_token_scheme``).
    - ``LML_REQUIRE_AUTH=true`` and the bearer token's SHA-256 hash resolves via the
      in-process ``ApiKeyCache`` (``core/api_key_cache.py``, backed by
      ``lml_cache.api_keys``) -> pass; fire a fire-and-forget ``last_used_at`` touch
      and stamp the resolved caller name onto the request's Sentry span.
    - ``LML_REQUIRE_AUTH=true`` and the token equals the legacy ``LML_API_KEY`` (when
      still configured) -> pass + WARNING log (``reason=legacy_shared_key_used``),
      transitional for the length of the per-consumer-keys migration.
    - ``LML_REQUIRE_AUTH=true`` and the token matches neither -> 403 + WARNING log
      (``reason=invalid_token_value``). A revoked table-backed key and a key that
      never existed both land here identically — the distinction is never leaked
      to the caller.
    """
    if not settings.lml_require_auth:
        return

    cache = get_api_key_cache()
    if not settings.lml_api_key and (cache is None or cache.is_empty):
        raise HTTPException(
            status_code=500,
            detail="LML auth required but no legacy key or active table-backed keys are configured",
        )

    if not authorization:
        _log_auth_rejection(request, reason="missing_authorization")
        raise HTTPException(status_code=401, detail="Missing authorization")

    token = _parse_bearer_token(authorization)
    if token is None:
        _log_auth_rejection(request, reason="invalid_token_scheme")
        raise HTTPException(status_code=403, detail="Invalid token")

    if cache is not None:
        # Hash once and reuse the digest for both the lookup and the touch.
        key_hash = hash_token(token)
        caller_name = cache.resolve_by_hash(key_hash)
        if caller_name is not None:
            cache.touch_by_hash(key_hash)
            _tag_caller(caller_name)
            return

    # Constant-time compare of the shared secret (see _token_matches).
    if settings.lml_api_key and _token_matches(token, settings.lml_api_key):
        _log_legacy_key_used(request)
        return

    _log_auth_rejection(request, reason="invalid_token_value")
    raise HTTPException(status_code=403, detail="Invalid token")


async def require_admin_token(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
) -> None:
    """Validate ``Authorization: Bearer <token>`` against ``ADMIN_TOKEN``.

    Guards ``routers/admin.py``'s incident-grade endpoints (library.db /
    streaming_availability.db upload + download, tombstone clearing) -- a
    deliberately separate secret from ``require_lml_key``'s ``LML_API_KEY``,
    so routine tubafrenzy / Backend-Service callers never inherit admin write
    access by sharing a token.

    Extracted from ``routers/admin.py``'s formerly hand-rolled
    ``_validate_auth`` (WXYC/library-metadata-lookup#1047), which compared the
    token with a timing-unsafe ``!=``; this shares ``_parse_bearer_token`` /
    ``_token_matches`` with ``require_lml_key``, so the comparison is now
    constant-time. Every status code and message below is otherwise unchanged
    from ``_validate_auth``'s wire behavior -- this is a consolidation, not a
    policy change.

    Behavior matrix:

    - ``ADMIN_TOKEN`` unset -> 403 "Admin endpoint disabled (no ADMIN_TOKEN
      set)", checked before the header is even inspected. This is a
      deliberate asymmetry with ``require_lml_key``, which 500s on its
      analogous misconfiguration (no legacy key + empty table-backed cache):
      admin's unset-token case has returned 403 since the endpoints were
      introduced, and this refactor preserves that rather than also
      reconciling the inconsistency.
    - Header missing -> 401 "Missing authorization".
    - Header present but malformed (not "Bearer <token>", or a non-bearer
      scheme) -> 403 "Invalid token".
    - Token present but doesn't match ``ADMIN_TOKEN`` -> 403 "Invalid token".
    - Token matches -> pass.
    """
    if not settings.admin_token:
        raise HTTPException(status_code=403, detail="Admin endpoint disabled (no ADMIN_TOKEN set)")

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization")

    token = _parse_bearer_token(authorization)
    if token is None or not _token_matches(token, settings.admin_token):
        raise HTTPException(status_code=403, detail="Invalid token")
