"""Tests for the writer-role heuristic (LML#699, BMI composer credits).

``is_writer_role`` is tested as a pure function with no release context; the
VA/compilation guard lives only in ``writer_credits_from_release`` and is
exercised there. Role strings are drawn from a live discogs-cache enumeration
(2026-06-28) so the matrix reflects the real hyphen/space, comma-compound, and
``[bracket]`` variants. See ``docs/plans/699-composer-credits-bmi.md``.
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
    # Bracket-internal comma: the qualifier is stripped BEFORE the comma split,
    # so the role is not fragmented into "Written-By [Words" + " Music]". Locks
    # the strip-then-split ordering the module docstring declares load-bearing.
    "Written-By [Words, Music]",
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


def test_writer_credits_roles_exclude_empty_name_credit() -> None:
    # A writer-role credit with an empty name is dropped from BOTH names and
    # roles, so the audit-trail ``roles`` never references a writer with no
    # name (no phantom unattributed role in the BMI payload). ``roles`` must
    # stay in sync with the ``names`` they were drawn from.
    release = _release(
        "Sessa",
        [
            ArtistCredit(name="Sessa", role="Music By"),
            ArtistCredit(name="", role="Lyrics By"),  # empty name → dropped from both
        ],
    )
    wc = writer_credits_from_release(release)
    assert wc is not None
    assert wc.names == ["Sessa"]
    assert wc.roles == ["Music By"]


def test_writer_credits_names_dedup_by_name_roles_dedup_by_role() -> None:
    # The two lists use different dedup keys by design: ``names`` dedups by
    # name (one artist under multiple writer roles collapses to one name),
    # ``roles`` dedups by verbatim role (one role shared by multiple writers
    # collapses to one role).
    release = _release(
        "Stereolab",
        [
            ArtistCredit(name="Tim Gane", role="Composed By"),
            ArtistCredit(name="Tim Gane", role="Written-By"),  # dup name, new role
            ArtistCredit(name="Laetitia Sadier", role="Written-By"),  # new name, dup role
        ],
    )
    wc = writer_credits_from_release(release)
    assert wc is not None
    assert wc.names == ["Tim Gane", "Laetitia Sadier"]
    assert wc.roles == ["Composed By", "Written-By"]


# ---------------------------------------------------------------------------
# Phase 2 (LML#699 PR-C): track-level precision. A resolved track position
# prefers that track's per-track writers (provenance="track"); otherwise the
# release-level Phase-1 path applies (provenance="release"). The track-level
# path bypasses the VA/compilation guard — a per-track writer is correct on a
# comp precisely because it is track-scoped.
# ---------------------------------------------------------------------------


def _release_with_track_writers(
    artist: str,
    track_writers: dict[str, list[ArtistCredit]],
    extra_artists: list[ArtistCredit] | None = None,
) -> ReleaseMetadataResponse:
    return ReleaseMetadataResponse(
        release_id=1,
        title="Test Album",
        artist=artist,
        release_url="https://www.discogs.com/release/1",
        extra_artists=extra_artists or [],
        track_writers=track_writers,
    )


def test_writer_credits_track_level_scoped() -> None:
    # Multi-track release: the played track's position returns ONLY that
    # track's writers (provenance="track"), with no bleed from the sibling.
    release = _release_with_track_writers(
        "Sessa",
        track_writers={
            "A1": [ArtistCredit(name="Track One Writer", role="Written-By")],
            "A2": [ArtistCredit(name="Track Two Writer", role="Composed By")],
        },
    )
    wc = writer_credits_from_release(release, track_position="A1")
    assert isinstance(wc, WriterCredits)
    assert wc.names == ["Track One Writer"]
    assert wc.provenance == "track"
    assert wc.track_position == "A1"
    assert "Track Two Writer" not in wc.names  # no cross-track bleed
    assert "Written-By" in (wc.roles or [])


def test_writer_credits_track_missing_falls_back_to_release() -> None:
    # The played track has no per-track writer entry; fall back to the
    # release-level credit (provenance="release", track_position=None).
    release = _release_with_track_writers(
        "Jessica Pratt",
        track_writers={"A1": [ArtistCredit(name="Track One Writer", role="Written-By")]},
        extra_artists=[ArtistCredit(name="Jessica Pratt", role="Written-By")],
    )
    wc = writer_credits_from_release(release, track_position="B2")  # no map entry
    assert isinstance(wc, WriterCredits)
    assert wc.names == ["Jessica Pratt"]
    assert wc.provenance == "release"
    assert wc.track_position is None


@pytest.mark.parametrize("position", [None, ""])
def test_writer_credits_null_position_falls_back_without_raising(position: str | None) -> None:
    # A null/empty resolved position falls back to release-level credits and
    # never raises — a missing position is normal, not exceptional.
    release = _release_with_track_writers(
        "Cat Power",
        track_writers={"A1": [ArtistCredit(name="Track One Writer", role="Written-By")]},
        extra_artists=[ArtistCredit(name="Cat Power", role="Written-By")],
    )
    wc = writer_credits_from_release(release, track_position=position)
    assert isinstance(wc, WriterCredits)
    assert wc.provenance == "release"
    assert wc.names == ["Cat Power"]


def test_writer_credits_track_position_without_map_falls_back() -> None:
    # A release that carries no per-track writer map at all (Phase-1 shape):
    # a supplied position can't index anything, so fall back to release-level
    # without raising.
    release = _release(
        "Juana Molina",
        [ArtistCredit(name="Juana Molina", role="Written-By")],
    )
    wc = writer_credits_from_release(release, track_position="A1")
    assert isinstance(wc, WriterCredits)
    assert wc.provenance == "release"
    assert wc.names == ["Juana Molina"]


def test_writer_credits_compilation_track_level_resolves() -> None:
    # On a VA/compilation release the release-level guard still returns None,
    # but a per-track writer on the played track resolves (provenance="track").
    release = _release_with_track_writers(
        "Various",
        track_writers={"3": [ArtistCredit(name="Comp Track Writer", role="Written-By")]},
        extra_artists=[ArtistCredit(name="Some Writer", role="Written-By")],
    )
    # Release-level path (no position) is still suppressed on the comp.
    assert writer_credits_from_release(release) is None
    # Track-level path bypasses the VA guard.
    wc = writer_credits_from_release(release, track_position="3")
    assert isinstance(wc, WriterCredits)
    assert wc.names == ["Comp Track Writer"]
    assert wc.provenance == "track"
    assert wc.track_position == "3"
