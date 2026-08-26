"""Templated streaming search-URL fallbacks for the enrichment path.

Extracted from ``enrich_one`` (``lookup/enrichment/item.py``) to keep that
file under its module-size budget (``tests/unit/test_module_budgets.py``) --
the same extraction posture ``streaming_status.py`` (LML#1053),
``bandcamp_probe.py`` (LML#1098) and ``apple_probe.py`` (LML#1101) took. These
are pure functions of the request scalars and the library row, with no
dependency on ``enrich_one``'s control flow.

**These URLs are the LAST tier of the resolution ladder** (see #1100's ladder:
librarian override > PG cache read-fill > inline probe > background warm >
offline drain > *this*). A search URL is never a verified link, and
``_RESOLUTION_PROVING_URL_SERVICES`` in ``item.py`` exists precisely so one is
never reported as one. What this module owes its caller is that when a
listener does land on a search page, the page contains the record.

LML#1284 fixed two ways it didn't:

* **The artist was the wrong name.** The fallbacks were built from
  ``item.alternate_artist_name or item.artist``, and every streaming service
  indexes the PERFORMING name -- which is ``item.artist``, the name the record
  is filed under. What the alternate holds is the pair's other half: how THIS
  release is billed. Sampled over the 4,757 non-compilation rows carrying one
  in the 2026-08-23 prod ``library.db``, that is usually a collaboration or
  cross-reference credit (``Marvin Gaye`` -> ``Marvin Gaye and Tammi Terrell``,
  ``TSVI`` -> ``TSVI & Loraine James``, ``Harold Budd`` ->
  ``Andy Partridge / Harold Budd``), sometimes a spelling variant
  (``B.J. Thomas`` -> ``bj thomas``), and sometimes the Discogs canonical or
  legal name with its disambiguation suffix (``Bryan Muller`` for skee mask,
  ``Claire Cottril`` for Clairo, ``Ear (11)``, ``Mia (106)``) -- the class
  that made this defect visible, but a minority of the field, not its
  definition. Every one of those shapes names something other than, or more
  than, the performer a listener is searching for.
* **Bandcamp's term was the played track.** Bandcamp deep-links are
  album-level -- the same property that makes the LML#1098 live probe gate on
  a resolved ``ctx.album``. Worse, whichever track first triggered enrichment
  froze its name onto every later play of that album, for the reason below.

**Why a bad value here is durable.** Backend-Service stops asking LML about an
album once its ``album_metadata`` row carries a non-null ``artwork_url`` /
``discogs_url`` (WXYC/Backend-Service#1747), so a streaming URL -- not itself
load-bearing -- is written once and read for the life of the album. Bandcamp
can still self-heal through BS#1915's bounded re-ask; **YouTube Music and
SoundCloud cannot**, having no status column to re-open on. The full rule, its
exceptions, and what they cost this module live in ``docs/architecture.md``'s
"What Backend-Service does with what LML returns".

Measured over the 100 most recent playcuts (2026-08-24): of the 87 rows
carrying a Bandcamp search fallback, 16 had the artist + album shape; 0 of 6
sampled shipped queries surfaced the album against Bandcamp's search, three
returning a literally empty page. The corrected shape surfaced the album for
every sampled release Bandcamp actually carries.

YouTube Music and SoundCloud stay **track**-scoped: both are track-searchable,
so only the artist half of the fix applies to them.

Review of that fix then found three places it overshot -- the strip could
empty a name, a compilation row's filed artist is a shelf heading, and the
album was request-scoped against a per-row fallback. All three are corrected
here, pinned by ``TestSearchUrlFallbackQueryShapeReviewFixes``, and each
function below carries the measurement for its own case.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from clients.streaming.matching import strip_discogs_disambig_preserving
from library.models import LibraryItem
from lookup.artist_resolution import _is_compilation_alias


def build_streaming_search_url(base: str, artist: str, term: str) -> str:
    """Build a streaming service search URL from artist + song/album."""
    query = f"{artist} {term}" if term else artist
    return f"{base}{quote(query)}"


def search_artist_for(item: LibraryItem) -> str:
    """The artist name to search a streaming service for.

    Deliberately NOT ``enrich_one``'s ``row_artist``, which leads with
    ``alternate_artist_name`` -- see the module docstring. ``item.artist`` is
    the filed (performing) name and is effectively always present
    (``alternate_artist_name`` is ``''`` on 59,816 of 64,676 catalog rows), but
    the alternate is kept as a second preference rather than dropped, so a row
    carrying only one of the two still yields a query.

    **A compilation row inverts that order**, because there ``item.artist`` is
    a SHELF HEADING rather than a performer -- ``Various Artists - Africa``,
    ``Soundtracks - G``, ``various``. 126 rows across 37 such headings in the
    2026-08-23 prod ``library.db`` carry a release-level credit in
    ``alternate_artist_name``, and ``Various Artists - Africa Ethiopiques 4``
    finds nothing on any service while ``mulatu astatke Ethiopiques 4`` finds
    the record. This is the one class where the pre-LML#1284
    ``alternate or artist`` order was right, so it is restored for exactly
    that class. It is restored on a WEAKER guarantee than the shelf heading it
    replaces is bad: the credit is the performer on most of those rows
    (``mulatu astatke``, ``Belle & Sebastian``, ``Ry Cooder``) but on some is a
    subtitle or an instrument list (``Ethiopian Groove: The Golden Seventies``
    on a row titled ``Ethiopiques 13``, ``Noh-Biwa-Shakuhachi``). That is
    still the right trade only because the alternative never works at all --
    no shelf heading is searchable -- so the inversion can degrade the query
    but cannot regress it. Exactly one of the 126 has a heading on BOTH sides
    (id 60381, ``Soundtracks - D`` / ``Various Artists``), which is a wash. ``lookup/artwork.py``'s
    ``track_artist`` reads the alternate as the same thing -- its comment
    calls it "the *track* artist (pre-compilation-form mutation)" -- though
    it leads with the alternate unconditionally and then swaps a compilation
    credit for ``COMPILATION_ARTIST_SEARCH_FORM``, because it is probing
    Discogs rather than labelling a consumer-facing search URL.

    The Discogs disambiguation suffix is structural metadata rather than part
    of how an artist is billed, so it is stripped with the shared LML#1206
    helper -- ``Ear (11)`` searched literally returns nothing. The strip is
    **never allowed to empty the name**: on ``(etre)`` (a real row -- id
    58112, the only one of 64,737 the broad strip empties) the whole name
    parses as a disambiguator, and an empty result does not merely degrade
    one fallback but disables all three: ``enrich_one`` guards SoundCloud's
    inline write on ``if search_artist and ...``, and
    ``apply_deferred_search_url_fallbacks`` below returns early on the same
    condition for Bandcamp and YouTube Music. A name the stripper cannot
    improve falls back to itself.

    **Emptying is not the only way the broad strip over-reaches**, and the
    empty guard above does not cover the rest. ``item.artist`` is cataloger
    free text, not a Discogs-structured field, so a parenthetical at the end
    of it is sometimes part of the billed name: the broad mode takes
    ``Was (Not Was)`` -> ``Was`` (9 rows), ``Add N to (X)`` -> ``Add N to``
    (3) and ``World (Of Dreams)`` -> ``World`` (1). Those 13 do not go empty
    -- they go generic, which is worse than it sounds, because the remnant
    then reads as part of the album phrase (row 41160 ships
    ``Was Born to Laugh at Tornadoes``, a query naming no artist at all).
    Accepted rather than fixed: over the same snapshot the broad mode alters
    143 rows, and the other 130 are cataloger disambiguators it should take
    (``Bill Evans (the late)``, ``Kaleidoscope (NYC hardcore)``,
    ``The Thing (jazz)``). The narrow (numeric-only) mode that would spare
    the 13 alters only 6 rows in total, so it gives back all 130 to save
    them. Splitting the two needs a name-shaped signal the crate primitive
    does not expose, so the fix belongs in ``wxyc-etl``, not in a
    per-call-site heuristic here.

    Both the compilation test and the strip go through the wrapped entry
    points rather than the raw crate primitives:
    ``_is_compilation_alias`` normalizes first, per the NORMALIZATION CONTRACT
    on ``clients/streaming/matching.py``'s ``artist_pair_is_compilation`` (the
    raw predicate is leading-anchored, so ``Vàrious Artists``, doubled
    whitespace and a wrapped ``(V/A)`` all read as real artist names -- the
    LML#1252 hole), and ``strip_discogs_disambig_preserving`` is where the
    never-empty rule above actually lives, shared with the one other call site
    that needs it.

    ``row_artist`` keeps its own meaning untouched: the LML#504 identity gate
    and the probe call sites still score against it.
    """
    filed = item.artist or ""
    alternate = item.alternate_artist_name or ""
    raw = (alternate or filed) if _is_compilation_alias(filed) else (filed or alternate)
    return strip_discogs_disambig_preserving(raw)


def bandcamp_search_term(
    requested_album: str | None,
    item: LibraryItem,
    *,
    requested_album_describes_row: bool,
) -> str:
    """The album title to search Bandcamp for.

    Prefers the requested album so this fallback keys on the same ALBUM the
    LML#1098 live probe does -- a later probe hit is then a like-for-like
    upgrade rather than an answer to a different question. Only the album half
    is shared: the probe scores the REQUEST artist (``ctx.artist``) while this
    pairs the ROW's artist, so on a row surfaced by title alone the two name
    different performers. That is deliberate -- a search URL advertises the
    row it is attached to, and the row's own artist is the honest label for
    it. Falls back to the catalog row's title so a free-form playcut with no
    parsed album still produces a query instead of dropping the field.

    ``requested_album_describes_row`` is required rather than defaulted
    because getting it wrong is silent: ``ctx.album`` is REQUEST-scoped while
    this fallback runs once per RESULT ROW, so on a multi-row response the
    non-top-1 rows are different albums and pairing their artist with the
    requested album mislabels them outright. Backend-Service then persists that
    pairing on the row's own album-keyed ``album_metadata`` and stops asking
    LML about the album once its artwork resolves (BS#1747 -- see the module
    docstring for the exact gate and its bounded exceptions), so the mislabel
    is durable rather than per-request. ``enrich_one`` passes
    LML#477's ``row_title_matches_requested_album`` -- the same predicate that
    decides whether to trust the row's Discogs binding, asked here of the
    query text; it is ``True`` when no album was requested, which is what
    keeps the row-title fallback above reachable.

    A CLOSED gate returns the row title or nothing -- never the requested
    album by another route. ``requested_album`` appears exactly once below,
    inside the branch the gate guards, so that is readable off the shape
    rather than inferred: an earlier ``item.title or requested_album or ""``
    fallback handed the requested album back to a titleless row, and was inert
    only because ``compute_row_title_matches_requested_album`` happens to
    return ``True`` whenever ``item.title`` is falsy -- an invariant living in
    a different module, which is precisely the arrangement the required
    parameter above exists to avoid.
    """
    if requested_album_describes_row and requested_album:
        return requested_album
    return item.title or ""


def apply_deferred_search_url_fallbacks(
    update: dict[str, Any],
    *,
    search_artist: str,
    search_term: str,
    bandcamp_term: str,
) -> None:
    """Fill the DEFERRED search-URL slots, in place, for services still empty.

    Bandcamp (LML#573 PR-3) and YouTube Music (LML#1103) both defer their
    templated fallback until after ``apply_streaming_url_postprocess`` and
    ``resolve_streaming_status`` have run. Two things depend on that ordering,
    and both fail silently rather than loudly if it regresses:

    * The post-process's active-filter only fires for a service whose URL field
      is ``None``, so a pre-filled search URL would disable that service's
      cache/warm leg entirely -- a resolved album page could never win its own
      slot.
    * ``_RESOLUTION_PROVING_URL_SERVICES`` reads these slots to decide which
      may report ``verified``. A search URL present at that point would be
      reported as a confirmed streaming match.

    Hence one function for both services: the deferral is a shared invariant,
    not two coincidentally similar lines, and the next service to earn a cache
    tier should join here rather than grow a third copy. SoundCloud is
    deliberately absent -- it has no cache tier, so its fallback still applies
    inline in ``enrich_one`` and it stays out of the resolution-proving set.

    Scoping differs by service and is the LML#1284 result: Bandcamp is
    album-keyed (``bandcamp_term``), YouTube Music is track-searchable
    (``search_term``). Both use the performing artist.
    """
    if not search_artist:
        return
    if bandcamp_term and not update["bandcamp_url"]:
        update["bandcamp_url"] = build_streaming_search_url(
            "https://bandcamp.com/search?q=", search_artist, bandcamp_term
        )
    if search_term and not update["youtube_music_url"]:
        update["youtube_music_url"] = build_streaming_search_url(
            "https://music.youtube.com/search?q=", search_artist, search_term
        )
