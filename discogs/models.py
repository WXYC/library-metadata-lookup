"""Pydantic models for Discogs API responses."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Field
from pydantic.json_schema import SkipJsonSchema

from generated.api_models import (
    Alias,
    DiscogsArtistCredit,
    DiscogsLabelCredit,
    DiscogsReleaseInfo,
    DiscogsReleaseMetadata,
    DiscogsReleaseVideo,
    DiscogsTrackItem,
    DiscogsTrackReleasesResponse,
    DiscogsWriterCredits,
    Member,
    StreamingResolution,
)
from generated.api_models import DiscogsMatchResult as _GeneratedDiscogsMatchResult

# Backward-compatible aliases for Discogs schemas now defined in api.yaml.
# See WXYC/library-metadata-lookup#111.
#
# ArtistDetails is intentionally NOT aliased to the generated DiscogsArtistDetails:
# its profile_tokens field uses the locally-defined ResolvedToken discriminated
# union (markup tokens), which the api.yaml schema currently flattens into a
# permissive single class. Keeping ArtistDetails local preserves type-safe
# variant access for callers that read profile_tokens.
ArtistCredit = DiscogsArtistCredit
LabelCredit = DiscogsLabelCredit
TrackItem = DiscogsTrackItem
ReleaseVideo = DiscogsReleaseVideo
ReleaseInfo = DiscogsReleaseInfo
TrackReleasesResponse = DiscogsTrackReleasesResponse
WriterCredits = DiscogsWriterCredits
ArtistRef = Alias
MemberRef = Member


class ReleaseMetadataResponse(DiscogsReleaseMetadata):
    """``DiscogsReleaseMetadata`` plus an LML-internal per-track writer map (LML#699).

    ``track_writers`` maps a track's display ``position`` (e.g. ``"A1"`` / ``"5"``)
    to the writer-role subset of that track's per-track Discogs credits
    (``release_track_artist`` ``extra = 1`` rows), populated by the cache read in
    ``discogs/cache_service.get_release``. It lets the BMI writer-credit
    enrichment (``discogs/writer_roles.py``) scope composer credits to the
    resolved playcut (``provenance="track"``) instead of the whole-release
    approximation. Internal-only: ``exclude=True`` keeps it off the wire, so the
    response contract — and ``DiscogsTrackItem.artists`` — is unchanged.
    ``SkipJsonSchema`` keeps it out of the generated OpenAPI schema too, so the
    documented ``GET /release/{id}`` contract doesn't advertise a property that
    never appears on the wire. ``None`` when the release has no per-track writer
    credits (or was built from the Discogs API path, which does not source them).

    Subclasses the generated model (mirroring ``EnrichedDiscogsMatchResult``) so
    it passes Pydantic validation everywhere ``DiscogsReleaseMetadata`` is
    expected; the regen overwrites only ``generated/api_models.py``.
    """

    track_writers: SkipJsonSchema[dict[str, list[DiscogsArtistCredit]] | None] = Field(
        default=None, exclude=True
    )


class TracksAutocompleteResponse(BaseModel):
    """Response for track title autocomplete from cache."""

    results: list[str] = []
    total: int = 0
    artist: str
    cached: bool = True


# MARK: - Resolved Markup Tokens


class PlainTextToken(BaseModel):
    """Plain text content."""

    type: Literal["plainText"] = "plainText"
    text: str


class ArtistLinkToken(BaseModel):
    """Artist link with display name and URL."""

    type: Literal["artistLink"] = "artistLink"
    name: str  # original name (may include disambiguation suffix)
    display_name: str  # suffix stripped for display
    url: str


class LabelNameToken(BaseModel):
    """Label name (displayed as plain text, not linked)."""

    type: Literal["labelName"] = "labelName"
    name: str


class ReleaseLinkToken(BaseModel):
    """Release link with title and URL."""

    type: Literal["releaseLink"] = "releaseLink"
    title: str
    url: str


class MasterLinkToken(BaseModel):
    """Master release link with title and URL."""

    type: Literal["masterLink"] = "masterLink"
    title: str
    url: str


class BoldToken(BaseModel):
    """Bold text content."""

    type: Literal["bold"] = "bold"
    content: str


class ItalicToken(BaseModel):
    """Italic text content."""

    type: Literal["italic"] = "italic"
    content: str


class UnderlineToken(BaseModel):
    """Underlined text content."""

    type: Literal["underline"] = "underline"
    content: str


class UrlLinkToken(BaseModel):
    """URL link with optional href and display content."""

    type: Literal["urlLink"] = "urlLink"
    href: str | None  # None when URL string is invalid
    content: str


ResolvedToken = Annotated[
    PlainTextToken
    | ArtistLinkToken
    | LabelNameToken
    | ReleaseLinkToken
    | MasterLinkToken
    | BoldToken
    | ItalicToken
    | UnderlineToken
    | UrlLinkToken,
    Discriminator("type"),
]


class ArtistDetails(BaseModel):
    """Full artist details from Discogs."""

    artist_id: int
    name: str
    profile: str | None = None
    profile_tokens: list[ResolvedToken] | None = None
    image_url: str | None = None
    name_variations: list[str] = []
    aliases: list[ArtistRef] = []
    members: list[MemberRef] = []
    urls: list[str] = []
    # Stamped only by `write_artist_details` (`now()` in SQL). NULL marks a
    # rebuild-created stub row that has never been hydrated from Discogs;
    # non-NULL means "we asked Discogs at least once," regardless of whether
    # the API returned a profile. Used as the cache-hit discriminator in
    # `DiscogsService.get_artist_details` (#502).
    fetched_at: datetime | None = None
    # Tombstone marker for Discogs 404s on `get_artist_details` (#510).
    # `True` means LML hit the live API for this id and got a 404; subsequent
    # reads short-circuit on this flag so callers don't re-burn the
    # rate-limit budget on the same 404. Tombstone rows carry `name = ""` and
    # otherwise-default fields — consumers of the public `DiscogsService`
    # surface never see them (the boundary translates to `None`), but direct
    # `cache_service` callers (`CachedOnlyResolver`, `get_artist_details_bulk`)
    # must explicitly guard.
    not_found: bool = False
    cached: bool = False


class MasterRelease(BaseModel):
    """Minimal master release metadata from Discogs."""

    master_id: int
    title: str
    year: int | None = None
    # Discogs' canonical release for this master (``main_release`` in the API).
    # ``None`` when absent or the ``0`` Discogs sends for "no release chosen";
    # the LML#858 master→release API-tail drain pins this id. See ``get_master``.
    main_release_id: int | None = None
    cached: bool = False


class EntityType(StrEnum):
    """Supported Discogs entity types for resolution."""

    artist = "artist"
    release = "release"
    master = "master"


class EntityResolveResponse(BaseModel):
    """Response for entity resolution: name, type, and ID."""

    name: str
    type: EntityType
    id: int


DISCOGS_SEARCH_PAGE_LIMIT = 5
"""Candidate page size for the release-search seam (LML#1321).

The single name behind three formerly-bare ``limit=5`` literals: the default of
``DiscogsService.search`` (which forwards it verbatim to its PG arm's
``DiscogsCacheService.search_releases`` call and to the API arm's ``per_page``),
the default of ``search_releases`` itself, and the explicit limit the LML#1318
album-level degrade (``lookup/album_level_match.py``) passes when it probes that
same cache directly.

That third caller is why this is a constant rather than three defaults: the
degrade claims to see "the same candidate set the album-level lookup path
would", which is only true while its limit equals the seam's. Widening one in
isolation silently narrows the degrade's match class relative to the step-3a
probe it claims parity with — ``tests/unit/test_typed_pair_floor_parity.py``
fails when they diverge.
"""


class DiscogsSearchRequest(BaseModel):
    """Request for general Discogs search."""

    artist: str | None = None
    album: str | None = None
    track: str | None = None
    label: str | None = None
    format: str | None = None


class DiscogsSearchResult(BaseModel):
    """A single result from Discogs search."""

    album: str | None = None
    artist: str | None = None
    # LML#784: the release's individual `extra = 0` credits, populated only by
    # the PG cache arm of `DiscogsService.search()` (whose `search_releases`
    # aggregates them alongside the joined `artist` presentation). The API arm
    # leaves this None — Discogs search results carry only the joined display
    # title. Floor consumers score the artist axis against `artist` plus these
    # variants, so single-credit and joined-credit queries both clear on a
    # multi-artist release.
    artist_credits: list[str] | None = None
    release_id: int
    release_url: str
    artwork_url: str | None = None
    confidence: float = 0.0
    # Enriched fields (populated after initial search by lookup/enrichment)
    # LML#688: the release's Discogs master_id, populated by the enrichment seam
    # from the resolved release so a catalog-popularity caller can collapse
    # pressings/formats of one logical album by the master. `None` when the
    # release has no master (one-offs, self-released) or for the streaming-only
    # sentinel (release_id == 0).
    master_id: int | None = None
    release_year: int | None = None
    artist_bio: str | None = None
    wikipedia_url: str | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    # Per-service resolution verdict (LML#1053) disambiguating WHY a sibling
    # ``*_url`` above is null: ``verified`` (url present), ``absent``
    # (consulted, no match — terminal), ``unresolved`` (probe attempted but
    # inconclusive — transient). A service key absent from the object was
    # never consulted at all and must not be conflated with ``absent``. Set
    # by ``lookup/enrichment/item.py`` from the same signals that already
    # produce the ``*_url`` fields — purely additive, never changes them.
    streaming_status: StreamingResolution | None = None
    # Extended fields (populated only when LookupRequest.extended=True). LML
    # already loads release + artist details during the streaming-URL
    # enrichment pass; these stash the rest of the payload so the response
    # can carry a full playcut metadata blob without a follow-up call.
    discogs_artist_id: int | None = None
    tracklist: list[DiscogsTrackItem] | None = None
    genres: list[str] | None = None
    styles: list[str] | None = None
    label: str | None = None
    full_release_date: str | None = None
    artist_image_url: str | None = None
    profile_tokens: list[ResolvedToken] | None = None
    # Songwriter/composer credits for BMI reporting (LML#699), populated by the
    # lookup enrichment seam (``lookup/enrichment``) from the resolved release's
    # writer-role credits. Rides the same extended gate as the other enriched fields.
    writer_credits: WriterCredits | None = None

    @classmethod
    def from_cache_row(cls, row: dict, *, confidence: float = 0.0) -> DiscogsSearchResult:
        """Build a candidate from one ``DiscogsCacheService.search_releases`` row.

        The sole mapping from that query's row shape to a search candidate
        (LML#1321). Two callers read those rows: ``DiscogsService.search``'s PG
        arm, which passes the ``calculate_confidence`` score it computes for
        ranking, and the LML#1318 album-level degrade, which probes the same
        cache directly and leaves ``confidence`` at its default — it floors the
        typed pair rather than ranking, so no confidence is computed (see
        ``lookup/typed_pair_floor.py`` on why the unranked order is equivalent).

        Keeping this in one place is what makes the degrade's "same candidate
        set" claim structural: a cache column added, renamed, or narrowed
        differently (``artist_credits or None`` collapses the query's ``[]`` to
        the API arm's ``None``, which the LML#784 artist-axis widening reads as
        "no per-credit variants") lands on both callers or neither.

        ``release_id`` and ``title`` are hard-indexed, not ``.get``: they are
        NOT NULL columns of the ``release`` table, so a missing key is a
        malformed row, and both callers would rather see the ``KeyError``
        degrade their probe than admit a candidate with a fabricated id.
        """
        return cls(
            release_id=row["release_id"],
            release_url=f"https://www.discogs.com/release/{row['release_id']}",
            artist=row["artist_name"],
            artist_credits=row.get("artist_credits") or None,
            album=row["title"],
            artwork_url=row.get("artwork_url"),
            confidence=confidence,
        )

    @classmethod
    def from_release_metadata(cls, metadata: ReleaseMetadataResponse) -> DiscogsSearchResult:
        """Build a candidate from a hydrated release (LML#1290).

        Sibling of :meth:`from_cache_row`, for the caller that has a release in
        hand rather than a search row: the override floor grades a hand-verified
        pin by scoring the pinned release as a single candidate against the
        card's query variants, and ``find_best_typed_match`` consumes
        ``DiscogsSearchResult`` members (``artist_variants()`` / ``album``).

        **``artist`` is rebuilt from ``artists[]``, NOT copied from
        ``metadata.artist``, and that is the whole point of this method.** The
        two sources disagree: ``get_release_lean`` sets the scalar ``artist`` to
        the FIRST ``extra = 0`` credit alone, while ``search_releases`` — which
        produces every candidate the pin is compared against — sets it to
        ``string_agg(artist_name, ', ' ORDER BY artist_name)`` over the same
        rows. Copying the scalar would leave the pin's candidate-side artist
        axis missing the joined form that every real candidate carries, so a
        query typed as the joined credit ("Merce Lemon & Fust") could clear
        against a matcher candidate and fail against the pin. Measured on the
        61,046-pin corpus: 554 pins (7.8% of floor failures) flip on this
        difference alone. ``artists`` is already the ``extra = 0`` set in
        ``artist_name`` order, so the join reproduces the aggregate exactly.

        ``artists[]`` also maps to ``artist_credits`` for the same reason
        ``from_cache_row`` maps the query's ``array_agg``: the LML#784 artist
        axis scores ``artist`` **plus** those variants. Empty collapses to
        ``None``, matching the API arm's shape.

        Does NOT guard the LML#510 tombstone (``title = ""`` / ``artist = ""``):
        that is the caller's decision, because "could not read this release" and
        "read a 404 marker" want the same answer at the *policy* layer (keep the
        pin) and this mapping has no policy. See
        ``lookup.artwork._pin_clears_floor``.
        """
        credits = [c.name for c in metadata.artists]
        return cls(
            release_id=metadata.release_id,
            release_url=metadata.release_url,
            # Mirrors ``_CREDIT_AGG_LATERAL``'s ``string_agg(..., ', ')``.
            artist=", ".join(credits) if credits else metadata.artist,
            artist_credits=credits or None,
            album=metadata.title,
            artwork_url=metadata.artwork_url,
        )

    def artist_variants(self) -> list[str | None]:
        """Artist-axis scoring variants: the joined credit plus the PG arm's
        per-credit entries (LML#784). Max-over these lets a single-credit
        query clear via its credit and a joined-credit query clear via the
        aggregate; API-arm results carry no ``artist_credits`` and score
        exactly as before.
        """
        return [self.artist, *(self.artist_credits or [])]

    def to_match_result(self) -> EnrichedDiscogsMatchResult:
        """Convert to the enriched API contract model."""
        return EnrichedDiscogsMatchResult(
            album=self.album,
            artist=self.artist,
            release_id=self.release_id,
            release_url=self.release_url,
            master_id=self.master_id,
            artwork_url=self.artwork_url,
            confidence=self.confidence,
            release_year=self.release_year,
            artist_bio=self.artist_bio,
            wikipedia_url=self.wikipedia_url,
            spotify_url=self.spotify_url,
            apple_music_url=self.apple_music_url,
            youtube_music_url=self.youtube_music_url,
            bandcamp_url=self.bandcamp_url,
            soundcloud_url=self.soundcloud_url,
            streaming_status=self.streaming_status,
            discogs_artist_id=self.discogs_artist_id,
            tracklist=self.tracklist,
            genres=self.genres,
            styles=self.styles,
            label=self.label,
            full_release_date=self.full_release_date,
            artist_image_url=self.artist_image_url,
            profile_tokens=self.profile_tokens,
            writer_credits=self.writer_credits,
        )


class DiscogsSearchResponse(BaseModel):
    """Response for general Discogs search."""

    results: list[DiscogsSearchResult] = []
    total: int = 0
    cached: bool = False
    # LML#784: arm provenance. True only when the PG cache arm built
    # ``results``. Distinct from ``cached``, which the in-memory memoization
    # layer (``@async_cached``) flips to True on every replay regardless of
    # which arm originally served — the library-miss floor-reject retry must
    # gate on this, or a memory-replayed API pass burns a duplicate API call.
    pg_served: bool = False


class DiscogsArtistSearchResult(BaseModel):
    """One artist hit from a ``/database/search?type=artist`` page (LML#759).

    ``title`` is the raw Discogs artist title with any "(N)" disambiguator
    intact. The bare-name resolver's overload detection normalizes it via
    ``to_identity_match_form`` — which strips the parenthetical — so
    "Popsicle (2)" collides with "Popsicle" and the family reads as
    ambiguous. Stripping the suffix here would erase that signal, and
    provenance wants the true Discogs string anyway.
    """

    artist_id: int
    title: str


class EnrichedDiscogsMatchResult(_GeneratedDiscogsMatchResult):
    """Extends the generated DiscogsMatchResult with enriched metadata fields.

    Subclasses the generated model so it passes Pydantic validation wherever
    DiscogsMatchResult is expected (e.g., LookupResultItem.artwork).

    ``profile_tokens`` is narrowed from the generated permissive
    ``DiscogsResolvedToken`` (single class, all-optional fields) to the
    local ``ResolvedToken`` discriminated union so per-variant access stays
    type-safe. Wire JSON is identical — both serialize with only the
    populated per-variant fields.
    """

    release_year: int | None = None
    artist_bio: str | None = None
    wikipedia_url: str | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    # Intentional narrowing from the generated permissive DiscogsResolvedToken
    # (flat class, all-optional fields) to the local discriminated union, for
    # type-safe per-variant access. Wire JSON is identical.
    profile_tokens: list[ResolvedToken] | None = None  # type: ignore[assignment]
