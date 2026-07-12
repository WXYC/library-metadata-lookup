"""Top-level resolve() for the ``POST /api/v1/releases/resolve`` endpoint.

Threads:

  paste-URL or (source, id)
        |
        v
  url_parser.parse_url      (pure)
        |
        v
  discogs_resolver.resolve_discogs_release   OR   bandcamp_resolver.resolve_bandcamp_album
        |
        v
  streaming.orchestrator.check_streaming_availability
        |
        v
  identity write-back via EntityStore.upsert_identity
        |
        v
  ReleaseResolveResponse

Each step accumulates ``warnings``. The endpoint always returns 200 with
whatever it managed to put together — the music director's form should be
able to fall back to manual entry without losing the partial prefill.
"""

from __future__ import annotations

import logging
import re

from wxyc_etl.text import is_compilation_artist

from clients.bandcamp import BandcampClient, extract_slug
from clients.streaming.apple_music import AppleMusicClient
from clients.streaming.deezer import DeezerClient
from clients.streaming.spotify import SpotifyClient
from discogs.service import DiscogsService
from entity.store import EntityStore
from release.apple_music_url_parser import apple_album_id_from_url
from release.bandcamp_resolver import resolve_bandcamp_album
from release.discogs_resolver import (
    resolve_discogs_master,
    resolve_discogs_release,
)
from release.models import (
    CanonicalRelease,
    ReleaseIdentifiers,
    ReleaseResolveRequest,
    ReleaseResolveResponse,
)
from release.url_parser import ParsedUrl, parse_url
from streaming.models import SourceMatch, StreamingCheckResponse, StreamingCheckSources
from streaming.orchestrator import check_streaming_availability

logger = logging.getLogger(__name__)


class _ResolverDependencies:
    """Bundle the I/O dependencies the orchestrator needs.

    Kept as a private class so the FastAPI router (and the tests) can build
    one explicitly. We don't use ``functools.partial`` because the streaming
    clients may individually be None when their credentials are unset.
    """

    def __init__(
        self,
        *,
        discogs: DiscogsService,
        bandcamp: BandcampClient,
        spotify: SpotifyClient | None,
        deezer: DeezerClient | None,
        apple_music: AppleMusicClient | None,
        entity_store: EntityStore | None,
    ) -> None:
        self.discogs = discogs
        self.bandcamp = bandcamp
        self.spotify = spotify
        self.deezer = deezer
        self.apple_music = apple_music
        self.entity_store = entity_store


async def resolve_release(
    request: ReleaseResolveRequest,
    deps: _ResolverDependencies,
) -> ReleaseResolveResponse:
    """Resolve a paste-URL or (source, id) into the prefill payload."""
    parsed = _resolve_to_parsed_url(request)
    if parsed is None:
        # Either the URL didn't parse or the explicit (source, id) was malformed.
        # Return 200 with a warning so the form falls back to manual entry; the
        # alternative (HTTPException) hides the partial information from the user.
        return ReleaseResolveResponse(
            source="unknown",
            source_id=request.url or request.id or "",
            canonical=CanonicalRelease(artist="", title=""),
            identifiers=ReleaseIdentifiers(),
            streaming=None,
            warnings=[
                "Could not recognize the URL or source. "
                "Supported: Discogs release URLs (e.g. discogs.com/release/12345) "
                "and Bandcamp album URLs (e.g. artist.bandcamp.com/album/slug)."
            ],
        )

    # Dispatch to the right resolver.
    if parsed.source == "discogs_release":
        discogs_result = await resolve_discogs_release(deps.discogs, parsed.identifier)
        canonical = discogs_result.canonical
        identifiers = discogs_result.identifiers
        warnings = list(discogs_result.warnings)
    elif parsed.source == "discogs_master":
        master_result = await resolve_discogs_master(deps.discogs, parsed.identifier)
        canonical = master_result.canonical
        identifiers = master_result.identifiers
        warnings = list(master_result.warnings)
    else:  # bandcamp
        bandcamp_result = await resolve_bandcamp_album(deps.bandcamp, parsed.identifier)
        canonical = bandcamp_result.canonical
        identifiers = bandcamp_result.identifiers
        warnings = list(bandcamp_result.warnings)

    streaming = await _check_streaming(canonical, parsed, deps, warnings)

    # Promote streaming-side identifiers (Spotify/Apple/Bandcamp) into the response.
    identifiers = _merge_streaming_identifiers(identifiers, streaming)

    # Identity write-back: any newly-discovered IDs land in entity.identity
    # for the artist. The discogs_artist_id write is fill-if-null (LML#766) so
    # a concurrent writer's id is never clobbered; bandcamp_id is COALESCE-
    # additive. Idempotent.
    # Require a non-empty, non-compilation artist name so we never insert
    # library_name="" or pollute the matcher's lookup space with V/A rows
    # (read-side handlers like identity/router.py already skip these, but the
    # rows still exist if write-side never guarded). See WXYC#379.
    if (
        canonical is not None
        and canonical.artist
        and not is_compilation_artist(canonical.artist)
        and deps.entity_store is not None
    ):
        await _writeback_identity(deps.entity_store, canonical, identifiers, warnings)

    return ReleaseResolveResponse(
        source=parsed.source,
        source_id=parsed.identifier,
        canonical=canonical or CanonicalRelease(artist="", title=""),
        identifiers=identifiers,
        streaming=streaming,
        warnings=warnings,
    )


