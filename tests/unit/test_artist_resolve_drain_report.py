"""Unit tests for the artist-resolve drain report layer (LML#759 PR D2).

`build_report` reduces the drain's JSONL verdict records to the distributions
BS#1614 asked for (per-method yield, cache-corroboration + candidate-count
distributions, ambiguity rate, alias-leg-only + trigram-near-miss sizing, the
`escalation_unavailable` residual); `sample_spot_check` draws the seeded,
reproducible `api_search`-mints-only table the human eyeballs before `--live`;
`format_report_markdown` renders both. These tests pin the aggregation math
against records built with the same `ArtistResolveResult` wire shape the drain
engine appends.
"""

from __future__ import annotations

import pytest

from scripts.artist_resolve_drain.drain import record_from_verdict
from scripts.artist_resolve_drain.report import (
    build_report,
    format_report_markdown,
    sample_spot_check,
)


# --------------------------------------------------------------------------- #
# Verdict / record builders mirroring the ArtistResolveResult wire shape.
# --------------------------------------------------------------------------- #
def _resolved(name, artist_id, *, method="api_search", corr=None, candidate_count=1):
    return {
        "name": name,
        "discogs_artist_id": artist_id,
        "canonical_name": f"{name}",
        "method": method,
        "cache_corroboration": list(corr or []),
        "unresolved_reason": None,
        "candidate_count": candidate_count,
    }


def _unresolved(name, reason, *, corr=None, candidate_count=None):
    return {
        "name": name,
        "discogs_artist_id": None,
        "canonical_name": None,
        "method": None,
        "cache_corroboration": list(corr or []),
        "unresolved_reason": reason,
        "candidate_count": candidate_count,
    }


def _rec(verdict, *, dry_run=True, attempt=1, ts="2026-07-12T00:00:00+00:00"):
    return record_from_verdict(verdict, dry_run=dry_run, attempt=attempt, ts=ts)


# --------------------------------------------------------------------------- #
# build_report
# --------------------------------------------------------------------------- #
class TestBuildReport:
    def _records(self):
        return [
            _rec(
                _resolved("api1", 1, method="api_search", corr=["cache_exact"], candidate_count=1)
            ),
            _rec(_resolved("store1", 2, method="identity_store", corr=[], candidate_count=None)),
            _rec(_unresolved("nf1", "not_found", corr=["cache_alias"], candidate_count=0)),
            _rec(_unresolved("nf2", "not_found", corr=["cache_trigram"], candidate_count=0)),
            _rec(_unresolved("amb1", "ambiguous", corr=[], candidate_count=2)),
            _rec(_unresolved("esc1", "escalation_unavailable", corr=[], candidate_count=None)),
        ]

    def test_counts_by_verdict_and_method(self):
        names = ["api1", "store1", "nf1", "nf2", "amb1", "esc1"]
        rep = build_report(self._records(), names, max_attempts=3)
        assert rep["resolved"] == 2
        assert rep["resolved_by_method"] == {"api_search": 1, "identity_store": 1}
        assert rep["not_found"] == 2
        assert rep["ambiguous"] == 1
        assert rep["escalation_residual"] == 1

    def test_ambiguity_rate_over_measured(self):
        names = ["api1", "store1", "nf1", "nf2", "amb1", "esc1"]
        rep = build_report(self._records(), names, max_attempts=3)
        # measured = candidate_count not None: api1(1), nf1(0), nf2(0), amb1(2) = 4
        assert rep["measured"] == 4
        assert rep["ambiguity_rate"] == pytest.approx(0.25)

    def test_corroboration_and_candidate_distributions(self):
        names = ["api1", "store1", "nf1", "nf2", "amb1", "esc1"]
        rep = build_report(self._records(), names, max_attempts=3)
        assert rep["corroboration_resolved"]["cache_exact"] == 1
        assert rep["corroboration_not_found"]["cache_alias"] == 1
        assert rep["candidate_count_dist"]["1"] == 1
        assert rep["candidate_count_dist"]["2"] == 1
        assert rep["candidate_count_dist"]["null"] == 2  # store1 + esc1

    def test_alias_only_and_trigram_near_miss(self):
        names = ["api1", "store1", "nf1", "nf2", "amb1", "esc1"]
        rep = build_report(self._records(), names, max_attempts=3)
        assert rep["alias_only_not_found"] == 1  # nf1
        assert rep["trigram_near_miss"] == 1  # nf2

    def test_alias_only_excludes_mixed_corroboration_not_found(self):
        # A not_found corroborated by an equality leg (cache_exact) *and* an alias
        # leg is NOT alias-leg-*only*: it would size the v2 alias arm only if the
        # alias family were its sole corroboration. `any(...)` over-counts it.
        recs = [_rec(_unresolved("nf", "not_found", corr=["cache_exact", "cache_alias"]))]
        rep = build_report(recs, ["nf"], max_attempts=3)
        assert rep["alias_only_not_found"] == 0

    def test_ambiguity_rate_excludes_unmeasured_store_conflict(self):
        # A store-conflict ambiguous verdict carries candidate_count=None ("doubt
        # without a measurement"); it must not inflate the rate over `measured`.
        recs = [
            _rec(_resolved("api1", 1, candidate_count=1)),
            _rec(_unresolved("amb_unmeasured", "ambiguous", candidate_count=None)),
        ]
        rep = build_report(recs, ["api1", "amb_unmeasured"], max_attempts=3)
        # Only api1 is measured; the unmeasured ambiguous verdict is out of the base.
        assert rep["measured"] == 1
        assert rep["ambiguity_rate"] == pytest.approx(0.0)

    def test_not_processed_counts_names_without_a_verdict(self):
        rep = build_report([_rec(_resolved("a", 1))], ["a", "b"], max_attempts=3)
        assert rep["not_processed"] == 1


