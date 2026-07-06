"""Lookup orchestrator: the core search logic extracted from request-o-matic.

This module contains the perform_lookup() function that orchestrates the full
search pipeline: artist correction -> album resolution -> search strategies ->
track validation -> artwork fetch -> metadata enrichment -> context message.
"""

import asyncio
import logging
from functools import partial
from typing import Any
from urllib.parse import quote

import sentry_sdk
from wxyc_etl.text import to_match_form as normalize_for_comparison
from wxyc_fastapi.observability import (
    RequestTelemetry,
)

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient, AppleMusicTrackMatch
from clients.streaming.matching import (
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    score_match,
    score_match_track,
    strip_discogs_disambig,
    strip_track_suffix,
)
from clients.streaming.spotify import SpotifyClient
from config.settings import get_settings
from core.search import (
    execute_search_pipeline,
    get_search_type_from_state,
)
from discogs.cache_service import DiscogsCacheService
from discogs.lookup import lookup_releases_by_track
from discogs.markup_parser import (
    CachedOnlyResolver,
    DiscogsServiceResolver,
    parse,
    parse_async,
)
from discogs.models import (
    ArtistDetails,
    DiscogsSearchResult,
    ReleaseMetadataResponse,
    ResolvedToken,
)
from discogs.service import DiscogsService, find_track_position
from discogs.writer_roles import writer_credits_from_release
from entity.sources import PgSource, PgSourceProtocol
from entity.store import EntityStore, Identity
from generated.api_models import (
    LibraryCatalogItem,
    ReconciledIdentity,
)
from library.db import LibraryDB
from library.models import LibraryItem
from lookup.artist_resolution import (
    _artist_identity_split_gate_enabled,
    _artist_pair_verified,
    _log_artist_identity_split_gate,
    _mb_rescue_song_match_required,
    _project_mb_rescue_attrs,
)
from lookup.artwork import fetch_artwork_for_items
from lookup.external_search import (
    search_external_albums,
    search_external_artists,
    search_external_tracks,
)
from lookup.matching import (
    _filter_results_by_song_as_album_title,
    library_artist_for,
    limit_results,
)
from lookup.models import LookupRequest, LookupResponse, LookupResultItem
from lookup.release_resolution import (
    ResolvedRelease,
)
from lookup.rowless import (
    ROWLESS_LIBRARY_ID,
)
from lookup.strategies import build_strategies
from lookup.strategies.artist_plus_album import search_library_with_fallback
from lookup.strategies.library_miss import _library_miss_discogs_search
from lookup.strategies.song_as_artist import search_song_as_artist
from lookup.strategies.song_as_track import search_song_as_track
from lookup.strategies.swapped_interpretation import search_with_alternative_interpretation
from lookup.strategies.track_on_compilation import search_compilations_for_track
from lookup.streaming_url_postprocess import apply_streaming_url_postprocess
from lookup.timeouts import apple_music_lookup_timeout_s
from lookup.validation import (
    filter_results_by_track_validation,
    find_library_albums_with_cached_track,
)
from release.musicbrainz_resolver import resolve_tracklist_via_musicbrainz
from services.parser import MessageType, ParsedRequest

logger = logging.getLogger(__name__)

_WARM_CACHE_CONCURRENCY: int = 4
"""Process-wide cap on concurrent bio cache-warm tasks.

A single warm task can fan out to many Discogs API calls (one per
unresolved `[a…]`/`[r…]`/`[m…]` ref the local cache misses). The semaphore
bounds the total in-flight API load when several flowsheet entries get
committed in quick succession. 4 is conservative — Discogs's published
rate limit is 60 RPM authenticated; warm bursts at 4 concurrent × a few
refs each still leave headroom for the read path.
"""

_warm_cache_semaphore: asyncio.Semaphore | None = None
"""Lazily-constructed (needs a running event loop). Re-bind on the first call."""


_background_tasks: set[asyncio.Task] = set()
"""References to fire-and-forget tasks scheduled by ``enrich_artwork_results``.

`asyncio.create_task` returns weak references — without anchoring the
task somewhere strong, the GC can reap it mid-execution and the warm
silently drops. The standard pattern is a module-level set; each task
removes itself in a done_callback. See
https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
"""


async def resolve_albums_for_track(
    parsed: ParsedRequest,
    discogs_service: DiscogsService | None = None,
) -> tuple[list[str], bool]:
    """Resolve album names for a track if not provided.

    Searches Discogs for ALL releases containing the track, not just the first one.

    Returns:
        Tuple of (list of album names, song_not_found_flag)
    """
    album_is_missing = not parsed.album
    album_is_artist = (
        parsed.album
        and parsed.artist
        and normalize_for_comparison(parsed.album).strip()
        == normalize_for_comparison(parsed.artist).strip()
    )

    if parsed.song and parsed.artist and (album_is_missing or album_is_artist):
        if album_is_artist:
            logger.info(f"Album '{parsed.album}' appears to be artist name, looking up albums")
        try:
            releases = await lookup_releases_by_track(
                parsed.song, parsed.artist, limit=10, service=discogs_service
            )
            if releases:
                albums = []
                artist_normalized = normalize_for_comparison(parsed.artist)
                for release_artist, album in releases:
                    if normalize_for_comparison(release_artist).startswith(artist_normalized):
                        if album not in albums:
                            albums.append(album)
                if albums:
                    logger.info(f"Found {len(albums)} albums for song '{parsed.song}': {albums}")
                    return albums, False
            logger.info(f"Could not find albums for song '{parsed.song}'")
            return [], True
        except Exception as e:
            logger.warning(f"Track lookup failed: {e}")
            return [], True
    return [parsed.album] if parsed.album else [], False


def _build_streaming_search_url(base: str, artist: str, term: str) -> str:
    """Build a streaming service search URL from artist + song/album."""
    query = f"{artist} {term}" if term else artist
    return f"{base}{quote(query)}"


