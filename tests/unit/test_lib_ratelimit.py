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


class TestFloatExtremes:
    """LML#1204 review (execution-confirmed): ``argparse type=float`` accepts
    ``inf``/``1e999`` — ``inf > 0`` passed the old floor check, then
    ``1/inf == 0.0`` made ``AsyncLimiter`` raise ``ZeroDivisionError`` at
    construction; and a denormal-tiny positive rate (``5e-324``) slipped
    past the ``> 0`` floor, overflowed ``1/rate`` to ``inf``, and produced a
    limiter that never refills — the run HANGS after one acquisition."""

    @pytest.mark.parametrize("rate", [float("inf"), float("-inf"), float("nan"), float("1e999")])
    def test_non_finite_rates_are_rejected_with_a_clear_error(self, rate):
        with pytest.raises(ValueError, match="--rate"):
            per_second_rate_limit(rate)

    @pytest.mark.parametrize("rate", [5e-324, 1e-300, 0.001])
    def test_sub_floor_positive_rates_are_floored_not_overflowed(self, rate):
        # The floor applies to ALL rates below it, not just <= 0 — a
        # denormal-tiny rate must never mint an infinite refill period.
        assert per_second_rate_limit(rate) == (1, 1 / 0.01)

    @pytest.mark.asyncio
    async def test_a_denormal_rate_builds_an_acquirable_limiter(self):
        limiter = build_rate_limiter(5e-324)
        assert limiter.time_period != float("inf")
        await limiter.acquire()  # must not raise (and must not hang forever)
