# Generated from wxyc-shared/api.yaml -- do not edit manually.
# Regenerate with: bash scripts/generate_api_models.sh

from __future__ import annotations

from datetime import date as date_aliased
from datetime import time as time_aliased
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, RootModel, confloat, conint


class ApiErrorResponse(BaseModel):
    message: str
    code: str | None = None
    details: dict[str, Any] | None = None


class PaginationParams(BaseModel):
    page: conint(ge=1) | None = None
    limit: conint(ge=1, le=100) | None = None


class PaginationInfo(BaseModel):
    page: int
    limit: int
    total: int | None = None
    hasMore: bool | None = None


class DateTimeEntry(BaseModel):
    day: str = Field(..., description='Day string (e.g., "Monday")')
    time: str = Field(..., description='Time string (e.g., "14:00")')


class Genre(StrEnum):
    Blues = "Blues"
    Rock = "Rock"
    Electronic = "Electronic"
    Hiphop = "Hiphop"
    Jazz = "Jazz"
    Classical = "Classical"
    Reggae = "Reggae"
    Soundtracks = "Soundtracks"
    OCS = "OCS"
    Unknown = "Unknown"


class Format(StrEnum):
    Vinyl = "Vinyl"
    CD = "CD"
    Unknown = "Unknown"


class RotationBin(StrEnum):
    H = "H"
    M = "M"
    L = "L"
    S = "S"


class DayOfWeek(StrEnum):
    Sunday = "Sunday"
    Monday = "Monday"
    Tuesday = "Tuesday"
    Wednesday = "Wednesday"
    Thursday = "Thursday"
    Friday = "Friday"
    Saturday = "Saturday"


class FlowsheetEntryBase(BaseModel):
    id: int
    play_order: int
    show_id: int


class FlowsheetEntryResponse(FlowsheetEntryBase):
    album_id: int | None = None
    track_title: str | None = None
    album_title: str | None = None
    artist_name: str | None = None
    record_label: str | None = None
    label_id: int | None = None
    rotation_id: int | None = None
    rotation_bin: RotationBin | None = None
    request_flag: bool
    segue: bool | None = None
    message: str | None = None
    artwork_url: str | None = None
    discogs_url: str | None = None
    release_year: int | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    artist_bio: str | None = None
    artist_wikipedia_url: str | None = None


class FlowsheetSongEntry(FlowsheetEntryBase):
    track_title: str
    artist_name: str
    album_title: str
    record_label: str
    label_id: int | None = None
    request_flag: bool
    segue: bool | None = None
    album_id: int | None = None
    rotation_id: int | None = None
    rotation_bin: RotationBin | None = None


class FlowsheetShowBlockEntry(FlowsheetEntryBase, DateTimeEntry):
    dj_name: str
    isStart: bool


class FlowsheetMessageEntry(FlowsheetEntryBase):
    message: str


class FlowsheetBreakpointEntry(FlowsheetMessageEntry, DateTimeEntry):
    pass


class FlowsheetCreateSongFromCatalog(BaseModel):
    album_id: int
    track_title: str
    rotation_id: int | None = None
    request_flag: bool
    segue: bool | None = None
    record_label: str | None = None


class FlowsheetCreateSongFreeform(BaseModel):
    artist_name: str
    album_title: str
    track_title: str
    request_flag: bool
    segue: bool | None = None
    record_label: str | None = None
    label_id: int | None = None


class EntryType(StrEnum):
    talkset = "talkset"
    breakpoint = "breakpoint"
    message = "message"


class FlowsheetCreateMessage(BaseModel):
    message: str
    entry_type: EntryType | None = Field(
        None,
        description="Explicit entry type. If omitted, the backend infers from message content.",
    )


class FlowsheetUpdateRequest(BaseModel):
    track_title: str | None = None
    artist_name: str | None = None
    album_title: str | None = None
    record_label: str | None = None
    label_id: int | None = None
    request_flag: bool | None = None
    segue: bool | None = None


class FlowsheetQueryParams(BaseModel):
    page: int | None = None
    limit: int | None = None
    start_id: int | None = None
    end_id: int | None = None
    shows_limit: int | None = None


