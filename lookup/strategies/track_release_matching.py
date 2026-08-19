"""Shared track-release-matching kernel for the Discogs-touching strategies.

Home of ``_match_track_releases_to_library`` — the release→library matcher
behind SONG_AS_TRACK and SWAPPED_INTERPRETATION (see the kernel note in
``docs/architecture.md``'s strategy-table section): Discogs track search →
:func:`search_album_fuzzy` → deferred per-release tracklist validation,
bounded by ``_chunked_gather`` + a ``MAX_SEARCH_RESULTS`` early-exit
(LML#536), with the LML#628 row-less A1 carry-through when the library walk
surfaces no row. ``search_album_fuzzy`` (the Discogs-title → library-row
fuzzy matcher) and its confidence constant ``SONG_AS_TRACK_CONFIDENCE`` live
here with their sole strategy-side consumers; ``search_album_fuzzy`` is also
called by ``search_compilations_for_track`` and the cached-track safety net,
which remain in ``lookup/orchestrator.py`` until later decomposition PRs.
Extracted verbatim from ``lookup/orchestrator.py`` (LML#726).
"""

import logging
import re

from wxyc_etl.text import is_compilation_artist

from config.settings import get_settings
from discogs.models import ReleaseInfo
from discogs.service import DiscogsService
from entity.sources import PgSource
from generated.api_models import TrackMatchHint, TrackMatchSource
from library.db import STOPWORDS, LibraryDB
from library.models import LibraryItem
from lookup.concurrency import _chunked_gather
from lookup.matching import (
    _TRAILING_PARENTHETICAL_RE,
    MAX_SEARCH_RESULTS,
    _release_matches_library_row,
    _va_series_title_match,
    album_title_acceptable,
    artist_matches_item,
)
from lookup.release_resolution import ResolvedRelease, validate_release_for_track
from lookup.rowless import (
    ROWLESS_LIBRARY_ID,
    _make_rowless_item,
    _recover_track_credit,
    _resolve_nonlibrary_release,
)

logger = logging.getLogger(__name__)


SONG_AS_TRACK_CONFIDENCE: float = 0.85
"""Default confidence floor for SONG_AS_TRACK matches.

Pinned at the master-cap value from catalog-track-search plan §5.2. The
underlying ``search_releases_by_track`` cache path doesn't currently distinguish
release- vs master-level matches, so we conservatively report the master cap.
When LML graduates onto ``library_identity`` per cross-cache-identity (#25),
this floor is replaced with ``library_identity.confidence`` per row.
"""


