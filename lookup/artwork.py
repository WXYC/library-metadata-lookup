"""Artwork fetch + release binding for the lookup pipeline.

Home of the Step-4 artwork fetch (``fetch_artwork_for_items`` — per-item
Discogs search with the LML#478 80/80 fuzzy floor), the LML#604
trust-and-bind of an already-validated ``ResolvedRelease``
(``_bind_resolved_release``), and the release-cover → artist-image →
label-image fallback cascade (``_resolve_fallback_artwork``). Extracted
verbatim from ``lookup/orchestrator.py`` (LML#728).
"""

import asyncio
import logging

from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import find_best_typed_match
from config.settings import get_settings
from discogs.models import DiscogsSearchRequest, DiscogsSearchResult
from discogs.service import DiscogsService
from library.models import LibraryItem
from lookup.artist_resolution import (
    _log_release_resolution_bind,
    resolve_canonical_artist,
)
from lookup.matching import is_self_titled, map_library_format_to_discogs
from lookup.release_resolution import ResolvedRelease, resolve_release_for_track_cached
from lookup.rowless import ROWLESS_LIBRARY_ID, ROWLESS_NO_ALBUM_CONFIDENCE

logger = logging.getLogger(__name__)

COMPILATION_ARTIST_SEARCH_FORM = "Various"
"""The bare form Discogs's search endpoint accepts for compilation artists."""

COMPILATION_ARTIST_CANONICAL_FORM = "Various Artists"
"""The full form Discogs's response payloads typically carry. Kept paired with
``COMPILATION_ARTIST_SEARCH_FORM`` for the variant-scoring path in
``fetch_artwork_for_items``; any change to one MUST change the other or
``score_match("Various","Various Artists")=63.6`` will start flipping
compilations to None at the 80/80 floor (LML#478 round-2 finding).

Module-public (no underscore prefix) so ``scripts/measure_artwork_match_floor.py``
can import the same constants the runtime path uses — keeping the
measurement's compilation handling provably-aligned with production."""


async def _resolve_fallback_artwork(discogs_service: DiscogsService, release_id: int) -> str | None:
    """Try the release's own cover (images[0]), then artist image, then label image.

    Structurally invalid ids (``release_id <= 0``) short-circuit before the
    Discogs round-trip — the LML#401 synthesis pattern produces a
    ``release_id=0`` sentinel that any future caller could leak in here (see
    issue #518). Discogs release ids start at 1, so the strict gate is also a
    correctness check against malformed upstream payloads.
    """
    if release_id <= 0:
        return None
    release = await discogs_service.get_release(release_id)
    if not release:
        return None

    # The search endpoint's `cover_image` is sometimes empty for releases whose
    # release-detail `images[0].uri` is populated. Prefer that over the
    # artist/label image fallback so enrichment-worker callers (single LML
    # round-trip) recover the same cover the /proxy/metadata/album legacy
    # two-call path produces via populateReleaseMetadata.
    if release.artwork_url:
        return release.artwork_url

    if release.artist_id:
        image = await discogs_service.get_artist_image(release.artist_id)
        if image:
            logger.info(f"Using artist image fallback for release {release_id}")
            return image

    if release.label_id:
        image = await discogs_service.get_label_image(release.label_id)
        if image:
            logger.info(f"Using label image fallback for release {release_id}")
            return image

    return None


async def _bind_resolved_release(
    discogs_service: DiscogsService,
    release: ResolvedRelease,
    item: LibraryItem,
    *,
    album: str | None = None,
) -> DiscogsSearchResult:
    """Trust-and-bind an already-validated ``ResolvedRelease`` (LML#604).

    The release was validated this same request — either carried on the
    ``discogs_titles`` seam by the search strategy or just validated by
    ``resolve_release_for_track`` — so the artist-floor re-search is skipped
    entirely. Art comes from the release's own cover via
    ``_resolve_fallback_artwork``; downstream ``enrich_artwork_results`` fills
    year/label/genres via ``get_release`` exactly as for a floor-matched result.

    The floor was never a validation backstop — it is a search disambiguator,
    and validation already settled the artist — so ``confidence`` defaults to the
    maximum 1.0.

    **A2 soft confidence (LML#629):** a *row-less* (id==0) bind is softened in two
    cases, and the result takes the lower (softer) of the two signals:

    - **No typed album in the request** — the release is the best-ranked guess,
      not a user-confirmed album, so any row-less bind drops to
      ``ROWLESS_NO_ALBUM_CONFIDENCE``.
    - **The seam already carries a soft confidence** (``release.confidence``) —
      the cached-track safety net (A4) picks by track only and never
      album-matches, so it stamps the seam soft *regardless* of a typed album;
      the bind can't tell A4 from an album-ranked carry-through otherwise. An
      album-ranked carry-through (A1/#628 with a typed album) leaves the seam at
      1.0, so a typed-album request keeps the full confidence there.

    An in-library bind (id != 0) is never softened.
    """
    artwork_url = await _resolve_fallback_artwork(discogs_service, release.release_id)
    confidence = release.confidence
    if item.id == ROWLESS_LIBRARY_ID and not (album or "").strip():
        confidence = min(confidence, ROWLESS_NO_ALBUM_CONFIDENCE)
    return DiscogsSearchResult(
        release_id=release.release_id,
        release_url=release.release_url,
        album=release.album_title,
        artist=item.artist,
        artwork_url=artwork_url,
        confidence=confidence,
    )


