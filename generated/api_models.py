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
    """
    Service-reported health. `healthy` = fully operational; `degraded`
    = serving but with reduced capability; `unhealthy` = should not
    receive traffic.

    """

    healthy = "healthy"
    degraded = "degraded"
    unhealthy = "unhealthy"


class HealthCheckResponse(BaseModel):
    """
    Liveness response shape for `GET /health` (and equivalent) endpoints.
    Cross-language contract owned by `wxyc-fastapi` (Python) and adopted
    by Backend-Service (Node) via generated TS types.

    `additionalProperties: true` lets services attach extra fields without
    breaking consumers; semantic-index, for example, returns `artist_count`
    alongside `status`.

    """

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
    """
    Readiness response shape for `GET /ready` (and equivalent) endpoints.
    Extends `HealthCheckResponse` with a per-dependency status map.
    Each entry in `services` reports the live state of one external
    dependency (database, upstream HTTP service, queue, etc.).

    Status values:
      - `ok`          — dependency reachable and responsive
      - `unavailable` — dependency reachable but reporting an error,
                        or the connection itself failed
      - `timeout`     — dependency exceeded the readiness probe deadline

    """

    services: dict[str, Services] = Field(
        ..., description="Per-dependency readiness map keyed by dependency name."
    )


class RotationBin(StrEnum):
    """
    Rotation bin levels: H=Heavy, M=Medium, L=Light, S=Single
    """

    H = "H"
    M = "M"
    L = "L"
    S = "S"


class FlowsheetEntryBase(BaseModel):
    id: int
    play_order: int
    show_id: int


class FlowsheetRangeEntryBase(BaseModel):
    """
    Identity block for `GET /flowsheet/range`. Identical to `FlowsheetEntryBase` except that `show_id` is nullable — see that property's description. Kept separate rather than relaxing `FlowsheetEntryBase` in place so the other flowsheet paths' documented shapes are untouched by this endpoint.
    """

    id: int
    play_order: int
    show_id: int | None = Field(
        ...,
        description="The show this entry belongs to, or `null` for an **unattributed** entry — a row that exists with no linked show. 20 of 2,619,011 rows are in that state (2005-02-06 → 2026-04-21), and the Phase 0 audit for the tubafrenzy decommissioning decided against backfilling them, so this endpoint returns them rather than dropping them. Consumers that group entries by show must render an unattributed bucket; they must not assume every entry joins to a member of `shows`, and must not crash on the null. The key is always present.",
    )


class FlowsheetRangeShow(BaseModel):
    """
    Show metadata for one show overlapping a `GET /flowsheet/range` window. A deliberately public-safe projection of the `shows` row: it carries the DJ's public handle but no user id and no real-name column. Distinct from `Show`, which exposes `primary_dj_id` and is used on authenticated surfaces.
    """

    id: int = Field(
        ...,
        description="Matches `FlowsheetRangeEntry.show_id` for every entry belonging to this show.",
    )
    show_name: str | None = Field(
        None, description="Specialty-show name, or `null` for a regular show."
    )
    dj_name: str | None = Field(
        None,
        description="Resolved public display name of the show's DJ. Same PII-safe resolution chain as `FlowsheetEntryFields.dj_name` (BS#1371): per-show override -> `user.djName` -> `shows.legacy_dj_name` (tubafrenzy's `DJ_HANDLE`) -> null. Never the real-name column.",
    )
    specialty_id: int | None = None
    start_time: AwareDatetime = Field(..., description="When the DJ signed on (ISO 8601).")
    end_time: AwareDatetime | None = Field(
        None,
        description='When the DJ signed off (ISO 8601), or `null`. Null has **two** causes and they are not distinguishable from this field alone: the show is genuinely on the air, or its `show_end` delivery was dropped and the column stayed null permanently (nothing re-closes it on a schedule). Do not render a null `end_time` as "on air now"; compare `start_time` against the present, or read the `show_end` marker row in `entries`, which is the second independent signal.',
    )


class FlowsheetSongEntry(FlowsheetEntryBase):
    """
    Song entry in the flowsheet
    """

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
    """
    Show block entry (start or end of a DJ's show)
    """

    dj_name: str
    isStart: bool


class FlowsheetMessageEntry(FlowsheetEntryBase):
    """
    Message entry (talkset, etc.)
    """

    message: str


class FlowsheetBreakpointEntry(FlowsheetMessageEntry, DateTimeEntry):
    """
    Breakpoint entry (marks time boundaries)
    """


