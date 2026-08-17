"""Slug-scored Wikipedia URL extractor (LML#513).

The pre-existing extractor (still the fallback here) is a first-substring
match over the Discogs artist ``urls`` list for anything containing
``"wikipedia.org"`` — no signal that the picked URL is the artist's own
page rather than a band member, a related label, or an eponymous
album/song page. :func:`pick_artist_wikipedia_url` replaces it with a
slug-similarity score against the resolved Discogs artist name, gated
behind :data:`WIKIPEDIA_SLUG_MATCH_ENV_VAR` (default OFF).

Placement: this helper imports the ``score_match`` / ``SCORE_MATCH_ACCEPTANCE_FLOOR``
/ ``strip_discogs_disambig`` trio from ``clients/streaming/matching.py`` the
same way ``lookup/artist_resolution.py`` already does — no ``discogs/``
module imports from ``clients/`` today (the edge runs the other way,
``clients/bandcamp.py`` imports ``discogs.admission``), so a ``discogs/``
home for this module would create the repo's first ``discogs -> clients``
edge and a package cycle.

Flag semantics: while ``LML_WIKIPEDIA_SLUG_MATCH`` is OFF, the served
``PickedWikiUrl.url`` is always the legacy first-match pick and
``below_floor`` is always ``True`` — this is what makes
``LML_BIO_PREFER_WIKIPEDIA`` (Phase B) inert without this flag: Phase B's
bio-fetch gate checks ``below_floor`` before ever touching the network, so
"no above-floor pick" while this flag is off is enough on its own to keep
that gate closed. Shadow telemetry (the ``wikipedia_slug_pick`` projection)
fires unconditionally so production divergence between the two picks is
quantified before the flag ever flips.

LML#1192 review round 6: candidate URL parsing, slug scoring, the
hard-reject denylist, and language normalization moved to
``lookup/wikipedia_candidates.py`` — this module owns the SERVING decision
(flag + floor gate) and shadow telemetry, and calls into that one for the
scoring mechanics. This module's own decision layer
(:func:`pick_artist_wikipedia_url`) always scores with the denylist fully
decisive (``include_denylisted=False``, the default) — see the extracted
module's docstring for why: this is the synchronous, no-fetch request path,
which has no live payload to validate a denylisted guess against and never
will (the plan's Non-goals rule out a live Wikipedia dependency on
``/lookup``), so the denylist must stay this path's only defense.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass

import sentry_sdk

from clients.streaming.matching import SCORE_MATCH_ACCEPTANCE_FLOOR
from core.search import resolve_bool_env
from lookup.wikipedia_candidates import (
    extract_lang,
    first_wikipedia_match,
    score_candidates,
    select_best,
)

logger = logging.getLogger(__name__)

WIKIPEDIA_SLUG_MATCH_ENV_VAR = "LML_WIKIPEDIA_SLUG_MATCH"
"""When set to a true spelling (``core.search.resolve_bool_env``'s
vocabulary), the slug-scored pick is served (when it clears the floor)
instead of the legacy first-match pick. Default OFF — see
``docs/env-vars.md``."""


def _wikipedia_slug_match_enabled() -> bool:
    """Read the flag at call time via the shared ``resolve_bool_env``
    (LML#1204 item 3) so it is a no-redeploy Railway lever, mirroring
    ``lookup.admission.is_shed_enforced``."""
    return resolve_bool_env(WIKIPEDIA_SLUG_MATCH_ENV_VAR, default=False)


@dataclass(frozen=True)
class PickedWikiUrl:
    """One resolved Wikipedia-URL decision for an artist.

    ``url``/``lang`` are the SERVED pick (flag- and floor-gated — see the
    module docstring); ``slug_score`` is the winning slug-scored candidate's
    score (``0.0`` when no wikipedia.org URL parsed to a scoreable
    candidate at all). ``below_floor`` is ``True`` whenever the served
    ``url`` is the legacy fallback rather than a confidently-scored slug
    pick — either because no candidate cleared
    ``SCORE_MATCH_ACCEPTANCE_FLOOR``, or because the flag is off. Phase B
    never fetches bio text for a ``below_floor=True`` pick.
    """

    url: str | None
    lang: str | None
    slug_score: float
    below_floor: bool


def _project_wikipedia_slug_pick(
    *,
    heuristic_pick: str | None,
    slug_pick: str | None,
    slug_score: float,
    clears_floor: bool,
    agreement: bool,
) -> None:
    """Shadow telemetry, fires regardless of the enable flag (LML#513).

    Mirrors ``lookup.artist_resolution._log_artist_identity_split_gate``:
    ``set_data`` on the active Sentry transaction (single-item view) AND
    ``add_breadcrumb`` (accumulating view — LML#1192 review, A4: a fixed
    ``set_data`` key is last-writer-wins across the multiple items sharing
    one ``/lookup/bulk`` transaction, which would silently drop every item's
    projection but the last; the breadcrumb list accumulates instead), plus
    a 1% sampled INFO log line. Best-effort — each SDK call is independently
    swallowed so telemetry can never break enrichment, and one surface's
    failure can't suppress another's.

    Callers must skip this entirely for the no-URL-at-all case (there is
    nothing to compare — see the call site) rather than pass ``None``/``None``
    through: recording that as a "disagreement" would flood the divergence
    signal with non-events.
    """
    payload = {
        "heuristic_pick": heuristic_pick,
        "slug_pick": slug_pick,
        "slug_score": slug_score,
        "clears_floor": clears_floor,
        "agreement": agreement,
    }
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("wikipedia_slug_pick", payload)
    except Exception as e:
        logger.warning("Failed to project wikipedia_slug_pick onto Sentry transaction: %s", e)
    try:
        sentry_sdk.add_breadcrumb(category="wikipedia_slug_pick", level="info", data=payload)
    except Exception as e:
        logger.warning("Failed to add wikipedia_slug_pick breadcrumb: %s", e)
    if random.random() < 0.01:
        logger.info("wikipedia_slug_pick %s", payload)


@dataclass(frozen=True)
class ExtractorComparison:
    """Both extractor picks for one artist, independent of the enable flag
    and the score floor — the shape ``scripts/wikipedia_url_validation.py``
    (LML#513's empirical gate) and the shadow-telemetry projection both need.
    ``slug_pick is None`` whenever every wikipedia.org URL was hard-rejected
    or scored on an empty stripped slug; ``slug_score``/``slug_lang`` are the
    winning candidate's, or ``0.0``/``None`` when there is none.
    """

    heuristic_pick: str | None
    slug_pick: str | None
    slug_score: float
    slug_lang: str | None

    @property
    def clears_floor(self) -> bool:
        """LML#1192 review round 3, finding 9: this exact predicate was
        hand-written three times (here, ``scripts/warm_wikipedia_bios.py``,
        ``scripts/wikipedia_url_validation.py``) — now the single owner."""
        return self.slug_pick is not None and self.slug_score >= SCORE_MATCH_ACCEPTANCE_FLOOR

    @property
    def agreement(self) -> bool:
        """Whether the slug-scored and legacy heuristic picks are the same
        URL. ``False`` (not ``None``-comparable) when there is no slug pick
        at all — this repo's convention for "no agreement to report"."""
        return self.slug_pick is not None and self.slug_pick == self.heuristic_pick

    def resolve(self, *, slug_enabled: bool) -> PickedWikiUrl | None:
        """Own the flag/floor SERVING decision — the one place that turns a
        comparison into what actually gets used.

        ``None`` only when neither extractor found a wikipedia.org URL at
        all (matching :func:`pick_artist_wikipedia_url`'s pre-existing
        contract). Otherwise: ``slug_enabled`` and :attr:`clears_floor` both
        true serves the slug pick; every other case falls back to the
        legacy heuristic pick, with ``lang`` derived from THAT url (LML#1192
        review, A5 — never from ``slug_lang``, which describes whichever
        candidate scored highest and may be a completely different page
        than the one actually being served here). This is the decision
        body for the live read path
        (``pick_artist_wikipedia_url``, ``slug_enabled=_wikipedia_slug_match_enabled()``).

        LML#1192 review round 4, P0-2: the offline drain
        (``scripts/warm_wikipedia_bios.py``) and background miss-warm no
        longer call this method — an unvalidated score-only pick can't
        distinguish a canonical page from its disambiguation/type-page
        sibling (three straight review rounds of a wrong string-only
        tiebreak proved that), so both now go through
        ``lookup.wikipedia_pick_validation.resolve_and_validate_pick``
        instead, which fetches and validates each ranked candidate in turn.
        """
        if self.heuristic_pick is None and self.slug_pick is None:
            return None
        if slug_enabled and self.clears_floor:
            return PickedWikiUrl(
                url=self.slug_pick,
                lang=self.slug_lang,
                slug_score=self.slug_score,
                below_floor=False,
            )
        fallback_lang = extract_lang(self.heuristic_pick or "")
        return PickedWikiUrl(
            url=self.heuristic_pick,
            lang=fallback_lang,
            slug_score=self.slug_score,
            below_floor=True,
        )


def compare_wikipedia_extractors(
    urls: Sequence[str] | None, artist_name: str
) -> ExtractorComparison:
    """Run both extractors over ``urls`` and return their raw picks.

    LML#1192 review round 6: ``score_candidates`` is called with
    ``include_denylisted`` left at its default (``False``) — the
    synchronous request path always keeps the hard-reject denylist fully
    decisive, since it has no live fetch to validate a denylisted guess
    against. See ``lookup/wikipedia_candidates.py``'s module docstring for
    the fetch-capable/sync asymmetry this is one half of.
    """
    heuristic_url = first_wikipedia_match(urls)
    best = select_best(score_candidates(urls, artist_name or ""))
    return ExtractorComparison(
        heuristic_pick=heuristic_url,
        slug_pick=best.url if best is not None else None,
        slug_score=best.score if best is not None else 0.0,
        slug_lang=best.lang if best is not None else None,
    )


def pick_artist_wikipedia_url(urls: Sequence[str] | None, artist_name: str) -> PickedWikiUrl | None:
    """Pick the Wikipedia URL to serve for ``artist_name`` from a Discogs
    artist's ``urls`` list.

    See the module docstring for the flag/floor interplay. Returns ``None``
    only when ``urls`` carries no ``wikipedia.org`` URL at all (matching the
    legacy extractor's contract).
    """
    comparison = compare_wikipedia_extractors(urls, artist_name)

    # LML#1192 review, A4: the no-URL-at-all case is a non-event, not a
    # disagreement — check it BEFORE emitting telemetry, not after, so it
    # never floods the divergence signal.
    if comparison.heuristic_pick is None and comparison.slug_pick is None:
        return None

    _project_wikipedia_slug_pick(
        heuristic_pick=comparison.heuristic_pick,
        slug_pick=comparison.slug_pick,
        slug_score=comparison.slug_score,
        clears_floor=comparison.clears_floor,
        agreement=comparison.agreement,
    )

    return comparison.resolve(slug_enabled=_wikipedia_slug_match_enabled())
