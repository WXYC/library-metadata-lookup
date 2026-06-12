"""Tests for the LML#537 rate-limiter telemetry tags.

When the ``fallthrough`` seam (or the ``request_context`` helper used by
the three API-only methods that bypass it) falls through to the API leg,
it sets ``_request_context_var`` carrying the seam's ``label`` and the
resolved ``cache_state``. ``_request_with_retry`` reads that contextvar
inside its ``lml.discogs.semaphore`` and ``lml.discogs.rate_limiter``
spans, tagging each with ``lml.discogs.method`` and
``lml.discogs.cache_state``. This lets the Sentry wait-time histogram be
split by which method's cache-miss triggered the wait and by how the
cache leg resolved (``miss``, ``skip``, ``no_pg``, ``cooldown``).

Covered:

1. **Tag presence on miss** — both spans carry method + cache_state.
2. **All four cache_state values** — parametrized over ``miss``, ``skip``,
   ``no_pg``, ``cooldown``.
3. **CancelledError reset** — the contextvar's ``Token`` reset survives
   ``asyncio.CancelledError``.
4. **No contextvar set** — direct ``_request_with_retry`` callers (a
   handful of legacy tests) don't have a contextvar set; spans should
   not blow up and should not carry the tags.
5. **request_context helper** — used by ``search_releases_by_album_title``,
   ``get_label_image``, ``get_master`` (the three API-only methods that
   bypass ``fallthrough``); spans get tagged the same as the seam path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from discogs import fallthrough as fallthrough_mod
from discogs.fallthrough import (
    _request_context_var,
    _reset_cool_down_for_tests,
    fallthrough,
    request_context,
)
from discogs.memory_cache import set_skip_cache
from discogs.service import DiscogsService


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


@pytest.fixture
def service():
    """A real DiscogsService — its ``_get_client`` honors a pre-set
    ``self._client`` (see service.py:286), letting tests inject a mock
    client without bypassing ``__init__``."""
    return DiscogsService(token="test-token")


@pytest.fixture
def captured_spans():
    """Patch ``sentry_sdk.start_span`` (the symbol both modules reference;
    they resolve to the same module object) with a recorder that captures
    each span as a MagicMock keyed by ``name``. Mirrors the canonical
    pattern used by ``test_discogs_service.py``."""
    spans: dict[str, MagicMock] = {}

    def _start_span(*, op: str, name: str, **kwargs):
        ctx = MagicMock()
        span = MagicMock()
        span.op = op
        ctx.__enter__.return_value = span
        ctx.__exit__.return_value = False
        spans[name] = span
        return ctx

    with (
        patch("discogs.service.sentry_sdk.start_span", side_effect=_start_span),
        patch("discogs.fallthrough.sentry_sdk.start_span", side_effect=_start_span),
    ):
        yield spans


async def _drive_request_with_retry(service: DiscogsService) -> None:
    """Drive ``_request_with_retry`` against an in-memory mock client +
    mocked semaphore/limiter. Does not issue an HTTP request."""
    fake_semaphore = MagicMock()
    fake_semaphore.acquire = AsyncMock()
    fake_semaphore.release = MagicMock()
    fake_semaphore._waiters = []

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.headers = {}

    mock_client = MagicMock()
    mock_client.request = AsyncMock(return_value=fake_response)
    service._client = mock_client

    with (
        patch("discogs.service.get_semaphore", return_value=fake_semaphore),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
    ):
        await service._request_with_retry("GET", "/releases/12345")


def _assert_tagged(span: MagicMock, method: str, state: str) -> None:
    """Both wait spans must carry ``lml.discogs.method`` and ``lml.discogs.cache_state``."""
    data = {call.args[0]: call.args[1] for call in span.set_data.call_args_list}
    assert data.get("lml.discogs.method") == method, (
        f"span {span} missing/wrong method tag; calls: {data}"
    )
    assert data.get("lml.discogs.cache_state") == state, (
        f"span {span} missing/wrong cache_state tag; calls: {data}"
    )


# ---------------------------------------------------------------------------
# 1. Tag presence on the miss path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semaphore_and_rate_limiter_spans_tagged_on_miss(
    service, captured_spans, _ensure_skip_cache_clear
):
    """The common cache-miss flow: PG returned None, seam fell through to
    the API leg, both wait spans carry the method + state tags."""
    pg_read = AsyncMock(return_value=None)

    async def api_fetch():
        await _drive_request_with_retry(service)
        return "fresh-value"

    result = await fallthrough(
        label="get_release",
        pg_read=pg_read,
        api_fetch=api_fetch,
    )

    assert result == "fresh-value"
    sem_span = captured_spans.get("lml.discogs.semaphore")
    rate_span = captured_spans.get("lml.discogs.rate_limiter")
    assert sem_span is not None and rate_span is not None, (
        f"expected both wait spans, got: {list(captured_spans)}"
    )
    _assert_tagged(sem_span, "get_release", "miss")
    _assert_tagged(rate_span, "get_release", "miss")


# ---------------------------------------------------------------------------
# 2. All four cache_state values
# ---------------------------------------------------------------------------


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
async def test_cache_state_resolves_correctly(
    service, captured_spans, _ensure_skip_cache_clear, scenario, expected_state
):
    """The seam must compute cache_state from its own gating logic, not
    from the caller's inputs verbatim."""
    pg_read: AsyncMock | None = AsyncMock(return_value=None)

    if scenario == "skip":
        set_skip_cache(True)
    elif scenario == "no_pg":
        pg_read = None
    elif scenario == "cooldown":
        # Arm the cool-down by hand — mirrors the existing test pattern in
        # test_fallthrough.py (which already pokes _cool_down_until).
        import time

        fallthrough_mod._cool_down_until = time.monotonic() + 5

    async def api_fetch():
        await _drive_request_with_retry(service)
        return "fresh-value"

    await fallthrough(
        label="get_release",
        pg_read=pg_read,
        api_fetch=api_fetch,
    )

    for name in ("lml.discogs.semaphore", "lml.discogs.rate_limiter"):
        span = captured_spans.get(name)
        assert span is not None, f"expected {name} span; got: {list(captured_spans)}"
        _assert_tagged(span, "get_release", expected_state)


