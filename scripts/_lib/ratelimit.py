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

import math

from aiolimiter import AsyncLimiter

_RATE_FLOOR = 0.01
"""Floor for ANY operator rate below it, not just non-positive typos — a
bare ``> 0`` check is not enough (see :func:`per_second_rate_limit`)."""


def per_second_rate_limit(rate_per_second: float) -> tuple[float, float]:
    """The ``(max_rate, time_period)`` pair correct for ANY finite rate.

    For call sites that hand a tuple to a client constructor
    (``clients.streaming.youtube_music.YouTubeMusicClient(rate_limit=...)``)
    rather than holding an ``AsyncLimiter`` themselves.

    ``argparse type=float`` happily parses ``inf``, ``1e999``, ``nan``, and
    denormals, so both extremes are handled here rather than trusted to the
    caller:

    * non-finite is rejected outright — ``1 / inf == 0.0`` makes
      ``AsyncLimiter`` raise ``ZeroDivisionError`` at construction, and there
      is no sensible throttle to substitute for "infinitely fast";
    * anything below :data:`_RATE_FLOOR` is floored, which is what makes a
      denormal-tiny positive rate (e.g. ``5e-324``, which passes ``> 0`` but
      overflows ``1 / rate`` to ``inf``) throttle hard instead of minting a
      limiter that never refills — the run would hang after one acquisition.
    """
    if not math.isfinite(rate_per_second):
        raise ValueError(f"operator rate (--rate) must be a finite number, got {rate_per_second!r}")
    return (1, 1 / max(rate_per_second, _RATE_FLOOR))


def build_rate_limiter(rate_per_second: float) -> AsyncLimiter:
    """Construct an ``AsyncLimiter`` honoring ``rate_per_second``, correct for
    any finite rate (see the module docstring for the fractional-rate trap
    this avoids; non-finite rates raise per :func:`per_second_rate_limit`)."""
    return AsyncLimiter(*per_second_rate_limit(rate_per_second))
