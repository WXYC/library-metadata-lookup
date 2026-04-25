"""Smoke tests verifying generated API contract models match expected shapes."""

from enum import StrEnum

import pytest
from pydantic import BaseModel

from generated.api_models import (
    DiscogsMatchResult,
    LibraryCatalogItem,
    LookupRequest,
    LookupResponse,
    LookupResultItem,
    SearchType,
)


class TestLookupRequest:
    def test_fields(self):
        req = LookupRequest(
            artist="Stereolab",
            song="Percolator",
            album="Dots and Loops",
            raw_message="Stereolab - Percolator",
        )
        assert req.artist == "Stereolab"
        assert req.song == "Percolator"
        assert req.album == "Dots and Loops"
        assert req.raw_message == "Stereolab - Percolator"

    def test_optional_fields_default_to_none(self):
        req = LookupRequest(raw_message="test")
        assert req.artist is None
        assert req.song is None
        assert req.album is None

    def test_all_fields_optional(self):
        req = LookupRequest()
        assert req.artist is None
        assert req.song is None
        assert req.album is None
        assert req.raw_message is None


class TestLibraryCatalogItem:
    def test_call_number_is_plain_field(self):
        """call_number must be a regular field, not a computed property."""
        item = LibraryCatalogItem(
            id=1,
            artist="Stereolab",
            title="Aluminum Tunes",
            call_number="Rock CD S 1/1",
            library_url="http://www.wxyc.info/wxycdb/libraryRelease?id=1",
        )
        data = item.model_dump()
        assert data["call_number"] == "Rock CD S 1/1"
        assert data["library_url"] == "http://www.wxyc.info/wxycdb/libraryRelease?id=1"

    def test_required_fields(self):
        with pytest.raises(ValueError):
            LibraryCatalogItem(id=1)  # missing call_number and library_url


class TestDiscogsMatchResult:
    def test_fields(self):
        result = DiscogsMatchResult(
            album="Aluminum Tunes",
            artist="Stereolab",
            release_id=12345,
            release_url="https://discogs.com/release/12345",
            artwork_url="https://img.discogs.com/test.jpg",
            confidence=0.95,
        )
        assert result.release_id == 12345
        assert result.confidence == 0.95

    def test_required_fields(self):
        with pytest.raises(ValueError):
            DiscogsMatchResult()  # missing release_id and release_url


class TestLookupResultItem:
    def test_library_item_uses_catalog_item(self):
        catalog = LibraryCatalogItem(
            id=1,
            call_number="Rock CD S 1/1",
            library_url="http://www.wxyc.info/wxycdb/libraryRelease?id=1",
        )
        item = LookupResultItem(library_item=catalog)
        assert item.library_item.id == 1
        assert item.artwork is None


class TestSearchType:
    def test_is_str_enum(self):
        assert issubclass(SearchType, StrEnum)

    @pytest.mark.parametrize(
        "value",
        ["direct", "fallback", "alternative", "compilation", "song_as_artist", "none"],
    )
    def test_valid_values(self, value):
        assert SearchType(value) == value


class TestLookupResponse:
    def test_defaults(self):
        resp = LookupResponse()
        assert resp.results == []
        assert resp.search_type == SearchType.none
        assert resp.song_not_found is False
        assert resp.found_on_compilation is False
        assert resp.context_message is None
        assert resp.corrected_artist is None
        assert resp.cache_stats is None

    def test_search_type_accepts_string(self):
        """Pydantic v2 should coerce valid enum strings."""
        resp = LookupResponse(search_type="direct")
        assert resp.search_type == SearchType.direct

    def test_is_pydantic_model(self):
        assert issubclass(LookupResponse, BaseModel)
