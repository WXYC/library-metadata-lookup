"""Unit tests for library/models.py."""

import pytest

from generated.api_models import LibraryCatalogItem
from library.models import LibraryItem, LibrarySearchResponse


class TestLibraryItemCallNumber:
    @pytest.mark.parametrize(
        "kwargs, expected",
        [
            pytest.param(
                {
                    "id": 1,
                    "genre": "Rock",
                    "format": "CD",
                    "call_letters": "Q",
                    "artist_call_number": 1,
                    "release_call_number": 2,
                },
                "Rock CD Q 1/2",
                id="all-fields",
            ),
            pytest.param(
                {
                    "id": 2,
                    "genre": "Rock",
                    "format": "CD",
                    "call_letters": "Q",
                    "artist_call_number": 1,
                },
                "Rock CD Q 1",
                id="no-release-num",
            ),
            pytest.param({"id": 3, "genre": "Jazz"}, "Jazz", id="genre-only"),
            pytest.param({"id": 4, "format": "LP"}, "LP", id="format-only"),
            pytest.param({"id": 5}, "", id="all-none"),
            pytest.param(
                {
                    "id": 6,
                    "genre": "Rock",
                    "call_letters": "Q",
                    "artist_call_number": 5,
                    "release_call_number": 3,
                },
                "Rock Q 5/3",
                id="no-format",
            ),
        ],
    )
    def test_call_number(self, kwargs, expected):
        item = LibraryItem(**kwargs)
        assert item.call_number == expected


def _compilation(**overrides) -> LibraryItem:
    """A library.db-shaped V/A row (Backend's export: `V/A`, artist number 0)."""
    fields = {
        "id": 1,
        "artist": "Various Artists",
        "genre": "Hiphop",
        "format": "cd",
        "call_letters": "V/A",
        "artist_call_number": 0,
        "release_call_number": 651,
    }
    return LibraryItem(**{**fields, **overrides})


class TestLibraryItemCompilationCallNumber:
    """LML#1427: V/A rows are filed by title, not artist, so the regular
    "<Letters> <ArtistNum>/<ReleaseNum>" pattern never existed on the shelf for
    them -- the artist_call_number is always 0 and must not render. Rock and
    Soundtracks additionally split into 26 lettered bins, and the bin letter
    survives in library.db only as a trailing " - <letter>" on `artist`.

    Backend-Service#2822 renders the same rule in TypeScript
    (`computeCallNumber`); the two must agree character for character, so a
    case added here belongs in its `it.each` table too, and vice versa."""

    @pytest.mark.parametrize(
        "overrides, expected",
        [
            pytest.param({}, "Hiphop cd V/A-651", id="single-bin-genre"),
            pytest.param(
                {
                    "artist": "Various Artists - Rock - M",
                    "genre": "Rock",
                    "release_call_number": 121,
                },
                "Rock cd V/A M-121",
                id="rock-with-bin",
            ),
            pytest.param(
                {"artist": "Soundtracks - M", "genre": "Soundtracks", "release_call_number": 12},
                "Soundtracks cd M-12",
                id="soundtracks-with-bin",
            ),
            pytest.param(
                {"genre": "Soundtracks", "release_call_number": 53},
                "Soundtracks cd V/A-53",
                id="soundtracks-without-bin",
            ),
            pytest.param(
                {
                    "artist": "Various Artists - Rock - m",
                    "genre": "Rock",
                    "release_call_number": 121,
                },
                "Rock cd V/A M-121",
                id="lowercase-name-bin-is-uppercased",
            ),
            pytest.param(
                {"call_letters": "  v/a  "}, "Hiphop cd V/A-651", id="lowercase-padded-v/a"
            ),
            pytest.param(
                {"genre": "Rock", "call_letters": "Z-M", "release_call_number": 121},
                "Rock cd V/A M-121",
                id="legacy-z-letter-read-from-code-not-name",
            ),
            pytest.param(
                {"genre": "Soundtracks", "call_letters": "Z-K", "release_call_number": 12},
                "Soundtracks cd K-12",
                id="legacy-z-letter-soundtracks",
            ),
            pytest.param({"call_letters": "Z--"}, "Hiphop cd V/A-651", id="legacy-z-no-letter"),
            pytest.param(
                {
                    "artist": "Various Artists - Rock - M",
                    "genre": "Rock",
                    "call_letters": "Z--",
                    "release_call_number": 121,
                },
                "Rock cd V/A-121",
                id="legacy-z-no-letter-ignores-name",
            ),
            pytest.param(
                {"call_letters": "Z-M"}, "Hiphop cd V/A-651", id="legacy-z-letter-single-bin-genre"
            ),
            pytest.param(
                {"genre": "Rock", "call_letters": "Z-1", "release_call_number": 121},
                "Rock cd V/A 1-121",
                id="legacy-z-takes-any-char-like-substring",
            ),
            pytest.param(
                {"genre": "Rock", "call_letters": "z-m", "release_call_number": 121},
                "Rock cd z-m 0/121",
                id="lowercase-z-is-not-a-compilation-marker",
            ),
            pytest.param(
                {"artist": "Various Artists - Rock - M"},
                "Hiphop cd V/A-651",
                id="rock-heading-ignored-outside-rock",
            ),
            pytest.param(
                {"artist": "Various Artists - M"},
                "Hiphop cd V/A-651",
                id="name-suffix-ignored-on-single-bin-genre",
            ),
            pytest.param(
                {"artist": "Various Artists - Africa", "genre": "Rock", "release_call_number": 121},
                "Rock cd V/A-121",
                id="multi-letter-suffix-is-not-a-bin",
            ),
            pytest.param(
                {"artist": " - M", "genre": "Rock", "release_call_number": 121},
                "Rock cd V/A-121",
                id="name-trimmed-before-suffix-read",
            ),
            pytest.param({"release_call_number": None}, "Hiphop cd V/A", id="no-release-number"),
            pytest.param(
                {
                    "artist": "Various Artists - Rock - M",
                    "genre": "Rock",
                    "release_call_number": None,
                },
                "Rock cd V/A M",
                id="no-release-number-with-bin",
            ),
            pytest.param(
                {"artist": "Various Artists - Rock - M", "genre": None, "format": None},
                "V/A-651",
                id="no-genre-no-format",
            ),
            pytest.param(
                {
                    "artist": "Stereolab",
                    "genre": "Rock",
                    "format": "CD",
                    "call_letters": "ST",
                    "artist_call_number": 1,
                    "release_call_number": 2,
                },
                "Rock CD ST 1/2",
                id="unchanged-named-artist",
            ),
        ],
    )
    def test_compilation_call_number(self, overrides, expected):
        assert _compilation(**overrides).call_number == expected


