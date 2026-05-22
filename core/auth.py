"""Bearer-token auth for tubafrenzy / Backend-Service -> LML calls.

This is a separate auth mechanism from ``routers/admin.py``'s ``ADMIN_TOKEN``
(used for library.db uploads) and from the ``ETL_NOTIFY_KEY`` LML uses when
*pushing* the streaming-status webhook to tubafrenzy. ``LML_API_KEY`` covers
the *inbound* direction: callers like tubafrenzy (servlets) and Backend-Service
include ``Authorization: Bearer <LML_API_KEY>`` on every request to a
protected endpoint.

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
The token itself is never logged — the reason code distinguishes the three
failure modes.
"""

from __future__ import annotations

import logging

from fastapi import Depends, Header, HTTPException, Request

from config.settings import Settings, get_settings

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


def _log_auth_rejection(request: Request, *, reason: str) -> None:
    """Emit a structured WARNING for an unauthenticated/unauthorized call.

    Keep the field set minimal and stable so a single Railway-log query can
    answer "who is the dominant caller". Fields land on the log record both
    as ``extra`` kwargs (so a JSON formatter can lift them) and as
    ``%(...)s`` substitutions in the message (so the default text formatter
    still surfaces them).
    """
    fields = {
        "client_ip": _client_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "path": request.url.path,
        "method": request.method,
        "reason": reason,
    }
    logger.warning("lml.auth.rejected %s", fields, extra=fields)


async def require_lml_key(
    request: Request,
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
) -> None:
    """Validate the ``Authorization: Bearer <token>`` header against ``LML_API_KEY``.

    Behavior matrix:

    - ``LML_REQUIRE_AUTH=false`` -> no-op (the deploy-then-flip rollout window).
    - ``LML_REQUIRE_AUTH=true`` and ``LML_API_KEY`` unset -> 500 (misconfig; fail loudly
      so we don't accept all requests by accident).
    - ``LML_REQUIRE_AUTH=true`` and header missing -> 401 + WARNING log
      (``reason=missing_authorization``).
    - ``LML_REQUIRE_AUTH=true`` and header present but malformed scheme -> 403
      + WARNING log (``reason=invalid_token_scheme``).
    - ``LML_REQUIRE_AUTH=true`` and scheme correct but token wrong -> 403
      + WARNING log (``reason=invalid_token_value``).
    - ``LML_REQUIRE_AUTH=true`` and header is ``Bearer <correct>`` (case-insensitive
      scheme per RFC 7235) -> pass.
    """
    if not settings.lml_require_auth:
        return

    if not settings.lml_api_key:
        raise HTTPException(
            status_code=500,
            detail="LML auth required but LML_API_KEY is not configured",
        )

    if not authorization:
        _log_auth_rejection(request, reason="missing_authorization")
        raise HTTPException(status_code=401, detail="Missing authorization")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        _log_auth_rejection(request, reason="invalid_token_scheme")
        raise HTTPException(status_code=403, detail="Invalid token")
    if parts[1] != settings.lml_api_key:
        _log_auth_rejection(request, reason="invalid_token_value")
        raise HTTPException(status_code=403, detail="Invalid token")
