"""Unit tests for ``entity.streaming_url_cache`` (get / set helpers).

The polymorphic streaming-URL cache (LML#573) generalizes the
Apple-Music-specific ``entity.album_apple_music_lookup_cache`` (LML#571) into
one ``lml_cache.album_streaming_url_cache`` table keyed on
``(service, artist_normalized, album_normalized)``. The cache module owns
normalization via ``wxyc_etl.text.to_match_form`` so callers pass raw request
strings; the ``service`` key is the only thing distinguishing an Apple row
from a Spotify row.

Hit/miss semantics carry over verbatim from LML#576 (staleness is a SQL-side
filter): a non-null ``url`` is a durable hit (never stale); a null ``url``
inside ``miss_ttl`` is a recent known miss the SELECT returns; a null ``url``
past ``miss_ttl`` is filtered out SQL-side so callers see the same "no row"
shape as an absent entry.

These tests parametrize on the two PR-1 services and pin both the public
``str | None`` contract and the SQL bind-shape (``service = $1``, the
``(url IS NOT NULL OR last_checked_at > $4)`` filter, ``to_match_form``
normalization). PG interaction is mocked with ``AsyncMock(spec=PgSource)``;
``tests/integration/test_streaming_url_persistent_lookup.py`` covers the real
SQL behavior end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from entity.sources import PgSource
from entity.streaming_url_cache import (
    _ERROR_TTL_ENV_VAR,
    DEFAULT_ERROR_TTL,
    DEFAULT_MISS_TTL,
    get_cached_streaming_url,
    peek_cached_streaming_url,
    resolve_streaming_error_ttl,
    set_cached_streaming_url,
)

# The two services PR-1 ships. The cache module is service-agnostic — it
# stores whatever URL the client returns under the ``service`` key — so the
# representative URL shape per service only matters for readability here.
_SERVICE_CASES = [
    ("apple_music_album", "https://music.apple.com/us/album/foo/1234567890"),
    ("spotify_album", "https://open.spotify.com/album/1A2GTWGt0LBTGQAyA3OKAf"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestGetCachedStreamingURL:
    """``get_cached_streaming_url`` returns ``str | None`` per service."""

    async def test_returns_none_when_row_absent(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        result = await get_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert result is None

    async def test_returns_url_when_row_carries_url(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": sample_url})

        result = await get_cached_streaming_url(
            pg, service=service, artist="Stereolab", album="Aluminum Tunes"
        )

        assert result == sample_url

    async def test_returns_none_for_fresh_known_miss(self, service, sample_url):
        # A row whose URL is NULL but inside the TTL still returns a row from
        # the SQL filter — the public surface collapses it to ``None``. The
        # resolver distinguishes fresh-miss from absent via the private
        # ``_fetch_cached_row.was_present`` seam.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": None})

        result = await get_cached_streaming_url(
            pg, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert result is None

    async def test_sql_binds_service_first_then_cutoff(self, service, sample_url):
        # LML#573: ``service`` is bind ``$1``; the LML#576 TTL cutoff moves
        # to ``$4`` (was ``$3`` in the apple-only table). Pin the bind shape
        # so a careless edit can't silently regress.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        miss_ttl = timedelta(days=7)

        await get_cached_streaming_url(
            pg,
            service=service,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            miss_ttl=miss_ttl,
            now=now,
        )

        assert pg.fetchone.await_count == 1
        call = pg.fetchone.await_args
        sql = call.args[0]
        assert "FROM lml_cache.album_streaming_url_cache" in sql
        assert "WHERE service = $1" in sql
        assert "artist_normalized = $2 AND album_normalized = $3" in sql
        # The TTL filter must be DB-side, not Python-side, and gated on ``$4``
        # for a genuine miss (LML#1121: `$5` is the sibling error-row cutoff,
        # pinned separately in ``test_sql_binds_error_cutoff_as_fifth_param``).
        assert "NOT is_error AND last_checked_at > $4" in sql
        assert "url IS NOT NULL" in sql
        # Bind order: service, artist, album, genuine-miss cutoff.
        assert call.args[1] == service
        assert call.args[4] == now - miss_ttl

    async def test_sql_binds_error_cutoff_as_fifth_param(self, service, sample_url):
        # LML#1121: a short-TTL error row is distinguished from a genuine
        # known-miss row via the ``is_error`` column; its own cutoff is bind
        # ``$5``, resolved from ``error_ttl`` the same way ``miss_ttl``
        # resolves ``$4``.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        error_ttl = timedelta(minutes=5)

        await get_cached_streaming_url(
            pg,
            service=service,
            artist="Hyd",
            album="Hold Onto Me Infinity",
            now=now,
            error_ttl=error_ttl,
        )

        call = pg.fetchone.await_args
        sql = call.args[0]
        assert "is_error" in sql
        assert "last_checked_at > $5" in sql
        assert call.args[5] == now - error_ttl

    async def test_error_ttl_defaults_to_env_resolution_when_omitted(self, service, sample_url):
        # Production callers (the /lookup post-process, the fail-fast probe)
        # never pass ``error_ttl`` explicitly -- omitting it must still resolve
        # a live value via ``resolve_streaming_error_ttl`` (env-tunable, no
        # redeploy), not silently skip the bind.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

        await get_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Hold Onto Me Infinity", now=now
        )

        call = pg.fetchone.await_args
        assert call.args[5] == now - DEFAULT_ERROR_TTL

    async def test_normalizes_artist_and_album_via_to_match_form(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        await get_cached_streaming_url(
            pg, service=service, artist="Nilüfer Yanya", album="PAINLESS"
        )

        assert pg.fetchone.await_count == 1
        call = pg.fetchone.await_args
        # ``to_match_form`` strips diacritics + lowercases; binds $2/$3.
        assert call.args[2] == "nilufer yanya"
        assert call.args[3] == "painless"

    async def test_returns_none_on_pg_error(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        result = await get_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestPeekCachedStreamingURL:
    """``peek_cached_streaming_url`` exposes the ``was_present`` bit so the
    /lookup post-process can tell a fresh known-miss (skip the warm) from an
    absent/stale row (warm) — without paying the live probe (LML#706).

    LML#1115: it also exposes the row's ``is_error`` bit as a third tuple
    element, so a fresh LML#1121 transport-failure row (couldn't ask) is
    distinguishable from a fresh genuine known miss (asked, confirmed absent)
    without the caller re-querying to find out.
    """

    async def test_hit_returns_url_and_fresh_decision(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": sample_url})

        url, has_fresh_decision, is_error = await peek_cached_streaming_url(
            pg, service=service, artist="Stereolab", album="Aluminum Tunes"
        )

        assert url == sample_url
        assert has_fresh_decision is True
        assert is_error is False

    async def test_fresh_known_miss_returns_none_with_fresh_decision(self, service, sample_url):
        # Row present, url NULL, inside the TTL (the SQL filter returned it):
        # the cache already knows "not found" — the caller must NOT warm.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": None, "is_error": False})

        url, has_fresh_decision, is_error = await peek_cached_streaming_url(
            pg, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert url is None
        assert has_fresh_decision is True
        assert is_error is False

    async def test_fresh_error_row_returns_none_with_fresh_decision_and_is_error(
        self, service, sample_url
    ):
        # LML#1115: a fresh ERROR row (is_error=true, still inside the short
        # error_ttl) is a "couldn't ask", not a confirmed absence -- the
        # third tuple element must say so, distinctly from a genuine miss.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value={"url": None, "is_error": True})

        url, has_fresh_decision, is_error = await peek_cached_streaming_url(
            pg, service=service, artist="Sessa", album="Estrela Acesa"
        )

        assert url is None
        assert has_fresh_decision is True
        assert is_error is True

    async def test_absent_or_stale_returns_none_without_fresh_decision(self, service, sample_url):
        # No row (absent, or a stale miss the SQL WHERE filtered out): the
        # caller should probe/warm.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(return_value=None)

        url, has_fresh_decision, is_error = await peek_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert url is None
        assert has_fresh_decision is False
        assert is_error is False

    async def test_pg_error_degrades_to_probe(self, service, sample_url):
        # A failed read must look like "absent" so the caller falls through to
        # a probe/warm — same posture as ``get_cached_streaming_url``.
        pg = AsyncMock(spec=PgSource)
        pg.fetchone = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        url, has_fresh_decision, is_error = await peek_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Hold Onto Me Infinity"
        )

        assert url is None
        assert has_fresh_decision is False
        assert is_error is False


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "sample_url"), _SERVICE_CASES)
class TestSetCachedStreamingURL:
    """``set_cached_streaming_url`` UPSERTs keyed on (service, artist, album)."""

    async def test_inserts_hit(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Hold Onto Me Infinity", url=sample_url
        )

        assert pg.execute.await_count == 1
        call = pg.execute.await_args
        sql = call.args[0]
        assert "INSERT INTO lml_cache.album_streaming_url_cache" in sql
        assert "ON CONFLICT (service, artist_normalized, album_normalized)" in sql
        # Bind: service, artist_normalized, album_normalized, url, is_error.
        assert call.args[1] == service
        assert call.args[2] == "hyd"
        assert call.args[3] == "hold onto me infinity"
        assert call.args[4] == sample_url
        # LML#1121: a genuine resolve clears any stale error marker.
        assert call.args[5] is False

    async def test_inserts_known_miss(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_streaming_url(
            pg, service=service, artist="Sessa", album="Estrela Acesa", url=None
        )

        call = pg.execute.await_args
        assert call.args[1] == service
        assert call.args[2] == "sessa"
        assert call.args[3] == "estrela acesa"
        # url bound as NULL
        assert call.args[4] is None
        # LML#1121: a genuine (non-error) miss keeps writing is_error=False.
        assert call.args[5] is False

    async def test_inserts_error_row(self, service, sample_url):
        # LML#1121: a transport-failure write marks the row as an error miss
        # (short TTL) rather than a genuine known-miss (7-day TTL) -- both
        # share the same NULL url, so the SQL text and the ``is_error`` bind
        # are the only distinguishing signal.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_streaming_url(
            pg, service=service, artist="Sessa", album="Estrela Acesa", url=None, is_error=True
        )

        call = pg.execute.await_args
        sql = call.args[0]
        assert "is_error" in sql
        assert call.args[4] is None
        assert call.args[5] is True

    async def test_is_error_coerced_false_when_url_present(self, service, sample_url):
        # Defensive guard (mirrors LML#824's ``crowd_out`` coercion): a resolved
        # URL is never simultaneously an error row, regardless of what a
        # careless caller passes.
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(return_value="INSERT 0 1")

        await set_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Angel", url=sample_url, is_error=True
        )

        call = pg.execute.await_args
        assert call.args[4] == sample_url
        assert call.args[5] is False

    async def test_swallows_pg_error(self, service, sample_url):
        pg = AsyncMock(spec=PgSource)
        pg.execute = AsyncMock(side_effect=RuntimeError("PG unreachable"))

        # No exception raised — cache writes are best-effort.
        await set_cached_streaming_url(
            pg, service=service, artist="Hyd", album="Angel", url=sample_url
        )


def test_default_miss_ttl_is_seven_days():
    # Pin the published default so a careless change to the constant gets
    # caught by review.
    assert DEFAULT_MISS_TTL == timedelta(days=7)


def test_default_error_ttl_is_five_minutes():
    # LML#1121: short enough that a real Bandcamp outage self-heals fast, long
    # enough to backpressure a hot /lookup loop's re-enqueue during the outage.
    assert DEFAULT_ERROR_TTL == timedelta(minutes=5)


class TestResolveStreamingErrorTTL:
    """``resolve_streaming_error_ttl`` -- the LML#1121 no-redeploy lever for the
    short error-row TTL, mirroring ``core.search.resolve_positive_int_env``'s
    WARN-on-junk contract (unparseable / zero / negative all fall back)."""

    def test_returns_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv(_ERROR_TTL_ENV_VAR, raising=False)

        assert resolve_streaming_error_ttl() == DEFAULT_ERROR_TTL

    def test_reads_env_override(self, monkeypatch):
        monkeypatch.setenv(_ERROR_TTL_ENV_VAR, "10")

        assert resolve_streaming_error_ttl() == timedelta(minutes=10)

    def test_falls_back_on_unparseable_value(self, monkeypatch):
        monkeypatch.setenv(_ERROR_TTL_ENV_VAR, "not-a-number")

        assert resolve_streaming_error_ttl() == DEFAULT_ERROR_TTL

    def test_falls_back_on_zero(self, monkeypatch):
        monkeypatch.setenv(_ERROR_TTL_ENV_VAR, "0")

        assert resolve_streaming_error_ttl() == DEFAULT_ERROR_TTL

    def test_falls_back_on_negative(self, monkeypatch):
        monkeypatch.setenv(_ERROR_TTL_ENV_VAR, "-5")

        assert resolve_streaming_error_ttl() == DEFAULT_ERROR_TTL
