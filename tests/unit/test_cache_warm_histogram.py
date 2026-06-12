"""Tests for ``scripts/cache_warm_histogram.py`` (LML#537 probe #2)."""

from __future__ import annotations

import datetime as dt
import io

from scripts.cache_warm_histogram import emit_csv, summarise


def test_summarise_splits_on_cutoff():
    rows = [
        (dt.date(2026, 6, 1), 100),
        (dt.date(2026, 6, 2), 200),
        (dt.date(2026, 6, 6), 50),
        (dt.date(2026, 6, 7), 30),  # cutoff day — counts as "after"
        (dt.date(2026, 6, 8), 40),
        (dt.date(2026, 6, 9), 35),
    ]
    summary = summarise(rows, dt.date(2026, 6, 7))

    assert summary["rows_before"] == 350
    assert summary["rows_after"] == 105
    assert summary["days_before"] == 3
    assert summary["days_after"] == 3
    assert summary["total_rows"] == 455
    assert summary["mean_before"] == 350 / 3
    assert summary["mean_after"] == 35.0


def test_summarise_handles_no_post_cutoff_days():
    """The post-deploy regime might be empty if the cutoff is in the future."""
    rows = [(dt.date(2026, 6, 1), 100)]
    summary = summarise(rows, dt.date(2026, 6, 10))

    assert summary["rows_after"] == 0
    assert summary["mean_after"] == 0.0
    assert summary["mean_before"] == 100.0


def test_summarise_handles_empty_input():
    summary = summarise([], dt.date(2026, 6, 7))
    assert summary["total_rows"] == 0
    assert summary["mean_before"] == 0.0
    assert summary["mean_after"] == 0.0


def test_emit_csv_round_trips():
    rows = [(dt.date(2026, 6, 7), 30), (dt.date(2026, 6, 8), 40)]
    out = io.StringIO()
    emit_csv(rows, out)
    assert out.getvalue().strip().splitlines() == [
        "day,rows",
        "2026-06-07,30",
        "2026-06-08,40",
    ]
