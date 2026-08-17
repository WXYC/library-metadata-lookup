"""Shared operator-rate limiter construction for the drain scripts (LML#1204 item 6).

The fractional-rate fix (LML#1192 review round 3, P0-2) was trapped as a
private function of ``scripts/warm_wikipedia_bios.py`` while
``scripts/ytm_coverage_drain.py`` still built the crashing direct tuple form
(``rate_limit=(args.rate, 1)``): ``aiolimiter`` sets ``max_rate`` (the
bucket's total capacity) from the first tuple element and requires
``acquire(1) <= max_rate``, so any operator rate below 1.0 — a deliberate
throttle-down under 429 pressure, e.g. ``--rate 0.5`` — raised ``ValueError``
on the very first acquisition, before any HTTP attempt. Capacity 1 refilled
over ``1 / rate`` seconds is correct for every positive rate, fractional or
not: 3.0 rps -> refill every ~0.33s; 0.5 rps -> refill every 2s. This module
is the one home for that construction; both drain scripts route their
operator ``--rate`` flags through it.
"""

from __future__ import annotations

from aiolimiter import AsyncLimiter

_RATE_FLOOR = 0.01
"""Fallback for a non-positive operator rate (a typo) — throttle hard rather
than divide by zero or go negative, matching the pre-promotion private copy."""


def per_second_rate_limit(rate_per_second: float) -> tuple[float, float]:
    """The ``(max_rate, time_period)`` pair correct for ANY positive rate.

    For call sites that hand a tuple to a client constructor
    (``clients.streaming.youtube_music.YouTubeMusicClient(rate_limit=...)``)
    rather than holding an ``AsyncLimiter`` themselves.
    """
    effective_rate = rate_per_second if rate_per_second > 0 else _RATE_FLOOR
    return (1, 1 / effective_rate)


def build_rate_limiter(rate_per_second: float) -> AsyncLimiter:
    """Construct an ``AsyncLimiter`` honoring ``rate_per_second``, correct for
    any positive rate — see the module docstring for the fractional-rate trap
    this avoids."""
    return AsyncLimiter(*per_second_rate_limit(rate_per_second))
