"""LML#940: process-global live-Discogs-request-attempt counter.

Mirrors ``tests/unit/test_event_loop_lag.py``'s coverage shape for the
process-global-primitive pattern this counter clones: it starts at a known
baseline, ``increment_discogs_live_requests_total()`` advances it, and
``reset_discogs_live_requests_total()`` returns it to that baseline for test
isolation.

The behavioral wiring -- an admitted OR breaker-shed live-Discogs attempt
increments it, a cache hit does not -- is covered where the counter is
actually driven, not here:

- ``tests/unit/test_discogs_breaker_request.py`` (the ``_request_with_retry``
  chokepoint, both the admitted and shed paths)
- ``tests/unit/test_discogs_service.py`` (a PG cache hit, which never reaches
  that chokepoint)
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate():
    from discogs.live_request_counter import reset_discogs_live_requests_total

    reset_discogs_live_requests_total()
    yield
    reset_discogs_live_requests_total()


class TestLiveRequestCounter:
    def test_starts_at_zero(self):
        from discogs.live_request_counter import get_discogs_live_requests_total

        assert get_discogs_live_requests_total() == 0

    def test_increment_advances_the_total(self):
        from discogs.live_request_counter import (
            get_discogs_live_requests_total,
            increment_discogs_live_requests_total,
        )

        increment_discogs_live_requests_total()
        assert get_discogs_live_requests_total() == 1

        increment_discogs_live_requests_total()
        assert get_discogs_live_requests_total() == 2

    def test_reset_returns_to_baseline(self):
        from discogs.live_request_counter import (
            get_discogs_live_requests_total,
            increment_discogs_live_requests_total,
            reset_discogs_live_requests_total,
        )

        increment_discogs_live_requests_total()
        increment_discogs_live_requests_total()

        reset_discogs_live_requests_total()

        assert get_discogs_live_requests_total() == 0
