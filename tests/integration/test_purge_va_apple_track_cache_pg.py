"""Integration tests for the LML#1139 V/A track-cache purge (`@pytest.mark.pg`).

The unit suite drives ``purge()`` against a fake connection, so it pins the
script's *shape* — which keys reach the DELETE, that a transaction is opened,
that the CSV is written before commit. What it cannot pin is whether any of
that SQL is valid, and this is a script that DELETEs from production.

Concrete production risks only a real PostgreSQL can catch:

* The POSIX ``~`` regex operator accepts ``SUPERSET_REGEX`` at all, and the
  alternation actually matches the keys the arbiter cares about — including the
  dotless ``v.a`` family the donor's pattern missed.
* ``artist_normalized = ANY($2)`` binds a Python ``list[str]`` to a text[]
  without a cast, rather than raising at execute time.
* ``DELETE ... RETURNING`` returns the removed rows, and the rows are really
  gone afterwards — the property the whole recovery-CSV story rests on.
* ``LIMIT $4`` composes with the ``artist_normalized > $3`` cursor so waves
  advance instead of re-selecting the same window.
* A dry-run opens no write transaction and leaves every row in place.

Run with: pytest -m pg -v tests/integration/test_purge_va_apple_track_cache_pg.py
"""

from __future__ import annotations

import csv

import pytest
import pytest_asyncio

from entity.track_streaming_url_cache import (
    APPLE_MUSIC_TRACK_SERVICE,
    set_cached_track_streaming_url,
    set_up_track_streaming_url_cache_schema,
)
from scripts.purge_va_apple_track_cache import purge
from tests.integration.conftest import skip_if_named_tables_populated

# Two V/A shelf-genre filings (the real WXYC convention), one dotless `V.A`
# filing, and two real artists that DO match the coarse net but are rejected by
# `is_compilation_artist` — the pairing that proves the arbiter is load-bearing
# rather than decorative.
_VA_BLUES = "Various Artists - Blues"
_VA_LATIN = "Various Artists - Latin"
_VA_DOTLESS = "V.A - Jazz"
_REAL_VARIOUS = "Various Production"
_REAL_SOUNDTRACK = "The Soundtrack of Our Lives"

_SEEDED = [
    (_VA_BLUES, "Blues Classics", "I'm On My Way"),
    (_VA_BLUES, "Blues Classics", "Rollin' and Tumblin'"),
    (_VA_LATIN, "Latin Gold", "Desafinado"),
    (_VA_DOTLESS, "Jazz Sampler", "In a Sentimental Mood"),
    (_REAL_VARIOUS, "The World Is Gone", "Hater"),
    (_REAL_SOUNDTRACK, "Behind the Music", "Sister Surround"),
]


async def _all_artist_keys(pg_pool) -> set[str]:
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT DISTINCT artist_normalized FROM lml_cache.track_streaming_url_cache "
            "WHERE service = $1",
            APPLE_MUSIC_TRACK_SERVICE,
        )
    return {r["artist_normalized"] for r in rows}


async def _row_count(pg_pool) -> int:
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT count(*) FROM lml_cache.track_streaming_url_cache WHERE service = $1",
            APPLE_MUSIC_TRACK_SERVICE,
        )


