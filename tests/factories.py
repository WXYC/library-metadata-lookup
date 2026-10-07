"""Shared test factories for model construction."""

import sqlite3
from unittest.mock import AsyncMock

from discogs.models import DiscogsSearchResult
from generated.api_models import DiscogsMatchResult, LibraryCatalogItem
from library.db import LIBRARY_FTS_CREATE_SQL, LibraryDB
from library.models import LibraryItem
from lookup.release_resolution import ResolvedRelease
from services.parser import MessageType, ParsedRequest


def make_parsed_request(artist: str, album: str) -> ParsedRequest:
    """Build the (artist, album) request shape the library-miss probe takes."""
    return ParsedRequest(
        artist=artist,
        album=album,
        message_type=MessageType.REQUEST,
        is_request=True,
    )


def make_library_item(id=1, artist="Stereolab", title="Aluminum Tunes", **kwargs):
    """Build a LibraryItem (internal domain model) with sensible defaults."""
    defaults = {
        "call_letters": "S",
        "artist_call_number": 1,
        "release_call_number": 1,
        "genre": "Rock",
        "format": "CD",
    }
    defaults.update(kwargs)
    return LibraryItem(id=id, artist=artist, title=title, **defaults)


def shelve(db, rows):
    """Point a mocked ``LibraryDB``'s artist-keyed queries at ``rows`` (LML#1406).

    ``artist_names_matching`` returns every stored spelling in ``rows``
    whatever the phrases (the real query is a containment test, so a superset
    is the honest stand-in); ``rows_by_artist`` returns the rows filed under
    exactly the spellings asked for. ``search_among`` returns the same rows
    whatever the query: a superset stand-in for its full-text match, which the
    lane's title filter then narrows (LML#1421). ``titles_by_artist`` returns
    those rows' titles, and ``artist_call_letters`` their ``(artist,
    call_letters)`` (LML#1449). Returns ``db``.
    """

    async def artist_names_matching(phrases):
        return list(dict.fromkeys(row.artist for row in rows))

    async def rows_by_artist(artists):
        return [row for row in rows if row.artist in artists]

    async def search_among(query, artists):
        return await rows_by_artist(artists)

    db.artist_names_matching = AsyncMock(side_effect=artist_names_matching)
    db.rows_by_artist = AsyncMock(side_effect=rows_by_artist)
    db.search_among = AsyncMock(side_effect=search_among)
    db.titles_by_artist = AsyncMock(
        side_effect=lambda artists: [row.title for row in rows if row.artist in artists]
    )
    db.artist_call_letters = AsyncMock(
        side_effect=lambda artists: {
            (row.artist, row.call_letters or "") for row in rows if row.artist in artists
        }
    )
    return db


CROWD = 60
"""Rows by other artists sharing a word with the artist under test. More than
the 50-row ``db.search`` window the artist-keyed lanes used to read."""


async def make_library_catalog(
    tmp_path,
    rows: list[tuple[str, ...]],
    call_letters: dict[str | tuple[str, str], str] | None = None,
) -> LibraryDB:
    """A connected ``LibraryDB`` over ``rows`` of ``(artist, title)``, ids from 1.

    A real ``library_fts`` index built with the repo's own DDL, for tests of
    the gap between what the full-text index returns and what a lane keeps,
    which a mocked ``LibraryDB`` cannot show (LML#1406, LML#1421). A row may
    carry a third element, its ``alternate_artist_name``; when one does, the
    table gains that column and the index covers it, as the production
    ``library.db`` built by discogs-etl does. ``call_letters`` maps an artist,
    or one ``(artist, title)`` row, to the letters it is filed under; every
    other row is filed under "A".
    """
    alternates = any(len(row) > 2 for row in rows)
    letters = call_letters or {}
    db_file = tmp_path / "library.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
        "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
        "genre TEXT, format TEXT" + (", alternate_artist_name TEXT)" if alternates else ")")
    )
    conn.execute(
        LIBRARY_FTS_CREATE_SQL.replace("title, artist,", "title, artist, alternate_artist_name,")
        if alternates
        else LIBRARY_FTS_CREATE_SQL
    )
    conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, ?, 1, 1, 'Rock', 'LP'"
        + (", ?)" if alternates else ")"),
        [
            (
                i,
                row[1],
                row[0],
                letters.get((row[0], row[1]), letters.get(row[0], "A")),
                *([row[2] if len(row) > 2 else None] if alternates else []),
            )
            for i, row in enumerate(rows, start=1)
        ],
    )
    conn.execute("INSERT INTO library_fts(library_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()
    db = LibraryDB(db_path=db_file)
    await db.connect()
    return db


def crowd_rows(word: str) -> list[tuple[str, str]]:
    """``CROWD`` rows that match ``word`` in full-text search and are not by it."""
    half = CROWD // 2
    return [(f"{word} Ensemble {i}", f"Volume {i}") for i in range(half)] + [
        (f"Orchestra {i}", f"{word} Suite {i}") for i in range(CROWD - half)
    ]


def make_discogs_result(release_id=123, **kwargs):
    """Build a DiscogsSearchResult (internal domain model) with sensible defaults."""
    defaults = {
        "release_url": f"https://discogs.com/release/{release_id}",
        "album": "Aluminum Tunes",
        "artist": "Stereolab",
    }
    defaults.update(kwargs)
    return DiscogsSearchResult(release_id=release_id, **defaults)


def make_catalog_item(id=1, artist="Stereolab", title="Aluminum Tunes", **kwargs):
    """Build a LibraryCatalogItem (API contract model) with sensible defaults."""
    defaults = {
        "call_letters": "S",
        "artist_call_number": 1,
        "release_call_number": 1,
        "genre": "Rock",
        "format": "CD",
        "call_number": "Rock CD S 1/1",
        "library_url": f"https://dj.wxyc.org/dashboard/album/legacy/{id}",
    }
    defaults.update(kwargs)
    return LibraryCatalogItem(id=id, artist=artist, title=title, **defaults)


def make_match_result(release_id=123, **kwargs):
    """Build a DiscogsMatchResult (API contract model) with sensible defaults.

    Extended-metadata fields (discogs_artist_id, tracklist, genres, styles,
    label, full_release_date, artist_image_url, profile_tokens) default to
    None — the API contract treats them as opt-in for `extended=true`
    lookups. Pass them through ``kwargs`` to assert on their shape.
    """
    defaults = {
        "release_url": f"https://discogs.com/release/{release_id}",
        "album": "Aluminum Tunes",
        "artist": "Stereolab",
    }
    defaults.update(kwargs)
    return DiscogsMatchResult(release_id=release_id, **defaults)


def make_resolved_release(release_id=123, album_title="Aluminum Tunes", **kwargs):
    """Build a ResolvedRelease (the widened ``discogs_titles`` seam value).

    Defaults to a compilation since that is the seam's primary case; override
    ``is_compilation`` / ``release_url`` via kwargs as needed.
    """
    defaults = {
        "release_url": f"https://www.discogs.com/release/{release_id}",
        "is_compilation": True,
    }
    defaults.update(kwargs)
    return ResolvedRelease(release_id=release_id, album_title=album_title, **defaults)


LOOKUP_BODY = {
    "artist": "Jessica Pratt",
    "album": "On Your Own Love Again",
    "raw_message": "Jessica Pratt - On Your Own Love Again",
}
