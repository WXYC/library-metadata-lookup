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
"""

from __future__ import annotations

import logging
import os
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import unquote

import sentry_sdk

from clients.streaming.matching import (
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    score_match,
    strip_discogs_disambig,
)

logger = logging.getLogger(__name__)

WIKIPEDIA_SLUG_MATCH_ENV_VAR = "LML_WIKIPEDIA_SLUG_MATCH"
"""When set to a ``_TRUE_FLAG_VALUES`` spelling, the slug-scored pick is
served (when it clears the floor) instead of the legacy first-match pick.
Default OFF — see ``docs/env-vars.md``."""

_TRUE_FLAG_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})
"""Spellings that enable a default-OFF boolean flag. Mirrors
``lookup.admission``'s flag of the same name/polarity."""


def _wikipedia_slug_match_enabled() -> bool:
    """Read the flag at call time (no Settings indirection) so it is a
    no-redeploy Railway lever, mirroring ``lookup.admission.is_shed_enforced``."""
    raw = os.getenv(WIKIPEDIA_SLUG_MATCH_ENV_VAR)
    if raw is None:
        return False
    return raw.strip().lower() in _TRUE_FLAG_VALUES


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


_WIKI_URL_RE = re.compile(r"^https?://([a-z0-9.-]+)\.wikipedia\.org/wiki/(.+)$", re.IGNORECASE)
_LANG_ONLY_RE = re.compile(r"^https?://([a-z0-9.-]+)\.wikipedia\.org", re.IGNORECASE)

# Hard-reject qualifiers (LML#513): a slug whose trailing parenthetical (or
# bare trailing token/phrase) names one of these is a non-artist page —
# rejected BEFORE any disambig stripping, since stripping first would
# destroy the only signal separating an eponymous album/song page from the
# artist page itself (``Sessa_(album)`` strips and scores 100 against
# artist ``Sessa`` exactly like the real ``Sessa_(2)`` disambiguator would).
_HARD_REJECT_QUALIFIERS: frozenset[str] = frozenset(
    {"album", "song", "single", "ep", "soundtrack", "film", "tv series", "discography"}
)
_TRAILING_PAREN_RE = re.compile(r"\(([^()]+)\)\s*$")


def _is_hard_rejected_slug(decoded_slug: str) -> bool:
    """True when ``decoded_slug`` names a non-artist qualifier (see above)."""
    match = _TRAILING_PAREN_RE.search(decoded_slug)
    if match and match.group(1).strip().lower() in _HARD_REJECT_QUALIFIERS:
        return True
    lowered = decoded_slug.strip().lower()
    return any(
        lowered == qualifier or lowered.endswith(" " + qualifier)
        for qualifier in _HARD_REJECT_QUALIFIERS
    )


def _first_wikipedia_match(urls: Sequence[str] | None) -> str | None:
    """The legacy heuristic: first URL substring-containing ``wikipedia.org``."""
    for url in urls or ():
        if isinstance(url, str) and "wikipedia.org" in url:
            return url
    return None


def _extract_lang(url: str) -> str | None:
    match = _LANG_ONLY_RE.match(url.strip())
    return match.group(1).lower() if match else None


@dataclass(frozen=True)
class _ScoredCandidate:
    url: str
    lang: str
    score: float


def _score_candidates(urls: Sequence[str] | None, artist_name: str) -> list[_ScoredCandidate]:
    scored: list[_ScoredCandidate] = []
    for url in urls or ():
        if not isinstance(url, str):
            continue
        match = _WIKI_URL_RE.match(url.strip())
        if not match:
            continue
        lang = match.group(1).lower()
        raw_slug = match.group(2).split("#", 1)[0].split("?", 1)[0]
        decoded_slug = unquote(raw_slug).replace("_", " ").strip()
        if not decoded_slug or _is_hard_rejected_slug(decoded_slug):
            continue
        stripped = strip_discogs_disambig(decoded_slug).strip()
        if not stripped:
            continue
        score = score_match(stripped, artist_name)
        scored.append(_ScoredCandidate(url=url, lang=lang, score=score))
    return scored


def _select_best(scored: list[_ScoredCandidate]) -> _ScoredCandidate | None:
    """Highest score wins; ``en`` wins a tie; otherwise first-encountered wins."""
    best: _ScoredCandidate | None = None
    for candidate in scored:
        if best is None or (candidate.score, candidate.lang == "en") > (
            best.score,
            best.lang == "en",
        ):
            best = candidate
    return best


def _project_wikipedia_slug_pick(
    *, heuristic_pick: str | None, slug_pick: str | None, agreement: bool
) -> None:
    """Shadow telemetry, fires regardless of the enable flag (LML#513).

    Mirrors ``lookup.artist_resolution._log_artist_identity_split_gate``:
    ``set_data`` on the active Sentry transaction at production rate, plus a
    1% sampled INFO log line. Best-effort — any SDK error is swallowed so
    telemetry can never break enrichment.
    """
    payload = {
        "heuristic_pick": heuristic_pick,
        "slug_pick": slug_pick,
        "agreement": agreement,
    }
    try:
        transaction = sentry_sdk.get_current_scope().transaction
        if transaction is not None:
            transaction.set_data("wikipedia_slug_pick", payload)
    except Exception as e:
        logger.warning("Failed to project wikipedia_slug_pick onto Sentry transaction: %s", e)
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


def compare_wikipedia_extractors(
    urls: Sequence[str] | None, artist_name: str
) -> ExtractorComparison:
    """Run both extractors over ``urls`` and return their raw picks."""
    heuristic_url = _first_wikipedia_match(urls)
    best = _select_best(_score_candidates(urls, artist_name or ""))
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

    _project_wikipedia_slug_pick(
        heuristic_pick=comparison.heuristic_pick,
        slug_pick=comparison.slug_pick,
        agreement=comparison.slug_pick is not None
        and comparison.slug_pick == comparison.heuristic_pick,
    )

    score_clears_floor = (
        comparison.slug_pick is not None and comparison.slug_score >= SCORE_MATCH_ACCEPTANCE_FLOOR
    )
    if _wikipedia_slug_match_enabled() and score_clears_floor:
        return PickedWikiUrl(
            url=comparison.slug_pick,
            lang=comparison.slug_lang,
            slug_score=comparison.slug_score,
            below_floor=False,
        )

    if comparison.heuristic_pick is None and comparison.slug_pick is None:
        return None

    fallback_lang = comparison.slug_lang or _extract_lang(comparison.heuristic_pick or "")
    return PickedWikiUrl(
        url=comparison.heuristic_pick,
        lang=fallback_lang,
        slug_score=comparison.slug_score,
        below_floor=True,
    )
