"""Unit test for ``scripts/audit_va_writeback_pollution.py``.

Pins the wxyc-etl 0.5.0 anchored-matcher classification at the audit call
site. ``classify()`` is a pure split over ``is_compilation_artist`` so this
test exercises both the skip set (V/A bucket filings) and the keep set
(real-artist false-positive controls) without touching a database.
"""

from __future__ import annotations

import re

import pytest

from scripts.audit_va_writeback_pollution import SUPERSET_REGEX, classify

_NET = re.compile(SUPERSET_REGEX)


@pytest.mark.parametrize(
    "library_name",
    [
        "various artists - blues",
        "v/a",
        "v/a - soundtracks",
        "v.a.",
        # LML#1139: the dotless family. The original `v\.a\.` alternative
        # required a trailing dot, so these were arbiter-True and net-False —
        # `--apply` left them classified as clean and never deleted them.
        "v.a",
        "v.a - jazz",
        "v.a 1998",
        "soundtrack",
        "compilation",
    ],
)
def test_regex_is_a_true_superset_of_the_arbiter(library_name: str):
    """The module docstring calls the regex a SUPERSET of the arbiter. Since the
    SQL net is what bounds the scan, any arbiter-positive name the net misses is
    pollution the audit can never see — so the claim has to be enforced, not
    just asserted in prose."""
    assert classify([(1, library_name)])[0], f"{library_name!r} should be arbiter-positive"
    assert _NET.search(library_name) is not None, (
        f"{library_name!r} is confirmed V/A pollution but the coarse net misses it"
    )


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
