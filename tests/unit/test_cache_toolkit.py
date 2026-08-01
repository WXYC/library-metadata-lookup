"""Unit tests for ``entity.cache_toolkit`` (LML#1034).

``swallowing_fetch`` / ``swallowing_execute`` wrap the "normalize keys ->
``pg.fetchone``/``pg.fetchall``/``pg.execute`` -> ``except Exception: log +
return a miss shape``" skeleton shared by the ``lml_cache.*`` store modules'
nine get/set bodies (``entity/streaming_url_cache.py``,
``entity/track_streaming_url_cache.py``, ``entity/release_resolution_cache.py``,
``entity/compilation_track_location.py``, ``entity/library_release_override.py``,
``entity/api_keys.py``'s ``mark_key_used``). These tests pin the toolkit's own
contract in isolation -- hit path, miss-shape return on exception, and
log-label emission via the CALLER's own logger (not one owned by this
module), which is load-bearing for ``entity.api_keys``'s existing
``test_swallows_pg_errors`` (it asserts ``record.name == "entity.api_keys"``).
The existing per-module test suites continue to pin each call site's exact
SQL bind shape and message text unchanged.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from entity.cache_toolkit import (
    DEFAULT_MISS_TTL,
    CachedValue,
    swallowing_execute,
    swallowing_fetch,
)
from entity.sources import PgSource

# A logger name distinct from any real module, so these tests can assert on
# ``record.name`` without colliding with a production logger.
_LOGGER = logging.getLogger("entity.cache_toolkit_test_caller")


@pytest.mark.asyncio
class TestSwallowingFetch:
    async def test_returns_row_on_hit_fetchone(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": "https://music.apple.com/us/album/x/1"})

        result = await swallowing_fetch(
            pg,
            "SELECT url FROM t WHERE artist = $1",
            "Stereolab",
            miss=None,
            logger=_LOGGER,
            log_label="t get failed for %s",
        )

        assert result == {"url": "https://music.apple.com/us/album/x/1"}
        pg.fetchone.assert_awaited_once_with("SELECT url FROM t WHERE artist = $1", "Stereolab")

    async def test_returns_rows_on_hit_fetchall(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[{"artist": "Juana Molina"}])

        result = await swallowing_fetch(
            pg,
            "SELECT artist FROM t",
            miss=[],
            logger=_LOGGER,
            log_label="t probe failed",
            many=True,
        )

        assert result == [{"artist": "Juana Molina"}]
        pg.fetchall.assert_awaited_once_with("SELECT artist FROM t")
        pg.fetchone.assert_not_awaited()

    async def test_returns_miss_shape_on_exception(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("pg down"))
        sentinel_miss = object()

        result = await swallowing_fetch(
            pg,
            "SELECT 1",
            miss=sentinel_miss,
            logger=_LOGGER,
            log_label="t get failed",
        )

        assert result is sentinel_miss

    async def test_logs_via_the_passed_logger_with_label_and_args(self, caplog):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("pg down"))

        with caplog.at_level(logging.ERROR, logger=_LOGGER.name):
            await swallowing_fetch(
                pg,
                "SELECT 1",
                "Jessica Pratt",
                "On Your Own Love Again",
                miss=None,
                logger=_LOGGER,
                log_label="streaming_url_cache get failed for %s / %s",
                log_args=("Jessica Pratt", "On Your Own Love Again"),
            )

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.name == _LOGGER.name
        assert record.levelno == logging.ERROR
        assert record.getMessage() == (
            "streaming_url_cache get failed for Jessica Pratt / On Your Own Love Again"
        )
        assert record.exc_info is not None

    async def test_no_log_emitted_on_success(self, caplog):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        with caplog.at_level(logging.ERROR, logger=_LOGGER.name):
            result = await swallowing_fetch(
                pg,
                "SELECT 1",
                miss=None,
                logger=_LOGGER,
                log_label="t get failed",
            )

        assert result is None
        assert caplog.records == []


@pytest.mark.asyncio
class TestSwallowingExecute:
    async def test_executes_and_returns_none_on_success(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="UPDATE 1")

        result = await swallowing_execute(
            pg,
            "UPDATE t SET x = $1 WHERE y = $2",
            "a",
            "b",
            logger=_LOGGER,
            log_label="t set failed",
        )

        assert result is None
        pg.execute.assert_awaited_once_with("UPDATE t SET x = $1 WHERE y = $2", "a", "b")

    async def test_swallows_exception_and_logs_label_with_args(self, caplog):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("pg down"))

        with caplog.at_level(logging.ERROR, logger=_LOGGER.name):
            result = await swallowing_execute(
                pg,
                "UPDATE t SET x = $1",
                "Juana Molina",
                logger=_LOGGER,
                log_label="t set failed for %s",
                log_args=("Juana Molina",),
            )  # must not raise

        assert result is None
        assert len(caplog.records) == 1
        assert caplog.records[0].getMessage() == "t set failed for Juana Molina"
        assert caplog.records[0].name == _LOGGER.name

    async def test_no_args_message_renders_verbatim(self, caplog):
        # Mirrors api_keys.mark_key_used's message, which has no interpolation.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("pg down"))

        with caplog.at_level(logging.ERROR, logger=_LOGGER.name):
            await swallowing_execute(
                pg,
                "UPDATE t SET x = $1",
                "k",
                logger=_LOGGER,
                log_label="api_keys last_used_at touch failed",
            )

        assert caplog.records[0].getMessage() == "api_keys last_used_at touch failed"


def test_default_miss_ttl_is_seven_days():
    assert DEFAULT_MISS_TTL == timedelta(days=7)


class TestCachedValue:
    def test_hit_shape(self):
        cached = CachedValue(value="https://music.apple.com/us/album/x/1", was_present=True)
        assert cached.value == "https://music.apple.com/us/album/x/1"
        assert cached.was_present is True

    def test_fresh_miss_shape(self):
        cached: CachedValue[str] = CachedValue(value=None, was_present=True)
        assert cached.value is None
        assert cached.was_present is True

    def test_absent_or_stale_shape(self):
        cached: CachedValue[str] = CachedValue(value=None, was_present=False)
        assert cached.value is None
        assert cached.was_present is False

    def test_is_frozen(self):
        cached = CachedValue(value=1, was_present=True)
        with pytest.raises(AttributeError):
            cached.value = 2  # type: ignore[misc]
