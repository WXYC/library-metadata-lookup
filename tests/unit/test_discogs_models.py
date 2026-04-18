"""Unit tests for discogs/models.py mapping methods."""

from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    DiscogsSearchResult,
    LabelCredit,
    MemberRef,
    ReleaseMetadataResponse,
    ReleaseVideo,
)
from generated.api_models import DiscogsMatchResult


class TestToMatchResult:
    def test_maps_all_fields(self):
        result = DiscogsSearchResult(
            album="Aluminum Tunes",
            artist="Stereolab",
            release_id=12345,
            release_url="https://discogs.com/release/12345",
            artwork_url="https://img.discogs.com/test.jpg",
            confidence=0.95,
        )
        match = result.to_match_result()
        assert isinstance(match, DiscogsMatchResult)
        assert match.album == "Aluminum Tunes"
        assert match.artist == "Stereolab"
        assert match.release_id == 12345
        assert match.release_url == "https://discogs.com/release/12345"
        assert match.artwork_url == "https://img.discogs.com/test.jpg"
        assert match.confidence == 0.95

    def test_minimal_result(self):
        result = DiscogsSearchResult(
            release_id=1,
            release_url="https://discogs.com/release/1",
        )
        match = result.to_match_result()
        assert match.release_id == 1
        assert match.album is None
        assert match.artist is None
        assert match.artwork_url is None
        assert match.confidence == 0.0


# ---------------------------------------------------------------------------
# ArtistCredit / LabelCredit models
# ---------------------------------------------------------------------------


class TestArtistCredit:
    def test_basic_construction(self):
        credit = ArtistCredit(artist_id=77, name="Autechre")
        assert credit.artist_id == 77
        assert credit.name == "Autechre"
        assert credit.join == ""
        assert credit.role is None

    def test_with_join_and_role(self):
        credit = ArtistCredit(artist_id=1, name="Rob Brown", join=" & ", role="Producer")
        assert credit.join == " & "
        assert credit.role == "Producer"

    def test_nullable_artist_id(self):
        credit = ArtistCredit(name="Unknown Artist")
        assert credit.artist_id is None


class TestLabelCredit:
    def test_basic_construction(self):
        credit = LabelCredit(label_id=233, name="Warp Records", catno="WAP 159 CD")
        assert credit.label_id == 233
        assert credit.name == "Warp Records"
        assert credit.catno == "WAP 159 CD"

    def test_defaults(self):
        credit = LabelCredit(name="Self-Released")
        assert credit.label_id is None
        assert credit.catno is None


# ---------------------------------------------------------------------------
# ReleaseMetadataResponse enriched fields
# ---------------------------------------------------------------------------


class TestReleaseMetadataResponseEnriched:
    def test_enriched_fields_default_to_empty(self):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Confield",
            artist="Autechre",
            release_url="https://www.discogs.com/release/1",
        )
        assert release.artists == []
        assert release.extra_artists == []
        assert release.labels == []
        assert release.released is None

    def test_enriched_fields_populated(self):
        release = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            release_url="https://www.discogs.com/release/28138",
            artists=[
                ArtistCredit(artist_id=77, name="Autechre"),
            ],
            extra_artists=[
                ArtistCredit(artist_id=100, name="Rob Brown", role="Producer"),
                ArtistCredit(artist_id=101, name="Sean Booth", role="Producer"),
            ],
            labels=[
                LabelCredit(label_id=233, name="Warp Records", catno="WAP 159 CD"),
            ],
            released="2001-04-30",
        )
        assert len(release.artists) == 1
        assert release.artists[0].name == "Autechre"
        assert len(release.extra_artists) == 2
        assert release.extra_artists[0].role == "Producer"
        assert len(release.labels) == 1
        assert release.labels[0].catno == "WAP 159 CD"
        assert release.released == "2001-04-30"

    def test_backward_compat_scalar_fields_still_work(self):
        """Existing scalar fields (artist, label, artist_id, label_id) still work."""
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Confield",
            artist="Autechre",
            artist_id=77,
            label="Warp Records",
            label_id=233,
            release_url="https://www.discogs.com/release/1",
            artists=[ArtistCredit(artist_id=77, name="Autechre")],
            labels=[LabelCredit(label_id=233, name="Warp Records", catno="WAP 159 CD")],
        )
        assert release.artist == "Autechre"
        assert release.artist_id == 77
        assert release.label == "Warp Records"
        assert release.label_id == 233


