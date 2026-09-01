"""Writer-role heuristic for BMI composer credits (LML#699).

Classifies Discogs ``role`` strings (from the ``release_track_artist`` /
``release_artist`` extra credits already fetched at resolution time) as
songwriter/composer credits, and assembles the ``DiscogsWriterCredits`` surfaced
on the lookup response. ``writer_credits_from_release`` resolves at two
precisions: when a played-track position is supplied and that track carries its
own writer credits (the per-track ``release_track_artist`` ``extra=1`` rows,
carried on ``ReleaseMetadataResponse.track_writers``), it emits
``provenance="track"`` scoped to the playcut — bypassing the VA guard, since a
per-track writer is correct even on a compilation; otherwise it falls back to
the release-level ``extra_artists`` subset tagged ``provenance="release"``,
which is suppressed for compilations / Various-Artists releases.

Role strings are stored verbatim as Discogs emits them, so the classifier
normalizes aggressively: any ``[qualifier]`` is stripped from the whole cell
(``"Written-By [Uncredited]"`` -> ``"Written-By"``), the cell is split on
``","`` into component roles, hyphens are folded to spaces
(``"Written-By"`` == ``"Written By"``), and each component is lowercased and
matched against a fixed base set. A cell counts as a writer credit if ANY
component matches. ``Arranged By`` and ``Adapted By`` are deliberately excluded
(plan decision); performer/engineer roles never match. The base set and its
variants are grounded in a live discogs-cache enumeration (2026-06-28). See
``docs/plans/699-composer-credits-bmi.md``.
"""

from __future__ import annotations

import re

from wxyc_etl.text import is_compilation_artist

from discogs.models import ArtistCredit, ReleaseMetadataResponse, WriterCredits

# The writer-credits provenance enum (track/release) is an INLINE enum in
# api.yaml, so datamodel-code-generator names it positionally: the digital
# archive's own inline ``provenance`` (rotation_upload/cd_rip) appears first in
# the spec and now owns the bare ``Provenance`` name, pushing this one to
# ``Provenance1``. The alias keeps every call site on the semantic name. A
# future inline provenance added ahead of it in the spec would shift the
# number again — loudly (the member sets differ), not silently. Stable
# contract-side names are WXYC/wxyc-shared#430.
from generated.api_models import Provenance1 as Provenance

# Normalized base writer roles. A Discogs role component is a writer credit iff,
# after the qualifier strip + hyphen fold + lowercase below, it equals one of
# these. Excludes ``arranged by`` / ``adapted by`` by decision (LML#699).
_WRITER_ROLES: frozenset[str] = frozenset(
    {
        "written by",
        "composed by",
        "music by",
        "lyrics by",
        "songwriter",
        "words by",
    }
)

# Strip a trailing/inline ``[qualifier]`` from a role cell. Applied to the whole
# cell BEFORE the comma split so a comma inside the brackets (e.g.
# ``"Written-By [Words, Music]"``) doesn't fragment the role.
_BRACKET_QUALIFIER = re.compile(r"\s*\[[^\]]*\]")


def _normalize(component: str) -> str:
    """Fold hyphens to spaces, collapse whitespace, lowercase one role component."""
    return " ".join(component.replace("-", " ").split()).lower()


def is_writer_role(role: str | None) -> bool:
    """True if ``role`` is (or contains) a songwriter/composer credit.

    Pure string classification with no release/compilation context. A
    comma-joined cell (e.g. ``"Written-By, Producer"``) counts if any component
    is a writer role; bracket qualifiers and hyphen/space variants are
    normalized away first.
    """
    if not role:
        return False
    cleaned = _BRACKET_QUALIFIER.sub("", role)
    return any(_normalize(part) in _WRITER_ROLES for part in cleaned.split(","))


def extract_writer_names(credits: list[ArtistCredit]) -> list[str]:
    """Distinct writer names from ``credits``, order-preserving (dedup by name).

    An artist credited under multiple writer roles collapses to a single entry;
    non-writer credits are dropped.
    """
    names: list[str] = []
    seen: set[str] = set()
    for credit in credits:
        if is_writer_role(credit.role) and credit.name and credit.name not in seen:
            seen.add(credit.name)
            names.append(credit.name)
    return names


def _matched_roles(credits: list[ArtistCredit]) -> list[str]:
    """Distinct verbatim writer-role strings, from credits that carry a name.

    Gated on ``credit.name`` (not just ``credit.role``) so the audit-trail
    roles stay in sync with the names ``extract_writer_names`` keeps: a
    writer-role credit with an empty name is dropped from both, never surfacing
    a role with no corresponding writer. (``is_writer_role`` already guarantees
    ``credit.role`` is non-empty, so the dedup key is safe.)
    """
    roles: list[str] = []
    seen: set[str] = set()
    for credit in credits:
        role = credit.role
        if role and credit.name and is_writer_role(role) and role not in seen:
            seen.add(role)
            roles.append(role)
    return roles


def _track_level_credits(
    release: ReleaseMetadataResponse, track_position: str | None
) -> WriterCredits | None:
    """Per-track writer credits for ``track_position`` on ``release``, or ``None``.

    The precise path (LML#699 Phase 2): when the resolved track carries its own
    writer credits, those are scoped exactly to the playcut, so this deliberately
    **bypasses the VA/compilation guard** — a per-track writer is correct on a
    compilation precisely *because* it is track-scoped. Returns ``None`` (so the
    caller falls back to the release-level path) when no position is supplied,
    the release carries no per-track writer map, the position has no map entry,
    or that entry yields no writer name. Null-safe throughout: a missing/empty
    position is normal, not exceptional, so the map is never indexed without a
    presence check and nothing is logged.
    """
    if not track_position:
        return None
    track_writers = getattr(release, "track_writers", None)
    if not track_writers:
        return None
    credits = track_writers.get(track_position)
    if not credits:
        return None
    names = extract_writer_names(credits)
    if not names:
        return None
    return WriterCredits(
        names=names,
        roles=_matched_roles(credits),
        provenance=Provenance.track,
        track_position=track_position,
    )


def writer_credits_from_release(
    release: ReleaseMetadataResponse,
    track_position: str | None = None,
) -> WriterCredits | None:
    """Writer credits for the resolved playcut on ``release``, or ``None`` (LML#699).

    Prefers track-level precision: when ``track_position`` resolves to a track
    that carries its own writer credits, returns those tagged
    ``provenance="track"`` (the playcut-scoped credit; bypasses the VA guard).
    Otherwise falls back to the release-level path (Phase 1): ``None`` for a
    compilation / Various-Artists release (release-level writers are meaningless
    for an individual comp track, so the guard fires before extraction), else the
    writer-role subset of ``extra_artists`` tagged ``provenance="release"`` (the
    whole-release approximation). Returns ``None`` when no writer credit resolves
    -- never fabricated.
    """
    track_level = _track_level_credits(release, track_position)
    if track_level is not None:
        return track_level
    if is_compilation_artist(release.artist):
        return None
    extra = release.extra_artists or []
    names = extract_writer_names(extra)
    if not names:
        return None
    return WriterCredits(
        names=names,
        roles=_matched_roles(extra),
        provenance=Provenance.release,
        track_position=None,
    )
