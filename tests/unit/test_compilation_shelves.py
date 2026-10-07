"""``lookup/compilation_shelves.py``: Various Artists' compilation shelves (LML#1421)."""

from unittest.mock import AsyncMock

import pytest

from lookup.compilation_shelves import is_compilation_shelf, shelf_rows


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("Various Artists - Rock - H", True, id="genre-and-letter"),
        pytest.param("VARIOUS ARTISTS - Jazz - M", True, id="any-genre-any-case"),
        pytest.param("Soundtracks - S", True, id="soundtracks-letter"),
        pytest.param("Various Artists", False, id="the-plain-shelf-is-own-not-a-shelf"),
        pytest.param("Various Artists [group]", False, id="no-dash"),
        pytest.param("Soundtracks - Sx", False, id="soundtracks-takes-one-letter"),
        pytest.param("Soundtracks of Love", False, id="a-band-named-soundtracks"),
        pytest.param("Various Artistsy - Rock - H", False, id="prefix-is-a-whole-name"),
    ],
)
def test_is_compilation_shelf(name, expected):
    assert is_compilation_shelf(name) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("spellings", [["Stereolab"], [], ["Various Artists [group]"]])
async def test_only_various_artists_reads_the_shelves(spellings):
    db = AsyncMock()

    assert await shelf_rows(db, "Hard as Hell", spellings) == []
    db.artist_names_matching.assert_not_awaited()
    db.search_among.assert_not_awaited()