# ---------------------------------------------------------------------------
# 3. CancelledError reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_resets_on_cancelled_error(captured_spans, _ensure_skip_cache_clear):
    """If the API leg is cancelled mid-flight, the seam's `finally` must
    still reset the contextvar (Token-paired set/reset)."""
    pg_read = AsyncMock(return_value=None)

    async def cancelling_api_fetch():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await fallthrough(
            label="get_release",
            pg_read=pg_read,
            api_fetch=cancelling_api_fetch,
        )

    assert _request_context_var.get() is None


@pytest.mark.asyncio
async def test_context_resets_on_normal_return(captured_spans, _ensure_skip_cache_clear):
    """Sibling of the cancellation case — normal return also resets so the
    next call in the same task doesn't see stale state."""
    pg_read = AsyncMock(return_value=None)

    async def api_fetch():
        return "fresh-value"

    await fallthrough(
        label="get_release",
        pg_read=pg_read,
        api_fetch=api_fetch,
    )

    assert _request_context_var.get() is None


# ---------------------------------------------------------------------------
# 4. No contextvar set — direct callers untouched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_tag_when_called_outside_seam(service, captured_spans):
    """A handful of legacy tests call ``_request_with_retry`` directly.
    Without a contextvar set, the spans must not blow up and must not
    carry the LML#537 tags."""
    assert _request_context_var.get() is None
    await _drive_request_with_retry(service)

    for name in ("lml.discogs.semaphore", "lml.discogs.rate_limiter"):
        span = captured_spans.get(name)
        assert span is not None
        data_keys = {call.args[0] for call in span.set_data.call_args_list}
        assert "lml.discogs.method" not in data_keys
        assert "lml.discogs.cache_state" not in data_keys


# ---------------------------------------------------------------------------
# 5. request_context helper — tags spans for API-only methods that bypass fallthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_context_tags_direct_caller_spans(
    service, captured_spans, _ensure_skip_cache_clear
):
    """``search_releases_by_album_title``, ``get_label_image``, ``get_master``
    each call ``_request_with_retry`` directly (no fallthrough). The
    ``request_context`` helper they wrap their call in must propagate the
    method tag + cache_state=no_pg the same way the seam does."""
    with request_context("get_label_image", "no_pg"):
        await _drive_request_with_retry(service)

    for name in ("lml.discogs.semaphore", "lml.discogs.rate_limiter"):
        span = captured_spans.get(name)
        assert span is not None
        _assert_tagged(span, "get_label_image", "no_pg")

    # And the contextvar resets cleanly outside the with-block.
    assert _request_context_var.get() is None
