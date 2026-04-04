"""Library deduplication: group library.db rows by normalized artist+title."""

from __future__ import annotations

from dataclasses import dataclass, field

import aiosqlite

from core.matching import is_compilation_artist
from scripts.streaming_availability.matching import normalize_album_title, normalize_artist_name


@dataclass
class DeduplicatedAlbum:
    """A unique album from the library, possibly spanning multiple formats/rows."""

    normalized_artist: str
    normalized_title: str
    display_artist: str
    display_title: str
    library_ids: list[int] = field(default_factory=list)
    formats: list[str] = field(default_factory=list)
    genre: str | None = None
    label: str | None = None
    is_compilation: bool = False


async def deduplicate_library(db_path: str) -> list[DeduplicatedAlbum]:
    """Read all rows from library.db and deduplicate by normalized (artist, title).

    Uses alternate_artist_name when available. Groups rows with the same
    normalized artist+title across formats into a single DeduplicatedAlbum.
    """
    groups: dict[tuple[str, str], DeduplicatedAlbum] = {}

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, artist, genre, format, alternate_artist_name, label FROM library"
        ) as cursor:
            async for row in cursor:
                artist = row["alternate_artist_name"] or row["artist"] or ""
                title = row["title"] or ""

                norm_artist = normalize_artist_name(artist)
                norm_title = normalize_album_title(title)
                key = (norm_artist, norm_title)

                if key not in groups:
                    groups[key] = DeduplicatedAlbum(
                        normalized_artist=norm_artist,
                        normalized_title=norm_title,
                        display_artist=artist,
                        display_title=title,
                        library_ids=[],
                        formats=[],
                        genre=row["genre"],
                        label=row["label"],
                        is_compilation=is_compilation_artist(artist),
                    )

                album = groups[key]
                album.library_ids.append(row["id"])
                fmt = row["format"]
                if fmt and fmt not in album.formats:
                    album.formats.append(fmt)

    return list(groups.values())
