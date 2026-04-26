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
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from config.settings import Settings, get_settings


async def require_lml_key(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
) -> None:
    """Validate the ``Authorization: Bearer <token>`` header against ``LML_API_KEY``.

    Behavior matrix:

    - ``LML_REQUIRE_AUTH=false`` -> no-op (the deploy-then-flip rollout window).
    - ``LML_REQUIRE_AUTH=true`` and ``LML_API_KEY`` unset -> 500 (misconfig; fail loudly
      so we don't accept all requests by accident).
    - ``LML_REQUIRE_AUTH=true`` and header missing -> 401.
    - ``LML_REQUIRE_AUTH=true`` and header present but malformed or wrong token -> 403.
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
        raise HTTPException(status_code=401, detail="Missing authorization")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != settings.lml_api_key:
        raise HTTPException(status_code=403, detail="Invalid token")
