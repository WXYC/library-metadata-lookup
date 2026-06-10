"""Tests for ``identity.release_validation``.

Pure unit tests — no DB, no HTTP. Cover the sentinel rules from LML#526:

- Discogs release / master: numeric ``external_id`` must parse and be ``> 0``.
- Bandcamp: ``external_id`` must parse as a Bandcamp *album* URL via
  ``release.url_parser.parse_url`` and is canonicalised on the way through.

These rules run before any DB write — a rejected input never reaches
``entity.release_identity``, so no poisoned rows can be minted.
"""

from __future__ import annotations

import pytest

from identity.release_validation import (
    RELEASE_SOURCE_COLUMN,
    InvalidReleaseExternalIdError,
    coerce_external_id,
    validate_and_canonicalize_external_id,
)


class TestSourceColumnDispatch:
    """``RELEASE_SOURCE_COLUMN`` is the source → column lookup the store uses."""

    def test_discogs_release_maps_to_discogs_release_id(self):
        assert RELEASE_SOURCE_COLUMN["discogs_release"] == "discogs_release_id"

    def test_discogs_master_maps_to_discogs_master_id(self):
        assert RELEASE_SOURCE_COLUMN["discogs_master"] == "discogs_master_id"

    def test_bandcamp_maps_to_bandcamp_album_url(self):
        assert RELEASE_SOURCE_COLUMN["bandcamp"] == "bandcamp_album_url"

    def test_unknown_source_is_absent(self):
        assert "musicbrainz_release" not in RELEASE_SOURCE_COLUMN
        assert "spotify_album" not in RELEASE_SOURCE_COLUMN


class TestValidateDiscogsRelease:
    """Discogs release IDs: positive integer-shaped strings only."""

    @pytest.mark.parametrize("external_id", ["1", "12345", "999999999"])
    def test_accepts_positive_integer_strings(self, external_id):
        assert validate_and_canonicalize_external_id("discogs_release", external_id) == external_id

    def test_rejects_zero_sentinel(self):
        # Discogs uses 0 to mark the unknown-release sentinel.
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("discogs_release", "0")

    @pytest.mark.parametrize("external_id", ["-1", "-12345"])
    def test_rejects_negative(self, external_id):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("discogs_release", external_id)

    @pytest.mark.parametrize("external_id", ["abc", "12.5", "1e2", "", " ", "1 2"])
    def test_rejects_non_integer(self, external_id):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("discogs_release", external_id)


class TestValidateDiscogsMaster:
    """Discogs master IDs follow the same rule as releases."""

    def test_accepts_positive(self):
        assert validate_and_canonicalize_external_id("discogs_master", "789") == "789"

    def test_rejects_zero(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("discogs_master", "0")

    def test_rejects_negative(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("discogs_master", "-5")


class TestValidateBandcamp:
    """Bandcamp external_id must round-trip through ``parse_url`` as a bandcamp album."""

    def test_accepts_album_url(self):
        url = "https://autechre.bandcamp.com/album/confield"
        assert validate_and_canonicalize_external_id("bandcamp", url) == url

    def test_canonicalises_trailing_slash(self):
        # parse_url strips trailing slashes — caller's "same URL" stays one row.
        result = validate_and_canonicalize_external_id(
            "bandcamp", "https://autechre.bandcamp.com/album/confield/"
        )
        assert result == "https://autechre.bandcamp.com/album/confield"

    def test_rejects_track_url(self):
        # Only album URLs are valid release identities.
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id(
                "bandcamp", "https://autechre.bandcamp.com/track/foo"
            )

    def test_rejects_bare_subdomain(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("bandcamp", "https://autechre.bandcamp.com/")

    def test_rejects_non_bandcamp_url(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("bandcamp", "https://example.com/album/whatever")

    def test_rejects_garbage(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("bandcamp", "not a url")

    def test_rejects_empty(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("bandcamp", "")


class TestValidateUnknownSource:
    """An unrecognised source is a programmer error — pydantic blocks it upstream,
    but the validator must still refuse rather than silently mint a bad row."""

    def test_rejects_unknown_source(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("musicbrainz_release", "abc-123")


class TestCoerceExternalId:
    """The store binds ints to INTEGER columns and strings to TEXT columns."""

    def test_discogs_release_coerces_to_int(self):
        assert coerce_external_id("discogs_release", "12345") == 12345

    def test_discogs_master_coerces_to_int(self):
        assert coerce_external_id("discogs_master", "789") == 789

    def test_bandcamp_stays_string(self):
        url = "https://autechre.bandcamp.com/album/confield"
        assert coerce_external_id("bandcamp", url) == url

    def test_unknown_source_raises(self):
        # Defense-in-depth — should never happen post-validation.
        with pytest.raises(KeyError):
            coerce_external_id("nonsense", "x")
