"""V/A-compilation rescue for the library-miss probe (LML#784 category 2).

Retrieval finds the right compilations — the fuzzy ``q=`` arm surfaces them —
but the standard 80/80 floor structurally cannot pass them: the release-level
credit is "Various" (artist axis ~27 against a real performer name) and comp
titles carry subtitles or embedded performer names the query never has
(album axis ~26–45 under ``token_sort_ratio``). The performer evidence lives
in two places neither scoring axis exploits:

* **Title segments** — Discogs files performer-retrospective comps under
  "Various" with the performer in the title: "Lee Scratch Perry - Born In
  The Sky (Upsetter At The Controls 1969-1975)".
* **Tracklist credits** — a featured artist on a themed comp appears only
  per-track: in the structured ``artists`` array, or embedded in the track
  title's " - " segments ("Lionel Tasquier - Hiroshima") when the uploader
  filed the credit under ``extraartists``, which the release model drops.

This module scores compilation-credited candidates against that evidence,
keeping every precision guard structural rather than statistical:

* Same 80 floor, same ``score_match`` (``token_sort``) scorer — never
  ``token_set_ratio``, so the #719 subset-inflation shape cannot re-enter
  (unrelated long material still degrades the score; the literal #719
  candidate "Black Leather (The Hound Dog Mix)" scores 42.9 full / 18.2
  paren-stripped).
* Variants are delimiter-bounded: " - "-split segments, their cumulative
  prefixes, and a single trailing-parenthetical strip. No token-subset
  scoring anywhere.
* Both axes must pass, and the artist axis is the load-bearing gate — it
  requires the queried performer to genuinely appear in the candidate's
  title segments or tracklist credits, which is exactly the evidence a
  human uses to resolve these comps.
* Only ``is_compilation_artist`` candidates enter (a normal credit already
  had its shot at the standard floor), and two query shapes skip the rescue
  entirely: a compilation-artist query (no performer signal to verify — the
  #638/#592 shape where the title is the only axis), and degenerate axes
  (artist ≈ album: typed-out self-titled or the category-4 swap, where one
  title segment would satisfy both axes).

Tracklist fetches ride the existing ``get_release`` read-through (in-mem →
PG → API) and are paid only for candidates that already passed the album
axis — at most ``per_page`` (5) per library-miss lookup, in practice one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import (
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    score_match,
)
from discogs.models import DiscogsSearchResult
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)

_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")

_SEGMENT_DELIMITER = " - "

_MAX_SEGMENTS = 4
"""Variant-generation bound. Comp titles with more " - " segments than this
are pathological; the cap keeps the variant cross-product small."""


def _segments(text: str) -> list[str]:
    """Non-empty, stripped " - "-delimited segments of ``text``."""
    return [s.strip() for s in text.split(_SEGMENT_DELIMITER) if s.strip()]


def _title_variants(title: str) -> list[str]:
    """Delimiter-bounded variants of a compilation title.

    The full title, each " - "-split segment, each left-cumulative prefix
    ("A - B" from "A - B - C"), and each of those with one trailing
    parenthetical stripped. Bounded transformations only — every variant is
    a contiguous piece of the real title, so ``token_sort`` scoring against
    them cannot subset-inflate the way ``token_set_ratio`` does (#719).
    """
    variants: list[str] = []

    def _add(v: str) -> None:
        v = v.strip()
        if v and v not in variants:
            variants.append(v)

    _add(title)
    segments = _segments(title)[:_MAX_SEGMENTS]
    for segment in segments:
        _add(segment)
    # Upper bound is inclusive of len(segments): when the cap truncated a
    # longer title, the full capped prefix is a distinct variant (for an
    # uncapped title it deduplicates against the full title).
    for i in range(2, len(segments) + 1):
        _add(_SEGMENT_DELIMITER.join(segments[:i]))
    for v in list(variants):
        _add(_TRAILING_PAREN_RE.sub("", v))
    return variants


def _clears_floor(query: str, candidates: Sequence[str]) -> bool:
    return any(
        score_match(query, candidate) >= SCORE_MATCH_ACCEPTANCE_FLOOR for candidate in candidates
    )


async def _tracklist_credits_artist(
    discogs_service: DiscogsService, release_id: int, query_artist: str
) -> bool:
    """True when any per-track artist evidence on the release clears the floor.

    Two evidence forms, both real on Discogs V/A comps (verified live on
    27518829): the structured per-track ``artists`` array, and the performer
    embedded in the track title's " - " segments ("Lionel Tasquier -
    Hiroshima") — uploaders who put credits in ``extraartists`` (which the
    release model drops) title tracks this way, leaving ``artists`` empty.

    A fetch failure (outage, rate limit, breaker shed) is "couldn't verify",
    not "verified absent" — logged and treated as no-evidence so the rescue
    degrades to the pre-#784 no-match rather than raising.
    """
    try:
        release = await discogs_service.get_release(release_id)
    except Exception:
        logger.warning("V/A rescue tracklist fetch failed for release %s", release_id)
        return False
    if release is None or not release.tracklist:
        return False
    for track in release.tracklist:
        for credit in track.artists or []:
            if score_match(query_artist, credit) >= SCORE_MATCH_ACCEPTANCE_FLOOR:
                return True
        for segment in _segments(track.title or ""):
            if score_match(query_artist, segment) >= SCORE_MATCH_ACCEPTANCE_FLOOR:
                return True
    return False


async def find_va_comp_match(
    results: Sequence[DiscogsSearchResult],
    *,
    query_artist: str,
    query_album: str,
    discogs_service: DiscogsService,
) -> DiscogsSearchResult | None:
    """Return the first compilation candidate with dual-axis evidence, or None.

    Called by ``_library_miss_discogs_search`` only after the standard floor
    (and its LML#784 category-1 API retry) found nothing. Candidates are
    consumed in input order (confidence order); the first to pass both axes
    wins:

    * **Album axis** — ``query_album`` clears the 80 floor against any
      :func:`_title_variants` of the candidate's title. Checked first
      because it is pure CPU.
    * **Artist axis** — ``query_artist`` clears against any title segment
      variant, or (fetching the tracklist once) against any per-track
      artist credit.
    """
    query_artist = (query_artist or "").strip()
    query_album = (query_album or "").strip()
    if not query_artist or not query_album or not results:
        return None
    if is_compilation_artist(query_artist):
        return None
    if score_match(query_artist, query_album) >= SCORE_MATCH_ACCEPTANCE_FLOOR:
        # Degenerate dual-axis: a (near-)identical artist and album — the
        # typed-out self-titled shape, or the category-4 placeholder swap
        # upstream — lets one embedded title segment satisfy both axes, and
        # a V/A compilation is never anyone's self-titled album.
        return None

    for candidate in results:
        if not is_compilation_artist(candidate.artist or ""):
            continue
        title = (candidate.album or "").strip()
        if not title:
            continue
        variants = _title_variants(title)
        if not _clears_floor(query_album, variants):
            continue
        if _clears_floor(query_artist, variants):
            logger.info(
                "V/A rescue: matched release %s via embedded title segment for artist=%r",
                candidate.release_id,
                query_artist,
            )
            return candidate
        if candidate.release_id and await _tracklist_credits_artist(
            discogs_service, candidate.release_id, query_artist
        ):
            logger.info(
                "V/A rescue: matched release %s via tracklist credit for artist=%r",
                candidate.release_id,
                query_artist,
            )
            return candidate
    return None
