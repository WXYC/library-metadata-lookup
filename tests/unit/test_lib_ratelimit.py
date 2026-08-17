"""Unit tests for ``scripts/_lib/ratelimit.py`` — the shared operator-rate
limiter construction (LML#1204 item 6; the full fractional-rate trap story
lives in that module's docstring)."""

from __future__ import annotations

import pytest

from scripts._lib.ratelimit import build_rate_limiter, per_second_rate_limit


class TestPerSecondRateLimit:
    @pytest.mark.parametrize("rate", [3.0, 1.0, 0.5, 0.01])
    def test_capacity_one_refilled_over_the_inverse_rate(self, rate):
        assert per_second_rate_limit(rate) == (1, 1 / rate)

    @pytest.mark.parametrize("rate", [0.0, -5.0])
    def test_zero_or_negative_rate_falls_back_to_a_floor(self, rate):
        # An operator typo must not divide by zero or go negative — same
        # 0.01 floor the pre-promotion private copy applied.
        assert per_second_rate_limit(rate) == (1, 1 / 0.01)


@pytest.mark.asyncio
class TestBuildRateLimiter:
    @pytest.mark.parametrize("rate", [3.0, 1.0, 0.9, 0.5, 0.1, 0.01])
    async def test_any_positive_rate_can_actually_be_acquired(self, rate):
        limiter = build_rate_limiter(rate)
        await limiter.acquire()  # must not raise, for any positive rate

    async def test_zero_or_negative_rate_falls_back_to_a_floor_not_a_crash(self):
        for rate in (0.0, -5.0):
            limiter = build_rate_limiter(rate)
            await limiter.acquire()