async def enrich_artwork_results(
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]],
    discogs_service: DiscogsService | None,
    song: str | None = None,
    album: str | None = None,
    artist: str | None = None,
    library_db: LibraryDB | None = None,
    *,
    extended: bool = False,
    warm_cache: bool = False,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
    apple_music: AppleMusicClient | None = None,
    spotify: SpotifyClient | None = None,
    bandcamp: BandcampClient | None = None,
    entity_store: EntityStore | None = None,
    discogs_cache_pg: PgSource | None = None,
    found_on_compilation: bool = False,
) -> list[tuple[LibraryItem, DiscogsSearchResult | None]]:
    """Enrich artwork results with release year, artist details, and streaming links.

    When library_db has a streaming_links table, uses direct URLs from the database.
    Falls back to search URLs when direct links are not available.

    ``apple_music`` is the authenticated Apple Music client (LML#443). When
    provided, the orchestrator probes Apple Music for each item: the happy
    path (library row clears the LML#477 / PR #481 title gate) calls
    ``find_track_url`` to surface the Apple Music URL only; the synthesis
    path (no Discogs match OR library row fails the title gate — LML#487)
    calls ``find_track_metadata`` to surface URL + ``artwork_url`` +
    ``release_year`` from the same ``search_song`` response. Exactly one
    Apple Music API call per item either way — picking ``find_track_url``
    vs. ``find_track_metadata`` does not inflate the per-request quota.
    When ``apple_music`` is None (credentials unconfigured), the probe is
    skipped and ``apple_music_url`` is set from the library DB
    ``streaming_links`` override (if present) or stays None. The DB
    override always wins over the probe — see the final assignment
    ``apple_music_override or apple_music_url or None``.

    **Behavior change vs. v0.5.0:** release/artist details (release_year,
    artist_bio, wikipedia_url) are fetched only for ``items_with_artwork[0]``.
    BS/iOS only consume the top-1 result, so paying N round-trips of Discogs
    cache (and on miss, API) latency for non-top-1 items was waste. The
    streaming-URL fallback build still runs per-result (cheap; no I/O).
    Gating is *positional*: a top-1 entry with ``artwork=None`` means no
    item in the response carries release-year/bio/wiki, even when items
    further down have artwork. BS/iOS only ever read ``results[0]`` so
    this is fine in practice; the lookup pipeline guarantees the strongest
    match is in position 0.

    **Behavior change vs. v0.6.0 (LML#401):** an item entering with
    ``artwork=None`` no longer round-trips as ``(item, None)``. It returns
    a synthesized ``DiscogsSearchResult(release_id=0, release_url="")``
    carrying only the streaming-URL fields (Apple via the authenticated
    ``AppleMusicClient`` + Spotify/YT/BC/SC search-URL fallbacks). Album-derived scalars
    (release_year, artist_bio, wikipedia_url) and the ``extended=True``
    payload stay None on the synthesized result, preserving the positional-
    gating invariant above. The ``(release_id=0, release_url="")`` pair is
    a cross-service contract: Backend-Service (BS#1185) keys off of it to
    skip ``extractAlbumMetadata`` while still consuming streaming URLs.
    Required for the WXYC/BS#1184 fix — releases on Apple Music that aren't
    in the WXYC/Discogs catalog now surface streaming buttons on iOS.

    **Behavior change vs. v0.7.0 (LML#487):** the synthesis path now also
    fires when ``artwork`` is non-None but its library row fails the
    LML#477 title gate (``score_match(album, item.title) < 80`` — the
    Noura Mint Seymali Tzenni-vs-Yenbett shape). The synthesized result
    additionally carries ``artwork_url`` and ``release_year`` from the
    ``find_track_metadata`` probe response, so flowsheet entries for an
    album absent from the WXYC catalog (but present on Apple Music)
    surface the *right* album's cover art on iOS / dj-site — not a
    sibling-album row's leaked Discogs image. ``release_year`` is sourced
    from the Apple Music ``releaseDate`` field; the Discogs-derived
    ``artist_bio`` / ``wikipedia_url`` / ``extended=True`` payload stay
    None on the synthesized result because they require a verified
    Discogs identity link (no equivalent surfaces from Apple).

    ``extended=True`` additionally populates the new DiscogsMatchResult
    fields LML already loaded during the release+artist fetches:
    ``discogs_artist_id``, ``tracklist``, ``genres``, ``styles``, ``label``,
    ``full_release_date``, ``artist_image_url``, ``profile_tokens``. Bio
    parsing uses ``CachedOnlyResolver`` when ``discogs_cache`` is provided
    — refs that miss the cache fall through as plain text, never trigger
    an inline Discogs API call. When ``discogs_cache`` is None (LML
    deployed without ``DATABASE_URL_DISCOGS``), bio parsing falls back to
    sync ``parse()`` which strips ID-based refs but keeps name-based and
    formatting tokens, so ``profile_tokens`` is still non-None for
    consistent client-side rendering.

    ``warm_cache=True`` schedules a fire-and-forget ``asyncio.create_task``
    after the response is composed that runs the *deep* async parse
    against the API-capable resolver, warming the PG cache for referenced
    entities so subsequent read-path lookups render richer. The task is
    not awaited — write-path callers (Backend-Service's flowsheet-linkage
    service) pay zero added latency. Concurrent warm tasks are bounded by
    ``_WARM_CACHE_CONCURRENCY`` to cap Discogs API amplification under
    burst load. Failures are logged via ``logger.exception`` so a stuck
    warm doesn't go silent.
    """
    if not discogs_service or not items_with_artwork:
        return items_with_artwork

    artist_identity_split_enabled = _artist_identity_split_gate_enabled()
    request_artist_stripped = (artist or "").strip()

    # Top-1-only expensive enrichment. fetch_release_details runs once;
    # non-top-1 items reuse the same per-result streaming-URL build.
    async def fetch_top1_release_details() -> tuple[
        int | None,
        str | None,
        str | None,
        ReleaseMetadataResponse | None,
        ArtistDetails | None,
    ]:
        """Returns (year, artist_bio, wikipedia_url, release, details) for the top-1 result.

        Returns the release + artist payloads alongside the legacy three
        scalars so the extended-field population can pluck additional
        fields without re-fetching.
        """
        top_artwork = items_with_artwork[0][1]
        # `release_id <= 0` short-circuits the LML#401 streaming-only
        # synthesis sentinel (see issue #518): the synthesized result
        # carries `release_id=0` as a BS#1185 cross-service contract,
        # and round-tripping it through Discogs hits `/releases/0` (404)
        # before the `if not release` branch silently swallows the response.
        if top_artwork is None or top_artwork.release_id <= 0:
            return None, None, None, None, None
        try:
            release = await discogs_service.get_release(top_artwork.release_id)
            if not release:
                return None, None, None, None, None

            year = release.year if isinstance(release.year, int) else None
            artist_id = release.artist_id
            if not isinstance(artist_id, int) or artist_id <= 0:
                return year, None, None, release, None

            details = await discogs_service.get_artist_details(artist_id)
            if not details:
                return year, None, None, release, None

            bio = details.profile if isinstance(details.profile, str) else None
            wiki = next(
                (url for url in details.urls if isinstance(url, str) and "wikipedia.org" in url),
                None,
            )
            return year, bio, wiki, release, details
        except Exception:
            return None, None, None, None, None

    top1_year, top1_bio, top1_wiki, top1_release, top1_details = await fetch_top1_release_details()

    # LML#504: release-side artist-identity hop. Computed once (shared across
    # all items in this batch) since ``top1_release`` is by definition top-1-only
    # and the same value is read by every enrich_one call. The library-row
    # hop CANNOT be hoisted the same way: the per-item artwork gate (the
    # LML#487 synth path runs per item, not just for top-1) needs to know
    # whether THAT item's library row anchors the request — hoisting to
    # top-1-only suppresses every non-top-1 item's probe artwork
    # unconditionally. Per-item library-row computation in ``_artist_pair_verified``
    # is cheap (two short helper calls; both early-exit on empty inputs).
    top1_release_artist = getattr(top1_release, "artist", None)
    release_side_artist_verified = _artist_pair_verified(
        request_artist_stripped, top1_release_artist
    )
    # When ``top1_release`` was fetched but its ``.artist`` field is empty or
    # whitespace-only, treat the release hop as if there was nothing to verify
    # against and fall through to library-row-only verification. Covers the
    # LML#507 prefetch-skipped case, the artwork=None case, the
    # get_release-errored case, AND corrupted/empty Discogs release rows.
    release_anchor_present = isinstance(top1_release_artist, str) and bool(
        strip_discogs_disambig(top1_release_artist).strip()
    )

    # Cache-only deep parse of the top-1 bio for the extended path. Refs
    # that miss the cache fall through; we never fire a new API call here.
    # When the PG cache is unavailable (no DATABASE_URL_DISCOGS), fall
    # back to sync parse() — drops ID-based refs but keeps name and
    # formatting tokens, so clients still get structured rendering.
    top1_profile_tokens: list[ResolvedToken] | None = None
    if extended and top1_bio:
        if discogs_cache is not None:
            try:
                top1_profile_tokens = await parse_async(top1_bio, CachedOnlyResolver(discogs_cache))
            except Exception:
                logger.exception("Cache-only bio parse failed; falling back to sync parse")
                top1_profile_tokens = parse(top1_bio)
        else:
            top1_profile_tokens = parse(top1_bio)

    async def enrich_one(
        item: LibraryItem,
        artwork: DiscogsSearchResult | None,
        *,
        is_top1: bool,
    ) -> tuple[LibraryItem, DiscogsSearchResult | None]:
        # ``row_artist`` (not ``artist``) — the outer parameter ``artist``
        # carries the request artist used by the LML#504 gate; shadowing it
        # here would silently break the gate for any future modification
        # that reaches for the request-side value.
        row_artist = item.alternate_artist_name or item.artist or ""
        search_term = song or item.title or ""

        # LML#477: only trust the library row when its title plausibly
        # matches the requested album. ``_fuzzy_search`` in library/db.py
        # accepts token_set_ratio >= 70 — permissive enough to surface
        # sibling-album rows (same artist, different release) whose
        # verified streaming URLs (and Discogs artwork) would otherwise
        # propagate as if they pointed at the requested album. The 80
        # floor mirrors ``is_acceptable_match`` in
        # ``clients/streaming/matching``. When no album was requested
        # (artist-only lookup) or the row's title is missing, there is
        # no signal to gate against — fall through.
        row_title_matches_requested_album = (
            not album
            or not item.title
            or score_match(album, item.title) >= SCORE_MATCH_ACCEPTANCE_FLOOR
            # LML#628: a row-less carry-through item (id == ROWLESS_LIBRARY_ID,
            # carrying an already-validated Discogs release) has no library row,
            # so the LML#487 sibling-leak concern — a *row's* artwork/streaming
            # links bleeding onto a mismatched album — cannot apply. The
            # carry-through resolves by *track*, so its release title (``item.title``)
            # routinely differs from a typed album; gating it here clobbered the
            # validated ``release_id`` down to the BS#1185 ``release_id=0`` sentinel
            # the feature exists to avoid. The release_id was validated to carry the
            # track, and item.title IS that release's title, so trust the binding.
            or (item.id == ROWLESS_LIBRARY_ID and artwork is not None and artwork.release_id > 0)
            # LML#684: an in-library found_on_compilation row is the analog of the
            # row-less carry-through above — TRACK_ON_COMPILATION located the track
            # on a release that IS shelved, and validate_release_for_track confirmed
            # the track sits on it. Its release title ("Orcutt-Shelley-Miller")
            # legitimately differs from the typed album (the trio/collab name
            # "Orcutt Shelley Miller", which scores 61.9 here), so the sibling-leak
            # gate would clobber the validated release_id to the release_id=0
            # sentinel — the silent-no-artwork bug this fix exists to kill. The row
            # was track-validated, so the leak concern doesn't apply.
            or (found_on_compilation and artwork is not None and artwork.release_id > 0)
        )

        # LML#487: the library row is "acceptable" (a real match for the
        # requested album) only when it carries Discogs artwork AND
        # clears the title gate. Otherwise the row's Discogs artwork /
        # release-year would be a sibling-album leak (Noura Mint Seymali
        # Tzenni-vs-Yenbett shape) — same risk as the PR #481 streaming-
        # link leak, just on a different field. When not acceptable, we
        # synthesize a streaming-only result (LML#401 / BS#1185 sentinel
        # contract) and try the Apple Music external probe to surface
        # the *right* album's artwork.
        library_row_acceptable = artwork is not None and row_title_matches_requested_album

        # Hoist the librarian-curated streaming_links override BEFORE the
        # Apple Music probe so the happy-path probe can short-circuit when
        # the override would win anyway (the final assignment is
        # ``apple_music_override or apple_music_url or None``). Saves one
        # Apple Music quota slot + up to apple_music_lookup_timeout_s() of
        # wall-clock per overridden item. The override gate still requires
        # row_title_matches_requested_album per PR #481 (LML#477).
        spotify_url = None
        apple_music_override = None
        youtube_music_url = None
        bandcamp_url = None
        soundcloud_url = None

        if (
            library_db
            and getattr(library_db, "_has_streaming_links", None) is True
            and item.id
            and row_title_matches_requested_album
        ):
            try:
                links = await library_db.get_streaming_links(item.id)
            except Exception:
                links = None
            if links:
                spotify_url = links.get("spotify_url")
                apple_music_override = links.get("apple_music_url")
                youtube_music_url = links.get("youtube_music_url")
                bandcamp_url = links.get("bandcamp_url")
                soundcloud_url = links.get("soundcloud_url")

        # Apple Music probe. ``library_row_acceptable`` picks ``find_track_url``
        # (URL only — preserves LML#401 baseline); the synthesis path
        # (LML#487) needs artwork + year too, so calls ``find_track_metadata``.
        # Both make one ``search_song`` call, so per-request quota is identical
        # to the LML#401 baseline. Skip the happy-path probe when the
        # librarian override would win anyway (saves one Apple call per
        # overridden item). The ``asyncio.wait_for`` ceiling caps single-call
        # latency under 429/5xx pressure (LML#449/#450). On timeout/error we
        # degrade to no-Apple-anything (LML#444 swallow). The Sentry data key
        # encodes the *method* so LML#462 dashboards can distinguish synthesis-
        # path timeouts (artwork still leaking, high impact) from happy-path
        # timeouts (URL only, low impact).
        apple_music_url: str | None = None
        probe_match: AppleMusicTrackMatch | None = None
        probe_artwork_url: str | None = None
        probe_release_year: int | None = None
        probe_method = "find_track_metadata" if not library_row_acceptable else "find_track_url"
        skip_happy_probe = library_row_acceptable and apple_music_override
        if apple_music is not None and row_artist and search_term and not skip_happy_probe:
            try:
                if library_row_acceptable:
                    apple_music_url = await asyncio.wait_for(
                        apple_music.find_track_url(row_artist, search_term, album=album),
                        timeout=apple_music_lookup_timeout_s(),
                    )
                else:
                    probe_match = await asyncio.wait_for(
                        apple_music.find_track_metadata(row_artist, search_term, album=album),
                        timeout=apple_music_lookup_timeout_s(),
                    )
                    if probe_match is not None:
                        apple_music_url = probe_match.url
                        probe_artwork_url = probe_match.artwork_url
                        probe_release_year = probe_match.release_year
            except TimeoutError:
                logger.warning(
                    "AppleMusicClient.%s timed out for %s - %s",
                    probe_method,
                    row_artist,
                    search_term,
                )
                # LML#462: project onto the active Sentry transaction so the
                # trace explorer can distinguish "Apple Music timed out" from
                # "Apple Music never ran" — the cancelled inner
                # `apple_music.search` span never sets its `result` data, so
                # without this marker the timeout shape is queryably
                # indistinguishable from a no-op. The legacy key stays for
                # dashboard continuity; the ``.method`` key disambiguates
                # synthesis-path timeouts from happy-path timeouts.
                try:
                    transaction = sentry_sdk.get_current_scope().transaction
                    if transaction is not None:
                        transaction.set_data("apple_music.find_track_url.timeout", True)
                        transaction.set_data("apple_music.timeout.method", probe_method)
                except Exception as e:
                    logger.warning(
                        "Failed to project apple_music timeout onto Sentry transaction: %s",
                        e,
                    )
            except Exception:
                logger.exception(
                    "AppleMusicClient.%s raised for %s - %s",
                    probe_method,
                    row_artist,
                    search_term,
                )

        # LML#505: post-hoc invalidation of sibling-row override URLs on
        # the synthesis branch. The LML#477 title gate
        # (``row_title_matches_requested_album``) clears on Deluxe /
        # Remaster / Reissue / Bonus / Limited / Expanded / Anniversary
        # suffixes because ``clients/streaming/matching.score_match``
        # strips the parenthetical before scoring —
        # ``score_match("Album X", "Album X (Deluxe Edition)") == 100.0``.
        # So a library row for the sibling original propagates its five
        # curated streaming URLs through the override block above when
        # the request is for the Deluxe. When Discogs lacks the Deluxe
        # (synthesis branch, ``not library_row_acceptable``) the Apple
        # probe fetches the *requested* album — ``find_track_metadata``
        # enforces ``album_score >= 80`` at
        # ``clients/streaming/apple_music.py:407-412`` — proving the
        # row's URLs are for a sibling, not the request. Clear them so
        # the precedence at the final ``update`` assignment lets the
        # probe URL win the Apple slot and the other four services
        # downgrade to ``_build_streaming_search_url`` placeholders
        # instead of leaking the wrong release to iOS / dj-site.
        #
        # The ``album`` and ``item.title`` guards exclude two paths the
        # collapse-to-``probe_match is not None`` rule mishandles:
        # artist-only lookups (``album=None`` → probe ran without the
        # ``album_score`` floor, so a probe match does not imply
        # override is wrong) and title-less rows (no row title to be
        # 'wrong' against). Both retain the override; the title-less
        # branch is a latent leak tracked as an open question on LML#505
        # and explicitly out of scope here.
        if not library_row_acceptable and probe_match is not None and album and item.title:
            apple_music_override = None
            spotify_url = None
            youtube_music_url = None
            bandcamp_url = None
            soundcloud_url = None

        # Album-derived fields are positionally gated: only on top-1, and
        # only when top-1 actually carries an *acceptable* library row.
        # LML#487 fall-through: when the row is not acceptable, the probe
        # supplies release_year on the synthesized result. The probe
        # already cost zero (same response that produced ``probe_artwork_url``),
        # so the original top1-only positional rationale doesn't apply —
        # surface ``probe_release_year`` whenever the synthesis branch ran.
        is_album_derived_eligible = is_top1 and library_row_acceptable
        year_result = top1_year if is_album_derived_eligible else probe_release_year

        # LML#688: the resolved release's Discogs master_id, gated like the
        # other release-sourced fields (top-1 + acceptable library row). Lets a
        # catalog-popularity caller (Backend) collapse pressings/formats of one
        # logical album by the master. ``None`` when the release has no master
        # (one-offs, self-released) or the top-1 release never resolved. Unlike
        # the extended-only fields below, it rides the album-derived gate alone
        # (not ``extended``): it is a lightweight release-identity integer that
        # the non-extended bulk-drain path also needs to group by.
        master_id_result = (
            top1_release.master_id
            if is_album_derived_eligible and top1_release is not None
            else None
        )

        # LML#504: library-row hop. ``artist_matches_item``
        # (orchestrator.py:527) and ``library/db.py``'s ``_fuzzy_search``
        # both consult ``item.artist`` AND ``item.alternate_artist_name``
        # — the gate mirrors that or it'd suppress bio on rows the
        # library code surfaced via the alternate name (cataloger
        # asymmetry: 'The Black Dog' filed as 'Black Dog Productions'
        # with the canonical form in alternate_artist_name). Computed
        # per-item: the artwork gate at the synth branch below uses
        # this for THIS item's row (each non-top-1 synth item has its
        # own probe artwork to verify), so hoisting to top-1-only would
        # silently suppress every non-top-1 probe artwork.
        library_row_artist_verified = _artist_pair_verified(
            request_artist_stripped, item.artist
        ) or _artist_pair_verified(request_artist_stripped, item.alternate_artist_name)
        # Composite: when the release has no usable artist anchor
        # (``top1_release`` is None OR ``release.artist`` is empty/whitespace),
        # fall through to library-row-only verification. Covers the
        # LML#507 prefetch-skipped case AND the corrupted-release case.
        # When the library row has NO usable artist anchor either (both
        # ``item.artist`` and ``item.alternate_artist_name`` empty — rare
        # but possible per the ``str | None`` schema), fall back to legacy
        # gate semantics rather than over-suppressing.
        library_row_anchor_present = bool(
            (item.artist or "").strip() or (item.alternate_artist_name or "").strip()
        )
        artist_identity_verified = library_row_artist_verified and (
            not release_anchor_present or release_side_artist_verified
        )
        # Rollout scope: the split-gate is opt-in via ``extended=True`` so
        # legacy non-extended consumers (request-o-matic request line,
        # dj-site proxy) stay on the broader ``is_album_derived_eligible``
        # gate. Backend-Service forces ``extended=true`` on every wire
        # call (BS' ``lookup-coordinator.ts``), so the split immediately
        # exercises on all BS write-path traffic (iOS reads + flowsheet
        # writes) without exposing the request-line / picker callers.
        # Additional fallbacks to legacy gate:
        # * empty ``request_artist`` (album-only lookups, ``parsed.artist=None``
        #   at orchestrator.py:888) — first hop would always fail with no
        #   anchor to score against.
        # * empty library-row anchor (corrupted/sparse catalog row) — the
        #   first hop would always fail with no candidate to score against.
        use_split_gate = (
            extended
            and artist_identity_split_enabled
            and bool(request_artist_stripped)
            and library_row_anchor_present
        )
        is_artist_derived_eligible = is_top1 and (
            artist_identity_verified if use_split_gate else library_row_acceptable
        )
        artist_bio = top1_bio if is_artist_derived_eligible else None
        wikipedia_url = top1_wiki if is_artist_derived_eligible else None

        # LML#504 rollout monitor: shadow-mode telemetry whenever the new
        # gate would land bio/wiki on a result where the legacy gate would
        # not (the synth-recovery this ticket exists for) or vice-versa
        # (gate-tightening regressions). Fires *regardless of*
        # ``artist_identity_split_enabled`` so the rollback flag preserves
        # the divergence signal needed to plan re-enablement. Gated on
        # ``extended`` + non-empty ``request_artist`` + library-row anchor
        # present — outside those preconditions the split gate can never
        # apply, so a "divergence" is just the trivial empty-input case
        # and would flood the dashboard with non-actionable noise. Pairs
        # ``set_data`` (queryable) with a 1% sampled INFO log, matching
        # ``_log_track_validation`` / ``_log_resolver_pre_pass``.
        if (
            is_top1
            and extended
            and bool(request_artist_stripped)
            and library_row_anchor_present
            and artist_identity_verified != library_row_acceptable
        ):
            _log_artist_identity_split_gate(
                library_row_acceptable=library_row_acceptable,
                artist_identity_verified=artist_identity_verified,
                library_row_artist_verified=library_row_artist_verified,
                release_side_artist_verified=release_side_artist_verified,
                release_anchor_present=release_anchor_present,
                use_split_gate=use_split_gate,
            )

        # Fall back to search URLs for any service without a direct link.
        # Spotify's templated fallback was deleted in LML#573 — the persistent
        # streaming-URL cache post-process below now backstops spotify_url with
        # a real album page (and mints the identity) instead of a generic search
        # URL. Bandcamp's fallback is DEFERRED past the post-process (LML#573
        # PR-3): the post-process only fires when its URL field is ``None``, so a
        # pre-filled search URL would silently disable the Bandcamp leg — the
        # search URL is applied below, only if the cache/probe leaves it None.
        # YouTube Music / SoundCloud have no album-cache tier, so they keep
        # their pre-post-process templated fallbacks.
        if row_artist and search_term:
            if not youtube_music_url:
                youtube_music_url = _build_streaming_search_url(
                    "https://music.youtube.com/search?q=", row_artist, search_term
                )
            if not soundcloud_url:
                soundcloud_url = _build_streaming_search_url(
                    "https://soundcloud.com/search?q=", row_artist, search_term
                )

        update: dict[str, Any] = {
            "release_year": year_result,
            "master_id": master_id_result,
            "artist_bio": artist_bio,
            "wikipedia_url": wikipedia_url,
            # spotify_url / bandcamp_url are normalized to None (like
            # apple_music_url) so an empty-string streaming_links override
            # (library.db returns the column verbatim, no '' -> None coercion) is
            # treated as "absent" by the post-process active-filter (`is None`).
            # Without this, "" skips the cache/probe leg AND either surfaces
            # straight to the client (spotify has no fallback) or gets a search
            # URL while the leg was skipped (bandcamp's deferred `not …`
            # fallback). youtube / soundcloud aren't post-process services and
            # overwrite "" with a search URL above, so they need no normalization.
            "spotify_url": spotify_url or None,
            "apple_music_url": apple_music_override or apple_music_url or None,
            "youtube_music_url": youtube_music_url,
            "bandcamp_url": bandcamp_url or None,
            "soundcloud_url": soundcloud_url,
        }

        # LML "streaming URLs for non-library albums" (LML#573) — when the
        # existing per-item probe + override couldn't surface a service URL,
        # the polymorphic post-process runs a cache-backed probe per configured
        # service (Apple + Spotify + Bandcamp) with the REQUEST's (artist, album)
        # — not the library row's. Fixes the wrong-fallback-row attack
        # (non-library album like Hyd / "Hold Onto Me Infinity" falls back to a
        # same-titled library row by a different artist, in-line probe runs with
        # the wrong artist name → null). Results persist to
        # ``lml_cache.album_streaming_url_cache`` so future lookups short-circuit
        # the upstream API, and live resolutions mint the parsed ID into
        # ``entity.release_identity``. The ``clients`` dict may carry ``None``
        # values (e.g. Spotify creds unconfigured) — the post-process filters
        # them. Gated by the master + per-service flags.
        await apply_streaming_url_postprocess(
            update,
            clients={"apple_music": apple_music, "spotify": spotify, "bandcamp": bandcamp},
            pg=discogs_cache_pg,
            entity_store=entity_store,
            request_artist=artist,
            request_album=album,
            settings=get_settings(),
        )

        # Bandcamp's templated search-URL fallback, deferred past the
        # post-process (LML#573 PR-3): apply it only if the cache/probe (and any
        # librarian-curated streaming_links override) left bandcamp_url empty, so
        # a resolved album page / direct link always wins over the generic search
        # link. Priority: direct link > cache/probe > search URL.
        if row_artist and search_term and not update["bandcamp_url"]:
            update["bandcamp_url"] = _build_streaming_search_url(
                "https://bandcamp.com/search?q=", row_artist, search_term
            )

        # Extended fields land on the top-1 result only and require artwork
        # (same positional + artwork gating as the album-derived scalars).
        # The non-top-1 items keep their lean shape so non-iOS lookup
        # callers (request line, dj-site proxy, BS catalog) don't pay
        # payload bloat for results they ignore.
        if extended and is_album_derived_eligible:
            if top1_release is not None:
                update["tracklist"] = (
                    list(top1_release.tracklist) if top1_release.tracklist else None
                )
                update["genres"] = list(top1_release.genres) if top1_release.genres else None
                update["styles"] = list(top1_release.styles) if top1_release.styles else None
                update["label"] = top1_release.label
                update["full_release_date"] = top1_release.released
                # BMI songwriter/composer credits (LML#699). Prefer the played
                # track's per-track writers (``provenance="track"``) when ``song``
                # resolves to a tracklist position; otherwise the release-level
                # writer-role subset of the already-fetched ``extra_artists``
                # (``provenance="release"``). Cache-only — the position is scanned
                # over the in-scope ``top1_release`` with no new Discogs call;
                # ``None`` for comps (release-level) / when no writer resolves.
                #
                # Unlike the sibling album-derived fields above, writer credits
                # are *person* attribution consumed for BMI royalty reporting, so
                # they additionally require artist-identity verification
                # (``is_artist_derived_eligible``) — the intersection of the album
                # and artist gates, for BOTH precisions. A fuzzy album-title
                # collision with a *different* artist's release
                # (``library_row_acceptable`` true but the artist gate false) must
                # not leak that artist's composers (release- or track-level) as
                # the played track's writers. No-op when the split gate is off
                # (``is_artist_derived_eligible`` then equals
                # ``library_row_acceptable``); also skips the position scan then.
                if is_artist_derived_eligible:
                    resolved_track_position = (
                        find_track_position(top1_release, song) if song else None
                    )
                    update["writer_credits"] = writer_credits_from_release(
                        top1_release, track_position=resolved_track_position
                    )
                else:
                    update["writer_credits"] = None
            # ``artist_image_url`` stays gated on ``is_album_derived_eligible``
            # despite being artist-scoped: neither wxyc-ios-64 nor
            # wxyc-dj-tool-ios mounts a UI affordance for it
            # (``ArtistMetadata`` at ``Shared/Metadata/Sources/Metadata/
            # PlaycutMetadata.swift`` doesn't carry the field), so surfacing
            # it on the synthesis path would be payload waste. Re-gate when
            # iOS adds an artist-image mount.
            update["artist_image_url"] = (
                top1_details.image_url if top1_details is not None else None
            )

        # ``profile_tokens`` parses from ``top1_bio``; ``discogs_artist_id``
        # IS ``release.artist_id``. Both are strictly artist-scoped, so they
        # ride the artist-identity gate (not the album-derived gate). This
        # keeps the API contract coherent: any response that carries
        # ``artist_bio`` also carries the ``discogs_artist_id`` key that
        # iOS/BS need to key an artist-metadata cache against (see
        # ``generated/api_models.DiscogsMatchResult.discogs_artist_id``).
        if extended and is_artist_derived_eligible:
            update["profile_tokens"] = top1_profile_tokens
            if top1_release is not None:
                update["discogs_artist_id"] = top1_release.artist_id

        if not library_row_acceptable:
            # No acceptable Discogs match: try MusicBrainz for a tracklist
            # before synthesizing the streaming-only result. Same positional
            # gate as the rest of the extended payload — only the top-1 item
            # is eligible, and only when extended mode is on.
            if is_top1 and extended and mb_pg is not None and item.artist and album:
                mb_tracklist = await resolve_tracklist_via_musicbrainz(
                    item.artist, album, mb_pg=mb_pg
                )
                # LML#506 post-rescue song-sanity check. The resolver runs a
                # pg_trgm ``LIMIT 1`` with a lenient 0.70 floor, so on the
                # Deluxe-vs-Original sibling-album shape (long shared
                # substring, both sides clear 0.70) it can return Original's
                # tracklist for a Deluxe request. When the DJ's song doesn't
                # appear in the rescued tracks, the candidate is almost
                # certainly the wrong release; drop it rather than surface a
                # wrong tracklist to the picker (which writes it
                # unchallenged to the flowsheet).
                #
                # Known limitation: only the bonus-only-track variant is
                # caught here. Shared-track-Deluxe leaks (DJ requests a
                # track present on both editions) pass the check
                # undetected. The fix lives in the resolver — top-K
                # candidates filtered by song-presence — and is filed as
                # a follow-up. Telemetry from ``mb_resolver.requested_album``
                # / ``mb_resolver.returned_album`` sizes whether the bigger
                # swing is justified.
                # Strip song upfront so whitespace-only inputs (``song='   '``)
                # follow the same skip path as ``song is None`` — a truthy
                # blank would otherwise enter the check, normalize to empty
                # inside ``score_match_track``, score 0 against every track,
                # and unconditionally drop the rescue. The acceptance floor
                # is the same 80 used across LML#477 / LML#504 / Apple Music
                # probe (``SCORE_MATCH_ACCEPTANCE_FLOOR``); imported rather
                # than re-declared so calibration bumps propagate uniformly.
                song_stripped = (song or "").strip()
                # Pre-strip the song once (loop-invariant) AND verify the
                # post-strip form is non-empty. If the request's song was
                # entirely variant-marker (e.g. ``song="(Live)"`` from a
                # malformed parse), the post-strip form is "" and
                # ``score_match("", t.title)`` returns 0 for every track —
                # falsely dropping the rescue. Treat the all-marker case
                # the same as ``song=None`` / whitespace-only: skip the
                # check rather than emit a misleading rejection.
                song_match_target = strip_track_suffix(song_stripped)
                song_sanity_checked = False
                song_sanity_rejected = False
                if mb_tracklist and song_match_target and _mb_rescue_song_match_required():
                    song_sanity_checked = True
                    # Require a non-empty stripped track title for the
                    # iteration to count as a hit. ``score_match_track("", "")``
                    # returns 100 by rapidfuzz convention, so without this
                    # guard a corrupt MB row with all-empty titles would
                    # falsely pass the check when the query side also
                    # normalizes to empty.
                    if not any(
                        (t.title or "").strip()
                        and score_match_track(song_match_target, t.title)
                        >= SCORE_MATCH_ACCEPTANCE_FLOOR
                        for t in mb_tracklist
                    ):
                        logger.info(
                            "mb_rescue: dropping tracklist for (%r, %r) — song %r "
                            "not in rescued tracks (likely sibling-release leak)",
                            item.artist,
                            album,
                            song_stripped,
                        )
                        mb_tracklist = None
                        song_sanity_rejected = True
                if mb_tracklist:
                    update["tracklist"] = mb_tracklist
                _project_mb_rescue_attrs(
                    attempted=True,
                    tracklist_found=bool(mb_tracklist),
                    song_sanity_checked=song_sanity_checked,
                    song_sanity_rejected=song_sanity_rejected,
                )

            # LML#487: surface the Apple Music probe's artwork URL on the
            # synthesized result. ``probe_artwork_url`` is non-None when
            # ``find_track_metadata`` returned a match clearing the 80/80(/80)
            # floor; ``None`` falls through to the no-artwork shape (legacy
            # LML#401 behaviour preserved when no probe match was found, no
            # Apple credentials configured, or the probe timed out / raised).
            #
            # LML#504: the probe was called with ``row_artist`` — when that
            # disagrees with the request artist (fuzzy-collision library row),
            # the probe returned the WRONG artist's artwork. Gate on the
            # library-row hop of the new predicate so a synth-path lookup
            # that failed artist verification doesn't surface a stranger's
            # cover art. Gated on the same ``use_split_gate`` predicate as
            # ``artist_bio`` / ``wikipedia_url`` so the rollback flag and
            # the extended-only rollout scope apply uniformly to all
            # LML#504-introduced gating — non-extended callers and operators
            # who flip the env-var to false get bit-for-bit legacy
            # LML#487 behavior back.
            if use_split_gate and not library_row_artist_verified:
                update["artwork_url"] = None
            else:
                update["artwork_url"] = probe_artwork_url

            # See docstring's "Behavior change vs. v0.6.0 (LML#401)"
            # section for the BS#1185 sentinel contract.
            return (item, DiscogsSearchResult(release_id=0, release_url="", **update))

        # ``library_row_acceptable`` ⟹ ``artwork is not None`` (by definition
        # on line above). Asserted for mypy narrowing — the runtime cost is
        # nil, and the explicit precondition makes the contract local.
        assert artwork is not None
        return (item, artwork.model_copy(update=update))

    enriched = await asyncio.gather(
        *[
            enrich_one(item, artwork, is_top1=(idx == 0))
            for idx, (item, artwork) in enumerate(items_with_artwork)
        ]
    )

    # Write-path warm: fire-and-forget deep async parse of the top-1 bio
    # using the API-capable resolver, so the PG cache gets populated for
    # `[a…]`/`[r…]`/`[m…]` references. The task is intentionally not
    # awaited — read-path latency is unaffected. The task reference is
    # parked in ``_background_tasks`` so the GC can't reap it mid-flight
    # (asyncio holds only weak refs to tasks).
    #
    # LML#504: don't warm a bio the response itself suppressed. The deep
    # parse fires per-ref Discogs API calls (DiscogsServiceResolver: cache
    # → API → write-back) — wasting those on a bio iOS will never render
    # is pure quota burn. Inspect the top-1 enriched result rather than
    # re-deriving the gate so the warm check is locked in step with the
    # actual surfaced output.
    top1_enriched_result = enriched[0][1] if enriched else None
    top1_bio_surfaced = top1_enriched_result is not None and bool(top1_enriched_result.artist_bio)
    if warm_cache and top1_bio and top1_bio_surfaced:
        task = asyncio.create_task(_warm_bio_cache(top1_bio, discogs_service))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return list(enriched)


