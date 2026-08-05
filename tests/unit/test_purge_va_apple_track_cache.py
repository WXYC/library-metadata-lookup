"""Unit tests for the LML#1139 V/A track-cache purge.

The arbiter (``classify``) and the recovery-CSV writer are pure, so the logic
that actually decides what gets deleted is tested here without a database. The
live DB path — wave cursoring, the ``ANY($2)`` array bind, the
``DELETE … RETURNING`` shape and its transaction — is exercised against a real
PostgreSQL in ``tests/integration/test_purge_va_apple_track_cache_pg.py``
(mirrors the split documented in
``tests/unit/test_seed_library_release_overrides.py``).
"""

from __future__ import annotations

import csv
import re
from contextlib import asynccontextmanager

import pytest

from scripts.purge_va_apple_track_cache import (
    SUPERSET_REGEX,
    _build_arg_parser,
    classify,
    purge,
    write_recovery_csv,
)

_NET = re.compile(SUPERSET_REGEX)


class TestSupersetProperty:
    """The coarse regex MUST be a superset of the arbiter, or guard-struck keys
    are silently never purged — a wrong deep-link serving forever from a
    hit-only, TTL-less table."""

    @pytest.mark.parametrize(
        "key",
        [
            "various artists - blues",
            "various artists - document records",
            "various artists & pan ron",
            "various",
            "v/a",
            "v/a - blues",
            "v.a.",
            # The dotless family the donor's `v\.a\.` regex missed. These are
            # arbiter-True, so a net that excluded them would leave them cached.
            "v.a",
            "v.a - jazz",
            "v.a 1998",
            "soundtrack",
            "soundtracks",
            "compilation",
        ],
    )
    def test_every_arbiter_positive_is_reachable_by_the_net(self, key: str):
        assert classify([key])[0] == [key], f"{key!r} should be arbiter-positive"
        assert _NET.search(key) is not None, f"{key!r} is purge-worthy but the net misses it"

    @pytest.mark.parametrize(
        "key",
        [
            "various production",
            "the various",
            "cagayano various artists",
            "soundtrack of our lives",
        ],
    )
    def test_net_negatives_reach_the_arbiter_and_are_rejected_there(self, key: str):
        """These prove the arbiter is load-bearing: each one DOES match the
        coarse net (so the SQL alone would delete it) and is excluded only
        because ``is_compilation_artist`` says no."""
        assert _NET.search(key) is not None
        purge_keys, keep_keys = classify([key])
        assert purge_keys == []
        assert keep_keys == [key]


class TestClassify:
    def test_splits_preserving_order(self):
        keys = ["various artists - blues", "the various", "v/a", "various production"]
        purge_keys, keep_keys = classify(keys)
        assert purge_keys == ["various artists - blues", "v/a"]
        assert keep_keys == ["the various", "various production"]

    def test_empty_input(self):
        assert classify([]) == ([], [])

    def test_empty_key_is_kept_not_purged(self):
        assert classify([""]) == ([], [""])


class TestWriteRecoveryCsv:
    def test_writes_full_tuple_with_header(self, tmp_path):
        out = tmp_path / "purged.csv"
        write_recovery_csv(
            out,
            [
                {
                    "service": "apple_music_track",
                    "artist_normalized": "various artists - blues",
                    "album_normalized": "blues classics",
                    "song_normalized": "im on my way",
                    "url": "https://music.apple.com/us/song/x/1",
                }
            ],
        )
        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        assert rows == [
            {
                "service": "apple_music_track",
                "artist_normalized": "various artists - blues",
                "album_normalized": "blues classics",
                "song_normalized": "im on my way",
                "url": "https://music.apple.com/us/song/x/1",
            }
        ]

    def test_refuses_to_overwrite_an_existing_file(self, tmp_path):
        """The recovery CSV is the ONLY record of a knowingly-over-deleting,
        irreversible DELETE. Truncating one wave's file with the next wave's
        rows destroys the earlier wave's recoverability, so a collision must
        abort loudly rather than silently clobber."""
        out = tmp_path / "purged.csv"
        out.write_text("wave 1 rows we must not lose\n", encoding="utf-8")

        with pytest.raises(FileExistsError, match="already exists"):
            write_recovery_csv(out, [_ROW])

        assert out.read_text(encoding="utf-8") == "wave 1 rows we must not lose\n"


