"""Unit tests for the library-release override seeder (LML#850).

The seeder's load-bearing, testable core: parse the verified CSV, drop invalid
pins, dedupe by library id, and upsert with the source-guarded ``ON CONFLICT
(library_id) DO UPDATE`` — idempotent, dry-run by default. The live DB
connection is exercised by the ``@pytest.mark.pg`` integration suite; here PG
is mocked.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scripts.seed_library_release_overrides import (
    OverrideRow,
    load_rows,
    seed_overrides,
)

_CSV = """card_catalog_id,discogs_release_id,artist,title,wxyc_genre,format,source_file
17,437503,Above the Law,Murder Rap,Hiphop,vinyl,x.xlsx
28,9590,John Acquaviva,Mainhatten Sound,Hiphop,cd,x.xlsx
99,0,Bad Pin,Zero Release,Rock,cd,x.xlsx
100,,No Release,Empty,Rock,cd,x.xlsx
28,12345,John Acquaviva,Dup Row Wins,Hiphop,cd,x.xlsx
"""


def _write_csv(tmp_path, text: str = _CSV):
    p = tmp_path / "phase1.csv"
    p.write_text(text)
    return p


class TestLoadRows:
    def test_parses_valid_rows(self, tmp_path):
        rows = load_rows(_write_csv(tmp_path))
        by_id = {r.library_id: r.discogs_release_id for r in rows}
        assert by_id[17] == 437503

    def test_drops_zero_and_empty_release_ids(self, tmp_path):
        rows = load_rows(_write_csv(tmp_path))
        ids = {r.library_id for r in rows}
        # library 99 (release 0) and 100 (empty) are invalid pins — excluded.
        assert 99 not in ids
        assert 100 not in ids

    def test_dedupes_by_library_id_last_wins(self, tmp_path):
        # library 28 appears twice; the last row wins (a later hand-correction
        # supersedes an earlier one).
        rows = load_rows(_write_csv(tmp_path))
        by_id = {r.library_id: r.discogs_release_id for r in rows}
        assert by_id[28] == 12345

    def test_returns_override_row_instances(self, tmp_path):
        rows = load_rows(_write_csv(tmp_path))
        assert all(isinstance(r, OverrideRow) for r in rows)


@pytest.mark.asyncio
class TestSeedOverrides:
    async def test_dry_run_writes_nothing(self):
        conn = AsyncMock()
        rows = [OverrideRow(library_id=17, discogs_release_id=437503)]

        written = await seed_overrides(conn, rows, execute=False, source="alex-l-2026")

        assert written == 0
        conn.executemany.assert_not_awaited()
        conn.execute.assert_not_awaited()

    async def test_execute_upserts_on_conflict(self):
        conn = AsyncMock()
        rows = [
            OverrideRow(library_id=17, discogs_release_id=437503),
            OverrideRow(library_id=28, discogs_release_id=12345),
        ]

        written = await seed_overrides(conn, rows, execute=True, source="alex-l-2026")

        assert written == 2
        assert conn.executemany.await_count == 1
        sql = conn.executemany.await_args.args[0]
        assert "INSERT INTO lml_cache.library_release_override" in sql
        assert "ON CONFLICT (library_id) DO UPDATE" in sql
        # Data-safety guard: the UPDATE only fires for rows this source owns, so a
        # re-run never clobbers a different-source (e.g. MD) hand-correction.
        assert "WHERE library_release_override.source = EXCLUDED.source" in sql
        # The source is stamped for reversibility.
        params = conn.executemany.await_args.args[1]
        assert all(p[2] == "alex-l-2026" for p in params)

    async def test_empty_rows_is_noop(self):
        conn = AsyncMock()
        written = await seed_overrides(conn, [], execute=True, source="alex-l-2026")
        assert written == 0
        conn.executemany.assert_not_awaited()