class TestLibraryItemLibraryUrl:
    def test_url_format(self):
        item = LibraryItem(id=42, artist="Stereolab", title="Aluminum Tunes")
        assert item.library_url == "https://dj.wxyc.org/dashboard/album/legacy/42"

    def test_url_included_in_serialization(self):
        item = LibraryItem(id=99)
        data = item.model_dump()
        assert "library_url" in data
        assert data["library_url"] == "https://dj.wxyc.org/dashboard/album/legacy/99"


class TestToCatalogItem:
    def test_maps_all_fields(self):
        item = LibraryItem(
            id=1,
            artist="Stereolab",
            title="Aluminum Tunes",
            call_letters="S",
            artist_call_number=1,
            release_call_number=2,
            genre="Rock",
            format="CD",
        )
        catalog = item.to_catalog_item()
        assert isinstance(catalog, LibraryCatalogItem)
        assert catalog.id == 1
        assert catalog.artist == "Stereolab"
        assert catalog.title == "Aluminum Tunes"
        assert catalog.call_letters == "S"
        assert catalog.artist_call_number == 1
        assert catalog.release_call_number == 2
        assert catalog.genre == "Rock"
        assert catalog.format == "CD"

    def test_includes_computed_call_number(self):
        item = LibraryItem(
            id=1,
            genre="Rock",
            format="CD",
            call_letters="S",
            artist_call_number=1,
            release_call_number=2,
        )
        catalog = item.to_catalog_item()
        assert catalog.call_number == "Rock CD S 1/2"

    def test_includes_library_url(self):
        item = LibraryItem(id=42)
        catalog = item.to_catalog_item()
        assert catalog.library_url == "https://dj.wxyc.org/dashboard/album/legacy/42"

    def test_includes_compilation_call_number(self):
        """LML#1427: the wire call_number must carry the shelf form, not
        "V/A 0/<n>", for a compilation row."""
        item = _compilation(
            artist="Various Artists - Rock - M", genre="Rock", release_call_number=121
        )
        assert item.to_catalog_item().call_number == "Rock cd V/A M-121"

    def test_minimal_item(self):
        item = LibraryItem(id=5)
        catalog = item.to_catalog_item()
        assert catalog.id == 5
        assert catalog.call_number == ""
        assert catalog.library_url == "https://dj.wxyc.org/dashboard/album/legacy/5"

    def test_excludes_alternate_artist_name(self):
        item = LibraryItem(id=1, alternate_artist_name="Alt Name")
        catalog = item.to_catalog_item()
        data = catalog.model_dump()
        assert "alternate_artist_name" not in data


class TestLibraryItemLabel:
    def test_label_defaults_to_none(self):
        item = LibraryItem(id=1)
        assert item.label is None

    def test_label_round_trips(self):
        item = LibraryItem(id=1, label="Matador Records")
        assert item.label == "Matador Records"


class TestLibrarySearchResponse:
    def test_empty_results(self):
        resp = LibrarySearchResponse(results=[], total=0)
        assert resp.results == []
        assert resp.total == 0
        assert resp.query is None

    def test_with_results(self):
        item = LibraryItem(id=1, artist="Queen", title="The Game")
        resp = LibrarySearchResponse(results=[item], total=1, query="Queen")
        assert len(resp.results) == 1
        assert resp.query == "Queen"