async def _warm_bio_cache(bio: str, discogs_service: DiscogsService) -> None:
    """Background task: deep-async parse of an artist bio to warm caches.

    Resolves every `[a<id>]` / `[r<id>]` / `[m<id>]` reference through
    ``DiscogsServiceResolver`` (cache → API → cache write-back), so
    subsequent ``parse_async(..., CachedOnlyResolver)`` calls on this bio
    return typed tokens instead of plain text. Bounded by the module-level
    semaphore to cap concurrent Discogs API amplification under burst
    load. Errors are logged and swallowed — the task must never propagate
    to the event loop. No Sentry tag is set here: the request scope has
    long since closed by the time this runs, so a tag on the active scope
    would land on whatever unrelated request is running next.
    """
    global _warm_cache_semaphore
    if _warm_cache_semaphore is None:
        _warm_cache_semaphore = asyncio.Semaphore(_WARM_CACHE_CONCURRENCY)
    try:
        async with _warm_cache_semaphore:
            await parse_async(bio, DiscogsServiceResolver(discogs_service))
    except Exception:
        logger.exception("Background bio cache-warm failed")


def build_context_message(
    parsed: ParsedRequest,
    found_on_compilation: bool,
    song_not_found: bool,
    has_results: bool = True,
) -> str | None:
    """Build context message based on search results."""
    if found_on_compilation:
        return f'Found "{parsed.song}" by {parsed.artist} on:'

    if song_not_found and has_results:
        if parsed.song and parsed.album:
            return (
                f'"{parsed.album}" not found in the library, '
                f"but here are other albums by {parsed.artist}:"
            )
        elif parsed.song:
            return (
                f'"{parsed.song}" is not on any album in the library, '
                f"but here are some albums by {parsed.artist}:"
            )
    elif song_not_found and not has_results:
        if parsed.song and parsed.artist:
            return f'"{parsed.song}" by {parsed.artist} not found in library.'

    return None