class Sort(StrEnum):
    date = "date"
    artist = "artist"
    song = "song"
    dj = "dj"


class Order(StrEnum):
    asc = "asc"
    desc = "desc"


class PlaylistSearchParams(BaseModel):
    q: str | None = Field(None, description='Search query (supports AND, OR, NOT, "", *)')
    page: conint(ge=0) | None = 0
    limit: conint(ge=1, le=100) | None = 50
    sort: Sort | None = "date"
    order: Order | None = "desc"


class PlaylistSearchResult(BaseModel):
    id: int
    play_date: AwareDatetime
    artist_name: str
    track_title: str
    album_title: str
    record_label: str
    dj_name: str
    show_id: int


class PlaylistSearchResponse(BaseModel):
    results: list[PlaylistSearchResult]
    total: int
    page: int
    totalPages: int


class FlowsheetEntryType(StrEnum):
    track = "track"
    show_start = "show_start"
    show_end = "show_end"
    dj_join = "dj_join"
    dj_leave = "dj_leave"
    talkset = "talkset"
    breakpoint = "breakpoint"
    message = "message"


class FlowsheetV2Base(BaseModel):
    id: int
    show_id: int
    play_order: int
    add_time: AwareDatetime


class EntryType1(StrEnum):
    track = "track"


class FlowsheetV2TrackEntry(FlowsheetV2Base):
    entry_type: Literal["track"]
    album_id: int | None = None
    rotation_id: int | None = None
    artist_name: str | None = None
    album_title: str | None = None
    track_title: str | None = None
    record_label: str | None = None
    request_flag: bool
    segue: bool | None = None
    rotation_bin: RotationBin | None = None
    artwork_url: str | None = None
    discogs_url: str | None = None
    release_year: int | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    artist_bio: str | None = None
    artist_wikipedia_url: str | None = None
    on_streaming: bool | None = Field(
        None,
        description="Whether this album is available on streaming platforms. False means WXYC library exclusive. Null if unknown.",
    )


class EntryType2(StrEnum):
    show_start = "show_start"


class FlowsheetV2ShowStartEntry(FlowsheetV2Base):
    entry_type: Literal["show_start"]
    dj_name: str
    timestamp: AwareDatetime


class EntryType3(StrEnum):
    show_end = "show_end"


class FlowsheetV2ShowEndEntry(FlowsheetV2Base):
    entry_type: Literal["show_end"]
    dj_name: str
    timestamp: AwareDatetime


class EntryType4(StrEnum):
    dj_join = "dj_join"


class FlowsheetV2DJJoinEntry(FlowsheetV2Base):
    entry_type: Literal["dj_join"]
    dj_name: str


class EntryType5(StrEnum):
    dj_leave = "dj_leave"


class FlowsheetV2DJLeaveEntry(FlowsheetV2Base):
    entry_type: Literal["dj_leave"]
    dj_name: str


class EntryType6(StrEnum):
    talkset = "talkset"


class FlowsheetV2TalksetEntry(FlowsheetV2Base):
    entry_type: Literal["talkset"]
    message: str


class EntryType7(StrEnum):
    breakpoint = "breakpoint"


class FlowsheetV2BreakpointEntry(FlowsheetV2Base):
    entry_type: Literal["breakpoint"]
    message: str | None = None


class EntryType8(StrEnum):
    message = "message"


class FlowsheetV2MessageEntry(FlowsheetV2Base):
    entry_type: Literal["message"]
    message: str


class Entries(
    RootModel[
        FlowsheetV2TrackEntry
        | FlowsheetV2ShowStartEntry
        | FlowsheetV2ShowEndEntry
        | FlowsheetV2DJJoinEntry
        | FlowsheetV2DJLeaveEntry
        | FlowsheetV2TalksetEntry
        | FlowsheetV2BreakpointEntry
        | FlowsheetV2MessageEntry
    ]
):
    root: (
        FlowsheetV2TrackEntry
        | FlowsheetV2ShowStartEntry
        | FlowsheetV2ShowEndEntry
        | FlowsheetV2DJJoinEntry
        | FlowsheetV2DJLeaveEntry
        | FlowsheetV2TalksetEntry
        | FlowsheetV2BreakpointEntry
        | FlowsheetV2MessageEntry
    ) = Field(..., discriminator="entry_type")


