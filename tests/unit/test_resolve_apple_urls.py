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


class TestRateGate:
    def test_min_interval_seconds(self):
        assert min_interval_seconds(60) == pytest.approx(1.0)
        assert min_interval_seconds(120) == pytest.approx(0.5)

    def test_min_interval_none_is_zero(self):
        assert min_interval_seconds(None) == 0.0

    def test_min_interval_nonpositive_is_zero(self):
        assert min_interval_seconds(0) == 0.0
        assert min_interval_seconds(-5) == 0.0


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
