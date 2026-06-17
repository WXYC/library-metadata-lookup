"""PostgreSQL cache service for Discogs data.

This service provides a local cache of Discogs release data stored in PostgreSQL.
It implements a hybrid cache strategy:
1. Query local DB first
2. On cache miss, caller should query Discogs API
3. Cache API results back to local DB for future queries

The cache uses PostgreSQL's pg_trgm extension for fuzzy text matching.
"""

import asyncio
import hashlib
import logging

from rapidfuzz import fuzz
from wxyc_etl.text import to_match_form as normalize_for_comparison

from discogs.matching import normalize_artist_for_validation, normalize_for_track_comparison
from discogs.memory_cache import async_cached, create_ttl_cache
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    LabelCredit,
    MemberRef,
    ReleaseInfo,
    ReleaseMetadataResponse,
    ReleaseVideo,
    TrackItem,
)

logger = logging.getLogger(__name__)


class CacheUnavailableError(Exception):
    """Raised when the PostgreSQL cache is unreachable."""

    pass


# Mirrors ``_ARTIST_FUZZY_MATCH_THRESHOLD`` in ``discogs/service.py`` — the
# release-level fuzzy fallback used here must score the same way the API path
# does so cache hits and cache misses can't disagree about whether a track is
# on a release. Tied to that constant by intent, not duplicated by accident.
_ARTIST_FUZZY_MATCH_THRESHOLD = 70

# Default TTL for a negative-cache entry (7 days, matching the
# migration's column default). 7 days is conservative: tracks that
# eventually land on Discogs (new releases) shouldn't be pinned negative
# forever, but most rare-artist lookups span >24 h between repeats, so the
# cross-deploy savings outweigh the catch-up lag. Per WXYC/library-
# metadata-lookup#341.
_NEGATIVE_CACHE_DEFAULT_TTL_SECONDS = 604_800

# In-process TTL cache for ``search_artists_by_name``. The trigram scan over
# UNION(artist, artist_name_variation) was the dominant DB chokepoint in prod
# (p50 = 303 ms × ~1k calls/day, ~317 s aggregate DB time per 24 h). The
# resolver pre-pass in ``lookup.orchestrator.resolve_canonical_artist`` now
# only runs when ``LML_RESOLVE_ARTIST_CANONICAL`` is enabled (WXYC/library-
# metadata-lookup#343 Option 2), so the dominant caller is the Phase 1.5
# mojibake-recovery fallback in ``lookup/external_search.py``. The WXYC
# catalog input space is bounded (~30k library artists), so a 2k-entry LRU +
# 1h TTL covers the working set after warmup. Registered with
# ``create_ttl_cache`` so ``clear_all_caches()`` resets it alongside the
# Discogs API memory caches. Per WXYC/library-metadata-lookup#359.
_ARTIST_SEARCH_CACHE = create_ttl_cache(maxsize=2000, ttl=3600)


def _negative_cache_key_hash(artist: str | None, track: str, artist_as_keyword: bool) -> bytes:
    """Hash the (artist, track, artist_as_keyword) tuple into a stable 32-byte key.

    Strings flow through `to_match_form` — the same normalizer used by
    `make_normalized_cache_key` in `discogs/memory_cache.py` (per A5 / LML#272)
    — so diacritic and case variations of the same user-typed query
    collapse to one key. The `artist_as_keyword` boolean is part of the
    key because it picks a different Discogs API call shape
    (`params["q"]` + `format=Compilation` vs. `params["artist"]`), and a
    negative answer on one shape doesn't transfer to the other.

    Returned as bytes for direct binding to the bytea PK column.
    """
    norm_artist = normalize_for_comparison(artist or "")
    norm_track = normalize_for_comparison(track)
    payload = f"{norm_artist}\x1f{norm_track}\x1f{int(bool(artist_as_keyword))}"
    return hashlib.sha256(payload.encode("utf-8")).digest()


