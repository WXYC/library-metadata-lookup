"""Unit tests for the BS#1631 off-prod Apple-Music-URL resolver.

The resolver drains the still-null ``album_metadata`` tail by calling LML's exact
album-level match path — ``AppleMusicClient.find_album_match(artist, album)`` (the
same call the async warm uses, ``entity/streaming_url_cache.py:350``) — off-prod at
bounded concurrency, and emits ``(album_id, apple_music_url)`` for accepted matches.

These tests exercise the pure decision core and the resumable checkpoint loop with a
fake client — no network, no Apple calls.
"""

from __future__ import annotations

import types

import pytest

from scripts.resolve_apple_urls import (
    STATUS_API_ERROR,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    TERMINAL_STATUSES,
    AlbumResolveOutcome,
    _build_apple_client,
    _RateGate,
    append_checkpoint,
    load_candidates,
    load_checkpoint,
    min_interval_seconds,
    resolve_one,
    run_resolve,
    write_url_tsv,
)


class FakeAppleClient:
    """Duck-typed stand-in for AppleMusicClient.find_album_match.

    ``matches`` maps ``(artist, title) -> url``; a missing key resolves to a
    ``None`` match (no Apple album). ``raise_on`` names ``(artist, title)`` pairs
    that raise (a transient Apple API error).
    """

    def __init__(self, matches=None, raise_on=None):
        self.matches = matches or {}
        self.raise_on = raise_on or set()
        self.album_match_calls: list[tuple[str, str]] = []
        self.track_calls: list = []

    async def find_album_match(self, artist, title):
        self.album_match_calls.append((artist, title))
        if (artist, title) in self.raise_on:
            raise RuntimeError("apple api 500")
        url = self.matches.get((artist, title))
        return types.SimpleNamespace(url=url) if url else None

    async def find_track_metadata(self, *args, **kwargs):
        # The album-phase capture path is album-level only. If the resolver ever
        # reaches for the track path it would diverge from what the warm caches.
        self.track_calls.append((args, kwargs))
        raise AssertionError("resolver must call find_album_match, not the track path")


class TestResolveOne:
    @pytest.mark.asyncio
    async def test_accepted_match_emits_url(self):
        url = "https://music.apple.com/us/album/doga/1"
        client = FakeAppleClient(matches={("Juana Molina", "DOGA"): url})
        oc = await resolve_one(client, 42, "Juana Molina", "DOGA")
        assert oc == AlbumResolveOutcome(42, url, STATUS_MATCHED)

    @pytest.mark.asyncio
    async def test_none_match_is_no_match(self):
        client = FakeAppleClient(matches={})  # no Apple album
        oc = await resolve_one(client, 42, "Obscure", "Nowhere")
        assert oc == AlbumResolveOutcome(42, None, STATUS_NO_MATCH)

    @pytest.mark.asyncio
    async def test_api_error_is_counted_not_raised(self):
        client = FakeAppleClient(raise_on={("Juana Molina", "DOGA")})
        oc = await resolve_one(client, 42, "Juana Molina", "DOGA")
        assert oc == AlbumResolveOutcome(42, None, STATUS_API_ERROR)

    @pytest.mark.asyncio
    async def test_match_with_empty_url_is_no_match(self):
        # A SourceMatch that clears the fuzz floor but carries no URL must not be
        # emitted as an empty-URL row; resolve_one demotes it to no_match.
        class EmptyUrlClient:
            async def find_album_match(self, artist, title):
                return types.SimpleNamespace(url="")

        oc = await resolve_one(EmptyUrlClient(), 7, "A", "b")
        assert oc == AlbumResolveOutcome(7, None, STATUS_NO_MATCH)

    @pytest.mark.asyncio
    async def test_swallowed_transient_none_is_terminal_no_match(self):
        # Parity note (module docstring): the real AppleMusicClient SWALLOWS
        # transient HTTP errors (exhausted 429/5xx, transport, bad JSON) to None,
        # indistinguishable from a genuine miss. Those settle as no_match
        # (terminal), NOT the retryable api_error — so a re-run does not re-probe
        # them from the same checkpoint. This pins that contract.
        class SwallowingClient:
            async def find_album_match(self, artist, title):
                return None  # transient failure, swallowed by the client

        oc = await resolve_one(SwallowingClient(), 9, "A", "b")
        assert oc == AlbumResolveOutcome(9, None, STATUS_NO_MATCH)
        assert oc.status in TERMINAL_STATUSES

    @pytest.mark.asyncio
    async def test_uses_album_level_match_not_track(self):
        # Match parity: the album-phase warm captures apple_music_url solely via
        # find_album_match. Assert the resolver calls exactly that, never the
        # track path (FakeAppleClient.find_track_metadata raises if touched).
        client = FakeAppleClient(matches={("Sessa", "Grandeza"): "u"})
        await resolve_one(client, 1, "Sessa", "Grandeza")
        assert client.album_match_calls == [("Sessa", "Grandeza")]
        assert client.track_calls == []


