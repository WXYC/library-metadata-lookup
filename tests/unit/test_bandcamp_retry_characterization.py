"""Characterization tests for ``BandcampClient._request_with_retry``'s 429
retry policy (LML#1040), pinned BEFORE converting it to the shared
``discogs.admission.retry_429`` loop.

These pin the exact numeric/attempt-count behavior that must survive the
conversion byte-for-byte:

- the delay progression on sustained 429s -- ``RETRY_BASE_DELAY * 2**attempt``,
  with NO jitter and NO cap (unlike Discogs's ``_compute_retry_delay``, which
  has both);
- ``Retry-After`` header handling -- a valid numeric value wins outright
  (bypassing the backoff formula entirely, even ignoring ``attempt``), an
  invalid/non-numeric value falls back to the backoff formula;
- the total attempt count on sustained 429s (``MAX_RETRIES + 1``);
- a non-429 request exception aborts immediately, with no retry and no sleep.

Pure unit: a pre-injected mock ``httpx.AsyncClient`` and a stubbed rate
limiter (``AsyncLimiter(1, 1)`` would otherwise pace real time between the
multiple attempts these tests drive) -- nothing leaves the process, nothing
sleeps in wall-clock time.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.bandcamp import MAX_RETRIES, RETRY_BASE_DELAY, BandcampClient

_URL = "https://bandcamp.com/api/fuzzysearch/2/app_autocomplete"


class _InstantLimiter:
    """A rate limiter stand-in with a no-op ``acquire`` -- mirrors the
    ``fake_limiter`` fixture pattern used throughout the Discogs retry test
    suites (``tests/unit/test_discogs_ratelimit.py`` et al.), so these tests
    exercise real attempt/delay counting without paying ``AsyncLimiter(1, 1)``'s
    real per-attempt pacing."""

    async def acquire(self) -> None:
        return None


def _response(status_code: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status_code, headers=headers or {}, request=httpx.Request("GET", _URL))


@pytest.fixture
def client() -> BandcampClient:
    c = BandcampClient()
    c._http = AsyncMock(spec=httpx.AsyncClient)
    c._rate_limiter = _InstantLimiter()  # type: ignore[assignment]
    return c


class TestDelayProgression:
    @pytest.mark.asyncio
    async def test_exponential_backoff_no_jitter_no_cap(self, client):
        """Sustained 429s (no ``Retry-After``) sleep ``RETRY_BASE_DELAY *
        2**attempt`` for each retried attempt, unmodified by jitter or a cap."""
        responses = [_response(429) for _ in range(MAX_RETRIES)] + [_response(200)]
        client._http.request = AsyncMock(side_effect=responses)

        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("clients.bandcamp.asyncio.sleep", side_effect=_fake_sleep):
            result = await client._request_with_retry("GET", _URL)

        assert result is not None
        assert result.status_code == 200
        assert sleep_calls == [RETRY_BASE_DELAY * (2**a) for a in range(MAX_RETRIES)]
        assert client._http.request.await_count == MAX_RETRIES + 1

    @pytest.mark.asyncio
    async def test_exhausts_after_max_retries_returns_none(self, client):
        """A 429 on every attempt exhausts at exactly ``MAX_RETRIES + 1``
        total attempts and gives up, returning ``None``."""
        client._http.request = AsyncMock(return_value=_response(429))

        with patch("clients.bandcamp.asyncio.sleep", new=AsyncMock()):
            result = await client._request_with_retry("GET", _URL)

        assert result is None
        assert client._http.request.await_count == MAX_RETRIES + 1


class TestRetryAfterHeader:
    @pytest.mark.asyncio
    async def test_valid_numeric_retry_after_wins_over_backoff(self, client):
        """A numeric ``Retry-After`` is honored verbatim -- it wins over the
        exponential-backoff formula entirely, not just as a floor/ceiling."""
        client._http.request = AsyncMock(
            side_effect=[_response(429, {"Retry-After": "2.5"}), _response(200)]
        )
        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("clients.bandcamp.asyncio.sleep", side_effect=_fake_sleep):
            result = await client._request_with_retry("GET", _URL)

        assert result is not None
        assert result.status_code == 200
        assert sleep_calls == [2.5]

    @pytest.mark.asyncio
    async def test_invalid_retry_after_falls_back_to_backoff(self, client):
        """A non-numeric ``Retry-After`` is ignored -- falls back to
        ``RETRY_BASE_DELAY * 2**attempt`` for that attempt."""
        client._http.request = AsyncMock(
            side_effect=[_response(429, {"Retry-After": "not-a-number"}), _response(200)]
        )
        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("clients.bandcamp.asyncio.sleep", side_effect=_fake_sleep):
            result = await client._request_with_retry("GET", _URL)

        assert result is not None
        assert sleep_calls == [RETRY_BASE_DELAY * (2**0)]

    @pytest.mark.asyncio
    async def test_missing_retry_after_uses_backoff(self, client):
        """No ``Retry-After`` header at all -- same fallback as an invalid one."""
        client._http.request = AsyncMock(side_effect=[_response(429), _response(200)])
        sleep_calls: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)

        with patch("clients.bandcamp.asyncio.sleep", side_effect=_fake_sleep):
            result = await client._request_with_retry("GET", _URL)

        assert result is not None
        assert sleep_calls == [RETRY_BASE_DELAY * (2**0)]


class TestRequestExceptionAbortsImmediately:
    @pytest.mark.asyncio
    async def test_request_exception_returns_none_without_retry(self, client):
        """A request-layer exception on ANY attempt aborts the whole call
        immediately -- it is not retried, unlike a 429."""
        client._http.request = AsyncMock(side_effect=httpx.ConnectError("boom"))

        with patch("clients.bandcamp.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            result = await client._request_with_retry("GET", _URL)

        assert result is None
        assert client._http.request.await_count == 1
        sleep_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_after_a_429_retry_still_aborts_without_further_retry(self, client):
        """A 429 retried once, then a request exception on the SECOND attempt,
        aborts there -- it does not retry past the exception."""
        client._http.request = AsyncMock(side_effect=[_response(429), httpx.ConnectError("boom")])

        with patch("clients.bandcamp.asyncio.sleep", new=AsyncMock()):
            result = await client._request_with_retry("GET", _URL)

        assert result is None
        assert client._http.request.await_count == 2


class TestSemaphoreReleasedBeforeSleep:
    @pytest.mark.asyncio
    async def test_permit_released_before_retry_sleep(self, client):
        """The semaphore permit must be free during the inter-attempt sleep,
        not held for its duration -- mirrors the Discogs LML#569 contract."""
        client._http.request = AsyncMock(side_effect=[_response(429), _response(200)])

        async def _fake_sleep(_delay: float) -> None:
            assert not client._semaphore.locked(), "permit still held entering the retry sleep"

        with patch("clients.bandcamp.asyncio.sleep", side_effect=_fake_sleep):
            result = await client._request_with_retry("GET", _URL)

        assert result is not None
        assert not client._semaphore.locked()
