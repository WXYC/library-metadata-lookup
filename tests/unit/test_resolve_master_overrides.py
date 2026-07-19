"""Unit tests for the master -> release tiered resolver (LML#858, Phase 2).

Alex L.'s hand-verified dataset pins 49,502 card-catalog entries to Discogs
*master* IDs. A master groups every version (release) of an album, so it yields
no single tracklist. This resolver converts each master to a concrete release
ID, choosing among the master's cached versions with a tiered policy:

* **Tier A** — one cached version, or several with identical tracklists: the
  choice is forced or provably irrelevant (high confidence).
* **Tier B** — versions have *divergent* tracklists and the flowsheet's played
  titles pick a unique strict-superset winner (high confidence). The flowsheet
  signal is asymmetric: a played title present on version A and absent from B is
  evidence *for* A, but the absence of a play proves nothing — hence a
  strict-superset margin, not raw-count scoring.
* **Tier C** — divergent tracklists, no distinguishing flowsheet signal: fall
  back to the master's ``main_release`` (or a deterministic pick) under a
  low-confidence tag so the residue stays enumerable.
* **no_cached** — the master has no cached release yet; pin ``main_release_id``
  if the master table supplies one, else leave unresolved (API tail).

This file covers the pure decision core (no DB). The PG query + CSV plumbing are
exercised by ``tests/integration/test_resolve_master_overrides.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv as _csv
import types

from scripts.resolve_master_overrides import (
    CONF_HIGH,
    CONF_LOW,
    NO_CACHED,
    TIER_A,
    TIER_B,
    TIER_C,
    UNRESOLVED,
    CandidateRelease,
    MasterLink,
    MasterResolution,
    _run,
    _split_by_confidence,
    load_flowsheet_titles,
    load_master_links,
    normalize_titles,
    resolve_all,
    resolve_master,
    tier_report,
    write_seed_csv,
    write_unresolved_csv,
)


def _cand(release_id: int, titles: list[str], fmt: str | None = None) -> CandidateRelease:
    return CandidateRelease(
        release_id=release_id,
        normalized_titles=normalize_titles(titles),
        format=fmt,
    )


class TestNormalizeTitles:
    def test_order_insensitive(self):
        # the same titles in a different order normalize to the same set
        assert normalize_titles(["Back, Baby", "On Your Own Love Again"]) == normalize_titles(
            ["On Your Own Love Again", "Back, Baby"]
        )

    def test_case_and_whitespace_normalized(self):
        assert normalize_titles(["ON YOUR OWN LOVE AGAIN", "back  baby"]) == normalize_titles(
            ["on your own love again", "Back Baby"]
        )
        assert isinstance(normalize_titles(["x"]), frozenset)

    def test_drops_blank_titles(self):
        assert normalize_titles(["", "  ", "Real Track"]) == frozenset({"real track"})


class TestTierA:
    def test_single_cached_version(self):
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(500, ["a", "b", "c"])],
            main_release_id=None,
            played_titles=frozenset(),
        )
        assert (r.tier, r.confidence, r.chosen_release_id) == (TIER_A, CONF_HIGH, 500)

    def test_multiple_versions_identical_tracklists(self):
        # two releases, same tracklist -> choice is irrelevant, pick deterministically
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(700, ["a", "b"]), _cand(500, ["b", "a"])],
            main_release_id=None,
            played_titles=frozenset(),
        )
        assert r.tier == TIER_A
        assert r.confidence == CONF_HIGH
        assert r.chosen_release_id == 500  # lowest id, deterministic

    def test_prefers_main_release_when_cached(self):
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(700, ["a", "b"]), _cand(500, ["b", "a"])],
            main_release_id=700,
            played_titles=frozenset(),
        )
        assert r.tier == TIER_A
        assert r.chosen_release_id == 700  # main_release wins the tie

    def test_lone_empty_tracklist_is_not_pinned_as_tier_a(self):
        # a cached release whose track titles are all blank/NULL normalizes to an
        # empty set: it carries no tracklist, so it must not be a high-confidence
        # Tier A pin. With no main_release it is unresolvable.
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(500, ["", "  "])],
            main_release_id=None,
            played_titles=frozenset(),
        )
        assert r.tier == UNRESOLVED
        assert r.chosen_release_id is None

    def test_empty_tracklist_dropped_leaving_single_real_version(self):
        # one real version + one all-blank version -> the blank one is dropped, so
        # the master has a single usable tracklist (Tier A) pinning the real id.
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(500, ["a", "b"]), _cand(700, [""])],
            main_release_id=700,  # the empty version, must NOT be pinned
            played_titles=frozenset(),
        )
        assert r.tier == TIER_A
        assert r.chosen_release_id == 500


class TestTierB:
    def test_flowsheet_strict_superset_winner(self):
        # version 500 covers both played titles; 700 covers only one -> 500 wins
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[
                _cand(500, ["hit one", "hit two", "deep cut"]),
                _cand(700, ["hit one", "bonus"]),
            ],
            main_release_id=700,  # main_release loses to the flowsheet evidence
            played_titles=normalize_titles(["Hit One", "Hit Two"]),
        )
        assert (r.tier, r.confidence, r.chosen_release_id) == (TIER_B, CONF_HIGH, 500)

    def test_crossing_evidence_has_no_winner(self):
        # each version covers a played title the other lacks -> ambiguous -> Tier C
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[
                _cand(500, ["hit one", "extra a"]),
                _cand(700, ["hit two", "extra b"]),
            ],
            main_release_id=700,
            played_titles=normalize_titles(["Hit One", "Hit Two"]),
        )
        assert r.tier == TIER_C
        assert r.confidence == CONF_LOW
        assert r.chosen_release_id == 700  # main_release fallback


class TestTierC:
    def test_divergent_no_flowsheet_signal(self):
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(500, ["a", "b"]), _cand(700, ["a", "c"])],
            main_release_id=None,
            played_titles=frozenset(),  # nothing played
        )
        assert r.tier == TIER_C
        assert r.confidence == CONF_LOW
        assert r.chosen_release_id == 500  # deterministic lowest when no main_release

    def test_divergent_prefers_main_release_when_cached(self):
        # main_release 700 is itself a cached version -> pin it (canonical + has tracklist)
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(500, ["a", "b"]), _cand(700, ["a", "c"])],
            main_release_id=700,
            played_titles=frozenset(),
        )
        assert r.tier == TIER_C
        assert r.chosen_release_id == 700

    def test_divergent_uncached_main_release_falls_to_cached_format_match(self):
        # main_release 999 is NOT cached (no tracklist) -> never pin it; instead
        # pick the cached version whose format matches the shelf copy. The whole
        # point of Phase 2 is a tracklist, so an uncached main_release is wrong.
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(500, ["a", "b"], "CD, Album"), _cand(700, ["a", "c"], "Vinyl, LP")],
            main_release_id=999,
            played_titles=frozenset(),
            csv_format="vinyl",
        )
        assert r.tier == TIER_C
        assert r.confidence == CONF_LOW
        assert r.chosen_release_id == 700  # cached Vinyl edition, not uncached 999

    def test_multitoken_format_matches_on_token_overlap(self):
        # a real-world multi-token CSV format ('vinyl - 7"') must still steer the
        # pick toward the vinyl edition; a naive whole-string compare never matches.
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[_cand(500, ["a", "b"], "CD, Album"), _cand(700, ["a", "c"], 'Vinyl, 7"')],
            main_release_id=None,
            played_titles=frozenset(),
            csv_format='vinyl - 7"',
        )
        assert r.tier == TIER_C
        assert r.chosen_release_id == 700  # shares {vinyl, 7} with the CSV format

    def test_format_overlap_prefers_more_specific_pressing(self):
        # LP vs 7" both share 'vinyl'; the 7" shares an extra token with the hint,
        # so the more specific pressing wins rather than the lowest id.
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[
                _cand(500, ["a", "b"], "Vinyl, LP"),
                _cand(700, ["a", "c"], 'Vinyl, 7"'),
            ],
            main_release_id=None,
            played_titles=frozenset(),
            csv_format='vinyl - 7"',
        )
        assert r.chosen_release_id == 700  # {vinyl,7} beats {vinyl} despite higher id


class TestNoCachedAndUnresolved:
    def test_no_cached_but_main_release_known(self):
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[],
            main_release_id=888,
            played_titles=frozenset(),
        )
        assert (r.tier, r.confidence, r.chosen_release_id) == (NO_CACHED, CONF_LOW, 888)

    def test_unresolved_when_nothing_to_pin(self):
        r = resolve_master(
            card_catalog_id=1,
            master_id=100,
            candidates=[],
            main_release_id=None,
            played_titles=frozenset(),
        )
        assert r.tier == UNRESOLVED
        assert r.chosen_release_id is None


_LINKS_CSV = (
    "card_catalog_id,artist,title,artist_correction,title_correction,wxyc_genre,"
    "format,discogs_url,discogs_type,discogs_id,note,source_file\n"
    "1,A Guy Called Gerald,Automanikk,,,Hiphop,cd,"
    "https://www.discogs.com/master/25205,master,25205,,x.xlsx\n"
    "2,Stereolab,Dots and Loops,,,Rock,vinyl,"
    "https://www.discogs.com/release/438,release,438,,x.xlsx\n"  # release row: skipped
    "3,Some Note,No Link,,,Rock,cd,,,,check library,x.xlsx\n"  # typeless: skipped
    "1,A Guy Called Gerald,Automanikk dup,,,Hiphop,vinyl,"
    "https://www.discogs.com/master/25205,master,25205,,x.xlsx\n"  # dup card: last wins
    "4,Bad,Zero Master,,,Rock,cd,,master,0,,x.xlsx\n"  # master id 0: invalid
)


class TestLoadMasterLinks:
    def _write(self, tmp_path, text=_LINKS_CSV):
        p = tmp_path / "merged.csv"
        p.write_text(text)
        return p

    def test_selects_only_master_rows(self, tmp_path):
        links = load_master_links(self._write(tmp_path))
        cards = {link.card_catalog_id for link in links}
        assert cards == {1}  # release row (2), typeless (3), zero-master (4) all dropped

    def test_dedupes_by_card_last_wins_keeps_format(self, tmp_path):
        links = load_master_links(self._write(tmp_path))
        link = next(link for link in links if link.card_catalog_id == 1)
        assert link.master_id == 25205
        assert link.csv_format == "vinyl"  # the later (dup) row's format wins

    def test_returns_master_link_instances(self, tmp_path):
        links = load_master_links(self._write(tmp_path))
        assert all(isinstance(link, MasterLink) for link in links)


class TestLoadFlowsheetTitles:
    def test_groups_and_normalizes_by_card(self, tmp_path):
        p = tmp_path / "titles.csv"
        p.write_text("card_catalog_id,title\n1,Hit One\n1,HIT ONE\n1,Deep Cut\n2,Another\n")
        by_card = load_flowsheet_titles(p)
        assert by_card[1] == frozenset({"hit one", "deep cut"})  # case-folded + deduped
        assert by_card[2] == frozenset({"another"})


class TestResolveAllAndReport:
    def test_resolve_all_maps_links_to_resolutions(self):
        links = [
            MasterLink(card_catalog_id=1, master_id=100, csv_format=None),
            MasterLink(card_catalog_id=2, master_id=200, csv_format=None),
        ]
        candidates = {100: [_cand(500, ["a", "b"])]}  # 200 has no cached version
        resolutions = resolve_all(links, candidates, {}, {})
        by_card = {r.card_catalog_id: r for r in resolutions}
        assert by_card[1].tier == TIER_A
        assert by_card[2].tier == UNRESOLVED

    def test_tier_report_counts(self):
        resolutions = [
            MasterResolution(1, 100, 500, TIER_A, CONF_HIGH, "x"),
            MasterResolution(2, 200, None, UNRESOLVED, CONF_LOW, "y"),
            MasterResolution(3, 300, 700, TIER_C, CONF_LOW, "z"),
        ]
        report = tier_report(resolutions)
        assert report["total"] == 3
        assert report["seedable"] == 2
        assert report["unresolved"] == 1
        # every tier/confidence bucket is present (0 for the empty ones) so the
        # report's key set is stable across runs and safe to index blindly.
        assert report["by_tier"] == {
            TIER_A: 1,
            TIER_B: 0,
            TIER_C: 1,
            NO_CACHED: 0,
            UNRESOLVED: 1,
        }
        assert report["by_confidence"] == {CONF_HIGH: 1, CONF_LOW: 2}


class TestSplitByConfidence:
    def test_routes_high_and_low_preserving_order(self):
        res = [
            MasterResolution(1, 100, 500, TIER_A, CONF_HIGH, "a"),
            MasterResolution(2, 200, 700, TIER_C, CONF_LOW, "b"),
            MasterResolution(3, 300, None, UNRESOLVED, CONF_LOW, "c"),
            MasterResolution(4, 400, 800, TIER_B, CONF_HIGH, "d"),
        ]
        high, low = _split_by_confidence(res)
        assert [r.card_catalog_id for r in high] == [1, 4]
        assert [r.card_catalog_id for r in low] == [2, 3]


class TestRunDriver:
    """The CLI driver, with the discogs-cache boundary monkeypatched out."""

    def test_run_writes_both_buckets_and_creates_report_dir(self, tmp_path, monkeypatch):
        inp = tmp_path / "merged.csv"
        inp.write_text(
            "card_catalog_id,discogs_type,discogs_id,format\n"
            "1,master,100,cd\n"  # -> Tier A high (one cached version)
            "2,master,200,vinyl\n"  # -> unresolved (no cached version)
        )

        async def fake_candidates(conn, master_ids):
            return {100: [CandidateRelease(500, normalize_titles(["a", "b"]), "CD, Album")]}

        async def fake_mains(conn, master_ids):
            return {}

        class _FakeCM:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *exc):
                return False

        class _FakePg:
            def __init__(self, *, dsn=None, pool=None):
                self.closed = False

            def acquire(self):
                return _FakeCM()

            async def close(self):
                self.closed = True

        monkeypatch.setattr("scripts.resolve_master_overrides.fetch_candidates", fake_candidates)
        monkeypatch.setattr("scripts.resolve_master_overrides.fetch_main_releases", fake_mains)
        monkeypatch.setattr(
            "config.settings.get_settings",
            lambda: types.SimpleNamespace(database_url_discogs="postgresql://x/y"),
        )
        monkeypatch.setattr("entity.sources.PgSource", _FakePg)

        out_high = tmp_path / "high.csv"
        out_low = tmp_path / "low.csv"
        report = tmp_path / "nested" / "report.json"  # parent dir does not exist yet
        out_unresolved = tmp_path / "unresolved.csv"
        ns = argparse.Namespace(
            input=str(inp),
            flowsheet=None,
            out_high=str(out_high),
            out_low=str(out_low),
            out_unresolved=str(out_unresolved),
            report=str(report),
        )

        asyncio.run(_run(ns))

        high_rows = list(_csv.DictReader(out_high.open()))
        assert [(r["card_catalog_id"], r["discogs_release_id"]) for r in high_rows] == [
            ("1", "500")
        ]
        low_rows = list(_csv.DictReader(out_low.open()))
        assert low_rows == []  # the unresolved card is skipped, header only
        unresolved_rows = list(_csv.DictReader(out_unresolved.open()))
        assert len(unresolved_rows) == 1  # the unresolved card lands on the API-tail list

        import json as _json

        assert report.exists()  # the missing parent dir was created
        payload = _json.loads(report.read_text())
        assert payload["total"] == 2
        assert payload["seedable"] == 1
        assert payload["unresolved"] == 1
        # No silent drops: every master the report counts as unresolved is on the
        # API-tail work-list CSV (pins the two definitions of "unresolved"
        # together so a future tier that leaves chosen_release_id None without
        # tier=UNRESOLVED can't slip through uncounted).
        assert len(unresolved_rows) == payload["unresolved"]


class TestWriteSeedCsv:
    def test_skips_unresolved_and_writes_seeder_columns(self, tmp_path):
        resolutions = [
            MasterResolution(1, 100, 500, TIER_A, CONF_HIGH, "x"),
            MasterResolution(2, 200, None, UNRESOLVED, CONF_LOW, "y"),
        ]
        out = tmp_path / "pins.csv"
        written = write_seed_csv(out, resolutions)
        assert written == 1
        rows = list(__import__("csv").DictReader(out.open()))
        assert len(rows) == 1
        assert rows[0]["card_catalog_id"] == "1"
        assert rows[0]["discogs_release_id"] == "500"


class TestWriteUnresolvedCsv:
    def test_writes_only_unresolved_card_master_pairs(self, tmp_path):
        # The complement of write_seed_csv: emit exactly the rows with no pin so
        # the LML#858 Discogs-API tail drain has an enumerable work-list (no
        # silent drops per the ticket's acceptance criteria).
        resolutions = [
            MasterResolution(1, 100, 500, TIER_A, CONF_HIGH, "pinned"),
            MasterResolution(2, 200, None, UNRESOLVED, CONF_LOW, "no cached/main"),
            MasterResolution(3, 300, None, UNRESOLVED, CONF_LOW, "no cached/main"),
        ]
        out = tmp_path / "sub" / "unresolved.csv"
        written = write_unresolved_csv(out, resolutions)
        assert written == 2
        assert out.exists()  # missing parent dir created
        rows = list(__import__("csv").DictReader(out.open()))
        assert [r["card_catalog_id"] for r in rows] == ["2", "3"]
        assert [r["master_id"] for r in rows] == ["200", "300"]

    def test_no_cached_tier_is_not_unresolved(self, tmp_path):
        # A no_cached row carries a main_release_id pin, so it is seedable, not
        # part of the API tail — only tier == UNRESOLVED belongs here.
        resolutions = [
            MasterResolution(4, 400, 900, NO_CACHED, CONF_LOW, "main_release only"),
        ]
        out = tmp_path / "unresolved.csv"
        written = write_unresolved_csv(out, resolutions)
        assert written == 0
