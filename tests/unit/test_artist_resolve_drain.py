"""Unit tests for the bulk artist-resolve drain engine (LML#759 PR D).

The drain drives the prod `POST /api/v1/artists/resolve/bulk` endpoint over a
name set exported by Backend-Service (BS#1614), paging 25 at a time, appending
verdicts to a JSONL log with crash-safe resume, and re-paging the retryable
`escalation_unavailable` verdict after a cool-down with bounded retries.

These tests pin the pure orchestration logic (parse / resume / retry) and the
HTTP envelope (`resolve_batch`) without touching the network. The yield report +
spot-check sampler are covered in `test_artist_resolve_drain_report.py`.
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
    make_post_batch,
    parse_names_file,
    record_from_verdict,
    resolve_batch,
    run_drain,
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
# make_post_batch — transient-HTTP retry adapter (retry 5xx/transport, not 4xx)
# --------------------------------------------------------------------------- #
class TestMakePostBatch:
    @pytest.mark.asyncio
    async def test_retries_transient_5xx_then_succeeds(self, httpx_mock):
        # A 5xx is a transient server hiccup — retried after a backoff, then the
        # 200 result is returned.
        httpx_mock.add_response(status_code=503)
        httpx_mock.add_response(json={"results": [_resolved("Wishy", 111)]})
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async with httpx.AsyncClient() as client:
            post = make_post_batch(client, "https://lml.example", "k", sleep=fake_sleep)
            results = await post(["Wishy"], True)
        assert [r["name"] for r in results] == ["Wishy"]
        assert len(sleeps) == 1  # one backoff between the 503 and the successful retry

    @pytest.mark.asyncio
    async def test_retries_transport_error_then_succeeds(self, httpx_mock):
        # A connection-layer failure (no HTTP status) is also transient → retried.
        httpx_mock.add_exception(httpx.ConnectError("boom"))
        httpx_mock.add_response(json={"results": [_resolved("Wishy", 111)]})
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async with httpx.AsyncClient() as client:
            post = make_post_batch(client, "https://lml.example", "k", sleep=fake_sleep)
            results = await post(["Wishy"], True)
        assert [r["name"] for r in results] == ["Wishy"]
        assert len(sleeps) == 1

    @pytest.mark.asyncio
    async def test_does_not_retry_4xx(self, httpx_mock):
        # A 4xx is a misconfig (bad key / oversized page / bad request), not a
        # transient fault — re-raised immediately with no retry.
        httpx_mock.add_response(status_code=401, json={"detail": "nope"})
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async with httpx.AsyncClient() as client:
            post = make_post_batch(client, "https://lml.example", "k", sleep=fake_sleep)
            with pytest.raises(httpx.HTTPStatusError):
                await post(["Wishy"], True)
        assert sleeps == []  # no backoff — failed fast
        assert len(httpx_mock.get_requests()) == 1  # exactly one attempt

    @pytest.mark.asyncio
    async def test_reraises_last_error_after_exhausting_retries(self, httpx_mock):
        # Persistent 5xx: retried up to max_http_retries, then the last error
        # surfaces (rather than a silent empty result).
        for _ in range(3):
            httpx_mock.add_response(status_code=502)
        sleeps: list[float] = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        async with httpx.AsyncClient() as client:
            post = make_post_batch(
                client, "https://lml.example", "k", max_http_retries=3, sleep=fake_sleep
            )
            with pytest.raises(httpx.HTTPStatusError):
                await post(["Wishy"], True)
        assert len(httpx_mock.get_requests()) == 3  # all attempts made
        assert len(sleeps) == 2  # slept between each attempt, not after the last


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

    # --------------------------------------------------------------------- #
    # LML#778 — a persistent HTTP error out of `post_batch` (after it has
    # exhausted its own transport/5xx retries) must degrade the whole page to
    # `escalation_unavailable` and let the round loop re-page it, NOT crash the
    # drain and skip its report. Surfaced by the 2026-07-13 live drain, which
    # aborted twice on `httpx.ReadTimeout` (slow-minting pages + LML#755 shed).
    # --------------------------------------------------------------------- #
    async def test_http_error_page_degrades_to_escalation_then_completes(self, tmp_path):
        out = tmp_path / "d.jsonl"
        calls = {"n": 0}

        async def flaky_post(names, dry_run):
            calls["n"] += 1
            if calls["n"] == 1:
                # post_batch gave up after its retries and raised (the #778 case).
                raise httpx.ReadTimeout("timed out")
            return [_resolved(name, 100 + i) for i, name in enumerate(names)]

        records = await run_drain(
            all_names=["Wishy", "REZN"],
            dry_run=False,
            out_path=out,
            post_batch=flaky_post,
            page_size=25,
            max_retries=2,
            cooldown=0,
            sleep=_noop_sleep,
            clock=lambda: "T",
        )

        loaded = load_records(out)
        # First attempt: the timed-out page is recorded as escalation_unavailable
        # for every name, shaped like a 200-level escalation verdict — no crash.
        first = [r for r in loaded if r["attempt"] == 1]
        assert {r["name"] for r in first} == {"Wishy", "REZN"}
        assert all(r["unresolved_reason"] == "escalation_unavailable" for r in first)
        assert all(r["discogs_artist_id"] is None for r in first)
        assert all(r["method"] is None for r in first)
        assert all(r["candidate_count"] is None for r in first)
        assert all(r["cache_corroboration"] == [] for r in first)
        # The run reached completion: the retry round minted both.
        latest = latest_by_name(loaded)
        assert latest["Wishy"]["discogs_artist_id"] == 100
        assert latest["REZN"]["discogs_artist_id"] == 101
        assert records is not None  # returned normally; no exception escaped run_drain

    async def test_persistent_http_error_settles_as_residual_without_crashing(self, tmp_path):
        out = tmp_path / "d.jsonl"

        async def always_timeout(names, dry_run):
            raise httpx.ReadTimeout("still timing out")

        records = await run_drain(
            all_names=["Wishy"],
            dry_run=False,
            out_path=out,
            post_batch=always_timeout,
            page_size=25,
            max_retries=2,  # 3 attempts total
            cooldown=7,
            sleep=_noop_sleep,
            clock=lambda: "T",
        )

        loaded = load_records(out)
        # Paged the full retry budget, every attempt an escalation, then settled
        # as residual — and the run still returned instead of crashing.
        assert attempt_counts(loaded)["Wishy"] == 3
        assert latest_by_name(loaded)["Wishy"]["unresolved_reason"] == "escalation_unavailable"
        assert records is not None

    async def test_drain_error_still_propagates_not_degraded(self, tmp_path):
        # A contract break (bad length / index misalignment) is not a transient
        # "couldn't ask" — it must fail loudly, never degrade to a verdict.
        async def contract_break(names, dry_run):
            raise DrainError("index misalignment")

        with pytest.raises(DrainError):
            await run_drain(
                all_names=["Wishy"],
                dry_run=False,
                out_path=tmp_path / "d.jsonl",
                post_batch=contract_break,
                page_size=25,
                max_retries=2,
                cooldown=0,
                sleep=_noop_sleep,
                clock=lambda: "T",
            )

    async def test_client_error_4xx_propagates_not_degraded(self, tmp_path):
        # A 4xx is a misconfig (bad key / oversized page), not "couldn't ask" —
        # surfacing it beats silently degrading every page to escalation residual,
        # which would mask e.g. a wrong API key as a transient breaker shed.
        request = httpx.Request("POST", "https://lml.example/api/v1/artists/resolve/bulk")
        response = httpx.Response(401, request=request)

        async def unauthorized(names, dry_run):
            raise httpx.HTTPStatusError("401 Unauthorized", request=request, response=response)

        with pytest.raises(httpx.HTTPStatusError):
            await run_drain(
                all_names=["Wishy"],
                dry_run=False,
                out_path=tmp_path / "d.jsonl",
                post_batch=unauthorized,
                page_size=25,
                max_retries=2,
                cooldown=0,
                sleep=_noop_sleep,
                clock=lambda: "T",
            )


async def _noop_sleep(seconds):
    return None