class TestCheckpoint:
    def test_round_trip_and_last_write_wins(self, tmp_path):
        path = tmp_path / "sub" / "ckpt.jsonl"
        append_checkpoint(path, AlbumResolveOutcome(10, None, STATUS_API_ERROR))
        append_checkpoint(path, AlbumResolveOutcome(20, "u20", STATUS_MATCHED))
        # A retry of 10 that now matches — last line wins.
        append_checkpoint(path, AlbumResolveOutcome(10, "u10", STATUS_MATCHED))
        loaded = load_checkpoint(path)
        assert loaded[10] == AlbumResolveOutcome(10, "u10", STATUS_MATCHED)
        assert loaded[20] == AlbumResolveOutcome(20, "u20", STATUS_MATCHED)

    def test_missing_file_is_empty(self, tmp_path):
        assert load_checkpoint(tmp_path / "nope.jsonl") == {}

    def test_corrupt_trailing_line_is_skipped(self, tmp_path):
        path = tmp_path / "ckpt.jsonl"
        append_checkpoint(path, AlbumResolveOutcome(10, "u10", STATUS_MATCHED))
        with path.open("a", encoding="utf-8") as f:
            f.write('{"album_id": 30, "apple_mus')  # truncated
        loaded = load_checkpoint(path)  # must not raise
        assert loaded[10] == AlbumResolveOutcome(10, "u10", STATUS_MATCHED)
        assert 30 not in loaded

    def test_terminal_statuses_exclude_api_error(self):
        assert STATUS_MATCHED in TERMINAL_STATUSES
        assert STATUS_NO_MATCH in TERMINAL_STATUSES
        # api_error is transient -> retried on re-run.
        assert STATUS_API_ERROR not in TERMINAL_STATUSES


