"""The ARTIST_PLUS_ALBUM match class — one implementation, two callers (LML#1321).

"Does this candidate release answer the typed ``(artist, album)`` pair?" is
asked twice in the pipeline, and the two asks must admit exactly the same class:

* ``lookup/strategies/library_miss.py`` — the LML#583 step-3a probe, over the
  candidates ``DiscogsService.search`` returns (PG cache arm, or the API arm on
  a genuine miss).
* ``lookup/album_level_match.py`` — the LML#1318 album-level degrade, over the
  rows it reads from the same local release cache directly.

Before this module the second caller held a verbatim copy of the first's floor
call and self-titled swap, with a comment asserting the parity. A gate change
applied to one copy forked the degrade's match class from the path it claims
parity with, silently. The class is now this file, and
``tests/unit/test_typed_pair_floor_parity.py`` drives both callers over one
candidate table so a fork fails a test instead of shipping.

What the class is:

1. :func:`typed_album_axis` — the LML#784 category-4 self-titled swap, applied
   to the typed album before anything scores against it.
2. :func:`floor_best_typed_pair` — ``find_best_typed_match``'s joint 80/80 floor
   over artist AND album, widened on the candidate side by the LML#1206
   suffix-stripped artist variants, tie-broken by :func:`floor_tie_break_key`.

**Candidate ordering is immaterial, deliberately.** ``DiscogsService.search``'s
PG arm confidence-sorts its candidates before the floor sees them; the degrade
floors ``search_releases`` rows in SQL order and computes no confidence at all.
That is not a latent tie-break divergence: ``find_best_typed_match`` resolves an
exact score tie by ascending ``key_fn`` (never by arrival), and
:func:`floor_tie_break_key` returns ``(exact_credit_rank, release_id)`` over a
candidate set that ``search_releases`` produces with ``SELECT DISTINCT ON
(r.id)`` — so the keys are a total order and the winner is a property of the
set, not of its order. Ranking the degrade's rows would therefore change no
verdict while adding a ``calculate_confidence`` call per row to a degrade whose
whole point is to spend nothing on the critical path (LML#1112).

Two things would end that equivalence, and both are pinned by tests rather than
left to prose: dropping ``key_fn`` at either call site (ties fall back to input
order), or a candidate set with duplicate release ids (the key stops separating
them). ``TestCandidateOrderingIsImmaterial`` asserts permutation invariance and
key uniqueness; if either fails, ordering has become load-bearing and the
degrade has to rank like the service arm.
"""

from __future__ import annotations

from collections.abc import Iterable

from clients.streaming.matching import find_best_typed_match
from discogs.models import DiscogsSearchResult
from lookup.matching import (
    artist_variant_tie_break_key,
    artist_variants_with_stripped_suffix,
    is_self_titled,
)

#: The floor's secondary sort. Re-exported under a name that says what it is to
#: this class (and gives the parity test something to assert totality over).
floor_tie_break_key = artist_variant_tie_break_key


def typed_album_axis(artist: str, album: str) -> str:
    """The album axis to score against — the typed album, or the artist name.

    LML#784 category 4: a query-side self-titled placeholder ("S/T", "s.t.",
    "self-titled") can never match a real Discogs or cache title, so the artist
    name is swapped in for both the search and the title-axis scoring —
    mirroring the library-side swap in ``lookup/artwork.py`` and the catalog's
    own S/T filing form. The trigger string is NOT kept as an additional
    scoring variant: a wrong-release candidate literally titled "S/T" would
    clear the floor trivially.

    Caveat carried over from ``artist_variants_with_stripped_suffix``' docstring:
    on the swapped path the title axis becomes a copy of the artist name, so it
    confirms nothing the artist axis did not already decide. That collapse
    predates LML#1206; tracked at WXYC/library-metadata-lookup#1208.
    """
    return artist if is_self_titled(album) else album


def floor_best_typed_pair(
    candidates: Iterable[DiscogsSearchResult], *, artist: str, album: str
) -> DiscogsSearchResult | None:
    """The best candidate clearing the joint 80/80 floor on the typed pair, or None.

    ``album`` is expected to have been through :func:`typed_album_axis` already
    (the swap belongs at the point the caller also uses the axis to *search*,
    not just to score).

    Nothing wider than ARTIST_PLUS_ALBUM passes: the floor is applied to artist
    AND album jointly, so this admits neither the LML#400 artist-fallback
    contamination shape (any release for any album) nor a BS#1359 same-artist
    substitution. The candidate-side artist axis is widened with each raw
    variant's Discogs-disambiguation-suffix-stripped form (LML#1206) so a
    bare-name query reaches a ``"Mavi (12)"`` credit, and ties are broken by
    :func:`floor_tie_break_key` — exact raw credit ahead of a stripped-only
    match (LML#1206 review finding 2), then ascending release_id (LML#1097) —
    rather than by input order. See the module docstring on why that makes the
    callers' differing candidate orders equivalent.
    """
    return find_best_typed_match(
        candidates,
        query_artist=artist,
        query_title=album,
        artist_fn=artist_variants_with_stripped_suffix,
        title_fn=lambda r: r.album,
        key_fn=lambda r: floor_tie_break_key(artist, r),
    )
