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
"""

from __future__ import annotations

from urllib.parse import quote

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

    The Discogs disambiguation suffix is structural metadata rather than part
    of how an artist is billed, so it is stripped with the shared LML#1206
    helper -- ``Ear (11)`` searched literally returns nothing.

    ``row_artist`` keeps its own meaning untouched: the LML#504 identity gate
    and the probe call sites still score against it.
    """
    return strip_discogs_disambig(item.artist or item.alternate_artist_name or "")


def bandcamp_search_term(requested_album: str | None, item: LibraryItem) -> str:
    """The album title to search Bandcamp for.

    Prefers the requested album so this fallback and the LML#1098 live probe
    key on the same ``(artist, album)`` pair -- a later probe hit is then a
    like-for-like upgrade of the same query rather than a different question.
    Falls back to the catalog row's title so a free-form playcut with no
    parsed album still produces a query instead of dropping the field.
    """
    return requested_album or item.title or ""