class TestArgParser:
    def test_out_default_is_unique_per_invocation(self):
        """A fixed default path would have later waves clobber earlier ones.
        Two parses must not agree on a filename."""
        first = _build_arg_parser().parse_args([]).out
        second = _build_arg_parser().parse_args([]).out
        assert first != second
        assert first.endswith(".csv")

    @pytest.mark.parametrize("bad", ["-5", "-1"])
    def test_negative_limit_is_rejected_at_parse_time(self, bad: str):
        """``LIMIT $4`` with a negative bind makes asyncpg raise partway through
        a prod run; reject it before any connection is opened."""
        with pytest.raises(SystemExit):
            _build_arg_parser().parse_args(["--limit", bad])

    def test_zero_limit_still_means_all(self):
        assert _build_arg_parser().parse_args(["--limit", "0"]).limit == 0


class _FakeConn:
    """Minimal asyncpg-connection stand-in with transaction + fetch."""

    def __init__(self, parent: _FakePg):
        self._parent = parent

    def transaction(self):
        return _FakeTransaction(self._parent)

    async def fetch(self, query: str, *args):
        self._parent.executed.append((query, args))
        return list(self._parent._rows)


class _FakeTransaction:
    def __init__(self, parent: _FakePg):
        self._parent = parent

    async def __aenter__(self):
        self._parent.transaction_depth += 1
        self._parent.max_transaction_depth = max(
            self._parent.max_transaction_depth, self._parent.transaction_depth
        )
        return self

    async def __aexit__(self, *exc):
        self._parent.transaction_depth -= 1
        self._parent.committed = exc[0] is None
        # Ordering proof: the recovery artifact must already be on disk when
        # the DELETE commits, so a crash can never lose rows it did not record.
        if self._parent.out_path is not None:
            self._parent.csv_existed_at_commit = self._parent.out_path.exists()
        return False


class _FakePg:
    """Records the SQL/args it is handed so the purge's shape can be asserted."""

    def __init__(self, keys: list[str], rows: list[dict] | None = None):
        self._keys = keys
        self._rows = rows if rows is not None else []
        self.calls: list[tuple[str, tuple]] = []
        self.executed: list[tuple[str, tuple]] = []
        self.transaction_depth = 0
        self.max_transaction_depth = 0
        self.committed = False
        # Set by tests that want the ordering proof in `_FakeTransaction`.
        self.out_path = None
        # Snapshot of whether the CSV existed at the moment the transaction
        # closed — proves ordering rather than merely final state.
        self.csv_existed_at_commit: bool | None = None

    async def fetchall(self, query: str, *args):
        self.calls.append((query, args))
        if "SELECT DISTINCT artist_normalized" in query:
            return [{"artist_normalized": k} for k in self._keys]
        return self._rows

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self)


_ROW = {
    "service": "apple_music_track",
    "artist_normalized": "various artists - blues",
    "album_normalized": "a",
    "song_normalized": "b",
    "url": "https://music.apple.com/us/song/x/1",
}


