import logging
import re
from pathlib import Path

import aiosqlite
from cachetools import TTLCache  # type: ignore[import-untyped]
from rapidfuzz import fuzz
from wxyc_etl.schema import library_columns
from wxyc_etl.text import strip_leading_article
from wxyc_etl.text import to_match_form as normalize_for_comparison

from library.models import LibraryItem

logger = logging.getLogger(__name__)

LEADING_ARTICLES = frozenset({"the", "a", "an"})
"""English articles stripped from normalized artist names.

Sole local survivor of the former ``core/text.py`` (LML#1042 folded that
module's ``strip_leading_article`` into ``wxyc_etl.text.strip_leading_article``
directly — the two were byte-identical, so it was a pure delegation, not a
behavior change). This frozenset is a word-membership filter for candidate
search-term selection below, distinct from the strip function itself, and
has no crate equivalent to promote to, so it stays local. ``library/db.py``
is its only consumer.
"""

STOPWORDS = frozenset(
    {
        # Articles
        "the",
        "a",
        "an",
        # Conjunctions/prepositions
        "and",
        "with",
        "from",
        # Demonstratives
        "that",
        "this",
        # Request-specific noise
        "play",
        "song",
        "remix",
        # Label/format noise
        "story",
        "records",
    }
)
"""Words to exclude when extracting significant keywords from search queries."""

# Default path to SQLite database (relative to project root)
DEFAULT_DB_PATH = Path(__file__).parent.parent / "library.db"

# FTS5 tokenizer extends unicode61 with the Symbol category and U+200D ZWJ so
# bare-emoji rows (`👋`, `🎵`) and ZWJ graphemes (`👨‍👩‍👧‍👦`, `🇺🇸`) get tokenized.
# The default unicode61 categories ('L* N* Co') drop everything else, leaving
# the FTS index with no token to anchor a supplementary-plane row on.
# Cf is NOT included wholesale — that would merge tokens around zero-width-space,
# BOM, soft-hyphen, etc. ZWJ is opted in explicitly via tokenchars.
# Production schema in WXYC/discogs-etl's export_to_sqlite.py must match.
#
# Note: this string is embedded inside a double-quoted SQL attribute below;
# it must not contain `"`. The U+200D ZWJ in `tokenchars` is written as the
# explicit `\u200d` escape so the source stays unambiguous in editors and
# diffs that render zero-width characters invisibly.
LIBRARY_FTS_TOKENIZER = "unicode61 categories 'L* N* Co S*' tokenchars '\u200d'"

LIBRARY_FTS_CREATE_SQL = f"""
    CREATE VIRTUAL TABLE library_fts USING fts5(
        title, artist,
        tokenize="{LIBRARY_FTS_TOKENIZER}",
        content=library,
        content_rowid=id
    )
"""


def _to_fts_match_query(query: str) -> str:
    """Quote each whitespace-separated term as an FTS5 string literal.

    FTS5 metacharacters (``.`` ``"`` ``*`` ``:`` ``(`` ``)`` etc.) inside a
    double-quoted term are treated as literal content, not query syntax, so
    punctuated/initials artist names ("M.I.A.", "J. Mascis") tokenize
    instead of raising ``fts5: syntax error``. Joining the quoted terms with
    spaces preserves the implicit AND-across-terms semantics of an
    unescaped multi-word MATCH query, so clean queries are unaffected.

    This is a query-parse fix only -- it does not touch the on-disk
    tokenizer (:data:`LIBRARY_FTS_TOKENIZER`), which must stay byte-identical
    to discogs-etl's ``export_to_sqlite.py``.

    Returns ``""`` when the query has no terms (empty or all-whitespace) --
    the caller must treat that as "no FTS query" and route to the LIKE/fuzzy
    fallback chain instead of executing ``MATCH ''``, which is itself an
    ``fts5: syntax error``.
    """
    terms = query.split()
    return " ".join('"' + t.replace('"', '""') + '"' for t in terms)


