"""Per-item enrichment (LML#730).

The former ``enrich_one`` closure from ``enrich_artwork_results``, un-nested
into a module function: the LML#477/#487 library-row gates, the Apple Music
probe, streaming-URL assignment (override → probe → post-process → search-URL
fallback), the ``extended=True`` payload, and the LML#401/BS#1185
streaming-only synthesis. Request-constant inputs arrive on the frozen
``EnrichmentContext``; per-request values derived by the coordinator after
the top-1 fetch (the ``top1_*`` payload and the LML#504 release-side
verification flags) arrive as explicit keyword parameters.
"""

import logging
from typing import Any
from urllib.parse import quote

from clients.streaming.matching import (
    SCORE_MATCH_ACCEPTANCE_FLOOR,
    score_match,
    score_match_track,
    strip_track_suffix,
)
from config.settings import get_settings
from discogs.models import (
    ArtistDetails,
    DiscogsSearchResult,
    ReleaseMetadataResponse,
    ResolvedToken,
)
from discogs.service import find_track_position
from discogs.writer_roles import writer_credits_from_release
from library.models import LibraryItem
from lookup.artist_resolution import (
    _artist_pair_verified,
    _log_artist_identity_split_gate,
    _mb_rescue_song_match_required,
    _project_mb_rescue_attrs,
)
from lookup.enrichment.apple_probe import run_apple_music_probe
from lookup.enrichment.bandcamp_probe import probe_owns_bandcamp_leg, run_bandcamp_live_probe
from lookup.enrichment.context import EnrichmentContext
from lookup.enrichment.streaming_status import resolve_streaming_status
from lookup.rowless import (
    ROWLESS_LIBRARY_ID,
)
from lookup.streaming_url_postprocess import apply_streaming_url_postprocess
from release.apple_music_url_parser import url_has_apple_music_host
from release.musicbrainz_resolver import resolve_tracklist_via_musicbrainz
from release.spotify_url_parser import url_has_spotify_host
from streaming.service import StreamingService

logger = logging.getLogger(__name__)


def _build_streaming_search_url(base: str, artist: str, term: str) -> str:
    """Build a streaming service search URL from artist + song/album."""
    query = f"{artist} {term}" if term else artist
    return f"{base}{quote(query)}"


