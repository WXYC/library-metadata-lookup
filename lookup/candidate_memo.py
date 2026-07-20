"""Request-scoped carry-through of artist-filtered Discogs track searches (LML#867).

A single artist+song lookup used to issue the *same* ``search_releases_by_track``
with the ``artist=`` field filter twice: once in step 2
(``resolve_albums_for_track`` -> ``lookup_releases_by_track``) and again in step 3
(``TRACK_ON_COMPILATION``'s Wave A). Because the two calls differ in incidental
params (``per_page``), neither the L1 nor the PG cache served the second from the
first — pure duplication on the request's critical path.

:class:`TrackCandidateMemo` is the request-scoped carrier that closes that gap.
The producer (step 2) records the raw candidate releases it fetched — plus the
per-candidate validation verdicts, when it validated — keyed by
``(track, artist)``. The consumer (step 3) reuses that candidate set as Wave A's
seed instead of re-searching. It is **request-scoped only**: created fresh per
``perform_lookup`` and never shared across requests (no new cross-request cache
tier — see the issue's "Request-scoped only" constraint).

Under LML#866's library-first inversion the producer may hold *unvalidated*
candidates (a non-library artist skips every tracklist fetch), so the memo carries
per-candidate verdict state rather than assuming every carried candidate was
validated. ``TrackCandidateSet.validated`` records whether validation ran at all;
``verdicts`` maps ``release_id -> bool`` for the candidates that were checked (an
absent id means "verdict unknown", not "invalid").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from wxyc_etl.text import to_match_form as normalize_for_comparison

if TYPE_CHECKING:
    from collections.abc import Iterable

    from discogs.models import ReleaseInfo


def _memo_key(track: str, artist: str) -> tuple[str, str]:
    """Normalize a ``(track, artist)`` pair to a stable, comparison-form key.

    Uses the same ``to_match_form`` normalization the strategies use for artist
    comparison, so a caller-typed name and its later use in the consumer collapse
    to one key regardless of case/diacritics/punctuation drift.
    """
    return (
        normalize_for_comparison(track or "").strip(),
        normalize_for_comparison(artist or "").strip(),
    )


@dataclass
class TrackCandidateSet:
    """One ``(track, artist)`` entry: the artist-filtered search's candidates.

    Attributes:
        releases: The raw ``ReleaseInfo`` candidates the ``artist=`` field search
            returned (pre-library-gate). This is Wave A's seed for the consumer.
        verdicts: ``release_id -> validated?`` for candidates the producer checked
            against Discogs tracklists. A release id absent from this map has an
            *unknown* verdict (validation may have early-exited or been skipped).
        validated: Whether per-candidate tracklist validation ran at all. False
            when LML#866's library-first gate skipped validation (non-library
            artist) — the candidates are still carried, just unvalidated.
    """

    releases: list[ReleaseInfo]
    verdicts: dict[int, bool] = field(default_factory=dict)
    validated: bool = False


@dataclass
class TrackCandidateMemo:
    """Request-scoped memo of artist-filtered track searches, keyed by ``(track, artist)``.

    One instance per ``perform_lookup`` pass. The producer calls :meth:`record`;
    the consumer calls :meth:`get`. A miss (``None``) means "no carried candidates
    for this exact query" and the consumer falls back to a fresh search — which is
    what happens when the consumer's query diverges from the producer's (e.g. a
    canonical-artist swap or a remix-augmented track title changes the key).
    """

    _by_key: dict[tuple[str, str], TrackCandidateSet] = field(default_factory=dict)

    def record(
        self,
        track: str,
        artist: str,
        releases: Iterable[ReleaseInfo],
        *,
        verdicts: dict[int, bool] | None = None,
        validated: bool = False,
    ) -> None:
        """Record (or overwrite) the candidate set for ``(track, artist)``.

        The producer calls this after its ``artist=`` search: first with the raw
        candidates (``validated=False``), then again with verdicts filled in once
        validation completes. The last write wins, so the post-validation call
        carries the richest state.
        """
        self._by_key[_memo_key(track, artist)] = TrackCandidateSet(
            releases=list(releases),
            verdicts=dict(verdicts or {}),
            validated=validated,
        )

    def get(self, track: str, artist: str) -> TrackCandidateSet | None:
        """Return the carried candidate set for ``(track, artist)``, or ``None``."""
        return self._by_key.get(_memo_key(track, artist))