def _fts_normalize(query: str) -> str:
    """Normalize a query for the LIKE/fuzzy fallback tokenizers.

    First strips diacritics and lowercases via the shared ``wxyc_etl``
    ``to_match_form`` (so e.g. "Nilüfer Yanya" matches "nilufer yanya"),
    then collapses every remaining non-ASCII-alphanumeric, non-whitespace
    character to a space. The collapse-to-space (rather than delete) keeps
    punctuation from fusing adjacent words together -- "Chuquimamani-Condori"
    becomes "chuquimamani condori", two tokens, not "chuquimamanicondori".

    Used by :meth:`LibraryDB._fallback_like_search` and
    :meth:`LibraryDB._fuzzy_search`, which both split the result on
    whitespace to build their word lists. That ``.split()`` is why the
    whitespace runs the substitution leaves behind ("A.R. Kane" becomes
    "a r  kane", two spaces) are never collapsed here — they are invisible
    at both call sites. A third caller would have to collapse them.

    Deliberately NOT the same function as
    :func:`lookup.name_folding.fold_punctuation_for_comparison`, which folds
    punctuation to a space for the same reason. This one is ASCII-only
    because the FTS5 and LIKE tokenizers it feeds are; that one is
    Unicode-aware because it compares artist *names*. Routing a name
    through this class would erase a CJK or Cyrillic artist to whitespace
    entirely ("少年ナイフ" -> five spaces). Same concept, two fidelities:
    do not merge them.
    """
    return re.sub(r"[^a-z0-9\s]", " ", normalize_for_comparison(query))


# Module-level caches for library search results.
# Lazy-initialized from settings. Cleared when the database is closed/replaced.
_artist_cache: TTLCache | None = None
_search_cache: TTLCache | None = None


def _get_library_caches() -> tuple[TTLCache, TTLCache]:
    """Get or create library caches from settings."""
    global _artist_cache, _search_cache
    if _artist_cache is None or _search_cache is None:
        from config.settings import get_settings

        settings = get_settings()
        _artist_cache = TTLCache(
            maxsize=settings.library_cache_maxsize // 2, ttl=settings.library_cache_ttl
        )
        _search_cache = TTLCache(
            maxsize=settings.library_cache_maxsize, ttl=settings.library_cache_ttl
        )
    return _artist_cache, _search_cache


def clear_library_caches() -> None:
    """Reset library search and artist caches (called on DB upload)."""
    global _artist_cache, _search_cache
    if _artist_cache is not None:
        _artist_cache.clear()
    if _search_cache is not None:
        _search_cache.clear()


