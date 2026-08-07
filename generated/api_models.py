# Generated from wxyc-shared/api.yaml -- do not edit manually.
# Regenerate with: bash scripts/generate_api_models.sh

from __future__ import annotations

from datetime import date as date_aliased
from datetime import time as time_aliased
from enum import Enum, IntEnum, StrEnum
from typing import Any, Literal

from pydantic import (
    AnyUrl,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    confloat,
    conint,
    constr,
)


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


class Status(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    unhealthy = "unhealthy"


class HealthCheckResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
    )
    status: Status = Field(
        ...,
        description="Service-reported health. `healthy` = fully operational; `degraded`\n= serving but with reduced capability; `unhealthy` = should not\nreceive traffic.\n",
    )


class Services(StrEnum):
    ok = "ok"
    unavailable = "unavailable"
    timeout = "timeout"


class ReadinessResponse(HealthCheckResponse):
    services: dict[str, Services] = Field(
        ..., description="Per-dependency readiness map keyed by dependency name."
    )


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
    track_position: str | None = Field(
        None,
        description='Track position on the release (e.g., "A1", "B2", "5"). Optional; populated by the dj-site picker when a release with a resolvable tracklist is chosen (catalog-track-search plan §5.3 / Track 3). Free-text `track_title` remains the source of truth; this field is additive metadata for future enrichment + analytics.\n',
    )


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
    track_position: str | None = Field(
        None,
        description='Track position on the release (e.g., "A1", "B2", "5", "1-12"). Written by the dj-site flowsheet picker (catalog-track-search plan §5.3 / Track 3) when the DJ selects a track from the resolved release. Omitted when the DJ enters a free-text track_title without a tracklist lookup or when the release has no resolvable identity. String-typed to match Discogs\'s `release_track.position` (vinyl side notation, multi-disc prefixes).\n',
    )
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
    rotation_id: int | None = Field(
        None,
        description="Rotation linkage for a track on a rotation album that isn't in the WXYC library catalog (BS#1308). The Backend snapshot/else branch persists `rotation_id` alongside `album_id IS NULL` so the V2 read path can JOIN back to `rotation` for `rotation_bin` and the iOS rotation-artwork resolver can find the entry. `rotation_bin` is derived on read and intentionally not declared here.\n",
    )


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
    track_position: str | None = Field(
        None,
        description='Track position on the release (e.g., "A1", "B2", "5", "1-12"). Set when re-picking a track via the flowsheet picker; cleared (omitted) when a DJ edits the entry to a free-text track_title. String-typed to match Discogs\'s `release_track.position`.\n',
    )
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
    radio_hour: AwareDatetime | None = Field(
        None,
        description="Exact top-of-hour this breakpoint marks (ISO 8601), sourced from tubafrenzy's RADIO_HOUR. Distinct from `add_time`, which is the row's logging instant (~1 min before the hour). Optional and nullable: absent on servers predating the producer rollout, null for breakpoint rows not yet backfilled. Clients use `radio_hour` for the hour-marker label when present and fall back to `add_time` otherwise.",
    )
    message: str | None = None


class EntryType8(StrEnum):
    message = "message"


class FlowsheetV2MessageEntry(FlowsheetV2Base):
    entry_type: Literal["message"]
    message: str


class OnAirInfo(BaseModel):
    dj_name: str = Field(..., description="Display name of the DJ currently on air.")


class OnAirDJ(BaseModel):
    id: str = Field(
        ...,
        description="The DJ's better-auth `auth_user.id` (an opaque `varchar(255)` string), or `null` for a legacy/tubafrenzy-mirrored show whose on-air DJ has no Backend-Service account (their identity is `legacy_dj_name`, surfaced on `/flowsheet/djs-on-air` with a null id). Historically mistyped as `integer`; corrected to the nullable string it is at runtime (BS#1547).",
    )
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


class Dj(BaseModel):
    dj_id: int | None = None
    dj_name: str | None = None


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
    discogsUnavailable: bool | None = Field(
        None,
        description="MD-set marker indicating this release is intentionally not on\nDiscogs (embargoed promo, audience-segment release, etc.). When\ntrue, the LML runtime-lookup chokepoint does not attempt Discogs\nresolution for this release. See WXYC/wiki plans/rotation-discogs-unavailable.md.\n",
    )
    discogsUnavailableNote: constr(max_length=500) | None = Field(
        None, description="Optional free-text reason for `discogsUnavailable`."
    )
    lastDiscogsRecheckAt: AwareDatetime | None = Field(
        None,
        description="Stamped on every recheck attempt by the\n`library-discogs-unavailable-recheck` cron. Read-only from the\nclient side.\n",
    )


class UpdateAlbumRequest(BaseModel):
    album_title: str | None = None
    label: str | None = None
    label_id: int | None = None
    genre_id: int | None = None
    format_id: int | None = None
    artist_id: int | None = None
    alternate_artist_name: str | None = None
    disc_quantity: int | None = None
    discogsUnavailable: bool | None = None
    discogsUnavailableNote: constr(max_length=500) | None = None


class CatalogExportRow(BaseModel):
    id: int = Field(
        ...,
        description="BS serial library.id — the existing on-device clone key for the iOS Spotlight consumer. NOT the library.db id: the library.db producer uses legacy_release_id instead (see below), so BS serials never leak into the tubafrenzy id space.\n",
    )
    legacy_release_id: int | None = Field(
        None,
        description="The library row's surrogate key (BS#1963), total in the database since the mint + NULL-legacy backfill. The Backend-sourced library.db producer (discogs-etl#351) emits this AS library.db's library.id.\nServer-side totality does NOT make it a required wire key: it is optional + nullable here so a client regenerated from this spec before BS#1965 ships still decodes the current 15-field body (see the schema description). The producer may treat a null/absent value as \"this Backend does not implement BS#1965 yet\" and fail loudly rather than emitting a library.db row with a null id.\n",
    )
    artist_name: str = Field(
        ...,
        description="Authoritative artist name. The server COALESCEs the denormalized library.artist_name to artists.artist_name (NOT NULL), so this never ships as null even before the denormalization backfill completes.\n",
    )
    alternate_artist_name: str | None = Field(
        None,
        description="Secondary artist name curated by the librarians (library.alternate_artist_name); null when unset. Maps to library.db's alternate_artist_name column.\n",
    )
    album_artist: str | None = Field(
        None,
        description="Album-level artist (library.album_artist), distinct from the shelf-filing artist_name; null when unset. Maps to library.db's album_artist column. (Previously omitted from this export; re-added for the library.db producer.)\n",
    )
    cross_reference_names: list[str] | None = Field(
        None,
        description='Names of the artists cataloger-cross-referenced to this row\'s artist, in either FK direction, via artist_crossreference (discogs-etl#334) — e.g. a band filed under its name carries a member\'s personal name. Empty array when the artist has no cross-references.\nAn ARRAY, not the pipe-joined string library.db stores: the producer does the `" | "` join when it writes the SQLite column. The wire must not carry the delimiter, because nothing constrains artists.artist_name from containing "|" or " | " — a joined string would silently split into phantom aliases on the consumer side (library-metadata-lookup splits this field on the pipe) with no escaping rule to recover from, and the legacy MySQL source has the same latent bug plus GROUP_CONCAT\'s silent group_concat_max_len truncation. Array-on-the-wire retires both.\nDerivation (pinned, because the legacy query it replaces is load-bearing for the producer — LIBRARY_CODE_CROSS_REFERENCE in discogs-etl/scripts/sync-library.sh):\n  * BOTH FK directions — an artist_crossreference row matching on\n    either source_artist_id or target_artist_id contributes its\n    OTHER side.\n  * EXCLUDES the row\'s own artist (the legacy query\'s\n    `AND xlc.ID != lc.ID`). A self-referencing crossreference row\n    must not produce a self-alias — LML would match the artist\n    against itself.\n  * Deduplicated.\n  * Ordered by Unicode code point (Postgres `COLLATE "C"`), NOT the\n    database\'s default collation. en_US.UTF-8 ignores punctuation\n    and case at the primary level, so it orders the diacritic- and\n    punctuation-bearing names most likely to carry aliases (Nilüfer\n    Yanya, Csillagrablók, Hermanos Gutiérrez, "C. Spencer Yeh")\n    unstably across locales. The legacy GROUP_CONCAT has no ORDER BY\n    at all, so there is no legacy order to match — pick the\n    deterministic one.\n\nFRESHNESS: this endpoint\'s conditional GET rides library_watermark. Migration 0105 fanned the watermark trigger out to library, artists, genres, format, genre_artist_crossreference, and rotation; artist_crossreference was left uncovered until migration 0138 added touch_library_watermark_from_artist_crossreference (AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ... FOR EACH STATEMENT). A cross-reference write now advances the watermark, the per-watermark gzip cache rebuilds, and the producer\'s next fetch carries the new alias. Before 0138 this was silent and unbounded rather than a bounded lag — a write to an uncovered source table changed nothing observable until an unrelated write on a covered table happened to bump the watermark, which is why every table a field on this row is derived from must carry the trigger.\n',
    )
    album_title: str
    code_letters: str = Field(..., description='Shelf call-number letters (e.g. "AU").')
    code_number: int = Field(..., description="Shelf call-number release number.")
    code_artist_number: int = Field(
        ..., description="Shelf call-number artist number (genre-scoped)."
    )
    label: str | None = Field(
        None,
        description="Record label (library.label), projected as its real value — null only when the row genuinely has none. The library.db producer ignores it (the legacy MySQL export it must match has no label column), but every other consumer of this export should treat it as live data.\n",
    )
    genre_name: str
    format_name: str
    on_streaming: bool | None = Field(
        None,
        description="True if on >=1 streaming service; false if physical-only; null if unknown.",
    )
    plays: int | None = Field(
        None,
        description="Per-pressing LINKED play count for THIS catalog row (from album_plays). The honest per-release number; to rank by real popularity across all pressings of a logical album, use `popularity` instead.\n",
    )
    popularity: int | None = Field(
        None,
        description="Attribution-corrected popularity (BS#1486 Phase-2 Track 3): a play count for this row's LOGICAL album rather than this single pressing. Pressings that resolve to the same Discogs master (~90% of RESOLVED library rows) collapse into one count; release-only and unresolved rows keep a per-release or per-library key, with no cross-pressing collapse. It also adds the free-text/unlinked plays Track 1 has resolved to the same release/master (the ~43% free-text tail is the ceiling, not all of it — only the resolved subset counts), which the linked-only `plays` cannot see. Rank releases by `popularity`. null when the album_popularity signal has no row for this row's logical key. Distinct from `plays`, which stays the per-pressing linked count.\n",
    )
    artwork_url: str | None = Field(
        None, description="Album cover URL from Discogs; null if not yet fetched."
    )
    rotation_bin: str | None = Field(
        None,
        description="RAW current-rotation bin — the most-recently-ADDED rotation record for this album — NOT the CURRENT_DATE-filtered value AlbumSearchResult returns. Nominal values are H/M/L/S (see RotationBin) but it is typed as a free string, NOT the RotationBin enum, so a value outside those cohorts (e.g. 'N', a current server-enum member — NOT legacy) cannot break a strict-enum decoder. Evaluate live rotation client-side together with rotation_kill_date:\n  in rotation  ==  rotation_bin != null\n                  AND (rotation_kill_date == null\n                       OR rotation_kill_date > today-in-client-tz)\nThe daily kill-date expiry is a clock event no DB trigger can observe, which is why this endpoint ships raw and defers expiry to the client.\n",
    )
    rotation_kill_date: date_aliased | None = Field(
        None,
        description="Date (YYYY-MM-DD; server ::text cast) the current rotation record expires, or null if it has none. Used with rotation_bin to evaluate live rotation client-side. Absent from AlbumSearchResult — a client that reuses AlbumSearchResult for this endpoint silently loses it.\n",
    )


