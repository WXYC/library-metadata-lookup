"""Wikipedia REST summary client (Phase B of the Wikipedia-preferred-artist-bio
program, ``docs/plans/lml-1192-wikipedia-artist-bio.md``).

Never on the request path (see the plan's Non-goals — no live Wikipedia
dependency in ``/lookup``): fetches happen only in the ``warm_cache``-gated
background task (``lookup/enrichment/wikipedia_bio.py``, PR-B2) and the
offline drain (``scripts/warm_wikipedia_bios.py``, PR-C). No circuit breaker
for the same reason every sibling breaker (``discogs/breaker.py``,
``clients/bandcamp_breaker.py``) exists to protect the request path this
client never touches.

``get_summary`` returns ``None`` for an AUTHORITATIVE negative — the page is
a disambiguation page, carries no extract, or its ``description`` field
names a non-artist qualifier the Phase-A slug denylist couldn't catch (an
unlisted qualifier like ``(mixtape)`` gets stripped and scores 100 against
the artist name, so the REST payload's own ``description`` field is the
closed-form backstop the open-vocabulary denylist can't be). Callers
negative-cache on ``None``. It RAISES :class:`WikipediaFetchError` for
anything that means "couldn't ask" — timeout, network error, a non-200/404/429
status, or exhausted 429 retries — and callers must never negative-cache on
that: a transient outage is not evidence the artist has no Wikipedia page.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from aiolimiter import AsyncLimiter
from wxyc_fastapi.http import async_singleton

logger = logging.getLogger(__name__)

USER_AGENT = "WXYCBioLookup/0.1 (https://wxyc.org; engineering@wxyc.org)"
"""Wikimedia-family convention (``entity/sources.py``'s ``SparqlSource``):
an org contact, never a personal address."""

_TOTAL_TIMEOUT_SECONDS = 4.0
_DEFAULT_RETRY_DELAY_SECONDS = 2.0
"""Fallback sleep on a 429 that carries no (or an unparseable) Retry-After."""

_SUMMARY_URL_TEMPLATE = "https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"

# The description-pattern reject (see module docstring): a non-artist page
# whose slug survived the Phase-A denylist. Matches Wikipedia's own
# machine-generated descriptions for album/EP/song/single/mixtape pages,
# e.g. "1975 studio album by Some Artist" or "song by Some Artist".
#
# LML#1192 review (round 2), finding B1-1: the original two patterns were
# English-only (a non-English Wikipedia edition's description sailed
# straight through) and "mixtape by" fell through both -- pattern 1
# required a leading year, and "mixtape" was absent from pattern 2's
# alternation entirely. Broadened to a language-agnostic release-noun
# vocabulary (the handful of European languages WXYC's Discogs-linked
# Wikipedia pages most commonly surface in) matched against a release
# PREPOSITION set, rather than one hand-built phrase shape per language --
# this is deliberately a wider net, not an exhaustive per-language grammar:
# it is a backstop behind the Phase-A slug denylist, not the primary
# signal, so trading a little precision for materially broader recall is
# the correct trade here. Python's ``re.IGNORECASE`` case-folds non-ASCII
# letters correctly (``Álbum`` matches ``álbum``), so no separate
# diacritic-stripping step is needed.
# PUBLIC (LML#1192 cross-PR review, round 7): this module owns the
# "work, not artist" vocabulary for BOTH rejection sites -- the slug
# denylist in ``lookup/wikipedia_candidates.py`` derives from the union of
# these two tuples, so a new word added here reaches both defenses at
# once. Through round 6 the two lists were hand-maintained separately and
# drifted within a single review cycle ("mixtape" was added to each by
# different findings), leaving film/soundtrack/TV/discography pages with
# no payload backstop at all after P2-2 demoted the slug denylist for
# fetch-capable callers.
RELEASE_NOUNS = (
    "album",
    "ep",
    "single",
    "song",
    "mixtape",
    "compilation",  # English
    "álbum",
    "sencillo",
    "canción",  # Spanish / Portuguese
    "disque",
    "chanson",  # French
    "studioalbum",
    "lied",  # German
    "disco",
    "canzone",  # Italian
)
"""Things credited TO an artist via a preposition ("album by X", "álbum de
X") -- the noun+preposition+capitalized-artist description grammar below."""

NON_ARTIST_PAGE_TYPES = (
    "film",
    "soundtrack",
    "discography",
    "tv series",
    "television series",
)
"""Page KINDS that are never an artist. Unlike release nouns these head
their descriptions without a crediting preposition ("1995 American crime
film directed by ...", "American television series"), so they get their own
head-position grammar below rather than joining the release-noun one."""

_RELEASE_PREPOSITIONS = ("by", "de", "von", "di", "par", "do", "da")
_RELEASE_NOUN_ALTERNATION = "|".join(re.escape(n) for n in RELEASE_NOUNS)
_RELEASE_PREPOSITION_ALTERNATION = "|".join(re.escape(p) for p in _RELEASE_PREPOSITIONS)
# LML#1192 review round 4, finding 9: pattern 2 used to match
# `\b(noun)\b\s+(prep)\s+` ANYWHERE in the description, not just at the
# start -- so an ordinary genre-descriptor phrase for a PERSON that
# happens to contain a release-noun followed by a preposition mid-sentence
# ("cantante de canción de autor española" -- Spanish for "Spanish
# singer-songwriter" -- "canción de autor" is the standard idiom for that
# genre, not "song by") false-positive-rejected, writing an authoritative
# extract=NULL for a real artist page. Verified live: every genuine
# release-page description across en/es/fr starts DIRECTLY with the
# release noun ("1997 studio album by Radiohead", "álbum de Radiohead",
# "album de Radiohead, sorti en 1997") -- so anchoring to the START of the
# description (narrowed, not reverted -- the broadened non-English
# vocabulary itself stays) keeps every true positive and drops the false
# ones. Anchored, not `\b`-prefixed: `^` already implies a word boundary.
#
# LML#1192 review round 6, P2-1: the anchor alone didn't close two further
# false-positive classes, both fixed by tightening what "a release noun"
# means here rather than by adding more excluded phrases (which would just
# be the next round's whack-a-mole):
#
# 1. Pattern 1's `.*` was UNBOUNDED between the year and the noun, and
#    matched the noun via a loose `\b...\b` with nothing required after
#    it. "disco" is in _RELEASE_NOUNS for its Spanish/Italian "record"
#    sense, but is also an ordinary English genre word -- "1977 British
#    disco band" / "1970 American disco group" both matched on that bare
#    `\bdisco\b`, even though "disco" here modifies "band"/"group", never
#    "album"/"song". A release noun only actually MEANS "this is a
#    release" when it's immediately followed by one of the prepositions
#    below ("disco album BY X", never "disco band"), so both patterns now
#    share that requirement via `_RELEASE_NOUN_PREP_ALTERNATION`.
# 2. Even a genuine noun+preposition match isn't enough on its own:
#    "Canción de autor española" (the standard Spanish genre label for
#    "singer-songwriter") opens with "canción de" exactly like a real
#    "canción de Sessa" would. What actually distinguishes them is what
#    comes next -- a real release-page description always names the
#    ARTIST after the preposition (capitalized: "by Radiohead", "de
#    Sessa"), never a lowercase common noun ("de autor"). Checked
#    case-SENSITIVELY (unlike the rest of this module) via
#    `_CAPITALIZED_WORD_RE`, searched from the noun+prep match's END so a
#    merely sentence-initial capital can't satisfy it by accident, and
#    over the whole REMAINDER (not just the next word) so
#    "álbum de estudio de Sessa" -- a real positive where the artist name
#    is one hop further out, past an infix noun -- still matches.
_RELEASE_NOUN_PREP_ALTERNATION = (
    rf"(?:{_RELEASE_NOUN_ALTERNATION})\b\s+(?:{_RELEASE_PREPOSITION_ALTERNATION})\b\s+"
)
_CAPITALIZED_WORD_RE = re.compile(r"[A-ZÀ-Þ]")  # deliberately NOT re.IGNORECASE
# LML#1192 cross-PR review, round 7: the page-TYPE grammar. A film/TV/
# discography/soundtrack description names its type as the phrase HEAD --
# start-anchored, at most a few modifier tokens deep ("1995 American crime
# film directed by ...", "American television series"), and followed by
# end-of-phrase or a WORKS continuation (directed/starring/of/by/...).
# The head-position + follower requirement is what keeps PEOPLE described
# via film/TV work out: "American film composer" and "composer of film and
# television scores" put another noun (or "and") right after the type
# word, which is not in the follower set. Longest-first alternation so
# "television series" can't be pre-empted by a shorter member.
_PAGE_TYPE_ALTERNATION = "|".join(
    re.escape(t) for t in sorted(NON_ARTIST_PAGE_TYPES, key=len, reverse=True)
)
_PAGE_TYPE_HEAD_RE = (
    rf"^(?:\d{{4}}\s+)?(?:[\w!'-]+\s+){{0,4}}(?:{_PAGE_TYPE_ALTERNATION})"
    rf"(?:\s*$|[,.]|\s+(?:directed|starring|by|based|about|of|written|produced|released|from)\b)"
)
_PAGE_TYPE_HEAD_PATTERN = re.compile(_PAGE_TYPE_HEAD_RE, re.IGNORECASE)
_DESCRIPTION_REJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^\d{{4}}\s+.*\b{_RELEASE_NOUN_PREP_ALTERNATION}", re.IGNORECASE),
    re.compile(rf"^{_RELEASE_NOUN_PREP_ALTERNATION}", re.IGNORECASE),
)


class WikipediaFetchError(Exception):
    """Transient "couldn't ask" failure — never a reason to negative-cache."""


@dataclass(frozen=True)
class WikipediaSummary:
    """The lead-paragraph extract for a positively-resolved artist page."""

    extract: str


def _is_rejected_description(description: str | None) -> bool:
    if not description:
        return False
    for pattern in _DESCRIPTION_REJECT_PATTERNS:
        match = pattern.search(description)
        if match and _CAPITALIZED_WORD_RE.search(description, match.end()):
            return True
    # The capitalized-word post-condition above belongs to the CREDITING
    # grammar (a real release description names its artist after the
    # preposition -- round 6, P2-1). A page-TYPE description needs no
    # artist at all ("American television series" simply ends), so the
    # head-position pattern stands on its own follower-set precision.
    return _PAGE_TYPE_HEAD_PATTERN.search(description) is not None


_MAX_RETRY_DELAY_SECONDS = 30.0
"""Hard ceiling on any single retry sleep, however parsed. LML#1192 review
(B1-2): ``float()`` happily accepts ``"inf"``/``"nan"`` and ``max(x, 0.0)``
clamps neither (``max(nan, 0.0)`` is itself undefined-by-comparison) --
``asyncio.sleep(inf)`` never wakes, permanently stranding one of the two
background-warm permits (or stalling the offline drain outright) on a
malicious or malformed upstream header. Every parsed value, finite or not,
is clamped into ``[0, _MAX_RETRY_DELAY_SECONDS]``."""


def _parse_retry_after(response: httpx.Response) -> float:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return _DEFAULT_RETRY_DELAY_SECONDS
    try:
        parsed = float(raw)
    except ValueError:
        # Retry-After may also be an HTTP-date; this client doesn't parse
        # that form (Wikipedia's REST API sends the delta-seconds form in
        # practice) — fall back to the default delay rather than raise.
        return _DEFAULT_RETRY_DELAY_SECONDS
    if not math.isfinite(parsed):
        # inf/-inf/nan: never trust an upstream header enough to sleep on
        # it directly. Falls back to the same default a malformed value
        # gets, rather than clamping to the ceiling — an upstream sending
        # "inf" is a malformed signal, not a "wait as long as possible" one.
        return _DEFAULT_RETRY_DELAY_SECONDS
    return min(max(parsed, 0.0), _MAX_RETRY_DELAY_SECONDS)


class WikipediaClient:
    """Thin wrapper over the Wikipedia REST ``/page/summary`` endpoint.

    LML#1192 review round 6, pass 3, A5: through this round, a fresh
    ``httpx.AsyncClient`` was opened on EVERY ``get_summary`` call
    (mirroring ``entity/sources.py``'s ``SparqlSource``) -- but unlike that
    module's genuinely one-shot SPARQL queries, both real callers hold one
    ``WikipediaClient`` instance for their whole scope and call
    ``get_summary`` repeatedly on it (the background warm, up to
    ``MAX_CANDIDATES_PER_WARM`` times per warm; the offline drain, once per
    artist across its entire session) -- so the per-call client was pure
    avoidable overhead: measured 3.45ms of event-loop-blocking CPU per
    fetch, ~30k avoidable TLS handshakes across a full drain. Now a
    lazily-built, per-INSTANCE ``async_singleton``
    (``wxyc_fastapi.http.async_singleton``, the LML#241/#242 FD-leak-race
    fix -- mirrors ``clients/streaming/base.py``'s identical adoption),
    reused across every ``get_summary`` call on this instance and released
    via :meth:`aclose`.
    """

    def __init__(self) -> None:
        self._singleton_get, self._singleton_close = async_singleton(self._make_client)

    async def _make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=_TOTAL_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    async def _get_client(self) -> httpx.AsyncClient:
        return await self._singleton_get()

    async def aclose(self) -> None:
        """Release the underlying connection. Safe to call even if no
        request was ever made (the singleton was never built)."""
        await self._singleton_close()

    async def get_summary(
        self,
        title: str,
        lang: str,
        *,
        max_retries: int = 1,
        rate_limiter: AsyncLimiter | None = None,
    ) -> WikipediaSummary | None:
        """Fetch the lead-paragraph summary for ``title`` on ``{lang}.wikipedia.org``.

        ``max_retries`` bounds the number of RETRY attempts on a 429
        response (total attempts = ``max_retries + 1``); each retry honors
        the response's ``Retry-After`` header (falling back to a fixed
        delay when absent or unparseable). Callers choose the policy: the
        background miss-warm passes a single retry then treats an
        exhausted 429 as a shed (this function raising is exactly that
        signal).

        ``rate_limiter`` (LML#1192 review round 2, C4), when given, is
        acquired immediately before EVERY actual HTTP attempt — the initial
        request AND each retry. This must live here, not in an outer
        wrapper around the whole call: a caller-side "one slot per call"
        throttle (the offline drain's original design) still lets a single
        429-heavy candidate fire up to ``max_retries + 1`` requests inside
        one slot, bursting past the configured rate exactly when 429
        pressure is highest. The background miss-warm doesn't pass one —
        its own concurrency cap (``lookup/enrichment/wikipedia_warm.py``)
        is a different, coarser throttle appropriate for its much lower
        volume.
        """
        url = _SUMMARY_URL_TEMPLATE.format(lang=lang, title=quote(title, safe=""))
        client = await self._get_client()
        attempt = 0
        while True:
            if rate_limiter is not None:
                await rate_limiter.acquire()
            try:
                response = await client.get(url)
            except httpx.HTTPError as exc:
                raise WikipediaFetchError(f"Wikipedia summary fetch failed: {exc}") from exc

            if response.status_code == 429:
                if attempt >= max_retries:
                    raise WikipediaFetchError(
                        f"Wikipedia summary fetch rate-limited after {attempt + 1} attempt(s)"
                    )
                delay = _parse_retry_after(response)
                logger.info(
                    "Wikipedia summary fetch 429 for %s (%s); retrying in %.1fs",
                    title,
                    lang,
                    delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if response.status_code == 404:
                return None

            if response.status_code != 200:
                raise WikipediaFetchError(
                    f"Wikipedia summary fetch returned HTTP {response.status_code}"
                )

            # LML#1192 review (B1-3): a malformed/non-JSON 200 body
            # (an upstream error page misrouted through a proxy, a
            # truncated response, ...) previously raised a raw
            # json.JSONDecodeError -- or, for well-formed-but-wrong-
            # shaped JSON (a bare list instead of an object), a raw
            # AttributeError out of _parse_summary's dict access --
            # neither of which is WikipediaFetchError, so both escaped
            # every caller's `except WikipediaFetchError` handling.
            # ``json.JSONDecodeError`` is a ``ValueError`` subclass.
            try:
                payload = response.json()
            except ValueError as exc:
                raise WikipediaFetchError(
                    f"Wikipedia summary fetch returned a non-JSON body: {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise WikipediaFetchError(
                    "Wikipedia summary fetch returned a non-object JSON body "
                    f"({type(payload).__name__})"
                )
            return _parse_summary(payload)


def _parse_summary(payload: dict) -> WikipediaSummary | None:
    if payload.get("type") != "standard":
        return None
    extract = payload.get("extract")
    if not isinstance(extract, str) or not extract.strip():
        return None
    if _is_rejected_description(payload.get("description")):
        return None
    return WikipediaSummary(extract=extract)