class FlowsheetV2PaginatedResponse(BaseModel):
    entries: list[Entries]
    page: int
    limit: int
    total: int = Field(..., description="Total number of entries")
    totalPages: int = Field(..., description="Total number of pages")


class OnAirDJ(BaseModel):
    id: int
    dj_name: str


class OnAirStatusResponse(BaseModel):
    djs: list[OnAirDJ]
    onAir: str = Field(..., description='Status indicator - "on" or "off"')


class Show(BaseModel):
    id: int | None = None
    primary_dj_id: int | None = None
    specialty_id: int | None = None
    show_name: str | None = None
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None


class ShowDJ(BaseModel):
    show_id: int | None = None
    dj_id: int | None = None
    active: bool | None = None


class ShowPlaylist(BaseModel):
    show_name: str | None = None
    specialty_show: str | None = None
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    show_djs: list[OnAirDJ] | None = None
    entries: list[FlowsheetEntryResponse] | None = None


class Dj(BaseModel):
    dj_id: int | None = None
    dj_name: str | None = None


class ShowPeek(BaseModel):
    show: int | None = None
    show_name: str | None = None
    date: AwareDatetime | None = None
    djs: list[Dj] | None = None
    specialty_show: str | None = None
    preview: list[FlowsheetEntryResponse] | None = None


class Artist(BaseModel):
    id: int
    artist_name: str
    code_letters: str
    code_artist_number: int
    genre_id: int


class ArtistWithGenre(Artist):
    genre_name: Genre


class Album(BaseModel):
    id: int
    artist_id: int
    album_title: str
    code_number: int
    genre_id: int
    format_id: int
    label: str | None = None
    label_id: int | None = None
    add_date: AwareDatetime | None = None
    disc_quantity: int | None = None
    alternate_artist_name: str | None = None
    album_artist: str | None = Field(
        None,
        description='Credited album artist for compilations (e.g., "Kruder & Dorfmeister" on a DJ-Kicks release filed under Various Artists).',
    )


class AlbumSearchResult(BaseModel):
    id: int
    add_date: AwareDatetime
    album_title: str
    artist_name: str
    code_letters: str
    code_number: int
    code_artist_number: int
    format_name: str
    genre_name: str
    label: str
    label_id: int | None = None
    album_dist: float | None = None
    artist_dist: float | None = None
    rotation_bin: RotationBin | None = None
    rotation_id: int | None = None
    plays: int | None = None
    on_streaming: bool | None = Field(
        None,
        description="True if this release is available on at least one streaming service. False means only available in the WXYC physical library. Null if unknown.",
    )
    album_artist: str | None = Field(None, description="Credited album artist for compilations.")
    date_lost: AwareDatetime | None = Field(
        None,
        description="When the release was marked missing from the physical library. Null if in library.",
    )
    date_found: AwareDatetime | None = Field(
        None,
        description="When a previously missing release was found. Null if never lost.",
    )
    artwork_url: str | None = Field(
        None,
        description="Album cover artwork URL from Discogs. Null if artwork has not been fetched yet or is unavailable.",
    )


class AddAlbumRequest(BaseModel):
    album_title: str
    artist_name: str | None = None
    artist_id: int | None = None
    label: str
    label_id: int | None = None
    genre_id: int
    format_id: int
    disc_quantity: int | None = None
    alternate_artist_name: str | None = None
    album_artist: str | None = None


class Label(BaseModel):
    id: int
    label_name: str
    parent_label_id: int | None = None


class CreateLabelRequest(BaseModel):
    label_name: str
    parent_label_id: int | None = None


class AddArtistRequest(BaseModel):
    artist_name: str
    code_letters: str
    genre_id: int


class OrderDirection(StrEnum):
    asc = "asc"
    desc = "desc"


class CatalogSearchParams(BaseModel):
    artist_name: str | None = None
    album_title: str | None = None
    n: int | None = Field(None, description="Maximum number of results")
    orderBy: str | None = None
    orderDirection: OrderDirection | None = None


class FormatEntry(BaseModel):
    id: int
    format_name: str


class GenreEntry(BaseModel):
    id: int
    genre_name: Genre
    code_letters: str