class LibraryDB:
    """Async SQLite client for library catalog searches."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        self._conn: aiosqlite.Connection | None = None
        self._has_alternate_artist: bool = False
        self._has_album_artist: bool = False
        self._has_label: bool = False
        self._has_cross_reference_names: bool = False
        self._has_compilation_track_artist: bool = False
        self._has_streaming_links: bool = False
        self._artist_name_pool_lower: set[str] = set()
        """Lowercased exact-match pool for :meth:`_artist_exists_exactly`
        (LML#1248). Built once by :meth:`_build_artist_name_pool` at the end
        of :meth:`connect`, from the same four source columns the old
        per-call SQL scan queried. Empty until ``connect()`` runs, or
        partial/empty if a source failed to load (schema drift)."""

    async def connect(self):
        """Open database connection."""
        if not self.db_path.exists():
            raise FileNotFoundError(
                f"Library database not found at {self.db_path}. "
                "Upload via POST /admin/upload-library-db or see discogs-etl repo."
            )

        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row

        # Detect columns via PRAGMA and validate against wxyc_etl schema
        cursor = await self._conn.execute("PRAGMA table_info(library)")
        columns = await cursor.fetchall()
        column_names = {row[1] for row in columns}

        # Validate expected columns from wxyc_etl.schema
        expected_columns = set(library_columns())
        missing = expected_columns - column_names
        if missing:
            logger.warning(
                f"library.db is missing expected columns: {sorted(missing)}. "
                "The database may have been generated by an older version of the ETL pipeline."
            )

        self._has_alternate_artist = "alternate_artist_name" in column_names
        self._has_album_artist = "album_artist" in column_names
        self._has_label = "label" in column_names
        self._has_cross_reference_names = "cross_reference_names" in column_names

        # Detect optional tables
        tables_cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='compilation_track_artist'"
        )
        self._has_compilation_track_artist = await tables_cursor.fetchone() is not None

        tables_cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='streaming_links'"
        )
        self._has_streaming_links = await tables_cursor.fetchone() is not None

        logger.info(
            f"Connected to SQLite database: {self.db_path} "
            f"(alternate_artist_name: {'yes' if self._has_alternate_artist else 'no'}, "
            f"album_artist: {'yes' if self._has_album_artist else 'no'}, "
            f"label: {'yes' if self._has_label else 'no'}, "
            f"cross_reference_names: {'yes' if self._has_cross_reference_names else 'no'}, "
            f"compilation_track_artist: {'yes' if self._has_compilation_track_artist else 'no'}, "
            f"streaming_links: {'yes' if self._has_streaming_links else 'no'})"
        )

        await self._build_artist_name_pool()

    async def _build_artist_name_pool(self) -> None:
        """Materialize the lowercased exact-name pool for :meth:`_artist_exists_exactly`.

        LML#1248: replaces a per-call, four-branch ``LOWER(artist) = ?``
        table scan (non-sargable against ``idx_artist`` -- SQLite degrades
        to `SCAN library USING COVERING INDEX`, ~4.9ms/branch, ~13-15ms
        across up to four unioned branches on 64,676 rows) with a one-time
        read into a ``set[str]``, so the per-call check becomes O(1)
        membership testing.

        Reads exactly the same four source columns the old SQL scanned --
        ``library.artist``, ``library.alternate_artist_name``,
        ``library.album_artist``, ``compilation_track_artist.artist_name``
        -- gated by the same ``_has_*`` flags set just above. Each source is
        loaded independently and the three OPTIONAL ones are wrapped in their
        own try/except: a schema surprise in one (notably
        ``compilation_track_artist``, whose presence is detected only by table
        *name* via ``sqlite_master`` in :meth:`connect` and never by checking
        for an ``artist_name`` column) degrades the pool to a partial set
        rather than raising out of ``connect()`` and taking down all library
        search over what is purely a performance optimization. This was a
        review finding on #1249's pool-building code.

        ``library.artist`` is deliberately EXCLUDED from that swallow. It is
        mandatory, and losing it does not cost a few names, it costs the whole
        pool -- and an empty pool makes :meth:`_artist_exists_exactly` answer
        ``False`` for every artist, silently disabling the #857 guard for the
        connection's lifetime while search still appears to work. That is the
        exact failure #857 exists to prevent (LML#1245 measured 52/52 such
        corrections landing on genuinely different artists). A database that
        cannot serve ``library.artist`` cannot serve catalog search either --
        every result row selects it -- so failing loudly here loses nothing
        that was still working.

        Footprint: one lowercased ``str`` per distinct name across the present
        sources. Measured at 27,517 names / ~3.4 MiB on the production
        catalog (64,676 rows, no ``compilation_track_artist`` table), held per
        worker process. Rebuilt on every :meth:`connect`, which includes the
        #706 hot-swap reconnect -- and that one runs inline in the request
        that detects the swap.
        """
        assert self._conn is not None  # Caller (connect) validates

        mandatory: tuple[str, str] = ("library", "artist")
        sources: list[tuple[str, str]] = [mandatory]
        if self._has_alternate_artist:
            sources.append(("library", "alternate_artist_name"))
        if self._has_album_artist:
            sources.append(("library", "album_artist"))
        if self._has_compilation_track_artist:
            sources.append(("compilation_track_artist", "artist_name"))

        pool: set[str] = set()
        for table, column in sources:
            try:
                cursor = await self._conn.execute(
                    f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL"
                )
                rows = await cursor.fetchall()
                # No truthiness filter: the SQL's IS NOT NULL is the whole
                # inclusion rule, and dropping empty strings here would make
                # the pool narrower than the predicate it reproduces --
                # alternate_artist_name is '' rather than NULL for most of the
                # catalog. See test_pool_admits_empty_strings_exactly_as_the_old_sql_did.
                pool.update(row[0].lower() for row in rows)
            except Exception:
                if (table, column) == mandatory:
                    raise
                logger.warning(
                    f"Failed to load '{column}' from '{table}' while building the "
                    "artist-name exact-match pool (LML#1248); that source is "
                    "skipped and the exact-match check degrades to a partial set "
                    "for it.",
                    exc_info=True,
                )

        self._artist_name_pool_lower = pool
        logger.info(f"Built artist-name exact-match pool: {len(pool)} names")

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
        """Close database connection and clear caches."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            # The pool is the only per-database state that would otherwise
            # outlive _conn; drop it with the connection so nothing can answer
            # an exact-existence check from a closed database's names.
            self._artist_name_pool_lower = set()
            clear_library_caches()
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
        if self._has_album_artist:
            cols += f", {p}album_artist"
        if self._has_label:
            cols += f", {p}label"
        if self._has_cross_reference_names:
            cols += f", {p}cross_reference_names"
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

        _, search_cache = _get_library_caches()
        cache_key = (query, artist, title, limit, fallback_to_like, fallback_to_fuzzy)
        if cache_key in search_cache:
            return search_cache[cache_key]

        result = await self._search_uncached(
            query, artist, title, limit, fallback_to_like, fallback_to_fuzzy
        )
        search_cache[cache_key] = result
        return result

    async def _search_uncached(
        self,
        query: str | None,
        artist: str | None,
        title: str | None,
        limit: int,
        fallback_to_like: bool,
        fallback_to_fuzzy: bool,
    ) -> list[LibraryItem]:
        assert self._conn is not None  # Caller validates
        if query:
            # Full-text search using FTS5. Query terms are quoted as FTS5
            # string literals (see _to_fts_match_query) so metacharacters
            # (. " * : ( ) etc.) in punctuated/initials artist names are
            # treated as literal content instead of raising
            # `fts5: syntax error`.
            match_query = _to_fts_match_query(query)
            sql = f"""
                SELECT {self._select_columns("l")}
                FROM library l
                JOIN library_fts fts ON l.id = fts.rowid
                WHERE library_fts MATCH ?
                LIMIT ?
            """
            try:
                if match_query:
                    cursor = await self._conn.execute(sql, (match_query, limit))
                    rows = await cursor.fetchall()
                else:
                    # An empty/all-whitespace query quotes down to "", and
                    # `MATCH ''` is itself an fts5 syntax error. Skip the FTS
                    # execute entirely and treat it as "no FTS results" so we
                    # fall straight through to the LIKE/fuzzy chain below.
                    rows = []

                # If no results and fallback enabled, try LIKE search
                if not rows and fallback_to_like:
                    logger.debug(
                        f"FTS search for '{query}' returned no results, trying LIKE fallback"
                    )
                    rows = await self._fallback_like_search(query, limit)

                # If still no results, try fuzzy search
                if not rows and fallback_to_fuzzy:
                    logger.debug(
                        f"LIKE search for '{query}' returned no results, trying fuzzy fallback"
                    )
                    return await self._fuzzy_search(query, limit)
            except Exception as e:
                # FTS syntax errors (e.g., special characters) - fall back to LIKE
                if fallback_to_like:
                    logger.debug(f"FTS search for '{query}' failed ({e}), trying LIKE fallback")
                    rows = await self._fallback_like_search(query, limit)

                    # If still no results, try fuzzy search
                    if not rows and fallback_to_fuzzy:
                        logger.debug(
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
                if self._has_alternate_artist and self._has_album_artist:
                    conditions.append(
                        "(artist LIKE ? OR alternate_artist_name LIKE ? OR album_artist LIKE ?)"
                    )
                    params.extend([f"%{artist}%", f"%{artist}%", f"%{artist}%"])
                elif self._has_alternate_artist:
                    conditions.append("(artist LIKE ? OR alternate_artist_name LIKE ?)")
                    params.extend([f"%{artist}%", f"%{artist}%"])
                elif self._has_album_artist:
                    conditions.append("(artist LIKE ? OR album_artist LIKE ?)")
                    params.extend([f"%{artist}%", f"%{artist}%"])
                else:
                    conditions.append("artist LIKE ?")
                    params.append(f"%{artist}%")
            if title:
                conditions.append("title LIKE ?")
                params.append(f"%{title}%")
            params.append(limit)

            cols = self._select_columns()
            sql = f"""
                SELECT {cols}
                FROM library
                WHERE {" AND ".join(conditions)}
                LIMIT ?
            """

            # Include compilations featuring the artist
            if artist and self._has_compilation_track_artist:
                cta_conditions = ["cta.artist_name LIKE ?"]
                cta_params: list[str | int] = [f"%{artist}%"]
                if title:
                    cta_conditions.append("library.title LIKE ?")
                    cta_params.append(f"%{title}%")
                cta_params.append(limit)

                sql = f"""
                    SELECT {cols} FROM library
                    WHERE {" AND ".join(conditions)}
                    UNION
                    SELECT {cols} FROM library
                    JOIN compilation_track_artist cta ON cta.library_release_id = library.id
                    WHERE {" AND ".join(cta_conditions)}
                    LIMIT ?
                """
                params = (
                    params[:-1] + cta_params
                )  # remove first LIMIT, add cta params + final LIMIT

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
        normalized = _fts_normalize(query)
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
            like_fields = ["title", "artist"]
            if self._has_alternate_artist:
                like_fields.append("alternate_artist_name")
            if self._has_album_artist:
                like_fields.append("album_artist")
            conditions.append("(" + " OR ".join(f"{f} LIKE ?" for f in like_fields) + ")")
            params.extend([f"%{word}%"] * len(like_fields))

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

    async def exact_title(self, album_title: str) -> list[LibraryItem]:
        """Return library rows whose ``title`` is literally ``album_title``.

        Case-insensitive but no tokenization, no fuzzy scoring, no prefix
        matching. Useful as a pre-pass before :meth:`search`: a literal Discogs
        album title is the most reliable signal of a library row, and the FTS5
        path otherwise truncates by rowid and can drop exact-title matches
        whose row was added later in the catalog (see
        ``lookup.strategies.track_release_matching.search_album_fuzzy``).

        Returns every match — no implicit ``LIMIT``. Library rows are
        intrinsically bounded by catalog conventions (high-collision titles
        like "Greatest Hits" top out around 150 in practice); a hidden cap
        would recreate the rowid-truncation bug class this method exists to
        avoid.

        Two-phase query: a case-sensitive equality lookup runs first so the
        ``idx_title`` BINARY index covers the common case (Title Case query
        against Title Case catalog), then a ``COLLATE NOCASE`` scan runs only
        on miss. The scan is ~500x slower than the index lookup on the live
        library, so the fast path matters when ``search_album_fuzzy`` runs
        per-Discogs-release inside a single lookup.
        """
        if not self._conn:
            raise RuntimeError("Database not connected")
        if not album_title:
            return []
        album_title = album_title.strip()
        if not album_title:
            return []

        select_cols = self._select_columns()
        # Fast path: BINARY equality uses idx_title.
        cursor = await self._conn.execute(
            f"SELECT {select_cols} FROM library WHERE title = ?",
            (album_title,),
        )
        rows = await cursor.fetchall()
        if rows:
            return [LibraryItem(**dict(row)) for row in rows]

        # Slow path: case-insensitive scan, only for the rare casing mismatch.
        cursor = await self._conn.execute(
            f"SELECT {select_cols} FROM library WHERE title = ? COLLATE NOCASE",
            (album_title,),
        )
        rows = await cursor.fetchall()
        return [LibraryItem(**dict(row)) for row in rows]

    async def _fuzzy_search(self, query: str, limit: int, threshold: int = 70) -> list[LibraryItem]:
        """
        Fuzzy search fallback using rapidfuzz for typo tolerance.

        Args:
            query: Search query
            limit: Max results to return
            threshold: Minimum fuzzy match score (0-100) to include results
        """
        # Strip diacritics first, then remove remaining special chars
        normalized = _fts_normalize(query)
        words = normalized.split()

        if not words:
            return []

        # Get the longest word to use for candidate search (more selective)
        search_word = max(words, key=len)

        # Search for candidates using partial match on longest word
        prefix = search_word[:3] if len(search_word) >= 3 else search_word

        fuzzy_fields = ["artist", "title"]
        if self._has_alternate_artist:
            fuzzy_fields.append("alternate_artist_name")
        if self._has_album_artist:
            fuzzy_fields.append("album_artist")
        where_clause = " OR ".join(f"{f} LIKE ?" for f in fuzzy_fields)
        sql = f"""
            SELECT {self._select_columns()}
            FROM library
            WHERE {where_clause}
            LIMIT 500
        """
        fuzzy_params: tuple[str, ...] = tuple(f"%{prefix}%" for _ in fuzzy_fields)

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
            logger.debug(f"Fuzzy search for '{query}' found {len(results)} results")

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

        artist_cache, _ = _get_library_caches()
        cache_key = artist.lower()
        if cache_key in artist_cache:
            return artist_cache[cache_key]

        result = await self._find_similar_artist_uncached(artist, threshold)
        artist_cache[cache_key] = result
        return result

    async def _artist_exists_exactly(self, artist_lower: str, artist_compare: str) -> bool:
        """Check whether the typed artist is already an exact library artist.

        Run before any fuzzy candidate search so a name that's already in the
        library (e.g. "John Lennon") is never "corrected" to a different,
        merely-similar-scoring artist that happened to survive the candidate cap.

        LML#1248: answered from :attr:`_artist_name_pool_lower`, the
        in-memory set :meth:`_build_artist_name_pool` materializes once at
        :meth:`connect` time, rather than a per-call SQL scan -- issues no
        SQL at all. Stays ``async def`` for interface stability with its one
        caller, which already ``await``s it.

        Tests only the two RAW lowercased query forms (``artist_lower``, the
        typed name; ``artist_compare``, its leading-article-stripped form)
        against the pool of RAW lowercased catalog names -- exactly what the
        old SQL's ``LOWER(artist) = :artist_lower OR LOWER(artist) =
        :artist_compare`` matched, column unstripped. Do not also build or
        test against an article-stripped *pool* of catalog names: that would
        widen the predicate beyond what today's SQL matches (e.g. a catalog
        holding only "The National" would start reporting the bare query
        "National" as an exact match, suppressing a correction that fires
        today). See LML#1248 and the ``TestArtistNameExactMatchPool`` tests
        in ``tests/unit/test_library_db.py`` for the guard.

        One deliberate divergence from the old SQL: case folding is now full
        Unicode. SQLite's ``LOWER()`` is ASCII-only, so "NILÜFER YANYA"
        survived it unchanged and never matched a Python-lowered query --
        the guard silently missed an artist the library genuinely owns.
        Python's ``str.lower()``, which builds the pool, folds the whole
        string, so it now matches. That widening runs toward the guard's
        intent rather than away from it, and is pinned by
        ``test_pool_case_folds_non_ascii_catalog_names``.
        """
        return artist_lower in self._artist_name_pool_lower or (
            artist_compare in self._artist_name_pool_lower
        )

    async def _find_similar_artist_uncached(self, artist: str, threshold: int = 85) -> str | None:
        assert self._conn is not None  # Caller validates
        # Get candidate artists using prefix of first significant word
        artist_lower = artist.lower()
        artist_compare = strip_leading_article(artist_lower) or artist_lower

        if await self._artist_exists_exactly(artist_lower, artist_compare):
            return None

        # For short names, a single-character difference is proportionally large,
        # so raise the threshold to prevent false corrections (e.g. "Plug" -> "Plugz")
        effective_threshold = max(threshold, 100 - len(artist_lower) * 2)

        words = artist_lower.split()

        # Use first non-article word with 3+ chars for candidate search
        search_word = next(
            (
                w
                for i, w in enumerate(words)
                if len(w) >= 3 and not (i == 0 and w in LEADING_ARTICLES)
            ),
            None,
        )
        if not search_word:
            return None

        prefix = search_word[:3]
        candidate_patterns = [f"{prefix}%"]
        candidate_patterns.extend(
            f"%{w[:3]}%"
            for w in words
            if len(w) >= 3 and w != search_word and w not in LEADING_ARTICLES
        )

        column_filter = " OR ".join("artist LIKE ?" for _ in candidate_patterns)
        unions = [f"SELECT artist AS name FROM library WHERE {column_filter}"]
        params_list = list(candidate_patterns)
        if self._has_alternate_artist:
            column_filter = " OR ".join("alternate_artist_name LIKE ?" for _ in candidate_patterns)
            unions.append(
                "SELECT alternate_artist_name AS name FROM library "
                f"WHERE alternate_artist_name IS NOT NULL AND ({column_filter})"
            )
            params_list.extend(candidate_patterns)
        if self._has_album_artist:
            column_filter = " OR ".join("album_artist LIKE ?" for _ in candidate_patterns)
            unions.append(
                "SELECT album_artist AS name FROM library "
                f"WHERE album_artist IS NOT NULL AND ({column_filter})"
            )
            params_list.extend(candidate_patterns)
        if self._has_compilation_track_artist:
            column_filter = " OR ".join("artist_name LIKE ?" for _ in candidate_patterns)
            unions.append(
                f"SELECT artist_name AS name FROM compilation_track_artist WHERE {column_filter}"
            )
            params_list.extend(candidate_patterns)

        sql = (
            f"SELECT DISTINCT name FROM ({' UNION '.join(unions)}) "
            "ORDER BY CASE WHEN LOWER(name) = ? OR LOWER(name) = ? THEN 0 ELSE 1 END "
            "LIMIT 100"
        )
        params_list = [*params_list, artist_lower, artist_compare]
        cursor = await self._conn.execute(sql, params_list)
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

            candidate_lower = candidate.lower()
            candidate_compare = strip_leading_article(candidate_lower) or candidate_lower
            score = max(
                fuzz.ratio(artist_lower, candidate_lower),
                fuzz.ratio(artist_compare, candidate_compare),
            )
            if score > best_score and score >= effective_threshold:
                best_score = score
                best_match = candidate

        if (
            best_match
            and best_match.lower() != artist_lower
            and strip_leading_article(best_match.lower()) != artist_compare
        ):
            logger.info(
                f"Corrected artist '{artist}' to '{best_match}' "
                f"(score: {best_score}, threshold: {effective_threshold})"
            )
            return best_match

        return None

    async def get_streaming_links(self, library_id: int) -> dict[str, str | None] | None:
        """Get streaming service URLs for a library release.

        Returns a dict with service URL keys (spotify_url, apple_music_url, etc.)
        or None if no streaming links are available.
        """
        if not self._has_streaming_links or self._conn is None:
            return None

        cursor = await self._conn.execute(
            "SELECT spotify_url, apple_music_url, deezer_url, bandcamp_url, "
            "tidal_url, youtube_music_url, soundcloud_url "
            "FROM streaming_links WHERE library_id = ?",
            (library_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        return {
            "spotify_url": row[0],
            "apple_music_url": row[1],
            "deezer_url": row[2],
            "bandcamp_url": row[3],
            "tidal_url": row[4],
            "youtube_music_url": row[5],
            "soundcloud_url": row[6],
        }

    async def get_items_by_ids(self, library_ids: list[int]) -> dict[int, LibraryItem]:
        """Bulk-fetch library rows by id, keyed by id.

        The shelf-metadata lookup the LML#1022 location union uses to bind a
        recall-index row's bare ``library_id`` back to a displayable shelf
        ``artist``/``title`` (the recall index itself stores neither -- only
        the normalized track credit and the Discogs release). One query for
        the whole candidate set, mirroring ``get_streaming_status``'s
        bulk-by-id shape; ids with no library row are simply absent from the
        result.
        """
        if not self._conn or not library_ids:
            return {}

        placeholders = ",".join("?" * len(library_ids))
        cursor = await self._conn.execute(
            f"SELECT {self._select_columns()} FROM library WHERE id IN ({placeholders})",
            library_ids,
        )
        rows = await cursor.fetchall()
        return {row["id"]: LibraryItem(**dict(row)) for row in rows}

    async def get_streaming_status(self, library_ids: list[int]) -> dict[int, bool]:
        """Check which library releases have streaming links.

        Returns a dict mapping library_id → True for IDs that have at least one
        streaming link. IDs not in the result are not on streaming.
        """
        if not self._has_streaming_links or self._conn is None or not library_ids:
            return {}

        placeholders = ",".join("?" * len(library_ids))
        cursor = await self._conn.execute(
            f"SELECT library_id FROM streaming_links WHERE library_id IN ({placeholders})",
            library_ids,
        )
        rows = await cursor.fetchall()
        return {row[0]: True for row in rows}
