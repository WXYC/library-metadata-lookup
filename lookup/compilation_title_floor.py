"""The compilation-artist title carve-out — one admission policy, two callers.

``TRACK_ON_COMPILATION``'s artist-verification phase cannot ask "is this row
credited to the release's artist?" of a Various-Artists row: the release credit
and the library's filing name agree on nothing. So a V/A row is admitted on its
*title* instead, and this module is that admission: the LML#1369 token-identity
veto, the LML#973 ``fuzz.ratio`` floor, and the LML#973 length-comparability
guard behind its kill switch.

Both of ``_filter_release_matches``'s branches ask it — the strict branch and
the album-title fallback's ``_fallback_row_acceptable`` — and the two must
admit exactly the same class, because a row the fallback admits is bound to the
same release with the same confidence as one the strict branch admits.

Hoisted out of ``lookup/strategies/track_on_compilation.py`` by LML#1369, at
that file's ``tests/unit/test_module_budgets.py`` ceiling, following the
``lookup/typed_pair_floor.py`` precedent: a shared match floor is a concern,
not a utility drawer. The predicate takes the two titles rather than
pre-computed scores, because before this both call sites derived
``title_score`` and ``length_ratio`` themselves — a parity the callers were
trusted to maintain by hand, with no test driving both over one table.
"""

from config.settings import get_settings
from lookup.title_identity import titles_differ_by_discriminating_token

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


def compilation_title_length_ratio(a: str, b: str) -> float:
    """``min(len)/max(len)`` of two titles, in [0, 1]; 0.0 if either is empty."""
    if not a or not b:
        return 0.0
    return min(len(a), len(b)) / max(len(a), len(b))


def compilation_title_carveout_admits(release_title_lower: str, match_title_lower: str) -> bool:
    """Whether a compilation-artist row's title clears the carve-out.

    Always requires the ``fuzz.ratio`` floor. When
    ``LML_TIGHTEN_COMPILATION_TITLE_CARVEOUT`` is True (default), also requires
    the LML#973 length-comparability guard; False is the kill switch that
    restores the pre-#973 ratio-floor-only admission without a redeploy.

    LML#1369's token-identity veto sits in front of both, the kill switch
    included. #973's floors reject on title *shape*, and a sibling volume has
    exactly the right shape — "Art Of Field Recording Volume I" against "Art of
    Field Recording, vol. 2" scores 87.1 with a 0.938 length ratio and clears
    both. The switch exists to restore pre-#973 *ratio* behavior if that guard
    costs recall in prod; it is not a switch for binding one volume's release
    to another volume's row.
    """
    from rapidfuzz import fuzz

    if titles_differ_by_discriminating_token(release_title_lower, match_title_lower):
        return False
    if fuzz.ratio(release_title_lower, match_title_lower) < _COMPILATION_TITLE_RATIO_FLOOR:
        return False
    if not get_settings().lml_tighten_compilation_title_carveout:
        return True
    length_ratio = compilation_title_length_ratio(release_title_lower, match_title_lower)
    return length_ratio >= _COMPILATION_TITLE_LENGTH_RATIO_FLOOR
