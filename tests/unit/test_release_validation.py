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

    @pytest.mark.parametrize(
        "external_id",
        [
            "abc",
            "12.5",
            "1e2",
            "",
            " ",
            "1 2",
            # Cases bare int() would silently accept — see _DISCOGS_POSITIVE_INT_RE
            # comment in identity/release_validation.py for the why.
            " 12",  # leading whitespace
            "12 ",  # trailing whitespace
            "+12",  # leading sign
            "12_000",  # PEP 515 underscore — int() accepts, would coerce to 12000
            "012",  # leading zero
            "0012345",  # leading zeros
        ],
    )
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


class TestRegistryDriftInvariant:
    """The set of release sources lives in four places that must stay in sync:

    - ``RELEASE_SOURCE_COLUMN`` (this module).
    - ``coerce_external_id`` if-chain (this module).
    - ``ReleaseIdentitySource`` enum in ``generated/api_models.py`` (and the
      ``wxyc-shared/api.yaml`` source it is generated from).
    - DDL columns in ``entity/release_identity.sql``.

    If a future PR adds a source to the enum / dict / DDL but forgets one of
    the other places, the drift would only surface when a real request with
    that source arrived. These tests make the drift fail at CI time.
    """

    def test_release_source_column_keys_match_pydantic_enum(self):
        from generated.api_models import ReleaseIdentitySource

        enum_values = {member.value for member in ReleaseIdentitySource}
        assert enum_values == set(RELEASE_SOURCE_COLUMN.keys()), (
            f"ReleaseIdentitySource enum ({enum_values}) and "
            f"RELEASE_SOURCE_COLUMN dict ({set(RELEASE_SOURCE_COLUMN.keys())}) "
            f"disagree. Adding a release source means updating both — and the "
            f"DDL in entity/release_identity.sql and a sentinel rule in "
            f"identity/release_validation.py."
        )

    def test_release_source_columns_match_ddl(self):
        # The DDL in entity/release_identity.sql is the canonical column
        # set. Cross-check that every column RELEASE_SOURCE_COLUMN points at
        # actually appears in the DDL — a typo or rename in either side
        # would otherwise silently produce a ``column does not exist`` PG
        # error only on first mint of that source.
        from pathlib import Path

        ddl_path = Path(__file__).resolve().parent.parent.parent / "entity" / "release_identity.sql"
        ddl = ddl_path.read_text()
        for source, column in RELEASE_SOURCE_COLUMN.items():
            assert column in ddl, (
                f"RELEASE_SOURCE_COLUMN[{source!r}] = {column!r} but that "
                f"column does not appear in entity/release_identity.sql"
            )

    def test_every_dict_entry_has_a_validator_branch(self):
        # Exercise validate_and_canonicalize_external_id once per source
        # with a clearly-shaped good input — if a new source is added to
        # RELEASE_SOURCE_COLUMN without a sentinel rule, the defensive
        # raise at the end of the validator fires here and tells the
        # author exactly what to add next.
        good_inputs = {
            "discogs_release": "12345",
            "discogs_master": "789",
            "bandcamp": "https://autechre.bandcamp.com/album/confield",
        }
        for source in RELEASE_SOURCE_COLUMN:
            assert source in good_inputs, (
                f"New source {source!r} added to RELEASE_SOURCE_COLUMN — "
                f"add a good-input case to this test so the validator "
                f"branch gets exercised."
            )
            # Should not raise.
            validate_and_canonicalize_external_id(source, good_inputs[source])

    def test_every_dict_entry_has_a_coerce_branch(self):
        # Sibling of the validator-branch check above, but for
        # coerce_external_id — the fourth of the "four places" the class
        # docstring names. If a new source is added to RELEASE_SOURCE_COLUMN
        # without a coerce if-branch, the explicit RuntimeError at the end
        # of coerce_external_id fires here at CI time rather than at
        # first-real-request time.
        canonical_inputs = {
            "discogs_release": "12345",
            "discogs_master": "789",
            "bandcamp": "https://autechre.bandcamp.com/album/confield",
        }
        for source in RELEASE_SOURCE_COLUMN:
            assert source in canonical_inputs, (
                f"New source {source!r} added to RELEASE_SOURCE_COLUMN — "
                f"add a canonical-input case to this test so the coerce "
                f"branch gets exercised."
            )
            # Should not raise; specifically, must not hit the
            # "no branch for source=..." RuntimeError at the bottom of
            # coerce_external_id.
            coerce_external_id(source, canonical_inputs[source])


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