class Rotation(BaseModel):
    id: int | None = None
    rotation_bin: RotationBin | None = None
    add_date: date_aliased | None = None
    kill_date: date_aliased | None = None


class AlbumInfoResponse(Album):
    artist_name: str
    code_letters: str
    format_name: str
    genre_name: Genre
    rotation: Rotation | None = None


class Source(StrEnum):
    discogs = "discogs"
    flowsheet = "flowsheet"
    bin = "bin"


class TrackSearchResult(BaseModel):
    track_id: int | None = None
    title: str
    position: str | None = None
    duration: str | None = None
    album_id: int | None = None
    album_title: str
    artist_name: str
    label: str | None = None
    rotation_id: int | None = None
    rotation_bin: RotationBin | None = None
    source: Source


class TrackSearchParams(BaseModel):
    song: str
    artist: str | None = None
    album: str | None = None
    label: str | None = None
    n: int | None = None


class RotationEntry(BaseModel):
    id: int
    album_id: int
    rotation_bin: RotationBin
    add_date: date_aliased
    kill_date: date_aliased | None = None


class AddRotationRequest(BaseModel):
    album_id: int
    rotation_bin: RotationBin


class KillRotationRequest(BaseModel):
    rotation_id: int
    kill_date: date_aliased | None = Field(None, description="ISO date string, defaults to today")


class RotationWithAlbum(RotationEntry):
    album_title: str
    artist_name: str
    code_letters: str
    code_number: int


class Rotation1(BaseModel):
    id: int | None = None
    code_letters: str | None = None
    code_artist_number: int | None = None
    code_number: int | None = None
    artist_name: str | None = None
    album_title: str | None = None
    record_label: str | None = None
    genre_name: str | None = None
    format_name: str | None = None
    rotation_id: int | None = None
    add_date: date_aliased | None = None
    play_freq: RotationBin | None = None
    kill_date: date_aliased | None = None
    plays: int | None = None


class DJ(BaseModel):
    id: int
    dj_name: str
    real_name: str | None = None
    email: str | None = None


class NewDJ(BaseModel):
    cognito_user_name: str | None = None
    real_name: str | None = None
    dj_name: str | None = None


class BinEntry(BaseModel):
    id: int
    dj_id: int
    album_id: int
    added_at: AwareDatetime
    album_title: str
    artist_name: str
    code_letters: str
    code_number: int


class AddToBinRequest(BaseModel):
    album_id: int


class Playlist(BaseModel):
    id: int
    dj_id: int
    name: str
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PlaylistEntry(BaseModel):
    id: int
    playlist_id: int
    album_id: int
    track_title: str | None = None
    position: int
    album_title: str
    artist_name: str


class PlaylistWithEntries(Playlist):
    entries: list[PlaylistEntry]


class DJBinResponse(BaseModel):
    dj_id: int
    entries: list[BinEntry]


class DJPlaylistsResponse(BaseModel):
    dj_id: int
    playlists: list[Playlist]


class BinLibraryDetails(BaseModel):
    album_id: int | None = None
    album_title: str | None = None
    artist_name: str | None = None
    label: str | None = None
    code_letters: str | None = None
    code_artist_number: int | None = None
    code_number: int | None = None
    format_name: str | None = None
    genre_name: str | None = None


class ScheduleShift(BaseModel):
    id: int
    dj_id: int
    dj_name: str
    day: DayOfWeek
    start_time: str = Field(..., description="Time in HH:MM format")
    end_time: str = Field(..., description="Time in HH:MM format")
    show_name: str | None = None
    specialty_id: int | None = None


class AddScheduleShiftRequest(BaseModel):
    dj_id: int
    day: DayOfWeek
    start_time: str
    end_time: str
    show_name: str | None = None
    specialty_id: int | None = None


class SpecialtyShow(BaseModel):
    id: int
    specialty_name: str
    description: str | None = None


