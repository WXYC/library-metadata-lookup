"""Tests for ``scripts/discogs_ranking_jitter.py`` (LML#537 probe #3).

The script issues live Discogs calls; tests cover the pure math (stats,
position stability, aggregate, serialization) and the response-extraction
helper. The HTTP wire is mocked via ``httpx.MockTransport`` so the real
Discogs endpoint is never touched.
"""

from __future__ import annotations

import math

import httpx
import pytest

from scripts.discogs_ranking_jitter import (
    JitterPair,
    _extract_release_ids,
    aggregate,
    run_pair,
)


def test_stats_jaccard_one_when_identical():
    pair = JitterPair(call_a_ids=[1, 2, 3], call_b_ids=[1, 2, 3], delay_seconds=30)
    overlap, union, jaccard = pair.stats()
    assert (overlap, union, jaccard) == (3, 3, 1.0)
    assert pair.position_stability() == 0.0


def test_stats_jaccard_zero_when_disjoint():
    pair = JitterPair(call_a_ids=[1, 2], call_b_ids=[3, 4], delay_seconds=30)
    overlap, union, jaccard = pair.stats()
    assert (overlap, union, jaccard) == (0, 4, 0.0)
    assert pair.position_stability() is None


def test_stats_partial_overlap_and_rank_shift():
    """Overlap of {1, 2} out of union {1, 2, 3, 4, 5}; ranks shifted."""
    pair = JitterPair(call_a_ids=[1, 2, 3], call_b_ids=[2, 1, 4, 5], delay_seconds=30)
    overlap, union, jaccard = pair.stats()
    assert overlap == 2
    assert union == 5
    assert jaccard == pytest.approx(0.4)
    # Rank-delta: 1 is at index 0 in A and 1 in B (delta 1). 2 is at 1 in A
    # and 0 in B (delta 1). Mean = 1.0
    assert pair.position_stability() == 1.0


def test_stats_empty_lists():
    pair = JitterPair(call_a_ids=[], call_b_ids=[], delay_seconds=30)
    assert pair.stats() == (0, 0, 0.0)
    assert pair.position_stability() is None


def test_to_record_includes_derived_metrics():
    """`--json` output must surface jaccard/overlap/union/stability per pair —
    using `dataclass.__dict__` would have dropped them (LML#537 review)."""
    pair = JitterPair(call_a_ids=[1, 2], call_b_ids=[2, 3], delay_seconds=30)
    record = pair.to_record()

    assert record["call_a_ids"] == [1, 2]
    assert record["call_b_ids"] == [2, 3]
    assert record["delay_seconds"] == 30
    assert record["overlap"] == 1
    assert record["union"] == 3
    assert record["jaccard"] == pytest.approx(1 / 3)
    assert record["position_stability"] == pytest.approx(1.0)


def test_extract_release_ids_skips_rows_without_id():
    payload = {"results": [{"id": 1}, {"id": 2, "title": "x"}, {"title": "no id"}]}
    assert _extract_release_ids(payload) == [1, 2]


def test_aggregate_means_and_min_max():
    pairs = [
        JitterPair(call_a_ids=[1, 2], call_b_ids=[1, 2], delay_seconds=30),  # jaccard 1.0, stab 0
        JitterPair(call_a_ids=[1, 2], call_b_ids=[3, 4], delay_seconds=30),  # jaccard 0.0, no stab
    ]
    agg = aggregate(pairs)
    assert agg["pairs"] == 2
    assert agg["jaccard_mean"] == 0.5
    assert agg["jaccard_min"] == 0.0
    assert agg["jaccard_max"] == 1.0
    assert agg["position_stability_mean"] == 0.0


def test_aggregate_no_shared_ids_yields_nan_stability():
    pairs = [JitterPair(call_a_ids=[1], call_b_ids=[2], delay_seconds=30)]
    agg = aggregate(pairs)
    assert math.isnan(agg["position_stability_mean"])


def test_aggregate_empty():
    assert aggregate([]) == {"pairs": 0}


@pytest.mark.asyncio
async def test_run_pair_issues_two_calls_and_extracts_ids(monkeypatch):
    """End-to-end of run_pair against a mocked Discogs transport. The two
    responses differ — the script must read each independently."""
    payloads = iter(
        [
            {"results": [{"id": 1}, {"id": 2}, {"id": 3}]},
            {"results": [{"id": 2}, {"id": 4}]},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(payloads))

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        # No actual sleep — fast-forward the delay.
        async def _no_sleep(seconds):
            return None

        monkeypatch.setattr("scripts.discogs_ranking_jitter.asyncio.sleep", _no_sleep)
        pair = await run_pair(
            client,
            track="Track",
            artist="Artist",
            delay_seconds=0,
            token="fake-token",
            limit=10,
        )

    assert pair.call_a_ids == [1, 2, 3]
    assert pair.call_b_ids == [2, 4]
    assert pair.stats()[0] == 1  # overlap
