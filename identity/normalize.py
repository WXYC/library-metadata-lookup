"""Artist-name canonicalization for the cross-cache-identity bulk lookup.

Issue WXYC/library-metadata-lookup#274 — `POST /api/v1/identity/bulk-resolve-libraries`
looks up ``entity.identity.library_name`` via exact match. Backend posts
``library.artist_name`` verbatim from its denormalized column, so any
divergence from the stored canonical form would surface as a silent
unresolved verdict.

This module implements **approach 2** from the issue: pre-canonicalize the
input in LML and exact-match against the stored canonical form. The bridge
holds until the Postgres analog of ``wxyc_etl::text::to_identity_match_form``
ships (plan §3.3 step 4), at which point the lookup can move to a
``WHERE wxyc_norm_artist(library_name) = wxyc_norm_artist($1)`` functional
index and the dependency on stored-row canonicality goes away.

The canonical form layered here on top of
``wxyc_etl.text.to_identity_match_form`` adds two extra folds that the
shared normalizer does not yet do but the issue lists as observed
divergence vectors:

- Smart-quote / modifier-letter apostrophe folds (`’`, `‘`, `ʼ` → `'`).
- Conjunction fold (``Sleater & Kinney`` ↔ ``Sleater and Kinney``).

Whitespace runs collapse to a single ASCII space, and the result is
lower-cased + leading/trailing-trimmed by ``to_identity_match_form``.
"""

from __future__ import annotations

import re

from wxyc_etl.text import to_identity_match_form

# Curly / modifier apostrophes that Backend's column may carry. Mapped to
# ASCII apostrophe U+0027 so ``Don't`` and ``Don’t`` resolve to the same row.
# Kept narrow on purpose — only apostrophe-shaped marks. Double-quote
# variants would land in a different equivalence class and are not the
# divergence vector the issue lists.
_APOSTROPHE_FOLD = str.maketrans(
    {
        "‘": "'",  # LEFT SINGLE QUOTATION MARK
        "’": "'",  # RIGHT SINGLE QUOTATION MARK
        "ʼ": "'",  # MODIFIER LETTER APOSTROPHE
        "ʹ": "'",  # MODIFIER LETTER PRIME
    }
)

# ``&`` is the conjunction fold key when it sits between whitespace
# (``Sleater & Kinney``). When it's glued to a token (``M&M``, ``A&E``)
# it's typographic, not the conjunction, so we leave it alone.
_AMPERSAND_CONJUNCTION_RE = re.compile(r"\s+&\s+")

# Final whitespace pass. ``to_identity_match_form`` trims and lowercases,
# but does not collapse interior runs introduced by upstream folds.
_WHITESPACE_RUN_RE = re.compile(r"\s+")


def canonicalize_for_identity_lookup(name: str) -> str:
    """Return the canonical form of ``name`` for ``entity.identity`` lookups.

    Layered transform:

    1. Apostrophe fold (curly / modifier → ASCII).
    2. Conjunction fold (`` & `` → `` and ``).
    3. ``wxyc_etl.text.to_identity_match_form`` (NFKC, lowercase, diacritic
       strip, trailing-paren strip, leading-article drop).
    4. Whitespace-run collapse.

    Accepts the empty string (returns ``""``) so callers don't need to guard
    optional-string inputs. Idempotent.
    """
    if not name:
        return ""
    folded = name.translate(_APOSTROPHE_FOLD)
    folded = _AMPERSAND_CONJUNCTION_RE.sub(" and ", folded)
    normalized = to_identity_match_form(folded)
    return _WHITESPACE_RUN_RE.sub(" ", normalized).strip()
