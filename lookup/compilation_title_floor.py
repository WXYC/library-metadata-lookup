"""The compilation-artist title carve-out: one admission policy, two callers.

``TRACK_ON_COMPILATION``'s artist-verification phase cannot ask "is this row
credited to the release's artist?" of a Various-Artists row: the release credit
and the library's filing name agree on nothing. So a V/A row is admitted on its
*title* instead, and this module is that admission -- the ``fuzz.ratio`` floor
and, behind its ``LML_TIGHTEN_COMPILATION_TITLE_CARVEOUT`` kill switch, the
LML#973 length-comparability guard.

Both branches of ``_filter_release_matches`` in
``lookup/strategies/track_on_compilation.py`` ask it -- the strict branch and
the album-title fallback's ``_fallback_row_acceptable`` -- and the two must
admit exactly the same class, because a row the fallback admits is bound to
the same release with the same confidence as one the strict branch admits.

Moved verbatim out of that strategy module (LML#1369 prep), which sat at its
``tests/unit/test_module_budgets.py`` ceiling with no headroom, following the
``lookup/typed_pair_floor.py`` precedent: a shared match floor is a concern,
not a utility drawer. The signature is unchanged -- callers still derive
``title_score`` and ``length_ratio`` themselves -- so this diff is a pure
move; consolidating that derivation is left to the behavior change that
follows.
"""

from config.settings import get_settings

_COMPILATION_TITLE_RATIO_FLOOR = 80
_COMPILATION_TITLE_LENGTH_RATIO_FLOOR = 0.9
"""LML#973: the compilation carve-out's ``fuzz.ratio`` floor alone can't tell
"same comp, reformatted title" from "different comp, similarly-worded title" —
e.g. "Greatest Hits Of The 50's" vs "Greatest hits of the 50s & 60s" clears
``ratio >= 80`` (87.3) even though the second title names a different,
track-less pressing. Requiring the shorter title to be at least 90% the
length of the longer catches that case (length ratio 0.83) while a genuine
reformatting — punctuation, an added "!", "Vol." vs "Vol" — stays
length-comparable and still clears both floors. Gated behind
``LML_TIGHTEN_COMPILATION_TITLE_CARVEOUT`` (default True); False restores
the pre-#973 ratio-floor-only admission without a redeploy."""


def _compilation_title_length_ratio(a: str, b: str) -> float:
    """``min(len)/max(len)`` of two titles, in [0, 1]; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    return min(len(a), len(b)) / max(len(a), len(b))


def _compilation_title_carveout_admits(title_score: float, length_ratio: float) -> bool:
    """Whether a compilation-artist row's title clears the carve-out's
    admission test. Always requires the ``fuzz.ratio`` floor. When
    ``LML_TIGHTEN_COMPILATION_TITLE_CARVEOUT`` is True (default), also
    requires the LML#973 length-comparability guard; False is the kill
    switch that restores the pre-#973 ratio-floor-only admission without a
    redeploy."""
    if title_score < _COMPILATION_TITLE_RATIO_FLOOR:
        return False
    if get_settings().lml_tighten_compilation_title_carveout:
        return length_ratio >= _COMPILATION_TITLE_LENGTH_RATIO_FLOOR
    return True