class Schedule(BaseModel):
    id: int | None = Field(None, description="Primary key")
    day: conint(ge=0, le=6) | None = Field(
        None, description="Day of the week 0 = Monday, 6 = Sunday"
    )
    start_time: time_aliased | None = Field(None, description="Show start time")
    show_duration: conint(ge=1) | None = Field(None, description="Duration in minutes")
    specialty_id: int | None = Field(
        None, description="Reference to specialty show, null for regular shows"
    )
    assigned_dj_id: int | None = Field(None, description="Reference to primary DJ")
    assigned_dj_id2: int | None = Field(None, description="Reference to secondary DJ")


class RequestStatus(StrEnum):
    pending = "pending"
    played = "played"
    rejected = "rejected"


class SongRequest(BaseModel):
    id: int
    device_id: str
    message: str
    created_at: AwareDatetime
    status: RequestStatus


class SubmitRequestPayload(BaseModel):
    message: str


class ParsedSongRequest(BaseModel):
    artist: str | None = None
    song: str | None = None
    album: str | None = None
    confidence: confloat(ge=0.0, le=1.0)
    interpretation: str | None = None


class MatchType(StrEnum):
    exact = "exact"
    fuzzy = "fuzzy"
    partial = "partial"


class LibraryMatch(BaseModel):
    album: AlbumSearchResult
    confidence: confloat(ge=0.0, le=1.0)
    matchType: MatchType
    reasoning: str | None = None


class EnhancedRequest(SongRequest):
    parsed: ParsedSongRequest | None = None
    matches: list[LibraryMatch] | None = None
    artwork_url: str | None = None
    discogs_url: str | None = None


class DeviceRegistration(BaseModel):
    device_id: str
    registered_at: AwareDatetime


class DeviceToken(BaseModel):
    token: str
    expires_at: AwareDatetime


class RateLimitInfo(BaseModel):
    remaining: int
    reset_at: AwareDatetime
    limit: int


class RequestSubmissionResponse(BaseModel):
    success: bool
    request_id: int | None = None
    rate_limit: RateLimitInfo | None = None
    message: str | None = None


class MetadataSource(StrEnum):
    discogs = "discogs"
    spotify = "spotify"
    apple_music = "apple_music"
    cache = "cache"
    none = "none"


class AlbumMetadata(BaseModel):
    album_id: int
    artwork_url: str | None = None
    discogs_url: str | None = None
    discogs_id: int | None = None
    release_year: int | None = None
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    last_fetched: AwareDatetime | None = None


class ArtistMetadata(BaseModel):
    artist_id: int
    bio: str | None = None
    wikipedia_url: str | None = None
    discogs_url: str | None = None
    discogs_id: int | None = None
    image_url: str | None = None
    last_fetched: AwareDatetime | None = None


class MetadataFetchRequest(BaseModel):
    album_id: int | None = None
    artist_id: int | None = None
    force_refresh: bool | None = None


class MetadataFetchResponse(BaseModel):
    album: AlbumMetadata | None = None
    artist: ArtistMetadata | None = None
    source: MetadataSource
    cached: bool


class Type(StrEnum):
    release = "release"
    master = "master"
    artist = "artist"


class DiscogsSearchResult(BaseModel):
    id: int
    title: str
    year: int | None = None
    thumb: str | None = None
    cover_image: str | None = None
    resource_url: str
    type: Type


class DiscogsArtistRef(BaseModel):
    name: str
    id: int


class DiscogsLabelRef(BaseModel):
    name: str
    id: int


class DiscogsTrack(BaseModel):
    position: str
    title: str
    duration: str | None = None


class DiscogsImage(BaseModel):
    type: str
    uri: str
    width: int
    height: int


class DiscogsRelease(BaseModel):
    id: int
    title: str
    year: int | None = None
    artists: list[DiscogsArtistRef]
    labels: list[DiscogsLabelRef]
    genres: list[str]
    styles: list[str]
    tracklist: list[DiscogsTrack]
    images: list[DiscogsImage] | None = None


class StreamingLinks(BaseModel):
    spotify_url: str | None = Field(None, description="Spotify album URL")
    apple_music_url: str | None = Field(None, description="Apple Music album URL")
    youtube_music_url: str | None = Field(None, description="YouTube Music search URL")
    bandcamp_url: str | None = Field(None, description="Bandcamp album URL")
    soundcloud_url: str | None = Field(None, description="SoundCloud search URL")


