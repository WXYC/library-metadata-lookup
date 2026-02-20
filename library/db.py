import logging
import re
from pathlib import Path

import aiosqlite
from rapidfuzz import fuzz

from core.matching import STOPWORDS, normalize_for_comparison
from library.models import LibraryItem

logger = logging.getLogger(__name__)

# Default path to SQLite database (relative to project root)
DEFAULT_DB_PATH = Path(__file__).parent.parent / "library.db"


class LibraryDB:
    """Async SQLite client for library catalog searches."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._conn: aiosqlite.Connection | None = None
        self._has_alternate_artist: bool = False

    async def connect(self):
        """Open database connection."""
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Library database not found at {self.db_path}. "
                "Run 'python scripts/export_to_sqlite.py' to create it."
            )

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # Detect whether library.db has the alternate_artist_name column
        cursor = await self._conn.execute("PRAGMA table_info(library)")
        columns = await cursor.fetchall()
        column_names = {row[1] for row in columns}
        self._has_alternate_artist = "alternate_artist_name" in column_names

        logger.info(
            f"Connected to SQLite database: {self.db_path} "
            f"(alternate_artist_name: {'yes' if self._has_alternate_artist else 'no'})"
        )

    async def is_available(self) -> bool:
        """Check if the database connection is alive."""
        try:
            if self._conn is None:
                return False
            async with self._conn.execute("SELECT 1") as cursor:
                row = await cursor.fetchone()
                return row is not None
        except Exception:
            return False

    async def close(self):
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Closed SQLite connection")

    def _select_columns(self, prefix: str = "") -> str:
        """Build the SELECT column list, conditionally including alternate_artist_name."""
        p = f"{prefix}." if prefix else ""
        cols = (
            f"{p}id, {p}title, {p}artist, {p}call_letters, "
            f"{p}artist_call_number, {p}release_call_number, {p}genre, {p}format"
        )
        if self._has_alternate_artist:
            cols += f", {p}alternate_artist_name"
        return cols

    async def search(
        self,
        query: str | None = None,
        artist: str | None = None,
        title: str | None = None,
        limit: int = 10,
        fallback_to_like: bool = True,
        fallback_to_fuzzy: bool = True,
    ) -> list[LibraryItem]:
        """
        Search the library catalog.

        Args:
            query: Full-text search across artist and title
            artist: Filter by artist name (partial match)
            title: Filter by title (partial match)
            limit: Max results to return
            fallback_to_like: If True and FTS query returns no results, try LIKE search on individual words
            fallback_to_fuzzy: If True and LIKE search returns no results, try fuzzy matching

        Returns:
            List of matching LibraryItems
        """
        if not self._conn:
            raise RuntimeError("Database not connected")

        if query:
            # Full-text search using FTS5
            sql = f"""
                SELECT {self._select_columns("l")}
                FROM library l
                JOIN library_fts fts ON l.id = fts.rowid
                WHERE library_fts MATCH ?
                LIMIT ?
            """
            try:
                cursor = await self._conn.execute(sql, (query, limit))
                rows = await cursor.fetchall()

                # If no results and fallback enabled, try LIKE search
                if not rows and fallback_to_like:
                    logger.info(
                        f"FTS search for '{query}' returned no results, trying LIKE fallback"
                    )
                    rows = await self._fallback_like_search(query, limit)

                # If still no results, try fuzzy search
                if not rows and fallback_to_fuzzy:
                    logger.info(
                        f"LIKE search for '{query}' returned no results, trying fuzzy fallback"
                    )
                    return await self._fuzzy_search(query, limit)
            except Exception as e:
                # FTS syntax errors (e.g., special characters) - fall back to LIKE
                if fallback_to_like:
                    logger.info(f"FTS search for '{query}' failed ({e}), trying LIKE fallback")
                    rows = await self._fallback_like_search(query, limit)

                    # If still no results, try fuzzy search
                    if not rows and fallback_to_fuzzy:
                        logger.info(
                            f"LIKE search for '{query}' returned no results, trying fuzzy fallback"
                        )
                        return await self._fuzzy_search(query, limit)
                else:
                    raise

            # Return results from FTS or fallback search
            return [LibraryItem(**dict(row)) for row in rows]

        elif artist or title:
            # Filtered search
            conditions: list[str] = []
            params: list[str | int] = []
            if artist:
                if self._has_alternate_artist:
                    conditions.append("(artist LIKE ? OR alternate_artist_name LIKE ?)")
                    params.extend([f"%{artist}%", f"%{artist}%"])
                else:
                    conditions.append("artist LIKE ?")
                    params.append(f"%{artist}%")
            if title:
                conditions.append("title LIKE ?")
                params.append(f"%{title}%")
            params.append(limit)

            sql = f"""
                SELECT {self._select_columns()}
                FROM library
                WHERE {" AND ".join(conditions)}
                LIMIT ?
            """
            cursor = await self._conn.execute(sql, params)
            rows = await cursor.fetchall()

        else:
            return []

        return [LibraryItem(**dict(row)) for row in rows]

    async def _fallback_like_search(self, query: str, limit: int) -> list[aiosqlite.Row]:
        """
        Fallback search using LIKE when FTS fails.
        Splits query into words and searches for titles/artists containing all words.
        """
        # Strip diacritics first, then remove remaining special chars
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalize_for_comparison(query))
        words = normalized.split()

        # Remove stopwords that might cause mismatches
        significant_words = [w for w in words if w not in STOPWORDS and len(w) > 1]

        # If we removed all words, use original words
        if not significant_words:
            significant_words = [w for w in words if len(w) > 1]

        if not significant_words:
            return []

        # Build LIKE conditions for each word
        conditions: list[str] = []
        params: list[str | int] = []
        for word in significant_words:
            if self._has_alternate_artist:
                conditions.append("(title LIKE ? OR artist LIKE ? OR alternate_artist_name LIKE ?)")
                params.extend([f"%{word}%", f"%{word}%", f"%{word}%"])
            else:
                conditions.append("(title LIKE ? OR artist LIKE ?)")
                params.extend([f"%{word}%", f"%{word}%"])

        params.append(limit)

        sql = f"""
            SELECT {self._select_columns()}
            FROM library
            WHERE {" AND ".join(conditions)}
            LIMIT ?
        """

        assert self._conn is not None, "Database not connected. Call connect() first."
        cursor = await self._conn.execute(sql, params)
        rows = await cursor.fetchall()
        return list(rows)

    async def _fuzzy_search(self, query: str, limit: int, threshold: int = 70) -> list[LibraryItem]:
        """
        Fuzzy search fallback using rapidfuzz for typo tolerance.

        Args:
            query: Search query
            limit: Max results to return
            threshold: Minimum fuzzy match score (0-100) to include results
        """
        # Strip diacritics first, then remove remaining special chars
        normalized = re.sub(r"[^a-z0-9\s]", " ", normalize_for_comparison(query))
        words = normalized.split()

        if not words:
            return []

        # Get the longest word to use for candidate search (more selective)
        search_word = max(words, key=len)

        # Search for candidates using partial match on longest word
        prefix = search_word[:3] if len(search_word) >= 3 else search_word

        if self._has_alternate_artist:
            sql = f"""
                SELECT {self._select_columns()}
                FROM library
                WHERE artist LIKE ? OR title LIKE ? OR alternate_artist_name LIKE ?
                LIMIT 500
            """
            fuzzy_params: tuple[str, ...] = (f"%{prefix}%", f"%{prefix}%", f"%{prefix}%")
        else:
            sql = f"""
                SELECT {self._select_columns()}
                FROM library
                WHERE artist LIKE ? OR title LIKE ?
                LIMIT 500
            """
            fuzzy_params = (f"%{prefix}%", f"%{prefix}%")

        assert self._conn is not None, "Database not connected. Call connect() first."
        cursor = await self._conn.execute(sql, fuzzy_params)
        rows = await cursor.fetchall()

        if not rows:
            return []

        # Score each result by fuzzy matching against the query
        scored_results = []
        for row in rows:
            item = LibraryItem(**dict(row))
            # Compare query against "artist - title" combined
            combined = f"{item.artist or ''} {item.title or ''}".lower()
            score = fuzz.token_set_ratio(query.lower(), combined)

            if score >= threshold:
                scored_results.append((score, item))

        # Sort by score descending and return top results
        scored_results.sort(key=lambda x: x[0], reverse=True)
        results = [item for _, item in scored_results[:limit]]

        if results:
            logger.info(f"Fuzzy search for '{query}' found {len(results)} results")

        return results

    async def find_similar_artist(self, artist: str, threshold: int = 85) -> str | None:
        """
        Find a similar artist name in the library using fuzzy matching.

        Args:
            artist: Artist name to match
            threshold: Minimum fuzzy match score (0-100) to accept

        Returns:
            Corrected artist name if a good match is found, None otherwise
        """
        if not self._conn:
            raise RuntimeError("Database not connected")

        # Get candidate artists using prefix of first significant word
        artist_lower = artist.lower()

        # For short names, a single-character difference is proportionally large,
        # so raise the threshold to prevent false corrections (e.g. "Plug" -> "Plugz")
        effective_threshold = max(threshold, 100 - len(artist_lower) * 2)

        words = artist_lower.split()

        # Use first word with 3+ chars for candidate search
        search_word = next((w for w in words if len(w) >= 3), None)
        if not search_word:
            return None

        prefix = search_word[:3]

        if self._has_alternate_artist:
            sql = """
                SELECT DISTINCT name FROM (
                    SELECT artist AS name FROM library WHERE artist LIKE ?
                    UNION
                    SELECT alternate_artist_name AS name FROM library
                    WHERE alternate_artist_name IS NOT NULL AND alternate_artist_name LIKE ?
                )
                LIMIT 100
            """
            cursor = await self._conn.execute(sql, (f"{prefix}%", f"{prefix}%"))
        else:
            sql = """
                SELECT DISTINCT artist FROM library
                WHERE artist LIKE ?
                LIMIT 100
            """
            cursor = await self._conn.execute(sql, (f"{prefix}%",))
        rows = await cursor.fetchall()

        if not rows:
            return None

        # Find best fuzzy match
        best_match: str | None = None
        best_score: float = 0

        for row in rows:
            candidate: str = row[0]
            if not candidate:
                continue

            score = fuzz.ratio(artist_lower, candidate.lower())
            if score > best_score and score >= effective_threshold:
                best_score = score
                best_match = candidate

        if best_match and best_match.lower() != artist_lower:
            logger.info(
                f"Corrected artist '{artist}' to '{best_match}' "
                f"(score: {best_score}, threshold: {effective_threshold})"
            )
            return best_match

        return None
