"""LML#973/#974: the TRACK_ON_COMPILATION title-ratio compilation carve-out.

Extracted from ``track_on_compilation.py`` (LML#974 code review) to keep that
module under its ``tests/unit/test_module_budgets.py`` line ceiling — this is
a self-contained, independently testable concern with no dependency on the
rest of that module's search-pipeline state.

``process_release``'s strict branch and its ``_fallback_row_acceptable``
album-title-fallback counterpart both admit a Various-Artists library row
purely on ``fuzz.ratio(release_album, library_title) >= 80``. Root cause
(LML#959): that ratio alone can't tell "same comp, filed under a shortened
title" apart from "different comp, similarly worded title" -- library row
58775 ("...50s & 60s") fuzz-matched a *different* Discogs compilation
("...50's") that Discogs validated the track on, at ratio ~87, even though
58775's own pressing lacks the track.

``_title_ratio_carveout_admits`` is the shared verdict both call sites use so
the fix can't drift between them. The extra-scope guard on top of the ratio
floor is gated behind ``lml_track_on_compilation_scope_guard`` (LML#974) --
default OFF, so merging it changes no prod behavior; a human flips it on only
after the LML#973 Tier-2 prod recall measurement.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from wxyc_etl.text import to_match_form as normalize_for_comparison

from config.settings import get_settings
from library.db import STOPWORDS

# LML#973: the compilation title-ratio carve-out floor. `process_release`'s
# strict branch and `_fallback_row_acceptable` (its album-title-fallback
# counterpart) both gate a Various-Artists library row admitted purely on
# title fuzz through this one floor plus the scope guard below -- named once
# at module scope so the two call sites can't drift the way a duplicated
# ad hoc `title_score >= 80` literal did pre-#973.
_COMPILATION_TITLE_RATIO_FLOOR = 80

# Below this length a token can't carry independent meaning EXCEPT for the
# compilation-title vocabulary this guard cares about -- decade tokens ("50s",
# "60s") and edition markers ("vol") are exactly 3 characters, so the floor
# here is one lower than the `len(w) > 3` threshold `track_on_compilation.py`'s
# own keyword-search significance filter uses.
_SIGNIFICANT_TITLE_WORD_MIN_LEN = 3


# Straight apostrophe (U+0027), left/right single "curly" quotation marks
# (U+2018/U+2019 -- U+2019 is the standard typographic apostrophe), and the
# modifier letter apostrophe (U+02BC) sometimes used in transliteration. All
# four must normalize identically or a title's *encoding*, not its content,
# can flip the #973 scope guard's verdict (LML#974 code-review finding 2).
_APOSTROPHE_RE = re.compile("['‘’ʼ]")


def _strip_apostrophes(text: str) -> str:
    """Remove every apostrophe variant in ``_APOSTROPHE_RE`` from ``text``.

    A bare ``.replace("'", "")`` only strips the ASCII apostrophe; a
    typographic one (U+2019, e.g. copy-pasted from Discogs) survives to the
    punctuation-stripping regex below, which -- being non-word -- splits
    "50’s" into the throwaway tokens "50" and "s" instead of the single
    significant token "50s" the ASCII spelling produces. Stripping every
    variant up front before tokenizing makes both spellings collapse to the
    same token regardless of which one a given title happens to use.
    """
    return _APOSTROPHE_RE.sub("", text)


def _significant_title_words(title: str) -> set[str]:
    """Content-bearing words in a compilation title, for the #973 scope guard.

    Strips apostrophes (see ``_strip_apostrophes``) before tokenizing so
    "50's" / "50’s" / "50s" all collapse to the same token, then reuses the
    shared ``to_match_form`` normalizer (imported as
    ``normalize_for_comparison``, the same one ``track_on_compilation.py``
    uses elsewhere) to fold diacritics and case consistently (LML#974
    code-review finding 3 -- a hand-rolled ``.lower()`` doesn't strip
    accents, so "Café" and "Cafe" previously read as different significant
    words). Finally drops punctuation, ``STOPWORDS``, and words too short to
    carry meaning alone.
    """
    normalized = normalize_for_comparison(_strip_apostrophes(title))
    words = re.sub(r"[^\w\s]", " ", normalized).split()
    return {w for w in words if len(w) >= _SIGNIFICANT_TITLE_WORD_MIN_LEN and w not in STOPWORDS}


def _library_title_claims_extra_scope(release_album: str, library_title: str) -> bool:
    """Does the library row's title assert content the *validated* Discogs
    release title doesn't?

    Root cause (LML#959): a title-ratio match alone can't tell "same comp,
    filed under a shortened title" apart from "different comp, similarly
    worded title" -- library row 58775 ("...50s & 60s") fuzz-matched the
    Discogs release actually validated to carry the track ("...50's") at
    ratio ~87, but the two are different pressings, and 58775's own copy
    lacks the track. The ratio alone can't separate them (both same-comp
    abbreviations and different-comp near-misses can land at any ratio
    >= 80); what does is that the library title adds a *new* significant
    word ("60s") the validated release's title doesn't have -- it asserts
    scope beyond what was actually validated.

    Deliberately asymmetric: a library title that only *omits* words the
    Discogs title has (the far more common card-catalog pattern -- dropping
    a subtitle, an edition tag, a leading article) is unaffected, since it
    doesn't claim anything the validation didn't cover. Only new words on
    the library side trip this guard.

    A blunt, title-text-only heuristic (see #959's options analysis): it
    won't catch every wrong-pressing collision, and a word-level
    abbreviation that isn't a shared token (e.g. "Volume" -> "Vol") can
    misread as a "new" word. The precision follow-ups -- #959 option 2's
    track/artist-credit gate (blocked on discogs-etl#332) and option 3's
    structural per-row release map -- are what close that gap; #973 is
    "tighten the carve-out" only, by design.
    """
    extra = _significant_title_words(library_title) - _significant_title_words(release_album)
    return bool(extra)


# Reject reasons `_title_ratio_carveout_admits` hands back so both call
# sites can log a greppable, unambiguous verdict (LML#974 item 5) instead of
# the pre-fix strict-branch log, which printed only the (passing-looking,
# >= 80) title_score with no indication *why* the row was still rejected.
_REASON_BELOW_RATIO_FLOOR = "below_ratio_floor"
_REASON_EXTRA_SCOPE = "extra_scope"


class _TitleCarveoutVerdict(NamedTuple):
    admitted: bool
    title_score: float
    reason: str | None
    """``None`` when admitted; otherwise one of the ``_REASON_*`` constants
    above -- what a human greps in Railway logs to size the LML#973 Tier-2
    recall impact."""


def _title_ratio_carveout_admits(release_album: str, library_title: str) -> _TitleCarveoutVerdict:
    """Shared verdict for the two `>= 80` title-ratio compilation carve-out
    sites (`process_release`'s strict branch and `_fallback_row_acceptable`
    in ``track_on_compilation.py``). One function so LML#973's fix can't
    drift between the two call sites.

    The ``>= 80`` ratio floor always applies (unconditionally, unchanged
    from pre-#973 behavior). The extra-scope guard on top of it is gated
    behind ``lml_track_on_compilation_scope_guard`` (LML#974) -- default
    OFF, so merging this guard alone changes no prod behavior. A human
    flips it on in Railway only after the LML#973 Tier-2 prod recall
    measurement; flipping it back off is an instant, fully reversible
    revert to the ratio-only carve-out.
    """
    from rapidfuzz import fuzz as _fuzz

    title_score = _fuzz.ratio(release_album.lower(), library_title.lower())
    if title_score < _COMPILATION_TITLE_RATIO_FLOOR:
        return _TitleCarveoutVerdict(False, title_score, _REASON_BELOW_RATIO_FLOOR)
    if not get_settings().lml_track_on_compilation_scope_guard:
        return _TitleCarveoutVerdict(True, title_score, None)
    if _library_title_claims_extra_scope(release_album, library_title):
        return _TitleCarveoutVerdict(False, title_score, _REASON_EXTRA_SCOPE)
    return _TitleCarveoutVerdict(True, title_score, None)
