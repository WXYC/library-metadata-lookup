"""Unit tests for ``scripts/_lib/ratelimit.py`` — the shared operator-rate
limiter construction (LML#1204 item 6).

The fractional-rate fix (LML#1192 review round 3, P0-2) was trapped as a
private function of ``scripts/warm_wikipedia_bios.py``: ``AsyncLimiter(rate, 1)``
sets ``max_rate`` (bucket capacity) to the rate itself, but ``aiolimiter``
requires ``acquire(1) <= max_rate`` — so any operator rate below 1.0 (a
deliberate throttle-down under 429 pressure, e.g. ``--rate 0.5``) raised
``ValueError`` on the very first acquisition, before any HTTP attempt.
Capacity 1 refilled over ``1 / rate`` seconds is correct for every positive
rate; this is now the one shared home both drain scripts route their
operator ``--rate`` flags through.
"""

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
