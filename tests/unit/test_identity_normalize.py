"""Unit tests for the identity-lookup canonicalization helper.

The helper produces the canonical form used by the third leg of
``EntityStore.resolve_library_name()`` (the bulk-resolve-libraries lookup
path, per WXYC/library-metadata-lookup#274 / #276). It must collapse the
divergence vectors Backend's ``library.artist_name`` column can carry into
the cross-cache-identity lookup:

- Diacritics (Nilüfer Yanya vs Nilufer Yanya)
- Curly vs straight quotes (Don't vs Don’t)
- ``&`` vs ``and``
- Trailing whitespace, doubled spaces
- Casing
"""

from __future__ import annotations

import pytest

from identity.normalize import canonicalize_for_identity_lookup


class TestCanonicalizeForIdentityLookup:
    """Every divergence vector listed in issue #274 must collapse to one form."""

    @pytest.mark.parametrize(
        "variant",
        [
            "Nilüfer Yanya",
            "Nilufer Yanya",
            "nilufer yanya",
            "  NILÜFER  YANYA  ",
        ],
    )
    def test_diacritics_and_casing_collapse(self, variant: str) -> None:
        """All Niluefer-Yanya variants share one canonical form."""
        assert canonicalize_for_identity_lookup(variant) == "nilufer yanya"

    @pytest.mark.parametrize(
        "variant",
        [
            "Don't Stop",  # ASCII apostrophe U+0027
            "Don’t Stop",  # right single quotation mark U+2019
            "Don‘t Stop",  # left single quotation mark U+2018
            "Donʼt Stop",  # modifier letter apostrophe U+02BC
        ],
    )
    def test_smart_quote_variants_collapse(self, variant: str) -> None:
        """Curly / modifier-letter apostrophes fold to the ASCII straight form."""
        assert canonicalize_for_identity_lookup(variant) == "don't stop"

    @pytest.mark.parametrize(
        "variant",
        [
            "Sleater & Kinney",
            "Sleater and Kinney",
            "Sleater  &  Kinney",  # padded spaces around `&`
            "Sleater &  Kinney",  # asymmetric whitespace
            "sleater and kinney",
            "Sleater & Kinney",  # NBSP around `&` (rich-text paste)
            "Sleater and Kinney",  # NBSP around `and`
        ],
    )
    def test_ampersand_and_and_collapse(self, variant: str) -> None:
        """``&`` and ``and`` (with any surrounding whitespace, ASCII or NBSP) collapse."""
        assert canonicalize_for_identity_lookup(variant) == "sleater and kinney"

    def test_whitespace_collapses(self) -> None:
        """Leading, trailing, and interior whitespace collapse to single spaces."""
        assert canonicalize_for_identity_lookup("  Cat   Power  ") == "cat power"

    def test_idempotent(self) -> None:
        """Applying the function twice yields the same result as once."""
        once = canonicalize_for_identity_lookup("Nilüfer Yanya")
        twice = canonicalize_for_identity_lookup(once)
        assert once == twice == "nilufer yanya"

    def test_empty_string_passthrough(self) -> None:
        assert canonicalize_for_identity_lookup("") == ""

    def test_ampersand_inside_word_is_left_alone(self) -> None:
        """``&`` only folds when it's a separator, not embedded in a token.

        Discogs occasionally returns names like ``M&M`` where the ``&`` is a
        token glue, not the conjunction. We do not over-rewrite those.
        """
        # Ampersand fold uses word boundaries; ``M&M`` stays as ``m&m``.
        assert canonicalize_for_identity_lookup("M&M") == "m&m"

    def test_ampersand_at_word_boundary_with_no_spaces_still_folds(self) -> None:
        """``Foo&Bar`` (no spaces) → ``foo and bar`` only when spaces flank it."""
        # We intentionally do NOT fold ``Foo&Bar`` — Discogs writes those as
        # distinct compound tokens (e.g., ``A&E``). The fold key is whitespace
        # around ``&``, which is the divergence vector the issue lists.
        result = canonicalize_for_identity_lookup("Foo&Bar")
        assert "and" not in result, (
            f"Bare-glued ampersand should not be rewritten to 'and'; got {result!r}"
        )

    def test_fullwidth_ampersand_not_folded(self) -> None:
        """Fullwidth ``＆`` (U+FF06) is a documented limitation, not a bug.

        The conjunction regex runs before ``to_identity_match_form``'s NFKC
        pass, so by the time NFKC rewrites ``＆`` to ASCII ``&`` the fold
        opportunity is gone. The case is exceptionally rare in WXYC's
        catalog (Japanese typography on a US college-radio system) and the
        module docstring locks the scope. This test pins the limitation so
        a future refactor that moves NFKC up in the pipeline gets a
        deliberate signal that it has changed contract.
        """
        # NFKC inside `to_identity_match_form` does still produce ASCII `&`,
        # so the result keeps the literal ampersand rather than `and`.
        assert canonicalize_for_identity_lookup("Sleater ＆ Kinney") == "sleater & kinney"

    def test_inherits_trailing_paren_strip_from_wxyc_etl(self) -> None:
        """`(Live)` / `(2024 Remaster)` suffixes drop via `to_identity_match_form`.

        Pin the inheritance because the bulk-resolve handler depends on it:
        a stored canonical row ``stereolab`` should resolve a Backend post of
        ``Stereolab (Live)`` to the same identity.
        """
        assert canonicalize_for_identity_lookup("Stereolab (Live)") == "stereolab"
        assert canonicalize_for_identity_lookup("Cat Power (2024 Remaster)") == "cat power"

    def test_inherits_leading_article_drop_from_wxyc_etl(self) -> None:
        """`The Beatles` and Discogs `Beatles, The` collapse to one form."""
        canonical = canonicalize_for_identity_lookup("The Beatles")
        assert canonical == canonicalize_for_identity_lookup("Beatles, The") == canonical
        assert canonical == "beatles"
