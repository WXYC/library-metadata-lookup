"""Integration (``@pytest.mark.pg``) tests for ``StreamingCatalogDAO`` (LML#842 PR B).

The PG rewrite of ``scripts/streaming_availability/results_db.py``'s
``ResultsDB``: same method surface (so PR D/E callers that go through
ResultsDB methods port by construction-site changes; the raw ``_db``
reach-ins get rewritten in PR D), backed by the PR A row-level schema
(``lml_cache.streaming_album`` + ``streaming_album_service`` +
``streaming_track_result`` + ``streaming_coverage_baseline``) instead of the
whole-file SQLite lineage. The load-bearing contracts pinned here:

* **Anti-join pending semantics** — a missing ``(album, service)`` row IS
  ``'pending'``. Fresh albums carry no service rows at all; every pending
  scan, stats bucket, and wide pivot column must coalesce absence to
  ``'pending'`` rather than fan out phantom rows at insert time.
* **Wide legacy row shape** — reads return the legacy SQLite column names
  (``spotify_status``, ``spotify_id``, ``apple_url``, ``bandcamp_slug``, …)
  via one shared four-way service pivot, so row consumers port unchanged.
* **Upsert discipline (plan D3)** — service writes are upserts on
  ``(album_id, service)`` that never write NULL over populated metadata
  (``COALESCE(EXCLUDED.x, existing.x)``), and PR A's no-regress guards
  propagate as-is: the DAO is a tripwire, not a pre-masker.
* **Coverage baseline refresh** — ``refresh_coverage_baseline`` upserts the
  five ``routers/admin.py`` metric keys from live counts; lowering a floor
  raises through the PR A baseline guard.

Run with: pytest -m pg -v tests/integration/test_streaming_catalog_dao.py
"""

from __future__ import annotations

import json

import asyncpg
import asyncpg.exceptions
import pytest
import pytest_asyncio

from entity.streaming_catalog import _TABLES
from routers.admin import _STREAMING_COVERAGE_METRICS
from scripts.streaming_availability.catalog_dao import StreamingCatalogDAO
from scripts.streaming_availability.dedup import DeduplicatedAlbum
from tests.integration.conftest import (
    DATABASE_URL,
    opted_in,
    skip_if_named_tables_populated,
)

# ``_TABLES`` is parents-before-children (creation order), so its reverse is
# FK-safe for plain DROP TABLE.
_DROP_ORDER = tuple(reversed(_TABLES))

_SPOTIFY_URL = "https://open.spotify.com/album/2FZLDLcLBKcQLTLdQ7Wvoj"
_APPLE_URL = "https://music.apple.com/us/album/aluminum-tunes/1443544224"
_DEEZER_URL = "https://www.deezer.com/album/302127"
_BANDCAMP_URL = "https://stereolab.bandcamp.com/album/aluminum-tunes"

# The legacy ``albums`` SELECT-* column set (post-``_migrate()``), i.e. the row
# shape every PR D/E row consumer was written against. The wide pivot must keep
# emitting at least these names.
_LEGACY_ALBUM_COLUMNS = {
    "id",
    "normalized_artist",
    "normalized_title",
    "display_artist",
    "display_title",
    "library_ids",
    "formats",
    "genre",
    "label",
    "is_compilation",
    "is_single",
    "discogs_artist",
    "discogs_title",
    "discogs_release_id",
    "discogs_status",
    "deezer_status",
    "deezer_url",
    "deezer_confidence",
    "deezer_matched_artist",
    "deezer_matched_title",
    "deezer_checked_at",
    "spotify_status",
    "spotify_url",
    "spotify_id",
    "spotify_confidence",
    "spotify_matched_artist",
    "spotify_matched_title",
    "spotify_checked_at",
    "apple_status",
    "apple_url",
    "apple_confidence",
    "apple_matched_artist",
    "apple_matched_title",
    "apple_checked_at",
    "bandcamp_slug",
    "bandcamp_url",
    "bandcamp_status",
    "bandcamp_checked_at",
    "created_at",
}

# Distinct-album kwarg bundles (WXYC-representative fixtures).
_PRATT = {
    "normalized_artist": "jessica pratt",
    "normalized_title": "on your own love again",
    "display_artist": "Jessica Pratt",
    "display_title": "On Your Own Love Again",
}
_MOLINA = {
    "normalized_artist": "juana molina",
    "normalized_title": "doga",
    "display_artist": "Juana Molina",
    "display_title": "DOGA",
}
_CAT_POWER = {
    "normalized_artist": "cat power",
    "normalized_title": "moon pix",
    "display_artist": "Cat Power",
    "display_title": "Moon Pix",
}
_COMPILATION = {
    "normalized_artist": "various artists",
    "normalized_title": "disco not disco",
    "display_artist": "Various Artists",
    "display_title": "Disco Not Disco",
    "is_compilation": True,
}