class TestPurgeWave:
    @pytest.mark.asyncio
    async def test_dry_run_writes_csv_but_never_deletes(self, tmp_path):
        pg = _FakePg(["various artists - blues"], [_ROW])
        out = tmp_path / "w1.csv"

        deleted = await purge(
            pg, service="apple_music_track", out=out, limit=0, after="", execute=False
        )

        assert deleted == 0
        assert pg.executed == []
        assert out.exists(), "the recovery artifact must exist even on a dry-run"

    @pytest.mark.asyncio
    async def test_execute_deletes_scoped_to_service_and_arbitrated_keys(self, tmp_path):
        pg = _FakePg(["various artists - blues", "the various"], [_ROW])

        await purge(
            pg,
            service="apple_music_track",
            out=tmp_path / "w1.csv",
            limit=0,
            after="",
            execute=True,
        )

        assert len(pg.executed) == 1
        sql, args = pg.executed[0]
        assert "DELETE FROM lml_cache.track_streaming_url_cache" in sql
        # Parameterized on both axes — no interpolation anywhere.
        assert "$1" in sql and "$2" in sql
        assert args[0] == "apple_music_track"
        # `the various` matched the coarse net but the arbiter rejected it, so
        # it must not appear in the DELETE's key array.
        assert args[1] == ["various artists - blues"]

    @pytest.mark.asyncio
    async def test_execute_recovers_from_the_delete_itself_in_one_transaction(self, tmp_path):
        """The recovery CSV must be built from ``DELETE … RETURNING`` inside a
        single transaction, not from a preceding autocommit SELECT.

        With two independent statements on two pooled connections, live
        ``/lookup`` traffic can insert a row for one of ``purge_keys`` in the
        gap — the guard does not block album-CONSTRAINED V/A cache writes — and
        that row is then deleted with no CSV entry. That is exactly the
        unrecoverable loss the CSV exists to prevent.
        """
        out = tmp_path / "w1.csv"
        pg = _FakePg(["various artists - blues"], [_ROW])
        pg.out_path = out

        deleted = await purge(
            pg, service="apple_music_track", out=out, limit=0, after="", execute=True
        )

        # Exactly one mutating statement, and it returns the rows it removed.
        assert len(pg.executed) == 1
        sql, _ = pg.executed[0]
        assert "RETURNING" in sql
        # No separate pre-DELETE row SELECT on the execute path — the key scan
        # is the only `fetchall`, so there is no gap to race.
        assert len(pg.calls) == 1
        assert "SELECT DISTINCT artist_normalized" in pg.calls[0][0]
        # Wrapped in a transaction, with the CSV already durable at commit.
        assert pg.max_transaction_depth == 1
        assert pg.committed is True
        assert pg.csv_existed_at_commit is True
        # The count reported is the DELETE's own row count.
        assert deleted == 1
        assert len(list(csv.DictReader(out.open(encoding="utf-8")))) == 1

    @pytest.mark.asyncio
    async def test_all_rejected_wave_still_advances_and_deletes_nothing(self, tmp_path):
        """Anti-stall: a wave whose keys are ALL rejected must not DELETE and
        must not leave the operator re-selecting the same window forever. The
        cursor advances past every key EXAMINED, which is what guarantees
        progress."""
        pg = _FakePg(["the various", "various production"], [])

        deleted = await purge(
            pg,
            service="apple_music_track",
            out=tmp_path / "w1.csv",
            limit=0,
            after="",
            execute=True,
        )

        assert deleted == 0
        assert pg.executed == []

    @pytest.mark.asyncio
    async def test_limit_and_after_are_bound_as_parameters(self, tmp_path):
        pg = _FakePg(["various artists - blues"], [_ROW])

        await purge(
            pg,
            service="apple_music_track",
            out=tmp_path / "w2.csv",
            limit=500,
            after="various artists - a",
            execute=False,
        )

        sql, args = pg.calls[0]
        assert "LIMIT $4" in sql
        assert "artist_normalized > $3" in sql
        assert args == ("apple_music_track", SUPERSET_REGEX, "various artists - a", 500)

    @pytest.mark.asyncio
    async def test_empty_wave_is_a_clean_noop(self, tmp_path):
        pg = _FakePg([], [])
        deleted = await purge(
            pg,
            service="apple_music_track",
            out=tmp_path / "w3.csv",
            limit=0,
            after="zzz",
            execute=True,
        )
        assert deleted == 0
        assert pg.executed == []


class TestServiceKeyIsDerived:
    def test_module_never_hardcodes_the_wrong_service_literal(self):
        """The issue body said ``service = 'apple_music'``, which cannot exist in
        this table (the real key is ``apple_music_track``, pinned by a CHECK).
        The script must import the constant rather than spell either literal."""
        source = (
            __import__("pathlib").Path(__file__).parents[2]
            / "scripts"
            / "purge_va_apple_track_cache.py"
        ).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith(("*", "#"))
        )
        assert "APPLE_MUSIC_TRACK_SERVICE" in code
        assert '"apple_music"' not in code
        assert "'apple_music'" not in code
