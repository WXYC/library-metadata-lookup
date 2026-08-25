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
  ``item.alternate_artist_name or item.artist``, and in this catalog
  ``alternate_artist_name`` carries the Discogs canonical/legal name
  (``Bryan Muller`` for skee mask, ``Claire Cottril`` for Clairo) along with
  its disambiguation suffix (``Ear (11)``, ``Mia (106)``). Every streaming
  service indexes the PERFORMING name.
* **Bandcamp's term was the played track.** Bandcamp deep-links are
  album-level -- the same property that makes the LML#1098 live probe gate on
  a resolved ``ctx.album``. Worse, Backend-Service persists the result on an
  album-keyed ``album_metadata`` row and never re-asks
  (WXYC/Backend-Service#1747), so whichever track first triggered enrichment
  froze its name onto every later play of that album.

Measured over the 100 most recent playcuts (2026-08-24): of the 87 rows
carrying a Bandcamp search fallback, 16 had the artist + album shape; 0 of 6
sampled shipped queries surfaced the album against Bandcamp's search, three
returning a literally empty page. The corrected shape surfaced the album for
every sampled release Bandcamp actually carries.

YouTube Music and SoundCloud stay **track**-scoped: both are track-searchable,
so only the artist half of the fix applies to them.

Review of that fix then found three places it overshot, each corrected here
and each pinned by ``TestSearchUrlFallbackQueryShapeReviewFixes``:

* **The strip could empty the name**, and an empty artist disables all three
  fallbacks rather than just degrading one.
* **A compilation row's filed artist is a shelf heading**, so preferring it
  over the alternate is right everywhere except the one class where the
  alternate holds the performer.
* **The album was request-scoped against a per-row fallback**, so a non-top-1
  row advertised an album it isn't.

Each function below carries the measurement for its own case.
"""

from __future__ import annotations

from urllib.parse import quote

from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import strip_discogs_disambig
from library.models import LibraryItem


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
    2026-08-23 prod ``library.db`` carry the actual performer in
    ``alternate_artist_name`` (``mulatu astatke``, ``Belle & Sebastian``), and
    ``Various Artists - Africa Ethiopiques 4`` finds nothing on any service
    while ``mulatu astatke Ethiopiques 4`` finds the record. This is the one
    class where the pre-LML#1284 ``alternate or artist`` order was right, so
    it is restored for exactly that class. ``lookup/artwork.py``'s
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
    this fallback but disables all three: ``enrich_one`` guards the inline
    YouTube-Music/SoundCloud pair and, separately, the deferred Bandcamp
    write on the same ``if search_artist and ...``. A name the stripper
    cannot improve falls back to itself.

    Note for future call sites: unlike ``strip_discogs_disambig``'s other
    consumers -- which use the output for in-request fuzzy scoring and a
    process-local cache, per its own docstring -- this output is
    consumer-facing and Backend-Service freezes it onto an album-keyed
    ``album_metadata`` row (BS#1747). A bad strip here is durable.

    ``row_artist`` keeps its own meaning untouched: the LML#504 identity gate
    and the probe call sites still score against it.
    """
    filed = item.artist or ""
    alternate = item.alternate_artist_name or ""
    raw = (alternate or filed) if is_compilation_artist(filed) else (filed or alternate)
    return strip_discogs_disambig(raw) or raw


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
    requested album mislabels them outright. Backend-Service then freezes that
    pairing onto the row's own album-keyed ``album_metadata`` and never
    re-asks (BS#1747), so the mislabel is durable. ``enrich_one`` passes
    LML#477's ``row_title_matches_requested_album`` -- the same predicate that
    decides whether to trust the row's Discogs binding, asked here of the
    query text; it is ``True`` when no album was requested, which is what
    keeps the row-title fallback above reachable.

    A CLOSED gate returns the row title or nothing -- never the requested
    album by another route. The branches are written so that reaching the
    requested album is impossible once the gate is closed, rather than merely
    unlikely: the earlier ``item.title or requested_album or ""`` chain handed
    the requested album back to a titleless row, and was inert only because
    ``compute_row_title_matches_requested_album`` happens to return ``True``
    whenever ``item.title`` is falsy. That is an invariant living in a
    different module, which is precisely the arrangement the required
    parameter above exists to avoid.
    """
    if not requested_album_describes_row:
        return item.title or ""
    return requested_album or item.title or ""
