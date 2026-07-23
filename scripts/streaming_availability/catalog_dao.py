"""PG-backed streaming-catalog DAO — the ``ResultsDB`` rewrite (LML#842 PR B).

``StreamingCatalogDAO`` reproduces the ``results_db.ResultsDB`` method surface
over the PR A row-level schema (``lml_cache.streaming_album`` +
``streaming_album_service`` + ``streaming_track_result`` +
``streaming_coverage_baseline``): callers that go through ResultsDB *methods*
port by swapping the construction site, not their call sites — except the
demotion-exposed calls the *Upsert discipline* bullet names (the validate
phase's ``false_positive`` write and the album pipeline's two
``not_found`` miss writes), which PR D must rewrite. Callers that
reach past the method surface into ``results_db._db`` raw SQL or
``_write_lock`` get no such promise — the streaming pipeline's
compilation-skip and ``--retry-misses`` bulk updates, the track pipeline's
extract/build-index/validate scans and album rollup, and the bandcamp
pipeline's slug-discovery writes (found-slug + searched-not-found
sentinel), Wikidata slug loader, and URL migration all bypass the methods
today, and PR D rewrites those sites against DAO methods (adding any
that are missing) rather than swapping a constructor. The load-bearing
translation decisions:

* **Anti-join pending semantics.** The legacy SQLite schema materialized a
  ``{service}_status = 'pending'`` column per album at insert time; the PG
  schema materializes NO service rows until a probe writes one. Every pending
  scan, stats bucket, and wide pivot column therefore coalesces row absence to
  ``'pending'`` — inserts stay single-table and services can be added without
  backfilling fan-out rows.
* **Wide legacy row shape.** Reads go through one shared four-way pivot
  (``_WIDE_ALBUM_SELECT``) emitting the legacy column names (``spotify_status``,
  ``spotify_id``, ``apple_url``, ``bandcamp_slug``, …), so row consumers written
  against ``SELECT * FROM albums`` keep working unchanged. ``library_ids`` /
  ``formats`` come back as JSON strings (asyncpg's default jsonb codec), which
  matches the callers' existing ``json.loads``.
* **Upsert discipline (plan D3).** Service writes are ``INSERT … ON CONFLICT
  (album_id, service) DO UPDATE`` with ``COALESCE(EXCLUDED.x, existing.x)`` on
  every metadata column: a follow-up write with omitted params never nulls
  collected data at the DAO layer, and the PR A no-regress guards backstop
  anything that slips past (the DAO is a tripwire, not a pre-masker — guard
  errors and CHECK violations propagate to the caller unmasked). The one
  deliberate demotion the legacy surface performs — the validate phase's
  ``api_match`` → ``false_positive`` flip — goes through
  ``mark_track_false_positive``, which opts into the guard
  transaction-locally instead of asking callers to speak GUCs;
  ``update_track_resolution`` rejects ``'false_positive'`` outright, so the
  legacy validate-phase demotion call fails loudly at port time instead of
  tripping the trigger mid-drain. That static tripwire is track-side only:
  the legacy album pipeline's miss handlers
  (``scripts/streaming_availability/__main__.py``) also demote through the
  method surface — a deezer miss writes ``update_result(album_id,
  "spotify", "not_found")`` cross-service ("not digitally distributed"),
  and a spotify-stage miss writes the same status for spotify itself —
  and either write can land on a row already ``'found'`` for spotify,
  because the pipeline admits every deezer-pending album regardless of
  spotify status. What mints those rows is the ``--retry-misses`` reset
  (same file): it flips ``deezer_status`` ``not_found``→``pending``
  unconditionally while its spotify reset only covers
  ``spotify_status='not_found' AND deezer_status='pending'``, so every
  spotify-found/deezer-not_found album (29,861 of 65,147 in the 2026-04
  snapshot) re-enters the deezer stage with spotify still ``'found'``.
  ``'not_found'`` is usually legal, so no static rejection is possible
  there; PR D must rewrite the two miss-handler call sites as conditional
  writes (skip services already resolved) and port ``--retry-misses`` in
  the same change, or the first retried drain trips the
  found→``not_found`` no-regress trigger per re-missed row.
* **Loud service validation.** Unknown or unsupported service tokens raise
  ``ValueError`` before any I/O (the legacy DAO silently no-opped on unknown
  services in ``update_result``). Validation precedes pool access so a typo'd
  token in an offline drain dies at the call site without dialing PG.
* **Deterministic batching.** Every list read carries an explicit ``ORDER BY``
  (the legacy code leaned on SQLite's implicit rowid order, which PG does not
  provide), so paged drains are stable across runs.
* **No write lock.** The SQLite DAO serialized writers behind an asyncio lock
  because the whole file was a single-writer resource; PG handles concurrent
  writers natively, so no such lock exists here.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from entity.sources import PgSource
from entity.streaming_catalog import ALLOW_URL_REMOVAL_GUC, set_up_streaming_catalog_schema
from scripts.streaming_availability.dedup import DeduplicatedAlbum

# Legacy service token -> canonical `_SERVICES` slug (the value stored in
# ``streaming_album_service.service``). The legacy surface says "apple"; the
# schema's CHECK list says "apple_music".
_SERVICE_ALIASES = {"apple": "apple_music"}

# Canonical slug -> SQL join alias in ``_WIDE_ALBUM_SELECT``. The alias doubles
# as the legacy wide-column prefix, which is why apple_music's alias is
# "apple". Only these four services have a legacy pivot column set; canonical
# slugs outside this map (e.g. "tidal") are rejected until a caller needs them.
_PIVOT_ALIAS = {
    "spotify": "spotify",
    "apple_music": "apple",
    "deezer": "deezer",
    "bandcamp": "bandcamp",
}

# Services ``update_result`` may write. Bandcamp is excluded on purpose: its
# slug/url lifecycle has dedicated methods, and routing it through
# ``update_result`` was never legal in the legacy DAO either (a silent no-op
# there; a loud ValueError here).
_UPDATE_RESULT_SERVICES = frozenset({"spotify", "apple_music", "deezer"})

# The one shared services pivot: album columns plus each legacy service's
# column set, with row absence coalesced to 'pending'. Callers append WHERE /
# ORDER BY / LIMIT clauses (predicates reference the join aliases directly —
# e.g. ``COALESCE(spotify.status, 'pending') = 'pending'`` is the anti-join
# pending scan).
_WIDE_ALBUM_SELECT = """
    SELECT
        a.id,
        a.normalized_artist,
        a.normalized_title,
        a.display_artist,
        a.display_title,
        a.library_ids,
        a.formats,
        a.genre,
        a.label,
        a.is_compilation,
        a.is_single,
        a.discogs_artist,
        a.discogs_title,
        a.discogs_release_id,
        a.discogs_status,
        COALESCE(deezer.status, 'pending') AS deezer_status,
        deezer.url AS deezer_url,
        deezer.confidence AS deezer_confidence,
        deezer.matched_artist AS deezer_matched_artist,
        deezer.matched_title AS deezer_matched_title,
        deezer.checked_at AS deezer_checked_at,
        COALESCE(spotify.status, 'pending') AS spotify_status,
        spotify.url AS spotify_url,
        spotify.service_item_id AS spotify_id,
        spotify.confidence AS spotify_confidence,
        spotify.matched_artist AS spotify_matched_artist,
        spotify.matched_title AS spotify_matched_title,
        spotify.checked_at AS spotify_checked_at,
        COALESCE(apple.status, 'pending') AS apple_status,
        apple.url AS apple_url,
        apple.confidence AS apple_confidence,
        apple.matched_artist AS apple_matched_artist,
        apple.matched_title AS apple_matched_title,
        apple.checked_at AS apple_checked_at,
        COALESCE(bandcamp.status, 'pending') AS bandcamp_status,
        bandcamp.slug AS bandcamp_slug,
        bandcamp.url AS bandcamp_url,
        bandcamp.checked_at AS bandcamp_checked_at,
        a.created_at
    FROM lml_cache.streaming_album AS a
    LEFT JOIN lml_cache.streaming_album_service AS deezer
        ON deezer.album_id = a.id AND deezer.service = 'deezer'
    LEFT JOIN lml_cache.streaming_album_service AS spotify
        ON spotify.album_id = a.id AND spotify.service = 'spotify'
    LEFT JOIN lml_cache.streaming_album_service AS apple
        ON apple.album_id = a.id AND apple.service = 'apple_music'
    LEFT JOIN lml_cache.streaming_album_service AS bandcamp
        ON bandcamp.album_id = a.id AND bandcamp.service = 'bandcamp'
