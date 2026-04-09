"""PostgreSQL cache service for Discogs data.

This service provides a local cache of Discogs release data stored in PostgreSQL.
It implements a hybrid cache strategy:
1. Query local DB first
2. On cache miss, caller should query Discogs API
3. Cache API results back to local DB for future queries

The cache uses PostgreSQL's pg_trgm extension for fuzzy text matching.
"""

import asyncio
import logging

from core.matching import normalize_for_track_comparison, strip_discogs_suffix
from discogs.models import (
    ArtistCredit,
    ArtistDetails,
    ArtistRef,
    LabelCredit,
    MemberRef,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
)

logger = logging.getLogger(__name__)


class CacheUnavailableError(Exception):
    """Raised when the PostgreSQL cache is unreachable."""

    pass


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
                "SELECT id, title, release_year, artwork_url, released FROM release WHERE id = $1",
                release_id,
            )

            if release_row is None:
                return None

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
                    """
                    SELECT track_sequence, artist_name
                    FROM release_track_artist
                    WHERE release_id = $1
                    ORDER BY track_sequence
                    """,
                    release_id,
                ),
            )

            # Genre/style tables may not exist if the pipeline hasn't been re-run
            genre_style_results = await asyncio.gather(
                self.pool.fetch(
                    "SELECT genre FROM release_genre WHERE release_id = $1",
                    release_id,
                ),
                self.pool.fetch(
                    "SELECT style FROM release_style WHERE release_id = $1",
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
                tracklist=tracklist,
                release_url=f"https://www.discogs.com/release/{release_id}",
                cached=True,
                artists=artist_credits,
                extra_artists=extra_artist_credits,
                labels=label_credits,
                released=release_row["released"],
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
            async with self.pool.acquire() as conn:
                # Upsert release row (including released date)
                await conn.execute(
                    """
                    INSERT INTO release (id, title, release_year, artwork_url, released)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (id) DO UPDATE SET
                        title = EXCLUDED.title,
                        release_year = EXCLUDED.release_year,
                        artwork_url = EXCLUDED.artwork_url,
                        released = EXCLUDED.released
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
                for a in release.extra_artists:
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

                # Delete + re-insert genres (table may not exist yet)
                try:
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
                    for i, t in enumerate(release.tracklist):
                        for artist in t.artists:
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
            artist_row = await self.pool.fetchrow(
                "SELECT id, name, profile, image_url FROM artist WHERE id = $1",
                artist_id,
            )

            if artist_row is None:
                return None

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
                cached=True,
            )

        except Exception as e:
            logger.error(f"Cache get_artist_details failed: {e}")
            raise CacheUnavailableError(f"Cache get_artist_details failed: {e}") from e

    async def write_artist_details(self, details: ArtistDetails) -> None:
        """Write or update artist details in the cache.

        Args:
            details: Artist details to cache

        Raises:
            CacheUnavailableError: If database is unreachable
        """
        try:
            async with self.pool.acquire() as conn:
                # Upsert artist row
                await conn.execute(
                    """
                    INSERT INTO artist (id, name, profile, image_url, fetched_at)
                    VALUES ($1, $2, $3, $4, now())
                    ON CONFLICT (id) DO UPDATE SET
                        name = EXCLUDED.name,
                        profile = EXCLUDED.profile,
                        image_url = EXCLUDED.image_url,
                        fetched_at = now()
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
                    "SELECT track_sequence, artist_name FROM release_track_artist WHERE release_id = $1",
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
            artist_lower = artist.lower().replace('"', "").replace("'", "")

            for row in track_rows:
                item_title = normalize_for_track_comparison(row["title"])

                if track_lower not in item_title and item_title not in track_lower:
                    continue

                seq = row["sequence"]
                artists_for_track = track_artists.get(seq, [])
                if artists_for_track:
                    for track_artist in artists_for_track:
                        track_artist_lower = track_artist.lower().replace('"', "").replace("'", "")
                        track_artist_lower = strip_discogs_suffix(track_artist_lower)
                        if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                            return True
                else:
                    release_artist_clean = primary_artist.lower().replace('"', "").replace("'", "")
                    release_artist_clean = strip_discogs_suffix(release_artist_clean)
                    if artist_lower in release_artist_clean or release_artist_clean in artist_lower:
                        return True

            return False

        except Exception as e:
            logger.error(f"Cache validate_track_on_release failed: {e}")
            raise CacheUnavailableError(f"Cache validate_track_on_release failed: {e}") from e
