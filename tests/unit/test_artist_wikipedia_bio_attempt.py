"""Unit tests for the Wikipedia bio program's write-nothing attempt record (LML#1192 review round 4, P0-8).

PG interaction is mocked with ``AsyncMock(spec=PgSource)``/a fake cursor,
mirroring ``tests/unit/test_artist_wikipedia_bio.py`` and
``tests/unit/test_discogs_rate_bucket.py``. The real UPSERT/JOIN behavior is
pinned by the ``pg``-marked integration test.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from entity.artist_wikipedia_bio_attempt import (
    record_artist_wikipedia_bio_attempt,
    set_up_artist_wikipedia_bio_attempt_schema,
)
from entity.sources import PgSource

_ARTIST_ID = 42


@pytest.mark.asyncio
class TestRecordArtistWikipediaBioAttempt:
    async def test_upserts_the_artist_id_and_outcome(self):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await record_artist_wikipedia_bio_attempt(
            pg, discogs_artist_id=_ARTIST_ID, outcome="fetch_error"
        )

        args = pg.execute.await_args.args
        _sql, artist_id, outcome = args
        assert artist_id == _ARTIST_ID
        assert outcome == "fetch_error"

    async def test_write_failure_is_swallowed(self):
        # Mirrors set_cached_artist_wikipedia_bio and every other write in
        # this module's sibling: a best-effort bookkeeping write must never
        # take down a multi-hour drain session.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        await record_artist_wikipedia_bio_attempt(
            pg, discogs_artist_id=_ARTIST_ID, outcome="fetch_error"
        )


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return None


class _FakeConnection:
    def __init__(self, source: _FakePgSource) -> None:
        self._source = source

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, sql: str, *args: object) -> str:
        self._source.executed.append(sql)
        return "CREATE TABLE"


class _FakePgSource:
    """Minimal fake mirroring tests/unit/test_artist_wikipedia_bio.py's
    _FakePgSource -- records every statement issued."""

    def __init__(self) -> None:
        self.executed: list[str] = []
        self.acquire_count = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquire_count += 1
        yield _FakeConnection(self)


@pytest.mark.asyncio
class TestSetUpArtistWikipediaBioAttemptSchema:
    async def test_creates_schema_then_table(self):
        pg = _FakePgSource()

        await set_up_artist_wikipedia_bio_attempt_schema(pg)

        assert any("CREATE SCHEMA" in s for s in pg.executed)
        assert any("CREATE TABLE" in s and "artist_wikipedia_bio_attempt" in s for s in pg.executed)


@pytest.mark.asyncio
class TestAdoptsCacheToolkit:
    """LML#1204 item 4: the swallow-and-log write funnels through
    ``entity.cache_toolkit.swallowing_execute`` (WARNING severity via its
    ``level`` param) instead of a hand-rolled try/except — the exact idiom
    that toolkit exists to own."""

    async def test_write_routes_through_swallowing_execute_at_warning(self):
        import logging
        from unittest.mock import patch

        from entity import artist_wikipedia_bio_attempt as module

        pg = AsyncMock(spec=PgSource)
        with patch.object(module, "swallowing_execute", new_callable=AsyncMock) as mock_execute:
            await record_artist_wikipedia_bio_attempt(
                pg, discogs_artist_id=_ARTIST_ID, outcome="fetch_error"
            )

        mock_execute.assert_awaited_once()
        args = mock_execute.await_args.args
        kwargs = mock_execute.await_args.kwargs
        assert args[0] is pg
        assert args[2] == _ARTIST_ID
        assert args[3] == "fetch_error"
        assert kwargs["logger"] is module.logger
        assert kwargs["level"] == logging.WARNING

    async def test_swallowed_failure_still_logs_the_original_warning_shape(self, caplog):
        import logging

        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        with caplog.at_level(logging.WARNING, logger="entity.artist_wikipedia_bio_attempt"):
            await record_artist_wikipedia_bio_attempt(
                pg, discogs_artist_id=_ARTIST_ID, outcome="fetch_error"
            )

        record = next(r for r in caplog.records if r.name == "entity.artist_wikipedia_bio_attempt")
        assert record.levelno == logging.WARNING
        assert "failed to record a wikipedia bio drain attempt" in record.getMessage()
        assert f"artist_id={_ARTIST_ID}" in record.getMessage()
        assert "outcome=fetch_error" in record.getMessage()
        assert record.exc_info
