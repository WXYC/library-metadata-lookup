"""Tests for ``discogs/fallthrough.py`` — the read-through cache seam (#393).

The seam concentrates the 3-tier (in-memory → PG → API + optional write-back)
ritual that ``discogs/service.py`` was hand-weaving across 5 cache-bearing
methods. See ``docs/plans/lml-393-discogs-cache-fallthrough.md``.

Tests cover, in order:

1. **PG hit / PG miss** — the basic read-through happy path.
2. **Write-back policy** — ``pg_write`` presence drives write-back; absence
   means "read-only by design" (the #393 drift fix).
3. **Negative-cache** — ``on_negative_hit`` factory, ``pg_negative_record``
   on empty API result, ``is_empty`` parameter validation.
4. **Cool-down (#324)** — narrow exception class set arms; expiry releases;
   unrelated exceptions do not arm.
5. **``should_skip_cache()`` bypass** — both PG read and negative-cache check
   honored as a single bypass.
6. **Tri-state PG read** — ``False`` from PG is treated as a hit, not a miss
   (the ``validate_track_on_release`` shape).
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import asyncpg.exceptions
import pytest

from discogs.fallthrough import (
    _COOL_DOWN_SECONDS,
    _reset_cool_down_for_tests,
    fallthrough,
)
from discogs.memory_cache import set_skip_cache


def _unused() -> str:
    """Placeholder factory for ``on_negative_hit`` when the test path doesn't
    exercise the negative-hit branch — keeps the seam's type contract happy
    without sprinkling ``lambda: "unused"  # noqa: E731`` at every call site."""
    return "unused"


@pytest.fixture(autouse=True)
def _reset_cool_down():
    """Cool-down is process-wide; reset between tests to keep them independent."""
    _reset_cool_down_for_tests()
    yield
    _reset_cool_down_for_tests()


# ---------------------------------------------------------------------------
# 1. Basic read-through
# ---------------------------------------------------------------------------


class TestBasicReadThrough:
    @pytest.mark.asyncio
    async def test_pg_hit_returns_cached_value_without_calling_api(self):
        pg_read = AsyncMock(return_value="cached-value")
        api_fetch = AsyncMock(return_value="fresh-value")

        result = await fallthrough(
            label="get_thing",
            pg_read=pg_read,
            api_fetch=api_fetch,
        )

        assert result == "cached-value"
        pg_read.assert_awaited_once()
        api_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pg_hit_logs_at_debug_not_info(self, caplog):
        """#798: the per-call ``Cache hit`` breadcrumb fires once per Discogs
        fallthrough on the lookup hot path, so it logs at DEBUG (matching the
        sibling ``Cache miss`` line) rather than flooding INFO under a
        saturation storm."""
        pg_read = AsyncMock(return_value="cached-value")

        with caplog.at_level(logging.DEBUG, logger="discogs.fallthrough"):
            await fallthrough(
                label="get_thing",
                pg_read=pg_read,
                api_fetch=AsyncMock(),
            )

        hits = [r for r in caplog.records if "Cache hit" in r.getMessage()]
        assert hits, "expected a cache-hit breadcrumb"
        assert all(r.levelno == logging.DEBUG for r in hits), [
            (r.levelname, r.getMessage()) for r in hits
        ]

    @pytest.mark.asyncio
    async def test_pg_miss_calls_api_returns_api_result(self):
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value="fresh-value")

        result = await fallthrough(
            label="get_thing",
            pg_read=pg_read,
            api_fetch=api_fetch,
        )

        assert result == "fresh-value"
        pg_read.assert_awaited_once()
        api_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_pg_leg_calls_api_directly(self):
        """``pg_read=None`` is an explicit opt-out — used for API-only methods."""
        api_fetch = AsyncMock(return_value="fresh-value")

        result = await fallthrough(
            label="get_thing",
            pg_read=None,
            api_fetch=api_fetch,
        )

        assert result == "fresh-value"
        api_fetch.assert_awaited_once()