async def fetch_artwork_for_items(
    items: list[LibraryItem],
    discogs_service: DiscogsService | None,
    discogs_titles: dict[int, ResolvedRelease] | None = None,
    *,
    song: str | None = None,
    album: str | None = None,
    allow_release_resolution_fallback: bool = True,
    found_on_compilation: bool = False,
    release_overrides: dict[int, int] | None = None,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Fetch artwork for multiple library items in parallel.

    ``song`` (the request-level track, when present) and the
    ``lml_resolve_compilation_release`` flag enable the LML#604 lazy
    release-resolution fallback in ``fetch_one``: when the artist-floor search
    rejects a Various-Artists compilation row, resolve and trust-bind the
    validated release instead of leaving ``discogs_url`` unbound.
    ``allow_release_resolution_fallback`` is the bulk kill switch — the
    /lookup/bulk drain passes ``False`` so the 35k-album backfill never triggers
    the per-row Discogs fan-out.

    ``album`` (the request-level typed album, when present) only gates the A2
    soft-confidence on a row-less carried bind (LML#629): a no-album query's
    row-less release binds at ``ROWLESS_NO_ALBUM_CONFIDENCE`` rather than 1.0.

    ``found_on_compilation`` (LML#684, widened by LML#956): when the result came
    from ``search_compilations_for_track`` (TRACK_ON_COMPILATION), an in-library
    row carries an already-validated ``ResolvedRelease`` on the seam. That
    carried release is trust-bound for artwork *before* the artist-floor
    re-search — whenever one is carried, not only when the floor rejects — so the
    displayed release is always the one validation confirmed. This covers two
    floor failure modes: the floor *rejects* every candidate (the systematic
    *non*-Various-Artists trio / collaboration case, e.g. the row filed under
    "Bill Orcutt" while Discogs credits the full trio, which would otherwise leave
    the result with no artwork), and the floor *clears on the wrong release* (a
    generic V/A comp title where the title+artist floor binds a *same-titled*
    release that does not carry the track — LML#956). Independent of
    ``lml_resolve_compilation_release``, since binding an already-carried release
    costs no extra Discogs fan-out (unlike the flag-gated lazy
    ``resolve_release_for_track`` fallback below).

    ``release_overrides`` (LML#850): a ``library_id -> discogs_release_id`` map of
    **hand-verified** pins the orchestrator prefetched for this request (empty /
    ``None`` when the ``lml_library_release_override`` flag is off, so this is a
    no-op by default). A pinned library row binds its release BEFORE the
    trust-bind and fuzzy paths — a human override is the most-trusted signal and
    wins even over a carried release; it also skips the Discogs ``search``, so an
    override hit *reduces* per-request work.
    """
    if not discogs_service:
        return [(item, None) for item in items]

    discogs_titles = discogs_titles or {}
    release_overrides = release_overrides or {}
    # Bound to a distinct name: ``fetch_one`` rebinds a local ``album`` (the
    # per-item search title), which would shadow this request-level value.
    request_album = album
    settings = get_settings()
    resolve_compilation_release = settings.lml_resolve_compilation_release
    resolve_nonlibrary_release = settings.lml_resolve_nonlibrary_release
    resolve_artist_canonical = settings.lml_resolve_artist_canonical

    async def fetch_one(item: LibraryItem) -> DiscogsSearchResult | None:
        try:
            # LML#850: a hand-verified library-release override is the most-
            # trusted signal — consult it FIRST, before the LML#604 trust-bind
            # and the LML#478 artist-floor fuzzy search. On a hit, trust-bind
            # the pinned release (reusing ``_bind_resolved_release``: it fetches
            # the pinned release's cover and downstream ``enrich_one`` pulls its
            # tracklist), skipping the fuzzy ``search`` entirely — an override
            # hit costs one cached ``get_release``, not a search + N-candidate
            # floor. ``album_title`` stays the library row's own title so the
            # surfaced album keeps the catalog naming; the pin only redirects the
            # release id (and thus the tracklist). ``> 0`` mirrors the DB CHECK;
            # a malformed 0/negative pin can never reach the ``release_id=0``
            # sentinel path.
            override_release_id = release_overrides.get(item.id)
            if override_release_id is not None and override_release_id > 0:
                override_release = ResolvedRelease(
                    release_id=override_release_id,
                    release_url=f"https://www.discogs.com/release/{override_release_id}",
                    is_compilation=False,
                    album_title=item.title or "",
                )
                return await _bind_resolved_release(
                    discogs_service, override_release, item, album=request_album
                )

            # The widened seam carries a ResolvedRelease; its album_title is the
            # value the seam used to carry as a bare string. Falls back to the
            # library row's own title when no release was resolved for this id.
            resolved = discogs_titles.get(item.id)

            # Carried-release trust-and-bind: the search strategy already
            # resolved and validated this release this same request, so bind it
            # by id and skip the artist-floor re-search. Two flag-gated callers:
            #   - LML#604: an in-library compilation row whose floor search the
            #     ``lml_resolve_compilation_release`` flag rescues.
            #   - LML#628: a *row-less* (id==0) non-library release the A1
            #     carry-through synthesized; ``lml_resolve_nonlibrary_release``
            #     gates it. There is no library row to floor-search against, so
            #     binding the carried release is the only way to surface it.
            # Flag-off on both re-searches exactly as before (the release's
            # album_title still seeds that search below for non-row-less items).
            # LML#652: the row-less (id==0) bind also honors the bulk kill switch —
            # belt-and-suspenders, since once the five row-less producers (the four
            # Discogs-aware strategies + the A4 cached-track safety net) are gated
            # no id==0 item reaches here on /lookup/bulk. The #604 compilation
            # trust-bind (the first operand) is NOT gated here; its own lazy
            # fallback already respects the switch below.
            bind_carried = resolve_compilation_release or (
                resolve_nonlibrary_release
                and item.id == ROWLESS_LIBRARY_ID
                and allow_release_resolution_fallback
            )
            if bind_carried and resolved is not None:
                return await _bind_resolved_release(
                    discogs_service, resolved, item, album=request_album
                )

            # Found-on-compilation trust-bind (LML#684, widened): an in-library
            # compilation row carries a release the strategy already resolved AND
            # track-validated this same request (via validate_release_for_track in
            # search_compilations_for_track). The title+artist re-search below
            # scores candidates against the row's OWN title (album_variants
            # appends item.title), so for a generic V/A comp title it can bind a
            # *same-titled* release that does NOT carry the track — the prod
            # divergence (LML#956) where "Greatest hits of the 50s & 60s" bound
            # Plaza House's 13332759 instead of the validated 605487. The carried release is
            # already validated and binding it costs no extra Discogs fan-out (only
            # a cached get_release), so prefer it over the unvalidated title pick —
            # not only when the search returns None (#684's original gate) but
            # whenever a validated release is carried. Independent of
            # lml_resolve_compilation_release (that flag was off at prod runtime).
            # The row-less (id==0) carry-through is bound by the flag-gated
            # bind_carried branch above, so it is excluded here.
            if found_on_compilation and resolved is not None and item.id != ROWLESS_LIBRARY_ID:
                return await _bind_resolved_release(
                    discogs_service, resolved, item, album=request_album
                )

            album = resolved.album_title if resolved is not None else item.title

            # Self-titled albums stored as "S/t" should use the artist name
            # for Discogs search instead of the abbreviation
            if is_self_titled(album or ""):
                album = item.artist

            # The *track* artist (pre-compilation-form mutation). The lazy
            # release-resolution fallback validates the per-track credit, so it
            # must probe with this — never the bare "Various" search form below.
            track_artist = item.alternate_artist_name or item.artist or ""

            artist = track_artist
            if is_compilation_artist(artist):
                artist = COMPILATION_ARTIST_SEARCH_FORM

            response = await discogs_service.search(
                DiscogsSearchRequest(
                    album=album,
                    artist=artist,
                    label=item.label,
                    format=map_library_format_to_discogs(item.format),
                )
            )
            # Score candidates against (artist, album) with an 80/80 floor.
            # Returns None when nothing clears — better than serving wrong
            # artwork for releases that share a title across multiple albums
            # (LML#478, e.g. Noura Mint Seymali's "Hebebeb (Zrag)" on both
            # *Tzenni* and *Yenbett*).
            #
            # Query variants:
            # - Artist: the compilation search uses bare "Various" because
            #   that's the form Discogs's search endpoint accepts, but
            #   Discogs's canonical artist field for compilations is often
            #   "Various Artists" (sometimes with a "(N)" disambig suffix).
            #   Score against both forms. Numeric disambigs clear via
            #   token_sort_ratio's tolerance; descriptive disambigs
            #   ("Brazilian Soul" etc.) are a known floor-rejection edge
            #   case — accept the loss in exchange for the floor's gain.
            # - Album: when discogs_titles[item.id] overrides the library
            #   title with a long Discogs-canonical form (compilation rescue
            #   path), Discogs's own search results may carry just the short
            #   library-side title. Score against both. Don't readmit a
            #   self-titled trigger ("S/t" etc.) as a variant — that would
            #   let a wrong-release candidate with album="S/t" clear the
            #   floor trivially.
            artist_variants = [artist]
            if artist == COMPILATION_ARTIST_SEARCH_FORM:
                artist_variants.append(COMPILATION_ARTIST_CANONICAL_FORM)
            album_variants = [album or ""]
            if item.title and item.title != album and not is_self_titled(item.title):
                album_variants.append(item.title)
            result = find_best_typed_match(
                # None = degraded Discogs call (LML#918); score an empty set,
                # which falls into the same "no match" path as an empty response.
                response.results if response is not None else [],
                query_artist=artist_variants,
                query_title=album_variants,
                artist_fn=lambda r: r.artist_variants(),
                title_fn=lambda r: r.album,
            )
            if result is None:
                # A found-on-compilation in-library row that carries a validated
                # release is already trust-bound above (before this re-search), so
                # no such row reaches here — the remaining found-on-compilation
                # rows that reach this branch carried nothing (resolved is None).
                # The lazy fallback below resolves a release for exactly that
                # resolved-is-None case.
                #
                # Lazy release-resolution fallback (LML#604): the artist floor
                # rejected every candidate — the systematic failure for a V/A
                # compilation row filed under the track artist. When the flag is
                # on, a song is present, no release was carried on the seam, and
                # the bulk kill switch allows it, resolve and trust-bind the
                # validated release (bypassing the floor — validation settles the
                # artist via the per-track credit). resolve_release_for_track
                # returns [] on probe failure, so a transient Discogs error can
                # never abort the request here.
                if (
                    resolve_compilation_release
                    and resolved is None
                    and song
                    and allow_release_resolution_fallback
                ):
                    # Probe parity with search_compilations_for_track (LML#604
                    # deferred finding #1): apply the resolver canonical-swap
                    # when lml_resolve_artist_canonical is on, and fire the
                    # album-title wave only when the artist was NOT swapped (a
                    # high-confidence swap makes the artist-scoped probe
                    # authoritative — same gate the strategy uses).
                    probe_artist = track_artist
                    swapped = False
                    if resolve_artist_canonical:
                        cache_service = getattr(discogs_service, "cache_service", None)
                        outcome = await resolve_canonical_artist(
                            track_artist, cache_service=cache_service
                        )
                        if outcome.swapped:
                            probe_artist = outcome.canonical
                            swapped = True
                    candidates = await resolve_release_for_track_cached(
                        discogs_service,
                        song,
                        probe_artist,
                        album,
                        bool(album) and not swapped,
                    )
                    best = candidates[0] if candidates else None
                    _log_release_resolution_bind(
                        song=song,
                        artist=track_artist,
                        album=album,
                        bound=best is not None,
                        release_id=best.release_id if best is not None else None,
                    )
                    if best is not None:
                        return await _bind_resolved_release(
                            discogs_service, best, item, album=request_album
                        )
                return None
            if not result.artwork_url:
                fallback = await _resolve_fallback_artwork(discogs_service, result.release_id)
                if fallback:
                    result = result.model_copy(update={"artwork_url": fallback})
            return result
        except Exception as e:
            logger.warning(f"Artwork lookup failed for {item.title}: {e}")
            return None

    artwork_results = await asyncio.gather(*[fetch_one(item) for item in items])
    return list(zip(items, artwork_results, strict=True))