class ReconciledIdentity(BaseModel):
    discogs_artist_id: int | None = Field(None, description="Discogs artist ID")
    musicbrainz_artist_id: str | None = Field(None, description="MusicBrainz artist UUID")
    wikidata_qid: str | None = Field(None, description='Wikidata QID (e.g. "Q12345")')
    spotify_artist_id: str | None = Field(
        None, description="Spotify artist ID (the Spotify URI suffix)"
    )
    apple_music_artist_id: str | None = Field(None, description="Apple Music artist ID")
    bandcamp_id: str | None = Field(
        None, description="Bandcamp slug (the subdomain in `<slug>.bandcamp.com`)"
    )


class LookupRequest(BaseModel):
    artist: str | None = Field(None, description="Parsed artist name")
    song: str | None = Field(None, description="Parsed song/track title")
    album: str | None = Field(None, description="Parsed album name")
    raw_message: str | None = Field(
        None,
        description="Original request message (used for ambiguous format detection). Optional when structured fields (artist, album, song) are provided.\n",
    )


class LibraryCatalogItem(BaseModel):
    id: int = Field(..., description="Unique identifier in the library database")
    title: str | None = Field(None, description="Album/release title")
    artist: str | None = Field(None, description="Artist name")
    call_letters: str | None = Field(None, description="Library call letter code")
    artist_call_number: int | None = Field(
        None, description="Numeric part of artist classification"
    )
    release_call_number: int | None = Field(
        None, description="Numeric part of release classification"
    )
    genre: str | None = Field(None, description="Genre classification")
    format: str | None = Field(None, description="Physical format (vinyl, CD, etc.)")
    label: str | None = Field(None, description="Record label name from the library catalog")
    call_number: str = Field(
        ...,
        description='Full call number for shelf lookup, e.g. "Rock CD ABC 123/45". Computed from genre, format, call_letters, artist_call_number, and release_call_number.\n',
    )
    library_url: str = Field(..., description="URL to view this release in the WXYC library")
    on_streaming: bool | None = Field(
        None,
        description="True if this release is available on at least one streaming service. False means only available in the WXYC physical library. Null if unknown.",
    )


class DiscogsMatchResult(BaseModel):
    album: str | None = Field(None, description="Release title")
    artist: str | None = Field(None, description="Release artist")
    release_id: int = Field(..., description="Discogs release ID")
    release_url: str = Field(..., description="URL to the release on Discogs")
    artwork_url: str | None = Field(None, description="Artwork image URL")
    confidence: confloat(ge=0.0, le=1.0) | None = Field(0, description="Match confidence score")
    release_year: int | None = Field(None, description="Release year from Discogs")
    artist_bio: str | None = Field(None, description="Artist biography from Discogs profile")
    wikipedia_url: str | None = Field(None, description="Wikipedia URL for the artist")
    spotify_url: str | None = Field(None, description="Spotify album URL")
    apple_music_url: str | None = Field(None, description="Apple Music album URL")
    youtube_music_url: str | None = Field(None, description="YouTube Music search URL")
    bandcamp_url: str | None = Field(None, description="Bandcamp album URL")
    soundcloud_url: str | None = Field(None, description="SoundCloud search URL")


class LookupResultItem(BaseModel):
    library_item: LibraryCatalogItem
    artwork: DiscogsMatchResult | None = None
    reconciled_identity: ReconciledIdentity | None = None


class SearchType(StrEnum):
    direct = "direct"
    fallback = "fallback"
    alternative = "alternative"
    compilation = "compilation"
    song_as_artist = "song_as_artist"
    none = "none"


class LookupResponse(BaseModel):
    results: list[LookupResultItem] | None = Field([], validate_default=True)
    search_type: SearchType | None = Field(
        "none",
        description="The search strategy that produced results: direct, fallback, alternative, compilation, song_as_artist, or none\n",
    )
    song_not_found: bool | None = Field(
        False,
        description="True if search fell back to artist-only (track not confirmed on results)",
    )
    found_on_compilation: bool | None = Field(
        False, description="True if the track was found on a compilation album"
    )
    context_message: str | None = Field(
        None, description="Human-readable context string for display"
    )
    corrected_artist: str | None = Field(
        None, description="Fuzzy-corrected artist name if different from the original"
    )
    cache_stats: dict[str, Any] | None = Field(
        None, description="Cache hit/miss statistics from the lookup"
    )