# ---------------------------------------------------------------------------
# 2. Write-back policy — the #393 drift fix
# ---------------------------------------------------------------------------


class TestWriteBackPolicy:
    @pytest.mark.asyncio
    async def test_pg_miss_with_write_calls_write_back(self):
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value="fresh-value")
        pg_write = AsyncMock()

        await fallthrough(
            label="get_thing",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_write=pg_write,
        )

        pg_write.assert_awaited_once_with("fresh-value")

    @pytest.mark.asyncio
    async def test_pg_miss_without_write_does_not_call_anything_extra(self):
        """``pg_write=None`` (the default) is "read-only by design"."""
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value="fresh-value")

        # Should not raise, should not try to write anywhere.
        result = await fallthrough(
            label="search",
            pg_read=pg_read,
            api_fetch=api_fetch,
            # pg_write omitted — explicit read-only-by-design.
        )

        assert result == "fresh-value"

    @pytest.mark.asyncio
    async def test_pg_hit_skips_write_back(self):
        """Write-back fires only on API path — PG hits don't re-write themselves."""
        pg_read = AsyncMock(return_value="cached-value")
        api_fetch = AsyncMock(return_value="should-never-be-called")
        pg_write = AsyncMock()

        await fallthrough(
            label="get_thing",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_write=pg_write,
        )

        pg_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_api_returns_none_does_not_write_back(self):
        """A None API result (failure / not-found) is not cached."""
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value=None)
        pg_write = AsyncMock()

        await fallthrough(
            label="get_thing",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_write=pg_write,
        )

        pg_write.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_write_back_exception_is_swallowed(self):
        """A write-back failure must not propagate — best-effort by design."""
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value="fresh-value")
        pg_write = AsyncMock(side_effect=RuntimeError("pg write blew up"))

        # Must not raise — caller still gets the API result.
        result = await fallthrough(
            label="get_thing",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_write=pg_write,
        )

        assert result == "fresh-value"


# ---------------------------------------------------------------------------
# 3. Negative-cache (the search_releases_by_track shape)
# ---------------------------------------------------------------------------


class TestNegativeCache:
    @pytest.mark.asyncio
    async def test_negative_hit_returns_factory_value_without_api(self):
        pg_read = AsyncMock(return_value=None)
        pg_negative_check = AsyncMock(return_value=True)
        api_fetch = AsyncMock()
        sentinel = object()
        on_negative_hit = lambda: sentinel  # noqa: E731

        result = await fallthrough(
            label="search_releases_by_track",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_negative_check=pg_negative_check,
            on_negative_hit=on_negative_hit,
            pg_negative_record=AsyncMock(),
            is_empty=lambda _: False,
        )

        assert result is sentinel
        api_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_miss_then_empty_api_writes_negative(self):
        pg_read = AsyncMock(return_value=None)
        pg_negative_check = AsyncMock(return_value=False)
        api_fetch = AsyncMock(return_value=[])
        pg_negative_record = AsyncMock()
        on_negative_hit = _unused

        await fallthrough(
            label="search_releases_by_track",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_negative_check=pg_negative_check,
            on_negative_hit=on_negative_hit,
            pg_negative_record=pg_negative_record,
            is_empty=lambda v: not v,
        )

        pg_negative_record.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_negative_miss_then_nonempty_api_skips_negative_write(self):
        pg_read = AsyncMock(return_value=None)
        pg_negative_check = AsyncMock(return_value=False)
        api_fetch = AsyncMock(return_value=["item"])
        pg_negative_record = AsyncMock()
        on_negative_hit = _unused

        await fallthrough(
            label="search_releases_by_track",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_negative_check=pg_negative_check,
            on_negative_hit=on_negative_hit,
            pg_negative_record=pg_negative_record,
            is_empty=lambda v: not v,
        )

        pg_negative_record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_negative_check_requires_on_negative_hit(self):
        """Without ``on_negative_hit`` the seam can't construct a return shape."""
        with pytest.raises(ValueError, match="on_negative_hit"):
            await fallthrough(
                label="search_releases_by_track",
                pg_read=AsyncMock(return_value=None),
                api_fetch=AsyncMock(),
                pg_negative_check=AsyncMock(return_value=False),
                # on_negative_hit omitted — should raise.
            )

    @pytest.mark.asyncio
    async def test_negative_record_requires_is_empty(self):
        """Without ``is_empty`` the seam doesn't know when to record a negative.

        The default ``not r`` is unsafe for Pydantic models (always truthy).
        """
        with pytest.raises(ValueError, match="is_empty"):
            await fallthrough(
                label="search_releases_by_track",
                pg_read=AsyncMock(return_value=None),
                api_fetch=AsyncMock(return_value=[]),
                pg_negative_record=AsyncMock(),
                # is_empty omitted — should raise.
            )

    @pytest.mark.asyncio
    async def test_negative_record_failure_is_swallowed(self):
        """Negative-cache writes are best-effort."""
        pg_read = AsyncMock(return_value=None)
        pg_negative_check = AsyncMock(return_value=False)
        api_fetch = AsyncMock(return_value=[])
        pg_negative_record = AsyncMock(side_effect=RuntimeError("write boom"))

        # Must not raise.
        await fallthrough(
            label="search_releases_by_track",
            pg_read=pg_read,
            api_fetch=api_fetch,
            pg_negative_check=pg_negative_check,
            on_negative_hit=_unused,
            pg_negative_record=pg_negative_record,
            is_empty=lambda v: not v,
        )


