"""Unit tests for library/models.py."""

import hashlib
import json
from pathlib import Path

import pytest

from generated.api_models import LibraryCatalogItem
from library.models import LibraryItem, LibrarySearchResponse

_CORPUS_PATH = Path(__file__).parent.parent / "fixtures" / "call-number-cases.json"
_CORPUS = json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))
_CASES = {case["id"]: case for case in _CORPUS["cases"]}

# Corpus rows this repo does not yet render as the corpus expects, id -> why.
# Each listed row must still FAIL (the self-check below), so an entry cannot go
# stale once the row starts passing.
KNOWN_DIVERGENCES: dict[str, str] = dict.fromkeys(
    (
        "volume-letter-named-artist",
        "volume-letter-rock-compilation",
        "volume-letter-single-bin-compilation",
        "volume-letter-soundtracks-compilation",
        "volume-letter-lowercase",
        "volume-letter-padded",
        "volume-letter-letters-without-artist-number",
        "volume-letter-release-only",
    ),
    "LML#1373: volume letters are not rendered yet",
)


def _item_from_case(case: dict) -> LibraryItem:
    """Map a corpus row onto the library.db-shaped LibraryItem fields."""
    return LibraryItem(
        id=1,
        artist=case["artist_name"],
        genre=case["genre"],
        format=case["format"],
        call_letters=case["call_letters"],
        artist_call_number=case["artist_number"],
        release_call_number=case["release_number"],
        artist_comp_letter=case["comp_letter"],
    )


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


class TestCallNumberCorpus:
    """Drives LibraryItem.call_number from the shared call-number corpus.

    The corpus (wxyc-shared `src/test-utils/call-number-cases.json`, vendored at
    commit 729990225c3f35de6d3362fd52190ca6e1eba0d0 and SHA-256 pinned) is the
    single hand-sync point with Backend-Service's `computeCallNumber` and
    dj-site's `libraryCode.ts`: add a case there, not here."""

    def test_vendored_corpus_matches_pinned_hash(self):
        pinned = _CORPUS_PATH.with_name(_CORPUS_PATH.name + ".sha256").read_text().split()[0]
        assert hashlib.sha256(_CORPUS_PATH.read_bytes()).hexdigest() == pinned

    @pytest.mark.parametrize(
        "case_id", [i for i in _CASES if i not in KNOWN_DIVERGENCES], ids=lambda i: i
    )
    def test_call_number(self, case_id):
        case = _CASES[case_id]
        assert _item_from_case(case).call_number == case["full"]

    @pytest.mark.parametrize("case_id", sorted(KNOWN_DIVERGENCES), ids=lambda i: i)
    def test_known_divergence_still_diverges(self, case_id):
        case = _CASES[case_id]
        assert _item_from_case(case).call_number != case["full"]

    def test_known_divergences_list_only_rows_that_exist(self):
        assert set(KNOWN_DIVERGENCES) <= set(_CASES)


class TestLibraryItemCompilationLetterSource:
    """LML#1431: the Rock/Soundtracks bin letter comes from library.db's
    structural `artist_comp_letter`, never from the artist name."""

    def test_renamed_artist_keeps_its_letter(self):
        item = LibraryItem(
            id=1,
            artist="Various Artists",
            genre="Rock",
            format="cd",
            call_letters="V/A",
            artist_call_number=0,
            release_call_number=121,
            artist_comp_letter="M",
        )
        assert item.call_number == "Rock cd V/A M-121"

    def test_name_suffix_without_column_value_renders_no_letter(self):
        item = LibraryItem(
            id=1,
            artist="Various Artists - Rock - M",
            genre="Rock",
            format="cd",
            call_letters="V/A",
            artist_call_number=0,
            release_call_number=121,
            artist_comp_letter=None,
        )
        assert item.call_number == "Rock cd V/A-121"

    def test_letter_is_not_serialized(self):
        item = LibraryItem(id=1, artist_comp_letter="M")
        assert "artist_comp_letter" not in item.model_dump()


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
        item = _compilation(genre="Rock", release_call_number=121, artist_comp_letter="M")
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