async def _match_track_releases_to_library(
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    track: str | None,
    *,
    artist: str | None,
    source: str,
    require_artist: str | None = None,
    album: str | None = None,
    pg: PgSource | None = None,
    allow_release_resolution_fallback: bool = True,
) -> tuple[list[LibraryItem], dict[int, list[TrackMatchHint]], dict[int, ResolvedRelease]]:
    """Find Discogs releases containing ``track`` and match them back to the library.

    The shared kernel behind two strategies:

    - **SONG_AS_TRACK** (``artist=None``, ``require_artist=None``) — song-only:
      any artist's release carrying the track is a valid hit, including VA
      compilations (the ``Various <album>`` re-query + the compilation arm of
      :func:`_release_matches_library_row`).
    - **SWAPPED_INTERPRETATION** (``artist=<identified>``,
      ``require_artist=<identified>``) — the query already resolved one side to
      an artist, so the result must stay scoped to *that* artist's own releases.
      ``require_artist`` re-filters surviving rows to the identified artist,
      which closes the precision hole where ``search_releases_by_track``'s
      under-3-results keyword supplement (``discogs/service.py``) returns
      other-artists' releases. VA-filed rows are out of scope on this path by
      design — SONG_AS_TRACK is the compilation path.

    Mechanics shared by both: find Discogs releases carrying the track,
    fuzzy-match each to the WXYC library, defer per-release tracklist validation
    until after a library hit so we only pay the API cost for rows we'd actually
    surface, and bound the per-request validate fan-out through
    :func:`_chunked_gather` with a ``MAX_SEARCH_RESULTS`` early-exit (LML#536).
    Each surviving row carries a ``TrackMatchHint``; ``source`` labels the
    validation breadcrumb (LML#344) and the log lines.

    **A1 carry-through (LML#628):** when the library walk surfaces *no* row and
    ``lml_resolve_nonlibrary_release`` is on, the kernel resolves + validates the
    release the track sits on (via :func:`_resolve_nonlibrary_release`, bounded +
    #632-cached) and emits a single **row-less** ``LibraryItem(id=0)`` carrying
    that :class:`ResolvedRelease` on the third tuple element (the
    ``discogs_titles`` seam ``fetch_artwork_for_items`` binds). This **bypasses
    the ``require_artist`` library re-filter** — there is no library row to
    filter, and the artist is already settled by the typed-artist
    ``search_releases_by_track`` probe + the per-track ``validate_track_on_release``
    credit check. The #632 cache is keyed on the typed anchor artist (the kernel's
    ``artist`` for SWAPPED; the surfaced release's own credit for SONG_AS_TRACK,
    which has no typed artist). **LML#649:** when that SONG_AS_TRACK fallback
    credit is a compilation marker ("Various" for a V/A comp), it is not a usable
    per-track artist — the carry-through is suppressed entirely rather than
    resolved/validated/cached under it (which would validate only against the
    release-level "Various" credit and collide same-titled tracks across comps on
    the ``("various", <title>, True)`` cache key).

    Returns:
        Tuple of (library_items, matched_via_by_id, discogs_titles).
        matched_via_by_id maps each library row's id to one-or-more
        TrackMatchHint entries — multiple hints accumulate when the same WXYC row
        is referenced by multiple Discogs releases (different pressings, etc.).
        discogs_titles is empty on the in-library path; on the row-less
        carry-through it carries ``{0: ResolvedRelease}``. ``([], {}, {})`` when
        there's no service, no track, or nothing cross-references.
    """
    if not discogs_service or not track or not track.strip():
        return [], {}, {}

    label = source.upper()
    response = await discogs_service.search_releases_by_track(track, artist=artist)
    raw_releases = list(response.releases or [])
    if not raw_releases:
        logger.info(f"{label}: no Discogs releases for '{track}'")
        return [], {}, {}

    logger.info(
        f"{label}: {len(raw_releases)} Discogs releases for '{track}', matching against library"
    )

    seen_ids: set[int] = set()
    matched_items: list[LibraryItem] = []
    matched_via_by_id: dict[int, list[TrackMatchHint]] = {}

    async def _validate_one(release: ReleaseInfo) -> list[LibraryItem] | None:
        """Library-match + validate one Discogs release.

        Returns the list of eligible library rows for this release when the
        track is validated on it, or None when the release should be dropped
        (too-short album title, no library hits, no eligible rows, or
        validation rejected). Order-preserving dedup happens in the caller's
        post-gather walk; this helper is order-agnostic.

        The ``validate_release_for_track`` call below passes ``release.artist``
        for both strategies, which leaves the title match inside
        ``scan_tracklist_for_match`` as what actually gates a release here; see
        the comment at that call site for why the artist leg does almost no
        discriminating work on this path.
        """
        if not release.album or len(release.album.strip()) < 3:
            return None

        matches = await search_album_fuzzy(db, release.album)
        if not matches and release.is_compilation:
            matches = await search_album_fuzzy(db, f"Various {release.album}")
        if not matches:
            return None

        eligible = [m for m in matches if _release_matches_library_row(release, m)]
        # SWAPPED scopes to the identified artist: drop rows that don't match it,
        # guarding against the keyword-supplement leaking other-artists' releases.
        if require_artist is not None:
            eligible = [m for m in eligible if artist_matches_item(m, require_artist)]
        if not eligible:
            return None

        # Validate the track actually appears on this release before surfacing
        # — Discogs's release-search index is keyword-driven and returns hits
        # that don't always contain the track on the tracklist. Deferred until
        # after library matching so we only pay the API cost for releases we'd
        # actually return, mirroring search_compilations_for_track.
        #
        # LML#1225: the artist argument here is `release.artist` — the
        # release's OWN credit — for BOTH SONG_AS_TRACK (which has no typed
        # artist to pass; it is song-only by definition) and
        # SWAPPED_INTERPRETATION (which HAS an identified artist, but this
        # call passes `release.artist` anyway). So the kernel's release-level
        # artist step compares this release's search-index credit against the
        # same release's detail credit, and in the overwhelming majority of
        # cases those agree — the step is very nearly always true and does
        # almost no discriminating work here. Validation at this call site is
        # therefore carried by the title gate; SWAPPED's real artist check
        # already happened above, via `require_artist` filtering `eligible`.
        #
        # Nearly-always-true is NOT tautological, and the difference matters
        # before anyone "simplifies" this away: the two strings come from two
        # different Discogs endpoints. `release.artist` is parsed out of the
        # search index's "Artist - Album" display string
        # (`discogs/service.py::_parse_title`), while `release_artist` inside
        # the kernel is the detail endpoint's first artist credit on the API
        # path, or an unordered `SELECT artist_name ... LIMIT 1` on the cache
        # path. For a multi-credit release the display string is the joined
        # credit while the detail/cache string is one member, so they can and
        # do diverge — and diverge *differently* between the two paths.
        # Dropping the step or passing a constant would remove a real (if
        # weak) check and reintroduce the API-vs-cache verdict divergence the
        # LML#1035 kernel extraction exists to prevent.
        #
        # Do not read this call as a strong second validation factor.
        if release.release_id:
            is_valid = await validate_release_for_track(
                discogs_service,
                release.release_id,
                track,
                release.artist,
                source=source,
            )
            if not is_valid:
                logger.debug(
                    f"{label}: skipping '{release.album}' — track not validated on release"
                )
                return None

        return eligible

    # Walk releases in input order through ``_chunked_gather``. Order
    # preservation comes from the iteration order of the generator itself
    # (chunks are dispatched and yielded in input order); accumulating
    # into ``matched_items`` / ``matched_via_by_id`` in that same order
    # guarantees a later release's hint never gets recorded before an
    # earlier release's. Breaking out of the loop after the response cap
    # is reached aborts the generator before un-fired chunks dispatch —
    # the per-request validate budget stays bounded (LML#536).
    done = False
    async for release, eligible in _chunked_gather(raw_releases, _validate_one, MAX_SEARCH_RESULTS):
        if eligible is None:
            continue

        for item in eligible:
            hint = TrackMatchHint(
                title=track,
                artist_credit=release.artist if release.is_compilation else None,
                position=None,
                confidence=SONG_AS_TRACK_CONFIDENCE,
                source=TrackMatchSource.discogs_release,
            )

            if item.id in seen_ids:
                matched_via_by_id[item.id].append(hint)
                continue

            seen_ids.add(item.id)
            matched_items.append(item)
            matched_via_by_id[item.id] = [hint]
            logger.debug(
                f"{label}: matched '{item.artist} - {item.title}' via release '{release.album}'"
            )

            if len(matched_items) >= MAX_SEARCH_RESULTS:
                done = True
                break
        if done:
            break

    if matched_items:
        return matched_items, matched_via_by_id, {}

    # A1 carry-through (LML#628): the library walk dropped every candidate (no
    # WXYC row), but a release may still resolve + validate from the inputs.
    # Surface it row-less rather than returning empty. Gated; off by default.
    # LML#652: the bulk kill switch also gates the carry-through — /lookup/bulk
    # passes ``allow_release_resolution_fallback=False`` so the backfill never
    # pays the per-row resolve + #632 cache write (parity with #604's lazy
    # fallback). Covers SONG_AS_TRACK and SWAPPED, both of which reach here.
    if get_settings().lml_resolve_nonlibrary_release and allow_release_resolution_fallback:
        # Anchor artist: the kernel's typed ``artist`` (SWAPPED's identified
        # side). SONG_AS_TRACK has none, so fall back to the surfaced release's
        # own credit — the only artist signal a song-only query carries. Either
        # way this bypasses ``require_artist`` (no library row to re-filter; the
        # per-track credit check settles the artist).
        anchor_artist = (
            artist or next((r.artist for r in raw_releases if r.artist and r.artist.strip()), "")
        ).strip()
        # LML#660: a compilation-marker anchor ("Various") — or no anchor at all —
        # is not a usable per-track artist. SONG_AS_TRACK on a V/A comp lands here
        # with the release-level "Various" credit, the exact case the carry-through
        # targets. Anchoring on it would (a) validate only against that release-
        # level credit, blind to the track's actual performer, and (b) key the
        # #632 cache on ``("various", <title>, True)``, collapsing same-titled
        # tracks across different comps onto one row. So recover the track's
        # *actual per-track credit* from the release tracklist (LML#660) and anchor
        # on that instead. When none is recoverable, suppress the carry-through
        # rather than surface an imprecise, collision-prone item — the LML#649
        # fallback, preserved. SWAPPED and TRACK_ON_COMPILATION carry a typed
        # artist and never reach this recovery.
        if not anchor_artist or is_compilation_artist(anchor_artist):
            recovered = await _recover_track_credit(discogs_service, raw_releases, track)
            if not recovered:
                logger.info(
                    f"{label}: suppressing row-less carry-through — no per-track "
                    f"credit recoverable for '{track}' (anchor would be '{anchor_artist}')"
                )
                return matched_items, matched_via_by_id, {}
            logger.info(
                f"{label}: anchoring row-less carry-through on recovered per-track "
                f"credit '{recovered}' (release-level anchor was '{anchor_artist}')"
            )
            anchor_artist = recovered
        resolved = await _resolve_nonlibrary_release(
            discogs_service,
            pg,
            song=track,
            artist=anchor_artist,
            album=album,
            is_track=True,
        )
        if resolved is not None:
            rowless = _make_rowless_item(artist=anchor_artist, title=resolved.album_title)
            logger.info(
                f"{label}: surfacing row-less Discogs release {resolved.release_id} "
                f"('{resolved.album_title}') — validated, not in library"
            )
            return [rowless], {}, {ROWLESS_LIBRARY_ID: resolved}

    return matched_items, matched_via_by_id, {}