# ---------------------------------------------------------------------------
# 4. Cool-down (#324)
# ---------------------------------------------------------------------------


class TestCoolDown:
    @pytest.mark.asyncio
    async def test_pg_postgres_connection_error_arms_cool_down(self, monkeypatch):
        """The narrow asyncpg error class that signals "DB unreachable"."""
        # First call: PG raises, falls back to API, arms cool-down.
        pg_read_a = AsyncMock(side_effect=asyncpg.exceptions.PostgresConnectionError("down"))
        api_fetch_a = AsyncMock(return_value="api-1")
        result_a = await fallthrough(
            label="get_thing",
            pg_read=pg_read_a,
            api_fetch=api_fetch_a,
        )
        assert result_a == "api-1"

        # Second call: cool-down is active; PG read must NOT be invoked.
        pg_read_b = AsyncMock(return_value="from-pg")
        api_fetch_b = AsyncMock(return_value="api-2")
        result_b = await fallthrough(
            label="get_thing",
            pg_read=pg_read_b,
            api_fetch=api_fetch_b,
        )
        assert result_b == "api-2"
        pg_read_b.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pg_cannot_connect_now_arms_cool_down(self):
        pg_read = AsyncMock(side_effect=asyncpg.exceptions.CannotConnectNowError("starting"))
        api_fetch = AsyncMock(return_value="api-result")

        await fallthrough(label="get_thing", pg_read=pg_read, api_fetch=api_fetch)

        # Cool-down should be armed — second call skips PG.
        next_pg = AsyncMock(return_value="should-not-be-called")
        await fallthrough(
            label="get_thing", pg_read=next_pg, api_fetch=AsyncMock(return_value="ok")
        )
        next_pg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pg_interface_error_arms_cool_down(self):
        pg_read = AsyncMock(side_effect=asyncpg.exceptions.InterfaceError("pool exhausted"))
        api_fetch = AsyncMock(return_value="api-result")

        await fallthrough(label="get_thing", pg_read=pg_read, api_fetch=api_fetch)

        next_pg = AsyncMock(return_value="should-not-be-called")
        await fallthrough(
            label="get_thing", pg_read=next_pg, api_fetch=AsyncMock(return_value="ok")
        )
        next_pg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pg_undefined_table_arms_cool_down(self):
        """The "relation does not exist" window during a dedup-swap."""
        pg_read = AsyncMock(
            side_effect=asyncpg.exceptions.UndefinedTableError("relation does not exist")
        )
        api_fetch = AsyncMock(return_value="api-result")

        await fallthrough(label="get_thing", pg_read=pg_read, api_fetch=api_fetch)

        next_pg = AsyncMock(return_value="should-not-be-called")
        await fallthrough(
            label="get_thing", pg_read=next_pg, api_fetch=AsyncMock(return_value="ok")
        )
        next_pg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_unavailable_error_arms_cool_down(self):
        """The locally-raised exception from ``cache_service`` methods."""
        from discogs.cache_service import CacheUnavailableError

        pg_read = AsyncMock(side_effect=CacheUnavailableError("cache down"))
        api_fetch = AsyncMock(return_value="api-result")

        await fallthrough(label="get_thing", pg_read=pg_read, api_fetch=api_fetch)

        next_pg = AsyncMock(return_value="should-not-be-called")
        await fallthrough(
            label="get_thing", pg_read=next_pg, api_fetch=AsyncMock(return_value="ok")
        )
        next_pg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_asyncio_timeout_does_not_arm_cool_down(self):
        """``asyncio.TimeoutError`` has its own semantics — don't arm cool-down."""

        pg_read_a = AsyncMock(side_effect=TimeoutError())
        api_fetch_a = AsyncMock(return_value="api-1")
        await fallthrough(label="get_thing", pg_read=pg_read_a, api_fetch=api_fetch_a)

        # Cool-down NOT armed — second call's PG read should be invoked.
        pg_read_b = AsyncMock(return_value="from-pg")
        api_fetch_b = AsyncMock(return_value="should-not-be-called")
        result_b = await fallthrough(label="get_thing", pg_read=pg_read_b, api_fetch=api_fetch_b)
        assert result_b == "from-pg"
        pg_read_b.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unrelated_exception_does_not_arm_cool_down(self):
        """A bare ``Exception`` (programming error) propagates; cool-down stays off."""
        pg_read_a = AsyncMock(side_effect=ValueError("programming bug"))
        api_fetch_a = AsyncMock(return_value="api-1")

        # The fallthrough swallows the cache leg's exception per the existing
        # pattern in service.py; we assert it does NOT arm cool-down.
        await fallthrough(label="get_thing", pg_read=pg_read_a, api_fetch=api_fetch_a)

        pg_read_b = AsyncMock(return_value="from-pg")
        result_b = await fallthrough(
            label="get_thing",
            pg_read=pg_read_b,
            api_fetch=AsyncMock(return_value="should-not-be-called"),
        )
        assert result_b == "from-pg"
        pg_read_b.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cool_down_expires_after_window(self, monkeypatch):
        """After ``_COOL_DOWN_SECONDS`` elapses, PG reads resume."""
        from discogs import fallthrough as fallthrough_module

        clock = [1000.0]

        def fake_monotonic():
            return clock[0]

        monkeypatch.setattr(fallthrough_module.time, "monotonic", fake_monotonic)

        # Arm the cool-down.
        await fallthrough(
            label="get_thing",
            pg_read=AsyncMock(side_effect=asyncpg.exceptions.PostgresConnectionError("down")),
            api_fetch=AsyncMock(return_value="api-result"),
        )

        # Advance the clock past the window.
        clock[0] += _COOL_DOWN_SECONDS + 1.0

        # PG read should now run again.
        pg_read = AsyncMock(return_value="from-pg")
        result = await fallthrough(
            label="get_thing",
            pg_read=pg_read,
            api_fetch=AsyncMock(return_value="should-not-be-called"),
        )
        assert result == "from-pg"
        pg_read.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. ``should_skip_cache()`` bypass
