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
    RELEASE_SOURCE_CONFIG,
    InvalidReleaseExternalIdError,
    ReleaseSourceConfig,
    coerce_external_id,
    validate_and_canonicalize_external_id,
)

# A canonical 22-char base62 Spotify album ID, reused across the
# spotify_album validator + registry-completeness cases below.
_VALID_SPOTIFY_ALBUM_ID = "1A2GTWGt0LBTGQAyA3OKAf"

# Sources minted internally (e.g. by the streaming-URL post-process) but
# deliberately NOT exposed on the public POST /api/v1/identity/resolve enum.
# Widening ReleaseIdentitySource is a wxyc-shared change tracked by #593.
_INTERNAL_ONLY_MINT_SOURCES = {"spotify_album"}


class TestSourceColumnDispatch:
    """``RELEASE_SOURCE_COLUMN`` is the source → column lookup the store uses."""

    def test_discogs_release_maps_to_discogs_release_id(self):
        assert RELEASE_SOURCE_COLUMN["discogs_release"] == "discogs_release_id"

    def test_discogs_master_maps_to_discogs_master_id(self):
        assert RELEASE_SOURCE_COLUMN["discogs_master"] == "discogs_master_id"

    def test_bandcamp_maps_to_bandcamp_album_url(self):
        assert RELEASE_SOURCE_COLUMN["bandcamp"] == "bandcamp_album_url"

    def test_apple_music_album_maps_to_apple_music_album_id(self):
        assert RELEASE_SOURCE_COLUMN["apple_music_album"] == "apple_music_album_id"

    def test_spotify_album_maps_to_spotify_album_id(self):
        # spotify_album joined RELEASE_SOURCE_CONFIG in #573 (internally
        # minted by the streaming-URL post-process).
        assert RELEASE_SOURCE_COLUMN["spotify_album"] == "spotify_album_id"

    def test_unknown_source_is_absent(self):
        # musicbrainz_release is read-only today (no write helper / sentinel
        # rule) so it never enters RELEASE_SOURCE_CONFIG, hence absent here.
        assert "musicbrainz_release" not in RELEASE_SOURCE_COLUMN


class TestReleaseSourceConfigDerivedView:
    """``RELEASE_SOURCE_COLUMN`` is a derived view of ``RELEASE_SOURCE_CONFIG``.

    #573 introduced the dataclass-keyed registry; ``RELEASE_SOURCE_COLUMN``
    stays as a backward-compat view so the store's read path and all 12
    existing call sites keep working unchanged.
    """

    def test_release_source_column_is_derived_view(self):
        assert RELEASE_SOURCE_COLUMN == {
            k: v.identity_column for k, v in RELEASE_SOURCE_CONFIG.items()
        }

    def test_config_entries_are_release_source_config_instances(self):
        for source, cfg in RELEASE_SOURCE_CONFIG.items():
            assert isinstance(cfg, ReleaseSourceConfig), source

    def test_config_carries_five_entries(self):
        # discogs ×2, bandcamp, apple_music_album, spotify_album. The cache
        # registry (STREAMING_URL_CACHE_CONFIG) is a deliberately smaller
        # subset — see lookup/streaming_url_postprocess.py.
        assert set(RELEASE_SOURCE_CONFIG) == {
            "discogs_release",
            "discogs_master",
            "bandcamp",
            "apple_music_album",
            "spotify_album",
        }


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

    def test_zero_sentinel_error_message_is_exact(self):
        # The Discogs and Apple Music validators share one factory
        # (identity/release_validation.py) — pin the exact wording so a
        # collapse of the two near-identical functions can't drift it. These
        # strings surface verbatim in the 422 response detail.
        with pytest.raises(InvalidReleaseExternalIdError) as exc_info:
            validate_and_canonicalize_external_id("discogs_release", "0")
        assert str(exc_info.value) == (
            "discogs_release external_id must be > 0; 0 is the Discogs unknown-release sentinel."
        )

    def test_malformed_shape_error_message_is_exact(self):
        with pytest.raises(InvalidReleaseExternalIdError) as exc_info:
            validate_and_canonicalize_external_id("discogs_release", "12_000")
        assert str(exc_info.value) == (
            "discogs_release external_id must be a positive decimal integer "
            "(digits only, no leading sign / whitespace / underscores / "
            "leading zeros), got '12_000'"
        )


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

    def test_zero_sentinel_error_message_is_exact(self):
        # Same shared factory as discogs_release, but the {source} token must
        # still resolve per-call — "master", not "release".
        with pytest.raises(InvalidReleaseExternalIdError) as exc_info:
            validate_and_canonicalize_external_id("discogs_master", "0")
        assert str(exc_info.value) == (
            "discogs_master external_id must be > 0; 0 is the Discogs unknown-release sentinel."
        )


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


