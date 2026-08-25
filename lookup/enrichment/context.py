"""Frozen per-request context for the enrichment pipeline (LML#730).

``EnrichmentContext`` makes explicit what the two former nested closures in
``enrich_artwork_results`` (``fetch_top1_release_details`` and ``enrich_one``)
captured implicitly: the service handles and the request-scoped scalars that
stay constant across every item in the batch. The coordinator in
``lookup/enrichment/__init__.py`` builds one instance per request and passes
it to ``lookup.enrichment.top1.fetch_top1_release_details`` and
``lookup.enrichment.item.enrich_one``.

Post-top-1-fetch *derived* values (the top-1 release payload, the LML#504
release-side verification flags, the pre-parsed profile tokens) are computed
by the coordinator after the top-1 fetch and passed to ``enrich_one`` as
explicit keyword parameters — they cannot live here because the frozen
context is constructed before that fetch runs. That is the boundary:
anything derivable at construction time belongs on the context (e.g.
``request_artist_stripped``, ``artist_identity_split_enabled``); only
fetch-dependent values ride as kwargs.

Two ``enrich_artwork_results`` parameters are deliberately absent because
neither closure captured them; both are consumed only by coordinator-inline
code in ``__init__.py``:

- ``discogs_cache`` — read only by the cache-only top-1 bio parse.
- ``warm_cache`` — read only by the fire-and-forget warm-schedule gate.
"""

from dataclasses import dataclass

from clients.bandcamp import BandcampClient
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.spotify import SpotifyClient
from clients.streaming.youtube_music import YouTubeMusicClient
from discogs.service import DiscogsService
from entity.sources import PgSource, PgSourceProtocol
from entity.store import EntityStore
from library.db import LibraryDB
from lookup.spine_deadline import SpineDeadline


@dataclass(frozen=True)
class EnrichmentContext:
    """Read-only bundle of service handles + request scalars for one enrichment pass.

    Service handles are ``None`` when the corresponding backend is
    unconfigured (each consumer degrades per its own gating), except
    ``discogs_service``: the coordinator returns early before constructing
    the context when it is unset, so it is always present here.
    """

    # --- Service handles ---
    discogs_service: DiscogsService
    """Discogs API/cache facade; drives the top-1 release + artist fetch."""

    mb_pg: PgSourceProtocol | None
    """MusicBrainz cache PG source for the extended-mode tracklist rescue."""

    apple_music: AppleMusicClient | None
    """Authenticated Apple Music client for the per-item URL/metadata probe (LML#443)."""

    spotify: SpotifyClient | None
    """Spotify client for the streaming-URL post-process (LML#573)."""

    bandcamp: BandcampClient | None
    """Bandcamp client for the streaming-URL post-process (LML#573)."""

    youtube_music: YouTubeMusicClient | None
    """YouTube Music client for the streaming-URL post-process (LML#1103).

    Warm-only: unlike ``bandcamp`` there is no inline-probe consumer, so this
    handle is read at exactly one site -- ``enrich_one``'s ``clients`` dict."""

    entity_store: EntityStore | None
    """Release-identity mint target for live streaming-URL resolutions."""

    discogs_cache_pg: PgSource | None
    """Discogs-cache PG source backing ``lml_cache.album_streaming_url_cache``."""

    library_db: LibraryDB | None
    """Library SQLite handle; supplies the librarian-curated streaming_links override."""

    # --- Request scalars ---
    song: str | None
    """Requested song/track title, when the caller parsed one."""

    album: str | None
    """Requested album title; anchor for the LML#477 sibling-leak title gate."""

    artist: str | None
    """Request-side artist (not the library row's); anchor for the LML#504 gate."""

    request_artist_stripped: str
    """``artist`` with surrounding whitespace stripped (``""`` when absent) — the
    exact form the LML#504 artist-identity gate scores against. Derived once by
    the coordinator so the gate and the raw ``artist`` reads cannot drift."""

    artist_identity_split_enabled: bool
    """LML#504 split-gate rollout flag, env-read ONCE per request by the
    coordinator — per-item re-reads would break the read-once invariant."""

    extended: bool
    """Extended-payload mode (tracklist, genres, writer credits, profile tokens)."""

    found_on_compilation: bool
    """True when TRACK_ON_COMPILATION track-validated the surfaced release (LML#684)."""

    spine_deadline: SpineDeadline | None = None
    """Request-scoped caller deadline (LML#930). When present, the per-item Apple
    Music probe clamps its fixed 4s ``wait_for`` to the remaining budget
    (``clamp_probe_timeout_s``) so a caller with little budget left doesn't wait
    the full ceiling on one item. ``None`` (e.g. a direct ``enrich_artwork_results``
    caller that predates the deadline) leaves the probe at its unbounded ceiling."""
