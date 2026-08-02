"""Unit tests for scripts/bandcamp_pipeline.py's ``--phase album-search`` (LML#1069)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from clients.bandcamp import BandcampSearchUnavailableError
from scripts.streaming_availability.dedup import DeduplicatedAlbum
from scripts.streaming_availability.results_db import ResultsDB
from streaming.models import SourceMatch


def _make_album(
    normalized_artist: str = "stereolab",
    normalized_title: str = "aluminum tunes",
    display_artist: str = "Stereolab",
    display_title: str = "Aluminum Tunes",
    library_ids: list[int] | None = None,
    formats: list[str] | None = None,
    is_compilation: bool = False,
) -> DeduplicatedAlbum:
    return DeduplicatedAlbum(
        normalized_artist=normalized_artist,
        normalized_title=normalized_title,
        display_artist=display_artist,
        display_title=display_title,
        library_ids=library_ids if library_ids is not None else [1],
        formats=formats if formats is not None else ["cd"],
        genre="Rock",
        label="Duophonic",
        is_compilation=is_compilation,
    )


@pytest_asyncio.fixture
async def db():
    results_db = ResultsDB(":memory:")
    await results_db.connect()
    yield results_db
    await results_db.close()


_MATCH = SourceMatch(url="https://stereolab.bandcamp.com/album/aluminum-tunes", confidence=100.0)


class TestPhaseAlbumSearchHits:
    @pytest.mark.asyncio
    async def test_dry_run_reports_hit_without_writing(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=_MATCH)

        report = await phase_album_search(client, db, execute=False, verify_hits=False)

        assert report["hits"] == 1
        assert report["would_write"][0]["bandcamp_url"] == _MATCH.url
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_url"] is None  # dry-run: no write

    @pytest.mark.asyncio
    async def test_execute_writes_hit(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=_MATCH)

        report = await phase_album_search(client, db, execute=True, verify_hits=False)

        assert report["hits"] == 1
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_url"] == _MATCH.url
        assert rows[0]["bandcamp_status"] == "found"

    @pytest.mark.asyncio
    async def test_verify_hits_passes_writes_url(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=_MATCH)
        client.verify_album_page = AsyncMock(return_value=True)

        report = await phase_album_search(client, db, execute=True, verify_hits=True)

        assert report["hits"] == 1
        assert report["verify_failed"] == 0
        client.verify_album_page.assert_awaited_once_with(_MATCH.url, "Stereolab", "Aluminum Tunes")
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_url"] == _MATCH.url

    @pytest.mark.asyncio
    async def test_verify_hits_fails_skips_write(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=_MATCH)
        client.verify_album_page = AsyncMock(return_value=False)

        report = await phase_album_search(client, db, execute=True, verify_hits=True)

        assert report["hits"] == 0
        assert report["verify_failed"] == 1
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_url"] is None


class TestPhaseAlbumSearchMisses:
    @pytest.mark.asyncio
    async def test_miss_on_pending_row_marks_not_found_when_executing(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=None)

        report = await phase_album_search(client, db, execute=True, verify_hits=False)

        assert report["misses"] == 1
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_status"] == "not_found"

    @pytest.mark.asyncio
    async def test_miss_on_pending_row_dry_run_does_not_write(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=None)

        report = await phase_album_search(client, db, execute=False, verify_hits=False)

        assert report["misses"] == 1
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_status"] == "pending"

    @pytest.mark.asyncio
    async def test_miss_on_already_not_found_row_is_noop(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.mark_bandcamp_not_found(rows[0]["id"])

        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=None)

        report = await phase_album_search(
            client, db, include_not_found=True, execute=True, verify_hits=False
        )

        assert report["skipped"] == 1
        assert report["misses"] == 0


class TestPhaseAlbumSearchFetchFailure:
    @pytest.mark.asyncio
    async def test_fetch_failure_tallied_and_row_stays_pending(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(
            side_effect=BandcampSearchUnavailableError("boom")
        )

        report = await phase_album_search(client, db, execute=True, verify_hits=False)

        assert report["fetch_failed"] == 1
        assert report["hits"] == 0
        assert report["misses"] == 0
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_status"] == "pending"
        assert rows[0]["bandcamp_url"] is None


class TestPhaseAlbumSearchSelectorScoping:
    @pytest.mark.asyncio
    async def test_real_slug_row_never_touched(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums([_make_album()])
        rows = await db.get_pending("spotify", limit=10)
        await db.update_bandcamp_slug(rows[0]["id"], "stereolab")

        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=_MATCH)

        report = await phase_album_search(client, db, execute=True, verify_hits=False)

        assert report["hits"] == 0
        assert report["misses"] == 0
        client.find_album_match_via_search.assert_not_awaited()
        rows = await db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_status"] == "pending"
        assert rows[0]["bandcamp_url"] is None

    @pytest.mark.asyncio
    async def test_no_pending_rows_returns_zeroed_report(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        client = AsyncMock()
        report = await phase_album_search(client, db, execute=True, verify_hits=False)

        assert report["hits"] == 0
        assert report["would_write"] == []
        client.find_album_match_via_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_limit(self, db):
        from scripts.bandcamp_pipeline import phase_album_search

        await db.insert_albums(
            [
                _make_album(),
                _make_album(
                    normalized_artist="autechre",
                    normalized_title="confield",
                    display_artist="Autechre",
                    display_title="Confield",
                    library_ids=[3],
                ),
            ]
        )
        client = AsyncMock()
        client.find_album_match_via_search = AsyncMock(return_value=None)

        report = await phase_album_search(client, db, limit=1, execute=False, verify_hits=False)

        assert report["misses"] == 1


class TestArgValidation:
    """Flag-interaction validation the plan calls out explicitly: --execute
    is only valid with --phase album-search, and cannot combine with
    --dry-run."""

    def test_execute_with_album_search_is_valid(self):
        from scripts.bandcamp_pipeline import build_arg_parser, validate_args

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "album-search", "--execute"])
        validate_args(parser, args)  # must not raise

    def test_execute_with_dry_run_errors(self):
        from scripts.bandcamp_pipeline import build_arg_parser, validate_args

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "album-search", "--execute", "--dry-run"])
        with pytest.raises(SystemExit):
            validate_args(parser, args)

    def test_execute_with_non_album_search_phase_errors(self):
        from scripts.bandcamp_pipeline import build_arg_parser, validate_args

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "lookup", "--execute"])
        with pytest.raises(SystemExit):
            validate_args(parser, args)

    def test_dry_run_alone_with_album_search_is_a_noop_not_an_error(self):
        from scripts.bandcamp_pipeline import build_arg_parser, validate_args

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "album-search", "--dry-run"])
        validate_args(parser, args)  # must not raise

    def test_verify_hits_defaults_to_none(self):
        from scripts.bandcamp_pipeline import build_arg_parser

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "album-search"])
        assert args.verify_hits is None

    def test_verify_hits_explicit_true(self):
        from scripts.bandcamp_pipeline import build_arg_parser

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "album-search", "--verify-hits"])
        assert args.verify_hits is True

    def test_no_verify_hits_explicit_false(self):
        from scripts.bandcamp_pipeline import build_arg_parser

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "album-search", "--execute", "--no-verify-hits"])
        assert args.verify_hits is False


class TestMainAlbumSearchDispatch:
    @pytest.mark.asyncio
    async def test_execute_writes_and_report_json_reflects_it(self, tmp_path, monkeypatch):
        from scripts.bandcamp_pipeline import build_arg_parser, main, validate_args

        db_path = str(tmp_path / "results.db")
        setup_db = ResultsDB(db_path)
        await setup_db.connect()
        await setup_db.insert_albums([_make_album()])
        await setup_db.close()

        fake_client = AsyncMock()
        fake_client.find_album_match_via_search = AsyncMock(return_value=_MATCH)
        monkeypatch.setattr("scripts.bandcamp_pipeline.BandcampClient", lambda: fake_client)

        report_path = str(tmp_path / "report.json")
        parser = build_arg_parser()
        args = parser.parse_args(
            [
                "--phase",
                "album-search",
                "--execute",
                "--db-path",
                db_path,
                "--report-json",
                report_path,
            ]
        )
        validate_args(parser, args)
        await main(args)

        written = json.loads(Path(report_path).read_text())
        assert written["hits"] == 1

        verify_db = ResultsDB(db_path)
        await verify_db.connect()
        rows = await verify_db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_url"] == _MATCH.url
        await verify_db.close()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write(self, tmp_path, monkeypatch):
        from scripts.bandcamp_pipeline import build_arg_parser, main, validate_args

        db_path = str(tmp_path / "results.db")
        setup_db = ResultsDB(db_path)
        await setup_db.connect()
        await setup_db.insert_albums([_make_album()])
        await setup_db.close()

        fake_client = AsyncMock()
        fake_client.find_album_match_via_search = AsyncMock(return_value=_MATCH)
        monkeypatch.setattr("scripts.bandcamp_pipeline.BandcampClient", lambda: fake_client)

        parser = build_arg_parser()
        args = parser.parse_args(["--phase", "album-search", "--db-path", db_path])
        validate_args(parser, args)
        await main(args)

        verify_db = ResultsDB(db_path)
        await verify_db.connect()
        rows = await verify_db.get_pending("spotify", limit=10)
        assert rows[0]["bandcamp_url"] is None
        await verify_db.close()