"""

# Set-based inserts: one statement per batch (columnar arrays + unnest), not
# one round-trip per row. The PR C full-catalog seed pushes ~65k albums through
# insert_albums against the remote discogs-cache PG, where a per-row loop pays
# tens of minutes in pure RTT. ON CONFLICT DO NOTHING arbitrates duplicates
# both against existing rows and within the batch itself, so interrupted runs
# stay idempotent; RETURNING id preserves the new-row-count contract.
_INSERT_ALBUMS_BULK = """
    INSERT INTO lml_cache.streaming_album (
        normalized_artist, normalized_title, display_artist, display_title,
        library_ids, formats, genre, label, is_compilation, is_single
    )
    SELECT * FROM unnest(
        $1::text[], $2::text[], $3::text[], $4::text[],
        $5::jsonb[], $6::jsonb[], $7::text[], $8::text[],
        $9::boolean[], $10::boolean[]
    )
    ON CONFLICT (normalized_artist, normalized_title) DO NOTHING
    RETURNING id
"""

_INSERT_TRACKS_BULK = """
    INSERT INTO lml_cache.streaming_track_result (
        album_id, artist, title, position, source, source_type
    )
    SELECT * FROM unnest(
        $1::bigint[], $2::text[], $3::text[], $4::text[], $5::text[], $6::text[]
    )
    ON CONFLICT (album_id, artist, title) DO NOTHING
    RETURNING id
