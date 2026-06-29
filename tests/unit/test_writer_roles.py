"""Tests for the writer-role heuristic (LML#699, BMI composer credits).

``is_writer_role`` is tested as a pure function with no release context; the
VA/compilation guard lives only in ``writer_credits_from_release`` and is
exercised there. Role strings are drawn from a live discogs-cache enumeration
(2026-06-28) so the matrix reflects the real hyphen/space, comma-compound, and
``[bracket]`` variants. See ``plans/699-composer-credits-bmi.md``.
"""

import pytest

from discogs.models import ArtistCredit, ReleaseMetadataResponse, WriterCredits
from discogs.writer_roles import (
    extract_writer_names,
    is_writer_role,
    writer_credits_from_release,
)

WRITER_ROLES = [
    "Written-By",
    "Written By",
    "Composed By",
    "Music By",
    "Lyrics By",
    "Songwriter",
    "Words By",
    "Words By, Music By",
    "Music By, Lyrics By",
    "Written-By, Producer",  # compound: a single writer component is enough
    "Vocals, Lyrics By",
    "Written-By [Uncredited]",
    "Written-By [Sample]",
    "Composed By [Composition By]",
    "Songwriter [All Songs By]",
]

NON_WRITER_ROLES = [
    "Producer",
    "Arranged By",  # excluded by decision (plan)
    "Adapted By",  # excluded by decision (plan)
    "Performer",
    "Featuring",
    "Mixed By",
    "Engineer",
    "Drums",
    "Bass",
    "Guitar",
    "Vocals",
    "Recorded By, Mixed By",  # compound, no writer component
    "Drums, Percussion",
    "",  # empty role string (the 288k empty-role rows must not count)
]


@pytest.mark.parametrize("role", WRITER_ROLES)
def test_is_writer_role_includes(role: str) -> None:
    assert is_writer_role(role) is True


@pytest.mark.parametrize("role", NON_WRITER_ROLES)
def test_is_writer_role_excludes(role: str) -> None:
    assert is_writer_role(role) is False


def test_is_writer_role_handles_none() -> None:
    assert is_writer_role(None) is False


def test_extract_writer_names_dedup_and_order() -> None:
    # Dedup is by name (order-preserving): an artist credited under multiple
    # writer roles collapses to one entry; non-writer credits are dropped.
    credits = [
        ArtistCredit(name="Alice", role="Composed By"),
        ArtistCredit(name="Bob", role="Producer"),  # not a writer
        ArtistCredit(name="Alice", role="Written-By"),  # dup name, diff role
        ArtistCredit(name="Carol", role="Lyrics By"),
    ]
    assert extract_writer_names(credits) == ["Alice", "Carol"]


def _release(artist: str, extra_artists: list[ArtistCredit]) -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=1,
        title="Test Album",
        artist=artist,
        release_url="https://www.discogs.com/release/1",
        extra_artists=extra_artists,
    )


def test_writer_credits_from_release_single_artist_release_level() -> None:
    release = _release(
        "Juana Molina",
        [
            ArtistCredit(name="Juana Molina", role="Written-By"),
            ArtistCredit(name="Some Producer", role="Producer"),
        ],
    )
    wc = writer_credits_from_release(release)
    assert isinstance(wc, WriterCredits)
    assert wc.names == ["Juana Molina"]
    assert wc.provenance == "release"
    assert wc.track_position is None
    assert "Written-By" in (wc.roles or [])


def test_writer_credits_from_release_various_artists_returns_none() -> None:
    # The VA guard fires BEFORE extraction: a Written-By on a compilation is
    # never collected — distinct from the single-artist case returning it.
    release = _release(
        "Various",
        [ArtistCredit(name="Some Writer", role="Written-By")],
    )
    assert writer_credits_from_release(release) is None


def test_writer_credits_from_release_no_writer_returns_none() -> None:
    release = _release(
        "Stereolab",
        [ArtistCredit(name="An Engineer", role="Mixed By")],
    )
    assert writer_credits_from_release(release) is None
