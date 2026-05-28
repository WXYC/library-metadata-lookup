"""Unit test for ``scripts/audit_va_writeback_pollution.py``.

Pins the wxyc-etl 0.5.0 anchored-matcher classification at the audit call
site. ``classify()`` is a pure split over ``is_compilation_artist`` so this
test exercises both the skip set (V/A bucket filings) and the keep set
(real-artist false-positive controls) without touching a database.
"""

from __future__ import annotations

from scripts.audit_va_writeback_pollution import classify


def test_classify_splits_v_a_filings_from_real_artists():
    """Four-artist mix: keep `Hermanos Gutiérrez` + `Epic Soundtracks`; drop the rest."""
    rows = [
        (1, "Soundtracks - A"),
        (2, "Various Artists - Rock - C"),
        (3, "Hermanos Gutiérrez"),
        (4, "Epic Soundtracks"),
    ]

    delete, keep = classify(rows)

    assert [name for _, name in delete] == ["Soundtracks - A", "Various Artists - Rock - C"]
    assert [name for _, name in keep] == ["Hermanos Gutiérrez", "Epic Soundtracks"]


def test_classify_preserves_id_pairs():
    """Both buckets carry the (id, name) tuples through unchanged."""
    rows = [(99, "V/A - Soundtracks"), (42, "The 27 Various")]

    delete, keep = classify(rows)

    assert delete == [(99, "V/A - Soundtracks")]
    assert keep == [(42, "The 27 Various")]


def test_classify_empty_input():
    assert classify([]) == ([], [])
