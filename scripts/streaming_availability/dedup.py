"""Library deduplication: group library.db rows by normalized artist+title."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import aiosqlite
from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import normalize_album_title, normalize_artist_name

logger = logging.getLogger(__name__)


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
    is_single: bool = False


def _diverges(existing: DeduplicatedAlbum, *, genre: str | None, label: str | None) -> bool:
    """True when a colliding row's genre/label/compilation-ness materially
    conflicts with the group's captured first-row values (LML#1095).

    Requires BOTH sides non-null for genre/label -- one row simply missing a
    field is a common real shape (incomplete cataloging of a second format),
    not a divergence signal. Measured against the real WXYC catalog: 31 of
    905 multi-row normalized-key groups show a genuine genre/label conflict,
    a mix of miscategorized re-catalogued duplicates and (plausibly) a few
    genuinely distinct same-named items -- ambiguous enough from title/artist
    alone that this stays a detection signal for operators to audit, not an
    auto-split trigger.
    """
    if genre and existing.genre and genre != existing.genre:
        return True
    if label and existing.label and label != existing.label:
        return True
    return False


async def deduplicate_library(db_path: str) -> list[DeduplicatedAlbum]:
    """Read all rows from library.db and deduplicate by normalized (artist, title).

    Uses alternate_artist_name when available. Groups rows with the same
    normalized artist+title across formats into a single DeduplicatedAlbum.

    LML#1095: a second row colliding on the same normalized key with a
    materially different genre/label/compilation-ness logs a warning (its
    own metadata is still discarded -- first-row-wins, unchanged) so a real
    metadata collision is visible to operators instead of silently dropped.
    """
    groups: dict[tuple[str, str], DeduplicatedAlbum] = {}

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, title, artist, genre, format, alternate_artist_name, label FROM library "
            "ORDER BY id"
        ) as cursor:
            async for row in cursor:
                artist = row["alternate_artist_name"] or row["artist"] or ""
                title = row["title"] or ""
                genre = row["genre"]
                label = row["label"]
                is_compilation = is_compilation_artist(artist)

                norm_artist = normalize_artist_name(artist)
                norm_title = normalize_album_title(title)
                key = (norm_artist, norm_title)

                existing = groups.get(key)
                if existing is not None and (
                    _diverges(existing, genre=genre, label=label)
                    or is_compilation != existing.is_compilation
                ):
                    logger.warning(
                        "dedup collision: library_id=%s (genre=%r, label=%r, "
                        "is_compilation=%s) collapses onto library_id(s)=%s "
                        "(genre=%r, label=%r, is_compilation=%s) under "
                        "normalized key %r -- keeping first-row metadata, "
                        "this row's genre/label/is_compilation is discarded",
                        row["id"],
                        genre,
                        label,
                        is_compilation,
                        existing.library_ids,
                        existing.genre,
                        existing.label,
                        existing.is_compilation,
                        key,
                    )

                if key not in groups:
                    groups[key] = DeduplicatedAlbum(
                        normalized_artist=norm_artist,
                        normalized_title=norm_title,
                        display_artist=artist,
                        display_title=title,
                        library_ids=[],
                        formats=[],
                        genre=genre,
                        label=label,
                        is_compilation=is_compilation,
                    )

                album = groups[key]
                album.library_ids.append(row["id"])
                fmt = row["format"]
                if fmt and fmt not in album.formats:
                    album.formats.append(fmt)

    # Determine is_single from formats: only non-LP vinyl = single
    single_formats = frozenset(['vinyl - 12"', 'vinyl - 7"', 'vinyl - 10"'])
    album_formats = frozenset(["cd", "vinyl - LP", "vinyl", "cdr"])
    for album in groups.values():
        if album.formats:
            has_album_format = any(
                f in album_formats or f.startswith("cd ") or f.startswith("vinyl - LP")
                for f in album.formats
            )
            has_only_singles = all(f in single_formats for f in album.formats)
            album.is_single = has_only_singles and not has_album_format

    return list(groups.values())
