"""Tests for the LML#537 rate-limiter telemetry tags.

When the `fallthrough` seam falls through to the API leg, it sets a
`_request_context_var` carrying the seam's `label` and the resolved
`cache_state`. `_request_with_retry` reads that contextvar inside its
`lml.discogs.semaphore` and `lml.discogs.rate_limiter` spans, tagging
each with `lml.discogs.method` and `lml.discogs.cache_state`. This lets
the Sentry wait-time histogram be split by which method's cache-miss
triggered the wait and by how the cache leg resolved (`miss`, `skip`,
`no_pg`, `cooldown`).

Covered:

1. **Tag presence on miss** — both spans carry method + cache_state.
2. **All four cache_state values** — parametrized over `miss`, `skip`,
   `no_pg`, `cooldown`.
3. **CancelledError reset** — the contextvar's `Token` reset survives
   `asyncio.CancelledError` so a cancelled call doesn't leak state into
   the next one.
4. **No contextvar set** — direct `_request_with_retry` callers (the
   health probe, tests) don't have a contextvar set; spans should not
   blow up and should not carry the tags.
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
)
from discogs.memory_cache import set_skip_cache


class _SpanRecorder:
    """Replacement for ``sentry_sdk.start_span`` that records every span's
    data dict so tests can assert tag values after the fact."""

    def __init__(self) -> None:
        self.spans: list[dict[str, object]] = []

    def __call__(self, *, op: str, name: str) -> _SpanRecorder._Ctx:
        record: dict[str, object] = {"op": op, "name": name, "data": {}}
        self.spans.append(record)
        return self._Ctx(record)

    class _Ctx:
        def __init__(self, record: dict[str, object]) -> None:
            self._record = record

        def __enter__(self) -> _SpanRecorder._Span:
            return _SpanRecorder._Span(self._record)

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    class _Span:
        def __init__(self, record: dict[str, object]) -> None:
            self._record = record

        def set_data(self, key: str, value: object) -> None:
            data = self._record["data"]
            assert isinstance(data, dict)
            data[key] = value


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
def span_recorder():
    """Patch ``sentry_sdk.start_span`` (the symbol both modules use)."""
    recorder = _SpanRecorder()
    with (
        patch("discogs.service.sentry_sdk.start_span", recorder),
        patch("discogs.fallthrough.sentry_sdk.start_span", recorder),
    ):
        yield recorder


def _request_with_retry_spans(recorder: _SpanRecorder) -> list[dict[str, object]]:
    """Filter the recorder's spans down to the two emitted by
    ``DiscogsService._request_with_retry`` (the `lock.acquire` ones)."""
    return [s for s in recorder.spans if s["op"] == "lock.acquire"]


async def _drive_request_with_retry() -> None:
    """Call `_request_with_retry` against a mocked-out semaphore +
    rate-limiter so the test doesn't actually issue an HTTP request.

    The httpx response is mocked too — we only care that the two spans
    fire under whichever contextvar state the test set up."""
    from discogs.service import DiscogsService

    fake_semaphore = MagicMock()
    fake_semaphore.acquire = AsyncMock()
    fake_semaphore.release = MagicMock()
    fake_semaphore._waiters = []

    fake_limiter = MagicMock()
    fake_limiter.acquire = AsyncMock()

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.headers = {}

    fake_client = MagicMock()
    fake_client.request = AsyncMock(return_value=fake_response)

    service = DiscogsService.__new__(DiscogsService)
    service._get_client = AsyncMock(return_value=fake_client)  # type: ignore[method-assign]

    with (
        patch("discogs.service.get_semaphore", return_value=fake_semaphore),
        patch("discogs.service.get_rate_limiter", return_value=fake_limiter),
    ):
        await service._request_with_retry("GET", "/releases/12345")


# ---------------------------------------------------------------------------
# 1. Tag presence on the miss path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semaphore_and_rate_limiter_spans_tagged_on_miss(
    span_recorder, _ensure_skip_cache_clear
):
    """The common cache-miss flow: PG returned None, seam fell through to
    the API leg, both wait spans carry the method + state tags."""
    pg_read = AsyncMock(return_value=None)

    async def api_fetch():
        await _drive_request_with_retry()
        return "fresh-value"

    result = await fallthrough(
        label="get_release",
        pg_read=pg_read,
        api_fetch=api_fetch,
    )

    assert result == "fresh-value"
    spans = _request_with_retry_spans(span_recorder)
    assert len(spans) == 2
    for span in spans:
        data = span["data"]
        assert isinstance(data, dict)
        assert data.get("lml.discogs.method") == "get_release"
        assert data.get("lml.discogs.cache_state") == "miss"


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
    span_recorder, _ensure_skip_cache_clear, scenario, expected_state
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
        await _drive_request_with_retry()
        return "fresh-value"

    await fallthrough(
        label="get_release",
        pg_read=pg_read,
        api_fetch=api_fetch,
    )

    spans = _request_with_retry_spans(span_recorder)
    assert len(spans) == 2
    for span in spans:
        data = span["data"]
        assert isinstance(data, dict)
        assert data.get("lml.discogs.method") == "get_release"
        assert data.get("lml.discogs.cache_state") == expected_state


# ---------------------------------------------------------------------------
# 3. CancelledError reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_resets_on_cancelled_error(span_recorder, _ensure_skip_cache_clear):
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
async def test_context_resets_on_normal_return(span_recorder, _ensure_skip_cache_clear):
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
async def test_no_tag_when_called_outside_seam(span_recorder):
    """The health probe and a handful of legacy tests call
    `_request_with_retry` directly. Without a contextvar set, the spans
    must not blow up and must not carry the LML#537 tags."""
    assert _request_context_var.get() is None
    await _drive_request_with_retry()

    spans = _request_with_retry_spans(span_recorder)
    assert len(spans) == 2
    for span in spans:
        data = span["data"]
        assert isinstance(data, dict)
        assert "lml.discogs.method" not in data
        assert "lml.discogs.cache_state" not in data
