"""Unit tests for the synthetic-typo positive-class generator.

The production use case is listener-typed artist names like "Robotnik" / "Robotnick".
The synthetic generator must produce a corruption distribution that approximates
real listener typos: single-letter deletion, transposition of adjacent letters,
ASCII-folding of diacritics, and single-letter duplication.

Determinism via ``seed`` is required so calibration runs are reproducible.
"""

from __future__ import annotations

import pytest

from scripts.resolver_calibration.synthetic_typos import (
    DIACRITIC_PAIRS,
    generate_typos,
)


class TestGenerateTypos:
    """Pure-Python corruption generator."""

    def test_too_short_name_returns_empty(self):
        """Names with fewer than 4 alpha chars are not corrupted — the trigram
        similarity floor breaks down on short strings and any corruption
        produces a different artist entirely."""
        assert generate_typos("ab", seed=0) == []
        assert generate_typos("abc", seed=0) == []

    def test_returns_at_most_max_typos(self):
        """``max_typos`` is a hard cap — calibration runs millions of probes
        and an unbounded generator would blow up the sample size."""
        typos = generate_typos("Stereolab", seed=42, max_typos=2)
        assert len(typos) <= 2

    def test_excludes_original_name(self):
        """A corruption that happens to round-trip back to the original
        (e.g., transpose of two identical letters) must not leak through."""
        typos = generate_typos("Stereolab", seed=42, max_typos=5)
        assert "Stereolab" not in typos
        assert "stereolab" not in typos

    def test_deterministic_given_seed(self):
        """Same seed → same output. Calibration sweeps are reproducible."""
        a = generate_typos("Stereolab", seed=42, max_typos=3)
        b = generate_typos("Stereolab", seed=42, max_typos=3)
        assert a == b

    def test_different_seeds_produce_different_outputs(self):
        """Determinism check is symmetric — different seeds shouldn't lock to
        the same output (probabilistic, but very unlikely for max_typos>=2)."""
        a = generate_typos("Stereolab", seed=1, max_typos=3)
        b = generate_typos("Stereolab", seed=99, max_typos=3)
        # at least one corruption should differ between the two seed runs
        assert a != b

    def test_produces_drop_letter_corruption(self):
        """The drop-letter class is the most common listener typo —
        'Robotnick' → 'Robotnik'. Across many seeds at least one drop
        should appear for any reasonable-length name."""
        # collect corruptions across many seeds to confirm the class exists
        seen = set()
        for s in range(50):
            for typo in generate_typos("Stereolab", seed=s, max_typos=5):
                # a drop-letter corruption has length len(name)-1
                if len(typo) == len("Stereolab") - 1:
                    seen.add(typo)
        assert len(seen) > 0, "drop-letter class never appeared across 50 seeds"

    def test_produces_transpose_corruption(self):
        """Transpose preserves length; verify the class fires."""
        seen = set()
        for s in range(50):
            for typo in generate_typos("Stereolab", seed=s, max_typos=5):
                # transpose preserves length and is not just a case fold
                if len(typo) == len("Stereolab") and typo != "stereolab":
                    # sorted chars match — confirms transpose vs other corruption
                    if sorted(typo) == sorted("Stereolab"):
                        seen.add(typo)
        assert len(seen) > 0, "transpose class never appeared across 50 seeds"

    def test_produces_ascii_fold_for_diacritic_name(self):
        """ASCII-folding is the listener-keyboard case: someone without easy
        diacritic input types 'Nilufer Yanya' for 'Nilüfer Yanya'.
        For names with no diacritics, this class should not appear."""
        typos = generate_typos("Nilüfer Yanya", seed=0, max_typos=10)
        assert any("ü" not in t and "u" in t.lower() for t in typos), (
            f"expected at least one ASCII-folded variant of 'Nilüfer Yanya', got: {typos}"
        )

    def test_no_ascii_fold_for_plain_ascii_name(self):
        """Negative complement: plain ASCII names produce only the other
        three corruption classes."""
        typos = generate_typos("Stereolab", seed=0, max_typos=10)
        # all typos should still be ASCII (no diacritics introduced)
        for t in typos:
            assert t.isascii(), f"unexpected non-ASCII char in corruption of plain name: {t!r}"

    def test_produces_duplicate_letter_corruption(self):
        """Duplicate-letter class (insertion variant): 'Stereolab' → 'Steereolab'.
        Length is one more than the original."""
        seen = set()
        for s in range(50):
            for typo in generate_typos("Stereolab", seed=s, max_typos=5):
                if len(typo) == len("Stereolab") + 1:
                    seen.add(typo)
        assert len(seen) > 0, "duplicate-letter class never appeared across 50 seeds"

    def test_handles_name_with_spaces(self):
        """Multi-word names like 'Cat Power' should still produce corruptions —
        spaces are skipped over for the letter-level corruptions but the
        result is still a valid string."""
        typos = generate_typos("Cat Power", seed=0, max_typos=3)
        assert all(isinstance(t, str) and len(t) > 0 for t in typos)

    def test_handles_unicode_uppercase_diacritic(self):
        """Diacritic-fold must work for uppercase too — 'Éliane Radigue' should
        produce 'Eliane Radigue'."""
        typos = generate_typos("Éliane Radigue", seed=0, max_typos=10)
        assert any("É" not in t and "E" in t for t in typos), (
            f"expected uppercase ASCII fold, got: {typos}"
        )


class TestDiacriticPairs:
    """Smoke checks on the diacritic table itself."""

    def test_diacritic_pairs_are_lowercase_pairs(self):
        """All entries are (lowercase-diacritic, lowercase-ascii) — the
        generator handles the uppercase mapping by .upper()-ing both sides."""
        for fancy, plain in DIACRITIC_PAIRS:
            assert fancy == fancy.lower()
            assert plain == plain.lower()
            assert plain.isascii()
            assert not fancy.isascii()

    @pytest.mark.parametrize(
        "fancy,plain",
        [("é", "e"), ("ü", "u"), ("ñ", "n"), ("ç", "c")],
    )
    def test_common_diacritics_present(self, fancy, plain):
        """The table covers the diacritics that show up in WXYC catalog
        artist names: Nilüfer Yanya, Hermanos Gutiérrez, Csillagrablók."""
        assert (fancy, plain) in DIACRITIC_PAIRS