def _resolve_to_parsed_url(request: ReleaseResolveRequest) -> ParsedUrl | None:
    """Translate a request into a ParsedUrl. URL field takes precedence."""
    if request.url:
        return parse_url(request.url)
    if request.source and request.id:
        return ParsedUrl(source=request.source, identifier=request.id)
    return None


async def _check_streaming(
    canonical: CanonicalRelease | None,
    parsed: ParsedUrl,
    deps: _ResolverDependencies,
    warnings: list[str],
) -> StreamingCheckResponse | None:
    """Run the streaming-availability fan-out.

    Skips the check when canonical metadata is missing — there's nothing to
    search for. Short-circuits the Bandcamp leg when the input itself is a
    Bandcamp URL: re-fuzzy-matching a URL the DJ explicitly gave us would
    just throw away certainty.
    """
    if canonical is None or not canonical.artist or not canonical.title:
        return None

    # Skip the Bandcamp leg when the input is a Bandcamp URL — we already know
    # the answer with certainty, and re-fuzzy-matching would burn 2+ rate-limited
    # HTTP calls (autocomplete + catalog scrape) for a result we'd discard.
    bandcamp_for_check = None if parsed.source == "bandcamp" else deps.bandcamp

    try:
        streaming = await check_streaming_availability(
            canonical.artist,
            canonical.title,
            spotify=deps.spotify,
            deezer=deps.deezer,
            apple_music=deps.apple_music,
            bandcamp=bandcamp_for_check,
        )
    except Exception:
        logger.exception("Streaming check failed for %s - %s", canonical.artist, canonical.title)
        warnings.append("Streaming-availability check failed; on-streaming flag not set.")
        return None

    if parsed.source == "bandcamp":
        # Trust the URL the DJ pasted. Confidence 100 + on_streaming=True are
        # safe overrides because we know the URL resolves to a real album.
        streaming.sources.bandcamp = SourceMatch(url=parsed.identifier, confidence=100.0)
        if streaming.on_streaming is not True:
            streaming.on_streaming = True

    return streaming


_SPOTIFY_ALBUM_ID_RE = re.compile(r"open\.spotify\.com/album/([A-Za-z0-9]+)")


def _spotify_album_id_from_url(url: str) -> str | None:
    """``open.spotify.com/album/<id>`` → ``<id>``. Returns None on a non-album URL."""
    match = _SPOTIFY_ALBUM_ID_RE.search(url)
    return match.group(1) if match else None


# Apple Music URL parsing lives in ``release.apple_music_url_parser``. Since
# LML#573 the streaming-URL post-process (``lookup/streaming_url_postprocess.py``)
# is the other caller: its ``apple_music_album`` registry entry uses the same
# parser to extract the album_id it mints into ``entity.release_identity``.