class FlowsheetCreateSongFromCatalog(BaseModel):
    album_id: int
    track_title: str
    track_position: str | None = Field(
        None,
        description='Track position on the release (e.g., "A1", "B2", "5", "1-12"). Written by the dj-site flowsheet picker (catalog-track-search plan §5.3 / Track 3) when the DJ selects a track from the resolved release. Omitted when the DJ enters a free-text track_title without a tracklist lookup. String-typed to match Discogs\'s `release_track.position` (vinyl side notation, multi-disc prefixes).\n',
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
    track_position: str | None = Field(
        None,
        description='Track position on the release (e.g., "A1", "B2", "5", "1-12"), for an LML-only row with no library linkage (routed onto this album_id-less branch). Carries the Discogs `release_track.position` straight through — BS\'s `buildSnapshotFieldsEntry` already persists `body.track_position ?? null` on the same allowlist that sets `album_id: null`. String-typed to match Discogs\'s `release_track.position` (vinyl side notation, multi-disc prefixes).\n',
    )


class EntryType(StrEnum):
    """
    Explicit entry type. If omitted, the backend infers from message content.
    """

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
    page: conint(ge=0) = 0
    limit: conint(ge=1, le=100) = 50
    sort: Sort = "date"
    order: Order = "desc"


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
    """
    Discriminator for V2 flowsheet entry types
    """

    track = "track"
    show_start = "show_start"
    show_end = "show_end"
    dj_join = "dj_join"
    dj_leave = "dj_leave"
    talkset = "talkset"
    breakpoint = "breakpoint"
    message = "message"


class FlowsheetV2Base(BaseModel):
    """
    Base fields shared by all V2 flowsheet entries
    """

    id: int
    show_id: int | None = Field(...)
    play_order: int
    add_time: AwareDatetime


class EntryType1(StrEnum):
    track = "track"


class EntryType2(StrEnum):
    show_start = "show_start"


class FlowsheetV2ShowStartEntry(FlowsheetV2Base):
    """
    V2 show start event - when a show begins
    """

    entry_type: Literal["show_start"]
    dj_name: str
    timestamp: AwareDatetime


class EntryType3(StrEnum):
    show_end = "show_end"


class FlowsheetV2ShowEndEntry(FlowsheetV2Base):
    """
    V2 show end event - when a show ends
    """

    entry_type: Literal["show_end"]
    dj_name: str
    timestamp: AwareDatetime


class EntryType4(StrEnum):
    dj_join = "dj_join"


class FlowsheetV2DJJoinEntry(FlowsheetV2Base):
    """
    V2 DJ join event - when a DJ joins an active show
    """

    entry_type: Literal["dj_join"]
    dj_name: str


class EntryType5(StrEnum):
    dj_leave = "dj_leave"


class FlowsheetV2DJLeaveEntry(FlowsheetV2Base):
    """
    V2 DJ leave event - when a DJ leaves an active show
    """

    entry_type: Literal["dj_leave"]
    dj_name: str


class EntryType6(StrEnum):
    talkset = "talkset"


class FlowsheetV2TalksetEntry(FlowsheetV2Base):
    """
    V2 talkset entry - DJ talk segment
    """

    entry_type: Literal["talkset"]
    message: str


class EntryType7(StrEnum):
    breakpoint = "breakpoint"


class FlowsheetV2BreakpointEntry(FlowsheetV2Base):
    """
    V2 breakpoint entry - hour marker
    """

    entry_type: Literal["breakpoint"]
    radio_hour: AwareDatetime | None = Field(
        None,
        description="Exact top-of-hour this breakpoint marks (ISO 8601), sourced from tubafrenzy's RADIO_HOUR. Distinct from `add_time`, which is the row's logging instant (~1 min before the hour). Optional and nullable: absent on servers predating the producer rollout, null for breakpoint rows not yet backfilled. Clients use `radio_hour` for the hour-marker label when present and fall back to `add_time` otherwise.",
    )
    message: str | None = None


class EntryType8(StrEnum):
    message = "message"


class FlowsheetV2MessageEntry(FlowsheetV2Base):
    """
    V2 message entry - custom/arbitrary message
    """

    entry_type: Literal["message"]
    message: str


class OnAirInfo(BaseModel):
    """
    Minimal, name-only on-air DJ shape for the `on_air` field. Deliberately separate from `OnAirDJ`, which additionally carries an `id` (a nullable `auth_user.id` string) that the `on_air` banner has no use for.
    """

    dj_name: str = Field(..., description="Display name of the DJ currently on air.")


class OnAirDJ(BaseModel):
    id: str | None = Field(
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


class OpenShow(BaseModel):
    """
    One open show as reported by `GET /flowsheet/open-shows`. Distinct from `Show`: it carries the derived `entry_count`, `is_current` and `likely_abandoned` fields, and omits `end_time`, which is NULL for every member of this collection by construction.

    """

    id: int
    primary_dj_id: str | None = Field(
        ...,
        description="NULL for legacy/tubafrenzy-originated shows and for any show whose DJ account was deleted (`ON DELETE SET NULL`). Such a show can still be closed via `force-end`.\n",
    )
    dj_name: str | None = Field(
        ...,
        description="The public DJ handle, resolved through the standard chain (per-show override, then the linked account's handle, then the legacy tubafrenzy handle). Never a real name. NULL when unresolvable.\n",
    )
    show_name: str | None = Field(...)
    start_time: AwareDatetime
    legacy_show_id: int | None = Field(
        ..., description="The tubafrenzy surrogate key, when this show was mirrored."
    )
    entry_count: int = Field(
        ...,
        description="Flowsheet rows belonging to this show, all entry types included.",
    )
    is_current: bool = Field(
        ...,
        description="True for the single show every on-air read resolves to. Present so a client can never offer to close the live broadcast.\n",
    )
    likely_abandoned: bool = Field(
        ...,
        description="Advisory only — an operator hint, not a verdict. True when the show is not current and has fewer than four entries. A show with many entries can still be abandoned; check `start_time`.\n",
    )


class OpenShowsResponse(BaseModel):
    shows: list[OpenShow] = Field(
        ..., description="Ordered oldest-first by `start_time`, tie-broken on `id`."
    )
    total_in_window: int = Field(
        ...,
        description="How many open shows the window holds in total, before `limit` truncates. `shows.length < total_in_window` is the only way a caller can tell it is looking at a partial answer.\n",
    )
    older_open_show_count: int = Field(
        ...,
        description="Open shows that started BEFORE the window, reported as a count and never listed. Almost all of these are legacy imports whose `show_end` never arrived; see the endpoint description. Widen `window_hours` to reach them.\n\n**Expected to go to zero and stay there.** This field and the 30-year `window_hours` ceiling both exist for one finite cohort — the ~2,813 shows WXYC/Backend-Service#1543 repairs from the final tubafrenzy dump. Once that lands, both are vestigial and a later contract version should drop them rather than preserve them out of habit.\n",
    )


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
    genre_name: str = Field(
        ...,
        description="Free-form: `genres.genre_name` is a `varchar(64)` in a lookup table `POST /library/genres` appends to. `GET /library/genres` is the authoritative enumeration (#367).\n",
    )


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
    """
    Partial-update payload for `PATCH /library/{id}` (BS#1154). Mirrors
    Backend-Service's wire-level `UpdateAlbumRequest` type
    (apps/backend/controllers/library.controller.ts) exactly: only fields
    present in the body are validated and written. The 8 legacy fields
    are snake_case (matching AddAlbumRequest / the DB columns); the two
    BS#1281 discogs fields are camelCase, per the whitelist BS's
    `UPDATABLE_ALBUM_FIELDS` actually reads from `req.body`.
    `artist_name` and `code_number` are deliberately absent — they are
    server-derived (re-attribution side effects of an `artist_id`
    change), never read from the client body. `lastDiscogsRecheckAt` is
    also absent — it is server-write-only (the recheck cron writes it
    directly).

    """

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
    """
    One row of the WXYC library catalog as served by GET /library/catalog for offline cloning (BS#1468, Epic F #1466). A query-independent snapshot of the core catalog row. Deliberately NOT AlbumSearchResult: it drops the search-only decoration (matched_via, matched_via_alias, album_dist, artist_dist) and the fields the export omits (add_date, label_id, rotation_id, date_lost, date_found), and instead ships rotation RAW (rotation_bin + rotation_kill_date) so the client evaluates rotation expiry against its own clock. See the rotation_bin description for the load-bearing semantic difference from AlbumSearchResult.
    BS#1965 adds the fields the Backend-sourced library.db producer (discogs-etl#351) needs to build a library.db over HTTP instead of a direct Postgres read: legacy_release_id (the producer emits it AS the library.db id, since BS serials collide with the tubafrenzy id space), album_artist, alternate_artist_name, and cross_reference_names.
    All four are producer-facing and OPTIONAL on the wire — none is in `required`, so the pre-BS#1965 15-field body still decodes cleanly against a client generated from this spec. That leniency is deliberate and load-bearing: this row is also the iOS Spotlight clone's shape, and wxyc-ios-64 regenerates from this SSOT on its own cadence. A required key the server does not yet emit would not degrade one field — it would fail EVERY NDJSON line and take the whole on-device clone with it. Same reasoning as `popularity` (BS#1486 Phase-2 Track 3). Note that oasdiff does NOT flag adding a required response property, so a green `check:breaking` is not evidence of safety here.

    """

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
        description="RAW current-rotation bin — the most-recently-ADDED rotation record for this album — NOT the CURRENT_DATE-filtered value AlbumSearchResult returns. The values are H/M/L/S (see RotationBin), typed here as a free string rather than that enum as a forward-compatibility hedge: a bin added server-side ahead of its clients then cannot break a strict-enum decoder mid-rollout. The hedge matters more on THIS endpoint than on the others that $ref RotationBin, because the body is NDJSON cloned in bulk — one unknown value would fail every line and take a client's whole on-device catalog with it, rather than one row. Evaluate live rotation client-side together with rotation_kill_date:\n  in rotation  ==  rotation_bin != null\n                  AND (rotation_kill_date == null\n                       OR rotation_kill_date > today-in-client-tz)\nThe daily kill-date expiry is a clock event no DB trigger can observe, which is why this endpoint ships raw and defers expiry to the client.\n",
    )
    rotation_kill_date: date_aliased | None = Field(
        None,
        description="Date (YYYY-MM-DD; server ::text cast) the current rotation record expires, or null if it has none. Used with rotation_bin to evaluate live rotation client-side. Absent from AlbumSearchResult — a client that reuses AlbumSearchResult for this endpoint silently loses it.\n",
    )


class CatalogCompilationTrackRow(BaseModel):
    """
    One compilation_track_artist (CTA) row as served by GET /library/catalog/compilation-tracks for the Backend-sourced library.db producer (BS#1965 / discogs-etl#351). The sibling of CatalogExportRow: where that endpoint feeds library.db's `library` table, this feeds its `compilation_track_artist` table (3 columns: library_release_id, artist_name, track_title). Keyed on legacy_release_id — the library row's surrogate key that the producer also emits AS library.db's library.id — so the producer maps legacy_release_id -> compilation_track_artist.library_release_id. Deliberately drops the CTA row id and track_position: library.db's 3-column CTA table carries neither, so shipping them would break parity. Distinct from CompilationTrack (the BS#1964 dj-site read shape, which keeps id + track_position and is keyed on the BS serial library_id).

    """

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
    """
    One row of `GET /library/genres` — the authoritative enumeration of the genres this station files under. Do not hardcode the set: it is a lookup table `POST /library/genres` appends to, and a closed enum here previously declared ten values while production held fifteen (#367). As of 2026-08 those fifteen are Africa, Asia, Blues, Classical, Comedy, Hiphop, Jazz, Latin, OCS, Reggae, Rock, Soundtracks, Spoken, Xmas, Electronic — listed to orient a reader, not as a contract.

    """

    id: int
    genre_name: str
    code_letters: str


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
    """
    A per-track artist entry to write to a compilation (BS#1964). The
    durable, table-agnostic shape: only the librarian-meaningful free-text
    fields, so it survives the future compilation_track_artist ->
    library_track generalization (BS#801), which adds the canonical artist
    link server-side rather than as a wire field.

    """

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
    """
    A stored compilation_track_artist row (BS#1964).
    """

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
    """
    Result of an additive CTA write (BS#1964). `inserted + skipped` equals
    the number of input rows — a row that duplicates one already inserted
    earlier in the same request counts toward `skipped` (matched against the
    just-inserted row on the CTA uniqueness key), so the identity always
    holds even when the request carries intra-batch duplicates. `tracks` is
    the release's full stored set after the write.

    """

    library_id: int
    inserted: int = Field(..., description="Rows newly created.")
    skipped: int = Field(
        ...,
        description="Rows already present (matched on a CTA uniqueness key); left untouched.",
    )
    tracks: list[CompilationTrack]


class CompilationTrackSuggestions(BaseModel):
    """
    Proposed (unwritten) compilation tracklist derived from the linked
    Discogs release (BS#1964). `discogs_release_id: null` + empty `tracks`
    signals no resolvable upstream release -> manual entry.

    """

    library_id: int
    discogs_release_id: int | None = Field(
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


class Rotation(BaseModel):
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
    legacy_release_id: int | None = Field(
        None,
        description="The library row's surrogate key (BS#1963). Nullable here (unlike AlbumSearchResult/BinLibraryDetails/AlbumInfoResponse): a library-unlinked rotation row has no library row at all, hence no legacy id.\n",
    )


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
    legacy_release_id: int | None = Field(
        None,
        description="The library row's surrogate key (BS#1963), NOT NULL in the database since migration 0137. Optional here (never required) because the column is emitted per-projection, not globally: NOT NULL is a claim about the column, while `required` is a promise that the key appears on the wire, and WXYC/Backend-Service#2167 is open precisely because the LML search-proxy rows do not emit it explicitly yet. Promoting this to required once every projection returning the schema provably emits it is worth doing, and needs a per-projection audit rather than a text edit. Matches the CatalogExportRow and BulkResolveInput precedent.\n",
    )


class ScheduleShift(BaseModel):
    id: int
    dj_id: int
    dj_name: str
    day: conint(ge=0, le=6) = Field(..., description="Day of the week 0 = Monday, 6 = Sunday")
    start_time: str = Field(..., description="Time in HH:MM format")
    end_time: str = Field(..., description="Time in HH:MM format")
    show_name: str | None = None
    specialty_id: int | None = None


class AddScheduleShiftRequest(BaseModel):
    dj_id: int
    day: conint(ge=0, le=6) = Field(..., description="Day of the week 0 = Monday, 6 = Sunday")
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
    """
    A live-music venue whose calendar WXYC ingests (Backend-Service
    `venues` table). Embedded whole in `Concert` — the venue set is tiny
    (~21 rows), so clients get name/city in one call with no follow-up
    join.

    """

    id: int
    slug: str = Field(..., description='Stable scraper seed key (e.g. "cats-cradle").')
    name: str
    city: str
    state: str
    address: str | None = Field(...)


class ConcertStatus(StrEnum):
    """
    Lifecycle state of a concert as observed at the source. Listener surfaces read this to grey out / strike through cancelled or sold-out shows.
    """

    on_sale = "on_sale"
    sold_out = "sold_out"
    cancelled = "cancelled"
    rescheduled = "rescheduled"


class SimilarArtist(BaseModel):
    """
    One affinity neighbor of a resolved concert headliner, drawn from the semantic-index graph. `artist_id` shares the WXYC catalog artist-id keyspace with `Concert.headlining_artist_id` (and `FlowsheetV2TrackEntry.artist_id`), so on-device For You matching can intersect it against liked-artist ids in one id space.
    """

    artist_id: int = Field(
        ...,
        description="WXYC catalog artist id, same keyspace as `Concert.headlining_artist_id`.",
    )
    weight: float = Field(
        ...,
        description="semantic-index affinity score, used for client-side ranking and the similar-tier noise cap. Higher is closer.",
    )


class Concert(BaseModel):
    """
    One upcoming (or recent) concert, as served by `GET /concerts`.

    Date semantics: `starts_on` is the venue-local (America/New_York)
    calendar date and is always present — clients should window and sort
    on it. `starts_at` is the exact start instant and is null for
    date-only events (many sources publish no start time; WXYC never
    fabricates one).

    Internal ingestion columns (`source`, `source_id`, `raw_data`,
    `scraped_at`, `first_scraped_at`) are deliberately not exposed.

    """

    id: int
    venue: Venue
    starts_on: date_aliased = Field(
        ..., description="Venue-local (America/New_York) calendar date."
    )
    starts_at: AwareDatetime | None = Field(
        ..., description="Exact start instant; null for date-only events."
    )
    doors_at: AwareDatetime | None = Field(
        ..., description="Doors-open instant, when the source publishes one."
    )
    headlining_artist_raw: str = Field(
        ..., description="Headliner billing string exactly as the source displays it."
    )
    headlining_artist_id: int | None = Field(
        ...,
        description="WXYC catalog artist id when the resolver matched the headliner; null otherwise. A non-null id is what `curated=true` filters on.",
    )
    title: str | None = Field(
        ...,
        description="Event name as the source displays it, when distinct from the artist billing.",
    )
    supporting_artists_raw: list[str] = Field(
        ..., description="Supporting-act billing strings, in source order."
    )
    ticket_url: str | None = Field(...)
    image_url: str | None = Field(...)
    event_url: str | None = Field(
        ...,
        description="The venue's own event-detail page, distinct from `ticket_url` (often a third-party seller like Etix/Ticketmaster). Null when no venue page is known; clients fall back to `ticket_url`.",
    )
    price_min: float | None = Field(..., description="Dollars. Free events carry price_min = 0.")
    price_max: float | None = Field(..., description="Dollars.")
    age_restriction: str | None = Field(
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
    """
    Paginated list of concerts, ordered by `starts_on` ascending.
    """

    concerts: list[Concert]
    pagination: PaginationInfo


class Delta(IntEnum):
    """
    `1` when the listener liked the song, `-1` when they unliked it. The server clamps the resulting tally at zero: deltas are fire-and-forget and lossy by design, so a `-1` can arrive without its matching `1`.
    """

    integer__1 = -1
    integer_1 = 1


class SongLikeDelta(BaseModel):
    """
    One increment or decrement to a song's public like tally. Carries the song's identity and nothing whatsoever about who liked it — see `POST /likes/tally` for why no listener key exists anywhere in this contract.
    """

    song_key: str = Field(
        ...,
        description="The client's folded identity for the song: `SongKey.fold` applied to artist and title, joined with `|` (case-, diacritic-, and width-insensitive, whitespace collapsed). The server stores this verbatim as the tally's key and MUST NOT recompute it — the client's fold is authoritative for song identity. Album is deliberately excluded from the key, so a single, an LP cut, and a free-text replay all tally together.",
    )
    song_title: str = Field(
        ..., description="Raw, unfolded song title, exactly as displayed on-device."
    )
    artist_name: str = Field(
        ...,
        description="Raw, unfolded artist name, exactly as displayed on-device — NOT the folded value baked into `song_key`. The server folds this itself with its own `fold_artist_name` (NFD, strip combining diacritics, lowercase — no width folding, no whitespace collapsing; the twin of the SQL `wxyc_schema.fold_artist_name`) to attempt a catalog match when `artist_id` is absent. The client's fold and the server's fold are deliberately different functions and are never required to agree: width-variant or multi-space artist names may match under one and miss under the other. This field exists for that best-effort match, not for identity — `song_key` alone owns that.",
    )
    release_title: str | None = Field(
        None,
        description="Album/release title, when the like has one. Optional: standalone tracks and some free-text plays carry no matched release.",
    )
    artist_id: int | None = Field(
        None,
        description="WXYC catalog artist id, when already resolved on-device (e.g. healed from a V2 flowsheet play). Optional and absent for name-only likes sourced from the V1 API or free-text plays; the server then falls back to matching `artist_name` via its own fold, leaving the artist unresolved — never failing the write — on a miss.",
    )
    delta: Delta = Field(
        ...,
        description="`1` when the listener liked the song, `-1` when they unliked it. The server clamps the resulting tally at zero: deltas are fire-and-forget and lossy by design, so a `-1` can arrive without its matching `1`.",
    )


class SongLikeTallyRequest(BaseModel):
    """
    Request body for `POST /likes/tally`. A batch of independent counter deltas — NOT a snapshot of anyone's library, and not an event log that could be reassembled into one.
    """

    deltas: list[SongLikeDelta] = Field(
        ...,
        description="One or more deltas, at most 1000. Order carries no meaning. A client normally sends a single delta per toggle; the array exists so a client adopting the feature can submit the likes already on the device in one request. A device holding more than 1000 likes chunks them across requests — there is no cross-request continuity to preserve, since each delta is independent and the endpoint holds no per-caller state.",
        max_length=1000,
        min_length=1,
    )


class SongLikeTallyResponse(BaseModel):
    """
    Response body for `POST /likes/tally`.
    """

    applied: int = Field(..., description="Number of deltas applied to the tallies.")
    resolved: int = Field(
        ...,
        description="Of `applied`, how many carried or matched a WXYC catalog artist id. Diagnostic only: it describes catalog coverage, not the caller.",
    )


class AlbumReview(BaseModel):
    """
    One archived DJ album review, sourced from the WXYC album-review
    Google Form (collected since March 2021) and synced nightly by
    Backend-Service's album-reviews ETL.

    Distinct from ADR 0006's in-app Review model (one-per-album,
    author-owned, `/reviews`): submissions here are append-only, may
    number several per album, and identify albums by free text with a
    best-effort `album_id` link. Reviewer identity is deliberately not
    exposed — the form promised reviewers their names would not be
    shared.

    """

    id: int
    album_id: int | None = Field(
        ...,
        description="WXYC library album id when the free-text artist/album resolved to exactly one catalog row; null otherwise.",
    )
    artist_name: str | None = Field(
        ..., description="Artist name exactly as the reviewer entered it."
    )
    album_title: str | None = Field(
        ..., description="Album title exactly as the reviewer entered it."
    )
    record_label: str | None = Field(...)
    artist_blurb: str | None = Field(
        ..., description="Short reviewer-written background on the artist."
    )
    review: str | None = Field(..., description="The review body.")
    recommended_tracks: str | None = Field(
        ...,
        description='Raw recommended-tracks text, including the form\'s `!`-rating notation (1–5 exclamation marks) and "Play All" shorthand.',
    )
    buzzwords: str | None = Field(..., description="Comma-separated reviewer-chosen descriptors.")
    fcc_violations: str | None = Field(
        ...,
        description='Verbatim reviewer answer listing FCC-violating track numbers. Blank (unanswered) is distinct from "None"/"N/A" (affirmatively clean); values are not normalized.',
    )
    review_purpose: str | None = Field(
        ...,
        description='Raw multi-select of "Rotation", "New DJ Assignment", "Album in the Library"; comma-joined when multiple were selected.',
    )
    rotated: bool | None = Field(
        ...,
        description='Curator-maintained "was this album rotated" flag; null when the sheet cell is blank or unparseable.',
    )
    released_within_six_months: bool | None = Field(
        ...,
        description="Whether the album was released within 6 months of the review; null when unanswered (question added mid-2024).",
    )
    social_consent: bool | None = Field(
        ...,
        description="Whether the reviewer consented to the review being shared on station social media (always anonymously); null when unanswered.",
    )
    submitted_at: AwareDatetime | None = Field(
        ...,
        description="Form submission instant. Null only for the rare row whose sheet timestamp is missing.",
    )


class AlbumReviewsResponse(BaseModel):
    """
    Paginated list of album-review submissions, ordered by `submitted_at` descending (nulls last).
    """

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
    """
    RFC 8628 device-flow grant type (fixed value).
    """

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
    """
    Current status of a device authorization (GET /auth/device).
    """

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
    """
    Error codes for POST /auth/device/code.
    """

    invalid_request = "invalid_request"
    invalid_client = "invalid_client"


class DeviceAuthCodeError(BaseModel):
    error: DeviceAuthCodeErrorCode
    error_description: str


class DeviceAuthTokenErrorCode(StrEnum):
    """
    Error codes for POST /auth/device/token — the RFC 8628 polling vocabulary the browser switches on. `server_error` is emitted at runtime (HTTP 500) though it is absent from the plugin's declared zod enum.

    """

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
    """
    Error codes for GET /auth/device. `expired_token` is emitted at runtime (HTTP 400) though the plugin's declared zod enum lists only invalid_request.

    """

    invalid_request = "invalid_request"
    expired_token = "expired_token"


class DeviceAuthVerifyError(BaseModel):
    error: DeviceAuthVerifyErrorCode
    error_description: str


class DeviceAuthActionErrorCode(StrEnum):
    """
    Error codes for POST /auth/device/approve and POST /auth/device/deny (shared — both routes emit the same set). `device_code_already_processed` is declared in the plugin's approve zod enum but is NEVER emitted on the wire — the already-processed branch throws `invalid_request` — so it is intentionally omitted here (mirror runtime).

    """

    invalid_request = "invalid_request"
    expired_token = "expired_token"
    unauthorized = "unauthorized"
    access_denied = "access_denied"


class DeviceAuthActionError(BaseModel):
    error: DeviceAuthActionErrorCode
    error_description: str


class AuthUser(BaseModel):
    """
    The better-auth user row as every sign-in / token-exchange response in this section embeds it — parseUserOutput's serialization of the `user` table plus the admin() plugin's fields plus WXYC's `user.additionalFields` (Backend-Service shared/authentication/src/auth.definition.ts). Shared across every sign-in response below: the row shape does not depend on which route created or found it. Verified field-for-field against a live POST /auth/sign-in/anonymous response.

    """

    id: str
    email: str
    emailVerified: bool
    name: str
    image: str | None = None
    role: str | None = Field(
        None,
        description='The admin() plugin\'s system role flag on `user.role` (default "user") — NOT a WXYC station role. Station role (member, dj, musicDirector, stationManager, admin) lives on `organization_member.role` and is resolved separately into the JWT\'s `role` claim (buildJwtPayload / selectMemberRole in auth.definition.ts); it is not this field. See that file\'s own comment: "auth_user.role ... carries no station role."\n',
    )
    banned: bool | None = None
    banReason: str | None = None
    banExpires: AwareDatetime | None = None
    username: str | None = None
    displayUsername: str | None = None
    isAnonymous: bool | None = None
    hasCompletedOnboarding: bool | None = Field(
        None, description="WXYC additionalField; defaults false at creation."
    )
    realName: str | None = None
    djName: str | None = None
    appSkin: str | None = Field(
        None, description='WXYC additionalField; defaults "modern-light" at creation.'
    )
    capabilities: list[str] | None = Field(
        None, description="WXYC additionalField; defaults [] at creation."
    )
    createdAt: AwareDatetime | None = None
    updatedAt: AwareDatetime | None = None


class EmailSignInRequest(BaseModel):
    """
    Body for POST /auth/sign-in/email.
    """

    email: str
    password: str
    callbackURL: str | None = Field(
        None,
        description="Redirect target after sign-in / email verification. WXYC's native clients never send this — a callbackURL flips `redirect` true in the response and sets a Location header, both meant for a browser.\n",
    )
    rememberMe: bool = Field(
        True,
        description="When false, the session is not remembered (shorter-lived cookie).",
    )


class UsernameSignInRequest(BaseModel):
    """
    Body for POST /auth/sign-in/username.
    """

    username: str
    password: str
    callbackURL: str | None = None
    rememberMe: bool | None = None


class AuthSignInResult(BaseModel):
    """
    Response body for POST /auth/sign-in/email and POST /auth/sign-in/username — identical shape, since both routes end in the same session-create + setSessionCookie call (better-auth dist/api/routes/sign-in.mjs signInEmail; dist/plugins/username signInUsername). `token` also arrives on the `set-auth-token` response header (see the two paths below); the body is the documented fallback when a caller can't read response headers.

    """

    redirect: bool = Field(
        ...,
        description="True when callbackURL was supplied and the client should follow Location.",
    )
    token: str = Field(..., description="The session token.")
    url: str | None = Field(
        None,
        description="The callbackURL echoed back. NOT nullable — when no callbackURL was supplied, better-auth's handler passes `url: ctx.body.callbackURL` (undefined) straight into `ctx.json(...)` (dist/api/routes/sign-in.mjs signInEmail), and JSON serialization drops an undefined-valued key rather than emitting `null`. The key is therefore OMITTED entirely in that case, not present-and-null — see this schema's `required` list, which does not include `url`.\n",
    )
    user: AuthUser


class OTPSignInRequest(BaseModel):
    """
    Body for POST /auth/sign-in/email-otp. `name`/`image` are dead on this deployment: Backend-Service sets `disableSignUp: true` on the emailOTP plugin (shared/authentication/src/auth.definition.ts), and that flag gates THIS route (dist/plugins/email-otp/routes.mjs signInEmailOTP: `if (!user) { if (opts.disableSignUp) throw APIError.from("BAD_REQUEST", INVALID_OTP); ... }`), not just send-verification-otp as an earlier version of this comment claimed. A correct code for an email with no existing account 400s INVALID_OTP rather than auto-registering, so `name`/`image` are never reached and registration via this route is unreachable on this deployment. See LookupEmailResponse's description, which already documented the true anti-enumeration behavior this field list previously contradicted.

    """

    email: str
    otp: str
    name: str | None = None
    image: str | None = None


class AuthTokenAndUserResult(BaseModel):
    """
    Response body shared by POST /auth/sign-in/anonymous and POST /auth/sign-in/email-otp — both answer `{token, user}` with no redirect/url (better-auth dist/plugins/anonymous/index.mjs signInAnonymous; dist/plugins/email-otp/routes.mjs signInEmailOTP). One type for two endpoints, per the DeviceAuthActionResponse precedent above.

    """

    token: str
    user: AuthUser


class AuthTokenResponse(BaseModel):
    """
    Response body for GET /auth/token — the short-lived JWT.
    """

    token: str = Field(
        ..., description="A signed JWT, verifiable against the JWKS at GET /auth/jwks."
    )


class AuthSignOutResult(BaseModel):
    """
    Response body for POST /auth/sign-out. Always `{success: true}` on 200 — a failure to delete the session server-side is logged, not surfaced (better-auth dist/api/routes/sign-out.mjs signOut).

    """

    success: bool


class LookupEmailRequest(BaseModel):
    """
    Body for POST /auth/wxyc/lookup-email — a WXYC-custom route (see the path's own comment), not a better-auth one.

    """

    identifier: str


class LookupEmailResponse(BaseModel):
    """
    `email: null` means no account matches `identifier` — the one precise failure this route can report (apps/auth/lookup-email.ts). Everything downstream of a resolved email is deliberately vague (anti-enumeration): send-verification-otp answers success for an unknown address, and sign-in/email-otp answers the same INVALID_OTP for a wrong code as for no account.

    """

    email: str | None = Field(...)


class OTPType(StrEnum):
    """
    The `type` values better-auth's email-otp plugin schema declares. WXYC's OTP sign-in flow always sends "sign-in". "change-email" is declared by the plugin's own schema but always 400s on POST /auth/email-otp/send-verification-otp — a dedicated /email-otp/request-email-change route exists for it. Named rather than left as an inline enum on SendLoginCodeRequest.type: an unnamed inline enum is emitted by the Python generator (datamodel-code-generator) as a bare `Type` class, silently renumbering (`Type1`, `Type2`, ...) every other unnamed inline enum in the document depending on declaration order — see the issue #379 review finding that named this one.

    """

    sign_in = "sign-in"
    email_verification = "email-verification"
    forget_password = "forget-password"
    change_email = "change-email"


class SendLoginCodeRequest(BaseModel):
    """
    Body for POST /auth/email-otp/send-verification-otp.
    """

    email: str
    type: OTPType


class AuthSendCodeResult(BaseModel):
    """
    Response body for POST /auth/email-otp/send-verification-otp. Always `{success: true}` on 2xx — disableSignUp: true makes the server answer identically for an unknown address (anti-enumeration), so a 2xx here does not confirm the account exists.

    """

    success: bool


class AuthErrorResponse(BaseModel):
    """
    better-auth's uniform error body (@better-auth/core's `APIError.from`/`defineErrorCodes`: `{message, code}`). Purpose-built rather than a $ref to the shared ApiErrorResponse — see this section's header comment. Answers every non-2xx response from this section's better-auth-native operations except the 429s — see AuthRateLimitedResponse and AuthPlainErrorResponse, below, for the two DIFFERENT shapes a 429 can actually carry.

    """

    message: str
    code: str | None = Field(
        None,
        description="A defineErrorCodes key, e.g. INVALID_EMAIL_OR_PASSWORD, INVALID_USERNAME_OR_PASSWORD, INVALID_EMAIL, EMAIL_NOT_VERIFIED, UNAUTHORIZED, INVALID_OTP, OTP_EXPIRED, TOO_MANY_ATTEMPTS, ANONYMOUS_USERS_CANNOT_SIGN_IN_AGAIN_ANONYMOUSLY, COULD_NOT_CREATE_SESSION, FAILED_TO_CREATE_USER, MISSING_OR_NULL_ORIGIN, VALIDATION_ERROR (better-call's generic body/query-schema failure, e.g. a missing required field — better-call/dist/endpoint.mjs: `throw new APIError(400, {message: validationError.message, code: \"VALIDATION_ERROR\"})` — thrown before any handler code runs, so it can occur on any operation in this section that takes a body). Left open rather than enumerated — unlike DeviceAuth's closed, small RFC 8628 vocabulary, better-auth's general error surface is broad and not practically exhaustible per operation.\n",
    )


class AuthRateLimitedResponse(BaseModel):
    """
    `{message: string}` — better-auth's OWN internal per-path rate limiter (dist/api/rate-limiter/index.mjs `rateLimitResponse`), which is what a client actually meets FIRST on FIVE of the six 429-declaring operations in this section — every one except POST /auth/wxyc/lookup-email, a plain Express handler that never reaches better-auth's request pipeline at all (see that operation's own 429 description). The window is NOT uniform across those five: `resolveRateLimitConfig` (dist/api/rate-limiter/index.mjs) applies `getDefaultSpecialRules()`'s generic `/sign-in*` rule (3 requests / 10 seconds) first, then walks each installed plugin's OWN `rateLimit` array and OVERWRITES the window/max on an exact `pathMatcher` match. The emailOTP plugin (dist/plugins/email-otp/index.mjs) declares exactly such an override for `path === "/sign-in/email-otp"` — 3 requests / 60 seconds, the plugin's own default (Backend-Service passes no `rateLimit` override in its `emailOTP(...)` config) — so POST /auth/sign-in/email-otp shares its window with POST /auth/email-otp/send-verification-otp, NOT with the other three `/sign-in*` paths in this section. Only `/sign-in/email`, `/sign-in/username`, and `/sign-in/anonymous` actually get the generic 3-per-10s rule; neither the username plugin nor the anonymous plugin declares a `rateLimit` array of its own to override it. Keyed per exact path per IP (createRateLimitKey(ip, path)) — NOT shared across the paths in this section, unlike the express-layer limiter below. Two things a naive client gets wrong: the body has no `code` field (just `message`) and is NOT reliably `Content-Type: application/json` — it's built via `new Response(JSON.stringify(...), {status: 429, ...})` with no explicit Content-Type header, so it falls back to the Fetch spec's `text/plain;charset=UTF-8` default for a string body; the JSON is valid, but a client that gates JSON-parsing on Content-Type (as this repo's own e2e harness does) will not auto-decode it. The window-reset hint arrives on THIS layer's own `X-Retry-After` response header (seconds) — a non-standard header name, chosen by better-auth itself, and distinct from the standard `Retry-After` the OTHER layer sets (see AuthPlainErrorResponse) — not in the body. See AuthPlainErrorResponse for the second, express-layer limiter a client can reach only after clearing this one.
    Also referenced from the device-authorization section above, where the ordering INVERTS. `getDefaultSpecialRules()` matches only `/sign-in*`, `/sign-up*`, `/change-password*`, `/change-email*` and the password-reset family, and the device-authorization plugin declares no `rateLimit` array of its own, so `/device/*` falls through to better-auth's GENERAL default — 100 requests / 10 s (`create-context.mjs`: `window: options.rateLimit?.window || 10`, `max: ... || 100`), not the 3-per-10-60s a path in this section gets. That is looser than the express layer's shared 10-per-15-min, so on the three device paths the express limiter is mounted in front of (/device/code, /device/approve, /device/deny) this shape is UNREACHABLE and those operations declare only AuthPlainErrorResponse. It is reachable on exactly the two device paths the express layer deliberately skips — POST /device/token and GET /device — which is why those two declare only this shape.

    """

    message: str


class AuthPlainErrorResponse(BaseModel):
    """
    `{error: string}` — the shape Backend-Service's own Express layer answers with, distinct from both of better-auth's own shapes (`{message, code}` on AuthErrorResponse; `{message}` with no code on AuthRateLimitedResponse). Two sources share it: the WXYC-custom /auth/wxyc/lookup-email route (apps/auth/app.ts lookupEmailHandler, `res.json({error: ...})`) and the express-rate-limit mutation limiter (apps/auth/app.ts authMutationRateLimit — 10 requests / 15 min, keyed on X-Real-IP, SHARED across all nine mount prefixes it's attached to via one reused middleware instance — `message: {error: ...}`). Those nine prefixes are NOT all in this section: six are (/auth/sign-in, /auth/sign-up, /auth/email-otp/send-verification-otp, /auth/forget-password, /auth/wxyc/lookup-email, /auth/wxyc/complete-onboarding) and three are device-authorization paths from the section above (/auth/device/code, /auth/device/approve, /auth/device/deny). The budget is one bucket across all nine, so device traffic and sign-in traffic from the same X-Real-IP consume each other's allowance — which the control room, sharing one egress address, can reach in ordinary use. THREE different roles depending on the operation: on FIVE of the six 429-declaring operations below, this is the SECOND layer — better-auth's own tighter, per-path AuthRateLimitedResponse limiter (3 requests per 10-60s, depending on path) is what a client meets first in practice, since its window is far easier to exhaust than this shared 10-per-15-min budget, and a client reaches this shape directly only by avoiding the internal limiter (e.g. spacing requests out) while still accumulating past 10 requests across the shared budget. On the sixth, POST /auth/wxyc/lookup-email, this is the ONLY layer — that operation never reaches better-auth's internal limiter at all (see its own 429 description), so its 429 is always this shape, never a "second" layer reached after a first. On the three device- authorization operations this is likewise the ONLY layer, for the opposite reason to lookup-email's: those DO reach better-auth's internal limiter, but `/device/*` matches none of its special rules and falls through to the general 100-per-10s default, which this 10-per-15-min bucket always exhausts first — see AuthRateLimitedResponse's device paragraph. `express-rate-limit@8.6.2` (apps/auth's pinned version) also sets the standard `Retry-After` header (plus `RateLimit` / `RateLimit-Policy`, from its `standardHeaders: 'draft-7'` config) on every 429 this schema answers — verified against node_modules/express-rate-limit/dist/index.cjs.

    """

    error: str


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
    """
    Enrichment lifecycle for a track row in the V2 flowsheet response. Source-of-truth is the `metadata_status_enum` Postgres type in Backend-Service (see WXYC/Backend-Service#891). Set to `pending` on insert; the enrichment worker (WXYC/Backend-Service#892) flips it to `enriching` while a Discogs lookup is in flight, then to one of the three terminal states. iOS branches on this to decide whether to render inline metadata or fall back to the proxy-fetch path (WXYC/wxyc-ios-64#270). Only emitted on track entries.
    Two surfaces reference this schema and must move together: the V2 flowsheet track entry's `metadata_status`, and `AlbumMetadataResponse.metadataStatus` — the latter a local-first echo of the same column off the linked flowsheet row (wxyc-shared#318, WXYC/Backend-Service#1827), so a client can reconcile a proxy response against the feed row it came from. Add a value here, not in a copy of the list.

    """

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
    """
    Streaming service URLs for a release. Used across multiple schemas (DiscogsMatchResult, AlbumMetadata, etc.) to canonicalize the set of streaming fields.

    """

    spotify_url: str | None = Field(None, description="Spotify album URL")
    apple_music_url: str | None = Field(None, description="Apple Music album URL")
    youtube_music_url: str | None = Field(None, description="YouTube Music search URL")
    bandcamp_url: str | None = Field(None, description="Bandcamp album URL")
    soundcloud_url: str | None = Field(None, description="SoundCloud search URL")


class StreamingResolutionStatus(StrEnum):
    """
    Per-service streaming resolution verdict for one release on a lookup result. `verified` — the service was consulted and a URL was found (accompanies a non-null sibling `*_url`). `absent` — the service was consulted and returned no match; the null `*_url` is terminal and must NOT be re-probed. `unresolved` — a probe was attempted but was inconclusive (timed out, the enrichment tail was shed, or the cache was a cold miss); the null `*_url` is transient and MAY resolve on a later retry. A service that was never consulted at all is represented by the ABSENCE of its key from `StreamingResolution` (never by `absent`), so a consumer never negative-caches a service that was simply never asked. Reuses the per-service verdict vocabulary of `/api/v1/streaming-check` (LML#376). See LML#1053 / BS#1819.

    """

    verified = "verified"
    absent = "absent"
    unresolved = "unresolved"


class StreamingResolution(BaseModel):
    """
    Per-service streaming resolution status for one lookup result, keyed by streaming service (keys match the `*_url` prefixes on `DiscogsMatchResult`). A service's key is PRESENT only when that service was consulted during this lookup; an omitted service was never probed and must NOT be treated as `absent` (the never-consulted state — key omission is its sole encoding, so a consumer has one rule, not two). Additive and independent of the `*_url` fields: it annotates why a `*_url` is null (transient `unresolved` vs terminal `absent`) without changing the meaning of the url fields themselves. Mirrors the per-service shape of `StreamingCheckSources` (LML#376), except a consulted-but-absent service is the explicit `absent` verdict rather than a null, so these per-service fields are non-nullable. Covers the services with a genuine probe/absence distinction on the lookup path (`spotify`, `apple_music`, `bandcamp`); YouTube Music and SoundCloud are search-URL-only and carry no resolution verdict.

    """

    spotify: StreamingResolutionStatus | None = None
    apple_music: StreamingResolutionStatus | None = None
    bandcamp: StreamingResolutionStatus | None = None


class ReconciledIdentity(BaseModel):
    """
    External identifiers for a real-world artist, suitable for joining records across services. All fields are bare platform IDs; URL construction is the consumer's responsibility (templates are stable for Spotify, Apple Music, and Bandcamp; YouTube Music and SoundCloud do not have stable per-ID URLs and are intentionally absent — those belong on `StreamingLinks`, which serves the playback/result layer).

    """

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
    include_identity: bool = Field(
        False,
        description="Per cross-cache-identity plan §3.2.2 (E2-LML write contract). When true, the response is meant to carry an additional `identity` block (the §3.2.5 cascade's per-source resolution detail) with `api_version` set to 2 — see `LookupResponse.api_version` and `LookupResponse.identity`. Neither side of that is implemented yet: no `LookupResponse(...)` construction site in the producer ever sets `api_version` or `identity`, not even when this flag is true, so both fields ship `null` — never an omitted key — on every `/lookup` response in production today, regardless of what this flag is set to (WXYC/wxyc-shared#316). A consumer MUST test `api_version === 2` to detect support, never presence of either key: absent, `null`, and a producer that hasn't implemented this at all must all read \"not supported\".\n\nNo current caller sets this to true. A source read of Backend-Service (re-verified for this fix at `67d6d7b`, the same SHA the original WXYC/wxyc-shared#309 read used — Backend-Service has not moved since) finds no `library-identity-writer.ts` file and no `include_identity` reference anywhere in its TypeScript; the only occurrence in that repo is prose in `plans/library-hook-canonicalization/architecture-pivot-2026-05-09.md` describing the planned E2-BS writer, which has not been built. request-o-matic constructs `LookupRequest` without setting this field, which serializes as the `false` default; dj-site and tubafrenzy do not call `/lookup` with this field at all.\n",
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
    """
    A single item from the WXYC library catalog. Mirrors request-parser's LibraryItem with computed properties serialized as regular fields.
    Library identity is the `id` field alone — there is deliberately no `album_id`. An `id` of `0` is the "no catalog row" marker: the release is not shelved in the WXYC library, but the result may still carry a real Discogs identity in its `LookupResultItem.artwork`. See `LookupResultItem` for that row-less shape and its discriminator.

    """

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
    """
    Cache hit/miss statistics for a single lookup operation. Populated by LML's `core/telemetry.py:get_cache_stats()` and forwarded onto Sentry trace data by both the LML transaction (LML#213) and the BS-side client span wrapping `lookupMetadata` (BS#646).

    """

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
    """
    Present and equal to 2 only when the request set `include_identity: true` and the producer understands the flag. No `LookupResponse(...)` construction site in the producer sets this field anywhere yet — not even when the request sets `include_identity: true` — and the endpoint is served through FastAPI's `response_model` without `response_model_exclude_none`, so it ships `null` — never an omitted key — on every `/lookup` response in production today (WXYC/wxyc-shared#316); `nullable: true` documents that wire instead of describing one nobody emits.

    Because of that, a consumer MUST test for the value `2` and must never test for key presence — `!== undefined` against the generated TypeScript reads TRUE for a producer that implements none of this, the inverse of what the marker exists to signal. Absent, `null`, and a producer that hasn't implemented `include_identity` at all must all read "not supported", and only the literal value `2` reads "supported" — which keeps the check immune to whether any given producer omits or nulls its unset optionals.

    """

    integer_2 = 2


class SearchType(StrEnum):
    """
    The search strategy that produced results: direct, fallback, alternative, compilation, song_as_artist, or none

    """

    direct = "direct"
    fallback = "fallback"
    alternative = "alternative"
    compilation = "compilation"
    song_as_artist = "song_as_artist"
    none = "none"


class DegradedReason(StrEnum):
    """
    Why the response was degraded. Present only when `degraded: true`; omitted otherwise. Non-exhaustive — LML may add reasons in a later minor version. The Swift and Kotlin codegen decode an unknown value into an `unknownDefault` case and TypeScript consumers see a widened string-literal union, but the Python (datamodel-codegen → pydantic) consumers use strict enums and must regenerate against the new `@wxyc/shared` minor before LML emits a newly added reason. `deadline_exceeded` — the caller's budget or LML's spine deadline elapsed and the enrichment tail was skipped; `cache_only` — LML served cached data without refreshing from upstream; `upstream_unavailable` — an upstream (Discogs, streaming providers) was rate limited or down and its contribution was omitted. Set by LML#930's caller-deadline / admission path; read by LML#931 (`degraded-mode-result` telemetry) and wxyc-canary#82.

    """

    deadline_exceeded = "deadline_exceeded"
    cache_only = "cache_only"
    upstream_unavailable = "upstream_unavailable"


class IdentitySource(StrEnum):
    """
    The metadata source whose external ID is being recorded. The set of sources matches the columns on `library_identity_source` per plan §3.2.0. Add new sources by appending to this enum and to the `library_identity_source` schema in lockstep.

    """

    discogs = "discogs"
    musicbrainz = "musicbrainz"
    wikidata = "wikidata"
    spotify = "spotify"
    apple_music = "apple_music"
    bandcamp = "bandcamp"


class IdentityMethod(StrEnum):
    """
    The matcher method that produced this resolution, drawn from plan §3.4.1's locked confidence-method enum. The order in this list also reflects descending confidence ceilings (manual = 1.00, cross_source_agreement up to 1.00, exact_match = 1.00, name_variation 0.90-0.99, member_group 0.80-0.89, alias_match 0.75-0.84, trigram 0.70+, llm 0.60-0.79). Backend's writer rejects rows where confidence falls outside the method's locked range (§3.2.2 sanity check).

    """

    manual = "manual"
    cross_source_agreement = "cross_source_agreement"
    exact_match = "exact_match"
    name_variation = "name_variation"
    member_group = "member_group"
    alias_match = "alias_match"
    trigram = "trigram"
    llm = "llm"


class IdentitySkipReason(StrEnum):
    """
    Why a source's leg of the cascade did not run. Set on `IdentityResolution.reason` when `attempted: false`. Order is irrelevant; values are mutually exclusive per leg.

    """

    error = "error"
    manual_override_protected = "manual_override_protected"
    disabled = "disabled"
    prerequisite_failed = "prerequisite_failed"


class IdentityResolution(BaseModel):
    """
    One source's leg of the §3.2.5 cascade for a single library row. Either resolved (`attempted: true` and `external_id` populated when a match was found, NULL when the source ran but found no match) or skipped (`attempted: false` with `reason` populated and the rest nullable). The full `LookupIdentityBlock.resolved` array is complete (one entry per known source), not sparse — consumers can tell apart "leg ran, no match" from "leg didn't run" without guessing.

    """

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
    """
    Per-source identity resolution detail for a single library row. Meant to be returned on `LookupResponse.identity` when the request set `include_identity: true` and the producer understands the flag. Per §3.2.2, the `resolved` array is complete (one entry per source LML knows about, including the sources whose legs didn't run with `attempted: false`) — consumers get a deterministic shape regardless of which legs were available, whenever this block is actually populated.

    No `LookupResponse(...)` construction site in the producer sets `LookupResponse.identity` anywhere yet — not even when the request sets `include_identity: true` — and the endpoint is served through FastAPI's `response_model` without `response_model_exclude_none`, so `identity` ships `null` — never an omitted key — on every `/lookup` response in production today (WXYC/wxyc-shared#316). `nullable: true` documents that wire instead of describing a shape nobody emits. A consumer distinguishes "identity resolution ran and found nothing" from "the producer hasn't implemented `include_identity`" the same way as `LookupResponse.api_version` — by reading `api_version`, not by probing `identity` for presence or nullness. `identity` alone cannot make that distinction: it is `null` in both cases.

    `nullable: true` lives here, on the schema, rather than as an `allOf`-wrapped sibling on `LookupResponse.identity` — the two are equivalent where oasdiff can see through the composition, but an `allOf`-wrapped nullable ref to an object schema with its own `required` list cascaded into a spurious `response-required-property-removed` finding that the direct schema-level `nullable: true` does not, and marking a bare `$ref` nullable by writing the keyword next to it does nothing at all (OpenAPI 3.0 ignores sibling keys next to a `$ref`). This is safe only because `LookupIdentityBlock` is referenced exactly once in this spec, from `LookupResponse.identity` — a second reference would inherit this nullability unconditionally, so re-examine whether `null` is correct there too before adding one.

    """

    resolved: list[IdentityResolution] = Field(
        ...,
        description="One entry per known source. Order is stable (matches the `IdentitySource` enum order). Always populated even when no source resolved — the array carries `attempted: false` entries in that case.\n",
    )


class BulkResolveInput(BaseModel):
    """
    One library row to resolve. Uniform shape — LML auto-detects V/A from the row and discriminates the response on `kind`. The denormalized hint fields (`artist_name`, `album_title`) are not required to be canonical; LML may use them as matcher hints or ignore them in favour of its own mirror of Backend's library data. They are present so LML doesn't need a Backend DB read to resolve.

    """

    library_id: int = Field(..., description="Backend `wxyc_schema.library.id` (FK target).")
    legacy_release_id: int | None = Field(
        None,
        description="The row's legacy MySQL `LIBRARY_RELEASE_ID` — Backend's `library.legacy_release_id` column, total and NOT NULL since the WXYC/Backend-Service#1963 mint + NULL-legacy backfill (migration 0137). This is the id space library.db (and therefore LML's per-track store, `lml_cache.compilation_track_identity`) is keyed by; `library_id` above is Backend's own serial and is NOT in this space, so without this field LML cannot join a row to its per-track data at all (WXYC/library-metadata-lookup#1021, WXYC/Backend-Service#1991). Optional and nullable for wire compatibility only: absent or NULL means the caller predates this field. Because Backend's column is total, an upgraded emitter always has a value to send — a NULL from an upgraded emitter is a projection defect worth surfacing, not routine data. LML answers bridge-less `include_tracks: true` rows of the resolved kinds with the not-yet-visited state (`tracks_attempted: false`, empty `tracks`) so consumers keep re-asking rather than mis-reading anything as resolved (`kind: unresolved` carries neither field, as ever). Meaningful only alongside `include_tracks: true`; harmless otherwise. Backend-authored catalog adds mint `legacy_release_id` from a sequence floored at 1,000,000 (WXYC/Backend-Service#1963), which the Backend-sourced library.db producer (discogs-etl#351) will emit AS library.db's `id` — joining the spaces for post-tubafrenzy rows once that source cutover (discogs-etl#346) lands; until then a Backend-minted id matches no library.db row and simply reads as not-yet-visited.\n",
    )
    artist_name: str = Field(
        ..., description="Hint — Backend's denormalized artist name for the row."
    )
    album_title: str = Field(
        ..., description="Hint — Backend's denormalized album title for the row."
    )


class BulkResolveResultKind(StrEnum):
    """
    Discriminator for `BulkResolveResult`. `single_artist` carries a composed `main` identity; `compilation` carries none (a V/A row has no album-level artist anchor). Either may carry `tracks` and `tracks_attempted` — per-track identity is gated by the request's `include_tracks` flag, not by `kind` (#297). `unresolved` is a first-class outcome — caching it (with TTL) prevents repeated re-asking for unresolvable rows — and never carries `main`, `tracks`, or `tracks_attempted`.

    """

    single_artist = "single_artist"
    compilation = "compilation"
    unresolved = "unresolved"


class BulkResolveProvenanceEntry(BaseModel):
    """
    One per-source row in LML's reconciliation log for a single result. Surfaces the (source, method, confidence, external_id) tuple that fed §3.4.1.1 composition. Always returned (empty provenance array on `BulkResolveResult` means LML attempted the cascade and no source produced a row).

    """

    source: IdentitySource
    method: IdentityMethod
    confidence: confloat(ge=0.0, le=1.0) | None = Field(
        ...,
        description="Per-source confidence in [0, 1], within the source's locked method range from §3.4.1. NULL when `external_id` is NULL — the source ran but produced no candidate, so confidence is undefined (not zero). When non-null, equal to or greater than the `confidence` of the object this entry hangs off — the composition rules either MIN or boost, and never lower a per-source row. That referent is grain-relative, because this schema is reused at two grains: under `BulkResolveResult.provenance` it is the result's own `confidence`, and under `BulkResolveTrackIdentity.sources` it is that track's `confidence`, not the result's. The two are unrelated — a `kind: compilation` result has no result-level `confidence` at all, while its tracks each have their own.\n",
    )
    external_id: str | None = Field(
        None,
        description="The external identifier this source resolved to (Discogs release ID, MusicBrainz MBID, Wikidata QID, Spotify URI suffix, etc.). NULL when the source ran but found no match (the row is still surfaced so consumers see the leg ran); `confidence` is also NULL in that case.\n",
    )


class BulkResolveTrackIdentity(BaseModel):
    """
    Per-track resolution for one library row — V/A or not. Returned only when the request set `include_tracks: true`; one entry per track LML's per-track matcher visited. `sources` reflects the per-source rows LML composed the track identity from — audit provenance, not a storage instruction: LML's per-track store is the per-source system of record, and consumers persist only the composed verdict fields on this entry. `1.29.0` said instead that Backend writes these rows verbatim into `library_track_identity_source`; that table was never built. WXYC/Backend-Service#792 — the Backend-side ticket that would have created it — closed as a design decision and the schema half never happened, confirmed against prod and all 136 migrations in https://github.com/WXYC/Backend-Service/issues/801#issuecomment-5187348795. (`1.29.0` attributed that instruction to WXYC/library-metadata-lookup#271, which is open and scopes LML's own per-track identity work, not Backend storage.) The chosen identifiers are not lifted into a per-track `external_ids` block at this contract version.

    The entry is addressable without `track_position`: 78% of Backend's `compilation_track_artist` rows carry a NULL position (WXYC/Backend-Service#1989), so `artist_name` + `track_title` are the join-back key a consumer can actually use. `1.29.0` shipped `track_position` as the only row identifier plus per-source legs, with no composed verdict and no canonical artist — unconsumable for WXYC/Backend-Service#1991, which went unnoticed because the array has only ever shipped empty. The four added fields below close that gap.

    """

    track_position: str | None = Field(
        ...,
        description='Track position as the source has it ("A1", "3", …). Required-but-nullable: the key is always present, and NULL says "no position is recoverable for this track" rather than "not echoed". Not a usable row identifier on its own — most of Backend\'s compilation-track rows are position-NULL (WXYC/Backend-Service#1989); join on `artist_name` + `track_title` instead.\n',
    )
    artist_name: constr(min_length=1) = Field(
        ...,
        description="Join-back echo of the per-track credit, verbatim, and the load-bearing half of the join key — never NULL, and never the empty string (`minLength: 1`, matching `CatalogCompilationTrackRow.artist_name`, since an empty join key is indistinguishable from a missing one). No `maxLength` here, unlike the CTA read/write shapes: those bound one physical `varchar(255)` column, while this field is dual-mode and the `kind: single_artist` arm carries a source credit that no WXYC column bounds. A cap the sources don't respect would turn a long credit into a decode failure. Dual-mode: for `kind: compilation` it echoes the `compilation_track_artist` row as exported in library.db, so a consumer can match it to its own CTA row; for `kind: single_artist` there is no CTA row and it carries the credit as LML's source has it.\n",
    )
    track_title: str | None = Field(
        ...,
        description="Join-back echo of the track title, completing the join key. Dual-mode in the same way as `artist_name` — the library.db CTA row's title for `kind: compilation`, the source's track title for `kind: single_artist`. NULL when that row or source carries no title (the underlying CTA column is nullable).\n",
    )
    resolved_artist_name: str | None = Field(
        ...,
        description="The composed canonical artist for this track — the per-track analogue of `BulkResolveResult.main`, carried as a name because LML does not know Backend's artist ids; consumers map it to their own artist tables locally. NULL when the matcher visited this track and resolved nothing: the entry is still returned so consumers see the leg ran (same convention as `BulkResolveProvenanceEntry.external_id`), and `confidence` and `method` are NULL alongside it.\n",
    )
    confidence: confloat(ge=0.0, le=1.0) | None = Field(
        ...,
        description="Composed track-level confidence in [0, 1] — where LML#1021's per-track composition lands (cross-source agreement boost, MIN-of-confidences fallback), applied at track grain exactly as `BulkResolveResult.confidence` applies at album grain. NULL when `resolved_artist_name` is NULL — the matcher ran and produced no verdict, so confidence is undefined (not zero).\n",
    )
    method: IdentityMethod | None = Field(
        ...,
        description="The composed track-level method — typically the strongest leg's method, or `cross_source_agreement` when LML#1021's boost fired. NULL when `resolved_artist_name` is NULL.\n",
    )
    sources: list[BulkResolveProvenanceEntry] = Field(
        ...,
        description="One per-source row per source LML composed the track from — audit detail, and the track-grain analogue of `BulkResolveResult.provenance`. Empty array means no source produced a row at track grain at all. That is a different statement from a populated array whose entries carry NULL `external_id`: there, the legs ran and reported no candidate. Read the verdict off `resolved_artist_name`, not off this array's length — an empty `sources` and a `sources` full of NULL-`external_id` legs both accompany a NULL `resolved_artist_name`.\n",
    )


class BulkResolveResult(BaseModel):
    """
    One verdict per request input, in the same order as the request. `kind` discriminates the body shape. `library_id` echoes the request input so consumers can re-key the response without relying on positional ordering.

    """

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
        description='Whether LML\'s per-track matcher has visited this row — `true` once it has run, **regardless of how many tracks it resolved**, including none. This is the resolved signal for per-track identity; `tracks.length` is not (WXYC/Backend-Service#1991).\n\nRead it with `tracks` as a pair, four states in total: **absent or NULL** — the caller did not ask (`include_tracks` false or omitted), or the result is `kind: unresolved`, and `tracks` is in the same state; **`false` with an empty `tracks`** — the caller asked and the matcher has not reached this row; **`true` with an empty `tracks`** — the matcher ran and resolved nothing; **`true` with a populated `tracks`** — the matcher ran and produced entries. `false` alongside a populated `tracks` is not a state; producers must not emit it. A consumer that observes `tracks_attempted: false` alongside a populated `tracks` anyway MUST read it as `true` — the populated array is stronger evidence of what happened than the flag that disagrees with it, and reading the row as unattempted would either drop already-resolved tracks or re-ask a row that already has an answer (WXYC/wxyc-shared#303).\n\nThe middle two are what this field exists for. They are byte-identical in `tracks`, they are not rare — a `kind: single_artist` release LML holds no tracklist for is the ordinary case, and extending the gate to that kind is the whole point of #297 — and a consumer that reads emptiness as "not yet visited" re-asks every one of them on every pass, forever. That is the re-asking pathology `kind: unresolved` was made a first-class outcome to prevent, reintroduced at track grain. A consumer cannot repair it locally, because the two states are indistinguishable on the wire without this field.\n\nAbsent and NULL are one state, for the same producer reason as `tracks` below.\n',
    )
    tracks: list[BulkResolveTrackIdentity] | None = Field(
        None,
        description='Per-track identity, returned only when the request set `include_tracks: true`. That flag — not `kind` — gates the field, and it gates it on both `kind: single_artist` and `kind: compilation` (#297); `kind: unresolved` never carries tracks. The array carries the entries; **it does not carry the state** — an empty array means the matcher found nothing *or* has not run, and only `tracks_attempted` separates those. See that field for the four states of the pair. Entries whose `resolved_artist_name` is NULL are tracks the matcher tried and failed on ("the leg ran", the same convention as `BulkResolveProvenanceEntry.external_id`), so a populated array is per-track detail rather than a per-track guarantee.\n\nAbsent and NULL are the same state, deliberately. LML builds every non-track result with `tracks=None` and serves the endpoint through FastAPI\'s `response_model` without `response_model_exclude_none`, so the wire has always carried `"tracks": null` rather than omitting the key. `nullable: true` documents the shipped producer instead of describing a wire nobody emits; consumers must accept both spellings.\n',
    )


class TracksContractVersion(IntEnum):
    """
    Present and equal to 1 only when the request set `include_tracks: true`, the producer understands the flag, AND the producer has emitted `tracks_attempted` for both `kind: single_artist` and `kind: compilation` results — see `BulkResolveResult.tracks_attempted`. The two producer arms ship independently (LML#1138, LML#1021); setting the marker with one arm unimplemented tells a consumer to trust `tracks_attempted` on rows where it is still meaningless.

    Absent or NULL otherwise: both when `include_tracks` was false or omitted, and when the producer predates this field entirely, or has implemented only one of the two arms above. LML does not set this field anywhere yet and serves this endpoint without `response_model_exclude_none`, so it ships `null` — never an omitted key — on every response in production today (WXYC/wxyc-shared#310); `nullable: true` documents that wire instead of describing one nobody emits, the same treatment as `tracks` / `tracks_attempted` above.

    Because of that, a consumer MUST test for the value `1` and must never test for key presence — `!== undefined` against the generated TypeScript reads TRUE for a producer that implements none of this, the inverse of what the marker exists to signal. Absent, `null`, and a producer that predates this field must all read "not supported", and only the literal value `1` reads "supported" — which keeps the check immune to whether any given producer omits or nulls its unset optionals. Same precedent as `LookupResponse.api_version` (`LookupRequest.include_identity`): a producer-echoed capability marker that lets a consumer distinguish "the producer understood my flag and the answer is genuinely nothing" from "the producer predates my flag" — a distinction `tracks_attempted`/`tracks` cannot make alone, because both are spelled `null` in either case (WXYC/wxyc-shared#303). No schema-level `default`, for the same `openapi-typescript` `defaultNonNullable` reason documented on `BulkResolveLibrariesRequest.include_tracks`.

    """

    integer_1 = 1


class BulkResolveLibrariesResponse(BaseModel):
    """
    Response to `POST /api/v1/identity/bulk-resolve-libraries`. Order of `results` matches the order of the request's `inputs`. The example below is the `include_tracks: true` shape; with the flag false or omitted, every result's `tracks` and `tracks_attempted` are the absent/NULL state (LML spells both `null`). Per-source `method`/`confidence` pairs in the example sit inside `IdentityMethod`'s locked bands — Backend's writer rejects rows that don't, so an example that violated them would hand LML#1021 an unwritable row to copy. The example also carries `tracks_contract_version: 1`, echoing that the producer that emitted it understood `include_tracks`; see that field for the rollout-window ambiguity it exists to resolve.

    """

    tracks_contract_version: TracksContractVersion | None = Field(
        None,
        description='Present and equal to 1 only when the request set `include_tracks: true`, the producer understands the flag, AND the producer has emitted `tracks_attempted` for both `kind: single_artist` and `kind: compilation` results — see `BulkResolveResult.tracks_attempted`. The two producer arms ship independently (LML#1138, LML#1021); setting the marker with one arm unimplemented tells a consumer to trust `tracks_attempted` on rows where it is still meaningless.\n\nAbsent or NULL otherwise: both when `include_tracks` was false or omitted, and when the producer predates this field entirely, or has implemented only one of the two arms above. LML does not set this field anywhere yet and serves this endpoint without `response_model_exclude_none`, so it ships `null` — never an omitted key — on every response in production today (WXYC/wxyc-shared#310); `nullable: true` documents that wire instead of describing one nobody emits, the same treatment as `tracks` / `tracks_attempted` above.\n\nBecause of that, a consumer MUST test for the value `1` and must never test for key presence — `!== undefined` against the generated TypeScript reads TRUE for a producer that implements none of this, the inverse of what the marker exists to signal. Absent, `null`, and a producer that predates this field must all read "not supported", and only the literal value `1` reads "supported" — which keeps the check immune to whether any given producer omits or nulls its unset optionals. Same precedent as `LookupResponse.api_version` (`LookupRequest.include_identity`): a producer-echoed capability marker that lets a consumer distinguish "the producer understood my flag and the answer is genuinely nothing" from "the producer predates my flag" — a distinction `tracks_attempted`/`tracks` cannot make alone, because both are spelled `null` in either case (WXYC/wxyc-shared#303). No schema-level `default`, for the same `openapi-typescript` `defaultNonNullable` reason documented on `BulkResolveLibrariesRequest.include_tracks`.\n',
    )
    results: list[BulkResolveResult]
    cache_stats: CacheStats | None = None


class IdentityKind(StrEnum):
    """
    Discriminator for the identity being resolved. v1 of the `/api/v1/identity/resolve` endpoint accepts only `release`; the `artist` value is reserved for the planned symmetric extension.

    """

    release = "release"


class ReleaseIdentitySource(StrEnum):
    """
    Source whose external ID is being resolved into a release identity. Matches the v1 source list from LML#526; new sources land in lockstep with their column on `entity.release_identity` and a matching sentinel rule on the LML side.

    """

    discogs_release = "discogs_release"
    discogs_master = "discogs_master"
    bandcamp = "bandcamp"
    apple_music_album = "apple_music_album"


class ReleaseIdentityResolveRequest(BaseModel):
    """
    Request body for `POST /api/v1/identity/resolve`. The triple `(kind, source, external_id)` is the identity key — the same triple always returns the same identity_id.

    """

    kind: IdentityKind
    source: ReleaseIdentitySource
    external_id: str = Field(
        ...,
        description="Source-specific identifier. For `discogs_release` / `discogs_master` it is the positive integer ID as a string; zero and negative values are rejected with 422 (Discogs uses `0` for the unknown-release sentinel). For `bandcamp` it is the canonical album URL (e.g. `https://autechre.bandcamp.com/album/confield`); LML URL-canonicalises before mint so trailing-slash and equivalent variants converge on one identity row. Non-album Bandcamp URLs (track URLs, bare subdomain) are rejected with 422. For `apple_music_album` it is the numeric Apple Music album ID as a string (e.g. `1234567890`); zero, negative, and non-numeric values are rejected with 422.\n",
    )


class ReleaseIdentityResolveResponse(BaseModel):
    """
    Response to `POST /api/v1/identity/resolve`. `identity_id` is stable across calls; `minted` distinguishes the first call (true) from subsequent lookups (false). Re-resolves do not write a new reconciliation log row — the resolver only logs the act of minting.

    """

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
    """
    Origin of an artist search alias variant. Opaque to BS storage (just a tag); used by future read-path ranking and audit. Open enum — new sources can be added by LML without a wire-format break.

    """

    discogs_name_variation = "discogs_name_variation"
    discogs_alias = "discogs_alias"
    discogs_member = "discogs_member"
    wxyc_library_alt = "wxyc_library_alt"


class ArtistSearchAliasMethod(StrEnum):
    """
    Semantic classification of how this variant relates to the subject artist. Distinct from source — multiple sources can emit the same method.

    """

    name_variation = "name_variation"
    alias = "alias"
    member = "member"
    alt_curated = "alt_curated"


class ArtistSearchAliasVariant(BaseModel):
    """
    One alias variant for an artist. The `variant` string is what gets matched against search queries via trigram similarity. The `source` tag identifies where the row came from; `method` classifies the semantic relationship.

    """

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
    """
    What decided a resolved verdict. `identity_store` — a pre-existing `entity.identity` row short-circuited resolution (unverified: the store's resolved-at-most-once promise). `api_search` — a single-page live Discogs API artist search returned exactly one artist whose fixed-point identity-match form equals the input's, whose own title qualifier matches the input's (a "(N)"-titled singleton answering a bare input — or vice versa — is family evidence, not a resolution), and with no equality-leg cache conflict. Cache legs never decide; their evidence appears in `cache_corroboration`.

    """

    identity_store = "identity_store"
    api_search = "api_search"


class ArtistResolveCacheLeg(StrEnum):
    """
    A discogs-cache candidate-generation leg that produced at least one candidate for the input name. Mirrors the entity-resolution reconciler cascade's legs (exact / member / alias / name-variation / trigram — the cascade subset of `IdentityMethod`), but as candidate sets used for corroboration and conflict detection — never as verdicts. The four equality legs can veto a mint into `ambiguous`; fuzzy `cache_trigram` neighbors never veto.

    """

    cache_exact = "cache_exact"
    cache_member = "cache_member"
    cache_alias = "cache_alias"
    cache_name_variation = "cache_name_variation"
    cache_trigram = "cache_trigram"


class ArtistResolveUnresolvedReason(StrEnum):
    """
    Why a name did not resolve. `not_found` — the API tier ran and observed zero exact-form candidates on its single search page (terminal in v1; a search observation, not proof no such artist exists — cache legs, including `cache_exact`, may still show yield in `cache_corroboration`). `ambiguous` — two or more exact-form Discogs artists (including "(N)" disambiguation families, which normalize to the same form); an equality-leg cache candidate (exact / member / alias / name-variation — never a fuzzy `cache_trigram` neighbor) pointing at a different artist id than the API's single candidate; a lone API candidate whose own title qualifier mismatches the input's; or conflicting identity-store rows across one deduplicated group's spellings (`candidate_count` null — nothing was measured); ambiguous names never mint (terminal). `escalation_unavailable` — the Discogs API tier couldn't answer: saturation breaker open, outage cool-down, 429 or 5xx after retries, network errors, other non-2xx responses, a distrusted/unparseable page, or a deployment with no Discogs credentials configured. It means "couldn't ask," not "asked and missed" — consumers must not apply a no-match TTL. Retry with bounded attempts and backoff: most causes are transient, but a persistent `escalation_unavailable` across attempts indicates a configuration or upstream condition that retrying will not move.

    """

    not_found = "not_found"
    ambiguous = "ambiguous"
    escalation_unavailable = "escalation_unavailable"


class ArtistResolveResult(BaseModel):
    """
    Verdict for one input name, index-aligned with `ArtistResolveBulkRequest.names`. Exactly one of `discogs_artist_id` (resolved, with `method`) or `unresolved_reason` is present.

    """

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
    dry_run: bool = Field(
        False,
        description="Run every tier identically — including live Discogs API verification — but skip the `entity.identity` write-back. No partial-write mode.\n",
    )


class ArtistResolveBulkResponse(BaseModel):
    """
    Response to `POST /api/v1/artists/resolve/bulk`. `results` is index-aligned with the request's `names`.

    """

    results: list[ArtistResolveResult]


class ArtistGenresSource(StrEnum):
    """
    Where an artist's genre/style answer came from, so the caller can distinguish a genuine "Discogs has no genre data for this artist" (safe to negative-cache) from a transient "couldn't ask Discogs" (retry). `cache` — served from the discogs-cache `release_genre` rows. `discogs_api` — cache miss; the Discogs API fallback produced genres. `not_found` — cache miss; the API answered but the artist has no release/genre data (safe to negative-cache). `unavailable` — cache miss and the API could not be consulted (no Discogs token, saturation breaker open, or a transient fetch failure); retry, do NOT negative-cache.

    """

    cache = "cache"
    discogs_api = "discogs_api"
    not_found = "not_found"
    unavailable = "unavailable"


class ArtistGenresInput(BaseModel):
    """
    One artist to aggregate genres/styles for.
    """

    artist_name: constr(min_length=1) = Field(..., description="Artist display name.")
    discogs_artist_id: int | None = Field(
        None,
        description="Discogs artist ID. Optional but strongly preferred: when present the cache aggregation keys on it (stable Discogs artist entity, homonym-safe); when absent it falls back to a case-insensitive exact `artist_name` match. Backend-Service resolves it via `POST /api/v1/artists/resolve/bulk` (LML#759) before calling this endpoint, so it is usually present.\n",
    )


class BulkArtistGenresRequest(BaseModel):
    """
    Request body for `POST /api/v1/artists/genres/bulk`. The per-request artist cap is enforced at the router (413, before validation); an empty batch returns 422.

    """

    artists: list[ArtistGenresInput] = Field(..., max_length=25, min_length=1)


class ArtistGenresResultItem(BaseModel):
    """
    Per-artist aggregation, index-aligned with the request `artists` list.

    """

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
    """
    Response to `POST /api/v1/artists/genres/bulk`. `results` is index-aligned with the request's `artists`. No top-level counters — the caller derives them via `Counter(r.source for r in results)`.

    """

    results: list[ArtistGenresResultItem]


class CacheRefreshSourceOutcome(StrEnum):
    """
    Per-source release-leg outcome. `success` covers both cache hits and fresh fetches AND tombstones (Discogs 404s — the cache state is still current). `error` is transport / 5xx / parse failure. The leg did not complete. `not_implemented` is for sources whose cache layer doesn't exist yet (discogs_master pre-`MasterRelease.artists`, musicbrainz_release pre-LML#217, etc.). Consumers that need to distinguish "warm-real" from "warm-tombstone" should read the cache row's `not_found` flag directly, not infer from this enum.

    """

    success = "success"
    error = "error"
    not_implemented = "not_implemented"


class CacheRefreshItemStatus(StrEnum):
    """
    Per-identity rollup of the per-source outcomes. Release-leg-gated: any source `success` → `warmed` (artist failures inside a release walk are partial value, not a retry trigger). Only `not_implemented` sources → `not_implemented`. All sources errored → `error`. Row absent in `entity.release_identity` → `not_found` (with `sources` as null). Only `error` is a retry signal.

    """

    warmed = "warmed"
    not_found = "not_found"
    not_implemented = "not_implemented"
    error = "error"


class CacheRefreshArtistOutcome(BaseModel):
    """
    Per-artist outcome inside the walk-to-artists step of a per-source release refresh. `external_id` is string-typed for future source-agnosticism — Discogs IDs serialize as decimal strings.

    """

    external_id: str
    outcome: CacheRefreshSourceOutcome
    message: str | None = Field(
        None,
        description="Exception class name when `outcome != success`. Bare `str()` of the exception is intentionally NOT serialized — it may carry upstream error bodies, SQL fragments, or file paths. Full traceback lands in Sentry.\n",
    )


class CacheRefreshSourceResult(BaseModel):
    """
    Per-source outcome for one identity. `artists` is the walk-to-artists fan-out result for the Discogs release leg — empty list on tombstone-success (no artists to walk) and on legs that don't do a walk (every non-Discogs source today).

    """

    release_outcome: CacheRefreshSourceOutcome
    artists: list[CacheRefreshArtistOutcome] = Field([], validate_default=True)
    message: str | None = None


class CacheRefreshResultItem(BaseModel):
    """
    Per-identity verdict. `sources` is null when `status == not_found` (no row to dispatch against). Otherwise it is keyed by source vocabulary (`discogs_release`, `discogs_master`, …); see `RELEASE_SOURCE_COLUMN` on the LML side for the canonical set.

    """

    identity_id: int
    status: CacheRefreshItemStatus
    sources: dict[str, CacheRefreshSourceResult] | None = None
    message: str | None = None


class BulkCacheRefreshRequest(BaseModel):
    """
    Request body. The 50-id batch cap is enforced server-side with HTTP 400 on overflow (not 422 — LML's `/api/v1/lookup/bulk` precedent). Downstream callers should encode 50 as a hard constant or assert at startup, not expose it as an env knob — the cap is bounded by Discogs rate-limit × cold-cache fan-out ≤ Railway's request-timeout ceiling, and recalibration must land alongside any ingress change. Empty `identity_ids` returns 422 (Pydantic `min_length=1`). Duplicate identity_ids are allowed; each runs independently and shows up as a separate result.

    """

    identity_ids: list[int] = Field(..., min_length=1)


class BulkCacheRefreshResponse(BaseModel):
    """
    Per-identity verdicts in input order. No top-level counters — callers derive them via `Counter(r.status for r in results)`, matching the `BulkLookupResultItem` precedent.

    """

    results: list[CacheRefreshResultItem]


class DiscogsTrackItem(BaseModel):
    """
    A track on a release, with optional per-track artist credits (for compilations).
    """

    position: str
    title: str
    duration: str | None = None
    artists: list[str] = []


class DiscogsArtistCredit(BaseModel):
    """
    An artist credit on a release (performer, producer, etc.).
    """

    artist_id: int | None = None
    name: str
    join: str = Field("", description='Join phrase (e.g. " & ", ", ")')
    role: str | None = Field(
        None, description='Role for extra artists (e.g. "Producer", "Mixed By")'
    )


class Provenance(StrEnum):
    """
    `track` = scoped to the resolved track's per-track credits (precise); `release` = a release-level credit applied to the whole release (approximate for an individual track, mirroring tubafrenzy's auto-fill-from-artist fallback). Populated as `release` in the initial rollout; `track` is added when per-track resolution lands.

    """

    track = "track"
    release = "release"


class DiscogsWriterCredits(BaseModel):
    """
    Songwriter/composer credits for the resolved playcut, derived from the Discogs writer-role credits already fetched at resolution time (the `extra=1` artist credits whose `role` is a writer role — e.g. "Written-By", "Composed By", "Music By", "Lyrics By", "Songwriter", "Words By", including hyphen/space, bracketed, and comma-compound variants). Surfaced on `DiscogsMatchResult.writer_credits` for BMI performance-list reporting; populated only when `extended` is true and a writer credit resolves, and omitted entirely when nothing resolves — names are never fabricated. A consumer with no resolved composer falls back to artist-as-proxy.

    """

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
    embed: bool = True


class DiscogsReleaseMetadata(BaseModel):
    """
    Full release metadata from LML, including tracklist and enriched credits.
    """

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
    genres: list[str] = []
    styles: list[str] = []
    tracklist: list[DiscogsTrackItem] = Field([], validate_default=True)
    artwork_url: str | None = None
    artwork_checked_at: AwareDatetime | None = Field(
        None,
        description="Timestamp of the most recent live Discogs API call that resolved\nthis release's artwork. `null` means LML's bulk loader populated\nthe row but the live API has not been queried yet (the \"never\nasked\" state); a value means LML hit the live API and either\npopulated `artwork_url` or confirmed Discogs has no cover. LML\nuses this to distinguish bulk-loader gaps (which it back-fills)\nfrom genuinely-imageless releases (which it does not refetch).\nSee WXYC/discogs-etl#239 + WXYC/library-metadata-lookup#423.\n",
    )
    not_found: bool = Field(
        False,
        description='Tombstone marker for Discogs 404s on `get_release`. `true` means\nLML hit the live Discogs API for this release id and Discogs\nreturned 404; subsequent reads short-circuit on this flag rather\nthan re-burning the rate-limit budget. The tombstone row carries\n`title = ""` and `artist = ""` as identifier sentinels;\nconsumers must guard against rendering those empty strings as\nreal values. `release_url` is identifier-derived\n(`https://www.discogs.com/release/{release_id}`) and remains\nvalid even on a tombstone. LML\'s public boundary translates\n`not_found = true` back to `None` for direct callers; this flag\nis observable only by consumers that read the cache row\ndirectly (none of which exist in LML\'s public API today, but\nthe contract is exposed here for future cross-service readers).\nSee WXYC/library-metadata-lookup#510.\n',
    )
    release_url: str
    cached: bool = False
    artists: list[DiscogsArtistCredit] = Field([], validate_default=True)
    extra_artists: list[DiscogsArtistCredit] = Field([], validate_default=True)
    labels: list[DiscogsLabelCredit] = Field([], validate_default=True)
    released: str | None = Field(None, description="Release date as ISO string")
    videos: list[DiscogsReleaseVideo] = Field([], validate_default=True)


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
    """
    A single resolved token from Discogs markup parsing. Discriminated by the `type` field.
    """

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
    active: bool = True


class DiscogsArtistDetails(BaseModel):
    """
    Full artist details from Discogs, including bio, aliases, and group members.
    """

    artist_id: int
    name: str
    profile: str | None = None
    profile_tokens: list[DiscogsResolvedToken] | None = Field(
        None, description="Pre-parsed structured tokens from the Discogs profile markup"
    )
    image_url: str | None = None
    name_variations: list[str] = []
    aliases: list[Alias] = Field([], validate_default=True)
    members: list[Member] = Field([], validate_default=True)
    urls: list[str] = []
    cached: bool = False


class DiscogsReleaseInfo(BaseModel):
    """
    A single release from the track-releases search.
    """

    album: str
    artist: str
    release_id: int
    release_url: str
    is_compilation: bool = False


class DiscogsTrackReleasesResponse(BaseModel):
    """
    Response from LML's track-releases search endpoint.
    """

    track: str | None = None
    artist: str | None = None
    releases: list[DiscogsReleaseInfo] = Field([], validate_default=True)
    total: int = 0
    cached: bool = False


class TrackMatchSource(StrEnum):
    """
    Provenance of a TrackMatchHint. `cta` indicates Backend's local `compilation_track_artist` lookup (Track 1, BS-local). `discogs_release` indicates LML matched via a Discogs release_id (Track 2; pre-cross-cache-identity, this is a direct CEI release-format hit; post-cross-cache-identity, it's a `library_identity.discogs_release_id` hit). `discogs_master` indicates LML matched via a Discogs master_id (Track 2; the more common pre-cross-cache-identity path, since ~90% of CEI rows are master-format). `library_identity` indicates an explicit substrate match — emitted post-cross-cache-identity when LML's pipeline exposes the distinction in its response.

    """

    cta = "cta"
    discogs_release = "discogs_release"
    discogs_master = "discogs_master"
    library_identity = "library_identity"


class TrackMatchHint(BaseModel):
    """
    Populated on library search / lookup result items when a track-title match drove the inclusion of this release into the results. The `source` field distinguishes the underlying match pathway so consumers can render appropriate UX (e.g., qualitative "matched via Discogs (verified)" tooltips) and downstream audits can break coverage down by source.

    """

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
    """
    Populated on library search result items when an artist-alias match from the BS-local `artist_search_alias` cache drove the inclusion of this release into the results (artist-search-alias plan PR 5). Sibling to TrackMatchHint — track hints carry title + position + artist_credit which have no meaning for alias matches; alias hints carry the variant string that scored and its source tag for UX rendering and audit.

    """

    matched_variant: str = Field(
        ..., description="The cached alias string that trigram-matched the query."
    )
    source: ArtistSearchAliasSource


class LibrarySearchItem(BaseModel):
    """
    A single item from LML's library catalog search.
    """

    id: int | None = Field(
        ...,
        description="The release's Backend identifier — `wxyc_schema.library.id` — once the proxy id re-map (WXYC/Backend-Service#2168) ships. Until that deploy it still carries the library.db legacy id, the same value as `legacy_release_id` below, because library.db has no other id to give. The distinction is not cosmetic: the two spaces are numerically coextensive but unrelated, and an id read in the wrong one lands on a *different real release* 87.7% of the time (WXYC/dj-site#1179) — a silently wrong album link, not a miss.\n\nRequired but nullable. A `null` here comes only from Backend's proxy rewrite path (`GET /proxy/library/search`), for a row whose legacy id has no Backend counterpart: 609 of 64,731 library.db rows as measured 2026-08-14 (599 collapsed into a sibling sharing their `LIBRARY_CODE`, 10 absent outright), all of them already unreachable from the dj-site picker. LML's own `GET /library/search` never emits null here.\n",
    )
    legacy_release_id: int | None = Field(
        None,
        description="The library.db producer key — upstream tubafrenzy `LIBRARY_RELEASE.ID`, or the Backend mint floored at 1,000,000 for Backend-authored catalog rows (WXYC/Backend-Service#1963). This is the space LML's own per-release reads resolve against, and the id embedded in `library_url` below; Backend's serial `library.id` is not in it. Optional and nullable for wire compatibility only — absent means the producer predates WXYC/Backend-Service#2167, in which case `id` above still carries this value.\n",
    )
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
        description="Per-release dj.wxyc.org permalink for this release. Points at the dj-site legacy front door `/dashboard/album/legacy/{id}`, whose `{id}` segment is the `legacy_release_id` above — not this row's `id`, which the proxy re-map (WXYC/Backend-Service#2168) moves into Backend's serial space. The front door resolves that legacy id to the canonical release route server-side and 308-redirects. Null when unavailable.\n",
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
    """
    A match found on a single streaming service.
    """

    url: str = Field(..., description="URL to the matched album on the service")
    confidence: float = Field(..., description="Match confidence score (0-100)")


class StreamingCheckSources(BaseModel):
    """
    Per-service streaming match results.
    """

    spotify: StreamingSourceMatch | None = None
    deezer: StreamingSourceMatch | None = None
    apple_music: StreamingSourceMatch | None = None
    bandcamp: StreamingSourceMatch | None = None


class StreamingCheckResponse(BaseModel):
    on_streaming: bool | None = Field(
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
    donateUrl: str | None = Field(
        None,
        description="Canonical hosted donation page URL behind the \"Support the station\" entry point. Backend-Service serves `''` — never `null`, never an omitted key — whenever its `DONATE_URL` variable is unset (WXYC/Backend-Service#2111), so a client MUST treat an empty string exactly as it treats an absent field and fall through to its own fallback destination. That empty-string wire value is also why this carries no `format: uri`: `''` is a legitimate value here and would fail uri validation. An enabled entry point with an empty or otherwise unusable URL is a reachable server state, because `DONATE_URL` and `DONATE_ENABLED` are independent variables on one deploy — `donateEnabled` governs visibility alone and never implies this field is usable. A client that cannot resolve a usable destination from this field or from its own fallback MUST NOT render the entry point.\n",
    )
    donateEnabled: bool | None = Field(
        None,
        description='Whether clients should render the donate entry point. `false` hides it. The field carries no schema-level `default` on purpose, for the same reason as `BulkResolveLibrariesRequest.include_tracks`: `openapi-typescript` emits a default-bearing property as non-optional even when it is absent from `required`, which would arm the very decode-failure cascade both donate fields are kept optional to avoid. Absent therefore means "the producer predates this field", and each client applies its own bootstrap default — absence is NOT a kill switch and MUST NOT be read as one, so a client whose own default is "show" owns the validity of its fallback destination for that window. `false` is likewise a deploy-time switch rather than an instant one: `GET /config` is served `Cache-Control: public, max-age=3600` and clients may cache the decoded config for the life of a process, so an entry point can keep rendering for an hour or more after the variable flips. Anything needing a faster kill than that belongs at the destination, not here.\n',
    )


class AppSecrets(BaseModel):
    discogsApiKey: str = Field(
        ...,
        description="Discogs API consumer key, for clients that call Discogs directly. Backend-Service serves an empty string (`''`) — never `null`, never an omitted key — whenever its `DISCOGS_API_KEY` variable is unset (WXYC/wxyc-shared#348 records the provenance; the Backend-Service handler falls back `process.env.DISCOGS_API_KEY || ''`), the same fallback shape `AppConfig.donateUrl` documents. Unlike `donateUrl`, there is no client-side substitute for a missing credential: a client that reads `''` here cannot call Discogs directly and must fall back to a server-proxied path instead. Both fields stay `required` because the handler always emits both keys via that fallback, so a spec-conformant producer never omits either; should a future producer ever omit one anyway, a strict consumer's decode of the whole `AppSecrets` object fails, which is the intended trade — a torn set of credentials (one present, one silently missing) is worse than a whole-object decode failure at the decode site. In practice that surfaces as a logged decode error and a degraded (server-proxied) Discogs path, not a crash — but it is still a diagnosable deploy misconfiguration, where half a credential pair would fail only at Discogs's door with an opaque 401.\n",
    )
    discogsApiSecret: str = Field(
        ...,
        description="Discogs API consumer secret, for clients that call Discogs directly. Same empty string (`''`) wire value when unset as `discogsApiKey` (`process.env.DISCOGS_API_SECRET || ''`) and the same required-together rationale: this field only carries meaning paired with `discogsApiKey`, so both are `required` and a producer omitting either arms a decode failure by design rather than a client silently attempting a call with half a credential pair.\n",
    )


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
    """
    Response body for `GET /proxy/metadata/album`. Two tiers of field share one flat wire shape, with no `base`/`enriched` wrapper to tell them apart.

    **Base** — durable Backend-Service state, never an upstream read (WXYC/Backend-Service#1827). Six fields in the handler. Three are declared here — `recordLabel`, `labelId`, `metadataStatus`, resolved from the linked flowsheet row. The other three are emitted but not yet declared: `artistName`, `releaseTitle` and `trackTitle`, echoed straight back from the request and therefore present on every response, including one where every upstream call failed (`artistName` unconditionally, the other two whenever the caller supplied them). Contracting those three is follow-up work on wxyc-shared#318 — read their absence from this schema as undeclared, not as unemitted.

    **Enriched** — `artworkUrl`, `discogsUrl`, `genres`, `label`, the streaming URLs and the rest, resolved from Discogs via library-metadata-lookup.

    The split is the guarantee: a library-metadata-lookup timeout can blank every enriched field but can never blank a base one.

    **Freshness caveat — the base fields are memoized, so they are not re-read per request.** The handler memoizes the assembled response for 1h (WXYC/Backend-Service#988), keyed on the normalized `(artistName, releaseTitle, trackTitle)` plus the critic-reviews flag bit. Only the three request-echoed fields are excluded from the cached value; `recordLabel`, `labelId` and `metadataStatus` are part of it. A cache hit short-circuits the flowsheet read entirely, so these three describe the linked row as it was when the entry was written rather than as it is now. Two consequences worth designing around: for up to an hour after a DJ links a previously free-text play, the response can still omit all three even though a linked row now exists; and a librarian's label correction can be shadowed by the old `recordLabel` for the same window. `metadataStatus` is bounded more tightly than its two siblings — a `pending` or `enriching` snapshot is never memoized (WXYC/Backend-Service#1893), so a cached value is always one of the three terminal states. None of this weakens the tier guarantee above: an upstream failure still cannot blank a base field. It bounds a different property — recency, not presence-on-failure.
    """

    recordLabel: str | None = Field(
        None,
        description="Local-first base field. The catalog record label Backend-Service wrote onto the flowsheet row at play time (`flowsheet.record_label`), read back off the linked flowsheet row — durable BS state, never a Discogs/LML read. Distinct from `label`, which is the Discogs *release* label; the two carry genuinely different values with different provenance and must not be merged, because collapsing them would make the catalog label blankable by an upstream timeout. Omitted in three cases: the key resolved to no linked flowsheet row (a free-text entry that never linked to an `album_id` has no local source for it); the resolved row's `record_label` was null or empty; or the response was served from the 1h memo and the entry was written under either of those conditions — see this schema's freshness caveat, which also covers the stale-value case where a memoized `recordLabel` outlives a librarian's correction.",
    )
    labelId: int | None = Field(
        None,
        description="Local-first base field. Backend-Service's `label` table id for `recordLabel`, read off the same linked flowsheet row (`flowsheet.label_id`). Lets a client join to the catalog label record instead of string-matching the name. Omitted in the same three cases as `recordLabel`: no linked flowsheet row resolved, the resolved row's `label_id` was null, or the response came from the 1h memo of a request where one of those held. Presence is independent of `recordLabel`'s — the handler tests the two columns separately, so a row can supply one without the other.",
    )
    metadataStatus: MetadataStatus | None = Field(
        None,
        description="Local-first base field. A faithful echo of the linked flowsheet row's `metadata_status` column — the same enrichment-lifecycle value the V2 flowsheet feed reports for that row, which is why it shares the `MetadataStatus` schema rather than restating the literals (the two must move together). It reports the row's stored state; the terminal-vs-non-terminal interpretation belongs to the client (WXYC/wxyc-ios-64#685). Omitted only when the key resolved to no linked flowsheet row, or when the response came from the 1h memo of such a request. Unlike `recordLabel` and `labelId`, it is never omitted for an empty column: `flowsheet.metadata_status` is `NOT NULL DEFAULT 'pending'` in Backend-Service, so a linked row always carries a value. A memoized value is additionally always terminal — non-terminal snapshots are excluded from the memo (WXYC/Backend-Service#1893) — so a `pending` or `enriching` reading here is always freshly read, never a stale echo.",
    )
    discogsReleaseId: int | None = Field(None, description="Discogs release ID")
    discogsUrl: str | None = Field(None, description="Discogs release page URL")
    releaseYear: int | None = Field(None, description="Release year from Discogs")
    artworkUrl: str | None = Field(None, description="Album artwork image URL")
    genres: list[str] | None = Field(None, description="Discogs genre classifications")
    styles: list[str] | None = Field(
        None, description="Discogs style classifications (more specific than genres)"
    )
    label: str | None = Field(
        None,
        description="Primary label on the Discogs *release*, from the cached `album_metadata` row or an LML fallthrough. Enriched, not base: absent when the upstream lookup fails, and still null on `album_metadata` rows enriched before WXYC/Backend-Service#1336 (see WXYC/Backend-Service#1442). Not the same field as `recordLabel`, which is the catalog label from the linked flowsheet row and survives an upstream failure.",
    )
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
    """
    Discogs entity type
    """

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
    """
    SSE event emitted on the `live-fs-topic` when bulk state changes (ETL runs, schema migrations) make targeted patches impractical. Consumers respond with a coarse cache invalidation. `payload.source` is a free-text label for telemetry only (`etl`, `migration`, etc.) — clients must not branch on it.

    """

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
    last_heartbeat_at: AwareDatetime | None = Field(...)
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


class FlowsheetEntryFields(BaseModel):
    """
    Every non-identity field of a v1 flowsheet row. Factored out of `FlowsheetEntryResponse` so `FlowsheetRangeEntry` can carry the identical field set while relaxing `show_id` to nullable — the two response shapes must not drift, because iOS V2 decodes both with one decoder (tubafrenzy-decommissioning plan §2.5, consumer #3). Not referenced directly by any path; compose it with an identity block instead.
    """

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


class FlowsheetEntryResponse(FlowsheetEntryBase, FlowsheetEntryFields):
    """
    Flowsheet entry as returned by the API with all possible fields
    """


class FlowsheetRangeEntry(FlowsheetRangeEntryBase, FlowsheetEntryFields):
    """
    A flowsheet row as returned by `GET /flowsheet/range`. Carries the exact field set of `FlowsheetEntryResponse` (both compose `FlowsheetEntryFields`) and differs from it in one respect: `show_id` is nullable. Field parity is a contract requirement, not a coincidence — iOS V2 decodes this endpoint and `GET /flowsheet` with a single decoder (tubafrenzy-decommissioning plan §2.5, consumer #3).
    """


class FlowsheetRangeResponse(BaseModel):
    """
    Response body of `GET /flowsheet/range`. `shows` is a deliberate superset: consumers that only need the entry stream (which carries inline `show_start` / `show_end` markers in `entry_type`) can ignore it.
    """

    shows: list[FlowsheetRangeShow] = Field(
        ...,
        description="Every show overlapping the window, ordered by `start_time` ascending. Empty when the window contains no shows.",
    )
    entries: list[FlowsheetRangeEntry] = Field(
        ...,
        description="Every flowsheet row in the window, ordered by `add_time` ascending and tie-broken on `id` — NOT by `play_order`, which is per-show and interleaves a multi-show window (see the endpoint description). Includes the `show_start` / `show_end` marker rows. Those markers are a convenience, not a guarantee: a show whose `show_end` delivery was dropped has no closing marker (the same failure that leaves `FlowsheetRangeShow.end_time` null), so a consumer that segments purely on markers will run one show's entries into the next. Segment on `show_id` and treat the markers as labels.",
    )


class FlowsheetV2TrackEntry(FlowsheetV2Base):
    """
    V2 track entry - a song that was played
    """

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
        description='Track position on the release (e.g., "A1", "B2", "5", "1-12"). Set by the dj-site flowsheet picker (catalog-track-search plan §5.3 / Track 3) when the DJ selected a track from the resolved release, or supplied directly on a freeform (LML-only) entry; null when the DJ entered a free-text track_title without a tracklist lookup. String-typed to match Discogs\'s `release_track.position`.\n',
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
    artist_id: int | None = Field(
        None,
        description="The WXYC catalog artist id, sharing the `library.artist_id` keyspace. Optional here (never required): it lands ahead of the Backend-Service change that populates it (BS#2227), so a consumer compiled against this field must still tolerate its absence until that deploys.\n",
    )
    legacy_release_id: int | None = Field(
        None,
        description="The library row's surrogate key (BS#1963), NOT NULL in the database since migration 0137. Optional here (never required) because the column is emitted per-projection, not globally: NOT NULL is a claim about the column, while `required` is a promise that the key appears on the wire, and WXYC/Backend-Service#2167 is open precisely because the LML search-proxy rows do not emit it explicitly yet. Promoting this to required once every projection returning the schema provably emits it is worth doing, and needs a per-projection audit rather than a text edit. Matches the CatalogExportRow and BulkResolveInput precedent.\n",
    )
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


class AlbumDetail(BaseModel):
    """
    The album-detail response: one `wxyc_schema.library` row plus its artist / format / genre joins, as `getAlbumFromDB` projects it.
    FOUR operations return exactly this shape — `GET /library/info` (both its `album_id` and its `legacy_release_id` branch), `PATCH /library/{id}`, `PATCH /library/{id}/missing`, and `PATCH /library/{id}/found`. The three PATCHes re-read through `getAlbumFromDB` after their write, so there is one shape here, not two. They previously pointed at `AlbumSearchResult` (#365), which declares seven search-only properties no album-detail handler emits (`album_dist`, `artist_dist`, `rotation_bin`, `rotation_id`, `artwork_url`, `matched_via`, `matched_via_alias`) — do not re-point a detail operation at it.
    Named for the shape, not for one endpoint: this was `AlbumInfoResponse`, which read as "the `/library/info` response" while serving four.
    Deliberately FLAT rather than `allOf: [Album, …]`, though it began that way and every property below `format_id` is also on `Album`. Two reasons. The differ compares `allOf` branches one at a time, so re-pointing the three PATCHes here from the flat `AlbumSearchResult` read as "removed ten required properties" three times over and reddened the breaking-change gate for a change that removes nothing. And the composition was only half true: `Album` is the RAW inserted row `POST /library` returns — snake_case `discogs_unavailable`, no joins, no `reconciled_identity` — while this is the serialized, joined read shape. Sharing a base implied an equivalence the two do not have (see WXYC/Backend-Service#2216).

    """

    id: int = Field(..., description="Backend's serial `wxyc_schema.library.id`.")
    artist_id: int
    album_title: str
    code_number: int
    genre_id: int
    format_id: int
    label: str | None = Field(
        ...,
        description="`library.label`. Nullable in the database; the key is always present because the projection always selects it.\n",
    )
    record_label: str | None = Field(
        None,
        description="The SAME column as `label` — `getAlbumFromDB` projects `library.label` twice, under both names, so both are always present and always equal. Prefer `label`; `record_label` is retained because consumers read it (#373).\n",
    )
    label_id: int | None = None
    add_date: AwareDatetime
    disc_quantity: int | None = None
    alternate_artist_name: str | None = None
    album_artist: str | None = Field(
        None,
        description='Credited album artist for compilations (e.g., "Kruder & Dorfmeister" on a DJ-Kicks release filed under Various Artists).',
    )
    artist_name: str = Field(..., description="Joined from `artists.artist_name`.")
    alphabetical_name: str | None = Field(
        None,
        description='Joined from `artists.alphabetical_name` — the filing form ("Beatles, The"), used for shelf order.\n',
    )
    code_letters: str = Field(..., description="Joined from `artists.code_letters`.")
    code_artist_number: int = Field(
        ...,
        description="Joined from `genre_artist_crossreference.artist_genre_code` — the artist's number *within* this release's genre, so the same artist has a different one per genre. Part of the call number, not an id.\n",
    )
    format_name: str = Field(..., description="Joined from `format.format_name`.")
    genre_name: str = Field(
        ...,
        description="Joined from `genres.genre_name`, a `varchar(64)`. Free-form: `GET /library/genres` is the authoritative enumeration (#367).\n",
    )
    plays: int | None = Field(None, description="Station play count for this release.")
    last_modified: AwareDatetime | None = None
    date_lost: AwareDatetime | None = Field(
        None,
        description="Set when a DJ flags the release missing from the stacks (BS#393). A release is currently missing when `date_lost` is set and `date_found` is either absent or earlier than it.\n",
    )
    date_found: AwareDatetime | None = None
    on_streaming: bool | None = Field(
        None,
        description="Whether the release resolved to a streaming service. `false` is meaningful (checked, absent — the WXYC EXCLUSIVE case); null means never checked.\n",
    )
    legacy_release_id: int | None = Field(
        None,
        description="The library row's surrogate key (BS#1963), NOT NULL in the database since migration 0137. Optional here, never required, matching the `CatalogExportRow` and `BulkResolveInput` precedent.\n",
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
    reconciled_identity: ReconciledIdentity | None = None


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
    """
    A processed Discogs search result with artwork and enriched metadata. Distinct from the raw DiscogsSearchResult which mirrors the Discogs API response directly. Streaming URL fields match the StreamingLinks schema and will be migrated to use $ref in a future version.
    Two distinct states share this shape, told apart by `release_id`: a real Discogs identity has `release_id > 0` with a non-empty `release_url`; the streaming-only sentinel (BS#1185) has `release_id == 0` and `release_url == ""`, signalling "I carry only streaming URLs, there is no Discogs release to link or to fetch album metadata from." A consumer keying "has Discogs identity" on `release_id != 0` therefore handles both states correctly, including the row-less identity described on `LookupResultItem`.

    """

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
    confidence: confloat(ge=0.0, le=1.0) = Field(0, description="Match confidence score")
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
    """
    A single lookup result pairing a library item with optional artwork and reconciled identity.
    `artwork` is optional and independent of `library_item`. This makes the *row-less* shape valid and first-class: `library_item.id == 0` (no WXYC catalog row) paired with a present `artwork` carrying `release_id > 0` and a non-empty `release_url` is a "Discogs identity, no catalog row" result — the album isn't shelved in the library, but Discogs confidently identifies the release, so the link, artwork, and downstream streaming buttons still render. LML emits this shape both from a library-miss Discogs search and from a non-library track carry-through (a confidently-resolved release for a track whose album isn't shelved).
    This is distinct from the `artwork.release_id == 0` / `release_url == ""` streaming-only sentinel (BS#1185), which carries streaming URLs but no linkable Discogs release. The discriminator is `artwork.release_id > 0 && library_item.id == 0`: unambiguous against the sentinel and backward-compatible with any consumer keying "has Discogs identity" on `release_id != 0`.

    """

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
    """
    Response from the lookup service containing library search results and metadata. When the request sets `include_identity: true` and the producer understands the flag, the response is meant to additionally carry `api_version: 2` and the `identity` block (per cross-cache-identity plan §3.2.2). Neither side is implemented in the producer yet, so in production today both fields ship `null` — never an omitted key — on every response, regardless of what the request set `include_identity` to (WXYC/wxyc-shared#316). A consumer MUST test `api_version === 2`, never key presence: absent, `null`, and a producer that hasn't implemented this at all must all read "not supported", and only the literal value `2` reads "supported". See `api_version` below for the full rule, and `LookupIdentityBlock` for `identity`'s.

    """

    api_version: ApiVersion | None = Field(
        None,
        description='Present and equal to 2 only when the request set `include_identity: true` and the producer understands the flag. No `LookupResponse(...)` construction site in the producer sets this field anywhere yet — not even when the request sets `include_identity: true` — and the endpoint is served through FastAPI\'s `response_model` without `response_model_exclude_none`, so it ships `null` — never an omitted key — on every `/lookup` response in production today (WXYC/wxyc-shared#316); `nullable: true` documents that wire instead of describing one nobody emits.\n\nBecause of that, a consumer MUST test for the value `2` and must never test for key presence — `!== undefined` against the generated TypeScript reads TRUE for a producer that implements none of this, the inverse of what the marker exists to signal. Absent, `null`, and a producer that hasn\'t implemented `include_identity` at all must all read "not supported", and only the literal value `2` reads "supported" — which keeps the check immune to whether any given producer omits or nulls its unset optionals.\n',
    )
    results: list[LookupResultItem] = Field([], validate_default=True)
    search_type: SearchType = Field(
        "none",
        description="The search strategy that produced results: direct, fallback, alternative, compilation, song_as_artist, or none\n",
    )
    song_not_found: bool = Field(
        False,
        description="True if search fell back to artist-only (track not confirmed on results)",
    )
    found_on_compilation: bool = Field(
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
    timeout: bool = Field(
        False,
        description="True when LML's server-side hard cap fired and the search pipeline was abandoned mid-execution (LML#370). `results` may be partial or empty in that case. Callers can use this to distinguish \"no match\" (empty `results`, `timeout: false`) from \"ran out of time\" (`results` may be empty, `timeout: true`). The hard cap is an internal LML safety floor independent of the caller's `X-Caller-Budget-Ms` header; see LML#338 / LML#340 / LML#370 for the cascade-budget design. Backend also forwards two sibling informal headers on the same `/lookup` and `/lookup/bulk` requests, both prose-referenced here rather than formal parameters (matching `X-Caller-Budget-Ms`'s own precedent): `X-Caller-Class` (the resolved BS→LML traffic class, an integer 1-5 per Backend-Service's per-caller policy, BS#1826) and `X-Caller-Reason` (the `caller` label string itself, e.g. `proxy-library-search` or `catalog-popularity-freetext-resolve`). Both are sent only when Backend has a registered caller for the request and are otherwise omitted; sending them is inert until LML reads them (LML#928 for `X-Caller-Class`-driven lane routing, LML#931 for `X-Caller-Reason` caller telemetry) — see BS#1843.\n",
    )
    degraded: bool = Field(
        False,
        description="True when LML returned partial or cache-only data because it intentionally shed the enrichment tail — under a caller deadline, admission-control pressure, or an unavailable upstream (LML#930). Distinguishes a degraded/cache-only result from a full success (`degraded: false`) and from a genuine no-match (empty `results`, `degraded: false`, `timeout: false`). Distinct from `timeout`, which signals the internal hard cap fired and the pipeline was abandoned mid-execution; `degraded` is a deliberate shed-the-tail outcome where the returned data is trustworthy but incomplete. Default false, so existing consumers see a byte-identical response.\n",
    )
    degraded_reason: DegradedReason | None = Field(
        None,
        description="Why the response was degraded. Present only when `degraded: true`; omitted otherwise. Non-exhaustive — LML may add reasons in a later minor version. The Swift and Kotlin codegen decode an unknown value into an `unknownDefault` case and TypeScript consumers see a widened string-literal union, but the Python (datamodel-codegen → pydantic) consumers use strict enums and must regenerate against the new `@wxyc/shared` minor before LML emits a newly added reason. `deadline_exceeded` — the caller's budget or LML's spine deadline elapsed and the enrichment tail was skipped; `cache_only` — LML served cached data without refreshing from upstream; `upstream_unavailable` — an upstream (Discogs, streaming providers) was rate limited or down and its contribution was omitted. Set by LML#930's caller-deadline / admission path; read by LML#931 (`degraded-mode-result` telemetry) and wxyc-canary#82.\n",
    )


class BulkResolveLibrariesRequest(BaseModel):
    """
    Bulk identity-resolution request. Order is preserved in the response. Up to 1,000 inputs per call (see endpoint cap).

    """

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
    """
    SSE event emitted on the `live-fs-topic` when an enrichment-worker (BS#892) finalizes the metadata for a flowsheet row. Payload carries the client-facing flowsheet row (the `FlowsheetEntryResponse` fields) so a newly-mounted client (e.g. a /live viewer that just opened the page) can surface the post-enrichment fields (`artwork_url`, `release_year`, ...) without a follow-up GET. The `live-fs-topic` is anonymous, so Backend projects the raw CDC row through its client-facing allow-list (BS#1534) — internal columns (`search_doc`, `composer`, `legacy_*`, ...) do not ride this stream. Provider: Backend-Service/apps/backend/services/metadata-broadcast/metadata-broadcast.ts (`filterMetadataUpdate`). Consumer: dj-site `live-updates-listener.ts`. Contract: `CONTRACTS.LIVE_FS_UPDATE_INCLUDES_FULL_ROW`.

    """

    type: Literal["update"]
    payload: FlowsheetEntryResponse
    timestamp: AwareDatetime


class LiveFsInsertEvent(BaseModel):
    """
    SSE event emitted on the `live-fs-topic` the instant a new flowsheet row is created (BS#1888), before enrichment runs. Payload carries the same client-facing flowsheet row (`FlowsheetEntryResponse`) as `LiveFsUpdateEvent`, but at insert time the row is `metadata_status: 'pending'` with the enrichment fields (`artwork_url`, `release_year`, `discogs_url`, ...) still null — all of which `FlowsheetEntryResponse` already models as nullable, so a not-yet-enriched row is valid. A subscriber appends the row immediately; the enrichment fields arrive seconds later as the existing `LiveFsUpdateEvent`. Same anonymous `live-fs-topic` allow-list projection as `update` (BS#1534) — internal columns (`search_doc`, `composer`, `legacy_*`, ...) do not ride this stream. Removes Epic C's ([WXYC/Backend-Service#877]) polling latency floor: consumers (WXYC/wxyc-ios-64#269, dj-site) learn of a new track on the SSE push instead of the next reconciliation poll.

    """

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
