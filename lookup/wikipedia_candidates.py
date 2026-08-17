"""Wikipedia candidate scoring/rejection policy (LML#513/#1192).

Turns a Discogs artist's ``urls`` list into scored, ranked ``wikipedia.org``
candidates: URL parsing, slug decoding, the hard-reject denylist, language
normalization, and the total-order try-ranking. Extracted from
``lookup/wikipedia_url.py`` (LML#1192 review round 6) once that module grew
past its module-budget ceiling adding the fetch-capable/sync-path asymmetry
below -- the scoring/rejection policy is a coherent concern on its own,
shared by ``lookup/wikipedia_url.py`` (the synchronous, no-fetch decision
layer) and ``lookup/wikipedia_pick_validation.py`` (the fetch-capable
picker).

**The synchronous/fetch-capable asymmetry (LML#1192 review round 6, P2-2).**
Through round 5, a slug matching the hard-reject denylist (``Film``,
``Single``, ``Ep``, ``Mixtape``, ``Compilation``, ``Discography``, and any
slug whose trailing WORD is one of those — not just a trailing qualifier
PHRASE) was excluded from scoring entirely, for every caller. That was the
right call when nothing could fetch: a pre-fetch guess hard-rejecting a
likely-wrong candidate was the only defense available. It inverted after
rounds 4-5, once both non-sync callers (the offline drain, the background
miss-warm) started fetching and validating each candidate's actual payload
before ever writing anything — for THEM, the denylist was deleting
candidates *before* the authoritative check those callers already pay for.
Verified live: the Polish/Croatian band Film has its real article at
``/wiki/Film`` — the denylist discarded it outright, ``slug_pick`` came back
``None``, ``clears_floor`` was ``False``, and the whole feature silently,
permanently excluded that artist with no telemetry distinguishing it from a
genuine no-page artist.

:func:`score_candidates` therefore takes ``include_denylisted`` (default
``False``, preserving the synchronous path's decisive hard-reject exactly):

* ``include_denylisted=False`` (the synchronous request path,
  ``lookup.wikipedia_url.pick_artist_wikipedia_url`` — no fetch and never
  will be, per the plan's Non-goals; the denylist is its only defense and
  must stay decisive there) — a denylisted candidate is never admitted, byte
  for byte the round-5 behavior.
* ``include_denylisted=True`` (fetch-capable callers, via
  ``lookup.wikipedia_pick_validation.resolve_and_validate_pick``) — a
  denylisted candidate IS scored and admitted, but :func:`candidate_sort_key`
  ranks it after every non-denylisted candidate regardless of score, so it
  sorts last but stays reachable: tried only once every better candidate has
  been tried and rejected, and only if the caller's ``max_candidates`` cap
  still has room. The live payload — the only thing that actually knows
  whether ``Film`` the slug is really Film the band — decides, not a
  pre-fetch guess. The asymmetry is the ``include_denylisted`` parameter
  itself, not which function happens to call which: every call site names
  its own capability explicitly.

See ``lookup/enrichment/wikipedia_warm.py``'s module docstring for the
paired analysis of what admitting denylisted candidates does to
``MAX_CANDIDATES_PER_WARM``'s bound (short version: nothing — a denylisted
candidate can never outrank a legitimate one for a cap slot, so it only ever
spends a slot the cap would otherwise have left idle).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import unquote

from clients.streaming.matching import score_match, strip_discogs_disambig

_WIKI_URL_RE = re.compile(r"^https?://([a-z0-9.-]+)\.wikipedia\.org/wiki/(.+)$", re.IGNORECASE)
_LANG_ONLY_RE = re.compile(r"^https?://([a-z0-9.-]+)\.wikipedia\.org", re.IGNORECASE)

# Hard-reject qualifiers (LML#513): a slug whose trailing parenthetical (or
# bare trailing token/phrase) NAMES one of these as its last word is a
# non-artist page — rejected BEFORE any disambig stripping, since stripping
# first would destroy the only signal separating an eponymous album/song
# page from the artist page itself (``Sessa_(album)`` strips and scores 100
# against artist ``Sessa`` exactly like the real ``Sessa_(2)`` disambiguator
# would). ``mixtape``/``compilation`` were added after the LML#1192 review
# (round 2) found real WXYC catalog collisions (Chuquimamani-Condori's
# "Edits" is a mixtape; compilation-album qualifiers are common on VA-style
# releases).
#
# LML#1192 review (round 2), finding A1: matching REQUIRES the qualifier to
# be the trailing WORD inside the parenthetical (or at the bare end of the
# slug), not the WHOLE parenthetical/slug — a compound qualifier like
# ``(2015 album)``, ``(Jeff Buckley album)``, or ``(compilation album)``
# must reject on its trailing ``album`` token exactly as readily as a bare
# ``(album)`` does. The original implementation required exact equality
# against the whole bracketed content, so any qualifier with a preceding
# year/name/adjective sailed straight through to scoring and stripped down
# to a leak that could score 100 against an unrelated artist sharing the
# slug's prefix.
#
# LML#1192 review round 6, P2-2: this over-broad "trailing WORD" match is
# KNOWN to reject real artist names too (``Film``, ``Single``, ``Ep``,
# ``Simple Song``, ...) — deliberately left as-is, not narrowed, because
# narrowing the regex can't fix the deeper problem (a string heuristic can
# never distinguish "artist literally named Film" from "Sessa's eponymous
# soundtrack page" without looking at the actual page). The fix is the
# include_denylisted asymmetry in :func:`score_candidates` below, not a
# smarter regex.
_HARD_REJECT_QUALIFIERS: frozenset[str] = frozenset(
    {
        "album",
        "song",
        "single",
        "ep",
        "soundtrack",
        "film",
        "tv series",
        "discography",
        "mixtape",
        "compilation",
    }
)
# Longest-first so "tv series" (which itself contains no shorter alternative
# as a strict prefix) and any future multi-word entry can't be pre-empted by
# a shorter alternative matching first within the same alternation.
_QUALIFIER_ALTERNATION = "|".join(
    re.escape(q) for q in sorted(_HARD_REJECT_QUALIFIERS, key=len, reverse=True)
)
# A trailing parenthetical whose LAST word (immediately before the closing
# paren) is a denylist qualifier -- content before that word, if any, is
# unconstrained, so "(2015 album)" / "(Jeff Buckley album)" match on their
# trailing "album" exactly as "(album)" alone does.
_TRAILING_PAREN_QUALIFIER_RE = re.compile(
    rf"\([^()]*\b(?:{_QUALIFIER_ALTERNATION})\)\s*$", re.IGNORECASE
)
# The no-parens form: the qualifier is the trailing word of the whole slug
# (preceded by whitespace, or the entire slug), e.g. "An Artist Discography".
_TRAILING_BARE_QUALIFIER_RE = re.compile(
    rf"(?:^|\s)(?:{_QUALIFIER_ALTERNATION})\s*$", re.IGNORECASE
)


def is_hard_rejected_slug(decoded_slug: str) -> bool:
    """True when ``decoded_slug`` names a non-artist qualifier (see above).

    This is a PURE classification — it does not decide admission on its
    own. Both fetch-capable and synchronous callers reach this through
    :func:`score_candidates`; see the module docstring for how the two
    diverge on what a ``True`` result means for them.
    """
    stripped = decoded_slug.strip()
    return bool(
        _TRAILING_PAREN_QUALIFIER_RE.search(stripped)
        or _TRAILING_BARE_QUALIFIER_RE.search(stripped)
    )


def first_wikipedia_match(urls: Sequence[str] | None) -> str | None:
    """The legacy heuristic: first URL substring-containing ``wikipedia.org``
    (case-insensitively).

    LML#1192 review round 2, C3: this must stay case-insensitive to agree
    with the two other places a "does this URL point at wikipedia.org"
    judgment gets made -- ``_WIKI_URL_RE``'s ``re.IGNORECASE`` in
    :func:`score_candidates` above, and ``scripts/warm_wikipedia_bios.py``'s
    seed SQL (``au.url ILIKE '%wikipedia.org%'``). A case-sensitive check
    here let a mixed-case domain (e.g. ``en.Wikipedia.org``) score a real
    ``slug_pick`` via the regex while this function returned ``None`` for
    the same URL -- silently dropping the URL entirely from a below-floor
    ``PickedWikiUrl`` on the live read path (``compare_wikipedia_extractors``);
    also used directly by ``lookup.wikipedia_pick_validation.resolve_and_validate_pick``
    (LML#1192 review round 4, P0-2) for the offline drain and background
    miss-warm's fetch-validated heuristic fallback.
    """
    for url in urls or ():
        if isinstance(url, str) and "wikipedia.org" in url.lower():
            return url
    return None


# LML#1192 review round 4, P0-7: "www.wikipedia.org/wiki/..." is a realistic
# input (`artist_url` is unvalidated free text, and a browser 301s a bare
# `wikipedia.org` root to the `www` host) -- but "www" is a hostname prefix,
# not a language code, and REPLACES the whole subdomain rather than
# prefixing a real one (there is no "www.en.wikipedia.org"). Verified live:
# the REST API 500s on `https://www.wikipedia.org/api/rest_v1/...` (`en.`
# 200s for the same title).
_WWW_HOST = "www"

# LML#1192 review round 6, P2-3: `en.m.wikipedia.org` (the mobile host) has
# a DIFFERENT shape from `www` -- here the real language code comes FIRST
# and a trailing ".m" mobile marker is APPENDED after it, so `raw` is
# "en.m", not "m.en". Un-normalized, "en.m" round-tripped straight into the
# NOT NULL `lang` column and, worse, into `candidate_sort_key`'s
# `lang == "en"` tiebreak, where "en.m" != "en" silently loses the
# English-preference tiebreak entirely. Verified live:
# `score_candidates(["https://en.m.wikipedia.org/wiki/Sun_Ra",
# "https://es.wikipedia.org/wiki/Sun_Ra"], "Sun Ra")` ranked the Spanish URL
# first; the same two URLs minus ".m" correctly ranked English first.
# `en.m.wikipedia.org` returns 200 (unlike `www`, which 500s), so this
# surfaces as WRONG CONTENT silently written and served, not a fetch error
# anything would notice. Stripping a trailing ".m" label (not denylisting
# "en.m" as one more literal -- "m." is a paste-common mobile prefix
# regardless of which language precedes it) fixes the sort tiebreak, the
# API host, and the stored `lang` column all at once, since all three read
# this one value. Legitimate multi-part codes (`zh-min-nan`, hyphenated, no
# dot) and `simple` (a real, single-label Wikipedia edition, not a host
# prefix) have no dot at all and are untouched by this check.
_MOBILE_HOST_SUFFIX = ".m"


def normalize_lang_subdomain(raw: str) -> str:
    """Normalize a captured Wikipedia subdomain into a real language code.

    ``www`` (round 4, P0-7) REPLACES the entire subdomain -> ``en``. A
    trailing ``.m`` (round 6, P2-3, the mobile host) is STRIPPED, keeping
    whatever real language code preceded it (``en.m`` -> ``en``,
    ``es.m`` -> ``es``). Everything else — including hyphenated codes like
    ``zh-min-nan`` and single-label editions like ``simple``, neither of
    which contains a dot — passes through unchanged.
    """
    if raw == _WWW_HOST:
        return "en"
    if raw.endswith(_MOBILE_HOST_SUFFIX):
        stripped = raw[: -len(_MOBILE_HOST_SUFFIX)]
        if stripped:
            return stripped
    return raw


def extract_lang(url: str) -> str | None:
    match = _LANG_ONLY_RE.match(url.strip())
    return normalize_lang_subdomain(match.group(1).lower()) if match else None


@dataclass(frozen=True)
class ScoredCandidate:
    """One ``wikipedia.org`` URL, scored against a query artist name.

    ``denylisted`` (LML#1192 review round 6, P2-2) is ``True`` when
    :func:`is_hard_rejected_slug` matched this candidate's slug — it is a
    SEPARATE fact from ``score`` (real name-similarity quality, computed
    the same way regardless), so a caller admitting denylisted candidates
    (``include_denylisted=True``) can still floor-filter and rank them
    correctly. Always ``False`` for a synchronous-path candidate (that
    caller never admits a denylisted one in the first place).
    """

    url: str
    lang: str
    score: float
    denylisted: bool = False


def score_candidates(
    urls: Sequence[str] | None, artist_name: str, *, include_denylisted: bool = False
) -> list[ScoredCandidate]:
    """Score every ``wikipedia.org`` URL in ``urls`` against ``artist_name``.

    ``include_denylisted`` (LML#1192 review round 6, P2-2 — see the module
    docstring for the full asymmetry rationale) controls whether a
    hard-rejected slug is admitted at all: ``False`` (the default, and the
    synchronous request path's only choice) excludes it entirely, exactly
    the round-5 behavior; ``True`` (fetch-capable callers) still scores and
    admits it, flagged via :attr:`ScoredCandidate.denylisted`, so
    :func:`candidate_sort_key` can rank it after every non-denylisted
    candidate rather than deleting it before the live fetch gets a say.
    """
    # LML#1192 review (round 2), finding A2: strip the QUERY artist name's
    # own Discogs disambiguation suffix too, symmetrically with the
    # candidate slug -- mirrors lookup/artist_resolution.py's
    # _artist_pair_verified (which strips both sides for exactly this
    # reason). A resolved Discogs artist name routinely carries its own
    # "(N)"/"(UK)"/"(band)" disambiguator (e.g. "Sessa (2)"), and without
    # stripping it here, "Sessa" vs "Sessa (2)" scores 71.43 -- below the 80
    # floor -- so a disambiguated artist could never match its own correct
    # Wikipedia page. Falls back to the original string if stripping would
    # leave it empty (an artist name that was entirely a disambiguator).
    artist_stripped = strip_discogs_disambig(artist_name).strip() or artist_name
    scored: list[ScoredCandidate] = []
    for url in urls or ():
        if not isinstance(url, str):
            continue
        match = _WIKI_URL_RE.match(url.strip())
        if not match:
            continue
        lang = normalize_lang_subdomain(match.group(1).lower())
        raw_slug = match.group(2).split("#", 1)[0].split("?", 1)[0]
        decoded_slug = unquote(raw_slug).replace("_", " ").strip()
        if not decoded_slug:
            continue
        denylisted = is_hard_rejected_slug(decoded_slug)
        if denylisted and not include_denylisted:
            continue
        stripped = strip_discogs_disambig(decoded_slug).strip()
        if not stripped:
            continue
        score = score_match(stripped, artist_stripped)
        scored.append(ScoredCandidate(url=url, lang=lang, score=score, denylisted=denylisted))
    return scored


def candidate_sort_key(candidate: ScoredCandidate) -> tuple[bool, float, bool, int, str]:
    """TRY-ORDER ranking key: non-denylisted before denylisted, then score
    desc, ``en`` breaks a tie, shorter URL breaks a remaining tie, the URL
    string is the final deterministic tiebreak (total order — LML#1192
    round 2, C1: two callers can see the same candidate set in different
    input orders). Shared by :func:`select_best` (picks rank 0) and
    ``lookup.wikipedia_pick_validation`` (round 4, P0-2 — needs the full
    ranked list to try candidates against a live fetch in order).

    LML#1192 review round 6, P2-2: ``not candidate.denylisted`` leads the
    tuple so a denylisted-but-admitted candidate (fetch-capable callers
    only — see :func:`score_candidates`) always sorts after every
    non-denylisted one, regardless of score — "sorts last but stays
    reachable." A no-op for the synchronous path, which never admits a
    denylisted candidate in the first place, so every candidate it ever
    ranks has ``denylisted=False`` and this leading element is constant.

    Round 3 found sorting the bare URL alone picks the qualified
    (frequently WRONG — a disambiguation/type page) URL 100% of a tie,
    since a bare slug always string-prefixes its qualified sibling. Round
    4 found "prefer shorter" is ALSO wrong standalone — verified live:
    ``Low``/``Sade``'s BARE titles are real disambiguation pages (the
    qualified url is correct); ``Sun Ra``/``Stereolab``/``Cat Power``'s
    qualified slugs don't exist as pages at all (the bare url is correct).
    No string rule can tell these apart — the deciding signal isn't in
    either URL. This key therefore only orders TRY order now; the final
    answer comes from validating each candidate against its fetched
    payload (``lookup.wikipedia_pick_validation.resolve_and_validate_pick``),
    falling through to the next-ranked candidate on rejection.
    """
    return (
        not candidate.denylisted,
        candidate.score,
        candidate.lang == "en",
        -len(candidate.url),
        candidate.url,
    )


def select_best(scored: list[ScoredCandidate]) -> ScoredCandidate | None:
    """The top-ranked candidate under :func:`candidate_sort_key` — a
    best-guess for the synchronous, no-fetch live path
    (``pick_artist_wikipedia_url``, which cannot afford a live Wikipedia
    call on ``/lookup``). Fetch-capable callers use
    ``lookup.wikipedia_pick_validation`` instead for the full ranked list.
    """
    return max(scored, key=candidate_sort_key, default=None)


def wikipedia_title_from_url(url: str) -> str | None:
    """Extract the decoded, space-form page title from a ``wikipedia.org``
    ``/wiki/`` URL — the form ``clients.wikipedia.WikipediaClient.get_summary``
    expects (it re-encodes internally, so the round trip is safe). ``None``
    when ``url`` isn't a recognizable wiki URL, or its slug is empty.

    Shared by ``lookup/enrichment/wikipedia_warm.py`` and
    ``scripts/warm_wikipedia_bios.py``, both of which need to turn a stored
    ``wikipedia_url`` back into a fetchable title.
    """
    match = _WIKI_URL_RE.match(url.strip())
    if not match:
        return None
    raw_slug = match.group(2).split("#", 1)[0].split("?", 1)[0]
    decoded = unquote(raw_slug).replace("_", " ").strip()
    return decoded or None