# ---------------------------------------------------------------------------


class TestSkipCacheBypass:
    @pytest.mark.asyncio
    async def test_skip_cache_bypasses_pg_read_and_negative_check(self):
        set_skip_cache(True)
        try:
            pg_read = AsyncMock(return_value="from-pg")
            pg_negative_check = AsyncMock(return_value=True)
            api_fetch = AsyncMock(return_value="from-api")

            result = await fallthrough(
                label="search_releases_by_track",
                pg_read=pg_read,
                api_fetch=api_fetch,
                pg_negative_check=pg_negative_check,
                on_negative_hit=_unused,
                pg_negative_record=AsyncMock(),
                is_empty=lambda v: not v,
            )

            assert result == "from-api"
            pg_read.assert_not_awaited()
            pg_negative_check.assert_not_awaited()
        finally:
            set_skip_cache(False)

    @pytest.mark.asyncio
    async def test_skip_cache_also_skips_write_back(self):
        """When the request opts out of caches, we don't write back either —
        write-back would re-pollute the cache the caller asked us to bypass."""
        set_skip_cache(True)
        try:
            pg_write = AsyncMock()
            await fallthrough(
                label="get_thing",
                pg_read=AsyncMock(return_value=None),
                api_fetch=AsyncMock(return_value="from-api"),
                pg_write=pg_write,
            )
            pg_write.assert_not_awaited()
        finally:
            set_skip_cache(False)