def _identity_to_reconciled(identity: Identity) -> ReconciledIdentity:
    """Convert an EntityStore Identity dataclass to the shared ReconciledIdentity schema."""
    return ReconciledIdentity(
        discogs_artist_id=identity.discogs_artist_id,
        musicbrainz_artist_id=identity.musicbrainz_artist_id,
        wikidata_qid=identity.wikidata_qid,
        spotify_artist_id=identity.spotify_artist_id,
        apple_music_artist_id=identity.apple_music_artist_id,
        bandcamp_id=identity.bandcamp_id,
    )


async def _resolve_identities(
    artist_names: list[str], entity_store: EntityStore
) -> dict[str, ReconciledIdentity]:
    """Look up reconciled identities for unique artist names.

    Returns a dict keyed by the artist name. Names not found in the entity
    store are omitted, so callers should treat a missing key as "no identity."
    Lookups across the unique names run concurrently.
    """
    unique = list({name for name in artist_names if name})
    if not unique:
        return {}

    identities = await asyncio.gather(
        *(entity_store.get_identity(name) for name in unique),
        return_exceptions=True,
    )

    result: dict[str, ReconciledIdentity] = {}
    for name, identity in zip(unique, identities, strict=True):
        if isinstance(identity, BaseException):
            logger.warning("EntityStore.get_identity failed for %r: %s", name, identity)
            continue
        if identity is not None:
            result[name] = _identity_to_reconciled(identity)
    return result


