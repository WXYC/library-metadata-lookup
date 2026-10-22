"""Tests for the LML#537 request-context contextvar.

When the ``fallthrough`` seam (or the ``request_context`` helper used by
the API-only methods that bypass it) falls through to the API leg, it sets
``_request_context_var`` carrying the seam's ``label`` and the resolved
``cache_state`` (``miss``, ``skip``, ``no_pg``, ``cooldown``). The Sentry
span tagging that read it is gone with tracing (BS#3393 D2); the contextvar
itself is still pinned here.

Covered:

1. **cache_state resolution** -- parametrized over ``miss``, ``skip``,
   ``no_pg``, ``cooldown``, read from inside the API leg.
2. **Reset** -- the contextvar's ``Token`` reset survives
   ``asyncio.CancelledError`` and a normal return.
3. **request_context helper** -- used by the API-only methods that bypass
   ``fallthrough``; defaults to ``cache_state="no_pg"`` and resets on exit.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from discogs import fallthrough as fallthrough_mod
from discogs.fallthrough import (
    _reset_cool_down_for_tests,
    fallthrough,
    get_request_context,
    request_context,
)
from discogs.memory_cache import set_skip_cache


@pytest.fixture(autouse=True)
def _reset_cool_down():
    """Cool-down is process-wide; reset between tests so cases don't bleed."""
    _reset_cool_down_for_tests()
    yield
    _reset_cool_down_for_tests()


@pytest.fixture
def _ensure_skip_cache_clear():
    """Reset the per-request skip flag between cases."""
    set_skip_cache(False)
    yield
    set_skip_cache(False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,expected_state",
    [
        ("miss", "miss"),
        ("skip", "skip"),
        ("no_pg", "no_pg"),
        ("cooldown", "cooldown"),
    ],
)
async def test_cache_state_resolves_correctly(_ensure_skip_cache_clear, scenario, expected_state):
    """The seam must compute cache_state from its own gating logic, not
    from the caller's inputs verbatim."""
    pg_read: AsyncMock | None = AsyncMock(return_value=None)

    if scenario == "skip":
        set_skip_cache(True)
    elif scenario == "no_pg":
        pg_read = None
    elif scenario == "cooldown":
        # Arm the cool-down by hand -- mirrors the existing test pattern in
        # test_fallthrough.py (which already pokes _cool_down_until).
        fallthrough_mod._cool_down_until = time.monotonic() + 5

    seen: list[dict[str, str] | None] = []

    async def api_fetch():
        seen.append(get_request_context())
        return "fresh-value"

    await fallthrough(label="get_release", pg_read=pg_read, api_fetch=api_fetch)

    assert seen == [{"method": "get_release", "cache_state": expected_state}]


@pytest.mark.asyncio
async def test_context_resets_on_cancelled_error(_ensure_skip_cache_clear):
    """If the API leg is cancelled mid-flight, the seam's `finally` must
    still reset the contextvar (Token-paired set/reset)."""
    pg_read = AsyncMock(return_value=None)

    async def cancelling_api_fetch():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await fallthrough(label="get_release", pg_read=pg_read, api_fetch=cancelling_api_fetch)

    assert get_request_context() is None


@pytest.mark.asyncio
async def test_context_resets_on_normal_return(_ensure_skip_cache_clear):
    """Sibling of the cancellation case -- normal return also resets so the
    next call in the same task doesn't see stale state."""
    pg_read = AsyncMock(return_value=None)

    async def api_fetch():
        return "fresh-value"

    await fallthrough(label="get_release", pg_read=pg_read, api_fetch=api_fetch)

    assert get_request_context() is None


def test_request_context_helper_sets_no_pg_and_resets():
    """``search_releases_by_album_title``, ``get_label_image``, ``get_master``,
    and ``search_artists`` wrap their direct ``_request_with_retry`` call in
    ``request_context``; it must carry the method + cache_state=no_pg and
    reset cleanly outside the with-block."""
    assert get_request_context() is None
    with request_context("get_label_image"):
        assert get_request_context() == {"method": "get_label_image", "cache_state": "no_pg"}
    assert get_request_context() is None
