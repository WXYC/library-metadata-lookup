"""Shared request envelope for the bulk-route family (LML#767).

Every bulk route that hand-parses its body (rather than taking a typed body
parameter) needs the same envelope: raw-JSON parse, client-disconnect
handling, a manual over-cap check that raises 413 *before* Pydantic (whose
`max_length` enforcement would otherwise 422 an oversize batch), then full
model validation. The api.yaml contract documents 413 for cap exceedance,
so the raw count is read first.

The artists routes (LML#764) were the first to consolidate their two copies
into a private helper; the older bulk routes each carried a drifted copy
(missing `InterfaceError` in the transient tuple, missing the
`ClientDisconnect` arm, and treating an absent batch field as a bare-string
422). This module is the one source of truth they all now share.

Two pieces:

- ``TRANSIENT_PG_ERRORS`` — the exception tuple a bulk route catches around
  its PG work to return a transient 503 (retry may succeed) rather than an
  application 500. Includes asyncpg's client-side ``InterfaceError``
  alongside the server-side ``PostgresError`` and socket-level ``OSError``:
  asyncpg raises pool-lifecycle errors ("pool is closing", "connection is
  closed") as the base ``InterfaceError``, which subclasses neither of the
  other two, so a deploy-window pool teardown would otherwise read as a 500.
  The same class ``discogs/fallthrough.py`` pins in ``_ARMING_EXCEPTIONS`` as
  its test-asserted "DB unreachable" set.

- ``parse_bulk_body(request, model, cap, field)`` — the envelope itself,
  generalized over the batch field name (``names`` / ``inputs`` /
  ``identity_ids`` / ``items``).
"""

from __future__ import annotations

from asyncpg.exceptions import InterfaceError, PostgresError
from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError
from starlette.requests import ClientDisconnect

# The transient DB-unreachable set. A bulk route catches this around its PG
# work and returns 503; anything else is an application bug and stays a 500.
# InterfaceError is asyncpg's client-side pool/connection lifecycle class,
# which subclasses neither PostgresError (server-side) nor OSError
# (socket-level) — without it a pool teardown during a deploy reads as a 500.
TRANSIENT_PG_ERRORS: tuple[type[BaseException], ...] = (
    PostgresError,
    InterfaceError,
    OSError,
)


async def parse_bulk_body[RequestT: BaseModel](
    request: Request,
    model: type[RequestT],
    cap: int,
    field: str,
    cap_status: int = 413,
) -> RequestT:
    """Parse + cap-check a bulk route's raw JSON body, then validate.

    The manual 413 check runs before Pydantic validation: the generated
    request models carry api.yaml's ``maxItems`` as ``max_length``, but
    Pydantic's enforcement is 422. The api.yaml contract documents 413 for
    cap exceedance, so the raw count of ``field`` is read first and 413
    raised; only then does the model validate.

    An absent or wrong-type ``field`` is deliberately NOT special-cased here:
    the cap guard is ``isinstance(x, list) and len(x) > cap``, so a non-list
    value falls straight through to ``model_validate``, which produces
    Pydantic's structured ``missing`` / ``list_type`` error rather than a
    bare-string 422. Every other field already gets that structured shape;
    the batch field now matches.

    ``cap_status`` is the over-cap status: 413 by default (the api.yaml
    contract for the identity/artists routes), overridable to 400 for the
    lookup/bulk and cache-refresh routes, which document 400 for oversize
    batches. Passing it preserves each route's existing wire contract.

    Status codes: 400 (malformed JSON / client disconnect / non-object body),
    ``cap_status`` (over cap), 422 (Pydantic validation).
    """
    try:
        body = await request.json()
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Malformed JSON body: {e}") from None
    except ClientDisconnect:
        # Client closed the connection before the body completed. There is no
        # point sending a 5xx — the client is gone — but we want this to land
        # as a 4xx in logs (the route did its job; the failure is the
        # client's). Treat as 400 to keep error-class accounting clean; a
        # dropped arm would surface these as unhandled 500-shaped noise.
        raise HTTPException(
            status_code=400,
            detail="Client disconnected before request body completed.",
        ) from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    batch = body.get(field)
    if isinstance(batch, list) and len(batch) > cap:
        raise HTTPException(
            status_code=cap_status,
            detail=f"Batch exceeded the {cap}-item cap (received {len(batch)}).",
        )
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=e.errors()) from None


def resolve_input_cap(request_model: type[BaseModel], field: str) -> int:
    """Read a batch field's ``maxItems`` from a generated model's JSON schema.

    Drift guard: api.yaml's ``maxItems`` for the batch field drives the
    generated request model's constraint. The routes enforce the cap as 413
    before Pydantic validation; sourcing the literal from the model means a
    future api.yaml change that regenerates the model automatically updates
    the manual 413 gate.

    Uses ``model_json_schema()`` rather than the lower-level
    ``model_fields[...].metadata`` introspection because the JSON-schema
    export is Pydantic's documented public API (it's tied to OpenAPI
    generation and unlikely to drift across minor versions), whereas the
    metadata shape is implementation detail that has changed before.
    ``pydantic`` is pinned only as a lower bound (``>=2.0.0``) so this
    distinction matters across deploys.

    Raises at module import time if ``maxItems`` is missing — fail loud, so a
    regenerated model that drops the constraint can't silently widen the 413
    gate and produce 422 responses for cap-exceeded requests.
    """
    schema = request_model.model_json_schema()
    field_schema = schema.get("properties", {}).get(field, {})
    max_items = field_schema.get("maxItems")
    if isinstance(max_items, int) and max_items > 0:
        return max_items
    raise RuntimeError(
        f"Cannot resolve `maxItems` for {request_model.__name__}.{field} "
        "from its JSON schema. The route's 413 gate must agree with the "
        "generated model; fix or regenerate the model before this module "
        f"can import (schema fragment: {field_schema!r})."
    )