class DiscogsCacheService:
    """Service for querying and updating the local Discogs cache.

    This service wraps a PostgreSQL connection pool and provides methods
    for searching and retrieving cached Discogs data.
    """

    def __init__(self, pool):
        """Initialize the cache service with a connection pool.

        Args:
            pool: asyncpg connection pool
        """
        self.pool = pool

    async def is_available(self) -> bool:
        """Check if the cache database is available."""
        try:
            result = await self.pool.fetchval("SELECT 1")
            return bool(result == 1)
        except Exception as e:
            logger.warning(f"Cache health check failed: {e}")
            return False

    async def lookup_negative_hit(
        self, artist: str | None, track: str, artist_as_keyword: bool
    ) -> bool:
        """Return True if a non-expired negative-cache entry exists for this query.

        Used by `discogs/service.py:search_releases_by_track` to short-circuit
        the Discogs API on queries we've already asked and got nothing for.
        TTL is enforced inline by the query (`now() < attempted_at +
        ttl_seconds * interval '1 second'`) so callers don't need to
        re-check expiration.

        Best-effort: any pool error returns False so the caller falls
        through to the API path exactly as if the negative cache said
        "no entry." Errors are logged at warning level.

        Args:
            artist: Artist name (None when LML's `q=` keyword search ran).
            track: Track title.
            artist_as_keyword: True when this corresponds to the API
                `params["q"]` + `format=Compilation` path; False for
                `params["artist"]` field filter.

        Returns:
            True on hit (the API would return empty), False otherwise.
        """
        key_hash = _negative_cache_key_hash(artist, track, artist_as_keyword)
        try:
            row = await self.pool.fetchval(
                """
                SELECT 1
                FROM lookup_negative
                WHERE key_hash = $1
                  AND now() < attempted_at + (ttl_seconds * interval '1 second')
                LIMIT 1
                """,
                key_hash,
            )
            return row is not None
        except Exception as e:
            logger.warning(f"lookup_negative_hit failed (treating as miss): {e}")
            return False

    async def record_lookup_negative(
        self,
        artist: str | None,
        track: str,
        artist_as_keyword: bool,
        ttl_seconds: int = _NEGATIVE_CACHE_DEFAULT_TTL_SECONDS,
    ) -> None:
        """Record a negative verdict so the next process can short-circuit.

        Upserts on conflict so a re-write resets `attempted_at` (the TTL
        clock starts fresh on every confirmation that the answer is still
        "nothing"). Best-effort: pool errors are swallowed at warning
        level — at worst we miss the write and the next request pays the
        API cost again, identical to today's behavior pre-A4.

        Args:
            artist: Artist name (None when LML's `q=` keyword search ran).
            track: Track title.
            artist_as_keyword: Distinguishes the API call shape (see
                `lookup_negative_hit`).
            ttl_seconds: Per-row TTL override. Defaults to 7 days; the
                table column also defaults to 604800 so the override is
                only useful for shorter (e.g. test-driven) windows.
        """
        key_hash = _negative_cache_key_hash(artist, track, artist_as_keyword)
        try:
            await self.pool.execute(
                """
                INSERT INTO lookup_negative
                  (key_hash, artist, track, artist_as_keyword, ttl_seconds)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (key_hash) DO UPDATE SET
                  attempted_at = now(),
                  ttl_seconds = EXCLUDED.ttl_seconds
                """,
                key_hash,
                artist,
                track,
                artist_as_keyword,
                ttl_seconds,
            )
        except Exception as e:
            logger.warning(f"record_lookup_negative failed (best-effort write): {e}")

    async def search_releases_by_track(
        self, track: str, artist: str | None = None, limit: int = 20
    ) -> list[ReleaseInfo]:
        """Search for releases containing a track.

        Uses trigram similarity for fuzzy matching on track title.

        Args:
            track: Track title to search for
            artist: Optional artist name to filter by
            limit: Maximum number of results to return

        Returns:
            List of ReleaseInfo objects

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        # `rta.extra = 0` constrains the LEFT JOIN to main-artist credits
        # (mirrors validate_track_on_release after #333). Unlike
        # validate_track_on_release, this query has no Python-side release-
        # level fuzz fallback — a release where the only matching credit is
        # `extra = 1` (featuring guest, composer, remixer) drops from the
        # candidate ranking. The fallthrough seam's API leg will surface it
        # at the cost of Discogs quota. See #333 for the documented recall
        # trade-off; #472-#475 track the parallel filters in sibling read
        # sites (get_release_metadata, va_disambiguate, match_compilations,
        # track_streaming). Keep the rationale here (not in `--` comments
        # inside the SQL) so it stays out of pg_stat_statements and PG
        # logs.
        try:
            query = """
                WITH matching_tracks AS (
                    SELECT DISTINCT rt.release_id, rt.sequence,
                           rt.title as track_title,
                           similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) as sim
                    FROM release_track rt
                    WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
                    ORDER BY sim DESC
                    LIMIT $2
                )
                SELECT r.id as release_id, r.title, ra.artist_name,
                       mt.track_title,
                       CASE WHEN lower(ra.artist_name) LIKE '%various%' THEN true ELSE false END as is_compilation
                FROM matching_tracks mt
                JOIN release r ON r.id = mt.release_id
                JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                LEFT JOIN release_track_artist rta
                    ON rta.release_id = mt.release_id
                    AND rta.track_sequence = mt.sequence
                    AND rta.extra = 0
                WHERE (
                    $3::text IS NULL
                    OR lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($3))
                    OR lower(f_unaccent(rta.artist_name)) % lower(f_unaccent($3))
                )
                ORDER BY mt.sim DESC
            """

            rows = await self.pool.fetch(query, track, limit * 2, artist)

            results = []
            seen_albums = set()

            for row in rows:
                album = row["title"]
                album_key = album.lower()

                if album_key in seen_albums:
                    continue
                seen_albums.add(album_key)

                results.append(
                    ReleaseInfo(
                        album=album,
                        artist=row["artist_name"],
                        release_id=row["release_id"],
                        release_url=f"https://www.discogs.com/release/{row['release_id']}",
                        is_compilation=row["is_compilation"],
                    )
                )

                if len(results) >= limit:
                    break

            return results

        except Exception as e:
            logger.error(f"Cache search failed: {e}")
            raise CacheUnavailableError(f"Cache search failed: {e}") from e

    @async_cached(_ARTIST_SEARCH_CACHE)
    async def search_artists_by_name(self, name: str, *, limit: int = 5) -> list[dict]:
        """Fuzzy-match an artist name against ``artist`` and ``artist_name_variation``.

        Used by the Phase 1.5 mojibake-recovery fallback in the lookup
        endpoint. Trigram similarity over diacritic-stripped lowercased
        names; the canonical ``artist.name`` is returned even when the
        match comes via ``artist_name_variation``.

        Wrapped in ``_ARTIST_SEARCH_CACHE`` (TTLCache, maxsize=2000, ttl=1h)
        because this query is the dominant DB chokepoint — see WXYC/library-
        metadata-lookup#359. The cache key collapses to ``(name, limit)``
        via the existing ``make_normalized_cache_key`` (case + diacritics
        folded). The 1h TTL is conservative against the monthly discogs-cache
        rebuild; intra-day staleness from incremental updates is acceptable.

        Args:
            name: Artist name (or skeleton) to search for.
            limit: Maximum number of distinct artists to return.

        Returns:
            List of dicts with keys ``id``, ``name``, ``score``.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT id, name, max(score) AS score FROM (
                    SELECT a.id, a.name,
                        similarity(lower(f_unaccent(a.name)), lower(f_unaccent($1))) AS score
                    FROM artist a
                    WHERE lower(f_unaccent(a.name)) % lower(f_unaccent($1))
                    UNION ALL
                    SELECT a.id, a.name,
                        similarity(lower(f_unaccent(anv.name)), lower(f_unaccent($1))) AS score
                    FROM artist a
                    JOIN artist_name_variation anv ON anv.artist_id = a.id
                    WHERE lower(f_unaccent(anv.name)) % lower(f_unaccent($1))
                ) sub
                GROUP BY id, name
                ORDER BY score DESC
                LIMIT $2
            """
            rows = await self.pool.fetch(query, name, limit)
            return [{"id": r["id"], "name": r["name"], "score": float(r["score"])} for r in rows]
        except Exception as e:
            logger.error(f"Cache search_artists_by_name failed: {e}")
            raise CacheUnavailableError(f"Cache search_artists_by_name failed: {e}") from e

    async def search_releases_by_title(self, title: str, *, limit: int = 5) -> list[dict]:
        """Fuzzy-match an album/release title against ``release.title``.

        Used by the Phase 1.7 mojibake-recovery fallback when the lossy
        matcher sends a RELEASE_TITLE skeleton (no artist). Pairs each
        matched release with its primary ``release_artist.artist_name`` so
        the matcher's skeleton scoring has both the canonical title and
        an artist context.

        Args:
            title: Release title (or skeleton) to search for.
            limit: Maximum number of distinct releases to return.

        Returns:
            List of dicts with keys ``id``, ``title``, ``artist``, ``score``.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT id, title, artist, max(score) AS score FROM (
                    SELECT r.id, r.title, ra.artist_name AS artist,
                        similarity(lower(f_unaccent(r.title)), lower(f_unaccent($1))) AS score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    WHERE lower(f_unaccent(r.title)) % lower(f_unaccent($1))
                ) sub
                GROUP BY id, title, artist
                ORDER BY score DESC
                LIMIT $2
            """
            rows = await self.pool.fetch(query, title, limit)
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "score": float(r["score"]),
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"Cache search_releases_by_title failed: {e}")
            raise CacheUnavailableError(f"Cache search_releases_by_title failed: {e}") from e

    async def search_tracks_by_title(self, title: str, *, limit: int = 5) -> list[dict]:
        """Fuzzy-match a track title against ``release_track.title``.

        Used by the Phase 1.7 mojibake-recovery fallback when the lossy
        matcher sends a SONG_TITLE skeleton. Returns the canonical track
        title plus the parent release's primary artist so the matcher has
        an artist context — the lossy bucket is dominated by song titles
        (443 of 815 rows) and library has no song-level FTS.

        Args:
            title: Track title (or skeleton) to search for.
            limit: Maximum number of distinct (track, artist) pairs to return.

        Returns:
            List of dicts with keys ``id`` (release_id), ``title``,
            ``artist``, ``score``.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT release_id AS id, title, artist, max(score) AS score FROM (
                    SELECT rt.release_id, rt.title, ra.artist_name AS artist,
                        similarity(lower(f_unaccent(rt.title)), lower(f_unaccent($1))) AS score
                    FROM release_track rt
                    JOIN release_artist ra ON ra.release_id = rt.release_id AND ra.extra = 0
                    WHERE lower(f_unaccent(rt.title)) % lower(f_unaccent($1))
                ) sub
                GROUP BY release_id, title, artist
                ORDER BY score DESC
                LIMIT $2
            """
            rows = await self.pool.fetch(query, title, limit)
            return [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "artist": r["artist"],
                    "score": float(r["score"]),
                }
                for r in rows
            ]
        except Exception as e:
            logger.error(f"Cache search_tracks_by_title failed: {e}")
            raise CacheUnavailableError(f"Cache search_tracks_by_title failed: {e}") from e

    async def autocomplete_tracks(
        self, artist: str, q: str, *, release: str | None = None, limit: int = 20
    ) -> list[str]:
        """Autocomplete track titles for an artist from the cache.

        Searches the release_track table for track titles matching the prefix,
        filtered by artist. Returns distinct, sorted titles.

        Args:
            artist: Artist name to filter by (required).
            q: Track title prefix to search for.
            release: Optional release title to filter by.
            limit: Maximum number of results after deduplication.

        Returns:
            Sorted list of distinct track titles.

        Raises:
            CacheUnavailableError: If the database is unreachable.
        """
        try:
            query = """
                SELECT rt.title
                FROM release_track rt
                JOIN release_artist ra ON ra.release_id = rt.release_id AND ra.extra = 0
                LEFT JOIN release r ON r.id = rt.release_id
                WHERE lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($1))
                  AND lower(f_unaccent(rt.title)) ILIKE f_unaccent($2) || '%'
                  AND ($3::text IS NULL OR lower(f_unaccent(r.title)) % lower(f_unaccent($3)))
                LIMIT $4
            """

            # Overfetch to allow for dedup
            rows = await self.pool.fetch(query, artist, q, release, limit * 5)

            # Case-insensitive dedup (first occurrence wins), then sort
            seen: set[str] = set()
            titles: list[str] = []
            for row in rows:
                title = row["title"]
                key = title.lower()
                if key not in seen:
                    seen.add(key)
                    titles.append(title)

            titles.sort(key=str.lower)
            return titles[:limit]

        except Exception as e:
            logger.error(f"Cache autocomplete_tracks failed: {e}")
            raise CacheUnavailableError(f"Cache autocomplete_tracks failed: {e}") from e

    async def get_release(self, release_id: int) -> ReleaseMetadataResponse | None:
        """Get full release metadata by ID.

        Args:
            release_id: Discogs release ID

        Returns:
            ReleaseMetadataResponse if found, None if not in cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            release_row = await self.pool.fetchrow(
                "SELECT id, title, release_year, artwork_url, released, "
                "artwork_checked_at, not_found "
                "FROM release WHERE id = $1",
                release_id,
            )

            if release_row is None:
                return None

            # LML#510: tombstone short-circuit. The parent row's `not_found`
            # flag is authoritative — there are no child rows for a
            # tombstone (the tombstone branch in write_release skips the
            # cascade), so the 4-7 PG round-trips below would all be empty
            # selects. Returning a tombstone-shaped model keeps the
            # `is_pg_hit` predicate at the public boundary happy (the
            # `artwork_checked_at` stamp is set, so the seam treats it as
            # a hit) and the public method's tombstone translation
            # converts it back to None for the caller.
            if release_row["not_found"]:
                return ReleaseMetadataResponse(
                    release_id=release_id,
                    title="",
                    artist="",
                    release_url=f"https://www.discogs.com/release/{release_id}",
                    not_found=True,
                    artwork_checked_at=release_row["artwork_checked_at"],
                    cached=True,
                )

            # Fetch all child tables in parallel (independent queries)
            artist_rows, label_rows, track_rows, track_artist_rows = await asyncio.gather(
                self.pool.fetch(
                    """
                    SELECT artist_id, artist_name, extra, role
                    FROM release_artist
                    WHERE release_id = $1
                    ORDER BY extra, artist_name
                    """,
                    release_id,
                ),
                self.pool.fetch(
                    "SELECT label_id, label_name, catno FROM release_label WHERE release_id = $1",
                    release_id,
                ),
                self.pool.fetch(
                    """
                    SELECT position, title, duration, sequence
                    FROM release_track
                    WHERE release_id = $1
                    ORDER BY sequence
                    """,
                    release_id,
                ),
                self.pool.fetch(
                    # `extra = 0` keeps TrackItem.artists tight on main-performer
                    # credits; extras (writer/producer/remixer) live with
                    # `extra = 1` and would otherwise cross-pollinate the
                    # `(artist, track)` validation `_scan_tracklist_for_match`
                    # runs over this list in `discogs/service.py`. Mirrors the
                    # filter `validate_track_on_release` already applies; see
                    # #333 for the precision/recall trade-off and #588 for this
                    # call site.
                    """
                    SELECT track_sequence, artist_name
                    FROM release_track_artist
                    WHERE release_id = $1
                      AND extra = 0
                    ORDER BY track_sequence
                    """,
                    release_id,
                ),
            )

            # Genre/style/video tables may not exist if the pipeline hasn't been re-run
            genre_style_results = await asyncio.gather(
                self.pool.fetch(
                    "SELECT genre FROM release_genre WHERE release_id = $1",
                    release_id,
                ),
                self.pool.fetch(
                    "SELECT style FROM release_style WHERE release_id = $1",
                    release_id,
                ),
                self.pool.fetch(
                    """
                    SELECT sequence, src, title, duration, embed
                    FROM release_video
                    WHERE release_id = $1
                    ORDER BY sequence
                    """,
                    release_id,
                ),
                return_exceptions=True,
            )
            genre_rows = (
                genre_style_results[0]
                if not isinstance(genre_style_results[0], BaseException)
                else []
            )
            style_rows = (
                genre_style_results[1]
                if not isinstance(genre_style_results[1], BaseException)
                else []
            )
            video_rows = (
                genre_style_results[2]
                if not isinstance(genre_style_results[2], BaseException)
                else []
            )

            primary_artist = ""
            primary_artist_id = None
            artist_credits: list[ArtistCredit] = []
            extra_artist_credits: list[ArtistCredit] = []
            for row in artist_rows:
                credit = ArtistCredit(
                    artist_id=row["artist_id"],
                    name=row["artist_name"],
                    role=row["role"],
                )
                if row["extra"] == 0:
                    artist_credits.append(credit)
                    if not primary_artist:
                        primary_artist = row["artist_name"]
                        primary_artist_id = row["artist_id"]
                else:
                    extra_artist_credits.append(credit)

            label_credits = [
                LabelCredit(
                    label_id=row["label_id"],
                    name=row["label_name"],
                    catno=row["catno"],
                )
                for row in label_rows
            ]
            primary_label = label_credits[0].name if label_credits else None
            primary_label_id = label_credits[0].label_id if label_credits else None

            track_artists: dict[int, list[str]] = {}
            for row in track_artist_rows:
                seq = row["track_sequence"]
                if seq not in track_artists:
                    track_artists[seq] = []
                track_artists[seq].append(row["artist_name"])

            tracklist = []
            for row in track_rows:
                seq = row["sequence"]
                tracklist.append(
                    TrackItem(
                        position=row["position"] or "",
                        title=row["title"],
                        duration=row["duration"],
                        artists=track_artists.get(seq, []),
                    )
                )

            videos = [
                ReleaseVideo(
                    src=row["src"],
                    title=row["title"],
                    duration=row["duration"],
                    embed=row["embed"] if row["embed"] is not None else True,
                )
                for row in video_rows
            ]

            return ReleaseMetadataResponse(
                release_id=release_id,
                title=release_row["title"],
                artist=primary_artist,
                artist_id=primary_artist_id,
                year=release_row["release_year"],
                label=primary_label,
                label_id=primary_label_id,
                genres=[row["genre"] for row in genre_rows],
                styles=[row["style"] for row in style_rows],
                artwork_url=release_row["artwork_url"],
                artwork_checked_at=release_row["artwork_checked_at"],
                tracklist=tracklist,
                release_url=f"https://www.discogs.com/release/{release_id}",
                cached=True,
                artists=artist_credits,
                extra_artists=extra_artist_credits,
                labels=label_credits,
                released=release_row["released"],
                videos=videos,
            )

        except Exception as e:
            logger.error(f"Cache get_release failed: {e}")
            raise CacheUnavailableError(f"Cache get_release failed: {e}") from e

    async def write_release(self, release: ReleaseMetadataResponse) -> None:
        """Write or update a release in the cache.

        Args:
            release: Release metadata to cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            # LML#510: tombstone branch. A 404-shaped write doesn't have any
            # rich content to cascade — and crucially, we must NOT clobber
            # any prior hydrated row's identifier columns (title / year /
            # artwork) nor wipe the child cascade. The narrow UPSERT below
            # only touches `not_found` + `artwork_checked_at`; the
            # ``ON CONFLICT DO UPDATE SET`` clause intentionally OMITS
            # title / year / artwork_url / released so a 404 after a 200
            # leaves the parent's identifier columns intact, and the early
            # `return` skips the child DELETE+INSERT cascade so existing
            # `release_artist` / `release_label` / `release_track` /
            # `release_track_artist` / `release_genre` / `release_style` /
            # `release_video` rows survive a tombstone overwrite.
            if release.not_found:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO release (
                            id, title, release_year, artwork_url, released,
                            artwork_checked_at, not_found
                        )
                        VALUES ($1, '', NULL, NULL, NULL, now(), TRUE)
                        ON CONFLICT (id) DO UPDATE SET
                            artwork_checked_at = EXCLUDED.artwork_checked_at,
                            not_found = TRUE
                        """,
                        release.release_id,
                    )
                logger.debug(f"Tombstoned release {release.release_id}")
                return

            async with self.pool.acquire() as conn, conn.transaction():
                # Wrap the entire DELETE+INSERT cascade in a single transaction.
                # Without this, asyncpg autocommits each statement and a
                # cancellation mid-cascade leaves an artworked release with
                # empty release_artist / release_track / etc., while
                # cache_metadata.cached_at advances to "fresh". See WXYC#375.

                # Upsert release row. `artwork_checked_at` is stamped to
                # `now()` on every write — `write_release` is only called
                # from the live-Discogs-API path (via the fallthrough seam),
                # so by definition we just asked Discogs about this row's
                # artwork. The downstream `is_pg_hit` predicate in
                # `discogs/service.py:get_release` reads this column to
                # avoid re-fetching genuinely-imageless releases
                # (WXYC/library-metadata-lookup#423, backed by the schema
                # column from WXYC/discogs-etl#239).
                #
                # `not_found = FALSE` in the SET clause (LML#510) clears any
                # prior tombstone in one statement so a recovered 200 makes
                # the row reachable again without an admin intervention.
                await conn.execute(
                    """
                    INSERT INTO release (
                        id, title, release_year, artwork_url, released,
                        artwork_checked_at, not_found
                    )
                    VALUES ($1, $2, $3, $4, $5, now(), FALSE)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        release_year = EXCLUDED.release_year,
                        artwork_url = EXCLUDED.artwork_url,
                        released = EXCLUDED.released,
                        artwork_checked_at = EXCLUDED.artwork_checked_at,
                        not_found = FALSE
                    """,
                    release.release_id,
                    release.title,
                    release.year,
                    release.artwork_url,
                    release.released,
                )

                # Delete + re-insert all artists (clean replacement)
                await conn.execute(
                    "DELETE FROM release_artist WHERE release_id = $1",
                    release.release_id,
                )

                artist_data = []
                # Main artists (extra=0)
                if release.artists:
                    for a in release.artists:
                        artist_data.append((release.release_id, a.artist_id, a.name, 0, a.role))
                elif release.artist:
                    # Backward compat: fall back to scalar artist
                    artist_data.append(
                        (release.release_id, release.artist_id, release.artist, 0, None)
                    )
                # Extra artists (extra=1)
                for a in release.extra_artists or []:
                    artist_data.append((release.release_id, a.artist_id, a.name, 1, a.role))

                if artist_data:
                    await conn.executemany(
                        """
                        INSERT INTO release_artist
                            (release_id, artist_id, artist_name, extra, role)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        artist_data,
                    )

                # Delete + re-insert labels
                await conn.execute(
                    "DELETE FROM release_label WHERE release_id = $1",
                    release.release_id,
                )

                if release.labels:
                    label_data = [
                        (release.release_id, lbl.label_id, lbl.name, lbl.catno)
                        for lbl in release.labels
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO release_label (release_id, label_id, label_name, catno)
                        VALUES ($1, $2, $3, $4)
                        """,
                        label_data,
                    )

                # Delete + re-insert genres (table may not exist yet).
                # Nested conn.transaction() creates a SAVEPOINT inside the outer
                # transaction, so a missing-table error reverts only this block
                # without aborting the outer transaction.
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM release_genre WHERE release_id = $1",
                            release.release_id,
                        )
                        if release.genres:
                            await conn.executemany(
                                "INSERT INTO release_genre (release_id, genre) VALUES ($1, $2)",
                                [(release.release_id, g) for g in release.genres],
                            )
                except Exception:
                    pass

                # Delete + re-insert styles (table may not exist yet)
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM release_style WHERE release_id = $1",
                            release.release_id,
                        )
                        if release.styles:
                            await conn.executemany(
                                "INSERT INTO release_style (release_id, style) VALUES ($1, $2)",
                                [(release.release_id, s) for s in release.styles],
                            )
                except Exception:
                    pass

                # Delete + re-insert videos (table may not exist yet)
                try:
                    async with conn.transaction():
                        await conn.execute(
                            "DELETE FROM release_video WHERE release_id = $1",
                            release.release_id,
                        )
                        if release.videos:
                            await conn.executemany(
                                """
                                INSERT INTO release_video
                                    (release_id, sequence, src, title, duration, embed)
                                VALUES ($1, $2, $3, $4, $5, $6)
                                """,
                                [
                                    (
                                        release.release_id,
                                        i + 1,
                                        v.src,
                                        v.title,
                                        v.duration,
                                        v.embed,
                                    )
                                    for i, v in enumerate(release.videos)
                                ],
                            )
                except Exception:
                    pass

                # Delete + re-insert tracks
                await conn.execute(
                    "DELETE FROM release_track WHERE release_id = $1",
                    release.release_id,
                )

                if release.tracklist:
                    track_data = [
                        (release.release_id, i + 1, t.position, t.title, t.duration)
                        for i, t in enumerate(release.tracklist)
                    ]
                    await conn.executemany(
                        """
                        INSERT INTO release_track (release_id, sequence, position, title, duration)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        track_data,
                    )

                    track_artist_data = []
                    for i, t in enumerate(release.tracklist or []):
                        for artist in t.artists or []:
                            track_artist_data.append((release.release_id, i + 1, artist))

                    if track_artist_data:
                        await conn.executemany(
                            """
                            INSERT INTO release_track_artist
                                (release_id, track_sequence, artist_name)
                            VALUES ($1, $2, $3)
                            ON CONFLICT DO NOTHING
                            """,
                            track_artist_data,
                        )

                await conn.execute(
                    """
                    INSERT INTO cache_metadata (release_id, source)
                    VALUES ($1, 'api_fetch')
                    ON CONFLICT (release_id) DO UPDATE SET
                        cached_at = now(),
                        source = 'api_fetch'
                    """,
                    release.release_id,
                )

                logger.debug(f"Cached release {release.release_id}: {release.title}")

        except Exception as e:
            logger.error(f"Cache write_release failed: {e}")
            raise CacheUnavailableError(f"Cache write_release failed: {e}") from e

    async def search_releases(
        self, artist: str | None = None, album: str | None = None, limit: int = 5
    ) -> list[dict]:
        """Search for releases by artist and/or album title.

        Uses trigram similarity for fuzzy matching.

        Args:
            artist: Artist name to search for
            album: Album/release title to search for
            limit: Maximum number of results to return

        Returns:
            List of dicts with keys: release_id, title, artist_name, artwork_url

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        if not artist and not album:
            return []

        try:
            if artist and album:
                query = """
                    SELECT DISTINCT ON (r.id)
                        r.id as release_id, r.title, ra.artist_name, r.artwork_url,
                        (similarity(lower(f_unaccent(r.title)), lower(f_unaccent($1)))
                         + similarity(lower(f_unaccent(ra.artist_name)), lower(f_unaccent($2)))) / 2.0 as score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    WHERE lower(f_unaccent(r.title)) % lower(f_unaccent($1))
                      AND lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($2))
                    ORDER BY r.id, score DESC
                """
                query = f"""
                    SELECT * FROM ({query}) sub
                    ORDER BY score DESC
                    LIMIT $3
                """
                rows = await self.pool.fetch(query, album, artist, limit * 2)
            elif artist:
                query = """
                    SELECT DISTINCT ON (r.id)
                        r.id as release_id, r.title, ra.artist_name, r.artwork_url,
                        similarity(lower(f_unaccent(ra.artist_name)), lower(f_unaccent($1))) as score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    WHERE lower(f_unaccent(ra.artist_name)) % lower(f_unaccent($1))
                    ORDER BY r.id, score DESC
                """
                query = f"""
                    SELECT * FROM ({query}) sub
                    ORDER BY score DESC
                    LIMIT $2
                """
                rows = await self.pool.fetch(query, artist, limit * 2)
            else:  # album only
                query = """
                    SELECT DISTINCT ON (r.id)
                        r.id as release_id, r.title, ra.artist_name, r.artwork_url,
                        similarity(lower(f_unaccent(r.title)), lower(f_unaccent($1))) as score
                    FROM release r
                    JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
                    WHERE lower(f_unaccent(r.title)) % lower(f_unaccent($1))
                    ORDER BY r.id, score DESC
                """
                query = f"""
                    SELECT * FROM ({query}) sub
                    ORDER BY score DESC
                    LIMIT $2
                """
                rows = await self.pool.fetch(query, album, limit * 2)

            results = []
            seen_titles = set()
            for row in rows:
                title_key = row["title"].lower()
                if title_key in seen_titles:
                    continue
                seen_titles.add(title_key)

                results.append(
                    {
                        "release_id": row["release_id"],
                        "title": row["title"],
                        "artist_name": row["artist_name"],
                        "artwork_url": row["artwork_url"],
                    }
                )

                if len(results) >= limit:
                    break

            return results

        except Exception as e:
            logger.error(f"Cache search_releases failed: {e}")
            raise CacheUnavailableError(f"Cache search_releases failed: {e}") from e

    async def get_artist_details(self, artist_id: int) -> ArtistDetails | None:
        """Get full artist details by ID.

        Args:
            artist_id: Discogs artist ID

        Returns:
            ArtistDetails if found, None if not in cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            # `fetched_at` is projected so the service-layer `is_pg_hit`
            # predicate can distinguish stub rows (rebuild-created, never
            # hydrated from Discogs) from rows we've actually fetched. See
            # `DiscogsService.get_artist_details` and WXYC#502.
            #
            # `not_found` (LML#510) is the tombstone discriminator — see
            # the short-circuit just below.
            artist_row = await self.pool.fetchrow(
                "SELECT id, name, profile, image_url, fetched_at, not_found "
                "FROM artist WHERE id = $1",
                artist_id,
            )

            if artist_row is None:
                return None

            # LML#510: tombstone short-circuit. Same rationale as the
            # release path — no child rows exist for a tombstone, so
            # the asyncio.gather below would just be empty selects.
            if artist_row["not_found"]:
                return ArtistDetails(
                    artist_id=artist_id,
                    name="",
                    not_found=True,
                    fetched_at=artist_row["fetched_at"],
                    cached=True,
                )

            # Fetch all child tables in parallel (independent queries)
            alias_rows, nv_rows, member_rows, url_rows = await asyncio.gather(
                self.pool.fetch(
                    "SELECT alias_id, alias_name FROM artist_alias WHERE artist_id = $1",
                    artist_id,
                ),
                self.pool.fetch(
                    "SELECT name FROM artist_name_variation WHERE artist_id = $1",
                    artist_id,
                ),
                self.pool.fetch(
                    "SELECT member_id, member_name, active FROM artist_member WHERE artist_id = $1",
                    artist_id,
                ),
                self.pool.fetch(
                    "SELECT url FROM artist_url WHERE artist_id = $1",
                    artist_id,
                ),
            )

            return ArtistDetails(
                artist_id=artist_row["id"],
                name=artist_row["name"],
                profile=artist_row["profile"],
                image_url=artist_row["image_url"],
                aliases=[
                    ArtistRef(id=r["alias_id"], name=r["alias_name"])
                    for r in alias_rows
                    if r["alias_id"] is not None
                ],
                name_variations=[r["name"] for r in nv_rows],
                members=[
                    MemberRef(id=r["member_id"], name=r["member_name"], active=r["active"])
                    for r in member_rows
                ],
                urls=[r["url"] for r in url_rows],
                fetched_at=artist_row["fetched_at"],
                cached=True,
            )

        except Exception as e:
            logger.error(f"Cache get_artist_details failed: {e}")
            raise CacheUnavailableError(f"Cache get_artist_details failed: {e}") from e

    async def get_artist_details_bulk(self, artist_ids: list[int]) -> dict[int, ArtistDetails]:
        """Batched cache-only read of full artist details across many ids.

        Used by the artist-search-alias bulk endpoint (PR 2, Phase 2). 4
        PG round-trips per call regardless of input cardinality — one per
        relevant table (`artist`, `artist_alias`, `artist_name_variation`,
        `artist_member`) — each binding the input list via
        `WHERE ... = ANY($1)`. All three child tables have a B-tree index
        on `artist_id`, so each leg stays index-driven even at the per-
        batch ceiling of ~125 ids.

        Returns a dict keyed by Discogs artist id with `urls=[]` — the
        `artist_url` table is NOT queried because the artist-search-alias
        composer doesn't read `details.urls`, and skipping the fetch
        saves one PG round-trip per call. Extend with a kwarg if a
        future caller needs URLs.

        Cache-miss ids are absent from the returned dict (never
        None-valued). Empty input returns an empty dict without querying.

        Cache-only — no Discogs API escalation, no rate-limit semaphore
        acquire. Callers wanting the API fall-through should still use
        `get_artist_details` (per-id with cache → API cascade).
        """
        if not artist_ids:
            return {}

        try:
            (
                artist_rows,
                alias_rows,
                nv_rows,
                member_rows,
            ) = await asyncio.gather(
                self.pool.fetch(
                    # `fetched_at` is projected so the bulk path can carry
                    # the LML#503 stub-vs-hydrated discriminator on the
                    # returned `ArtistDetails`, matching the singular
                    # `get_artist_details` shape. Bulk stays cache-only
                    # by design (LML#503) — this is information-level
                    # parity, not an API-escalation change.
                    "SELECT id, name, profile, image_url, fetched_at "
                    "FROM artist WHERE id = ANY($1::int[])",
                    artist_ids,
                ),
                self.pool.fetch(
                    "SELECT artist_id, alias_id, alias_name "
                    "FROM artist_alias WHERE artist_id = ANY($1::int[])",
                    artist_ids,
                ),
                self.pool.fetch(
                    "SELECT artist_id, name "
                    "FROM artist_name_variation WHERE artist_id = ANY($1::int[])",
                    artist_ids,
                ),
                self.pool.fetch(
                    "SELECT artist_id, member_id, member_name, active "
                    "FROM artist_member WHERE artist_id = ANY($1::int[])",
                    artist_ids,
                ),
            )

            # Bucket child rows by artist_id once, then assemble.
            aliases_by_id: dict[int, list[ArtistRef]] = {}
            for row in alias_rows:
                if row["alias_id"] is None:
                    continue
                aliases_by_id.setdefault(row["artist_id"], []).append(
                    ArtistRef(id=row["alias_id"], name=row["alias_name"])
                )
            nvs_by_id: dict[int, list[str]] = {}
            for row in nv_rows:
                nvs_by_id.setdefault(row["artist_id"], []).append(row["name"])
            members_by_id: dict[int, list[MemberRef]] = {}
            for row in member_rows:
                members_by_id.setdefault(row["artist_id"], []).append(
                    MemberRef(
                        id=row["member_id"],
                        name=row["member_name"],
                        active=row["active"],
                    )
                )
            result: dict[int, ArtistDetails] = {}
            for row in artist_rows:
                artist_id = row["id"]
                result[artist_id] = ArtistDetails(
                    artist_id=artist_id,
                    name=row["name"],
                    profile=row["profile"],
                    image_url=row["image_url"],
                    aliases=aliases_by_id.get(artist_id, []),
                    name_variations=nvs_by_id.get(artist_id, []),
                    members=members_by_id.get(artist_id, []),
                    urls=[],
                    fetched_at=row["fetched_at"],
                    cached=True,
                )
            return result

        except Exception as e:
            logger.error(f"Cache get_artist_details_bulk failed: {e}")
            raise CacheUnavailableError(f"Cache get_artist_details_bulk failed: {e}") from e

    async def write_artist_details(self, details: ArtistDetails) -> None:
        """Write or update artist details in the cache.

        Args:
            details: Artist details to cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            # LML#510: tombstone branch. Same shape as write_release —
            # narrow UPSERT, early return before the child cascade so a
            # 404 doesn't wipe rich catalog metadata; `ON CONFLICT DO
            # UPDATE SET` intentionally omits name / profile / image_url
            # so a 404 after a 200 leaves identifier columns intact.
            if details.not_found:
                async with self.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO artist (id, name, profile, image_url, fetched_at, not_found)
                        VALUES ($1, '', NULL, NULL, now(), TRUE)
                        ON CONFLICT (id) DO UPDATE SET
                            fetched_at = now(),
                            not_found = TRUE
                        """,
                        details.artist_id,
                    )
                logger.debug(f"Tombstoned artist {details.artist_id}")
                return

            async with self.pool.acquire() as conn, conn.transaction():
                # Wrap the UPSERT + 4×(DELETE+INSERT) cascade in a single
                # transaction so a cancellation mid-write doesn't leave the
                # artist row updated with empty child tables. See WXYC#375.

                # Upsert artist row.
                #
                # `not_found = FALSE` in the SET clause (LML#510) clears
                # any prior tombstone in one statement, so a recovered 200
                # makes the row reachable again without admin intervention.
                await conn.execute(
                    """
                    INSERT INTO artist (id, name, profile, image_url, fetched_at, not_found)
                    VALUES ($1, $2, $3, $4, now(), FALSE)
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        profile = EXCLUDED.profile,
                        image_url = EXCLUDED.image_url,
                        fetched_at = now(),
                        not_found = FALSE
                    """,
                    details.artist_id,
                    details.name,
                    details.profile,
                    details.image_url,
                )

                # Delete + re-insert child tables
                await conn.execute(
                    "DELETE FROM artist_alias WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.aliases:
                    await conn.executemany(
                        """
                        INSERT INTO artist_alias (artist_id, alias_id, alias_name)
                        VALUES ($1, $2, $3)
                        """,
                        [(details.artist_id, a.id, a.name) for a in details.aliases],
                    )

                await conn.execute(
                    "DELETE FROM artist_name_variation WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.name_variations:
                    await conn.executemany(
                        """
                        INSERT INTO artist_name_variation (artist_id, name)
                        VALUES ($1, $2)
                        """,
                        [(details.artist_id, nv) for nv in details.name_variations],
                    )

                await conn.execute(
                    "DELETE FROM artist_member WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.members:
                    await conn.executemany(
                        """
                        INSERT INTO artist_member (artist_id, member_id, member_name, active)
                        VALUES ($1, $2, $3, $4)
                        """,
                        [(details.artist_id, m.id, m.name, m.active) for m in details.members],
                    )

                await conn.execute(
                    "DELETE FROM artist_url WHERE artist_id = $1",
                    details.artist_id,
                )
                if details.urls:
                    await conn.executemany(
                        """
                        INSERT INTO artist_url (artist_id, url)
                        VALUES ($1, $2)
                        """,
                        [(details.artist_id, url) for url in details.urls],
                    )

                logger.debug(f"Cached artist {details.artist_id}: {details.name}")

        except Exception as e:
            logger.error(f"Cache write_artist_details failed: {e}")
            raise CacheUnavailableError(f"Cache write_artist_details failed: {e}") from e

    async def delete_tombstone(self, entity_type: str, entity_id: int) -> str:
        """Delete a tombstoned row so the next API call re-fetches it.

        Backs the admin recovery endpoint (`DELETE /admin/discogs/tombstone/...`).
        The `WHERE id = $1 AND not_found = TRUE` guard means a real row can
        never be deleted through this surface — even with a typo'd id.

        Args:
            entity_type: `"release"` or `"artist"`.
            entity_id: Discogs id.

        Returns:
            ``"deleted"`` — a tombstoned row matched and was removed.
            ``"exists_not_tombstone"`` — `entity_id` exists but
                ``not_found = FALSE``; refusing to delete (real data).
            ``"not_found"`` — no row at all.
        """
        if entity_type not in ("release", "artist"):
            raise ValueError(f"entity_type must be 'release' or 'artist', got {entity_type!r}")
        table = entity_type
        try:
            async with self.pool.acquire() as conn:
                # Probe the row's existence and tombstone state in one query
                # so the response code is unambiguous. A separate DELETE …
                # RETURNING would conflate "no row" and "row not tombstone".
                row = await conn.fetchrow(
                    f"SELECT not_found FROM {table} WHERE id = $1",
                    entity_id,
                )
                if row is None:
                    return "not_found"
                if not row["not_found"]:
                    return "exists_not_tombstone"
                await conn.execute(
                    f"DELETE FROM {table} WHERE id = $1 AND not_found = TRUE",
                    entity_id,
                )
            logger.info("Tombstone deleted: %s/%s", entity_type, entity_id)
            return "deleted"
        except Exception as e:
            logger.error("Cache delete_tombstone failed: %s", e)
            raise CacheUnavailableError(f"Cache delete_tombstone failed: {e}") from e

    async def validate_track_on_release(
        self, release_id: int, track: str, artist: str
    ) -> bool | None:
        """Validate that a track by an artist exists on a release.

        Uses lightweight queries instead of fetching the full release metadata.
        Only retrieves tracks, track artists, and the primary release artist —
        skipping labels, extra artists, and release metadata that validation
        doesn't need.

        Args:
            release_id: Discogs release ID
            track: Track title to find
            artist: Artist name to find

        Returns:
            True if track by artist found, False if not found, None if release not cached

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            exists = await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM release WHERE id = $1)", release_id
            )
            if not exists:
                return None  # Cache miss - caller should try API

            # Fetch only what validation needs (tracks + track artists + primary artist)
            track_rows, track_artist_rows, release_artist_row = await asyncio.gather(
                self.pool.fetch(
                    "SELECT sequence, title FROM release_track WHERE release_id = $1",
                    release_id,
                ),
                self.pool.fetch(
                    # `extra = 0` keeps the read tight on main-artist credits;
                    # extra credits (writer/producer/performer) live with
                    # `extra = 1` and would otherwise cross-pollinate a
                    # precision match here. The release-level fallback below
                    # remains as defense-in-depth for legitimate misses.
                    "SELECT track_sequence, artist_name FROM release_track_artist "
                    "WHERE release_id = $1 AND extra = 0",
                    release_id,
                ),
                self.pool.fetchrow(
                    "SELECT artist_name FROM release_artist WHERE release_id = $1 AND extra = 0 LIMIT 1",
                    release_id,
                ),
            )

            # Build track_artists lookup
            track_artists: dict[int, list[str]] = {}
            for row in track_artist_rows:
                seq = row["track_sequence"]
                if seq not in track_artists:
                    track_artists[seq] = []
                track_artists[seq].append(row["artist_name"])

            primary_artist = release_artist_row["artist_name"] if release_artist_row else ""

            track_lower = normalize_for_track_comparison(track)
            artist_lower = normalize_artist_for_validation(artist)

            release_artist_clean = normalize_artist_for_validation(primary_artist)

            for row in track_rows:
                item_title = normalize_for_track_comparison(row["title"])

                if track_lower not in item_title and item_title not in track_lower:
                    continue

                seq = row["sequence"]
                artists_for_track = track_artists.get(seq, [])
                if artists_for_track:
                    for track_artist in artists_for_track:
                        track_artist_lower = normalize_artist_for_validation(track_artist)
                        if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                            return True
                    joined = normalize_artist_for_validation(" ".join(artists_for_track))
                    if (
                        joined
                        and fuzz.token_set_ratio(artist_lower, joined)
                        >= _ARTIST_FUZZY_MATCH_THRESHOLD
                    ):
                        return True

                # Release-level fallback, consulted whenever the per-track
                # main credits (post-#333 ``extra = 0`` filter) didn't
                # close the match — either no per-track main credit exists
                # for this row at all, or one exists but didn't substring/
                # fuzz-match the requested artist. Rescued by a
                # release-level credit that does match. The band-member
                # shape (per-track main credits list only members, release-
                # level is the band) is one canonical case; see #333
                # acceptance criteria. Mirrors the API path in
                # ``discogs/service.py``. Do not remove without replacing
                # the rescue path.
                if release_artist_clean and (
                    artist_lower in release_artist_clean or release_artist_clean in artist_lower
                ):
                    return True
                if (
                    release_artist_clean
                    and fuzz.token_set_ratio(artist_lower, release_artist_clean)
                    >= _ARTIST_FUZZY_MATCH_THRESHOLD
                ):
                    return True

            return False

        except Exception as e:
            logger.error(f"Cache validate_track_on_release failed: {e}")
            raise CacheUnavailableError(f"Cache validate_track_on_release failed: {e}") from e
