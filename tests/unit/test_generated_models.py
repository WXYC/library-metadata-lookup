"""Smoke tests verifying generated API contract models match expected shapes."""

import typing
from enum import StrEnum

import pytest
from pydantic import BaseModel, ValidationError

from generated.api_models import (
    AlbumMetadataResponse,
    DiscogsMatchResult,
    LibraryCatalogItem,
    LookupRequest,
    LookupResponse,
    LookupResultItem,
    MetadataStatus,
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
        assert req.raw_message is None
        assert req.artist is None


class TestLibraryCatalogItem:
    def test_call_number_is_plain_field(self):
        """call_number must be a regular field, not a computed property."""
        item = LibraryCatalogItem(
            id=1,
            artist="Stereolab",
            title="Aluminum Tunes",
            call_number="Rock CD S 1/1",
            library_url="https://dj.wxyc.org/dashboard/album/legacy/1",
        )
        data = item.model_dump()
        assert data["call_number"] == "Rock CD S 1/1"
        assert data["library_url"] == "https://dj.wxyc.org/dashboard/album/legacy/1"

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
            library_url="https://dj.wxyc.org/dashboard/album/legacy/1",
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


class TestMetadataStatus:
    def test_is_str_enum(self):
        assert issubclass(MetadataStatus, StrEnum)

    @pytest.mark.parametrize(
        "value",
        ["pending", "enriching", "enriched_match", "enriched_no_match", "failed_no_retry"],
    )
    def test_valid_values(self, value):
        assert MetadataStatus(value) == value


class TestAlbumMetadataResponse:
    """The `GET /proxy/metadata/album` contract ([WXYC/wxyc-shared#318]).

    Two tiers share one flat wire shape. *Base* fields are durable
    Backend-Service state read off the linked flowsheet row; *enriched* fields
    are resolved from Discogs via LML. The split is the guarantee: an LML
    timeout can blank every enriched field but never a base one.

    [WXYC/wxyc-shared#318]: https://github.com/WXYC/wxyc-shared/issues/318
    """

    def test_base_fields_are_optional_and_default_to_none(self):
        resp = AlbumMetadataResponse()
        assert resp.recordLabel is None
        assert resp.labelId is None
        assert resp.metadataStatus is None

    def test_base_fields_round_trip(self):
        resp = AlbumMetadataResponse(
            recordLabel="Sonamos", labelId=4321, metadataStatus="enriched_match"
        )
        assert resp.recordLabel == "Sonamos"
        assert resp.labelId == 4321
        assert resp.metadataStatus is MetadataStatus.enriched_match

    def test_record_label_is_distinct_from_the_discogs_release_label(self):
        """`recordLabel` is the catalog label; `label` is the Discogs release label.

        They carry different values with different provenance and must not be
        merged -- collapsing them would make the catalog label blankable by an
        upstream timeout, which is exactly what the base tier exists to prevent.
        """
        resp = AlbumMetadataResponse(recordLabel="Sonamos", label="Drag City")
        assert resp.recordLabel == "Sonamos"
        assert resp.label == "Drag City"

    def test_metadata_status_stays_coupled_to_the_flowsheet_enum(self):
        """Not a bare `str`: api.yaml `$ref`s the same schema the V2 flowsheet
        feed reports, so the two must move together. A regen that widened this
        field back to `str` would silently decouple them.
        """
        annotation = AlbumMetadataResponse.model_fields["metadataStatus"].annotation
        assert MetadataStatus in typing.get_args(annotation)

    def test_unknown_metadata_status_is_rejected(self):
        """Pins the enum's closed-set behavior in Python, which is *not* the
        org's usual "adding a response-enum value is non-breaking" rule.

        That rule holds for the generated TypeScript (a widened union stays
        assignable) and fails here: pydantic raises on an unrecognized member,
        so a purely additive upstream enum value breaks every pinned Python
        consumer until it regenerates. See `docs/scripts.md`.
        """
        with pytest.raises(ValidationError):
            AlbumMetadataResponse(metadataStatus="quiescent")

    def test_schema_description_is_emitted_as_a_class_docstring(self):
        """The field descriptions cross-reference "this schema's freshness
        caveat", which only exists in the schema-level description. Without
        `--use-schema-description` that pointer dangles: the caveat (base
        fields are memoized for 1h, so a librarian's label correction can be
        shadowed for that window) never reaches a Python consumer.
        """
        assert (AlbumMetadataResponse.__doc__ or "").strip(), (
            "AlbumMetadataResponse has no docstring -- schema-level descriptions "
            "are not reaching generated/api_models.py"
        )

    def test_undeclared_base_fields_are_dropped_on_decode(self):
        """Characterizes a known gap, tracked upstream at [WXYC/wxyc-shared#324].

        Three of the six base fields -- `artistName`, `releaseTitle`,
        `trackTitle` -- are on every response but declared nowhere in api.yaml,
        so pydantic's default `extra="ignore"` discards them. `artistName` is
        the one field guaranteed present when every upstream call fails, so a
        consumer that decodes through this model loses precisely the degraded
        case the base tier exists for.

        This asserts today's behavior deliberately. When #324 lands and LML
        regenerates, this test flips red -- which is the signal to drop it and
        assert the fields survive instead.

        [WXYC/wxyc-shared#324]: https://github.com/WXYC/wxyc-shared/issues/324
        """
        decoded = AlbumMetadataResponse.model_validate(
            {
                "artistName": "Juana Molina",
                "releaseTitle": "DOGA",
                "trackTitle": "la paradoja",
                "recordLabel": "Sonamos",
            }
        ).model_dump()
        assert decoded["recordLabel"] == "Sonamos"
        assert "artistName" not in decoded
        assert "releaseTitle" not in decoded
        assert "trackTitle" not in decoded


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