async def search_album_fuzzy(db: LibraryDB, album_title: str) -> list[LibraryItem]:
    """Search for album with fuzzy keyword matching."""
    from rapidfuzz import fuzz

    # Exact-title pre-pass. The FTS5 path below truncates to
    # ``MAX_SEARCH_RESULTS`` rows in implementation-defined (rowid) order, which
    # silently drops literal-title matches whose library row was added later in
    # the catalog. Discogs typically hands us the album title verbatim, so a
    # literal hit is the most reliable signal — surface it before falling
    # through to fuzzy scoring.
    exact = await db.exact_title(album_title)
    if exact:
        return exact

    async def _search_and_filter(query: str) -> list[LibraryItem]:
        raw = await db.search(query=query, limit=MAX_SEARCH_RESULTS)
        if not raw:
            return []
        q_lower = query.lower()
        return [
            r
            for r in raw
            if _va_series_title_match(q_lower, r)
            or album_title_acceptable(q_lower, (r.title or "").lower())
        ]

    # First try the un-stripped query. If it produces no surviving candidates,
    # retry with all trailing parenthetical groups stripped — this rescues
    # the over-specific Discogs subtitle case (WXYC#531). The full Discogs
    # title fails the FTS5 search layer in three distinct ways depending on
    # its shape, all of which strip-retry repairs:
    #   1. FTS5 throws on special characters in the subtitle (``&`` is the
    #      common offender), ``db.search`` swallows the error and falls
    #      through to the LIKE / fuzzy fallback layers.
    #   2. ``_fallback_like_search`` uses implicit-AND across title+artist,
    #      requiring every significant word to appear — terse library rows
    #      (``Disco Not Disco, vol. 1``) lack the subtitle's words and
    #      return empty.
    #   3. ``_fuzzy_search`` candidate-trawls on the longest word's 3-char
    #      prefix and returns tangentially related rows that the post-filter
    #      then drops.
    # In all three shapes the actual library row goes unseen until the
    # paren-strip retry produces a query FTS5 can answer cleanly.
    results = await _search_and_filter(album_title)

    if not results:
        stripped = _TRAILING_PARENTHETICAL_RE.sub("", album_title).strip()
        if stripped and stripped != album_title:
            results = await _search_and_filter(stripped)

    if not results:
        words = re.sub(r"[^\w\s]", " ", album_title.lower()).split()
        significant_words = [w for w in words if len(w) > 3 and w not in STOPWORDS]

        if significant_words:
            album_lower = album_title.lower()
            # Require roughly half the keywords to match — lenient enough for
            # abbreviated titles (e.g., "Punk 82-88" vs "Post Punk 1982 - 1988")
            # but strict enough to reject unrelated albums that share a few words
            # (e.g., "20th Anniversary Concert" vs "Trax Records 20th Anniversary Edition").
            # The similarity and album_title_acceptable checks provide additional gating.
            min_keywords = max(2, (len(significant_words) + 1) // 2)

            # Try progressively shorter queries to handle word mismatches between
            # Discogs and library titles (e.g., "Edition" vs "Collection").
            # Filter inside the loop so false positives don't block shorter queries.
            max_words = min(4, len(significant_words))
            for n_words in range(max_words, 1, -1):
                fuzzy_query = " ".join(significant_words[:n_words])
                logger.debug(
                    f"Exact match failed for '{album_title}', trying fuzzy: '{fuzzy_query}'"
                )
                raw_results = await db.search(query=fuzzy_query, limit=MAX_SEARCH_RESULTS)
                if not raw_results:
                    continue

                filtered_results = []
                for result in raw_results:
                    result_title_lower = (result.title or "").lower()

                    keyword_matches = sum(
                        1 for word in significant_words if word in result_title_lower
                    )
                    similarity = fuzz.token_set_ratio(album_lower, result_title_lower)
                    title_ok = album_title_acceptable(album_lower, result_title_lower)

                    if keyword_matches >= min_keywords and similarity >= 60 and title_ok:
                        logger.debug(
                            f"Album match: '{result.title}' "
                            f"(keywords={keyword_matches}/{len(significant_words)}, "
                            f"similarity={similarity})"
                        )
                        filtered_results.append(result)
                    else:
                        logger.debug(
                            f"Album rejected: '{result.title}' "
                            f"(keywords={keyword_matches}/{len(significant_words)}, "
                            f"similarity={similarity})"
                        )

                if filtered_results:
                    results = filtered_results
                    break

    return results
