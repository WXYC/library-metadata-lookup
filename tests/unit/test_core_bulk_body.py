"""Unit tests for the shared bulk-route request envelope (`core/bulk_body.py`).

The envelope helper (`parse_bulk_body`) and the transient-PG-error tuple
(`TRANSIENT_PG_ERRORS`) are the single source of truth the whole bulk-route
family shares (LML#767). The artists routes (LML#764) were the template; this
module pins the generalized behavior directly, independent of any one route:

- the `field` parameter generalizes what was a hardcoded `names`;
- absent/wrong-type batch fields fall through to `model_validate` so
  Pydantic produces its structured `errors()` array (not a bare-string 422);
- `TRANSIENT_PG_ERRORS` includes asyncpg's client-side `InterfaceError`
  alongside `PostgresError` and `OSError`.
"""

from __future__ import annotations

import pytest
from asyncpg.exceptions import InterfaceError, PostgresError
from fastapi import HTTPException
from pydantic import BaseModel, Field
from starlette.requests import ClientDisconnect

from core.bulk_body import TRANSIENT_PG_ERRORS, parse_bulk_body


class _NamesModel(BaseModel):
    names: list[str] = Field(..., min_length=1, max_length=3)


class _InputsModel(BaseModel):
    inputs: list[int] = Field(..., min_length=1, max_length=3)


class _FakeRequest:
    """Minimal stand-in for starlette.Request — only `.json()` is exercised."""

    def __init__(self, *, body=None, raises=None):
        self._body = body
        self._raises = raises

    async def json(self):
        if self._raises is not None:
            raise self._raises
        return self._body


class TestTransientPgErrors:
    def test_includes_interface_error(self):
        """asyncpg's client-side pool-lifecycle errors subclass neither
        PostgresError nor OSError; the tuple must name InterfaceError so a
        deploy-window pool teardown reads as a transient 503, not a 500."""
        assert InterfaceError in TRANSIENT_PG_ERRORS

    def test_includes_postgres_error_and_oserror(self):
        assert PostgresError in TRANSIENT_PG_ERRORS
        assert OSError in TRANSIENT_PG_ERRORS


class TestParseBulkBody:
    @pytest.mark.asyncio
    async def test_valid_body_returns_model(self):
        req = _FakeRequest(body={"names": ["Wishy"]})
        result = await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert isinstance(result, _NamesModel)
        assert result.names == ["Wishy"]

    @pytest.mark.asyncio
    async def test_field_parameter_is_honored(self):
        """The helper is generalized over the batch field name — `inputs`
        here, not the artists routes' hardcoded `names`."""
        req = _FakeRequest(body={"inputs": [1, 2]})
        result = await parse_bulk_body(req, _InputsModel, cap=3, field="inputs")
        assert result.inputs == [1, 2]

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self):
        req = _FakeRequest(raises=ValueError("bad json"))
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_client_disconnect_returns_400(self):
        req = _FakeRequest(raises=ClientDisconnect())
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert exc.value.status_code == 400
        assert "disconnected" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_non_dict_body_returns_400(self):
        req = _FakeRequest(body=["not", "a", "dict"])
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_over_cap_returns_413(self):
        req = _FakeRequest(body={"names": ["a", "b", "c", "d"]})
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert exc.value.status_code == 413

    @pytest.mark.asyncio
    async def test_cap_status_is_parameterized(self):
        """Some routes (lookup/bulk, cache-refresh) document 400 for oversize
        batches, not 413. The helper takes `cap_status` (default 413) so the
        shared envelope serves both without changing any route's wire
        contract."""
        req = _FakeRequest(body={"names": ["a", "b", "c", "d"]})
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names", cap_status=400)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_absent_field_falls_through_to_structured_422(self):
        """A missing batch field must NOT be a bare-string "must be a JSON
        array" 422 — it falls through to `model_validate` so Pydantic emits
        its structured `errors()` list (the missing/list_type shape every
        other field gets)."""
        req = _FakeRequest(body={})
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert exc.value.status_code == 422
        # Structured detail: a list of Pydantic error dicts, not a string.
        assert isinstance(exc.value.detail, list)
        assert exc.value.detail
        assert exc.value.detail[0]["loc"] == ("names",)
        assert exc.value.detail[0]["type"] == "missing"

    @pytest.mark.asyncio
    async def test_wrong_type_field_falls_through_to_structured_422(self):
        """A wrong-type batch field (string, not list) also falls through to
        Pydantic's structured list_type error rather than the bare-string
        type gate — same 422 status, richer detail."""
        req = _FakeRequest(body={"names": "not a list"})
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert exc.value.status_code == 422
        assert isinstance(exc.value.detail, list)
        assert exc.value.detail[0]["loc"] == ("names",)
        assert exc.value.detail[0]["type"] == "list_type"

    @pytest.mark.asyncio
    async def test_over_cap_check_tolerates_non_list_field(self):
        """The cap guard must be `isinstance(x, list) and len(x) > cap` so a
        non-list field doesn't raise TypeError in `len()` before falling
        through to structured validation."""
        # A dict field is neither over-cap nor a list — must not 413, must
        # reach model_validate and 422 there.
        req = _FakeRequest(body={"names": {"not": "a list"}})
        with pytest.raises(HTTPException) as exc:
            await parse_bulk_body(req, _NamesModel, cap=3, field="names")
        assert exc.value.status_code == 422