async def perform_lookup(
    request: LookupRequest,
    db: LibraryDB,
    discogs_service: DiscogsService | None,
    telemetry: RequestTelemetry,
    *,
    entity_store: EntityStore | None = None,
    discogs_cache: DiscogsCacheService | None = None,
    mb_pg: PgSourceProtocol | None = None,
    apple_music: AppleMusicClient | None = None,
    spotify: SpotifyClient | None = None,
    bandcamp: BandcampClient | None = None,
    discogs_cache_pg: PgSource | None = None,
    caller_budget_ms: int | None = None,
    allow_release_resolution_fallback: bool = True,
) -> LookupResponse:
    """Orchestrate the full lookup pipeline.

    Steps:
    1. Correct artist spelling
    2. Resolve album names from Discogs (if song provided without album)
    3. Execute search strategy pipeline
    4. Validate fallback results against Discogs tracklists
    5. Fetch artwork for results
    6. Build context message
    """
    # Build a ParsedRequest from the LookupRequest for compatibility with
    # search functions that expect ParsedRequest
    parsed = ParsedRequest(
        song=request.song,
        album=request.album,
        artist=request.artist,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message=request.raw_message,
    )

    library_results: list[LibraryItem] = []
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]] = []
    song_not_found = False
    found_on_compilation = False
    discogs_titles: dict[int, ResolvedRelease] = {}
    corrected_artist: str | None = None

    # Steps 1+2: Correct artist spelling and resolve albums (parallel)
    if parsed.artist:
        correction_task = db.find_similar_artist(parsed.artist)
        with telemetry.track_step("album_lookup"):
            if parsed.song and not parsed.album:
                telemetry.record_api_call("discogs")
            corrected, (albums_for_search, song_not_found) = await asyncio.gather(
                correction_task,
                resolve_albums_for_track(parsed, discogs_service),
            )
        if corrected:
            corrected_artist = corrected
            # Two-channel seam (WXYC/library-metadata-lookup#626): do NOT
            # overwrite ``parsed.artist``. The typed value must keep flowing to
            # every Discogs-facing path (the three Discogs-aware strategies'
            # probes, ``validate_release_for_track``, and the library-miss
            # Discogs probe), or a *distinct* non-library artist one edit from a
            # library name gets snapped into the library's vocabulary on Discogs.
            # Thread the correction on ``library_artist``, read only by the
            # library-side legs (``db.search`` queries + ``artist_matches_item``
            # match-backs).
            parsed.library_artist = corrected
    else:
        with telemetry.track_step("album_lookup"):
            albums_for_search, song_not_found = await resolve_albums_for_track(
                parsed, discogs_service
            )

    # Step 3: Execute search strategy pipeline
    with telemetry.track_step("library_search"):
        # Strategies hold their own ``db`` handle (no per-call db arg on the
        # runner post-#399). The discogs_service is captured via ``partial`` on
        # the strategies that need it; ARTIST_PLUS_ALBUM is the only
        # library-only strategy (SWAPPED_INTERPRETATION now cross-references the
        # non-artist token as a track via Discogs — LML#622).
        # ``discogs_cache_pg`` threads the #632 row-less resolution cache into the
        # three Discogs-aware strategies' A1 carry-through (LML#628). The library
        # channel and ARTIST_PLUS_ALBUM / SONG_AS_ARTIST never touch it.
        # LML#652: the per-request bulk kill switch (``False`` on /lookup/bulk)
        # threads into the four Discogs-aware row-less producers here via the
        # partials, and into the fifth (the A4 cached-track safety net) at its
        # own Step-3b call site below — exactly as it already reaches
        # ``fetch_artwork_for_items`` (Step 4) for #604's lazy fallback. So no
        # row-less item surfaces, nor any carry-through resolve or #632 cache
        # write, on the backfill path.
        strategies = build_strategies(
            db,
            search_library_func=search_library_with_fallback,
            search_alternative_func=partial(
                search_with_alternative_interpretation,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_compilations_func=partial(
                search_compilations_for_track,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_song_as_artist_func=partial(
                search_song_as_artist,
                discogs_service=discogs_service,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
            search_song_as_track_func=partial(
                search_song_as_track,
                discogs_service=discogs_service,
                pg=discogs_cache_pg,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
            ),
        )

        search_state = await execute_search_pipeline(
            parsed=parsed,
            raw_message=request.raw_message or "",
            strategies=strategies,
            albums_for_search=albums_for_search,
            song_not_found=song_not_found,
            caller_budget_ms=caller_budget_ms,
        )

        library_results = limit_results(search_state.results)
        song_not_found = search_state.song_not_found
        found_on_compilation = search_state.found_on_compilation
        discogs_titles = search_state.discogs_titles
        search_type = get_search_type_from_state(search_state)

        if found_on_compilation:
            telemetry.record_api_call("discogs")

    # Step 3a: Library-miss Discogs search (LML#583).
    # When the entire search pipeline returned no library results AND the request
    # carries both artist and album, probe Discogs directly. A confident match
    # (80/80-floor via find_best_typed_match) is synthesized into a
    # LibraryItem(id=0) + DiscogsSearchResult pair and handed directly to Step
    # 4b (enrich_artwork_results) — bypassing fetch_artwork_for_items (which
    # would re-search and risk a second floor mis-selection) and bypassing Step
    # 3b track validation (the synthesized item is already 80-floored on
    # (artist, album) jointly; routing it through an unfloored top-1 re-search
    # has non-zero probability of flipping to a different release).
    #
    # Gated on ``allow_release_resolution_fallback`` (the bulk kill switch, LML#671):
    # /lookup/bulk passes ``False`` so the 35k-album backfill never pays a per-row
    # Discogs ``search()`` here, nor surfaces a row-less ``LibraryItem(id=0)``.
    # An album-only backfill item (artist+album, no song) hits *this* producer —
    # not the five song-bearing LML#652 producers — so gating it is what makes the
    # "no per-row Discogs surfacing/cost on bulk" guarantee true for the real drain.
    # /lookup keeps the default (``True``), so #583 fires there exactly as before.
    library_miss_outcome: str | None = None
    if (
        not library_results
        and allow_release_resolution_fallback
        and parsed.artist
        and parsed.album
        and parsed.album.strip()
    ):
        with telemetry.track_step("library_miss_discogs_search"):
            miss_match = await _library_miss_discogs_search(parsed, discogs_service)
        if miss_match is not None:
            synthesized_lib_item, discogs_result = miss_match
            items_with_artwork = [(synthesized_lib_item, discogs_result)]
            # A synthesized Discogs match resolves the request — clear song_not_found so
            # build_context_message and LookupResponse.song_not_found reflect reality.
            song_not_found = False
            library_miss_outcome = "library_miss_discogs_match"
            telemetry.record_api_call("discogs")
        elif discogs_service is not None:
            # Discogs was available and searched but found no confident match.
            library_miss_outcome = "library_miss_no_discogs_match"

    # Step 3b: Validate results against Discogs track data.
    # Synthesized id=0 items from Step 3a are excluded: library_results is empty
    # on that path, so the gate below never fires. The filter on real_results is
    # a defensive guard for any future path that might add id=0 items to
    # library_results.
    real_results = [r for r in library_results if r.id != 0]
    if real_results and parsed.song and parsed.artist:
        if not found_on_compilation:
            # Normal case: validate all results against Discogs tracklists
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    real_results, parsed.song, parsed.artist, discogs_service
                )
                if validated:
                    library_results = validated
                    song_not_found = False
                elif song_not_found:
                    # Per-result validation confirmed nothing. Ask the local
                    # PG cache directly: "any release by this artist whose
                    # tracklist contains this song?" — and promote the matching
                    # library album. Catches the case where the upstream
                    # track→releases lookup missed a release the cache holds.
                    promoted, promoted_titles = await find_library_albums_with_cached_track(
                        db,
                        parsed.song,
                        parsed.artist,
                        discogs_service,
                        match_artist=library_artist_for(parsed),
                        allow_release_resolution_fallback=allow_release_resolution_fallback,
                    )
                    if promoted:
                        library_results = promoted
                        # A4 (LML#629): a row-less promotion carries its resolved
                        # release on the seam so Step 4 binds discogs_url by id.
                        discogs_titles = {**discogs_titles, **promoted_titles}
                        song_not_found = False
                    else:
                        # Last resort before declaring song-not-found: the
                        # request shape "<album-title>, <artist>" can route to
                        # us as ``song=<album-title>``. If a surviving result's
                        # title clears the floor against ``parsed.song``, the
                        # user wanted that album. Surfacing it as found-the-
                        # album avoids the misleading 'not on any album'
                        # message about a row sitting in the result list.
                        title_matches = _filter_results_by_song_as_album_title(
                            library_results, parsed.song
                        )
                        if title_matches:
                            logger.info(
                                f"Promoted {len(title_matches)} of {len(library_results)} "
                                f"artist-fallback row(s) whose title matches song "
                                f"'{parsed.song}' — treating as album request"
                            )
                            library_results = title_matches
                            song_not_found = False
        elif search_state.artist_fallback_results:
            # Compilation found, but the artist's own album may also contain the track.
            # Validate the artist fallback results (saved before compilation search
            # replaced them) and prepend any confirmed matches.
            with telemetry.track_step("track_validation"):
                validated = await filter_results_by_track_validation(
                    search_state.artist_fallback_results,
                    parsed.song,
                    parsed.artist,
                    discogs_service,
                )
                if validated:
                    compilation_ids = {r.id for r in library_results}
                    merged = [r for r in validated if r.id not in compilation_ids]
                    merged.extend(library_results)
                    library_results = merged

    # Step 3c: Populate streaming status
    if library_results and getattr(db, "_has_streaming_links", None) is True:
        streaming_status = await db.get_streaming_status([r.id for r in library_results])
        for result in library_results:
            result.on_streaming = streaming_status.get(result.id, False)

    # Step 4: Fetch artwork
    with telemetry.track_step("artwork_fetch"):
        if library_results:
            for _ in library_results:
                telemetry.record_api_call("discogs")
            items_with_artwork = await fetch_artwork_for_items(
                library_results,
                discogs_service,
                discogs_titles,
                song=parsed.song,
                album=parsed.album,
                allow_release_resolution_fallback=allow_release_resolution_fallback,
                found_on_compilation=found_on_compilation,
            )

    # The generated LookupRequest models these as bool | None (api.yaml
    # describes them with `default: false` but the field is not in the
    # required set, so callers can omit them). Coerce once and reuse.
    extended_mode = bool(request.extended)
    warm_cache_mode = bool(request.warm_cache)

    # Step 4b: Enrich with release year, artist details, streaming links
    with telemetry.track_step("metadata_enrichment"):
        if items_with_artwork:
            items_with_artwork = await enrich_artwork_results(
                items_with_artwork,
                discogs_service,
                song=parsed.song,
                album=parsed.album,
                artist=parsed.artist,
                library_db=db,
                extended=extended_mode,
                warm_cache=warm_cache_mode,
                discogs_cache=discogs_cache,
                mb_pg=mb_pg,
                apple_music=apple_music,
                spotify=spotify,
                bandcamp=bandcamp,
                entity_store=entity_store,
                discogs_cache_pg=discogs_cache_pg,
                found_on_compilation=found_on_compilation,
            )

    # Project the request-side flags and result-quality signals onto the
    # active Sentry transaction so the trace can be filtered by request mode
    # (lml.lookup.extended, lml.lookup.warm_cache) and by match outcome
    # (lookup.results_count, lookup.match_type — LML#158). Mirrors the
    # cache_stats projection pattern (LML#213).
    try:
        scope = sentry_sdk.get_current_scope()
        if scope.transaction is not None:
            scope.transaction.set_data("lml.lookup.extended", extended_mode)
            scope.transaction.set_data("lml.lookup.warm_cache", warm_cache_mode)
            scope.transaction.set_data(
                "lookup.results_count",
                len(items_with_artwork) if items_with_artwork else len(library_results),
            )
            scope.transaction.set_data("lookup.match_type", search_type)
            if library_miss_outcome is not None:
                scope.transaction.set_data("lookup.outcome", library_miss_outcome)
    except Exception:
        # Observability must not break the request path.
        pass

    # Step 5: Build context message
    context = build_context_message(
        parsed,
        found_on_compilation,
        song_not_found,
        has_results=bool(library_results) or bool(items_with_artwork),
    )

    # Step 6: Resolve external identifiers for each result's artist.
    # Collect artist names from library_results (normal path) and from
    # items_with_artwork (synthesized Step 3a path, where library_results is empty
    # but the synthesized LibraryItem carries a real artist name worth resolving).
    identities_by_artist: dict[str, ReconciledIdentity] = {}
    _identity_artist_names = [item.artist for item in library_results if item.artist] + [
        item.artist for item, _ in items_with_artwork if item.id == 0 and item.artist
    ]
    if entity_store is not None and _identity_artist_names:
        with telemetry.track_step("identity_resolution"):
            identities_by_artist = await _resolve_identities(_identity_artist_names, entity_store)

    def _identity_for(item: LibraryItem) -> ReconciledIdentity | None:
        if not item.artist:
            return None
        return identities_by_artist.get(item.artist)

    # Build response (convert internal models to API contract models)
    matched_via_by_id = search_state.matched_via_by_id
    result_items = []
    if items_with_artwork:
        for item, artwork in items_with_artwork:
            # Synthesized items (id=0, from Step 3a) have no library call-number
            # components; build LibraryCatalogItem directly with the "(external)"
            # sentinel that Backend-Service already understands (same contract as
            # the Step 7 include_external_caches path).
            catalog_item = (
                LibraryCatalogItem(
                    id=0,
                    artist=item.artist,
                    title=item.title,
                    call_number="(external)",
                    library_url="",
                )
                if item.id == 0
                else item.to_catalog_item()
            )
            result_items.append(
                LookupResultItem(
                    library_item=catalog_item,
                    artwork=artwork.to_match_result() if artwork else None,
                    reconciled_identity=_identity_for(item),
                    # Synthesized items (id=0) carry no track-title-provenance hint;
                    # do not look up key 0 in matched_via_by_id to prevent accidental
                    # collision with any future strategy that might write to that key.
                    matched_via=None if item.id == 0 else matched_via_by_id.get(item.id),
                )
            )
    elif library_results:
        for item in library_results:
            result_items.append(
                LookupResultItem(
                    library_item=item.to_catalog_item(),
                    reconciled_identity=_identity_for(item),
                    matched_via=matched_via_by_id.get(item.id),
                )
            )

    # Step 7: External-cache fallback (Phase 1.5 + 1.7 mojibake recovery).
    # Opt-in via include_external_caches. The lossy-mojibake matcher sends
    # column-typed bodies, so we dispatch by which skeleton field is set:
    # artist takes precedence (highest-precision lookup), then album, then
    # song. A bare raw_message with no typed field skips the fallback —
    # LABEL_NAME is too noisy to be useful here.
    external_source: str | None = "library" if result_items else None
    if not result_items and request.include_external_caches:
        candidates: list[dict[str, Any]] = []
        source: str | None = None
        if parsed.artist:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_artists(
                    parsed.artist,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["name"], "title": ""} for r in rows]
        elif parsed.album:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_albums(
                    parsed.album,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["artist"], "title": r["title"]} for r in rows]
        elif parsed.song:
            with telemetry.track_step("external_cache_fallback"):
                rows, source = await search_external_tracks(
                    parsed.song,
                    discogs_cache=discogs_cache,
                    mb_pg=mb_pg,
                )
            candidates = [{"artist": r["artist"], "title": r["title"]} for r in rows]

        if candidates:
            external_source = source
            for candidate in candidates:
                result_items.append(
                    LookupResultItem(
                        library_item=LibraryCatalogItem(
                            id=0,
                            artist=candidate["artist"],
                            title=candidate["title"] or None,
                            call_number="(external)",
                            library_url="",
                        ),
                    )
                )

    return LookupResponse(
        results=result_items,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        corrected_artist=corrected_artist,
        external_source=external_source,
        timeout=search_state.timed_out,
    )