def compute_row_title_matches_requested_album(
    album: str | None,
    item: LibraryItem,
    artwork: DiscogsSearchResult | None,
    *,
    found_on_compilation: bool,
) -> bool:
    """LML#477: does ``item``'s catalog title plausibly match the requested album?

    Extracted (LML#507) so the per-item synthesis gate in ``enrich_one`` and
    the top-1-only prefetch-skip gate in
    ``lookup/enrichment/__init__.py::enrich_artwork_results`` share one
    implementation instead of two copies that can drift.

    ``_fuzzy_search`` in library/db.py accepts token_set_ratio >= 70 —
    permissive enough to surface sibling-album rows (same artist, different
    release) whose verified streaming URLs (and Discogs artwork) would
    otherwise propagate as if they pointed at the requested album. The 80
    floor mirrors ``is_acceptable_match`` in ``clients/streaming/matching``.
    When no album was requested (artist-only lookup) or the row's title is
    missing, there is no signal to gate against — fall through.
    """
    return (
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


async def enrich_one(
    ctx: EnrichmentContext,
    item: LibraryItem,
    artwork: DiscogsSearchResult | None,
    *,
    is_top1: bool,
    top1_year: int | None,
    top1_bio: str | None,
    top1_wiki: str | None,
    top1_release: ReleaseMetadataResponse | None,
    top1_details: ArtistDetails | None,
    top1_profile_tokens: list[ResolvedToken] | None,
    release_side_artist_verified: bool,
    release_anchor_present: bool,
) -> tuple[LibraryItem, DiscogsSearchResult | None]:
    """Enrich one (library item, artwork) pair; see ``enrich_artwork_results``.

    Returns the pair with the artwork result carrying streaming URLs and —
    on the top-1 item, per the positional gates — the album/artist-derived
    scalars and the ``extended`` payload. When the library row is not
    acceptable, returns the LML#401 synthesized streaming-only result
    (``release_id=0`` BS#1185 sentinel) instead.
    """
    # ``row_artist`` (not ``ctx.artist``) — the context fields ``ctx.artist``
    # / ``ctx.request_artist_stripped`` carry the request artist the LML#504
    # gate scores against; conflating the row-side and request-side values
    # here would silently break the gate for any future modification that
    # reaches for the request-side value.
    row_artist = item.alternate_artist_name or item.artist or ""
    search_term = ctx.song or item.title or ""

    # LML#477: only trust the library row when its title plausibly matches
    # the requested album. Rationale + the ROWLESS_LIBRARY_ID / #684
    # carve-outs live on ``compute_row_title_matches_requested_album``
    # (LML#507 extracted it so this per-item gate and the top-1-only
    # prefetch-skip gate in ``lookup/enrichment/__init__.py`` share one
    # implementation).
    row_title_matches_requested_album = compute_row_title_matches_requested_album(
        ctx.album, item, artwork, found_on_compilation=ctx.found_on_compilation
    )
    # LML#850: a hand-verified library-release override (bound in ``fetch_one``
    # via ``release_overrides``) deliberately gets NO carve-out here. This gate
    # scores the request album against the library ROW's catalog title
    # (``item.title``), so a genuine library-album lookup — the dj-site picker
    # and BS flowsheet-linkage both send request == row title — passes it
    # naturally and the pinned release's tracklist surfaces. A carve-out would
    # only change the case where the request album diverges from the surfaced
    # row's title (the sibling-leak shape), where collapsing to the sentinel is
    # the correct, safe behavior: trusting the pin there would leak not just the
    # release but the row's curated ``streaming_links`` (gated on the same flag
    # below, PR#481) onto a mismatched-album request.

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
        ctx.library_db
        and getattr(ctx.library_db, "_has_streaming_links", None) is True
        and item.id
        and row_title_matches_requested_album
    ):
        try:
            links = await ctx.library_db.get_streaming_links(item.id)
        except Exception:
            links = None
        if links:
            spotify_url = links.get("spotify_url")
            apple_music_override = links.get("apple_music_url")
            youtube_music_url = links.get("youtube_music_url")
            bandcamp_url = links.get("bandcamp_url")
            soundcloud_url = links.get("soundcloud_url")

            # LML#873: the streaming-links reconciliation pipeline sometimes
            # stores a non-Spotify (Deezer/Apple/Bandcamp) URL under the
            # spotify_url column, and likewise a non-Apple URL under
            # apple_music_url. Enforce the field-name/host invariant here,
            # before either value can propagate to a response — a mismatched
            # host is treated the same as "no override", not surfaced under
            # the wrong field.
            if spotify_url and not url_has_spotify_host(spotify_url):
                spotify_url = None
            if apple_music_override and not url_has_apple_music_host(apple_music_override):
                apple_music_override = None

    # LML#1101: the inline Apple Music live probe (bounded, L1-cache-first;
    # apple_probe.py). Extracted from here for the same reason LML#1098's
    # Bandcamp probe was — item.py's module-size budget — leaving enrich_one
    # with two sibling probe calls instead of one longhand block and one call.
    # See apple_probe.py's module docstring for why the two probes are sibling
    # modules rather than rows in a config-driven engine.
    settings = get_settings()
    (
        apple_music_url,
        apple_status,
        probe_match,
        probe_artwork_url,
        probe_release_year,
    ) = await run_apple_music_probe(
        ctx,
        settings=settings,
        row_artist=row_artist,
        search_term=search_term,
        library_row_acceptable=library_row_acceptable,
        apple_music_override=apple_music_override,
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
    # (synthesis branch, ``not library_row_acceptable``) an
    # ``album_verified`` Apple probe match proves the *requested*
    # album exists on Apple — ``find_track_metadata`` sets the flag
    # only when the winner cleared the ``album_score >= 80`` floor
    # against the supplied album — so the row's URLs must be for a
    # sibling, not the request. Clear them so the precedence at the
    # final ``update`` assignment lets the probe URL win the Apple
    # slot and the other four services downgrade to
    # ``_build_streaming_search_url`` placeholders instead of
    # leaking the wrong release to iOS / dj-site.
    #
    # ``album_verified`` is the load-bearing guard: it excludes the
    # paths a collapse-to-``probe_match is not None`` rule
    # mishandles. Artist-only lookups (``album=None``) never ran the
    # ``album_score`` floor, and LML#782 album-fallback winners
    # FAILED it — Apple simply titles the album differently than the
    # catalog (Friko "RED XEROX" vs Apple "Get Numb to It!"), so the
    # row may well be the requested album and its curated URLs
    # correct. Both carry ``album_verified=False`` and retain the
    # override. ``ctx.album`` stays as defense-in-depth for the
    # artist-only path (a verified match implies an album was
    # supplied, so the conjunct is redundant when the flag honors
    # its contract). ``item.title`` excludes title-less rows (no row
    # title to be 'wrong' against); that branch is a latent leak
    # tracked as an open question on LML#505 and explicitly out of
    # scope here.
    if (
        not library_row_acceptable
        and probe_match is not None
        and probe_match.album_verified
        and ctx.album
        and item.title
    ):
        apple_music_override = None
        spotify_url = None
        youtube_music_url = None
        bandcamp_url = None
        soundcloud_url = None

    # LML#1098: inline Bandcamp live probe (bounded, cache-first; bandcamp_probe.py).
    bandcamp_url, bandcamp_status = await run_bandcamp_live_probe(
        ctx, settings=settings, current_bandcamp_url=bandcamp_url, is_top1=is_top1
    )
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
        top1_release.master_id if is_album_derived_eligible and top1_release is not None else None
    )

    # LML#504: library-row hop. ``artist_matches_item`` (in
    # ``lookup/matching.py``) and ``library/db.py``'s ``_fuzzy_search``
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
        ctx.request_artist_stripped, item.artist
    ) or _artist_pair_verified(ctx.request_artist_stripped, item.alternate_artist_name)
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
    # * empty ``request_artist`` (album-only lookups — the spine in
    #   ``lookup/orchestrator.py`` passes ``artist=parsed.artist``, which
    #   album-only requests parse with no artist) — first hop would
    #   always fail with no anchor to score against.
    # * empty library-row anchor (corrupted/sparse catalog row) — the
    #   first hop would always fail with no candidate to score against.
    use_split_gate = (
        ctx.extended
        and ctx.artist_identity_split_enabled
        and bool(ctx.request_artist_stripped)
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
    # ``ctx.artist_identity_split_enabled`` so the rollback flag preserves
    # the divergence signal needed to plan re-enablement. Gated on
    # ``extended`` + non-empty ``request_artist`` + library-row anchor
    # present — outside those preconditions the split gate can never
    # apply, so a "divergence" is just the trivial empty-input case
    # and would flood the dashboard with non-actionable noise. Pairs
    # ``set_data`` (queryable) with a 1% sampled INFO log, matching
    # ``_log_track_validation`` / ``_log_resolver_pre_pass``.
    if (
        is_top1
        and ctx.extended
        and bool(ctx.request_artist_stripped)
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
    postprocess_status = await apply_streaming_url_postprocess(
        update,
        clients={
            "apple_music": ctx.apple_music,
            "spotify": ctx.spotify,
            # LML#1098: withhold only on a SOURCED verdict (FIX 3, LML#1106 review).
            "bandcamp": None if probe_owns_bandcamp_leg(bandcamp_status) else ctx.bandcamp,
        },
        pg=ctx.discogs_cache_pg,
        entity_store=ctx.entity_store,
        request_artist=ctx.artist,
        request_album=ctx.album,
        settings=settings,
        # Rowless (non-library) items are the only ones eligible for the
        # bulk-path Bandcamp warm exemption (LML#1087): library rows are already
        # warmed by the offline #1069 drain, so warming them at runtime (1 req/s)
        # would re-introduce the serialized-backfill starvation the bulk
        # suppression prevents. Consulted only under bulk suppression, for
        # Bandcamp; inert on the interactive path.
        is_rowless=item.id == ROWLESS_LIBRARY_ID,
    )

    # LML#1053: resolve the final per-service verdict — this leg's own
    # signals (override precedence + the Apple probe outcome) merged with the
    # post-process's (authoritative for any service IT consulted). Captured
    # here, BEFORE the deferred Bandcamp search-URL fallback below, so a
    # generic search page can never masquerade as "verified". See
    # ``lookup/enrichment/streaming_status.py`` for the merge rule.
    # LML#1101: per-service maps, not per-service parameters. ``verified_urls``
    # carries only the URL that FORCES ``verified`` for each service — for
    # Apple the librarian override alone (a probe-resolved URL reports itself
    # through ``apple_status``), for Spotify the override alone (no probe leg),
    # for Bandcamp the post-probe slot (LML#1098's probe writes its resolved
    # URL back there and reports ``verified`` alongside it). A new probing
    # service is a new key here and no signature change in streaming_status.py.
    update["streaming_status"] = resolve_streaming_status(
        verified_urls={
            StreamingService.APPLE_MUSIC: apple_music_override,
            StreamingService.SPOTIFY: spotify_url,
            StreamingService.BANDCAMP: bandcamp_url,
        },
        probe_status={
            StreamingService.APPLE_MUSIC: apple_status,
            StreamingService.BANDCAMP: bandcamp_status,
        },
        postprocess_status=postprocess_status,
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
    if ctx.extended and is_album_derived_eligible:
        if top1_release is not None:
            update["tracklist"] = list(top1_release.tracklist) if top1_release.tracklist else None
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
                    find_track_position(top1_release, ctx.song) if ctx.song else None
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
        update["artist_image_url"] = top1_details.image_url if top1_details is not None else None

    # ``profile_tokens`` parses from ``top1_bio``; ``discogs_artist_id``
    # IS ``release.artist_id``. Both are strictly artist-scoped, so they
    # ride the artist-identity gate (not the album-derived gate). This
    # keeps the API contract coherent: any response that carries
    # ``artist_bio`` also carries the ``discogs_artist_id`` key that
    # iOS/BS need to key an artist-metadata cache against (see
    # ``generated/api_models.DiscogsMatchResult.discogs_artist_id``).
    if ctx.extended and is_artist_derived_eligible:
        update["profile_tokens"] = top1_profile_tokens
        if top1_release is not None:
            update["discogs_artist_id"] = top1_release.artist_id

    if not library_row_acceptable:
        # No acceptable Discogs match: try MusicBrainz for a tracklist
        # before synthesizing the streaming-only result. Same positional
        # gate as the rest of the extended payload — only the top-1 item
        # is eligible, and only when extended mode is on.
        if is_top1 and ctx.extended and ctx.mb_pg is not None and item.artist and ctx.album:
            mb_tracklist = await resolve_tracklist_via_musicbrainz(
                item.artist, ctx.album, mb_pg=ctx.mb_pg
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
            song_stripped = (ctx.song or "").strip()
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
                        ctx.album,
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

        # See the ``enrich_artwork_results`` docstring's "Behavior change
        # vs. v0.6.0 (LML#401)" section for the BS#1185 sentinel contract.
        return (item, DiscogsSearchResult(release_id=0, release_url="", **update))

    # ``library_row_acceptable`` ⟹ ``artwork is not None`` (by definition
    # on line above). Asserted for mypy narrowing — the runtime cost is
    # nil, and the explicit precondition makes the contract local.
    assert artwork is not None
    return (item, artwork.model_copy(update=update))
