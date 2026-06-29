"""Writer-role heuristic for BMI composer credits (LML#699).

Classifies Discogs ``role`` strings (from the ``release_track_artist`` /
``release_artist`` extra credits already fetched at resolution time) as
songwriter/composer credits, and assembles the release-level
``DiscogsWriterCredits`` surfaced on the lookup response.

Role strings are stored verbatim as Discogs emits them, so the classifier
normalizes aggressively: any ``[qualifier]`` is stripped from the whole cell
(``"Written-By [Uncredited]"`` -> ``"Written-By"``), the cell is split on
``","`` into component roles, hyphens are folded to spaces
(``"Written-By"`` == ``"Written By"``), and each component is lowercased and
matched against a fixed base set. A cell counts as a writer credit if ANY
component matches. ``Arranged By`` and ``Adapted By`` are deliberately excluded
(plan decision); performer/engineer roles never match. The base set and its
variants are grounded in a live discogs-cache enumeration (2026-06-28). See
``plans/699-composer-credits-bmi.md``.
"""

from __future__ import annotations

import re

from wxyc_etl.text import is_compilation_artist

from discogs.models import ArtistCredit, ReleaseMetadataResponse, WriterCredits
from generated.api_models import Provenance

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
    """Distinct verbatim role strings that classified as writer credits."""
    roles: list[str] = []
    seen: set[str] = set()
    for credit in credits:
        if is_writer_role(credit.role) and credit.role and credit.role not in seen:
            seen.add(credit.role)
            roles.append(credit.role)
    return roles


def writer_credits_from_release(
    release: ReleaseMetadataResponse,
) -> WriterCredits | None:
    """Release-level writer credits for ``release``, or ``None`` (LML#699 Phase 1).

    Returns ``None`` for a compilation / Various-Artists release: release-level
    writers are meaningless for an individual track on a comp, so the guard
    fires before any extraction. Otherwise extracts the writer-role subset of
    ``extra_artists`` and tags it ``provenance="release"`` (the whole-release
    approximation; per-track precision is Phase 2). Returns ``None`` when no
    writer credit resolves -- never fabricated.
    """
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