@pytest_asyncio.fixture(autouse=True)
async def seeded_cache(pg_pool, pg_source):
    """Reset just ``lml_cache.track_streaming_url_cache``, then seed the mix.

    Surgical, and refuses to run if the table already holds rows — a mispointed
    ``DATABASE_URL_TEST`` would otherwise drop real cached streaming URLs.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(conn, (("lml_cache", "track_streaming_url_cache"),))
        await conn.execute("DROP TABLE IF EXISTS lml_cache.track_streaming_url_cache")
    await set_up_track_streaming_url_cache_schema(pg_source)
    for index, (artist, album, song) in enumerate(_SEEDED):
        await set_cached_track_streaming_url(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            artist=artist,
            album=album,
            song=song,
            url=f"https://music.apple.com/us/song/seeded/{index}",
        )
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS lml_cache.track_streaming_url_cache")


@pytest.mark.pg
class TestPurgeAgainstRealPostgres:
    @pytest.mark.asyncio
    async def test_execute_deletes_only_arbiter_confirmed_va_rows(
        self, pg_source, pg_pool, tmp_path
    ):
        """End-to-end: coarse net in SQL, arbiter in Python, DELETE in one
        transaction. The two real artists match the regex and must survive."""
        out = tmp_path / "wave.csv"

        deleted = await purge(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=out,
            limit=0,
            after="",
            execute=True,
        )

        assert deleted == 4  # 2 blues + 1 latin + 1 dotless V.A
        assert await _all_artist_keys(pg_pool) == {
            "various production",
            "the soundtrack of our lives",
        }

    @pytest.mark.asyncio
    async def test_dotless_va_family_is_reachable_through_the_sql_net(
        self, pg_source, pg_pool, tmp_path
    ):
        """``V.A - Jazz`` is ``is_compilation_artist``-True. The donor's
        ``v\\.a\\.`` pattern did not match it, which would have left it cached
        and serving a wrong deep-link forever from this hit-only table."""
        await purge(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=tmp_path / "wave.csv",
            limit=0,
            after="",
            execute=True,
        )

        assert "v.a - jazz" not in await _all_artist_keys(pg_pool)

    @pytest.mark.asyncio
    async def test_recovery_csv_holds_every_deleted_row(self, pg_source, tmp_path):
        """The CSV comes from ``DELETE ... RETURNING``, so it is the deleted set
        by construction — not a separately-SELECTed approximation of it."""
        out = tmp_path / "wave.csv"

        deleted = await purge(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=out,
            limit=0,
            after="",
            execute=True,
        )

        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        assert len(rows) == deleted
        assert {r["artist_normalized"] for r in rows} == {
            "various artists - blues",
            "various artists - latin",
            "v.a - jazz",
        }
        # Every row carries a restorable tuple: the full PK plus the URL.
        assert all(r["url"].startswith("https://music.apple.com/") for r in rows)
        assert all(r["service"] == APPLE_MUSIC_TRACK_SERVICE for r in rows)

    @pytest.mark.asyncio
    async def test_dry_run_deletes_nothing(self, pg_source, pg_pool, tmp_path):
        before = await _row_count(pg_pool)

        deleted = await purge(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=tmp_path / "wave.csv",
            limit=0,
            after="",
            execute=False,
        )

        assert deleted == 0
        assert await _row_count(pg_pool) == before

    @pytest.mark.asyncio
    async def test_waves_advance_under_limit_and_cursor(self, pg_source, pg_pool, tmp_path):
        """``LIMIT $4`` + ``artist_normalized > $3`` must compose so consecutive
        waves cover the whole key space without re-selecting a window.

        All five seeded keys match the coarse net, and sort as::

            the soundtrack of our lives  (arbiter: keep)
            v.a - jazz                   (purge, 1 row)
            various artists - blues      (purge, 2 rows)
            various artists - latin      (purge, 1 row)
            various production           (arbiter: keep)

        So a two-key wave deliberately spans a kept key and a purged one — the
        cursor has to advance past every key EXAMINED, not just the deleted
        ones, or the next wave re-selects the same window forever.
        """
        first = await purge(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=tmp_path / "w1.csv",
            limit=2,
            after="",
            execute=True,
        )
        # Examined the kept soundtrack key and `v.a - jazz`; only the latter goes.
        assert first == 1
        assert "various artists - blues" in await _all_artist_keys(pg_pool)

        second = await purge(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=tmp_path / "w2.csv",
            limit=2,
            after="v.a - jazz",
            execute=True,
        )

        # `various artists - blues` (2 rows) + `various artists - latin` (1 row).
        assert second == 3
        assert await _all_artist_keys(pg_pool) == {
            "various production",
            "the soundtrack of our lives",
        }

    @pytest.mark.asyncio
    async def test_wave_of_only_arbiter_rejects_opens_no_delete(self, pg_source, pg_pool, tmp_path):
        """A window whose keys ALL fail the arbiter must not DELETE, and must
        not write a CSV it would then refuse to overwrite on the next wave."""
        out = tmp_path / "wave.csv"
        before = await _row_count(pg_pool)

        deleted = await purge(
            pg_source,
            service=APPLE_MUSIC_TRACK_SERVICE,
            out=out,
            limit=1,
            after="various artists - latin",  # leaves only `various production`
            execute=True,
        )

        assert deleted == 0
        assert await _row_count(pg_pool) == before
        assert not out.exists()
