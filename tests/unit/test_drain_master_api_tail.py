"""Unit tests for the LML#858 Discogs-API master-tail drain.

The drain resolves the ``UNRESOLVED`` tail from ``resolve_master_overrides``
(masters with no cached version and no local ``main_release_id``) by calling the
Discogs API: ``get_master`` → ``.main_release_id``, then ``get_release`` to warm
the PG cache and confirm a non-blank tracklist before the master is pinned.

These tests exercise the pure decision core and the resumable checkpoint loop
with a fake service — no DB, no network.
"""

from __future__ import annotations

import types

import pytest

from scripts.drain_master_api_tail import (
    STATUS_DEAD,
    STATUS_ERROR,
    STATUS_NO_MAIN,
    STATUS_RESOLVED,
    STATUS_TRACKLESS,
    TERMINAL_STATUSES,
    MasterDrainOutcome,
    append_checkpoint,
    build_seed_rows,
    drain_master,
    group_cards_by_master,
    load_checkpoint,
    load_unresolved,
    run_drain,
    write_seed_csv,
)


def _master(main_release_id):
    return types.SimpleNamespace(main_release_id=main_release_id)


def _release(n_tracks):
    # Title-bearing tracks: the drain counts real (non-blank-title) tracks, so a
    # stand-in track must carry a title to be counted (matches ``TrackItem``).
    return types.SimpleNamespace(
        tracklist=[types.SimpleNamespace(title=f"t{i}") for i in range(n_tracks)]
    )


def _release_untitled(n_tracks):
    # A release whose rows are all blank-title heading/index markers — a
    # non-empty tracklist that carries no real track (the Phase-2 invariant
    # drops these), so the drain must treat it as trackless.
    return types.SimpleNamespace(
        tracklist=[types.SimpleNamespace(title="") for _ in range(n_tracks)]
    )


class FakeService:
    """Duck-typed stand-in for DiscogsService.get_master / get_release."""

    def __init__(self, masters=None, releases=None, raise_on=None):
        self.masters = masters or {}
        self.releases = releases or {}
        self.raise_on = raise_on or set()  # master_ids that raise (transient)
        self.master_calls: list[int] = []
        self.release_calls: list[int] = []

    async def get_master(self, master_id):
        self.master_calls.append(master_id)
        if master_id in self.raise_on:
            raise RuntimeError("transient breaker shed")
        return self.masters.get(master_id)

    async def get_release(self, release_id):
        self.release_calls.append(release_id)
        return self.releases.get(release_id)


class TestGroupCardsByMaster:
    def test_dedups_masters_and_keeps_all_cards(self):
        rows = [(1, 100), (2, 100), (3, 200)]
        assert group_cards_by_master(rows) == {100: [1, 2], 200: [3]}


class TestDrainMaster:
    @pytest.mark.asyncio
    async def test_resolved(self):
        svc = FakeService(masters={10: _master(500)}, releases={500: _release(3)})
        oc = await drain_master(svc, 10)
        assert oc == MasterDrainOutcome(10, 500, 3, STATUS_RESOLVED)

    @pytest.mark.asyncio
    async def test_no_main_release_skips_get_release(self):
        svc = FakeService(masters={10: _master(None)})
        oc = await drain_master(svc, 10)
        assert oc == MasterDrainOutcome(10, None, None, STATUS_NO_MAIN)
        assert svc.release_calls == []  # never fetched a release

    @pytest.mark.asyncio
    async def test_dead_master_returns_dead(self):
        svc = FakeService(masters={})  # get_master -> None
        oc = await drain_master(svc, 10)
        assert oc == MasterDrainOutcome(10, None, None, STATUS_DEAD)

    @pytest.mark.asyncio
    async def test_trackless_main_release(self):
        svc = FakeService(masters={10: _master(500)}, releases={500: _release(0)})
        oc = await drain_master(svc, 10)
        assert oc == MasterDrainOutcome(10, 500, 0, STATUS_TRACKLESS)

    @pytest.mark.asyncio
    async def test_release_fetch_error(self):
        svc = FakeService(masters={10: _master(500)}, releases={})  # get_release -> None
        oc = await drain_master(svc, 10)
        assert oc == MasterDrainOutcome(10, 500, None, STATUS_ERROR)

    @pytest.mark.asyncio
    async def test_blank_title_tracklist_is_trackless(self):
        # A non-empty tracklist made only of blank-title rows (headings/index
        # markers) is not a real tracklist and must not be pinned — matching the
        # resolver's Phase-2 invariant that drops empty-normalized-title
        # candidates. Counting raw ``len(tracklist)`` would wrongly resolve it.
        svc = FakeService(masters={10: _master(500)}, releases={500: _release_untitled(3)})
        oc = await drain_master(svc, 10)
        assert oc == MasterDrainOutcome(10, 500, 0, STATUS_TRACKLESS)


