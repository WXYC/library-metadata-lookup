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
_RELEASE_NOUNS = (
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
_RELEASE_PREPOSITIONS = ("by", "de", "von", "di", "par", "do", "da")
_RELEASE_NOUN_ALTERNATION = "|".join(re.escape(n) for n in _RELEASE_NOUNS)
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
_DESCRIPTION_REJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^\d{{4}}\s+.*\b(?:{_RELEASE_NOUN_ALTERNATION})\b", re.IGNORECASE),
    re.compile(
        rf"^(?:{_RELEASE_NOUN_ALTERNATION})\b\s+(?:{_RELEASE_PREPOSITION_ALTERNATION})\s+",
        re.IGNORECASE,
    ),
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
    return any(pattern.search(description) for pattern in _DESCRIPTION_REJECT_PATTERNS)


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

    A fresh ``httpx.AsyncClient`` is opened per call (mirrors
    ``entity/sources.py``'s ``SparqlSource`` — this is a low-frequency,
    background-only fetch, not a hot-path client that would want a shared
    connection pool).
    """

    async def get_summary(
        self, title: str, lang: str, *, max_retries: int = 1
    ) -> WikipediaSummary | None:
        """Fetch the lead-paragraph summary for ``title`` on ``{lang}.wikipedia.org``.

        ``max_retries`` bounds the number of RETRY attempts on a 429
        response (total attempts = ``max_retries + 1``); each retry honors
        the response's ``Retry-After`` header (falling back to a fixed
        delay when absent or unparseable). Callers choose the policy: the
        background miss-warm passes a single retry then treats an
        exhausted 429 as a shed (this function raising is exactly that
        signal); the offline drain wraps calls to this method in its own
        outer sleep-and-retry loop across the whole batch throttle.
        """
        url = _SUMMARY_URL_TEMPLATE.format(lang=lang, title=quote(title, safe=""))
        async with httpx.AsyncClient(
            timeout=_TOTAL_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            attempt = 0
            while True:
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
