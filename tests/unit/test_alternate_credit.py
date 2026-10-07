"""``lookup/alternate_credit.py::credits_artist`` (LML#1425 decision 5)."""

import pytest

from lookup.alternate_credit import credits_artist


@pytest.mark.parametrize(
    ("credit", "artist", "expected"),
    [
        pytest.param("Plug", "Plug", True, id="equal"),
        pytest.param("PLUG", "plug", True, id="case-folds"),
        pytest.param("Nilüfer Yanya", "nilufer yanya", True, id="diacritics-fold"),
        pytest.param("  Aki  Onda ", "Aki Onda", True, id="whitespace-folds"),
        pytest.param(
            "Afel Bocoum, Damon Albarn, Toumani Diabate and friends",
            "Afel Bocoum",
            True,
            id="first-name-before-a-comma",
        ),
        pytest.param("Andy Summers & Robert Fripp", "Andy Summers", True, id="ampersand"),
        pytest.param("Jessica Pratt and Friends", "Jessica Pratt", True, id="and"),
        pytest.param("Sessa / Ana Frango Eletrico", "Sessa", True, id="slash"),
        pytest.param(
            "Afel Bocoum, Damon Albarn, Toumani Diabate and friends",
            "Damon Albarn",
            False,
            id="a-later-name-does-not-count",
        ),
        pytest.param("Andy Summers & Robert Fripp", "Robert Fripp", False, id="name-after-and"),
        pytest.param("Agnes Obel & Friends", "Agnes", False, id="prefix-inside-a-name"),
        pytest.param("Stereolab", "Stereo", False, id="prefix-of-a-single-name"),
        pytest.param("Sun Ra Arkestra", "Sun Ra", False, id="longer-name"),
        pytest.param("Sun Ra- Arkestra", "Sun Ra", False, id="other-punctuation-is-no-separator"),
        pytest.param("Sun Ra,Arkestra", "Sun Ra", False, id="separator-needs-its-spaces"),
        pytest.param("Andy Summers & Robert Fripp", "", False, id="blank-artist"),
        pytest.param("", "Plug", False, id="blank-credit"),
        pytest.param(None, "Plug", False, id="no-credit"),
    ],
)
def test_credits_artist(credit, artist, expected):
    assert credits_artist(credit, artist) is expected