class TestBuildSeedRows:
    def test_expands_only_resolved_masters_per_card(self):
        outcomes = {
            100: MasterDrainOutcome(100, 500, 3, STATUS_RESOLVED),
            200: MasterDrainOutcome(200, None, None, STATUS_NO_MAIN),
            300: MasterDrainOutcome(300, 700, 0, STATUS_TRACKLESS),
        }
        cards_by_master = {100: [1, 2], 200: [3], 300: [4]}
        rows = build_seed_rows(outcomes, cards_by_master)
        # Only the resolved master expands, one row per card, pinning main_release.
        assert rows == [(1, 500, 100), (2, 500, 100)]


class TestCheckpoint:
    def test_round_trip_and_last_write_wins(self, tmp_path):
        path = tmp_path / "sub" / "ckpt.jsonl"
        append_checkpoint(path, MasterDrainOutcome(10, None, None, STATUS_DEAD))
        append_checkpoint(path, MasterDrainOutcome(20, 500, 3, STATUS_RESOLVED))
        # A retry of master 10 that now resolves — last line must win.
        append_checkpoint(path, MasterDrainOutcome(10, 900, 5, STATUS_RESOLVED))
        loaded = load_checkpoint(path)
        assert loaded[10] == MasterDrainOutcome(10, 900, 5, STATUS_RESOLVED)
        assert loaded[20] == MasterDrainOutcome(20, 500, 3, STATUS_RESOLVED)

    def test_missing_file_is_empty(self, tmp_path):
        assert load_checkpoint(tmp_path / "nope.jsonl") == {}

    def test_corrupt_trailing_line_is_skipped(self, tmp_path):
        # A hard kill (OOM/SIGKILL/disk-full) mid-append can leave a truncated
        # final JSON line. Resume must skip it and still recover every complete
        # prior outcome — a partial write must never strand the whole drain.
        path = tmp_path / "ckpt.jsonl"
        append_checkpoint(path, MasterDrainOutcome(10, 500, 3, STATUS_RESOLVED))
        append_checkpoint(path, MasterDrainOutcome(20, 600, 4, STATUS_NO_MAIN))
        with path.open("a", encoding="utf-8") as f:
            f.write('{"master_id": 30, "main_rele')  # truncated, no newline
        loaded = load_checkpoint(path)  # must not raise
        assert loaded[10] == MasterDrainOutcome(10, 500, 3, STATUS_RESOLVED)
        assert loaded[20] == MasterDrainOutcome(20, 600, 4, STATUS_NO_MAIN)
        assert 30 not in loaded

    def test_line_missing_status_is_skipped(self, tmp_path):
        # A well-formed JSON object lacking the required ``status`` key is also
        # malformed for our schema and must be skipped, not raise a KeyError.
        path = tmp_path / "ckpt.jsonl"
        append_checkpoint(path, MasterDrainOutcome(10, 500, 3, STATUS_RESOLVED))
        with path.open("a", encoding="utf-8") as f:
            f.write('{"master_id": 40, "main_release_id": 700}\n')
        loaded = load_checkpoint(path)
        assert set(loaded) == {10}

    def test_terminal_statuses_exclude_retryable(self):
        assert STATUS_RESOLVED in TERMINAL_STATUSES
        assert STATUS_NO_MAIN in TERMINAL_STATUSES
        assert STATUS_TRACKLESS in TERMINAL_STATUSES
        # DEAD + ERROR are ambiguous/transient -> retried on re-run.
        assert STATUS_DEAD not in TERMINAL_STATUSES
        assert STATUS_ERROR not in TERMINAL_STATUSES