def _merge_streaming_identifiers(
    identifiers: ReleaseIdentifiers, streaming: StreamingCheckResponse | None
) -> ReleaseIdentifiers:
    """Promote Spotify/Apple/Bandcamp IDs found during the streaming check into the response."""
    if streaming is None:
        return identifiers

    sources: StreamingCheckSources = streaming.sources
    spotify_id = _spotify_album_id_from_url(sources.spotify.url) if sources.spotify else None
    apple_id = apple_album_id_from_url(sources.apple_music.url) if sources.apple_music else None
    bandcamp_url = sources.bandcamp.url if sources.bandcamp else None

    return identifiers.model_copy(
        update={
            "spotify_album_id": identifiers.spotify_album_id or spotify_id,
            "apple_music_album_id": identifiers.apple_music_album_id or apple_id,
            "bandcamp_album_url": identifiers.bandcamp_album_url or bandcamp_url,
        }
    )


async def _writeback_identity(
    store: EntityStore,
    canonical: CanonicalRelease,
    identifiers: ReleaseIdentifiers,
    warnings: list[str],
) -> None:
    """Push newly-learned IDs into ``entity.identity`` keyed on the artist name.

    The ``discogs_artist_id`` is written via ``EntityStore.
    fill_identity_discogs_id`` (LML#766): fill-if-null, so a non-null id set
    by a concurrent writer — the bare-name resolver's ``artists/resolver.py``
    mint is the realistic contender, and this release-derived id is the
    HIGHER-evidence of the two — is never silently clobbered. A lost race is
    logged by the store; there is nothing to surface to the DJ (either id is
    a valid Discogs artist for this release), so no warning is appended.

    ``bandcamp_id`` is written separately via ``upsert_identity`` (COALESCE-
    additive: it only fills a NULL, never overwrites). It is not the
    TOCTOU-contested column, so new-wins-when-non-null is not a hazard for it;
    the resolver never writes it. The artist row is created on first sight by
    whichever write runs first.
    """
    # Bandcamp doesn't expose an integer artist ID, so we use the album URL's
    # subdomain as the bandcamp_id placeholder. This matches existing
    # entity.identity rows populated by bandcamp_pipeline.py, which also uses
    # the slug as bandcamp_id.
    bandcamp_id = (
        extract_slug(identifiers.bandcamp_album_url) if identifiers.bandcamp_album_url else None
    )

    try:
        # discogs_artist_id: fill-if-null so we never clobber a concurrent
        # writer's id (LML#766). Only write when we actually learned one.
        if identifiers.discogs_artist_id is not None:
            outcome = await store.fill_identity_discogs_id(
                canonical.artist, identifiers.discogs_artist_id
            )
            # identity=None means the row vanished between the blocked upsert
            # and the read-back — a concurrent orphan-pass delete (or a
            # swallowed store failure). The learned id is not re-stamped (and
            # re-creating a row the orphan pass just deleted would only
            # re-orphan it), but the drop must not be silent.
            if outcome.identity is None:
                logger.warning(
                    "entity.identity fill-if-null found no row to stamp for "
                    "'%s' (discogs_artist_id=%d) — concurrent delete or "
                    "swallowed store failure; id not recorded",
                    canonical.artist,
                    identifiers.discogs_artist_id,
                )
        # bandcamp_id (and the first-sight row creation when no Discogs id was
        # learned): COALESCE-additive upsert. Skipping this entirely when both
        # ids are absent avoids an empty no-op write.
        if bandcamp_id is not None or identifiers.discogs_artist_id is None:
            await store.upsert_identity(
                canonical.artist,
                bandcamp_id=bandcamp_id,
            )
    except Exception:
        logger.exception("Identity write-back failed for %s", canonical.artist)
        warnings.append(
            "Could not record cross-source identifiers for "
            f"'{canonical.artist}' — prefill returned, identity store left untouched."
        )
