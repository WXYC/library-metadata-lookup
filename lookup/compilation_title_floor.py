"""The compilation-artist title carve-out: one admission policy, two callers.

``TRACK_ON_COMPILATION``'s artist-verification phase cannot ask "is this row
credited to the release's artist?" of a Various-Artists row: the release credit
and the library's filing name agree on nothing. So a V/A row is admitted on its
*title* instead, and this module is that admission. It takes the two lowered
titles, derives the ``fuzz.ratio`` and the length ratio itself, and answers as
a :class:`CarveoutVerdict` that carries those numbers with the decision, so a
caller that logs a rejection prints what the policy actually saw.

Both branches of ``_filter_release_matches`` in
``lookup/strategies/track_on_compilation.py`` call it -- the strict branch and
the album-title fallback's ``_fallback_row_acceptable``. They sit in one
function, so this is not a ``typed_pair_floor.py``-style two-caller parity
seam: the module exists because the strategy file was at its budget ceiling
(LML#1369 prep, on the precedent of ``fallback_artwork.py`` and
``override_floor.py``), and one function owning the derivation is what lets
the strict branch's debug line print the numbers the verdict was judged on.
The floors and the LML#973 rationale behind them are documented once, on the
constants below; the docs and the flag entry point here.
"""

from dataclasses import dataclass

from rapidfuzz import fuzz

from config.settings import get_settings
from lookup.title_identity import title_token_gate_rejects

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


@dataclass(frozen=True)
class CarveoutVerdict:
    """One carve-out decision with the inputs that produced it and, on a
    rejection, which gate fired -- the LML#1369 token gate, the ratio floor or
    the #973 length guard -- with numbers only where they decided it (F6)."""

    admitted: bool
    title_score: float
    length_ratio: float
    reason: str | None = None


def compilation_title_carveout_verdict(
    release_title_lower: str, row_title_lower: str
) -> CarveoutVerdict:
    """Whether a compilation-artist row's title clears the carve-out's
    admission test, with the ``fuzz.ratio`` and length ratio it was judged on.
    Always requires the ``fuzz.ratio`` floor. When
    ``LML_TIGHTEN_COMPILATION_TITLE_CARVEOUT`` is True (default), also
    requires the LML#973 length-comparability guard; False is the kill
    switch that restores the pre-#973 ratio-floor-only admission without a
    redeploy.

    Takes the two lowered titles, not pre-computed scores: this function owns
    the derivation, so both branches of ``_filter_release_matches`` judge and
    log the same numbers.

    The LML#1369 gate (``LML_TITLE_TOKEN_IDENTITY_GATE``, default off) sits in
    front of both floors, kill switch included: #973's floors reject on title
    *shape*, and a sibling volume has the right shape (87.1 / 0.938).

    Tests flip either flag via ``monkeypatch.setenv`` + ``get_settings.cache_clear()``
    or by patching THIS module's ``get_settings`` (the strategy module's does not
    reach here); both functions are value-imported, so to intercept the strategy's
    call patch ``lookup.strategies.track_on_compilation.<name>``."""
    title_score = fuzz.ratio(release_title_lower, row_title_lower)
    length_ratio = compilation_title_length_ratio(release_title_lower, row_title_lower)
    reason = None
    if title_token_gate_rejects(release_title_lower, row_title_lower):
        reason = "title identity (LML#1369)"
    elif title_score < _COMPILATION_TITLE_RATIO_FLOOR:
        reason = f"ratio floor (title_score={title_score:.0f})"
    elif (
        get_settings().lml_tighten_compilation_title_carveout
        and length_ratio < _COMPILATION_TITLE_LENGTH_RATIO_FLOOR
    ):
        reason = f"length guard (title_score={title_score:.0f}, length_ratio={length_ratio:.2f})"
    return CarveoutVerdict(reason is None, title_score, length_ratio, reason)


def compilation_title_carveout_admits(release_title_lower: str, row_title_lower: str) -> bool:
    """:func:`compilation_title_carveout_verdict` for a caller that needs only
    the yes/no."""
    return compilation_title_carveout_verdict(release_title_lower, row_title_lower).admitted
