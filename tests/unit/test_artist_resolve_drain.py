"""Unit tests for the bulk artist-resolve drain script (LML#759 PR D).

The drain drives the prod `POST /api/v1/artists/resolve/bulk` endpoint over a
name set exported by Backend-Service (BS#1614), paging 25 at a time, appending
verdicts to a JSONL log with crash-safe resume, re-paging the retryable
`escalation_unavailable` verdict after a cool-down with bounded retries, and
finally building a yield report + a random spot-check table for the human
wrong-mint review that gates the `--live` run.

These tests pin the pure orchestration logic (parse / resume / retry / report)
and the HTTP envelope (`resolve_batch`) without touching the network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from scripts.artist_resolve_drain import drain
from scripts.artist_resolve_drain.drain import (
    DrainError,
    attempt_counts,
    chunk,
    compute_pending,
    filter_records_for_mode,
    is_terminal,
    latest_by_name,
    load_records,
    parse_names_file,
    record_from_verdict,
    resolve_batch,
    run_drain,
)
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
# parse_names_file
# --------------------------------------------------------------------------- #
class TestParseNamesFile:
    def test_newline_delimited_strips_blanks_and_trims(self):
        text = "Wishy\n  REZN  \n\nL'Rain\n"
        assert parse_names_file(text) == ["Wishy", "REZN", "L'Rain"]

    def test_dedupes_preserving_first_occurrence_order(self):
        text = "glaive\nThe Tubs\nglaive\nSiM\n"
        assert parse_names_file(text) == ["glaive", "The Tubs", "SiM"]

    def test_json_array_input(self):
        text = json.dumps(["Popsicle", "Wishy", "Popsicle"])
        assert parse_names_file(text) == ["Popsicle", "Wishy"]

    def test_json_non_list_falls_back_to_line_mode(self):
        # A bare JSON object is not a name list; treat the whole thing as lines.
        text = '{"names": ["a"]}'
        assert parse_names_file(text) == ['{"names": ["a"]}']

    def test_empty_input_yields_empty_list(self):
        assert parse_names_file("\n\n   \n") == []


# --------------------------------------------------------------------------- #
# chunk
# --------------------------------------------------------------------------- #
class TestChunk:
    def test_pages_into_fixed_size_batches(self):
        assert list(chunk(list(range(5)), 2)) == [[0, 1], [2, 3], [4]]

    def test_empty_yields_nothing(self):
        assert list(chunk([], 25)) == []

    def test_rejects_non_positive_size(self):
        with pytest.raises(ValueError):
            list(chunk([1, 2], 0))


# --------------------------------------------------------------------------- #
# is_terminal / record helpers
# --------------------------------------------------------------------------- #
class TestIsTerminal:
    def test_resolved_is_terminal(self):
        assert is_terminal(_rec(_resolved("Wishy", 111))) is True

    @pytest.mark.parametrize("reason", ["not_found", "ambiguous"])
    def test_not_found_and_ambiguous_are_terminal(self, reason):
        assert is_terminal(_rec(_unresolved("x", reason))) is True

    def test_escalation_unavailable_is_not_terminal(self):
        assert is_terminal(_rec(_unresolved("x", "escalation_unavailable"))) is False


class TestRecordFromVerdict:
    def test_carries_mode_attempt_ts_and_wire_fields(self):
        rec = record_from_verdict(
            _resolved("Wishy", 111, corr=["cache_exact"]),
            dry_run=False,
            attempt=2,
            ts="T",
        )
        assert rec["name"] == "Wishy"
        assert rec["discogs_artist_id"] == 111
        assert rec["cache_corroboration"] == ["cache_exact"]
        assert rec["dry_run"] is False
        assert rec["attempt"] == 2
        assert rec["ts"] == "T"

    def test_missing_corroboration_defaults_to_empty_list(self):
        verdict = {"name": "x", "unresolved_reason": "not_found"}
        rec = record_from_verdict(verdict, dry_run=True, attempt=1, ts="T")
        assert rec["cache_corroboration"] == []
        assert rec["discogs_artist_id"] is None


class TestLatestAndCounts:
    def test_latest_by_name_keeps_last_append(self):
        recs = [
            _rec(_unresolved("a", "escalation_unavailable"), attempt=1),
            _rec(_resolved("a", 9), attempt=2),
        ]
        assert latest_by_name(recs)["a"]["discogs_artist_id"] == 9

    def test_attempt_counts_counts_records_per_name(self):
        recs = [
            _rec(_unresolved("a", "escalation_unavailable"), attempt=1),
            _rec(_unresolved("a", "escalation_unavailable"), attempt=2),
            _rec(_resolved("b", 1), attempt=1),
        ]
        assert attempt_counts(recs) == {"a": 2, "b": 1}


# --------------------------------------------------------------------------- #
# filter_records_for_mode
# --------------------------------------------------------------------------- #
class TestFilterRecordsForMode:
    def test_keeps_only_records_matching_dry_run(self):
        recs = [
            _rec(_resolved("a", 1), dry_run=True),
            _rec(_resolved("b", 2), dry_run=False),
        ]
        assert [r["name"] for r in filter_records_for_mode(recs, dry_run=True)] == ["a"]
        assert [r["name"] for r in filter_records_for_mode(recs, dry_run=False)] == ["b"]


# --------------------------------------------------------------------------- #
# compute_pending — the resume + retry brain
# --------------------------------------------------------------------------- #
class TestComputePending:
    def test_fresh_run_returns_all_names_deduped(self):
        assert compute_pending(["a", "b", "a"], [], max_attempts=3) == ["a", "b"]

    def test_terminal_names_are_skipped_on_resume(self):
        recs = [_rec(_resolved("a", 1)), _rec(_unresolved("b", "not_found"))]
        assert compute_pending(["a", "b", "c"], recs, max_attempts=3) == ["c"]

    def test_escalation_is_repaged_below_max_attempts(self):
        recs = [_rec(_unresolved("a", "escalation_unavailable"), attempt=1)]
        assert compute_pending(["a"], recs, max_attempts=3) == ["a"]

    def test_escalation_is_residual_at_max_attempts(self):
        recs = [
            _rec(_unresolved("a", "escalation_unavailable"), attempt=1),
            _rec(_unresolved("a", "escalation_unavailable"), attempt=2),
            _rec(_unresolved("a", "escalation_unavailable"), attempt=3),
        ]
        assert compute_pending(["a"], recs, max_attempts=3) == []

    def test_later_terminal_verdict_wins_over_earlier_escalation(self):
        recs = [
            _rec(_unresolved("a", "escalation_unavailable"), attempt=1),
            _rec(_resolved("a", 7), attempt=2),
        ]
        assert compute_pending(["a"], recs, max_attempts=3) == []


# --------------------------------------------------------------------------- #
# JSONL round-trip
# --------------------------------------------------------------------------- #
class TestJsonlIo:
    def test_append_then_load_round_trips(self, tmp_path):
        path = tmp_path / "drain.jsonl"
        drain.append_record(path, _rec(_resolved("a", 1)))
        drain.append_record(path, _rec(_unresolved("b", "not_found")))
        loaded = load_records(path)
        assert [r["name"] for r in loaded] == ["a", "b"]

    def test_load_missing_file_returns_empty(self, tmp_path):
        assert load_records(tmp_path / "nope.jsonl") == []


# --------------------------------------------------------------------------- #
# resolve_batch — HTTP envelope
# --------------------------------------------------------------------------- #
class TestResolveBatch:
    @pytest.mark.asyncio
    async def test_sends_bearer_auth_and_dry_run_body_and_returns_results(self, httpx_mock):
        httpx_mock.add_response(
            url="https://lml.example/api/v1/artists/resolve/bulk",
            json={"results": [_resolved("Wishy", 111), _unresolved("REZN", "not_found")]},
        )
        async with httpx.AsyncClient() as client:
            results = await resolve_batch(
                client, "https://lml.example", "secret-key", ["Wishy", "REZN"], dry_run=True
            )
        assert [r["name"] for r in results] == ["Wishy", "REZN"]
        req = httpx_mock.get_requests()[0]
        assert req.headers["Authorization"] == "Bearer secret-key"
        body = json.loads(req.content)
        assert body == {"names": ["Wishy", "REZN"], "dry_run": True}

    @pytest.mark.asyncio
    async def test_trailing_slash_base_url_is_normalized(self, httpx_mock):
        httpx_mock.add_response(
            url="https://lml.example/api/v1/artists/resolve/bulk",
            json={"results": [_resolved("Wishy", 111)]},
        )
        async with httpx.AsyncClient() as client:
            await resolve_batch(client, "https://lml.example/", "k", ["Wishy"], dry_run=False)
        # No assertion error from add_response's exact-url match == pass.

    @pytest.mark.asyncio
    async def test_length_mismatch_raises(self, httpx_mock):
        httpx_mock.add_response(json={"results": [_resolved("Wishy", 111)]})
        async with httpx.AsyncClient() as client:
            with pytest.raises(DrainError):
                await resolve_batch(client, "https://lml.example", "k", ["Wishy", "REZN"], True)

    @pytest.mark.asyncio
    async def test_index_misalignment_raises(self, httpx_mock):
        # Endpoint must echo name verbatim, index-aligned; a mismatch is a contract break.
        httpx_mock.add_response(json={"results": [_resolved("SOMEONE-ELSE", 111)]})
        async with httpx.AsyncClient() as client:
            with pytest.raises(DrainError):
                await resolve_batch(client, "https://lml.example", "k", ["Wishy"], True)

    @pytest.mark.asyncio
    async def test_http_error_status_raises(self, httpx_mock):
        httpx_mock.add_response(status_code=401, json={"detail": "nope"})
        async with httpx.AsyncClient() as client:
            with pytest.raises(httpx.HTTPStatusError):
                await resolve_batch(client, "https://lml.example", "k", ["Wishy"], True)


# --------------------------------------------------------------------------- #
# run_drain — the full loop, with an injected post + sleep (no network, no wait)
# --------------------------------------------------------------------------- #
class _FakePost:
    """Records calls and returns scripted verdicts keyed by name.

    `plan[name]` is a list of verdicts consumed one per call; the last entry
    repeats once exhausted.
    """

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.batches: list[list[str]] = []

    async def __call__(self, names, dry_run):
        self.batches.append(list(names))
        out = []
        for name in names:
            seq = self.plan[name]
            verdict = seq.pop(0) if len(seq) > 1 else seq[0]
            out.append(verdict)
        return out


@pytest.mark.asyncio
class TestRunDrain:
    async def test_writes_jsonl_verdict_per_name(self, tmp_path):
        out = tmp_path / "d.jsonl"
        post = _FakePost({"a": [_resolved("a", 1)], "b": [_unresolved("b", "not_found")]})
        await run_drain(
            all_names=["a", "b"],
            dry_run=True,
            out_path=out,
            post_batch=post,
            page_size=25,
            max_retries=2,
            cooldown=0,
            sleep=_noop_sleep,
            clock=lambda: "T",
        )
        names = [r["name"] for r in load_records(out)]
        assert sorted(names) == ["a", "b"]

    async def test_pages_at_page_size(self, tmp_path):
        post = _FakePost({n: [_resolved(n, i)] for i, n in enumerate("abcde")})
        await run_drain(
            all_names=list("abcde"),
            dry_run=True,
            out_path=tmp_path / "d.jsonl",
            post_batch=post,
            page_size=2,
            max_retries=2,
            cooldown=0,
            sleep=_noop_sleep,
            clock=lambda: "T",
        )
        assert [len(b) for b in post.batches] == [2, 2, 1]

    async def test_resume_skips_terminal_names_from_prior_run(self, tmp_path):
        out = tmp_path / "d.jsonl"
        drain.append_record(out, _rec(_resolved("a", 1), dry_run=True))
        post = _FakePost({"b": [_resolved("b", 2)]})
        await run_drain(
            all_names=["a", "b"],
            dry_run=True,
            out_path=out,
            post_batch=post,
            page_size=25,
            max_retries=2,
            cooldown=0,
            sleep=_noop_sleep,
            clock=lambda: "T",
        )
        # "a" was already terminal → never re-posted.
        assert post.batches == [["b"]]

    async def test_live_run_ignores_prior_dry_run_records(self, tmp_path):
        out = tmp_path / "d.jsonl"
        drain.append_record(out, _rec(_resolved("a", 1), dry_run=True))
        post = _FakePost({"a": [_resolved("a", 1)]})
        await run_drain(
            all_names=["a"],
            dry_run=False,  # live
            out_path=out,
            post_batch=post,
            page_size=25,
            max_retries=2,
            cooldown=0,
            sleep=_noop_sleep,
            clock=lambda: "T",
        )
        # Prior record was dry_run=True; the live run must actually re-post to mint.
        assert post.batches == [["a"]]

    async def test_escalation_is_repaged_after_cooldown_then_residual(self, tmp_path):
        out = tmp_path / "d.jsonl"
        sleeps: list[float] = []

        async def rec_sleep(seconds):
            sleeps.append(seconds)

        # "a" resolves on 2nd attempt; "b" is escalation forever → residual.
        post = _FakePost(
            {
                "a": [
                    _unresolved("a", "escalation_unavailable"),
                    _resolved("a", 5),
                ],
                "b": [_unresolved("b", "escalation_unavailable")],
            }
        )
        await run_drain(
            all_names=["a", "b"],
            dry_run=True,
            out_path=out,
            post_batch=post,
            page_size=25,
            max_retries=2,  # 3 attempts total
            cooldown=42,
            sleep=rec_sleep,
            clock=lambda: "T",
        )
        latest = latest_by_name(load_records(out))
        assert latest["a"]["discogs_artist_id"] == 5
        assert latest["b"]["unresolved_reason"] == "escalation_unavailable"
        # b tried on 3 attempts total.
        assert attempt_counts(load_records(out))["b"] == 3
        # Cool-down slept once per retry round (2 retries), each for `cooldown`.
        assert sleeps == [42, 42]

    async def test_stops_early_on_shutdown_request(self, tmp_path):
        class _Flag:
            requested = True

        post = _FakePost({"a": [_resolved("a", 1)]})
        await run_drain(
            all_names=["a"],
            dry_run=True,
            out_path=tmp_path / "d.jsonl",
            post_batch=post,
            page_size=25,
            max_retries=2,
            cooldown=0,
            sleep=_noop_sleep,
            clock=lambda: "T",
            shutdown=_Flag(),
        )
        assert post.batches == []


async def _noop_sleep(seconds):
    return None


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