# ---------------------------------------------------------------------------
# ArtistDetails model
# ---------------------------------------------------------------------------


class TestArtistDetails:
    def test_basic_construction(self):
        details = ArtistDetails(
            artist_id=77,
            name="Autechre",
            profile="Electronic duo from Rochdale, England.",
            image_url="https://i.discogs.com/autechre.jpg",
            name_variations=["Ae", "Autechre."],
            aliases=[ArtistRef(id=500, name="Gescom")],
            members=[
                MemberRef(id=200, name="Rob Brown", active=True),
                MemberRef(id=201, name="Sean Booth", active=True),
            ],
            urls=["https://autechre.ws"],
        )
        assert details.artist_id == 77
        assert details.name == "Autechre"
        assert details.profile == "Electronic duo from Rochdale, England."
        assert details.image_url == "https://i.discogs.com/autechre.jpg"
        assert len(details.name_variations) == 2
        assert len(details.aliases) == 1
        assert details.aliases[0].name == "Gescom"
        assert len(details.members) == 2
        assert details.members[0].name == "Rob Brown"
        assert len(details.urls) == 1

    def test_defaults(self):
        details = ArtistDetails(artist_id=1, name="Unknown")
        assert details.profile is None
        assert details.image_url is None
        assert details.name_variations == []
        assert details.aliases == []
        assert details.members == []
        assert details.urls == []
        assert details.cached is False

    def test_artist_ref(self):
        ref = ArtistRef(id=500, name="Gescom")
        assert ref.id == 500
        assert ref.name == "Gescom"

    def test_member_ref_defaults(self):
        ref = MemberRef(id=200, name="Rob Brown")
        assert ref.active is True


# ---------------------------------------------------------------------------
# ReleaseVideo model
# ---------------------------------------------------------------------------


class TestReleaseVideo:
    def test_required_field(self):
        video = ReleaseVideo(src="https://www.youtube.com/watch?v=abc")
        assert video.src == "https://www.youtube.com/watch?v=abc"
        assert video.title is None
        assert video.duration is None
        assert video.embed is True

    def test_all_fields(self):
        video = ReleaseVideo(
            src="https://www.youtube.com/watch?v=abc",
            title="Stereolab - French Disko",
            duration=204,
            embed=False,
        )
        assert video.title == "Stereolab - French Disko"
        assert video.duration == 204
        assert video.embed is False

    def test_embed_defaults_true(self):
        video = ReleaseVideo(src="https://www.youtube.com/watch?v=abc")
        assert video.embed is True


class TestReleaseMetadataResponseVideos:
    def test_videos_default_to_empty(self):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Emperor Tomato Ketchup",
            artist="Stereolab",
            release_url="https://www.discogs.com/release/1",
        )
        assert release.videos == []

    def test_videos_populated(self):
        release = ReleaseMetadataResponse(
            release_id=1,
            title="Emperor Tomato Ketchup",
            artist="Stereolab",
            release_url="https://www.discogs.com/release/1",
            videos=[
                ReleaseVideo(
                    src="https://www.youtube.com/watch?v=abc",
                    title="Metronomic Underground",
                    duration=456,
                ),
                ReleaseVideo(
                    src="https://www.youtube.com/watch?v=def", title="French Disko", duration=204
                ),
            ],
        )
        assert len(release.videos) == 2
        assert release.videos[0].title == "Metronomic Underground"
        assert release.videos[1].duration == 204