# --------------------------------------------------------------------------- #
# sample_spot_check + format
# --------------------------------------------------------------------------- #
class TestSpotCheck:
    def _verdicts(self):
        return [_rec(_resolved(f"api{i}", 100 + i, method="api_search")) for i in range(50)] + [
            _rec(_resolved("store", 9, method="identity_store")),
            _rec(_unresolved("nf", "not_found")),
        ]

    def test_samples_only_api_search_resolutions(self):
        rows = sample_spot_check(self._verdicts(), seed=0, k=20)
        assert len(rows) == 20
        # Only the api_search pool (names api0..api49) is eligible: the
        # identity_store row and the not_found row must never be sampled.
        assert all(r["name"].startswith("api") for r in rows)
        assert all(r["discogs_artist_id"] is not None for r in rows)

    def test_seeded_sampling_is_deterministic(self):
        a = sample_spot_check(self._verdicts(), seed=7, k=10)
        b = sample_spot_check(self._verdicts(), seed=7, k=10)
        assert [r["name"] for r in a] == [r["name"] for r in b]

    def test_k_larger_than_pool_returns_whole_pool(self):
        rows = sample_spot_check([_rec(_resolved("only", 3, method="api_search"))], seed=0, k=20)
        assert len(rows) == 1

    def test_rows_carry_discogs_artist_url(self):
        rows = sample_spot_check([_rec(_resolved("x", 187553, method="api_search"))], seed=0, k=20)
        assert rows[0]["url"] == "https://www.discogs.com/artist/187553"


class TestFormatReportMarkdown:
    def test_markdown_has_headline_mode_and_spot_check_links(self):
        rep = build_report(
            [_rec(_resolved("x", 187553, method="api_search"))], ["x"], max_attempts=3
        )
        rows = sample_spot_check([_rec(_resolved("x", 187553, method="api_search"))], seed=0, k=20)
        md = format_report_markdown(rep, rows, dry_run=True)
        assert "DRY RUN" in md
        assert "https://www.discogs.com/artist/187553" in md
        assert "Residual" in md or "residual" in md

    def test_live_mode_label(self):
        rep = build_report([], [], max_attempts=3)
        md = format_report_markdown(rep, [], dry_run=False)
        assert "LIVE" in md

    def test_pipe_in_name_is_escaped_in_spot_check_row(self):
        # A `|` in an artist name would split the markdown row into extra cells,
        # shifting the Discogs id against the wrong name — the exact failure the
        # human spot-check gate exists to prevent. Free-text cells must be escaped
        # so every row keeps the header's column count.
        name = "Sonic Youth | Ciccone Youth"
        recs = [_rec(_resolved(name, 5, method="api_search"))]
        rep = build_report(recs, [name], max_attempts=3)
        rows = sample_spot_check(recs, seed=0, k=20)
        md = format_report_markdown(rep, rows, dry_run=True)
        row_line = next(line for line in md.splitlines() if "discogs" in line)
        # The name's own pipe is escaped, so it no longer opens a real cell
        # boundary: the row has exactly the 5-column header's 6 unescaped pipes.
        assert row_line.replace("\\|", "").count("|") == 6
        assert "Sonic Youth \\| Ciccone Youth" in row_line

    def test_none_candidate_count_renders_as_dash(self):
        # A guarded edge — an api_search row with a null candidate_count must show
        # a dash, matching the `canonical_name or '—'` treatment, not literal None.
        recs = [_rec(_resolved("Stereolab", 5, method="api_search", candidate_count=None))]
        rep = build_report(recs, ["Stereolab"], max_attempts=3)
        rows = sample_spot_check(recs, seed=0, k=20)
        md = format_report_markdown(rep, rows, dry_run=True)
        row_line = next(line for line in md.splitlines() if "discogs" in line)
        assert "None" not in row_line

    def test_max_attempts_surfaced_in_report(self):
        # The escalation-retry budget the operator ran under is the context needed
        # to read the residual (0 retries vs 2?); it must reach the report reader.
        recs = [_rec(_resolved("Juana Molina", 5, method="api_search"))]
        rep = build_report(recs, ["Juana Molina"], max_attempts=3)
        md = format_report_markdown(rep, [], dry_run=True)
        assert "attempt" in md.lower()
        assert "3" in md