class TestValidateAppleMusicAlbum:
    """Apple Music album IDs are numeric strings; reject empty / non-numeric / zero.

    Apple's URL parser (release/apple_music_url_parser.py) admits IDs of 6 or
    more digits. The validator mirrors that floor: positive decimal integer,
    no leading sign / whitespace / underscores / leading zeros, no zero
    sentinel. Same posture as the Discogs sentinel rule — see the docstring
    on ``_validate_discogs_positive_int`` for the rationale.
    """

    @pytest.mark.parametrize("external_id", ["123456", "1234567890", "999999999999"])
    def test_accepts_positive_integer_strings(self, external_id):
        assert (
            validate_and_canonicalize_external_id("apple_music_album", external_id) == external_id
        )

    def test_rejects_zero(self):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("apple_music_album", "0")

    @pytest.mark.parametrize("external_id", ["-1", "-12345"])
    def test_rejects_negative(self, external_id):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("apple_music_album", external_id)

    @pytest.mark.parametrize(
        "external_id",
        [
            "abc",
            "12.5",
            "1e2",
            "",
            " ",
            "1 2",
            " 12",  # leading whitespace
            "12 ",  # trailing whitespace
            "+12",  # leading sign
            "12_000",  # PEP 515 underscore
            "012",  # leading zero
            "0012345",  # leading zeros
        ],
    )
    def test_rejects_non_integer(self, external_id):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("apple_music_album", external_id)

    def test_zero_error_message_is_exact(self):
        # Apple's "0 is not a valid..." explanation differs from Discogs's
        # "0 is the Discogs unknown-release sentinel." wording even though
        # both validators now share one factory — pin the exact string so a
        # collapse can't quietly blend the two source-specific explanations.
        # These strings surface verbatim in the 422 response detail.
        with pytest.raises(InvalidReleaseExternalIdError) as exc_info:
            validate_and_canonicalize_external_id("apple_music_album", "0")
        assert str(exc_info.value) == (
            "apple_music_album external_id must be > 0; 0 is not a valid Apple Music album ID."
        )

    def test_malformed_shape_error_message_is_exact(self):
        with pytest.raises(InvalidReleaseExternalIdError) as exc_info:
            validate_and_canonicalize_external_id("apple_music_album", "12_000")
        assert str(exc_info.value) == (
            "apple_music_album external_id must be a positive decimal integer "
            "(digits only, no leading sign / whitespace / underscores / "
            "leading zeros), got '12_000'"
        )