class TestRunDrain:
    @pytest.mark.asyncio
    async def test_drains_todo_and_checkpoints(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        svc = FakeService(
            masters={100: _master(500), 200: _master(None)},
            releases={500: _release(4)},
        )
        cards_by_master = {100: [1, 2], 200: [3]}
        done = await run_drain(svc, cards_by_master, ckpt, concurrency=2)
        assert done[100].status == STATUS_RESOLVED
        assert done[200].status == STATUS_NO_MAIN
        # Checkpoint persisted both.
        assert set(load_checkpoint(ckpt)) == {100, 200}

    @pytest.mark.asyncio
    async def test_resume_skips_terminal_and_retries_nonterminal(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        # Seed the checkpoint: 100 terminal (resolved), 200 non-terminal (dead).
        append_checkpoint(ckpt, MasterDrainOutcome(100, 500, 4, STATUS_RESOLVED))
        append_checkpoint(ckpt, MasterDrainOutcome(200, None, None, STATUS_DEAD))
        svc = FakeService(masters={200: _master(600)}, releases={600: _release(2)})
        cards_by_master = {100: [1], 200: [2]}
        done = await run_drain(svc, cards_by_master, ckpt, concurrency=2)
        # 100 skipped (terminal) — never re-fetched; 200 retried and now resolves.
        assert svc.master_calls == [200]
        assert done[200].status == STATUS_RESOLVED

    @pytest.mark.asyncio
    async def test_second_pass_is_a_noop(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        svc = FakeService(masters={100: _master(500)}, releases={500: _release(4)})
        cards_by_master = {100: [1]}
        await run_drain(svc, cards_by_master, ckpt, concurrency=1)
        svc2 = FakeService(masters={100: _master(500)}, releases={500: _release(4)})
        await run_drain(svc2, cards_by_master, ckpt, concurrency=1)
        assert svc2.master_calls == []  # everything terminal — no API calls

    @pytest.mark.asyncio
    async def test_transient_exception_is_not_checkpointed(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        svc = FakeService(masters={100: _master(500)}, releases={500: _release(4)}, raise_on={100})
        done = await run_drain(svc, {100: [1]}, ckpt, concurrency=1)
        # A raise leaves the master absent from the checkpoint -> retried next run.
        assert 100 not in done
        assert load_checkpoint(ckpt) == {}

    @pytest.mark.asyncio
    async def test_zero_concurrency_raises_instead_of_hanging(self, tmp_path):
        # argparse accepts --concurrency 0; without a guard run_drain would build
        # asyncio.Semaphore(0), which no worker can acquire, hanging the drain
        # forever while holding the DB pool. Fail loud and fast instead.
        svc = FakeService(masters={100: _master(500)}, releases={500: _release(2)})
        with pytest.raises(ValueError):
            await run_drain(svc, {100: [1]}, tmp_path / "c.jsonl", concurrency=0)
        assert svc.master_calls == []  # never started a worker

    @pytest.mark.asyncio
    async def test_negative_limit_raises_instead_of_slicing(self, tmp_path):
        # --limit is a smoke-test cap; a negative value slices todo[:-1] and would
        # silently drain all-but-one of the tail against the prod token — the
        # opposite of a small batch. Guard it symmetrically with concurrency.
        svc = FakeService(
            masters={n: _master(n * 10) for n in (100, 200, 300)},
            releases={n * 10: _release(2) for n in (100, 200, 300)},
        )
        cards = {100: [1], 200: [2], 300: [3]}
        with pytest.raises(ValueError):
            await run_drain(svc, cards, tmp_path / "c.jsonl", limit=-1)
        assert svc.master_calls == []  # never started a worker

    @pytest.mark.asyncio
    async def test_limit_caps_masters_processed(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        svc = FakeService(
            masters={n: _master(n * 10) for n in (100, 200, 300)},
            releases={n * 10: _release(2) for n in (100, 200, 300)},
        )
        cards_by_master = {100: [1], 200: [2], 300: [3]}
        done = await run_drain(svc, cards_by_master, ckpt, concurrency=1, limit=2)
        assert len(done) == 2  # only two masters drained this run


class TestLoadUnresolved:
    def test_parses_pairs_and_skips_malformed(self, tmp_path):
        # Ids are parsed with the shared parse_positive_int (as the resolver and
        # seeder do): a blank or non-integer cell skips the row rather than
        # aborting the whole rate-limited drain with a bare ValueError.
        p = tmp_path / "u.csv"
        p.write_text(
            "card_catalog_id,master_id\n1,100\n,200\n3,notanint\n0,400\n4,400\n",
            encoding="utf-8",
        )
        assert load_unresolved(p) == [(1, 100), (4, 400)]

    def test_reads_utf8_sig_bom(self, tmp_path):
        # A BOM-prefixed Excel export must still match the header (utf-8-sig).
        p = tmp_path / "u.csv"
        p.write_text("\ufeffcard_catalog_id,master_id\n5,500\n", encoding="utf-8")
        assert load_unresolved(p) == [(5, 500)]


class TestWriteSeedCsvSchema:
    def test_header_and_column_order_match_seeder(self, tmp_path):
        # The seeder reads card_catalog_id + discogs_release_id by NAME, so a
        # column-order regression would silently pin the wrong release. Pin the
        # emitted header + row layout as a guard.
        import csv as _csv

        p = tmp_path / "seed.csv"
        written = write_seed_csv(p, [(7, 500, 100)])
        assert written == 1
        rows = list(_csv.DictReader(p.open()))
        assert rows[0]["card_catalog_id"] == "7"
        assert rows[0]["discogs_release_id"] == "500"
        assert rows[0]["master_id"] == "100"