def _make_album(**overrides) -> DeduplicatedAlbum:
    kwargs: dict = {
        "normalized_artist": "stereolab",
        "normalized_title": "aluminum tunes",
        "display_artist": "Stereolab",
        "display_title": "Aluminum Tunes",
        "library_ids": [87],
        "formats": ["CD"],
        "genre": "Rock",
        "label": "Duophonic",
    }
    kwargs.update(overrides)
    return DeduplicatedAlbum(**kwargs)


def _make_track(album_id: int, **overrides) -> dict:
    track: dict = {
        "album_id": album_id,
        "artist": "Juana Molina",
        "title": "la paradoja",
        "position": "A1",
        "source": "discogs_tracklist",
        "source_type": "compilation",
    }
    track.update(overrides)
    return track


@pytest_asyncio.fixture(autouse=True)
async def reset_catalog_tables(pg_pool):
    """Reset just the four streaming-catalog tables around each test.

    Same surgical shape as PR A's ``test_streaming_catalog.py`` fixture: veto
    through the shared table-scoped guard first (a mispointed
    ``DATABASE_URL_TEST`` at the shared discogs-cache PG must skip, not drop
    collected data), then drop only the tables this suite owns.
    """
    async with pg_pool.acquire() as conn:
        await skip_if_named_tables_populated(
            conn, tuple(("lml_cache", table) for table in _DROP_ORDER)
        )
        for table in _DROP_ORDER:
            await conn.execute(f"DROP TABLE IF EXISTS lml_cache.{table}")
    yield
    async with pg_pool.acquire() as conn:
        for table in _DROP_ORDER:
            await conn.execute(f"DROP TABLE IF EXISTS lml_cache.{table}")


@pytest_asyncio.fixture
async def dao(pg_pool, reset_catalog_tables):
    """A connected DAO borrowing the test pool; ``connect()`` bootstraps the schema."""
    d = StreamingCatalogDAO(pool=pg_pool)
    await d.connect()
    yield d
    await d.close()


async def _seed_album(dao: StreamingCatalogDAO, pg_pool, **overrides) -> int:
    """Insert one album through the DAO and return its PG id."""
    album = _make_album(**overrides)
    await dao.insert_albums([album])
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM lml_cache.streaming_album "
            "WHERE normalized_artist = $1 AND normalized_title = $2",
            album.normalized_artist,
            album.normalized_title,
        )


async def _seed_track(dao: StreamingCatalogDAO, pg_pool, album_id: int, **overrides) -> int:
    """Insert one track through the DAO and return its PG id."""
    track = _make_track(album_id, **overrides)
    await dao.insert_tracks([track])
    async with pg_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT id FROM lml_cache.streaming_track_result "
            "WHERE album_id = $1 AND artist = $2 AND title = $3",
            album_id,
            track["artist"],
            track["title"],
        )


@pytest.mark.pg
class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_bootstraps_schema(self, dao, pg_pool) -> None:
        """``connect()`` on a bare PG stands up all four catalog tables."""
        async with pg_pool.acquire() as conn:
            for table in _TABLES:
                regclass = await conn.fetchval("SELECT to_regclass($1)", f"lml_cache.{table}")
                assert regclass is not None, f"lml_cache.{table} missing after connect()"

    @pytest.mark.asyncio
    async def test_connect_is_idempotent(self, dao) -> None:
        await dao.connect()
        assert await dao.insert_albums([_make_album()]) == 1

    @pytest.mark.asyncio
    async def test_dsn_construction_owns_its_pool(self, reset_catalog_tables) -> None:
        """The dsn shape (offline scripts, no shared app pool) round-trips:
        connect → bootstrap → write → read → close."""
        d = StreamingCatalogDAO(dsn=DATABASE_URL)
        try:
            await d.connect()
            assert await d.insert_albums([_make_album()]) == 1
            stats = await d.get_stats()
            assert stats["total"] == 1
        finally:
            await d.close()