class DiscogsTrackItem(BaseModel):
    position: str
    title: str
    duration: str | None = None
    artists: list[str] | None = []


class DiscogsArtistCredit(BaseModel):
    artist_id: int | None = None
    name: str
    join: str | None = Field("", description='Join phrase (e.g. " & ", ", ")')
    role: str | None = Field(
        None, description='Role for extra artists (e.g. "Producer", "Mixed By")'
    )


class DiscogsLabelCredit(BaseModel):
    label_id: int | None = None
    name: str
    catno: str | None = Field(None, description="Catalog number")


class DiscogsReleaseVideo(BaseModel):
    src: str
    title: str | None = None
    duration: int | None = Field(None, description="Duration in seconds")
    embed: bool | None = True


class DiscogsReleaseMetadata(BaseModel):
    release_id: int
    title: str
    artist: str
    year: int | None = None
    label: str | None = None
    artist_id: int | None = None
    label_id: int | None = None
    genres: list[str] | None = []
    styles: list[str] | None = []
    tracklist: list[DiscogsTrackItem] | None = Field([], validate_default=True)
    artwork_url: str | None = None
    release_url: str
    cached: bool | None = False
    artists: list[DiscogsArtistCredit] | None = Field([], validate_default=True)
    extra_artists: list[DiscogsArtistCredit] | None = Field([], validate_default=True)
    labels: list[DiscogsLabelCredit] | None = Field([], validate_default=True)
    released: str | None = Field(None, description="Release date as ISO string")
    videos: list[DiscogsReleaseVideo] | None = Field([], validate_default=True)


class Type1(StrEnum):
    plainText = "plainText"
    artistLink = "artistLink"
    labelName = "labelName"
    releaseLink = "releaseLink"
    masterLink = "masterLink"
    bold = "bold"
    italic = "italic"
    underline = "underline"
    urlLink = "urlLink"


class DiscogsResolvedToken(BaseModel):
    type: Type1
    text: str | None = Field(None, description="Content for plainText tokens")
    name: str | None = Field(None, description="Name for artistLink and labelName tokens")
    display_name: str | None = Field(
        None,
        description="Display name for artistLink tokens (disambiguation suffix stripped)",
    )
    title: str | None = Field(None, description="Title for releaseLink and masterLink tokens")
    url: str | None = Field(None, description="URL for artistLink, releaseLink, masterLink tokens")
    href: str | None = Field(None, description="URL for urlLink tokens (null if URL is invalid)")
    content: str | None = Field(
        None, description="Content for bold, italic, underline, and urlLink tokens"
    )


class Alias(BaseModel):
    id: int
    name: str


class Member(BaseModel):
    id: int
    name: str
    active: bool | None = True


class DiscogsArtistDetails(BaseModel):
    artist_id: int
    name: str
    profile: str | None = None
    profile_tokens: list[DiscogsResolvedToken] | None = Field(
        None, description="Pre-parsed structured tokens from the Discogs profile markup"
    )
    image_url: str | None = None
    name_variations: list[str] | None = []
    aliases: list[Alias] | None = Field([], validate_default=True)
    members: list[Member] | None = Field([], validate_default=True)
    urls: list[str] | None = []
    cached: bool | None = False


class DiscogsReleaseInfo(BaseModel):
    album: str
    artist: str
    release_id: int
    release_url: str
    is_compilation: bool | None = False


class DiscogsTrackReleasesResponse(BaseModel):
    track: str | None = None
    artist: str | None = None
    releases: list[DiscogsReleaseInfo] | None = Field([], validate_default=True)
    total: int | None = 0
    cached: bool | None = False


class LibrarySearchItem(BaseModel):
    id: int
    title: str | None = None
    artist: str | None = None
    call_letters: str | None = None
    artist_call_number: int | None = None
    release_call_number: int | None = None
    genre: str | None = None
    format: str | None = None
    alternate_artist_name: str | None = None
    label: str | None = None
    on_streaming: bool | None = None
    call_number: str | None = Field(None, description='Computed call number (e.g. "Rock CD S 1/1")')
    library_url: str | None = Field(None, description="URL to the release on wxyc.info")


