"""Unit tests for scripts/match_compilations.py's normalize_comp_title.

LML#1096: characterizes current behavior before consolidating the duplicated
``_FORMAT_SUFFIX_RE`` with clients/streaming/matching.py's canonical copy.
``_BRACKET_RE``/``_PAREN_ANNOTATION_RE`` stay local and unconditional (see the
module-level comment on them) -- compilation-title annotations like
"[techno comp]"/"(14 cds)" are cataloger noise a real compilation title never
legitimately carries, unlike an album title where similar bracket content
(e.g. "[Live]"/"[Disc One]") can be meaningful, so this domain intentionally
strips more aggressively than the conservative canonical regex.
"""

import pytest

from scripts.match_compilations import normalize_comp_title


@pytest.mark.parametrize(
    "title,expected",
    [
        ("CMJ New Music Monthly [techno comp]", "CMJ New Music Monthly"),
        ("Even More Metal Massacre (14 cds)", "Even More Metal Massacre"),
        ('Nuggets 12"', "Nuggets"),
        ("Left of the Dial x 4", "Left of the Dial"),
        ("Just a normal title", "Just a normal title"),
    ],
)
def test_normalize_comp_title(title, expected):
    assert normalize_comp_title(title) == expected