@pytest.mark.pg
class TestInsertAlbums:
    @pytest.mark.asyncio
    async def test_insert_returns_count_and_persists(self, dao) -> None:
        inserted = await dao.insert_albums([_make_album(), _make_album(**_PRATT)])
        assert inserted == 2
        rows = await dao.get_all_results()
        assert [r["display_title"] for r in rows] == [
            "On Your Own Love Again",
            "Aluminum Tunes",
        ]

    @pytest.mark.asyncio
    async def test_duplicate_insert_ignored(self, dao) -> None:
        assert await dao.insert_albums([_make_album()]) == 1
        assert await dao.insert_albums([_make_album()]) == 0
        assert (await dao.get_stats())["total"] == 1

    @pytest.mark.asyncio
    async def test_insert_skips_intra_batch_duplicates(self, dao) -> None:
        """Two rows sharing a normalized key inside ONE batch: first wins,
        duplicate skipped. Pins the set-based single-statement insert path,
        where intra-batch conflicts are arbitrated by ON CONFLICT DO NOTHING
        rather than by the earlier row already being committed."""
        batch = [
            _make_album(),
            _make_album(display_title="Aluminum Tunes (reissue)"),
            _make_album(**_PRATT),
        ]
        assert await dao.insert_albums(batch) == 2
        assert (await dao.get_stats())["total"] == 2

    @pytest.mark.asyncio
    async def test_provenance_json_round_trips_as_str(self, dao) -> None:
        """``library_ids``/``formats`` stay JSON strings on the way out —
        the PR D/E row consumers call ``json.loads`` on them today."""
        await dao.insert_albums([_make_album(library_ids=[87, 91], formats=["CD", "Vinyl"])])
        row = (await dao.get_all_results())[0]
        assert isinstance(row["library_ids"], str)
        assert json.loads(row["library_ids"]) == [87, 91]
        assert json.loads(row["formats"]) == ["CD", "Vinyl"]

    @pytest.mark.asyncio
    async def test_flags_round_trip(self, dao) -> None:
        await dao.insert_albums([_make_album(**_COMPILATION), _make_album(is_single=True)])
        rows = {r["display_title"]: r for r in await dao.get_all_results()}
        assert rows["Disco Not Disco"]["is_compilation"]
        assert not rows["Disco Not Disco"]["is_single"]
        assert rows["Aluminum Tunes"]["is_single"]