class TestRunResolve:
    @pytest.mark.asyncio
    async def test_resolves_and_checkpoints(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        client = FakeAppleClient(matches={("A", "a1"): "urlA"})
        candidates = [(100, "A", "a1"), (200, "B", "b1")]
        done = await run_resolve(client, candidates, ckpt, concurrency=2)
        assert done[100] == AlbumResolveOutcome(100, "urlA", STATUS_MATCHED)
        assert done[200] == AlbumResolveOutcome(200, None, STATUS_NO_MATCH)
        assert set(load_checkpoint(ckpt)) == {100, 200}

    @pytest.mark.asyncio
    async def test_api_error_does_not_abort_run(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        client = FakeAppleClient(matches={("C", "c1"): "urlC"}, raise_on={("A", "a1")})
        candidates = [(100, "A", "a1"), (300, "C", "c1")]
        done = await run_resolve(client, candidates, ckpt, concurrency=1)
        # The erroring candidate is recorded transient; the other still resolves.
        assert done[100].status == STATUS_API_ERROR
        assert done[300] == AlbumResolveOutcome(300, "urlC", STATUS_MATCHED)

    @pytest.mark.asyncio
    async def test_resume_skips_terminal_and_retries_api_error(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        append_checkpoint(ckpt, AlbumResolveOutcome(100, "u100", STATUS_MATCHED))
        append_checkpoint(ckpt, AlbumResolveOutcome(200, None, STATUS_API_ERROR))
        client = FakeAppleClient(matches={("B", "b1"): "u200"})
        candidates = [(100, "A", "a1"), (200, "B", "b1")]
        done = await run_resolve(client, candidates, ckpt, concurrency=2)
        # 100 terminal -> never re-called; 200 transient -> retried and now matches.
        assert client.album_match_calls == [("B", "b1")]
        assert done[200] == AlbumResolveOutcome(200, "u200", STATUS_MATCHED)

    @pytest.mark.asyncio
    async def test_dry_run_emits_no_output_but_still_tallies(self, tmp_path):
        out = tmp_path / "out.tsv"
        ckpt = tmp_path / "ckpt.jsonl"
        client = FakeAppleClient(matches={("A", "a1"): "urlA"})
        done = await run_resolve(
            client, [(100, "A", "a1")], ckpt, concurrency=1, dry_run=True, out_path=out
        )
        assert done[100].status == STATUS_MATCHED  # resolved + tallied
        assert not out.exists()  # but nothing emitted

    @pytest.mark.asyncio
    async def test_writes_output_tsv_when_all_candidates_already_terminal(self, tmp_path):
        # Regression: the documented workflow is a --dry-run pass (which
        # checkpoints every candidate to a terminal state) followed by the real
        # run to emit the TSV. On that second run ``todo`` is empty, but the
        # output TSV must still be written from the checkpoint — otherwise the
        # BS fill-only apply has nothing to consume. Also covers re-running to
        # regenerate a deleted/stale TSV from an already-complete checkpoint.
        ckpt = tmp_path / "ckpt.jsonl"
        out = tmp_path / "out.tsv"
        append_checkpoint(ckpt, AlbumResolveOutcome(100, "u100", STATUS_MATCHED))
        append_checkpoint(ckpt, AlbumResolveOutcome(200, None, STATUS_NO_MATCH))
        client = FakeAppleClient()
        candidates = [(100, "A", "a1"), (200, "B", "b1")]
        await run_resolve(client, candidates, ckpt, concurrency=2, out_path=out)
        assert client.album_match_calls == []  # nothing re-resolved, budget untouched
        lines = out.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "album_id\tapple_music_url"
        assert lines[1] == "100\tu100"
        assert len(lines) == 2  # only the matched row

    @pytest.mark.asyncio
    async def test_all_terminal_dry_run_still_emits_nothing(self, tmp_path):
        # The mirror of the regression above: an all-terminal checkpoint under
        # --dry-run must still emit no TSV.
        ckpt = tmp_path / "ckpt.jsonl"
        out = tmp_path / "out.tsv"
        append_checkpoint(ckpt, AlbumResolveOutcome(100, "u100", STATUS_MATCHED))
        client = FakeAppleClient()
        await run_resolve(
            client, [(100, "A", "a1")], ckpt, concurrency=1, dry_run=True, out_path=out
        )
        assert not out.exists()

    @pytest.mark.asyncio
    async def test_writes_output_tsv_end_to_end_after_real_resolve(self, tmp_path):
        # The script's actual deliverable path: a non-dry-run resolve with an
        # out_path writes the (album_id, url) TSV. Previously untested end-to-end.
        ckpt = tmp_path / "ckpt.jsonl"
        out = tmp_path / "out.tsv"
        client = FakeAppleClient(matches={("A", "a1"): "urlA"})
        candidates = [(100, "A", "a1"), (200, "B", "b1")]
        await run_resolve(client, candidates, ckpt, concurrency=2, out_path=out)
        lines = out.read_text(encoding="utf-8").splitlines()
        assert lines == ["album_id\tapple_music_url", "100\turlA"]

    @pytest.mark.asyncio
    async def test_zero_concurrency_raises_instead_of_hanging(self, tmp_path):
        client = FakeAppleClient()
        with pytest.raises(ValueError):
            await run_resolve(client, [(1, "A", "a")], tmp_path / "c.jsonl", concurrency=0)
        assert client.album_match_calls == []

    @pytest.mark.asyncio
    async def test_negative_limit_raises_instead_of_slicing(self, tmp_path):
        client = FakeAppleClient()
        candidates = [(n, "A", str(n)) for n in (1, 2, 3)]
        with pytest.raises(ValueError):
            await run_resolve(client, candidates, tmp_path / "c.jsonl", limit=-1)
        assert client.album_match_calls == []

    @pytest.mark.asyncio
    async def test_limit_caps_candidates_processed(self, tmp_path):
        ckpt = tmp_path / "ckpt.jsonl"
        client = FakeAppleClient()
        candidates = [(n, "A", str(n)) for n in (1, 2, 3)]
        done = await run_resolve(client, candidates, ckpt, concurrency=1, limit=2)
        assert len(done) == 2

    @pytest.mark.asyncio
    async def test_limit_caps_nonterminal_work_not_raw_candidates(self, tmp_path):
        # --limit must cap NON-terminal work: a resume over a mostly-done
        # checkpoint with --limit 1 must still do one *new* resolve, not select an
        # already-terminal id and do zero Apple work (which would silently stall).
        ckpt = tmp_path / "ckpt.jsonl"
        append_checkpoint(ckpt, AlbumResolveOutcome(1, "u1", STATUS_MATCHED))
        append_checkpoint(ckpt, AlbumResolveOutcome(2, "u2", STATUS_MATCHED))
        client = FakeAppleClient(matches={("C", "c"): "u3"})
        candidates = [(1, "A", "a"), (2, "B", "b"), (3, "C", "c"), (4, "D", "d")]
        await run_resolve(client, candidates, ckpt, concurrency=1, limit=1)
        # 1 and 2 are terminal -> filtered out first; limit=1 then picks the first
        # PENDING candidate (3), not the already-terminal (1).
        assert client.album_match_calls == [("C", "c")]

    @pytest.mark.asyncio
    async def test_rate_gate_constructed_and_awaited_per_candidate(self, tmp_path, monkeypatch):
        # Pin the --rate-per-min wiring end-to-end: run_resolve builds a _RateGate
        # from the derived interval and awaits it once per processed candidate.
        # Without this, a broken cap would silently do nothing against the shared
        # prod Apple token.
        import scripts.resolve_apple_urls as mod

        intervals: list[float] = []
        waits = {"n": 0}

        class SpyGate:
            def __init__(self, min_interval):
                intervals.append(min_interval)

            async def wait(self):
                waits["n"] += 1

        monkeypatch.setattr(mod, "_RateGate", SpyGate)
        client = FakeAppleClient(matches={("A", "a1"): "u"})
        candidates = [(1, "A", "a1"), (2, "B", "b1"), (3, "C", "c1")]
        await run_resolve(client, candidates, tmp_path / "c.jsonl", concurrency=1, rate_per_min=120)
        assert intervals == [pytest.approx(0.5)]  # 60 / 120
        assert waits["n"] == 3


class TestRateGate:
    def test_min_interval_seconds(self):
        assert min_interval_seconds(60) == pytest.approx(1.0)
        assert min_interval_seconds(120) == pytest.approx(0.5)

    def test_min_interval_none_is_zero(self):
        assert min_interval_seconds(None) == 0.0

    def test_min_interval_nonpositive_is_zero(self):
        assert min_interval_seconds(0) == 0.0
        assert min_interval_seconds(-5) == 0.0


class TestRateGateWait:
    @pytest.mark.asyncio
    async def test_wait_spaces_starts_by_min_interval(self, monkeypatch):
        # Deterministic (no wall-clock): a fake monotonic clock advanced by a fake
        # sleep pins that back-to-back waits are spaced by exactly min_interval.
        import scripts.resolve_apple_urls as mod

        clock = {"t": 100.0}
        sleeps: list[float] = []

        async def fake_sleep(secs):
            sleeps.append(secs)
            clock["t"] += secs  # a real sleep advances monotonic time

        monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)

        gate = _RateGate(min_interval=2.0)
        await gate.wait()  # first start: no wait, next_allowed = 102
        assert sleeps == []
        await gate.wait()  # clock still 100 -> sleep 2.0 to reach 102, next = 104
        await gate.wait()  # clock now 102 -> sleep 2.0 to reach 104, next = 106
        assert sleeps == [2.0, 2.0]

    @pytest.mark.asyncio
    async def test_wait_zero_interval_never_sleeps(self, monkeypatch):
        import scripts.resolve_apple_urls as mod

        sleeps: list[float] = []

        async def fake_sleep(secs):
            sleeps.append(secs)

        monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
        gate = _RateGate(min_interval=0.0)
        await gate.wait()
        await gate.wait()
        assert sleeps == []


class TestBuildAppleClient:
    def test_missing_all_creds_raises_system_exit(self, monkeypatch):
        for var in ("APPLE_MUSIC_TEAM_ID", "APPLE_MUSIC_KEY_ID", "APPLE_MUSIC_PRIVATE_KEY"):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(SystemExit):
            _build_apple_client()

    def test_partial_creds_raises_system_exit(self, monkeypatch):
        # Fail fast before any Apple call, not deep in a JWT/HTTP request.
        monkeypatch.setenv("APPLE_MUSIC_TEAM_ID", "team")
        monkeypatch.setenv("APPLE_MUSIC_KEY_ID", "key")
        monkeypatch.delenv("APPLE_MUSIC_PRIVATE_KEY", raising=False)
        with pytest.raises(SystemExit):
            _build_apple_client()


class TestLoadCandidates:
    def test_parses_tsv_and_skips_malformed(self, tmp_path):
        p = tmp_path / "c.tsv"
        p.write_text(
            "album_id\tartist\talbum\n"
            "100\tJuana Molina\tDOGA\n"
            "\tBlank\tId\n"  # missing id -> skip
            "300\tSessa\t\n"  # blank album -> skip
            "0\tZero\tId\n"  # non-positive id -> skip
            "400\tCat Power\tMoon Pix\n",
            encoding="utf-8",
        )
        assert load_candidates(p) == [
            (100, "Juana Molina", "DOGA"),
            (400, "Cat Power", "Moon Pix"),
        ]

    def test_reads_utf8_sig_bom(self, tmp_path):
        p = tmp_path / "c.tsv"
        p.write_text("﻿album_id\tartist\talbum\n5\tStereolab\tDots and Loops\n", encoding="utf-8")
        assert load_candidates(p) == [(5, "Stereolab", "Dots and Loops")]

    def test_double_quotes_in_fields_are_literal_and_drop_no_rows(self, tmp_path):
        # A BS export is genuine (unescaped) TSV, so a double-quote in an
        # artist/album cell must be treated as a literal character. With csv's
        # default quote processing a leading quote is stripped (altering the
        # query) and an unbalanced quote swallows every following line (dropping
        # candidates). Both must be avoided.
        p = tmp_path / "c.tsv"
        p.write_text(
            "album_id\tartist\talbum\n"
            '100\tArtist\t"Heroes"\n'  # leading+trailing quote: must stay literal
            '300\tBand\tsome "quoted album\n'  # unbalanced quote must not swallow row 400
            "400\tCat Power\tMoon Pix\n"
            '500\tLucrecia Dalt\t7" Single\n',  # mid-field quote
            encoding="utf-8",
        )
        assert load_candidates(p) == [
            (100, "Artist", '"Heroes"'),
            (300, "Band", 'some "quoted album'),
            (400, "Cat Power", "Moon Pix"),
            (500, "Lucrecia Dalt", '7" Single'),
        ]


class TestWriteUrlTsv:
    def test_writes_only_matched_rows(self, tmp_path):
        p = tmp_path / "out.tsv"
        outcomes = {
            100: AlbumResolveOutcome(100, "u100", STATUS_MATCHED),
            200: AlbumResolveOutcome(200, None, STATUS_NO_MATCH),
            300: AlbumResolveOutcome(300, None, STATUS_API_ERROR),
        }
        n = write_url_tsv(p, outcomes)
        assert n == 1
        lines = p.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "album_id\tapple_music_url"
        assert lines[1] == "100\tu100"
        assert len(lines) == 2  # only the matched row

    def test_multiple_matched_rows_sorted_by_album_id(self, tmp_path):
        # Stable, diff-able output: matched rows are emitted in album_id order
        # regardless of dict insertion order; no_match/api_error are dropped.
        p = tmp_path / "out.tsv"
        outcomes = {
            300: AlbumResolveOutcome(300, "u300", STATUS_MATCHED),
            100: AlbumResolveOutcome(100, "u100", STATUS_MATCHED),
            200: AlbumResolveOutcome(200, None, STATUS_NO_MATCH),
            150: AlbumResolveOutcome(150, "u150", STATUS_MATCHED),
            250: AlbumResolveOutcome(250, None, STATUS_API_ERROR),
        }
        n = write_url_tsv(p, outcomes)
        assert n == 3
        lines = p.read_text(encoding="utf-8").splitlines()
        assert lines == [
            "album_id\tapple_music_url",
            "100\tu100",
            "150\tu150",
            "300\tu300",
        ]
