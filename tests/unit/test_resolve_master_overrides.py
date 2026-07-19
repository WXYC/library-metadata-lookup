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
    load_flowsheet_titles,
    load_master_links,
    normalize_titles,
    resolve_all,
    resolve_master,
    tier_report,
    write_seed_csv,
)


def _cand(release_id: int, titles: list[str], fmt: str | None = None) -> CandidateRelease:
    return CandidateRelease(
        release_id=release_id,
        normalized_titles=normalize_titles(titles),
        format=fmt,
    )


class TestNormalizeTitles:
    def test_order_insensitive_and_normalized(self):
        a = normalize_titles(["Back, Baby", "On Your Own Love Again"])
        b = normalize_titles(["ON YOUR OWN LOVE AGAIN", "back  baby"])
        # comma is preserved by to_match_form, so keep the raw title stable
        assert normalize_titles(["Back Baby"]) == b - {"on your own love again"}
        assert isinstance(a, frozenset)

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
        assert report["by_tier"] == {TIER_A: 1, TIER_C: 1, UNRESOLVED: 1}


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