@pytest.mark.pg
class TestPendingScans:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("service", ["spotify", "apple", "deezer", "bandcamp"])
    async def test_fresh_album_is_pending_everywhere(self, dao, pg_pool, service) -> None:
        """The anti-join contract: a fresh album has NO service rows, yet is
        pending for every probed service."""
        album_id = await _seed_album(dao, pg_pool)
        rows = await dao.get_pending(service)
        assert [r["id"] for r in rows] == [album_id]
        async with pg_pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(*) FROM lml_cache.streaming_album_service")
        assert n == 0, "pending must come from row ABSENCE, not fan-out rows"

    @pytest.mark.asyncio
    async def test_found_excluded_from_pending_others_unaffected(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_result(album_id, "spotify", "found", url=_SPOTIFY_URL)
        assert await dao.get_pending("spotify") == []
        assert [r["id"] for r in await dao.get_pending("apple")] == [album_id]
        assert [r["id"] for r in await dao.get_pending("deezer")] == [album_id]

    @pytest.mark.asyncio
    async def test_pending_row_carries_legacy_wide_shape(self, dao, pg_pool) -> None:
        await _seed_album(dao, pg_pool)
        row = (await dao.get_pending("spotify"))[0]
        assert _LEGACY_ALBUM_COLUMNS <= set(row.keys())
        assert row["spotify_status"] == "pending"
        assert row["apple_status"] == "pending"
        assert row["bandcamp_status"] == "pending"
        assert row["spotify_url"] is None
        assert row["bandcamp_slug"] is None
        assert row["deezer_matched_artist"] is None

    @pytest.mark.asyncio
    async def test_discogs_pseudo_service_reads_album_column(self, dao, pg_pool) -> None:
        """``get_pending("discogs")`` is the album-column pseudo-service the
        ``__main__.py`` orchestrator uses — and unlike ``get_pending_discogs``
        it applies NO compilation filter (legacy contract)."""
        album_id = await _seed_album(dao, pg_pool)
        comp_id = await _seed_album(dao, pg_pool, **_COMPILATION)
        assert {r["id"] for r in await dao.get_pending("discogs")} == {album_id, comp_id}
        await dao.update_discogs_result(
            album_id, "found", artist="Stereolab", title="Aluminum Tunes", release_id=118423
        )
        assert {r["id"] for r in await dao.get_pending("discogs")} == {comp_id}

    @pytest.mark.asyncio
    async def test_limit_respected(self, dao, pg_pool) -> None:
        await _seed_album(dao, pg_pool)
        await _seed_album(dao, pg_pool, **_PRATT)
        await _seed_album(dao, pg_pool, **_MOLINA)
        assert len(await dao.get_pending("spotify", limit=2)) == 2
        assert len(await dao.get_pending("discogs", limit=1)) == 1


@pytest.mark.pg
class TestDiscogsEnrichment:
    @pytest.mark.asyncio
    async def test_get_pending_discogs_excludes_compilations(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        await _seed_album(dao, pg_pool, **_COMPILATION)
        assert [r["id"] for r in await dao.get_pending_discogs()] == [album_id]

    @pytest.mark.asyncio
    async def test_update_discogs_result_persists_match_group(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_discogs_result(
            album_id, "found", artist="Stereolab", title="Aluminum Tunes", release_id=118423
        )
        row = (await dao.get_all_results())[0]
        assert row["discogs_status"] == "found"
        assert row["discogs_artist"] == "Stereolab"
        assert row["discogs_title"] == "Aluminum Tunes"
        assert row["discogs_release_id"] == 118423
        assert await dao.get_pending_discogs() == []

    @pytest.mark.asyncio
    async def test_update_discogs_result_coalesces_metadata(self, dao, pg_pool) -> None:
        """Plan D3: a follow-up write with omitted metadata must not null what
        an earlier write collected."""
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_discogs_result(album_id, "error", artist="Stereolab")
        await dao.update_discogs_result(album_id, "found", release_id=118423)
        row = (await dao.get_all_results())[0]
        assert row["discogs_status"] == "found"
        assert row["discogs_artist"] == "Stereolab"
        assert row["discogs_release_id"] == 118423


@pytest.mark.pg
class TestCascadeQueries:
    @pytest.mark.asyncio
    async def test_deezer_hits_pending_spotify(self, dao, pg_pool) -> None:
        hit_id = await _seed_album(dao, pg_pool)
        miss_id = await _seed_album(dao, pg_pool, **_PRATT)
        await dao.update_result(hit_id, "deezer", "found", url=_DEEZER_URL)
        await dao.update_result(miss_id, "deezer", "not_found")
        assert [r["id"] for r in await dao.get_deezer_hits_pending_spotify()] == [hit_id]
        await dao.update_result(hit_id, "spotify", "found", url=_SPOTIFY_URL)
        assert await dao.get_deezer_hits_pending_spotify() == []

    @pytest.mark.asyncio
    async def test_spotify_misses_pending_apple(self, dao, pg_pool) -> None:
        miss_id = await _seed_album(dao, pg_pool)
        await _seed_album(dao, pg_pool, **_PRATT)  # spotify never probed: excluded
        await dao.update_result(miss_id, "spotify", "not_found")
        assert [r["id"] for r in await dao.get_spotify_misses_pending_apple()] == [miss_id]
        await dao.update_result(miss_id, "apple", "not_found")
        assert await dao.get_spotify_misses_pending_apple() == []


@pytest.mark.pg
class TestUpdateResult:
    @pytest.mark.asyncio
    async def test_spotify_metadata_lands_in_wide_row(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_result(
            album_id,
            "spotify",
            "found",
            url=_SPOTIFY_URL,
            spotify_id="2FZLDLcLBKcQLTLdQ7Wvoj",
            confidence=0.97,
            matched_artist="Stereolab",
            matched_title="Aluminum Tunes",
        )
        row = (await dao.get_all_results())[0]
        assert row["spotify_status"] == "found"
        assert row["spotify_url"] == _SPOTIFY_URL
        assert row["spotify_id"] == "2FZLDLcLBKcQLTLdQ7Wvoj"
        assert row["spotify_confidence"] == pytest.approx(0.97)
        assert row["spotify_matched_artist"] == "Stereolab"
        assert row["spotify_matched_title"] == "Aluminum Tunes"
        assert row["spotify_checked_at"] is not None

    @pytest.mark.asyncio
    async def test_apple_alias_writes_apple_music_service_row(self, dao, pg_pool) -> None:
        """The legacy ``apple`` token maps to the canonical ``apple_music``
        slug in the service rows while the wide pivot keeps the legacy
        ``apple_*`` column names."""
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_result(album_id, "apple", "found", url=_APPLE_URL)
        async with pg_pool.acquire() as conn:
            service = await conn.fetchval(
                "SELECT service FROM lml_cache.streaming_album_service WHERE album_id = $1",
                album_id,
            )
        assert service == "apple_music"
        row = (await dao.get_all_results())[0]
        assert row["apple_status"] == "found"
        assert row["apple_url"] == _APPLE_URL

    @pytest.mark.asyncio
    async def test_null_metadata_does_not_clobber(self, dao, pg_pool) -> None:
        """Plan D3 upsert discipline: NULL params never overwrite populated
        metadata (``COALESCE(EXCLUDED.x, existing.x)``). The legacy SQLite DAO
        would have nulled the URL here."""
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_result(
            album_id, "spotify", "found", url=_SPOTIFY_URL, matched_artist="Stereolab"
        )
        await dao.update_result(album_id, "spotify", "found", confidence=0.88)
        row = (await dao.get_all_results())[0]
        assert row["spotify_url"] == _SPOTIFY_URL
        assert row["spotify_matched_artist"] == "Stereolab"
        assert row["spotify_confidence"] == pytest.approx(0.88)

    @pytest.mark.asyncio
    async def test_upserts_row_that_does_not_exist_yet(self, dao, pg_pool) -> None:
        """Writes are upserts, not UPDATEs — the anti-join model means the
        target row usually doesn't exist when the first probe lands."""
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_result(album_id, "deezer", "error")
        row = (await dao.get_all_results())[0]
        assert row["deezer_status"] == "error"
        assert await dao.get_pending("deezer") == []

    @pytest.mark.asyncio
    async def test_empty_url_rejected_by_schema_check(self, dao, pg_pool) -> None:
        """The extractors' ``''`` default must trip PR A's column CHECK — the
        DAO passes it through rather than silently normalizing, so the broken
        caller gets fixed instead of masked."""
        album_id = await _seed_album(dao, pg_pool)
        with pytest.raises(asyncpg.CheckViolationError):
            await dao.update_result(album_id, "spotify", "found", url="")

    @pytest.mark.asyncio
    async def test_demotion_propagates_guard_error(self, dao, pg_pool) -> None:
        """PR A's no-regress status gate fires through the DAO unmasked."""
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_result(album_id, "spotify", "found", url=_SPOTIFY_URL)
        with pytest.raises(asyncpg.exceptions.PostgresError, match="allow_url_removal"):
            await dao.update_result(album_id, "spotify", "not_found")


@pytest.mark.pg
class TestBandcamp:
    @pytest.mark.asyncio
    async def test_update_bandcamp_slug_creates_pending_row(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_bandcamp_slug(album_id, "stereolab")
        row = (await dao.get_all_results())[0]
        assert row["bandcamp_slug"] == "stereolab"
        assert row["bandcamp_status"] == "pending"
        assert [r["id"] for r in await dao.get_pending_bandcamp_lookup()] == [album_id]

    @pytest.mark.asyncio
    async def test_pending_bandcamp_lookup_slug_filter(self, dao, pg_pool) -> None:
        a_id = await _seed_album(dao, pg_pool)
        b_id = await _seed_album(dao, pg_pool, **_PRATT)
        await dao.update_bandcamp_slug(a_id, "stereolab")
        await dao.update_bandcamp_slug(b_id, "jessicapratt")
        rows = await dao.get_pending_bandcamp_lookup(slug="jessicapratt")
        assert [r["id"] for r in rows] == [b_id]

    @pytest.mark.asyncio
    async def test_pending_bandcamp_lookup_excludes_compilations(self, dao, pg_pool) -> None:
        comp_id = await _seed_album(dao, pg_pool, **_COMPILATION)
        await dao.update_bandcamp_slug(comp_id, "variousartists")
        assert await dao.get_pending_bandcamp_lookup() == []

    @pytest.mark.asyncio
    async def test_update_bandcamp_url_marks_found_and_preserves_slug(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_bandcamp_slug(album_id, "stereolab")
        await dao.update_bandcamp_url(album_id, _BANDCAMP_URL)
        row = (await dao.get_all_results())[0]
        assert row["bandcamp_url"] == _BANDCAMP_URL
        assert row["bandcamp_status"] == "found"
        assert row["bandcamp_slug"] == "stereolab"
        assert row["bandcamp_checked_at"] is not None
        assert await dao.get_pending_bandcamp_lookup() == []

    @pytest.mark.asyncio
    async def test_mark_bandcamp_not_found_is_resumable_marker(self, dao, pg_pool) -> None:
        """#661 contract: 'tried, no match' must be durably distinct from 'not
        yet tried' so a bulk drain skips already-attempted slugs on re-run."""
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_bandcamp_slug(album_id, "stereolab")
        await dao.mark_bandcamp_not_found(album_id)
        row = (await dao.get_all_results())[0]
        assert row["bandcamp_status"] == "not_found"
        assert row["bandcamp_url"] is None
        assert await dao.get_pending_bandcamp_lookup() == []

    @pytest.mark.asyncio
    async def test_artists_without_slug_default_scope(self, dao, pg_pool) -> None:
        """Default scope = spotify not_found, non-compilation, slugless,
        DISTINCT display_artist."""
        miss_id = await _seed_album(dao, pg_pool)
        slugged_id = await _seed_album(dao, pg_pool, **_PRATT)
        await _seed_album(dao, pg_pool, **_MOLINA)  # spotify never probed
        comp_id = await _seed_album(dao, pg_pool, **_COMPILATION)
        for album_id in (miss_id, slugged_id, comp_id):
            await dao.update_result(album_id, "spotify", "not_found")
        await dao.update_bandcamp_slug(slugged_id, "jessicapratt")
        rows = await dao.get_artists_without_bandcamp_slug()
        assert [r["display_artist"] for r in rows] == ["Stereolab"]

    @pytest.mark.asyncio
    async def test_artists_without_slug_all_scope_and_limit(self, dao, pg_pool) -> None:
        await _seed_album(dao, pg_pool)
        await _seed_album(dao, pg_pool, **_MOLINA)
        rows = await dao.get_artists_without_bandcamp_slug(not_on_streaming_only=False)
        assert {r["display_artist"] for r in rows} == {"Stereolab", "Juana Molina"}
        limited = await dao.get_artists_without_bandcamp_slug(not_on_streaming_only=False, limit=1)
        assert len(limited) == 1


@pytest.mark.pg
class TestStatsAndReports:
    @pytest.mark.asyncio
    async def test_get_stats_empty(self, dao) -> None:
        stats = await dao.get_stats()
        assert stats == {
            "spotify": {},
            "apple": {},
            "discogs": {},
            "deezer": {},
            "bandcamp": {},
            "total": 0,
        }

    @pytest.mark.asyncio
    async def test_get_stats_counts_missing_rows_as_pending(self, dao, pg_pool) -> None:
        found_id = await _seed_album(dao, pg_pool)
        await _seed_album(dao, pg_pool, **_PRATT)
        await dao.update_result(found_id, "spotify", "found", url=_SPOTIFY_URL)
        stats = await dao.get_stats()
        assert stats["total"] == 2
        assert stats["spotify"] == {"found": 1, "pending": 1}
        assert stats["apple"] == {"pending": 2}
        assert stats["deezer"] == {"pending": 2}
        assert stats["bandcamp"] == {"pending": 2}
        assert stats["discogs"] == {"pending": 2}

    @pytest.mark.asyncio
    async def test_get_not_on_streaming(self, dao, pg_pool) -> None:
        miss_a = await _seed_album(dao, pg_pool)
        miss_b = await _seed_album(dao, pg_pool, **_CAT_POWER)
        hit_id = await _seed_album(dao, pg_pool, **_PRATT)
        comp_id = await _seed_album(dao, pg_pool, **_COMPILATION)
        for album_id in (miss_a, miss_b, comp_id):
            await dao.update_result(album_id, "spotify", "not_found")
        await dao.update_result(hit_id, "spotify", "found", url=_SPOTIFY_URL)
        rows = await dao.get_not_on_streaming()
        assert [r["display_artist"] for r in rows] == ["Cat Power", "Stereolab"]

    @pytest.mark.asyncio
    async def test_get_all_results_ordering(self, dao, pg_pool) -> None:
        await _seed_album(dao, pg_pool)
        await _seed_album(dao, pg_pool, **_MOLINA)
        await _seed_album(dao, pg_pool, **_CAT_POWER)
        rows = await dao.get_all_results()
        assert [r["display_artist"] for r in rows] == [
            "Cat Power",
            "Juana Molina",
            "Stereolab",
        ]


@pytest.mark.pg
class TestTracks:
    @pytest.mark.asyncio
    async def test_insert_tracks_counts_and_dedupes(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        tracks = [
            _make_track(album_id),
            _make_track(album_id, artist="Jessica Pratt", title="Back, Baby", position="A2"),
        ]
        assert await dao.insert_tracks(tracks) == 2
        assert await dao.insert_tracks(tracks) == 0

    @pytest.mark.asyncio
    async def test_get_pending_tracks_and_limit(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        await _seed_track(dao, pg_pool, album_id)
        await _seed_track(dao, pg_pool, album_id, artist="Jessica Pratt", title="Back, Baby")
        assert len(await dao.get_pending_tracks()) == 2
        assert len(await dao.get_pending_tracks(limit=1)) == 1

    @pytest.mark.asyncio
    async def test_update_track_resolution_full(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        track_id = await _seed_track(dao, pg_pool, album_id)
        await dao.update_track_resolution(
            track_id,
            "local_match",
            resolved_via="library",
            resolved_album_id=album_id,
            resolved_release_id=118423,
            spotify_url=_SPOTIFY_URL,
            spotify_confidence=0.93,
        )
        assert await dao.get_pending_tracks() == []
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM lml_cache.streaming_track_result WHERE id = $1", track_id
            )
        assert row["resolution_status"] == "local_match"
        assert row["resolved_via"] == "library"
        assert row["spotify_url"] == _SPOTIFY_URL
        assert row["spotify_confidence"] == pytest.approx(0.93)
        assert row["checked_at"] is not None

    @pytest.mark.asyncio
    async def test_update_track_resolution_coalesces(self, dao, pg_pool) -> None:
        """The lateral ``local_match`` → ``api_match`` move is legal, and the
        second write's omitted params must not null the first write's URLs."""
        album_id = await _seed_album(dao, pg_pool)
        track_id = await _seed_track(dao, pg_pool, album_id)
        await dao.update_track_resolution(track_id, "local_match", spotify_url=_SPOTIFY_URL)
        await dao.update_track_resolution(track_id, "api_match", deezer_url=_DEEZER_URL)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM lml_cache.streaming_track_result WHERE id = $1", track_id
            )
        assert row["resolution_status"] == "api_match"
        assert row["spotify_url"] == _SPOTIFY_URL
        assert row["deezer_url"] == _DEEZER_URL

    @pytest.mark.asyncio
    async def test_get_local_miss_tracks(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        track_id = await _seed_track(dao, pg_pool, album_id)
        await dao.update_track_resolution(track_id, "local_miss")
        assert [r["id"] for r in await dao.get_local_miss_tracks()] == [track_id]
        await dao.update_track_resolution(track_id, "api_match", spotify_url=_SPOTIFY_URL)
        assert await dao.get_local_miss_tracks() == []

    @pytest.mark.asyncio
    async def test_track_demotion_propagates_guard_error(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        track_id = await _seed_track(dao, pg_pool, album_id)
        await dao.update_track_resolution(track_id, "local_match", spotify_url=_SPOTIFY_URL)
        with pytest.raises(asyncpg.exceptions.PostgresError, match="allow_url_removal"):
            await dao.update_track_resolution(track_id, "not_found")

    @pytest.mark.asyncio
    async def test_insert_tracks_skips_intra_batch_duplicates(self, dao, pg_pool) -> None:
        """Same intra-batch arbitration pin as the album insert: a duplicate
        (album_id, artist, title) inside one batch is skipped, not an error."""
        album_id = await _seed_album(dao, pg_pool)
        batch = [
            _make_track(album_id),
            _make_track(album_id, position="B7"),
            _make_track(album_id, artist="Jessica Pratt", title="Back, Baby"),
        ]
        assert await dao.insert_tracks(batch) == 2

    @pytest.mark.asyncio
    async def test_mark_track_false_positive_demotes_api_match(self, dao, pg_pool) -> None:
        """The one deliberate demotion in the legacy surface: the track
        pipeline's validate phase flips ``api_match`` tracks whose URLs fail
        service-metadata validation to ``false_positive``. The DAO opts into
        the PR A no-regress guard transaction-locally for exactly this write;
        the collected URLs stay in place as the audit trail — deliberately
        better than the legacy demotion, which nulled every omitted metadata
        column (nothing downstream reads URLs off ``false_positive`` rows)."""
        album_id = await _seed_album(dao, pg_pool)
        track_id = await _seed_track(dao, pg_pool, album_id)
        await dao.update_track_resolution(track_id, "api_match", spotify_url=_SPOTIFY_URL)
        await dao.mark_track_false_positive(track_id)
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM lml_cache.streaming_track_result WHERE id = $1", track_id
            )
        assert row["resolution_status"] == "false_positive"
        assert row["spotify_url"] == _SPOTIFY_URL
        assert row["checked_at"] is not None

    @pytest.mark.asyncio
    async def test_mark_track_false_positive_opt_in_does_not_leak(self, dao, pg_pool) -> None:
        """The demotion's guard opt-in is transaction-local: a plain demotion
        attempted right after — very likely on the same pooled connection —
        must still trip the no-regress guard."""
        album_id = await _seed_album(dao, pg_pool)
        demoted_id = await _seed_track(dao, pg_pool, album_id)
        guarded_id = await _seed_track(
            dao, pg_pool, album_id, artist="Jessica Pratt", title="Back, Baby"
        )
        await dao.update_track_resolution(demoted_id, "api_match", spotify_url=_SPOTIFY_URL)
        await dao.update_track_resolution(guarded_id, "api_match", deezer_url=_DEEZER_URL)
        await dao.mark_track_false_positive(demoted_id)
        with pytest.raises(asyncpg.exceptions.PostgresError, match="allow_url_removal"):
            await dao.update_track_resolution(guarded_id, "not_found")

    @pytest.mark.asyncio
    async def test_get_track_stats(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        resolved_id = await _seed_track(dao, pg_pool, album_id)
        await _seed_track(dao, pg_pool, album_id, artist="Jessica Pratt", title="Back, Baby")
        await dao.update_track_resolution(resolved_id, "local_match", spotify_url=_SPOTIFY_URL)
        stats = await dao.get_track_stats()
        assert stats == {"local_match": 1, "pending": 1, "total": 2}

    @pytest.mark.asyncio
    async def test_get_album_track_summary(self, dao, pg_pool) -> None:
        album_id = await _seed_album(dao, pg_pool)
        statuses = {
            "la paradoja": ("local_match", {"spotify_url": _SPOTIFY_URL}),
            "Back, Baby": ("api_match", {"deezer_url": _DEEZER_URL}),
            "Eras": ("pending", None),
            "Call Your Name": ("not_found", None),
        }
        for title, (status, extra) in statuses.items():
            track_id = await _seed_track(dao, pg_pool, album_id, title=title)
            if status != "pending":
                await dao.update_track_resolution(track_id, status, **(extra or {}))
        summary = await dao.get_album_track_summary(album_id)
        assert summary == {
            "total": 4,
            "resolved": 2,
            "not_found": 1,
            "pending": 1,
            "on_streaming": True,
        }

    @pytest.mark.asyncio
    async def test_get_album_track_summary_counts_only_resolved_statuses(
        self, dao, pg_pool
    ) -> None:
        """``resolved`` is a direct count of the two resolved statuses, not a
        subtraction of the enumerated unresolved ones — a novel/unmapped
        status (a future resolution_status, or a raw pipeline write the
        subtraction list never learned about) must not silently inflate it."""
        album_id = await _seed_album(dao, pg_pool)
        track_id = await _seed_track(dao, pg_pool, album_id)
        await dao.update_track_resolution(track_id, "skipped")  # novel, unmapped status
        summary = await dao.get_album_track_summary(album_id)
        assert summary["total"] == 1
        assert summary["resolved"] == 0
        assert summary["on_streaming"] is False


@pytest.mark.pg
class TestCoverageBaseline:
    @pytest.mark.asyncio
    async def test_refresh_on_empty_catalog(self, dao, pg_pool) -> None:
        metrics = await dao.refresh_coverage_baseline()
        assert metrics == {
            "apple_url": 0,
            "spotify_url": 0,
            "deezer_url": 0,
            "albums": 0,
            "track_results": 0,
        }
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT metric, value FROM lml_cache.streaming_coverage_baseline"
            )
        assert {r["metric"]: r["value"] for r in rows} == metrics

    @pytest.mark.asyncio
    async def test_refresh_metric_keys_match_admin_constant(self, dao) -> None:
        """The floor keys the DAO writes are exactly the reader's
        ``routers/admin.py`` ``_STREAMING_COVERAGE_METRICS`` set — pinning the
        two hardcoded lists together so a future edit to one that forgets the
        other fails here instead of silently desyncing the export check."""
        floors = await dao.refresh_coverage_baseline()
        assert set(floors) == set(_STREAMING_COVERAGE_METRICS)

    @pytest.mark.asyncio
    async def test_refresh_computes_live_counts(self, dao, pg_pool) -> None:
        """The five metric keys mirror ``routers/admin.py``'s
        ``_STREAMING_COVERAGE_METRICS``; ``track_results`` counts only usable
        rows (resolved AND carrying a streaming URL)."""
        a_id = await _seed_album(dao, pg_pool)
        b_id = await _seed_album(dao, pg_pool, **_PRATT)
        await dao.update_result(a_id, "spotify", "found", url=_SPOTIFY_URL)
        await dao.update_result(b_id, "apple", "found", url=_APPLE_URL)
        usable_id = await _seed_track(dao, pg_pool, a_id)
        bare_id = await _seed_track(dao, pg_pool, a_id, title="Back, Baby")
        await dao.update_track_resolution(usable_id, "local_match", spotify_url=_SPOTIFY_URL)
        await dao.update_track_resolution(bare_id, "api_match")  # resolved, no URL: unusable
        metrics = await dao.refresh_coverage_baseline()
        assert metrics == {
            "apple_url": 1,
            "spotify_url": 1,
            "deezer_url": 0,
            "albums": 2,
            "track_results": 1,
        }

    @pytest.mark.asyncio
    async def test_refresh_ratchets_upward(self, dao, pg_pool) -> None:
        await _seed_album(dao, pg_pool)
        first = await dao.refresh_coverage_baseline()
        assert first["albums"] == 1
        await _seed_album(dao, pg_pool, **_PRATT)
        second = await dao.refresh_coverage_baseline()
        assert second["albums"] == 2

    @pytest.mark.asyncio
    async def test_refresh_lowering_holds_the_floor(self, dao, pg_pool) -> None:
        """A refresh computing LESS coverage than the stored floor must not
        lower it: the baseline is a monotonic high-water mark. Each metric
        ratchets up only (``GREATEST(live, stored)``), so a live regression —
        e.g. a sanctioned ``mark_track_false_positive`` demotion between runs —
        holds the floor rather than lowering it or aborting the whole refresh
        through PR A's guard. Regression *detection* is the read-side export
        check's job, not this writer's."""
        await _seed_album(dao, pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO lml_cache.streaming_coverage_baseline (metric, value) "
                "VALUES ('albums', 5)"
            )
        floors = await dao.refresh_coverage_baseline()
        assert floors["albums"] == 5  # held at the high-water mark, not lowered to live 1
        async with pg_pool.acquire() as conn:
            stored = await conn.fetchval(
                "SELECT value FROM lml_cache.streaming_coverage_baseline WHERE metric = 'albums'"
            )
        assert stored == 5


@pytest.mark.pg
class TestOptedInRequeue:
    @pytest.mark.asyncio
    async def test_opted_in_reset_requeues_album(self, dao, pg_pool) -> None:
        """The operator re-probe flow: an opted-in transaction may reset a
        collected probe to pending, and the DAO's anti-join scan picks the
        album back up."""
        album_id = await _seed_album(dao, pg_pool)
        await dao.update_result(album_id, "spotify", "found", url=_SPOTIFY_URL)
        assert await dao.get_pending("spotify") == []
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await opted_in(conn)
                await conn.execute(
                    "UPDATE lml_cache.streaming_album_service "
                    "SET status = 'pending', url = NULL "
                    "WHERE album_id = $1 AND service = 'spotify'",
                    album_id,
                )
        assert [r["id"] for r in await dao.get_pending("spotify")] == [album_id]