"""

_UPSERT_SERVICE_RESULT = """
    INSERT INTO lml_cache.streaming_album_service (
        album_id, service, status, url, service_item_id,
        confidence, matched_artist, matched_title, checked_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
    ON CONFLICT (album_id, service) DO UPDATE SET
        status = EXCLUDED.status,
        url = COALESCE(EXCLUDED.url, streaming_album_service.url),
        service_item_id = COALESCE(
            EXCLUDED.service_item_id, streaming_album_service.service_item_id
        ),
        confidence = COALESCE(EXCLUDED.confidence, streaming_album_service.confidence),
        matched_artist = COALESCE(
            EXCLUDED.matched_artist, streaming_album_service.matched_artist
        ),
        matched_title = COALESCE(EXCLUDED.matched_title, streaming_album_service.matched_title),
        checked_at = EXCLUDED.checked_at
"""

_SERVICE_STATS = """
    SELECT COALESCE(s.status, 'pending') AS status, COUNT(*) AS n
    FROM lml_cache.streaming_album AS a
    LEFT JOIN lml_cache.streaming_album_service AS s
        ON s.album_id = a.id AND s.service = $1
    GROUP BY 1
"""

_UPSERT_BASELINE = """
    INSERT INTO lml_cache.streaming_coverage_baseline (metric, value, updated_at)
    VALUES ($1, $2, now())
    ON CONFLICT (metric) DO UPDATE SET
        value = EXCLUDED.value,
        updated_at = EXCLUDED.updated_at
"""


def _resolve_pivot_service(service: str) -> str:
    """Map a legacy service token to its wide-pivot join alias, or raise."""
    canonical = _SERVICE_ALIASES.get(service, service)
    alias = _PIVOT_ALIAS.get(canonical)
    if alias is None:
        raise ValueError(
            f"unknown streaming service {service!r}: expected one of "
            "spotify/apple/deezer/bandcamp or the 'discogs' pseudo-service"
        )
    return alias


class StreamingCatalogDAO:
    """Async DAO over the ``lml_cache`` streaming-catalog tables.

    Construction mirrors ``PgSource``: exactly one of ``dsn`` (offline
    scripts — the DAO owns a lazily-created pool) or ``pool`` (app paths
    borrowing the shared discogs-cache pool; ``close()`` is then a no-op so
    teardown here never closes the pool under a live sibling service).
    """

    def __init__(self, dsn: str | None = None, *, pool: asyncpg.Pool | None = None) -> None:
        self._pg = PgSource(dsn, pool=pool)

    async def connect(self) -> None:
        """Bootstrap the catalog schema (idempotent; shared with the lifespan)."""
        await set_up_streaming_catalog_schema(self._pg)

    async def close(self) -> None:
        await self._pg.close()

    # -- albums ----------------------------------------------------------

    async def insert_albums(self, albums: list[DeduplicatedAlbum]) -> int:
        """Insert deduplicated albums, ignoring already-known ones; return the new-row count.

        One set-based statement per call (see ``_INSERT_ALBUMS_BULK``); an
        empty batch returns 0 without touching PG.
        """
        if not albums:
            return 0
        rows = await self._pg.fetchall(
            _INSERT_ALBUMS_BULK,
            [a.normalized_artist for a in albums],
            [a.normalized_title for a in albums],
            [a.display_artist for a in albums],
            [a.display_title for a in albums],
            [json.dumps(a.library_ids) for a in albums],
            [json.dumps(a.formats) for a in albums],
            [a.genre for a in albums],
            [a.label for a in albums],
            [a.is_compilation for a in albums],
            [a.is_single for a in albums],
        )
        return len(rows)

    async def get_pending(self, service: str, limit: int = 100) -> list[dict[str, Any]]:
        """Albums still pending for ``service`` (or the ``discogs`` pseudo-service).

        ``discogs`` reads the album-table status column and — legacy contract —
        applies NO compilation filter (the orchestrator's discogs branch scans
        everything; ``get_pending_discogs`` is the filtered variant).
        """
        if service == "discogs":
            return await self._pg.fetchall(
                _WIDE_ALBUM_SELECT + " WHERE a.discogs_status = 'pending' ORDER BY a.id LIMIT $1",
                limit,
            )
        alias = _resolve_pivot_service(service)
        return await self._pg.fetchall(
            _WIDE_ALBUM_SELECT
            + f" WHERE COALESCE({alias}.status, 'pending') = 'pending' ORDER BY a.id LIMIT $1",
            limit,
        )

    async def update_result(
        self,
        album_id: int,
        service: str,
        status: str,
        *,
        url: str | None = None,
        spotify_id: str | None = None,
        confidence: float | None = None,
        matched_artist: str | None = None,
        matched_title: str | None = None,
    ) -> None:
        """Upsert one probe result onto ``(album_id, service)``.

        An upsert, not an UPDATE: under the anti-join model the target row
        usually does not exist when the first probe lands. NULL params never
        overwrite populated metadata (plan D3); schema CHECKs and no-regress
        guard errors propagate unmasked.
        """
        canonical = _SERVICE_ALIASES.get(service, service)
        if canonical == "bandcamp":
            raise ValueError(
                "bandcamp results go through update_bandcamp_url / "
                "mark_bandcamp_not_found, not update_result"
            )
        if canonical not in _UPDATE_RESULT_SERVICES:
            raise ValueError(
                f"unknown streaming service {service!r}: update_result accepts spotify/apple/deezer"
            )
        await self._pg.execute(
            _UPSERT_SERVICE_RESULT,
            album_id,
            canonical,
            status,
            url,
            spotify_id,
            confidence,
            matched_artist,
            matched_title,
        )

    # -- discogs enrichment -----------------------------------------------

    async def get_pending_discogs(self, limit: int = 100) -> list[dict[str, Any]]:
        """Non-compilation albums whose Discogs enrichment is still pending."""
        return await self._pg.fetchall(
            _WIDE_ALBUM_SELECT
            + " WHERE a.discogs_status = 'pending' AND NOT a.is_compilation"
            + " ORDER BY a.id LIMIT $1",
            limit,
        )

    async def update_discogs_result(
        self,
        album_id: int,
        status: str,
        *,
        artist: str | None = None,
        title: str | None = None,
        release_id: int | None = None,
    ) -> None:
        """Record a Discogs match attempt; omitted metadata never nulls prior finds."""
        await self._pg.execute(
            """
            UPDATE lml_cache.streaming_album SET
                discogs_status = $2,
                discogs_artist = COALESCE($3, discogs_artist),
                discogs_title = COALESCE($4, discogs_title),
                discogs_release_id = COALESCE($5, discogs_release_id)
            WHERE id = $1
            """,
            album_id,
            status,
            artist,
            title,
            release_id,
        )

    # -- cascade scans ------------------------------------------------------

    async def get_deezer_hits_pending_spotify(self, limit: int = 100) -> list[dict[str, Any]]:
        """Deezer-found albums whose Spotify probe has not run yet."""
        return await self._pg.fetchall(
            _WIDE_ALBUM_SELECT
            + " WHERE deezer.status = 'found' AND COALESCE(spotify.status, 'pending') = 'pending'"
            + " ORDER BY a.id LIMIT $1",
            limit,
        )

    async def get_spotify_misses_pending_apple(self, limit: int = 100) -> list[dict[str, Any]]:
        """Spotify-missed albums whose Apple Music probe has not run yet."""
        return await self._pg.fetchall(
            _WIDE_ALBUM_SELECT
            + " WHERE spotify.status = 'not_found'"
            + " AND COALESCE(apple.status, 'pending') = 'pending'"
            + " ORDER BY a.id LIMIT $1",
            limit,
        )

    # -- bandcamp -----------------------------------------------------------

    async def update_bandcamp_slug(self, album_id: int, slug: str) -> None:
        """Attach a bandcamp artist slug; the lookup status stays/defaults to pending."""
        await self._pg.execute(
            """
            INSERT INTO lml_cache.streaming_album_service (album_id, service, slug)
            VALUES ($1, 'bandcamp', $2)
            ON CONFLICT (album_id, service) DO UPDATE SET slug = EXCLUDED.slug
            """,
            album_id,
            slug,
        )

    async def update_bandcamp_url(self, album_id: int, url: str) -> None:
        """Record a confirmed bandcamp album URL (slug untouched)."""
        await self._pg.execute(
            """
            INSERT INTO lml_cache.streaming_album_service (album_id, service, status, url, checked_at)
            VALUES ($1, 'bandcamp', 'found', $2, now())
            ON CONFLICT (album_id, service) DO UPDATE SET
                status = 'found',
                url = EXCLUDED.url,
                checked_at = EXCLUDED.checked_at
            """,
            album_id,
            url,
        )

    async def mark_bandcamp_not_found(self, album_id: int) -> None:
        """Durably mark 'tried, no match' so re-runs skip already-attempted slugs (#661)."""
        await self._pg.execute(
            """
            INSERT INTO lml_cache.streaming_album_service (album_id, service, status, checked_at)
            VALUES ($1, 'bandcamp', 'not_found', now())
            ON CONFLICT (album_id, service) DO UPDATE SET
                status = 'not_found',
                checked_at = EXCLUDED.checked_at
            """,
            album_id,
        )

    async def get_artists_without_bandcamp_slug(
        self, *, not_on_streaming_only: bool = True, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Distinct non-compilation artists with no bandcamp slug yet.

        Default scope narrows to spotify-missed albums (the not-on-streaming
        report population the slug hunt targets); ``not_on_streaming_only=False``
        widens to every slugless artist.
        """
        query = """
            SELECT DISTINCT a.display_artist
            FROM lml_cache.streaming_album AS a
            LEFT JOIN lml_cache.streaming_album_service AS bandcamp
                ON bandcamp.album_id = a.id AND bandcamp.service = 'bandcamp'
            LEFT JOIN lml_cache.streaming_album_service AS spotify
                ON spotify.album_id = a.id AND spotify.service = 'spotify'
            WHERE bandcamp.slug IS NULL AND NOT a.is_compilation
        """
        params: list[Any] = []
        if not_on_streaming_only:
            query += " AND spotify.status = 'not_found'"
        query += " ORDER BY a.display_artist"
        if limit is not None:
            params.append(limit)
            query += f" LIMIT ${len(params)}"
        return await self._pg.fetchall(query, *params)

    async def get_pending_bandcamp_lookup(
        self, limit: int | None = None, slug: str | None = None
    ) -> list[dict[str, Any]]:
        """Slugged, unresolved, non-compilation albums awaiting a bandcamp page fetch."""
        query = _WIDE_ALBUM_SELECT + (
            " WHERE bandcamp.slug IS NOT NULL AND bandcamp.slug <> ''"
            " AND bandcamp.url IS NULL AND bandcamp.status = 'pending'"
            " AND NOT a.is_compilation"
        )
        params: list[Any] = []
        if slug is not None:
            params.append(slug)
            query += f" AND bandcamp.slug = ${len(params)}"
        query += " ORDER BY a.id"
        if limit is not None:
            params.append(limit)
            query += f" LIMIT ${len(params)}"
        return await self._pg.fetchall(query, *params)

    # -- stats & reports ------------------------------------------------------

    async def get_stats(self) -> dict[str, Any]:
        """Per-service status histograms plus the album total (legacy key set)."""
        stats: dict[str, Any] = {}
        async with self._pg.acquire() as conn:
            for legacy, canonical in (
                ("spotify", "spotify"),
                ("apple", "apple_music"),
                ("deezer", "deezer"),
                ("bandcamp", "bandcamp"),
            ):
                rows = await conn.fetch(_SERVICE_STATS, canonical)
                stats[legacy] = {row["status"]: row["n"] for row in rows}
            discogs_rows = await conn.fetch(
                "SELECT discogs_status AS status, COUNT(*) AS n"
                " FROM lml_cache.streaming_album GROUP BY 1"
            )
            stats["discogs"] = {row["status"]: row["n"] for row in discogs_rows}
            stats["total"] = await conn.fetchval("SELECT COUNT(*) FROM lml_cache.streaming_album")
        return stats

    async def get_not_on_streaming(self) -> list[dict[str, Any]]:
        """Non-compilation albums Spotify definitively missed (the report population)."""
        return await self._pg.fetchall(
            _WIDE_ALBUM_SELECT
            + " WHERE spotify.status = 'not_found' AND NOT a.is_compilation"
            + " ORDER BY a.display_artist, a.display_title"
        )

    async def get_all_results(self) -> list[dict[str, Any]]:
        """Every album in the wide legacy shape, ordered for stable exports."""
        return await self._pg.fetchall(
            _WIDE_ALBUM_SELECT + " ORDER BY a.display_artist, a.display_title"
        )

    # -- tracks ---------------------------------------------------------------

    async def insert_tracks(self, tracks: list[dict[str, Any]]) -> int:
        """Insert track rows, ignoring already-known ones; return the new-row count.

        One set-based statement per call (see ``_INSERT_TRACKS_BULK``); an
        empty batch returns 0 without touching PG.
        """
        if not tracks:
            return 0
        rows = await self._pg.fetchall(
            _INSERT_TRACKS_BULK,
            [t["album_id"] for t in tracks],
            [t["artist"] for t in tracks],
            [t["title"] for t in tracks],
            [t.get("position") for t in tracks],
            [t["source"] for t in tracks],
            [t["source_type"] for t in tracks],
        )
        return len(rows)

    async def get_pending_tracks(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._pg.fetchall(
            "SELECT * FROM lml_cache.streaming_track_result"
            " WHERE resolution_status = 'pending' ORDER BY id LIMIT $1",
            limit,
        )

    async def get_local_miss_tracks(self, limit: int = 100) -> list[dict[str, Any]]:
        return await self._pg.fetchall(
            "SELECT * FROM lml_cache.streaming_track_result"
            " WHERE resolution_status = 'local_miss' ORDER BY id LIMIT $1",
            limit,
        )

    async def update_track_resolution(
        self,
        track_id: int,
        status: str,
        *,
        resolved_via: str | None = None,
        resolved_album_id: int | None = None,
        resolved_release_id: int | None = None,
        spotify_url: str | None = None,
        spotify_confidence: float | None = None,
        deezer_url: str | None = None,
        deezer_confidence: float | None = None,
    ) -> None:
        """Record a track-resolution attempt; omitted params never null prior finds.

        Rejects ``status='false_positive'`` before any I/O: that is the
        guarded track demotion, sanctioned only via
        ``mark_track_false_positive``. The legacy method accepted it (the
        validate phase's demotion call is a site PR D must rewrite, not
        merely re-point); accepting it here would let the PR A trigger kill
        a drain mid-phase with a RaiseError instead of failing loudly at
        the call site. The album pipeline's ``not_found`` miss writes
        carry the same trigger exposure but admit no static tripwire —
        see the module docstring's *Upsert discipline* bullet.
        """
        if status == "false_positive":
            raise ValueError(
                "status 'false_positive' is the guarded demotion — route it through "
                "mark_track_false_positive, which opts into the no-regress guard "
                "transaction-locally"
            )
        await self._pg.execute(
            """
            UPDATE lml_cache.streaming_track_result SET
                resolution_status = $2,
                resolved_via = COALESCE($3, resolved_via),
                resolved_album_id = COALESCE($4, resolved_album_id),
                resolved_release_id = COALESCE($5, resolved_release_id),
                spotify_url = COALESCE($6, spotify_url),
                spotify_confidence = COALESCE($7, spotify_confidence),
                deezer_url = COALESCE($8, deezer_url),
                deezer_confidence = COALESCE($9, deezer_confidence),
                checked_at = now()
            WHERE id = $1
            """,
            track_id,
            status,
            resolved_via,
            resolved_album_id,
            resolved_release_id,
            spotify_url,
            spotify_confidence,
            deezer_url,
            deezer_confidence,
        )

    async def mark_track_false_positive(self, track_id: int) -> None:
        """Demote a resolved track to ``false_positive`` (the validate phase).

        The one deliberate status demotion in the legacy surface: URL
        validation flips ``api_match`` tracks whose URLs fail service-metadata
        checks. The PR A no-regress guard blocks demotions by default, so this
        method opts in via ``set_config(..., is_local => true)`` inside its own
        transaction — the opt-in covers exactly this UPDATE and evaporates at
        COMMIT, never leaking to other statements on the pooled connection.

        Collected URLs and resolution metadata stay in place as the audit
        trail. That is a deliberate improvement over the legacy demotion, NOT
        parity: the legacy ``update_track_resolution`` wrote every metadata
        column from its None-defaulted params, so a legacy ``false_positive``
        flip nulled the URLs, confidences, and provenance it was invalidating.
        Retention is safe because nothing downstream reads URLs off
        ``false_positive`` rows — the streaming-links export and the coverage
        baseline both filter on ``resolution_status IN ('local_match',
        'api_match')``.
        """
        async with self._pg.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config($1, 'on', true)", ALLOW_URL_REMOVAL_GUC)
                await conn.execute(
                    "UPDATE lml_cache.streaming_track_result SET"
                    " resolution_status = 'false_positive', checked_at = now()"
                    " WHERE id = $1",
                    track_id,
                )

    async def get_track_stats(self) -> dict[str, int]:
        """Track counts by resolution status plus their total (legacy shape)."""
        rows = await self._pg.fetchall(
            "SELECT resolution_status AS status, COUNT(*) AS n"
            " FROM lml_cache.streaming_track_result GROUP BY 1"
        )
        stats: dict[str, int] = {row["status"]: row["n"] for row in rows}
        stats["total"] = sum(row["n"] for row in rows)
        return stats

    async def get_album_track_summary(self, album_id: int) -> dict[str, Any]:
        """Per-album track rollup with the legacy resolved-by-subtraction arithmetic."""
        rows = await self._pg.fetchall(
            "SELECT resolution_status AS status, COUNT(*) AS n"
            " FROM lml_cache.streaming_track_result WHERE album_id = $1 GROUP BY 1",
            album_id,
        )
        counts = {row["status"]: row["n"] for row in rows}
        total = sum(counts.values())
        pending = counts.get("pending", 0)
        not_found = counts.get("not_found", 0)
        resolved = (
            total
            - pending
            - counts.get("local_miss", 0)
            - not_found
            - counts.get("false_positive", 0)
            - counts.get("error", 0)
        )
        return {
            "total": total,
            "resolved": resolved,
            "not_found": not_found,
            "pending": pending,
            "on_streaming": resolved > 0,
        }

    # -- coverage baseline -----------------------------------------------------

    async def refresh_coverage_baseline(self) -> dict[str, int]:
        """Recompute and upsert the coverage floors from live catalog counts.

        The five metric keys mirror ``routers/admin.py``'s
        ``_STREAMING_COVERAGE_METRICS`` exactly — that endpoint is the reader
        this table feeds post-cutover (plan PR F). Runs in one transaction so a
        partial failure never publishes a half-refreshed floor set; the PR A
        lowering guard fires through unmasked if live counts ever regress.
        """
        async with self._pg.acquire() as conn:
            async with conn.transaction():
                albums = await conn.fetchval("SELECT COUNT(*) FROM lml_cache.streaming_album")
                url_rows = await conn.fetch(
                    "SELECT service, COUNT(*) AS n FROM lml_cache.streaming_album_service"
                    " WHERE url IS NOT NULL GROUP BY service"
                )
                url_counts = {row["service"]: row["n"] for row in url_rows}
                track_results = await conn.fetchval(
                    "SELECT COUNT(*) FROM lml_cache.streaming_track_result"
                    " WHERE resolution_status IN ('local_match', 'api_match')"
                    " AND (spotify_url IS NOT NULL OR deezer_url IS NOT NULL)"
                )
                metrics: dict[str, int] = {
                    "apple_url": url_counts.get("apple_music", 0),
                    "spotify_url": url_counts.get("spotify", 0),
                    "deezer_url": url_counts.get("deezer", 0),
                    "albums": albums,
                    "track_results": track_results,
                }
                for metric, value in metrics.items():
                    await conn.execute(_UPSERT_BASELINE, metric, value)
        return metrics
