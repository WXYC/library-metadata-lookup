"""Unit tests for library/models.py."""

import pytest

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


class TestLibraryItemLibraryUrl:
    def test_url_format(self):
        item = LibraryItem(id=42, artist="Queen", title="The Game")
        assert item.library_url == "http://www.wxyc.info/wxycdb/libraryRelease?id=42"

    def test_url_included_in_serialization(self):
        item = LibraryItem(id=99)
        data = item.model_dump()
        assert "library_url" in data
        assert data["library_url"] == "http://www.wxyc.info/wxycdb/libraryRelease?id=99"


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