class CatalogCompilationTrackRow(BaseModel):
    legacy_release_id: int = Field(
        ...,
        description="The owning library row's legacy_release_id (BS#1963). Becomes library.db's compilation_track_artist.library_release_id, which joins to library.db's library.id.\n",
    )
    artist_name: constr(min_length=1, max_length=255) = Field(
        ...,
        description="Per-track performing artist (CTA.artist_name). Bounds mirror CompilationTrackInput and the underlying varchar(255) NOT NULL — the read and write shapes over the same physical column must not disagree about their own constraints, and the producer needs the width to size its SQLite column.\n",
    )
    track_title: constr(max_length=255) | None = Field(
        None, description="Track title; nullable varchar(255), matching the CTA column."
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


class CompilationTrackInput(BaseModel):
    artist_name: constr(min_length=1, max_length=255) = Field(
        ..., description="Per-track performing artist (CTA.artist_name)."
    )
    track_title: constr(max_length=255) | None = Field(
        None, description="Track title; nullable, matching the CTA column."
    )
    track_position: constr(max_length=20) | None = Field(
        None, description='Sleeve position label, e.g. "A1", "3".'
    )


class CompilationTrack(BaseModel):
    id: int = Field(..., description="Server-assigned CTA row id.")
    artist_name: str
    track_title: str | None = None
    track_position: str | None = None


class CompilationTrackList(BaseModel):
    library_id: int
    tracks: list[CompilationTrack]


class CompilationTracksWriteRequest(BaseModel):
    tracks: list[CompilationTrackInput] = Field(..., min_length=1)


class CompilationTracksWriteResponse(BaseModel):
    library_id: int
    inserted: int = Field(..., description="Rows newly created.")
    skipped: int = Field(
        ...,
        description="Rows already present (matched on a CTA uniqueness key); left untouched.",
    )
    tracks: list[CompilationTrack]


class CompilationTrackSuggestions(BaseModel):
    library_id: int
    discogs_release_id: int = Field(
        ..., description="The resolved Discogs release id, or null if none resolved."
    )
    tracks: list[CompilationTrackInput]


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


class DeviceRegistration(BaseModel):
    device_id: str
    registered_at: AwareDatetime


class DeviceToken(BaseModel):
    token: str
    expires_at: AwareDatetime


class Venue(BaseModel):
    id: int
    slug: str = Field(..., description='Stable scraper seed key (e.g. "cats-cradle").')
    name: str
    city: str
    state: str
    address: str


class ConcertStatus(StrEnum):
    on_sale = "on_sale"
    sold_out = "sold_out"
    cancelled = "cancelled"
    rescheduled = "rescheduled"


class SimilarArtist(BaseModel):
    artist_id: int = Field(
        ...,
        description="WXYC catalog artist id, same keyspace as `Concert.headlining_artist_id`.",
    )
    weight: float = Field(
        ...,
        description="semantic-index affinity score, used for client-side ranking and the similar-tier noise cap. Higher is closer.",
    )


class Concert(BaseModel):
    id: int
    venue: Venue
    starts_on: date_aliased = Field(
        ..., description="Venue-local (America/New_York) calendar date."
    )
    starts_at: AwareDatetime = Field(
        ..., description="Exact start instant; null for date-only events."
    )
    doors_at: AwareDatetime = Field(
        ..., description="Doors-open instant, when the source publishes one."
    )
    headlining_artist_raw: str = Field(
        ..., description="Headliner billing string exactly as the source displays it."
    )
    headlining_artist_id: int = Field(
        ...,
        description="WXYC catalog artist id when the resolver matched the headliner; null otherwise. A non-null id is what `curated=true` filters on.",
    )
    title: str = Field(
        ...,
        description="Event name as the source displays it, when distinct from the artist billing.",
    )
    supporting_artists_raw: list[str] = Field(
        ..., description="Supporting-act billing strings, in source order."
    )
    ticket_url: str
    image_url: str
    event_url: str = Field(
        ...,
        description="The venue's own event-detail page, distinct from `ticket_url` (often a third-party seller like Etix/Ticketmaster). Null when no venue page is known; clients fall back to `ticket_url`.",
    )
    price_min: float = Field(..., description="Dollars. Free events carry price_min = 0.")
    price_max: float = Field(..., description="Dollars.")
    age_restriction: str = Field(
        ..., description='Source-displayed age restriction (e.g. "18+", "All Ages").'
    )
    status: ConcertStatus
    genres: list[str] | None = Field(
        None,
        description="Discogs genre tags for the resolved headlining artist, aggregated across their releases (LML discogs-cache, majority-take). Null when the headliner is unresolved or enrichment has not run. Optional (not in `required`) so the field can land ahead of the Backend-Service emitter and older clients decode forward-compatibly — same discipline as `FlowsheetV2TrackEntry.upcoming_show`. Same taxonomy as `FlowsheetV2TrackEntry.genres`.",
    )
    similar_artists: list[SimilarArtist] | None = Field(
        None,
        description="Top-K affinity neighbors of the resolved headliner, computed nightly from the semantic-index graph and ordered by `weight` descending. Null when the headliner is unresolved or enrichment has not run. Powers on-device For You matching (set intersection against liked-artist ids). Optional (not in `required`) so it can land ahead of the Backend-Service emitter and older clients decode forward-compatibly — same discipline as `Concert.genres`.",
    )
    station_plays: int | None = Field(
        None,
        deprecated=True,
        description='Deprecated — use `station_recommended` instead. Play counts were rejected as the station-tier signal in the "WXYC recommends" redesign (WXYC/wxyc-ios-64#576); the tier now keys on rotation membership. All-time WXYC flowsheet play count of the resolved (in-library) headliner, computed nightly from the semantic-index graph; null when the headliner is not in the library or has no play count. Identical for every listener. Stays on the wire until the removal ticket (WXYC/Backend-Service#1732) clears its adoption gate; will be removed in a future major version.',
    )
    station_recommended: bool | None = Field(
        None,
        description='True when the concert\'s resolved headliner (`headlining_artist_id`) has at least one WXYC library release that has been in rotation — any rotation row, past or present ("has been in rotation", not "currently in rotation"). Omitted or false for concerts with no library-resolved headliner, including Discogs-only resolutions (`headlining_discogs_artist_id` without `headlining_artist_id` in the Backend), which by construction have no library releases to hold rotation membership. The rotation-membership signal behind the On Tour "For You" station tier ("WXYC recommends", WXYC/wxyc-ios-64#576); replaces the deprecated `station_plays`. This is the GATE (rotation membership) — see `station_recommended_rank` for the plays-ordered selection rank within that gate. Identical for every listener; carries no listener data. Optional (not in `required`) so it can land ahead of the Backend-Service emitter and older clients decode forward-compatibly — same discipline as `Concert.similar_artists`.',
    )
    station_recommended_rank: int | None = Field(
        None,
        description="1-based rank of this concert within the station's \"WXYC recommends\" set — among upcoming concerts whose resolved headliner is in rotation (station_recommended = true), ordered by the station's ranking signal (all-time WXYC plays), rank 1 = strongest. Null when the concert is not in the set (headliner not rotation-gated). This is the field the client sorts the station tier by; station_recommended is the gate (rotation membership), this is the plays-ordered selection rank within that gate. Identical for every listener; carries no listener data. Optional (not in `required`) and nullable so it can land ahead of the Backend-Service emitter and older clients decode forward-compatibly — same discipline as `Concert.station_plays`.",
    )
    artist_bio: str | None = Field(
        None,
        description='Artist biography from the resolved headliner\'s Discogs profile (raw Discogs markup, parsed client-side), keyed on the effective Discogs artist id and cached nightly on `artist_metadata`. Null when the headliner is unresolved, has no Discogs profile, or enrichment has not run. Identical for every listener; carries no listener data. Renders the On Tour concert-detail "About the Artist" card. Optional (not in `required`) so it can land ahead of the Backend-Service emitter and older clients decode forward-compatibly — same discipline as `Concert.similar_artists`.',
    )


class ConcertsResponse(BaseModel):
    concerts: list[Concert]
    pagination: PaginationInfo


class AlbumReview(BaseModel):
    id: int
    album_id: int = Field(
        ...,
        description="WXYC library album id when the free-text artist/album resolved to exactly one catalog row; null otherwise.",
    )
    artist_name: str = Field(..., description="Artist name exactly as the reviewer entered it.")
    album_title: str = Field(..., description="Album title exactly as the reviewer entered it.")
    record_label: str
    artist_blurb: str = Field(..., description="Short reviewer-written background on the artist.")
    review: str = Field(..., description="The review body.")
    recommended_tracks: str = Field(
        ...,
        description='Raw recommended-tracks text, including the form\'s `!`-rating notation (1–5 exclamation marks) and "Play All" shorthand.',
    )
    buzzwords: str = Field(..., description="Comma-separated reviewer-chosen descriptors.")
    fcc_violations: str = Field(
        ...,
        description='Verbatim reviewer answer listing FCC-violating track numbers. Blank (unanswered) is distinct from "None"/"N/A" (affirmatively clean); values are not normalized.',
    )
    review_purpose: str = Field(
        ...,
        description='Raw multi-select of "Rotation", "New DJ Assignment", "Album in the Library"; comma-joined when multiple were selected.',
    )
    rotated: bool = Field(
        ...,
        description='Curator-maintained "was this album rotated" flag; null when the sheet cell is blank or unparseable.',
    )
    released_within_six_months: bool = Field(
        ...,
        description="Whether the album was released within 6 months of the review; null when unanswered (question added mid-2024).",
    )
    social_consent: bool = Field(
        ...,
        description="Whether the reviewer consented to the review being shared on station social media (always anonymously); null when unanswered.",
    )
    submitted_at: AwareDatetime = Field(
        ...,
        description="Form submission instant. Null only for the rare row whose sheet timestamp is missing.",
    )


class AlbumReviewsResponse(BaseModel):
    album_reviews: list[AlbumReview]
    pagination: PaginationInfo


class DeviceAuthCodeRequest(BaseModel):
    client_id: str = Field(
        ..., description="The client ID of the application requesting authorization."
    )
    scope: str | None = Field(None, description="Optional space-separated list of OAuth scopes.")
    user_id: str | None = Field(
        None,
        description="Optional. The user ID to which the device code should be pre-bound (a better-auth 1.6.20+ plugin field). WXYC's shared-computer QR flow does not send this — the DJ is identified later at /auth/device/approve — but it is mirrored here for runtime fidelity.\n",
    )


class DeviceAuthCodeResponse(BaseModel):
    device_code: str = Field(
        ..., description="The device verification code, polled at /auth/device/token."
    )
    user_code: str = Field(..., description="The short code shown to the user / encoded in the QR.")
    verification_uri: AnyUrl = Field(..., description="URL where the user verifies the code.")
    verification_uri_complete: AnyUrl = Field(
        ...,
        description="verification_uri with user_code pre-filled as a query parameter.",
    )
    expires_in: int = Field(
        ...,
        description="Lifetime of the device/user code in seconds (WXYC config: 300 = 5min).",
    )
    interval: int = Field(..., description="Minimum polling interval in seconds (WXYC config: 5).")


class GrantType(StrEnum):
    urn_ietf_params_oauth_grant_type_device_code = "urn:ietf:params:oauth:grant-type:device_code"


class DeviceAuthTokenRequest(BaseModel):
    grant_type: GrantType = Field(..., description="RFC 8628 device-flow grant type (fixed value).")
    device_code: str = Field(..., description="The device_code returned by /auth/device/code.")
    client_id: str = Field(..., description="The client ID of the application.")


class TokenType(StrEnum):
    Bearer = "Bearer"


class DeviceAuthTokenResponse(BaseModel):
    access_token: str = Field(..., description="The session bearer token for the browser.")
    token_type: TokenType
    expires_in: int = Field(
        ...,
        description="Token lifetime in seconds. WXYC's auth-service (Backend-Service#1495, hooks.after on /device/token) rewrites the plugin's session-derived value to a fixed 43200 (12h). The vanilla-plugin value would otherwise be the global ~7-day session TTL.\n",
    )
    scope: str = Field(..., description="Granted scopes (empty string when none were requested).")


class DeviceAuthStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


class DeviceAuthVerifyResponse(BaseModel):
    user_code: str
    status: DeviceAuthStatus


class DeviceAuthApproveRequest(BaseModel):
    userCode: str = Field(
        ...,
        description="The user code to approve. NOTE: camelCase on the wire — the plugin's approve/deny bodies differ from the snake_case code/token bodies.\n",
    )


class DeviceAuthDenyRequest(BaseModel):
    userCode: str = Field(
        ...,
        description="The user code to deny. NOTE: camelCase on the wire (see DeviceAuthApproveRequest).\n",
    )


class DeviceAuthActionResponse(BaseModel):
    success: bool = Field(..., description="Always true on success. Shared by /approve and /deny.")


class DeviceAuthCodeErrorCode(StrEnum):
    invalid_request = "invalid_request"
    invalid_client = "invalid_client"


class DeviceAuthCodeError(BaseModel):
    error: DeviceAuthCodeErrorCode
    error_description: str


class DeviceAuthTokenErrorCode(StrEnum):
    authorization_pending = "authorization_pending"
    slow_down = "slow_down"
    expired_token = "expired_token"
    access_denied = "access_denied"
    invalid_request = "invalid_request"
    invalid_grant = "invalid_grant"
    server_error = "server_error"


class DeviceAuthTokenError(BaseModel):
    error: DeviceAuthTokenErrorCode
    error_description: str


class DeviceAuthVerifyErrorCode(StrEnum):
    invalid_request = "invalid_request"
    expired_token = "expired_token"


class DeviceAuthVerifyError(BaseModel):
    error: DeviceAuthVerifyErrorCode
    error_description: str


class DeviceAuthActionErrorCode(StrEnum):
    invalid_request = "invalid_request"
    expired_token = "expired_token"
    unauthorized = "unauthorized"
    access_denied = "access_denied"


class DeviceAuthActionError(BaseModel):
    error: DeviceAuthActionErrorCode
    error_description: str


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


class MetadataStatus(StrEnum):
    pending = "pending"
    enriching = "enriching"
    enriched_match = "enriched_match"
    enriched_no_match = "enriched_no_match"
    failed_no_retry = "failed_no_retry"


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


class StreamingResolutionStatus(StrEnum):
    verified = "verified"
    absent = "absent"
    unresolved = "unresolved"


class StreamingResolution(BaseModel):
    spotify: StreamingResolutionStatus | None = None
    apple_music: StreamingResolutionStatus | None = None
    bandcamp: StreamingResolutionStatus | None = None


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
    include_identity: bool | None = Field(
        False,
        description="Per cross-cache-identity plan §3.2.2 (E2-LML write contract). When true, the response carries an additional `identity` block (the §3.2.5 cascade's per-source resolution detail), and `api_version` is set to 2. When false (the default), the response is byte-identical to v0.5.0 — `identity` is absent and `api_version` is omitted. Backend's `library-identity-writer.ts` (E2-BS) sets this to true on every call; other consumers (catalog search, dj-site proxy, iOS apps) leave it false.\n",
    )
    extended: bool | None = Field(
        None,
        description="When true, the top-1 result's `artwork` block is populated with additional fields LML already fetches during enrichment but normally discards: `discogs_artist_id`, `tracklist`, `genres`, `styles`, `label`, `full_release_date`, `artist_image_url`, and `profile_tokens` (cache-only deep parse of the artist's profile markup). Lets a caller obtain a full playcut metadata payload in a single `/lookup` call instead of following up with separate `/discogs/release/{id}` and `/discogs/artist/{id}` requests. Absent or false leaves the response shape unchanged.\n",
    )
    warm_cache: bool | None = Field(
        None,
        description="When true, LML schedules a fire-and-forget background task after the response is built that runs a *deep* async parse of the top-1 artist's bio. The task resolves all `[a…]`/`[r…]`/`[m…]` references against the Discogs API where the local cache misses, warming the PG cache so subsequent reads of the same artist render richer bio tokens. Intended for write-path callers (e.g. Backend-Service's flowsheet-linkage service committing a new DJ entry); read-path callers should leave this absent/false to avoid doubling the Discogs-API load per request.\n",
    )


class LibraryCatalogItem(BaseModel):
    id: int = Field(
        ...,
        description='Unique identifier in the library database. `0` means there is no WXYC catalog row for this result (a row-less / "(external)" item, e.g. a Discogs-only library miss); any positive value is a real shelved release.\n',
    )
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
    library_url: str = Field(
        ...,
        description="Per-release dj.wxyc.org permalink for this release. Points at the dj-site legacy front door `/dashboard/album/legacy/{id}`, which resolves the legacy library `id` to the canonical release route server-side and 308-redirects. Empty string for a row-less result (`id == 0`).\n",
    )
    on_streaming: bool | None = Field(
        None,
        description="True if this release is available on at least one streaming service. False means only available in the WXYC physical library. Null if unknown.",
    )


class CacheStats(BaseModel):
    memory_hits: conint(ge=0) = Field(
        ..., description="Hits in LML's per-process in-memory TTL cache"
    )
    pg_hits: conint(ge=0) = Field(..., description="Hits in the discogs-cache PostgreSQL cache")
    pg_misses: conint(ge=0) = Field(
        ...,
        description="Misses in the discogs-cache PostgreSQL cache (forced an API call or fallback)",
    )
    api_calls: conint(ge=0) = Field(
        ..., description="Number of Discogs API calls made during the lookup"
    )
    pg_time_ms: confloat(ge=0.0) = Field(
        ..., description="Total milliseconds spent in PostgreSQL cache lookups"
    )
    api_time_ms: confloat(ge=0.0) = Field(
        ..., description="Total milliseconds spent in Discogs API calls"
    )


class ApiVersion(IntEnum):
    integer_2 = 2


class SearchType(StrEnum):
    direct = "direct"
    fallback = "fallback"
    alternative = "alternative"
    compilation = "compilation"
    song_as_artist = "song_as_artist"
    none = "none"


class DegradedReason(StrEnum):
    deadline_exceeded = "deadline_exceeded"
    cache_only = "cache_only"
    upstream_unavailable = "upstream_unavailable"


class IdentitySource(StrEnum):
    discogs = "discogs"
    musicbrainz = "musicbrainz"
    wikidata = "wikidata"
    spotify = "spotify"
    apple_music = "apple_music"
    bandcamp = "bandcamp"


class IdentityMethod(StrEnum):
    manual = "manual"
    cross_source_agreement = "cross_source_agreement"
    exact_match = "exact_match"
    name_variation = "name_variation"
    member_group = "member_group"
    alias_match = "alias_match"
    trigram = "trigram"
    llm = "llm"


class IdentitySkipReason(StrEnum):
    error = "error"
    manual_override_protected = "manual_override_protected"
    disabled = "disabled"
    prerequisite_failed = "prerequisite_failed"


class IdentityResolution(BaseModel):
    source: IdentitySource
    attempted: bool = Field(
        ...,
        description="True when this source's leg actually ran (whether or not it found a match). False when it was skipped — `reason` is populated, `external_id`/`method`/`confidence` are NULL.\n",
    )
    external_id: str | None = Field(
        None,
        description="The external identifier this leg resolved to (Discogs release ID, MusicBrainz MBID, Wikidata QID, Spotify URI suffix, Apple Music ID, Bandcamp slug). Stored as a string regardless of the source's native type so the API surface is uniform; Backend casts to the per-source column type when writing `library_identity_source`. NULL when `attempted: false`, or when the leg ran but found no match.\n",
    )
    method: IdentityMethod | None = Field(
        None,
        description="The matcher method that produced the resolution. NULL when `attempted: false` or when the leg ran but found no match.\n",
    )
    confidence: confloat(ge=0.0, le=1.0) | None = Field(
        None,
        description="Confidence in [0, 1]. Must fall within the method's locked range from plan §3.4.1 — Backend's writer rejects rows where confidence is out of range. NULL when `attempted: false` or when the leg ran but found no match.\n",
    )
    reason: IdentitySkipReason | None = Field(
        None,
        description="Required when `attempted: false`; absent or NULL otherwise.\n",
    )


class LookupIdentityBlock(BaseModel):
    resolved: list[IdentityResolution] = Field(
        ...,
        description="One entry per known source. Order is stable (matches the `IdentitySource` enum order). Always populated even when no source resolved — the array carries `attempted: false` entries in that case.\n",
    )


class BulkResolveInput(BaseModel):
    library_id: int = Field(..., description="Backend `wxyc_schema.library.id` (FK target).")
    artist_name: str = Field(
        ..., description="Hint — Backend's denormalized artist name for the row."
    )
    album_title: str = Field(
        ..., description="Hint — Backend's denormalized album title for the row."
    )


class BulkResolveResultKind(StrEnum):
    single_artist = "single_artist"
    compilation = "compilation"
    unresolved = "unresolved"


class BulkResolveProvenanceEntry(BaseModel):
    source: IdentitySource
    method: IdentityMethod
    confidence: confloat(ge=0.0, le=1.0) = Field(
        ...,
        description="Per-source confidence in [0, 1], within the source's locked method range from §3.4.1. NULL when `external_id` is NULL — the source ran but produced no candidate, so confidence is undefined (not zero). When non-null, equal to or greater than the `confidence` of the object this entry hangs off — the composition rules either MIN or boost, and never lower a per-source row. That referent is grain-relative, because this schema is reused at two grains: under `BulkResolveResult.provenance` it is the result's own `confidence`, and under `BulkResolveTrackIdentity.sources` it is that track's `confidence`, not the result's. The two are unrelated — a `kind: compilation` result has no result-level `confidence` at all, while its tracks each have their own.\n",
    )
    external_id: str | None = Field(
        None,
        description="The external identifier this source resolved to (Discogs release ID, MusicBrainz MBID, Wikidata QID, Spotify URI suffix, etc.). NULL when the source ran but found no match (the row is still surfaced so consumers see the leg ran); `confidence` is also NULL in that case.\n",
    )


class BulkResolveTrackIdentity(BaseModel):
    track_position: str = Field(
        ...,
        description='Track position as the source has it ("A1", "3", …). Required-but-nullable: the key is always present, and NULL says "no position is recoverable for this track" rather than "not echoed". Not a usable row identifier on its own — most of Backend\'s compilation-track rows are position-NULL (WXYC/Backend-Service#1989); join on `artist_name` + `track_title` instead.\n',
    )
    artist_name: constr(min_length=1) = Field(
        ...,
        description="Join-back echo of the per-track credit, verbatim, and the load-bearing half of the join key — never NULL, and never the empty string (`minLength: 1`, matching `CatalogCompilationTrackRow.artist_name`, since an empty join key is indistinguishable from a missing one). No `maxLength` here, unlike the CTA read/write shapes: those bound one physical `varchar(255)` column, while this field is dual-mode and the `kind: single_artist` arm carries a source credit that no WXYC column bounds. A cap the sources don't respect would turn a long credit into a decode failure. Dual-mode: for `kind: compilation` it echoes the `compilation_track_artist` row as exported in library.db, so a consumer can match it to its own CTA row; for `kind: single_artist` there is no CTA row and it carries the credit as LML's source has it.\n",
    )
    track_title: str = Field(
        ...,
        description="Join-back echo of the track title, completing the join key. Dual-mode in the same way as `artist_name` — the library.db CTA row's title for `kind: compilation`, the source's track title for `kind: single_artist`. NULL when that row or source carries no title (the underlying CTA column is nullable).\n",
    )
    resolved_artist_name: str = Field(
        ...,
        description="The composed canonical artist for this track — the per-track analogue of `BulkResolveResult.main`, carried as a name because LML does not know Backend's artist ids; consumers map it to their own artist tables locally. NULL when the matcher visited this track and resolved nothing: the entry is still returned so consumers see the leg ran (same convention as `BulkResolveProvenanceEntry.external_id`), and `confidence` and `method` are NULL alongside it.\n",
    )
    confidence: confloat(ge=0.0, le=1.0) = Field(
        ...,
        description="Composed track-level confidence in [0, 1] — where LML#1021's per-track composition lands (cross-source agreement boost, MIN-of-confidences fallback), applied at track grain exactly as `BulkResolveResult.confidence` applies at album grain. NULL when `resolved_artist_name` is NULL — the matcher ran and produced no verdict, so confidence is undefined (not zero).\n",
    )
    method: IdentityMethod = Field(
        ...,
        description="The composed track-level method — typically the strongest leg's method, or `cross_source_agreement` when LML#1021's boost fired. NULL when `resolved_artist_name` is NULL.\n",
    )
    sources: list[BulkResolveProvenanceEntry] = Field(
        ...,
        description="One per-source row per source LML composed the track from — audit detail, and the track-grain analogue of `BulkResolveResult.provenance`. Empty array means no source produced a row at track grain at all. That is a different statement from a populated array whose entries carry NULL `external_id`: there, the legs ran and reported no candidate. Read the verdict off `resolved_artist_name`, not off this array's length — an empty `sources` and a `sources` full of NULL-`external_id` legs both accompany a NULL `resolved_artist_name`.\n",
    )


class BulkResolveResult(BaseModel):
    kind: BulkResolveResultKind
    library_id: int = Field(..., description="Backend `library.id` for this result.")
    main: ReconciledIdentity | None = Field(
        None,
        description="Composed external IDs for `kind: single_artist`. NULL for every other `kind`. URL construction is the consumer's job (see existing ReconciledIdentity contract).\n",
    )
    method: IdentityMethod | None = Field(
        None,
        description="The composed top-level method LML's bulk-resolve handler picked per §3.4.1.1 (typically the strongest leg's method, or `cross_source_agreement` when Rule 2 boosted). NULL for `kind: unresolved`.\n",
    )
    confidence: confloat(ge=0.0, le=1.0) | None = Field(
        None,
        description="Composed top-level confidence in [0, 1] — the MIN of resolved sources' confidences after composition (§3.4.1.1 Rule 4), unless boosted by cross-source agreement (Rule 2). NULL for `kind: unresolved`.\n",
    )
    provenance: list[BulkResolveProvenanceEntry] = Field(
        ...,
        description="Per-source rows feeding LML's composition. Always present; empty array means LML attempted the cascade and no source produced a row above the §3.4.1.1 Rule 6 floor.\n",
    )
    tracks_attempted: bool | None = Field(
        None,
        description='Whether LML\'s per-track matcher has visited this row — `true` once it has run, **regardless of how many tracks it resolved**, including none. This is the resolved signal for per-track identity; `tracks.length` is not (WXYC/Backend-Service#1991).\n\nRead it with `tracks` as a pair, four states in total: **absent or NULL** — the caller did not ask (`include_tracks` false or omitted), or the result is `kind: unresolved`, and `tracks` is in the same state; **`false` with an empty `tracks`** — the caller asked and the matcher has not reached this row; **`true` with an empty `tracks`** — the matcher ran and resolved nothing; **`true` with a populated `tracks`** — the matcher ran and produced entries. `false` alongside a populated `tracks` is not a state; producers must not emit it.\n\nThe middle two are what this field exists for. They are byte-identical in `tracks`, they are not rare — a `kind: single_artist` release LML holds no tracklist for is the ordinary case, and extending the gate to that kind is the whole point of #297 — and a consumer that reads emptiness as "not yet visited" re-asks every one of them on every pass, forever. That is the re-asking pathology `kind: unresolved` was made a first-class outcome to prevent, reintroduced at track grain. A consumer cannot repair it locally, because the two states are indistinguishable on the wire without this field.\n\nAbsent and NULL are one state, for the same producer reason as `tracks` below.\n',
    )
    tracks: list[BulkResolveTrackIdentity] | None = Field(
        None,
        description='Per-track identity, returned only when the request set `include_tracks: true`. That flag — not `kind` — gates the field, and it gates it on both `kind: single_artist` and `kind: compilation` (#297); `kind: unresolved` never carries tracks. The array carries the entries; **it does not carry the state** — an empty array means the matcher found nothing *or* has not run, and only `tracks_attempted` separates those. See that field for the four states of the pair. Entries whose `resolved_artist_name` is NULL are tracks the matcher tried and failed on ("the leg ran", the same convention as `BulkResolveProvenanceEntry.external_id`), so a populated array is per-track detail rather than a per-track guarantee.\n\nAbsent and NULL are the same state, deliberately. LML builds every non-track result with `tracks=None` and serves the endpoint through FastAPI\'s `response_model` without `response_model_exclude_none`, so the wire has always carried `"tracks": null` rather than omitting the key. `nullable: true` documents the shipped producer instead of describing a wire nobody emits; consumers must accept both spellings.\n',
    )


class BulkResolveLibrariesResponse(BaseModel):
    results: list[BulkResolveResult]
    cache_stats: CacheStats | None = None


class IdentityKind(StrEnum):
    release = "release"


class ReleaseIdentitySource(StrEnum):
    discogs_release = "discogs_release"
    discogs_master = "discogs_master"
    bandcamp = "bandcamp"
    apple_music_album = "apple_music_album"


class ReleaseIdentityResolveRequest(BaseModel):
    kind: IdentityKind
    source: ReleaseIdentitySource
    external_id: str = Field(
        ...,
        description="Source-specific identifier. For `discogs_release` / `discogs_master` it is the positive integer ID as a string; zero and negative values are rejected with 422 (Discogs uses `0` for the unknown-release sentinel). For `bandcamp` it is the canonical album URL (e.g. `https://autechre.bandcamp.com/album/confield`); LML URL-canonicalises before mint so trailing-slash and equivalent variants converge on one identity row. Non-album Bandcamp URLs (track URLs, bare subdomain) are rejected with 422. For `apple_music_album` it is the numeric Apple Music album ID as a string (e.g. `1234567890`); zero, negative, and non-numeric values are rejected with 422.\n",
    )


class ReleaseIdentityResolveResponse(BaseModel):
    identity_id: int = Field(
        ...,
        description="`entity.release_identity.id` for the resolved row. Stable across calls with the same `(kind, source, external_id)`.\n",
    )
    kind: IdentityKind
    minted: bool = Field(
        ...,
        description="`true` when this call inserted a new row, `false` when an existing row was returned. Useful for callers that want to observe whether they were the first to surface a given release.\n",
    )


class ArtistSearchAliasSource(StrEnum):
    discogs_name_variation = "discogs_name_variation"
    discogs_alias = "discogs_alias"
    discogs_member = "discogs_member"
    wxyc_library_alt = "wxyc_library_alt"


class ArtistSearchAliasMethod(StrEnum):
    name_variation = "name_variation"
    alias = "alias"
    member = "member"
    alt_curated = "alt_curated"


class ArtistSearchAliasVariant(BaseModel):
    source: ArtistSearchAliasSource
    variant: str = Field(..., description='The searchable string (e.g. "Thee Oh Sees").')
    related_external_id: str | None = Field(
        None,
        description='For alias / member kinds, the namespaced source-side id of the related artist (e.g. "discogs:artist:67890", "musicbrainz:artist:<mbid>"). NULL for name_variation and alt_curated.\n',
    )
    related_name: str | None = Field(
        None,
        description="For alias / member kinds, the canonical name of the related artist as the source records it. NULL otherwise.\n",
    )
    active: bool | None = Field(
        None,
        description="For member kind only — Discogs records active/inactive band membership. NULL for other kinds.\n",
    )
    method: ArtistSearchAliasMethod
    confidence: confloat(ge=0.0, le=1.0) = Field(
        ...,
        description="Per-source default confidence (e.g. 0.95 for discogs_name_variation, 0.85 for discogs_alias / wxyc_library_alt, 0.70 for discogs_member). v1 search ignores this; stored for v2 ranking calibration.\n",
    )


class ArtistSearchAliasesResult(BaseModel):
    name: str = Field(..., description="The WXYC canonical artist name from the request.")
    variants: list[ArtistSearchAliasVariant]
    sources_present: list[ArtistSearchAliasSource] = Field(
        ...,
        description="Sources the composer actually queried for this artist. A source appears here iff the composer reached its fetch code path — independent of whether that path produced any variants. Consumers use this to scope reconciliation: only delete cached rows whose `source` is listed here. Sources skipped because of missing prerequisites (e.g., no `discogs_artist_id` in `entity.identity`) are ABSENT from this list, and the consumer must leave their cached rows alone — that source did not run, so its absence is not evidence of upstream deletion. Modeled on the empty-provenance semantic of `BulkResolveProvenanceEntry`.\n",
    )


class ArtistSearchAliasesBulkRequest(BaseModel):
    names: list[str] = Field(
        ..., description="WXYC canonical artist names.", max_length=1000, min_length=1
    )


class ArtistSearchAliasesBulkResponse(BaseModel):
    artists: list[ArtistSearchAliasesResult]
    missing: list[str] = Field(
        ...,
        description="Input names with no `entity.identity` row in LML — i.e., LML doesn't know any external IDs for these. BS can choose to retry later (after a discogs-cache rebuild and entity-resolution campaign) or leave them.\n",
    )
    cache_stats: CacheStats | None = None


class ArtistResolveMethod(StrEnum):
    identity_store = "identity_store"
    api_search = "api_search"


class ArtistResolveCacheLeg(StrEnum):
    cache_exact = "cache_exact"
    cache_member = "cache_member"
    cache_alias = "cache_alias"
    cache_name_variation = "cache_name_variation"
    cache_trigram = "cache_trigram"


class ArtistResolveUnresolvedReason(StrEnum):
    not_found = "not_found"
    ambiguous = "ambiguous"
    escalation_unavailable = "escalation_unavailable"


class ArtistResolveResult(BaseModel):
    name: str = Field(..., description="Verbatim echo of the input name at this index.")
    discogs_artist_id: int | None = Field(
        None,
        description="The resolved Discogs artist id. Non-null iff resolved (serialized as an explicit null on unresolved verdicts — the response never omits fields).\n",
    )
    canonical_name: str | None = Field(
        None,
        description="The raw Discogs artist title, disambiguation suffix included (e.g. `Popsicle (2)`) — the true Discogs string, kept for provenance; callers render their own input name. Present iff resolved via `api_search`: `entity.identity` rows store no Discogs title, so `identity_store` resolutions omit it.\n",
    )
    method: ArtistResolveMethod | None = Field(
        None,
        description="What decided the resolution. Non-null iff resolved (serialized as an explicit null on unresolved verdicts).\n",
    )
    cache_corroboration: list[ArtistResolveCacheLeg] = Field(
        ...,
        description="Cache legs that produced at least one candidate for this name's identity-match form, on BOTH verdict kinds — the per-leg yield telemetry sizing a possible v2 alias arm. On resolved verdicts, listed equality legs necessarily agreed with the deciding tier (a disagreeing equality leg forces `ambiguous`), while `cache_trigram` entries are fuzzy near-misses that never veto (`cache_trigram` is measured against the group's probe string — its lexicographically least sanitized spelling — where the equality legs bind the identity-match form). Empty when no leg produced candidates, and ALWAYS empty — the cache was never consulted, which is not a measured zero-yield — on (a) `identity_store` short-circuits (the store decides before cache evidence is consulted), (b) qualifier-bearing inputs (stripped-form evidence cannot distinguish family members, so the cache tier is skipped), and (c) store-conflict `ambiguous` verdicts.\n",
    )
    unresolved_reason: ArtistResolveUnresolvedReason | None = Field(
        None,
        description='Why the name did not resolve. Non-null iff unresolved (serialized as an explicit null on resolved verdicts). The response carries every field explicitly — consumers must treat null, not absence, as the "other kind" marker.\n',
    )
    candidate_count: int | None = Field(
        None,
        description='Exact-form candidates the API tier observed: 1 on resolved via `api_search`, 0 on not_found; on ambiguous, >= 2 for an overloaded family, or exactly 1 when the ambiguity is an equality-leg cache conflict or a qualifier mismatch between the lone candidate\'s own title and the input (a "(N)"-titled singleton answering a bare input, or vice versa, is family evidence — never a resolution). Always serialized — never omitted; null when the API tier did not run: identity_store short-circuit, escalation_unavailable, or an `ambiguous` verdict from conflicting identity-store rows within one deduplicated group (the store contradicting itself is doubt without a measurement; such verdicts also carry an empty `cache_corroboration` — the cache tier was never consulted) — null means "not measured," never zero.\n',
    )


class Name(RootModel[constr(min_length=1, max_length=255)]):
    root: constr(min_length=1, max_length=255)


class ArtistResolveBulkRequest(BaseModel):
    names: list[Name] = Field(
        ...,
        description='Bare artist names to resolve, verbatim (Unicode format characters are dropped and whitespace trimmed for all internal work; responses echo the raw string). Inputs are deduplicated internally on their fixed-point identity-match form PLUS any trailing parenthesized/bracketed qualifier. Qualifier detection mirrors the normalizer: every balanced trailing ()/[] group peels off the NFKC-folded lowercase name (nested tails included), bare-number groups canonicalize across bracket/width/spacing spellings ("[2]", "( 2 )", "（２）" all key as "(2)"; zero-padded "(02)" stays distinct), and multi-group tails concatenate ("(2)(uk)"); non-numeric groups drop internal whitespace, so "(Chk Chk Chk)" and "(ChkChkChk)" share a key. Duplicate positions receive the shared verdict, but a qualified name is its own work unit — a Discogs-style disambiguator denotes a different artist than the bare name, so it never inherits (or supplies) the bare form\'s verdict, and it resolves only when the API candidate\'s own title carries the same qualifier. A name whose only content IS a bracketed group ("(Smog)") is a bare name, not a qualifier — the normalizer makes the same refusal. A verified mint is keyed on the group\'s lexicographically-least sanitized spelling (deterministic in batch content — the same representative the API probe uses; the store\'s read legs are case-insensitive, so any spelling stays findable), EXCEPT when the identity read found an existing row for the name that lacks `discogs_artist_id` — the mint then fills that row\'s stored key in place rather than inserting a near-duplicate key (deterministically, the lexicographically-least stored key when several id-less rows match); qualified names never mint. Names that are blank, contain U+0000 or unencodable code points, normalize to an empty identity-match form, or whose entire identity content is a bare parenthesized ASCII number ("(2)", "[2]", "(2)(3)" — a Discogs disambiguator whose artist name was lost upstream; a fully-bracketed NON-numeric name like "(Smog)" stays resolvable) are rejected with 422 — never given an in-band verdict (`not_found` means the API tier ran and measured zero).\n',
        max_length=25,
        min_length=1,
    )
    dry_run: bool | None = Field(
        False,
        description="Run every tier identically — including live Discogs API verification — but skip the `entity.identity` write-back. No partial-write mode.\n",
    )


class ArtistResolveBulkResponse(BaseModel):
    results: list[ArtistResolveResult]


class ArtistGenresSource(StrEnum):
    cache = "cache"
    discogs_api = "discogs_api"
    not_found = "not_found"
    unavailable = "unavailable"


class ArtistGenresInput(BaseModel):
    artist_name: constr(min_length=1) = Field(..., description="Artist display name.")
    discogs_artist_id: int | None = Field(
        None,
        description="Discogs artist ID. Optional but strongly preferred: when present the cache aggregation keys on it (stable Discogs artist entity, homonym-safe); when absent it falls back to a case-insensitive exact `artist_name` match. Backend-Service resolves it via `POST /api/v1/artists/resolve/bulk` (LML#759) before calling this endpoint, so it is usually present.\n",
    )


class BulkArtistGenresRequest(BaseModel):
    artists: list[ArtistGenresInput] = Field(..., max_length=25, min_length=1)


class ArtistGenresResultItem(BaseModel):
    artist_name: str = Field(..., description="Echoed from the request for index alignment.")
    discogs_artist_id: int | None = Field(
        None,
        description="Echoed from the request (explicit null when not supplied). Kept out of `required` deliberately: datamodel-codegen (LML's generator) types a required-plus-nullable field as non-nullable and would reject the null this field must carry — the same treatment as `ArtistResolveResult.discogs_artist_id`.\n",
    )
    genres: list[str] = Field(
        ...,
        description="Top coarse Discogs genres (majority-take, frequency-ranked, truncated to the top-K). Empty when unknown. The ~15-value Discogs genre taxonomy the iOS On Tour filter chips render.\n",
    )
    styles: list[str] = Field(
        ...,
        description="Frequency-ranked Discogs styles (full list, untruncated). Empty when unknown. The finer Discogs style taxonomy; the iOS filter ignores it — it is a detail-view treatment.\n",
    )
    source: ArtistGenresSource


class BulkArtistGenresResponse(BaseModel):
    results: list[ArtistGenresResultItem]


class CacheRefreshSourceOutcome(StrEnum):
    success = "success"
    error = "error"
    not_implemented = "not_implemented"


class CacheRefreshItemStatus(StrEnum):
    warmed = "warmed"
    not_found = "not_found"
    not_implemented = "not_implemented"
    error = "error"


class CacheRefreshArtistOutcome(BaseModel):
    external_id: str
    outcome: CacheRefreshSourceOutcome
    message: str | None = Field(
        None,
        description="Exception class name when `outcome != success`. Bare `str()` of the exception is intentionally NOT serialized — it may carry upstream error bodies, SQL fragments, or file paths. Full traceback lands in Sentry.\n",
    )


class CacheRefreshSourceResult(BaseModel):
    release_outcome: CacheRefreshSourceOutcome
    artists: list[CacheRefreshArtistOutcome] | None = Field([], validate_default=True)
    message: str | None = None


class CacheRefreshResultItem(BaseModel):
    identity_id: int
    status: CacheRefreshItemStatus
    sources: dict[str, CacheRefreshSourceResult] | None = None
    message: str | None = None


class BulkCacheRefreshRequest(BaseModel):
    identity_ids: list[int] = Field(..., min_length=1)


class BulkCacheRefreshResponse(BaseModel):
    results: list[CacheRefreshResultItem]


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


class Provenance(StrEnum):
    track = "track"
    release = "release"


class DiscogsWriterCredits(BaseModel):
    names: list[str] = Field(
        ..., description="Distinct songwriter/composer names for the resolved track."
    )
    roles: list[str] | None = Field(
        None,
        description='The verbatim Discogs role strings the names were drawn from (e.g. "Written-By", "Words By, Music By"), for auditability of the writer-role mapping.\n',
    )
    provenance: Provenance = Field(
        ...,
        description="`track` = scoped to the resolved track's per-track credits (precise); `release` = a release-level credit applied to the whole release (approximate for an individual track, mirroring tubafrenzy's auto-fill-from-artist fallback). Populated as `release` in the initial rollout; `track` is added when per-track resolution lands.\n",
    )
    track_position: str | None = Field(
        None,
        description='The resolved track\'s position (e.g. "A1", "5") when `provenance` is `track`; null for release-level credits.\n',
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
    master_id: int | None = Field(
        None,
        description="Discogs master ID for this release, when the release belongs to a master. `null` when Discogs has no master (one-offs, self-released). Lets a caller collapse multiple pressings/formats of one logical album into a single record keyed on the master.\n",
    )
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
    artwork_checked_at: AwareDatetime | None = Field(
        None,
        description="Timestamp of the most recent live Discogs API call that resolved\nthis release's artwork. `null` means LML's bulk loader populated\nthe row but the live API has not been queried yet (the \"never\nasked\" state); a value means LML hit the live API and either\npopulated `artwork_url` or confirmed Discogs has no cover. LML\nuses this to distinguish bulk-loader gaps (which it back-fills)\nfrom genuinely-imageless releases (which it does not refetch).\nSee WXYC/discogs-etl#239 + WXYC/library-metadata-lookup#423.\n",
    )
    not_found: bool | None = Field(
        False,
        description='Tombstone marker for Discogs 404s on `get_release`. `true` means\nLML hit the live Discogs API for this release id and Discogs\nreturned 404; subsequent reads short-circuit on this flag rather\nthan re-burning the rate-limit budget. The tombstone row carries\n`title = ""` and `artist = ""` as identifier sentinels;\nconsumers must guard against rendering those empty strings as\nreal values. `release_url` is identifier-derived\n(`https://www.discogs.com/release/{release_id}`) and remains\nvalid even on a tombstone. LML\'s public boundary translates\n`not_found = true` back to `None` for direct callers; this flag\nis observable only by consumers that read the cache row\ndirectly (none of which exist in LML\'s public API today, but\nthe contract is exposed here for future cross-service readers).\nSee WXYC/library-metadata-lookup#510.\n',
    )
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


class TrackMatchSource(StrEnum):
    cta = "cta"
    discogs_release = "discogs_release"
    discogs_master = "discogs_master"
    library_identity = "library_identity"


class TrackMatchHint(BaseModel):
    title: str = Field(..., description="The track title as stored in the source.")
    artist_credit: str | None = Field(
        None,
        description="Per-track artist credit (for compilations, where each track may credit a distinct artist). Null for non-comp tracks where the release-level artist applies.\n",
    )
    position: str | None = Field(
        None,
        description='Track position on the release as stored in the source (e.g., "A1", "B2", "5"). String-typed to match Discogs\'s `release_track.position` shape, which uses vinyl-side notation for LP releases. Null when position is unknown or inapplicable (e.g., CTA-derived matches, since `compilation_track_artist` has no position column).\n',
    )
    confidence: confloat(ge=0.0, le=1.0) | None = Field(
        None,
        description="Confidence score for the underlying identity match. Sourced from `library_identity.confidence` post-cross-cache-identity, from `canonical_entity_confidence` pre-cross-cache-identity, or fixed at 1.0 for `cta`-source hits (the curated VA-disambiguation data is treated as authoritative). Used by the UI to render qualitative tooltips on the chip; consumers should not assume null for `cta` source.\n",
    )
    source: TrackMatchSource


class ArtistMatchHint(BaseModel):
    matched_variant: str = Field(
        ..., description="The cached alias string that trigram-matched the query."
    )
    source: ArtistSearchAliasSource


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
    library_url: str | None = Field(
        None,
        description="Per-release dj.wxyc.org permalink for this release. Points at the dj-site legacy front door `/dashboard/album/legacy/{id}`, which resolves the legacy library `id` to the canonical release route server-side and 308-redirects. Null when unavailable.\n",
    )
    matched_via: list[TrackMatchHint] | None = Field(
        None,
        description="Populated when a track-title match drove this release into the results (catalog-track-search plan §5.1). Empty or absent when the release matched on artist / title normally. Backward-compatible — existing consumers ignore the field.\n",
    )
    matched_via_alias: list[ArtistMatchHint] | None = Field(
        None,
        description="Sibling to `matched_via`. Reserved for the day LML composes artist-alias hits itself; not produced in v1 of the artist-search-alias plan (BS-local cache only). Optional and backward-compatible.\n",
    )


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
        description='True if found on any service, false if all services confirmed absent (no errors),\nnull if inconclusive — either no services checked OR at least one service raised\nan error (see `errored_sources`). Treat null as "do not persist" / "retry later".\n',
    )
    sources: StreamingCheckSources
    errored_sources: list[str] | None = Field(
        None,
        description="Names of services whose check raised an exception (transient network/rate-limit/\nscraping failure). Empty when every dispatched check completed without raising.\nWhen non-empty, callers should consider the listed services unchecked and may\nschedule a selective retry. Independent of `on_streaming`: a service can both\nmatch (populating `sources.<service>`) and other services can error.\n",
    )


class AppConfig(BaseModel):
    posthogApiKey: str = Field(..., description="PostHog analytics write key (public by design)")
    posthogHost: str = Field(..., description="PostHog ingestion host")
    requestOMaticUrl: str = Field(..., description="Request-o-matic service URL for song requests")
    apiBaseUrl: str = Field(..., description="Backend API base URL")


class TrackListItem(BaseModel):
    position: str = Field(..., description='Track position (e.g. "1", "A1")')
    title: str = Field(..., description="Track title")
    duration: str | None = Field(None, description='Track duration (e.g. "5:23")')


class CriticReviewItem(BaseModel):
    source: str = Field(..., description='Publication name (e.g. "The Quietus")')
    url: str = Field(
        ...,
        description="Link to the original review on the publisher's site (mandatory link-out)",
    )
    snippet: str = Field(
        ...,
        description="Short attributed excerpt (<= ~300 chars); never the full review body",
    )
    author: str | None = Field(None, description="Review author, when available")
    publishedDate: str | None = Field(
        None, description='Review publication date when available (e.g. "2024-03-15")'
    )
    rating: str | None = Field(
        None, description='Source-native rating string when available (e.g. "8.0")'
    )


class WxycReviewItem(BaseModel):
    review: str = Field(..., description="The DJ review body")
    artistBlurb: str | None = Field(
        None, description="Reviewer-written artist background, when provided"
    )
    recommendedTracks: str | None = Field(
        None,
        description='Raw recommended-tracks text, including the form\'s "!"-rating notation',
    )
    buzzwords: str | None = Field(
        None, description="Comma-separated descriptors chosen by the reviewer"
    )
    submittedDate: str | None = Field(
        None, description='Form submission date when available (e.g. "2024-03-15")'
    )


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
    criticReviews: list[CriticReviewItem] | None = Field(
        None,
        description="Attributed external critic-review snippets, each linking out to the original",
    )
    wxycReviews: list[WxycReviewItem] | None = Field(
        None,
        description="Consented WXYC DJ album reviews (social_consent = true) for this album. Reviewer identity is never included.",
    )
    spotifyUrl: str | None = Field(None, description="Spotify URL for the album or track")
    appleMusicUrl: str | None = Field(None, description="Apple Music URL for the album or track")
    youtubeMusicUrl: str | None = Field(None, description="YouTube Music search URL")
    bandcampUrl: str | None = Field(None, description="Bandcamp search URL")
    soundcloudUrl: str | None = Field(None, description="SoundCloud search URL")
    discogsUnavailable: bool | None = Field(
        None,
        description="True when a music director has marked this release as not on Discogs; downstream render surfaces should suppress Discogs-derived artwork/links/tracklist. Mirrors the `Album` schema's MD-set marker (WXYC/wiki plans/rotation-discogs-unavailable.md).",
    )
    discogsUnavailableNote: constr(max_length=500) | None = Field(
        None, description="Optional free-text reason for `discogsUnavailable`."
    )
    lastDiscogsRecheckAt: AwareDatetime | None = Field(
        None,
        description="Stamped on every recheck attempt by the\n`library-discogs-unavailable-recheck` cron. Read-only from the\nclient side.\n",
    )


class ArtistMetadataResponse(BaseModel):
    discogsArtistId: int | None = Field(None, description="Discogs artist ID")
    bio: str | None = Field(None, description="Artist biography from Discogs")
    wikipediaUrl: str | None = Field(None, description="Wikipedia URL for the artist")
    imageUrl: str | None = Field(None, description="Artist image URL from Discogs")
    bioTokens: list[DiscogsResolvedToken] | None = Field(
        None,
        description="Pre-parsed structured tokens from the artist's Discogs profile markup. Pass-through of DiscogsArtistDetails.profile_tokens, so clients can share token rendering across the two payloads.\n",
    )


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


class Type3(StrEnum):
    update = "update"


class Type4(StrEnum):
    refetch = "refetch"


class Payload(BaseModel):
    source: str = Field(
        ..., description="Free-text label naming the upstream cause (telemetry only)."
    )


class LiveFsRefetchEvent(BaseModel):
    type: Literal["refetch"]
    payload: Payload
    timestamp: AwareDatetime


class Type5(StrEnum):
    insert = "insert"


class AutoDJState(StrEnum):
    BOOTING = "BOOTING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    ERROR_STATE = "ERROR_STATE"


class AutoDJTransport(StrEnum):
    ethernet = "ethernet"
    wifi = "wifi"


class AutoDJCommandAction(StrEnum):
    set_config = "set_config"
    pause = "pause"
    resume = "resume"
    end_show = "end_show"
    restart = "restart"
    ping = "ping"


class AutoDJErrorLevel(StrEnum):
    warning = "warning"
    error = "error"
    fatal = "fatal"


class AutoDJErrorCode(StrEnum):
    HTTP_TIMEOUT = "HTTP_TIMEOUT"
    JSON_PARSE = "JSON_PARSE"
    WIFI_DISCONNECT = "WIFI_DISCONNECT"
    WS_DISCONNECT = "WS_DISCONNECT"
    TLS_HANDSHAKE = "TLS_HANDSHAKE"
    NTP_FAIL = "NTP_FAIL"
    KVSTORE_WRITE = "KVSTORE_WRITE"
    FLOWSHEET_POST = "FLOWSHEET_POST"
    AZURACAST_POLL = "AZURACAST_POLL"


class AutoDJActivationSourceType(StrEnum):
    virtual_switch = "virtual_switch"
    button = "button"
    relay = "relay"


class AutoDJRelayState(StrEnum):
    auto_dj_active = "auto_dj_active"
    dj_live = "dj_live"


class AutoDJLastTrack(BaseModel):
    artist: str
    title: str
    posted_at: int = Field(..., description="Unix timestamp")


class Type6(StrEnum):
    heartbeat = "heartbeat"


class AutoDJHeartbeat(BaseModel):
    type: Literal["heartbeat"]
    state: AutoDJState
    transport: AutoDJTransport
    uptime_s: int
    wifi_rssi: int | None = None
    free_ram: int
    radio_show_id: int | None = Field(
        None,
        description="Always null in the reporter model; retained for schema compatibility.",
    )
    last_track: AutoDJLastTrack | None = None
    last_error: str | None = None
    firmware_version: str
    config_hash: str
    loop_max_ms: int
    reconnect_count: int
    tracks_detected: int
    tracks_posted: int
    errors_since_boot: int
    button_press_count: int | None = Field(
        None,
        description="Button presses since last heartbeat (HTTP/WiFi fallback; orchestrator toggles if odd). 0 if none.",
    )
    relay_auto_dj_active: bool | None = Field(
        None,
        description="Current debounced relay level. true = relay reports auto-DJ-active (no live DJ); false = live DJ on air.",
    )


class Type7(StrEnum):
    command = "command"


class AutoDJCommand(BaseModel):
    type: Literal["command"]
    id: str = Field(..., description="Unique command ID for ack correlation")
    action: AutoDJCommandAction
    key: str | None = Field(None, description="Config key (only for set_config)")
    value: str | None = Field(None, description="Config value (only for set_config)")


class Type8(StrEnum):
    ack = "ack"


class Status1(StrEnum):
    ok = "ok"
    error = "error"
    unknown_command = "unknown_command"


class AutoDJAck(BaseModel):
    type: Literal["ack"]
    id: str = Field(..., description="The id from the command being acknowledged")
    status: Status1
    error: str | None = Field(None, description="Error message (only when status is error)")
    result: dict[str, Any] | None = Field(
        None,
        description="Optional result data (e.g. { active: boolean } for button_toggle acks)",
    )


class Type9(StrEnum):
    now_playing = "now_playing"


class AutoDJNowPlaying(BaseModel):
    type: Literal["now_playing"]
    sh_id: int = Field(..., description="AzuraCast song history ID")
    artist: str
    title: str
    album: str
    is_live: bool = Field(..., description="Whether a live DJ is streaming")


class Type10(StrEnum):
    error = "error"


class AutoDJErrorReport(BaseModel):
    type: Literal["error"]
    level: AutoDJErrorLevel
    module: str = Field(..., description="Source module (e.g. mgmt_client, relay_monitor)")
    code: AutoDJErrorCode
    message: str
    state: AutoDJState
    uptime_s: int
    free_ram: int
    count: int = Field(..., description="Occurrences since last report")


class Type11(StrEnum):
    button_toggle = "button_toggle"


class AutoDJButtonToggle(BaseModel):
    type: Literal["button_toggle"]
    timestamp: int = Field(..., description="Unix timestamp of the button press (from NTP)")


class AutoDJWebSocketMessage(
    RootModel[
        AutoDJHeartbeat
        | AutoDJCommand
        | AutoDJAck
        | AutoDJNowPlaying
        | AutoDJErrorReport
        | AutoDJButtonToggle
    ]
):
    root: (
        AutoDJHeartbeat
        | AutoDJCommand
        | AutoDJAck
        | AutoDJNowPlaying
        | AutoDJErrorReport
        | AutoDJButtonToggle
    ) = Field(..., discriminator="type")


class Transport(StrEnum):
    ethernet = "ethernet"
    wifi = "wifi"
    none = "none"


class AutoDJDeviceStatus(BaseModel):
    connected: bool
    transport: Transport
    last_heartbeat_at: AwareDatetime
    last_heartbeat: AutoDJHeartbeat | None = None
    pending_commands: int | None = Field(
        None, description="Number of unacknowledged commands in the queue"
    )
    firmware_version: str
    device_id: str | None = Field(None, description="MAC address or other unique identifier")


class AutoDJActivationSource(BaseModel):
    source: AutoDJActivationSourceType
    userId: str | None = Field(
        None, description="Better Auth user ID (only for virtual_switch source)"
    )
    userName: str | None = Field(None, description="Display name (only for virtual_switch source)")
    detail: str | None = Field(
        None,
        description='Additional context (e.g. "Live DJ detected" for relay source)',
    )


class AutoDJCurrentTrack(BaseModel):
    artist: str
    title: str
    album: str
    detectedAt: AwareDatetime


class AutoDJDeviceSummary(BaseModel):
    online: bool
    transport: AutoDJTransport
    lastHeartbeat: AwareDatetime
    relayState: AutoDJRelayState


class AutoDJStatus(BaseModel):
    active: bool
    activatedBy: AutoDJActivationSource | None = None
    activatedAt: AwareDatetime | None = None
    showId: int | None = None
    currentTrack: AutoDJCurrentTrack | None = None
    lastDeactivatedAt: AwareDatetime | None = None
    lastDeactivatedBy: AutoDJActivationSource | None = None
    device: AutoDJDeviceSummary | None = None


class Active(Enum):
    boolean_False = False


class AutoDJDeactivateResponse(BaseModel):
    active: Active
    deactivatedBy: AutoDJActivationSource
    deactivatedAt: AwareDatetime


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
    discogsUnavailable: bool | None = Field(
        None,
        description='Rides the same MD-set "not on Discogs" flag as the Album surfaces (see `Album.discogsUnavailable`), non-nullable to match them. Deliberately camelCase — unlike its snake_case siblings in this block — to match Backend\'s `withDiscogsUnavailableCamelCase` serializer. Contract-first: the V2 flowsheet album embed will carry this field once the BS-emit piece (WXYC/Backend-Service#1908) lands; it is not emitted there yet.',
    )
    discogsUnavailableNote: constr(max_length=500) | None = Field(
        None, description="Optional free-text reason for `discogsUnavailable`."
    )
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None
    artist_bio: str | None = None
    artist_wikipedia_url: str | None = None
    track_position: str | None = Field(
        None,
        description='Track position on the release (e.g., "A1", "B2", "5"). Populated when the flowsheet entry was created via the dj-site picker after release selection (catalog-track-search plan §5.3 / Track 3). String-typed to match Discogs\'s `release_track.position`. Null for free-text entries and legacy rows.\n',
    )
    metadata_status: MetadataStatus | None = None
    entry_type: FlowsheetEntryType | None = None
    add_time: AwareDatetime | None = Field(
        None, description="The instant this row was logged (ISO 8601)."
    )
    radio_hour: AwareDatetime | None = Field(
        None,
        description="Top-of-hour a breakpoint row marks (ISO 8601), sourced from tubafrenzy's RADIO_HOUR. Null on non-breakpoint rows and rows predating the producer rollout. Mirrors the V2 breakpoint entry.",
    )
    dj_name: str | None = Field(
        None,
        description="Resolved public display name of the DJ on the row's show. Nullable per the PII-safe resolution chain (BS#1371): user.djName -> shows.legacy_dj_name -> null; never the real-name PII column.",
    )


class FlowsheetV2TrackEntry(FlowsheetV2Base):
    entry_type: Literal["track"]
    album_id: int | None = None
    rotation_id: int | None = None
    artist_id: int | None = Field(
        None,
        description="The played release's resolved WXYC catalog artist id (`flowsheet.album_id → library.artist_id`), computed server-side on the same join that powers `upcoming_show`. Shares the catalog artist-id keyspace with `Concert.headlining_artist_id` and `SimilarArtist.artist_id`, so on-device features — the On Tour likes match — can intersect a liked playcut's artist against concert headliners and their affinity neighbors in one id space.\n\nNull for free-form entries (no `album_id`) and for library rows whose album has no linked artist. Additive and optional: older app builds that don't decode it are unaffected.\n",
    )
    artist_name: str | None = None
    album_title: str | None = None
    track_title: str | None = None
    track_position: str | None = Field(
        None,
        description='Track position on the release (e.g., "A1", "B2", "5", "1-12"). Set by the dj-site flowsheet picker (catalog-track-search plan §5.3 / Track 3) when the DJ selected a track from the resolved release; null when the track_title was entered free-form or the release had no resolvable identity. String-typed to match Discogs\'s `release_track.position`.\n',
    )
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
    metadata_status: MetadataStatus | None = None
    genres: list[str] | None = Field(
        None, description="Discogs genre tags surfaced on the Playcut Detail card."
    )
    styles: list[str] | None = Field(
        None, description="Discogs style tags (finer-grained than genres)."
    )
    upcoming_show: Concert | None = Field(
        None,
        description="An optional embedded upcoming Triangle-area concert whose headliner is this track's resolved catalog artist, attached server-side at feed-assembly time so the iOS \"On Tour\" Box Office CTA renders inline with no second round-trip.\n\nMatch rule (mirrors `GET /concerts?curated=true`): the track's resolved artist — `flowsheet.album_id → library.artist_id` — is matched against `concerts.headlining_artist_id` on curated, non-tombstoned, upcoming rows (`headlining_artist_id IS NOT NULL`, `removed_at IS NULL`, `starts_on >= today` America/New_York). When an artist has several upcoming dates the **soonest** wins (`ORDER BY starts_on ASC LIMIT 1`), so at most one concert rides each playcut.\n\nAbsent/null when the track has no resolved artist (free-form entries with no `album_id`, or an `album_id` whose library row has no matched artist) or when that artist has no curated upcoming date. The field is additive and optional — older app builds that don't decode it are unaffected. Reuses the `Concert` schema verbatim so iOS decodes one type across the On Tour tab and the playcut CTA; the `BoxOfficeTicketPresenter` reads `id`, `title` / `headlining_artist_raw`, `venue` (name + city), `starts_on`, `doors_at`, `status`, `price_min` / `price_max`, `ticket_url`, and `image_url` off it.\n",
    )
    critic_reviews: list[CriticReviewItem] | None = Field(
        None,
        description="An optional array of attributed external critic-review snippets for this track's resolved album, attached server-side at feed-assembly time so the iOS Reviews card can render inline for enriched playcuts with no second round-trip — iOS skips the `/proxy/metadata/album` fetch for terminal rows (wxyc-ios-64#685/#691).\n\nPopulated only on `track` entries whose linked `album_id` has rows in Backend-Service's `album_critic_reviews` table; attached via one batched query per page (no per-row lookups). Capped at 5 items (`CRITIC_REVIEWS_LIMIT`), ordered `published_at DESC NULLS LAST`. Absent — never `null` or empty — when the track has no matching album, when the attach is skipped, or on servers where Backend's `CRITIC_REVIEWS_ENABLED` env flag (ADR 0012) is off, including older servers that predate this field. Reuses the `CriticReviewItem` schema verbatim — the same shape `AlbumMetadataResponse.criticReviews` already serves — so clients decode one type across both surfaces.\n",
    )


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
    on_air: OnAirInfo | None = Field(
        None,
        description='The DJ currently on air, when known. An object with `dj_name` means a named DJ is live; JSON `null` means the station is confirmed on automation ("Auto DJ"); the field being absent entirely means the server does not report on-air status (older backends / non-default query branches) and clients should treat it as unknown rather than asserting automation. Not in `required` so absence stays distinct from an explicit `null`.\n\nCodegen note: this three-way distinction (object / null / absent) survives openapi-typescript (`on_air?: OnAirInfo | null`), but a *synthesized* decoder in Swift (Codable `decodeIfPresent`), Kotlin (kotlinx default value), or Python (pydantic `| None = None`) collapses JSON `null` and an absent key into the same value. A consumer that needs the third state must decode by key presence (e.g. Swift\'s `container.contains` + `decodeNil`), as the iOS app does — do not rely on a generated model to tell `null` from absent.',
    )


class ShowPlaylist(BaseModel):
    show_name: str | None = None
    specialty_show: str | None = None
    start_time: AwareDatetime | None = None
    end_time: AwareDatetime | None = None
    show_djs: list[OnAirDJ] | None = None
    entries: list[FlowsheetEntryResponse] | None = None


class ShowPeek(BaseModel):
    show: int | None = None
    show_name: str | None = None
    date: AwareDatetime | None = None
    djs: list[Dj] | None = None
    specialty_show: str | None = None
    preview: list[FlowsheetEntryResponse] | None = None


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
    discogsUnavailable: bool | None = Field(
        None,
        description="MD-set marker indicating this release is intentionally not on\nDiscogs (embargoed promo, audience-segment release, etc.). When\ntrue, the LML runtime-lookup chokepoint does not attempt Discogs\nresolution for this release. See WXYC/wiki plans/rotation-discogs-unavailable.md.\n",
    )
    discogsUnavailableNote: constr(max_length=500) | None = Field(
        None, description="Optional free-text reason for `discogsUnavailable`."
    )
    lastDiscogsRecheckAt: AwareDatetime | None = Field(
        None,
        description="Stamped on every recheck attempt by the\n`library-discogs-unavailable-recheck` cron. Read-only from the\nclient side.\n",
    )
    matched_via: list[TrackMatchHint] | None = Field(
        None,
        description="Populated by Backend's catalog `/library/` search when a track-title match (CTA or LML proxy fallback) drove this release into the results, per catalog-track-search plan §5.1. Empty or absent on normal artist/album hits. Backward-compatible — existing consumers ignore the field.\n",
    )
    matched_via_alias: list[ArtistMatchHint] | None = Field(
        None,
        description="Populated by Backend's catalog search when an artist-alias match (from `artist_search_alias`) drove this release into the results, per artist-search-alias plan PR 5. Sibling field to `matched_via` (which is track-title provenance). Empty or absent on normal artist/album hits. Backward-compatible — existing consumers ignore the field.\n",
    )


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


class DiscogsMatchResult(BaseModel):
    album: str | None = Field(None, description="Release title")
    artist: str | None = Field(None, description="Release artist")
    release_id: int = Field(
        ...,
        description='Discogs release ID. `> 0` is a real release identity; `0` is the streaming-only sentinel (paired with `release_url == ""`, BS#1185) and is not a linkable Discogs release.\n',
    )
    master_id: int | None = Field(
        None,
        description="Discogs master ID for the release, when the release belongs to a master. `null` when Discogs has no master for this release (one-offs, self-released) or for the streaming-only sentinel (`release_id == 0`). Lets a caller collapse multiple pressings/formats of one logical album into a single record keyed on the master. Populated from the resolved release's `master_id` field on `/lookup` (single and bulk).\n",
    )
    release_url: str = Field(
        ...,
        description="URL to the release on Discogs. Non-empty for a real release identity; the empty string accompanies the `release_id == 0` streaming-only sentinel.\n",
    )
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
    streaming_status: StreamingResolution | None = Field(
        None,
        description="Per-service streaming resolution status (verified / absent / unresolved) for this result, disambiguating WHY a sibling `*_url` field is null. A service marked `unresolved` timed out, had its enrichment tail shed, or was a cold cache miss — its null url is transient and MAY resolve on a later retry; `absent` means the service was consulted and genuinely has no match — its null url is terminal and must NOT be re-probed (re-asking `absent` is the per-play LML-call amplifier BS#1747 killed). A service whose key is OMITTED from this object was never consulted (e.g. Bandcamp on the `/lookup/bulk` path, or the library.db override skipped for a non-library row) and must not be treated as `absent`. Additive and optional: null/omitted on responses from an LML predating the producer rollout, or on paths that resolve no per-service status. Does not change the meaning of the `*_url` fields — it only annotates them. Emitted identically on `/lookup` and `/lookup/bulk` (the LML#681 parity rule). Mirrors the per-service verdict vocabulary of `/api/v1/streaming-check` (LML#376). See LML#1053 / BS#1819.\n",
    )
    discogs_artist_id: int | None = Field(
        None,
        description="Discogs artist ID for this release. Populated only when the originating `LookupRequest.extended` is true. Lets a caller key an artist-metadata cache without a follow-up release fetch.\n",
    )
    tracklist: list[DiscogsTrackItem] | None = Field(
        None,
        description="Release tracklist with per-track artist credits where available. Populated only when `extended` is true.\n",
    )
    genres: list[str] | None = Field(
        None,
        description='Discogs genre tags (e.g. "Rock", "Electronic"). Populated only when `extended` is true.\n',
    )
    styles: list[str] | None = Field(
        None,
        description='Discogs style tags (finer-grained than genres; e.g. "Indie Rock", "Ambient"). Populated only when `extended` is true.\n',
    )
    label: str | None = Field(
        None,
        description="Primary record-label name from the Discogs release. Distinct from `LibraryCatalogItem.label` (which comes from the WXYC catalog's rotation-release join). Populated only when `extended` is true.\n",
    )
    full_release_date: str | None = Field(
        None,
        description='Release date as an ISO string (e.g. "1997-09-22"). May be year-only ("1997") or year-month ("1997-09") if Discogs lacks the full date. Populated only when `extended` is true.\n',
    )
    artist_image_url: str | None = Field(
        None,
        description="Primary artist image URL from Discogs (the artist's profile photo, not the release artwork). Populated only when `extended` is true.\n",
    )
    profile_tokens: list[DiscogsResolvedToken] | None = Field(
        None,
        description="Pre-parsed structured tokens from the artist's `profile` markup, using a cache-only resolver — references to entities not in the local PG cache fall through as plain-text tokens (no inline Discogs API calls on the read path). Populated only when `extended` is true. Field name matches `DiscogsArtistDetails.profile_tokens` so callers can share rendering code across the two payloads. Pair with `LookupRequest.warm_cache=true` on write-path calls to progressively populate the cache so subsequent reads render richer.\n",
    )
    writer_credits: DiscogsWriterCredits | None = None


class LookupResultItem(BaseModel):
    library_item: LibraryCatalogItem
    artwork: DiscogsMatchResult | None = None
    reconciled_identity: ReconciledIdentity | None = None
    matched_via: list[TrackMatchHint] | None = Field(
        None,
        description="Populated when a track-title match drove this release into the results. Two producers: LML's SONG_AS_TRACK strategy (per catalog-track-search plan §5.1) for the primary match, and the comprehensive multi-location union (LML#1018/#1019/#1022) for every other WXYC shelf location carrying the same track (V/A compilations, soundtracks) — those rows are folded directly into `results` alongside the primary, tagged with `source: discogs_release` since the recall index is Discogs-release-derived. Empty or absent when the release matched via artist/album strategies. Backward-compatible — existing consumers ignore the field.\n",
    )
    matched_via_alias: list[ArtistMatchHint] | None = Field(
        None,
        description="Sibling to `matched_via`. Reserved for the day LML composes artist-alias hits itself; not produced in v1 of the artist-search-alias plan (BS-local cache only). Optional and backward-compatible.\n",
    )


class LookupResponse(BaseModel):
    api_version: ApiVersion | None = Field(
        None,
        description="Present and equal to 2 only when the request set `include_identity: true`. Absent for the v1-compatible shape so existing consumers see byte-identical responses.\n",
    )
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
    cache_stats: CacheStats | None = None
    identity: LookupIdentityBlock | None = None
    timeout: bool | None = Field(
        False,
        description="True when LML's server-side hard cap fired and the search pipeline was abandoned mid-execution (LML#370). `results` may be partial or empty in that case. Callers can use this to distinguish \"no match\" (empty `results`, `timeout: false`) from \"ran out of time\" (`results` may be empty, `timeout: true`). The hard cap is an internal LML safety floor independent of the caller's `X-Caller-Budget-Ms` header; see LML#338 / LML#340 / LML#370 for the cascade-budget design. Backend also forwards two sibling informal headers on the same `/lookup` and `/lookup/bulk` requests, both prose-referenced here rather than formal parameters (matching `X-Caller-Budget-Ms`'s own precedent): `X-Caller-Class` (the resolved BS→LML traffic class, an integer 1-5 per Backend-Service's per-caller policy, BS#1826) and `X-Caller-Reason` (the `caller` label string itself, e.g. `proxy-library-search` or `catalog-popularity-freetext-resolve`). Both are sent only when Backend has a registered caller for the request and are otherwise omitted; sending them is inert until LML reads them (LML#928 for `X-Caller-Class`-driven lane routing, LML#931 for `X-Caller-Reason` caller telemetry) — see BS#1843.\n",
    )
    degraded: bool | None = Field(
        False,
        description="True when LML returned partial or cache-only data because it intentionally shed the enrichment tail — under a caller deadline, admission-control pressure, or an unavailable upstream (LML#930). Distinguishes a degraded/cache-only result from a full success (`degraded: false`) and from a genuine no-match (empty `results`, `degraded: false`, `timeout: false`). Distinct from `timeout`, which signals the internal hard cap fired and the pipeline was abandoned mid-execution; `degraded` is a deliberate shed-the-tail outcome where the returned data is trustworthy but incomplete. Default false, so existing consumers see a byte-identical response.\n",
    )
    degraded_reason: DegradedReason | None = Field(
        None,
        description="Why the response was degraded. Present only when `degraded: true`; omitted otherwise. Non-exhaustive — LML may add reasons in a later minor version. The Swift and Kotlin codegen decode an unknown value into an `unknownDefault` case and TypeScript consumers see a widened string-literal union, but the Python (datamodel-codegen → pydantic) consumers use strict enums and must regenerate against the new `@wxyc/shared` minor before LML emits a newly added reason. `deadline_exceeded` — the caller's budget or LML's spine deadline elapsed and the enrichment tail was skipped; `cache_only` — LML served cached data without refreshing from upstream; `upstream_unavailable` — an upstream (Discogs, streaming providers) was rate limited or down and its contribution was omitted. Set by LML#930's caller-deadline / admission path; read by LML#931 (`degraded-mode-result` telemetry) and wxyc-canary#82.\n",
    )


class BulkResolveLibrariesRequest(BaseModel):
    inputs: list[BulkResolveInput] = Field(
        ...,
        description="Inputs to resolve. Order preserved in `BulkResolveLibrariesResponse.results`.\n",
        max_length=1000,
        min_length=1,
    )
    include_tracks: bool | None = Field(
        None,
        description="Opt in to per-track identity, for every input in the batch (the flag is batch-level, not per-input). Omitting the field means `false`; it carries no schema-level `default` on purpose, because `openapi-typescript` emits a default-bearing property as non-optional even when it is absent from `required`, which would force every TypeScript caller to pass the one field whose whole contract is that you need not. When `false` or omitted — the default, and what an un-upgraded caller sends — `BulkResolveResult.tracks` is absent from every result, so an album-identity drain pays nothing for track composition it would discard. When `true`, LML composes per-track identity for both `kind: single_artist` and `kind: compilation` results and sets `tracks_attempted` on each of them; `kind: unresolved` never carries either field. See `BulkResolveResult.tracks_attempted` for the four states the pair distinguishes, and why the array's length is not one of them.\n\nThe flag is batch-level by design, and the payload it admits is not bounded by the schema — `inputs` still caps at 1,000 either way. A caller that can classify its own rows should partition its drains rather than send a mixed page: Backend holds both signals LML auto-detects on (`library.code_volume_letters LIKE 'Z%'`, `EXISTS compilation_track_artist`), so WXYC/Backend-Service#1991 runs a small-paged V/A drain separately from a full-width non-V/A one. A per-input flag was considered and rejected as mechanism for a problem the one consumer can solve locally.\n",
    )


class LiveFsUpdateEvent(BaseModel):
    type: Literal["update"]
    payload: FlowsheetEntryResponse
    timestamp: AwareDatetime


class LiveFsInsertEvent(BaseModel):
    type: Literal["insert"]
    payload: FlowsheetEntryResponse
    timestamp: AwareDatetime


class LiveFsEvent(RootModel[LiveFsUpdateEvent | LiveFsRefetchEvent | LiveFsInsertEvent]):
    root: LiveFsUpdateEvent | LiveFsRefetchEvent | LiveFsInsertEvent = Field(
        ...,
        description="Discriminated union of events emitted on the `live-fs-topic`. Every event has the same `{ type, payload, timestamp }` envelope — pinned by `CONTRACTS.LIVE_FS_EVENT_ENVELOPE_SHAPE`.\n",
        discriminator="type",
    )


class LibraryQueryResponse(BaseModel):
    results: list[AlbumSearchResult]
    total: int
    page: int
    totalPages: int