class TestValidateSpotifyAlbum:
    """Spotify album IDs are 22-char base62 strings.

    Spotify's open-graph album URL carries a 22-character base62 ID
    (``open.spotify.com/album/<id>``). The validator pins that exact shape:
    reject anything that is not exactly 22 ``[0-9A-Za-z]`` characters. Unlike
    the Discogs / Apple rules there is no zero-sentinel concept — base62 IDs
    are opaque, not ordinal.
    """

    def test_accepts_canonical_id(self):
        assert (
            validate_and_canonicalize_external_id("spotify_album", _VALID_SPOTIFY_ALBUM_ID)
            == _VALID_SPOTIFY_ALBUM_ID
        )

    @pytest.mark.parametrize(
        "external_id",
        [
            "1A2GTWGt0LBTGQAyA3OKA",  # 21 chars — too short
            "1A2GTWGt0LBTGQAyA3OKAfX",  # 23 chars — too long
            "1A2GTWGt0LBTGQAyA3OK-f",  # 22 chars with a hyphen (not base62)
            "1A2GTWGt0LBTGQAyA3OK_f",  # 22 chars with an underscore
            "1A2GTWGt0LBTGQAyA3OK f",  # 22 chars with a space
            "",
            " ",
        ],
    )
    def test_rejects_wrong_shape(self, external_id):
        with pytest.raises(InvalidReleaseExternalIdError):
            validate_and_canonicalize_external_id("spotify_album", external_id)


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

    def test_release_source_config_keys_match_pydantic_enum(self):
        # #573: spotify_album is minted internally by the streaming-URL
        # post-process but deliberately NOT exposed on the public
        # ReleaseIdentitySource enum (that widening is a wxyc-shared change
        # tracked by #593). Subtract the documented exception set so the
        # guard stays loud about any *other* divergence while recording the
        # one intentional gap.
        from generated.api_models import ReleaseIdentitySource

        enum_values = {member.value for member in ReleaseIdentitySource}
        public_config_keys = set(RELEASE_SOURCE_CONFIG.keys()) - _INTERNAL_ONLY_MINT_SOURCES
        assert public_config_keys == enum_values, (
            f"RELEASE_SOURCE_CONFIG keys (minus internal-only mint sources "
            f"{_INTERNAL_ONLY_MINT_SOURCES!r}) and ReleaseIdentitySource enum diverged. "
            f"In config but not enum: {public_config_keys - enum_values!r}; "
            f"in enum but not config: {enum_values - public_config_keys!r}. "
            f"Either add the source to wxyc-shared/api.yaml's "
            f"ReleaseIdentitySource enum and regenerate generated/api_models.py, "
            f"or add the dispatch entry to RELEASE_SOURCE_CONFIG + a sentinel "
            f"rule in identity/release_validation.py + a DDL column in "
            f"entity/release_identity.sql. If the source is intentionally "
            f"internal-only, add it to _INTERNAL_ONLY_MINT_SOURCES with a note."
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
            "apple_music_album": "1234567890",
            "spotify_album": _VALID_SPOTIFY_ALBUM_ID,
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
        # docstring names. Two failure modes are covered:
        #
        # 1. A new source key was added to RELEASE_SOURCE_COLUMN but no
        #    canonical input was added here — the assert fires with the
        #    message below, telling the author exactly what's missing.
        # 2. The author did update canonical_inputs, but coerce_external_id
        #    has no matching if-branch — the explicit RuntimeError at the
        #    bottom of coerce_external_id fires below, telling the author
        #    to wire the branch.
        #
        # Either way the drift is caught at CI time, not at first-request
        # time in prod.
        canonical_inputs = {
            "discogs_release": "12345",
            "discogs_master": "789",
            "bandcamp": "https://autechre.bandcamp.com/album/confield",
            "apple_music_album": "1234567890",
            "spotify_album": _VALID_SPOTIFY_ALBUM_ID,
        }
        for source in RELEASE_SOURCE_COLUMN:
            assert source in canonical_inputs, (
                f"New source {source!r} added to RELEASE_SOURCE_COLUMN — "
                f"add a canonical-input case to this test so the coerce "
                f"branch gets exercised."
            )
            # Will hit the "no branch for source=..." RuntimeError if
            # coerce_external_id has no matching if-branch for this source.
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

    def test_apple_music_album_stays_string(self):
        # apple_music_album_id is a TEXT column (Apple's numeric IDs can grow
        # past INT32 — the column is TEXT so the bind value also stays str).
        assert coerce_external_id("apple_music_album", "1234567890") == "1234567890"

    def test_spotify_album_stays_string(self):
        # spotify_album_id is a TEXT column; the base62 ID binds verbatim.
        assert (
            coerce_external_id("spotify_album", _VALID_SPOTIFY_ALBUM_ID) == _VALID_SPOTIFY_ALBUM_ID
        )

    def test_unknown_source_raises(self):
        # Defense-in-depth — should never happen post-validation.
        with pytest.raises(KeyError):
            coerce_external_id("nonsense", "x")