# ---------------------------------------------------------------------------
# 6. Tri-state PG read (the validate_track_on_release shape)
# ---------------------------------------------------------------------------


class TestTriStateRead:
    @pytest.mark.asyncio
    async def test_pg_returns_false_is_treated_as_hit(self):
        """``validate_track_on_release`` returns ``True | False | None``; the
        default ``is_pg_hit`` (``v is not None``) makes False a hit, not a miss."""
        pg_read = AsyncMock(return_value=False)
        api_fetch = AsyncMock(return_value=True)

        result = await fallthrough(
            label="validate_track_on_release",
            pg_read=pg_read,
            api_fetch=api_fetch,
        )

        assert result is False
        api_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pg_returns_true_is_treated_as_hit(self):
        pg_read = AsyncMock(return_value=True)
        api_fetch = AsyncMock(return_value=False)

        result = await fallthrough(
            label="validate_track_on_release",
            pg_read=pg_read,
            api_fetch=api_fetch,
        )

        assert result is True
        api_fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pg_returns_none_falls_through_to_api(self):
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value=True)

        result = await fallthrough(
            label="validate_track_on_release",
            pg_read=pg_read,
            api_fetch=api_fetch,
        )

        assert result is True
        api_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_is_pg_hit_lambda(self):
        """Callers with non-default sentinels can override ``is_pg_hit``."""
        # PG returns an empty list — caller wants empty to count as "miss",
        # not "hit with empty value".
        pg_read = AsyncMock(return_value=[])
        api_fetch = AsyncMock(return_value=["a"])

        result = await fallthrough(
            label="custom",
            pg_read=pg_read,
            api_fetch=api_fetch,
            is_pg_hit=lambda v: bool(v),  # only non-empty counts as hit
        )

        assert result == ["a"]
        api_fetch.assert_awaited_once()


# ---------------------------------------------------------------------------
# 7. Sentry span decomposition
# ---------------------------------------------------------------------------


