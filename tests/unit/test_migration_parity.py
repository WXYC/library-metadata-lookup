"""Parity tests: verify wxyc_etl functions produce identical output to core/matching.py originals.

This file is temporary. It will be removed after the migration is complete
because the old import paths will no longer exist.
"""

import pytest

from core.matching import (
    is_compilation_artist as local_is_compilation,
    normalize_for_comparison as local_normalize,
    strip_diacritics as local_strip,
)
from wxyc_etl.text import (
    is_compilation_artist as etl_is_compilation,
    normalize_artist_name as etl_normalize,
    strip_diacritics as etl_strip,
)


# ---------------------------------------------------------------------------
# normalize_for_comparison vs normalize_artist_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Bjork", id="ascii"),
        pytest.param("Juana Molina", id="multi-word"),
        pytest.param("Duke Ellington & John Coltrane", id="ampersand"),
        pytest.param("Chuquimamani-Condori", id="hyphen"),
        pytest.param("Zoé", id="diacritics"),
        pytest.param("Björk", id="umlaut"),
        pytest.param("SIGUR RÓS", id="uppercase-diacritics"),
        pytest.param("Motörhead", id="umlaut-mid"),
        pytest.param("", id="empty"),
        pytest.param(None, id="none"),
    ],
)
def test_normalization_parity(text):
    assert local_normalize(text) == etl_normalize(text)


def test_normalization_whitespace_trimming():
    """wxyc_etl trims whitespace; local does not.

    This is a known behavioral improvement. All callers either chain .strip()
    or use the result for comparison where leading/trailing whitespace is
    irrelevant. Document rather than assert equal.
    """
    assert local_normalize("  Stereolab  ") == "  stereolab  "
    assert etl_normalize("  Stereolab  ") == "stereolab"


# ---------------------------------------------------------------------------
# strip_diacritics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("Björk", id="bjork"),
        pytest.param("Sigur Rós", id="sigur-ros"),
        pytest.param("Zoé", id="zoe"),
        pytest.param("Motörhead", id="motorhead"),
        pytest.param("Godspeed You! Black Emperor", id="punctuation"),
        pytest.param("Bjork", id="ascii-only"),
        pytest.param("", id="empty"),
        pytest.param("Hüsker Dü", id="husker-du"),
        pytest.param("Café Tacvba", id="cafe-tacvba"),
    ],
)
def test_strip_diacritics_parity(text):
    assert local_strip(text) == etl_strip(text)


# ---------------------------------------------------------------------------
# is_compilation_artist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "artist",
    [
        pytest.param("Various Artists", id="various-artists"),
        pytest.param("VARIOUS", id="various-upper"),
        pytest.param("various", id="various-lower"),
        pytest.param("Soundtrack Collection", id="soundtrack"),
        pytest.param("V/A", id="v-slash-a"),
        pytest.param("v.a.", id="v-dot-a"),
        pytest.param("Stereolab", id="not-compilation"),
        pytest.param("DJ Shadow", id="not-compilation-dj"),
        pytest.param("", id="empty"),
    ],
)
def test_compilation_parity(artist):
    assert local_is_compilation(artist) == etl_is_compilation(artist)