class LibrarySearchResponse(BaseModel):
    results: list[LibrarySearchItem] | None = None
    total: int | None = None
    query: str | None = None


class StreamingCheckRequest(BaseModel):
    artist: str = Field(..., description="Artist name to search for")
    title: str = Field(..., description="Album title to search for")


class StreamingSourceMatch(BaseModel):
    url: str = Field(..., description="URL to the matched album on the service")
    confidence: float = Field(..., description="Match confidence score (0-100)")


class StreamingCheckSources(BaseModel):
    spotify: StreamingSourceMatch | None = None
    deezer: StreamingSourceMatch | None = None
    apple_music: StreamingSourceMatch | None = None
    bandcamp: StreamingSourceMatch | None = None


class StreamingCheckResponse(BaseModel):
    on_streaming: bool = Field(
        ...,
        description="True if found on any service, false if absent on all, null if inconclusive.",
    )
    sources: StreamingCheckSources


class AppConfig(BaseModel):
    posthogApiKey: str = Field(..., description="PostHog analytics write key (public by design)")
    posthogHost: str = Field(..., description="PostHog ingestion host")
    requestOMaticUrl: str = Field(..., description="Request-o-matic service URL for song requests")
    apiBaseUrl: str = Field(..., description="Backend API base URL")


class TrackListItem(BaseModel):
    position: str = Field(..., description='Track position (e.g. "1", "A1")')
    title: str = Field(..., description="Track title")
    duration: str | None = Field(None, description='Track duration (e.g. "5:23")')


class AlbumMetadataResponse(BaseModel):
    discogsReleaseId: int | None = Field(None, description="Discogs release ID")
    discogsUrl: str | None = Field(None, description="Discogs release page URL")
    releaseYear: int | None = Field(None, description="Release year from Discogs")
    artworkUrl: str | None = Field(None, description="Album artwork image URL")
    genres: list[str] | None = Field(None, description="Discogs genre classifications")
    styles: list[str] | None = Field(
        None, description="Discogs style classifications (more specific than genres)"
    )
    label: str | None = Field(None, description="Primary record label name")
    discogsArtistId: int | None = Field(
        None, description="Discogs artist ID, for linking to artist metadata"
    )
    fullReleaseDate: str | None = Field(
        None, description='Full release date when available (e.g. "2024-03-15")'
    )
    tracklist: list[TrackListItem] | None = Field(None, description="Release tracklist")
    spotifyUrl: str | None = Field(None, description="Spotify URL for the album or track")
    appleMusicUrl: str | None = Field(None, description="Apple Music URL for the album or track")
    youtubeMusicUrl: str | None = Field(None, description="YouTube Music search URL")
    bandcampUrl: str | None = Field(None, description="Bandcamp search URL")
    soundcloudUrl: str | None = Field(None, description="SoundCloud search URL")


class ArtistMetadataResponse(BaseModel):
    discogsArtistId: int | None = Field(None, description="Discogs artist ID")
    bio: str | None = Field(None, description="Artist biography from Discogs")
    wikipediaUrl: str | None = Field(None, description="Wikipedia URL for the artist")
    imageUrl: str | None = Field(None, description="Artist image URL from Discogs")


class ArtworkSearchResponse(BaseModel):
    artworkUrl: str | None = Field(None, description="Best-match artwork image URL")
    source: str | None = Field(
        None, description='Provider that supplied the artwork (e.g. "discogs")'
    )
    confidence: float | None = Field(None, description="Confidence score of the match (0-1)")


class Type2(StrEnum):
    artist = "artist"
    release = "release"
    master = "master"


class EntityResolveResponse(BaseModel):
    name: str = Field(..., description="Entity name")
    type: Type2 = Field(..., description="Discogs entity type")
    id: int = Field(..., description="Discogs entity ID")


class SpotifyTrackResponse(BaseModel):
    title: str = Field(..., description="Track title")
    artist: str = Field(..., description="Primary artist name")
    album: str = Field(..., description="Album name")
    artworkUrl: str | None = Field(None, description="Album artwork URL from Spotify")
