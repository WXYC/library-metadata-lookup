"""Tests for ``scripts/cache_miss_provenance.py`` (LML#537 probe #1)."""

from __future__ import annotations

import io
from unittest.mock import MagicMock

from scripts.cache_miss_provenance import (
    MissEvent,
    classify,
    compute_first_seen,
    emit_csv,
    extract_from_log,
)


def _fake_conn(rows: list[tuple[int, int]]):
    """Build a psycopg-shaped mock that yields ``rows`` from ``fetchall()``."""
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=None)
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn


def test_extract_pulls_release_ids_from_validate_log_lines():
    lines = [
        "2026-06-11T07:42:01.123Z INFO Validated: 'Aluminum Tunes' by 'Stereolab' found on release 9876",
        "2026-06-11T07:42:02.456Z INFO Track 'Yoshimura' by 'X' NOT found on release 11111",
        "2026-06-11T07:42:03.789Z INFO Validated (fuzzy): 'Foo' by 'Bar' on release 22222",
        "2026-06-11T07:42:05.000Z INFO unrelated noise",
    ]
    events = extract_from_log(lines)

    assert [e.release_id for e in events] == [9876, 11111, 22222]
    assert all(e.method == "validate_track_on_release" for e in events)
    assert events[0].timestamp == "2026-06-11T07:42:01.123Z"


def test_extract_skips_api_failure_lines():
    """Failed-to-fetch warnings fire on API faults, not cache misses, so the
    extractor must NOT pull them into the H1/H3' provenance sample (LML#537)."""
    lines = [
        "2026-06-11T07:42:04.999Z WARNING Failed to fetch release 33333 (rate limited or error)",
    ]
    assert extract_from_log(lines) == []


def test_classify_release_existed_with_tracks_is_h3prime_candidate():
    """When the release row was already present *with* track rows but the
    seam still missed, we suspect H3' (validate False-as-hit). The
    classifier must surface this distinction."""
    events = [MissEvent(release_id=9876, method="validate_track_on_release", timestamp="t")]
    indexed = list(enumerate(events))
    first_seen = compute_first_seen(events)
    conn = _fake_conn([(9876, 7)])  # release exists with 7 tracks

    rows = classify(conn, indexed, first_seen)

    assert rows == [
        {
            "timestamp": "t",
            "method": "validate_track_on_release",
            "release_id": 9876,
            "release_existed": True,
            "release_track_count": 7,
            "prior_seen_in_window": False,
        }
    ]


def test_classify_release_absent_is_h1_first_time_miss():
    """The H1 case: release row didn't exist at miss time → ETL gap."""
    events = [MissEvent(release_id=12345, method="validate_track_on_release", timestamp="t")]
    indexed = list(enumerate(events))
    first_seen = compute_first_seen(events)
    conn = _fake_conn([])  # no matching row

    rows = classify(conn, indexed, first_seen)

    assert rows[0]["release_existed"] is False
    assert rows[0]["release_track_count"] == 0


def test_classify_marks_prior_seen_within_window():
    """A release_id that appeared earlier in the same input window flags
    the second occurrence — relevant when the warm-up of the same release
    by an earlier validate didn't help the subsequent one."""
    events = [
        MissEvent(release_id=42, method="validate_track_on_release", timestamp="t1"),
        MissEvent(release_id=42, method="validate_track_on_release", timestamp="t2"),
    ]
    indexed = list(enumerate(events))
    first_seen = compute_first_seen(events)
    conn = _fake_conn([(42, 3)])

    rows = classify(conn, indexed, first_seen)

    assert rows[0]["prior_seen_in_window"] is False
    assert rows[1]["prior_seen_in_window"] is True


def test_classify_preserves_prior_seen_across_random_sampling():
    """LML#537 review iter-3 regression: rng.sample() returns elements in
    random order, so the prior_seen_in_window flag must be anchored to
    the original log position (first_seen computed BEFORE sampling) and
    each sampled event must carry its original index forward."""
    # Full log: id=99 appears first, id=42 appears later.
    events = [
        MissEvent(release_id=99, method="validate_track_on_release", timestamp="t1"),
        MissEvent(release_id=42, method="validate_track_on_release", timestamp="t2"),
        MissEvent(release_id=99, method="validate_track_on_release", timestamp="t3"),
    ]
    first_seen = compute_first_seen(events)
    # Sampled in reversed order — simulating what rng.sample might produce.
    indexed_reordered = [(2, events[2]), (0, events[0])]
    conn = _fake_conn([(99, 5)])

    rows = classify(conn, indexed_reordered, first_seen)

    # Row 0 in the sample carries original_idx=2; first_seen[99]=0 < 2 → True.
    assert rows[0]["prior_seen_in_window"] is True
    # Row 1 in the sample carries original_idx=0; first_seen[99]=0 < 0 → False.
    assert rows[1]["prior_seen_in_window"] is False


def test_emit_csv_round_trips():
    rows = [
        {
            "timestamp": "t",
            "method": "validate_track_on_release",
            "release_id": 1,
            "release_existed": True,
            "release_track_count": 5,
            "prior_seen_in_window": False,
        }
    ]
    out = io.StringIO()
    emit_csv(rows, out)
    text = out.getvalue().strip().splitlines()
    assert (
        text[0]
        == "timestamp,method,release_id,release_existed,release_track_count,prior_seen_in_window"
    )
    assert text[1] == "t,validate_track_on_release,1,True,5,False"


def test_classify_handles_empty_input():
    conn = _fake_conn([])
    assert classify(conn, [], {}) == []