class TestSentrySpanInstrumentation:
    """Pins the seam emitting per-leg child spans so a cache-bearing route's
    auto-FastApi transaction decomposes into ``cache.get`` / ``cache.put`` legs
    in the trace explorer. The API leg is covered by the httpx auto-instrumentation
    in ``wxyc_fastapi``."""

    @staticmethod
    def _install_span_recorder(
        monkeypatch,
    ) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
        """Replace ``sentry_sdk.start_span`` with a recorder.

        Returns ``(spans_started, span_data)`` where ``spans_started`` is a list
        of ``(op, name)`` tuples and ``span_data`` is the list of mappings the
        seam called ``set_data`` with on each span.
        """
        from unittest.mock import MagicMock

        spans_started: list[tuple[str, str]] = []
        span_data: list[dict[str, object]] = []

        def fake_start_span(*, op: str, name: str, **_kwargs):
            spans_started.append((op, name))
            data: dict[str, object] = {}
            span_data.append(data)
            span = MagicMock()
            span.set_data = lambda k, v: data.__setitem__(k, v)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=span)
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        monkeypatch.setattr("discogs.fallthrough.sentry_sdk.start_span", fake_start_span)
        return spans_started, span_data

    @pytest.mark.asyncio
    async def test_pg_read_emits_cache_get_span_with_hit_data(self, monkeypatch):
        spans_started, span_data = self._install_span_recorder(monkeypatch)
        pg_read = AsyncMock(return_value="cached")
        api_fetch = AsyncMock()

        await fallthrough(label="get_release", pg_read=pg_read, api_fetch=api_fetch)

        assert ("cache.get", "l2:get_release") in spans_started
        # `cache.hit=True` because pg_read returned non-None (default predicate).
        idx = spans_started.index(("cache.get", "l2:get_release"))
        assert span_data[idx].get("cache.hit") is True

    @pytest.mark.asyncio
    async def test_pg_read_miss_records_cache_hit_false(self, monkeypatch):
        spans_started, span_data = self._install_span_recorder(monkeypatch)
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value="fresh")

        await fallthrough(label="get_release", pg_read=pg_read, api_fetch=api_fetch)

        idx = spans_started.index(("cache.get", "l2:get_release"))
        assert span_data[idx].get("cache.hit") is False

    @pytest.mark.asyncio
    async def test_pg_write_emits_cache_put_span(self, monkeypatch):
        spans_started, _ = self._install_span_recorder(monkeypatch)
        pg_read = AsyncMock(return_value=None)
        api_fetch = AsyncMock(return_value="fresh")
        pg_write = AsyncMock()

        await fallthrough(
            label="get_release", pg_read=pg_read, api_fetch=api_fetch, pg_write=pg_write
        )

        assert ("cache.put", "l2:get_release") in spans_started

    @pytest.mark.asyncio
    async def test_negative_cache_check_emits_cache_get_span_with_hit_data(self, monkeypatch):
        spans_started, span_data = self._install_span_recorder(monkeypatch)
        pg_negative_check = AsyncMock(return_value=False)
        api_fetch = AsyncMock(return_value="fresh")

        await fallthrough(
            label="search",
            pg_read=None,
            api_fetch=api_fetch,
            pg_negative_check=pg_negative_check,
            on_negative_hit=_unused,
        )

        assert ("cache.get", "l2_negative:search") in spans_started
        idx = spans_started.index(("cache.get", "l2_negative:search"))
        assert span_data[idx].get("cache.hit") is False

    @pytest.mark.asyncio
    async def test_negative_cache_write_emits_cache_put_span(self, monkeypatch):
        """Symmetry with the positive write-back: the negative-record path also
        gets a span so trace decomposition shows both write legs."""
        spans_started, _ = self._install_span_recorder(monkeypatch)
        pg_negative_check = AsyncMock(return_value=False)
        pg_negative_record = AsyncMock()
        api_fetch = AsyncMock(return_value="empty-api-result")

        await fallthrough(
            label="search",
            pg_read=None,
            api_fetch=api_fetch,
            pg_negative_check=pg_negative_check,
            on_negative_hit=_unused,
            pg_negative_record=pg_negative_record,
            is_empty=lambda v: True,
        )

        assert ("cache.put", "l2_negative:search") in spans_started

    @pytest.mark.asyncio
    async def test_pg_read_span_omitted_when_no_pg_leg(self, monkeypatch):
        """``pg_read=None`` short-circuits the L2 read — no span should be emitted."""
        spans_started, _ = self._install_span_recorder(monkeypatch)
        api_fetch = AsyncMock(return_value="fresh")

        await fallthrough(label="search", pg_read=None, api_fetch=api_fetch)

        assert not any(op == "cache.get" and name == "l2:search" for op, name in spans_started)
