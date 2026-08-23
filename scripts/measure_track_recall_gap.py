"""Coverage census for LML#1264: how wide is the non-V/A track-recall gap?

MEASUREMENT ONLY. This script changes no matcher, no strategy, and no
``/lookup`` behaviour -- it is the ticket's acceptance criterion #1 ("the gap
is measured before it is fixed"), not the fix itself.

LML#1264 documents that track-level recall exists for exactly two cases --
V/A compilations (``lml_cache.compilation_track_location``) and songs with no
artist (``SONG_AS_TRACK``) -- and that a track on a single-artist release,
requested with its artist, falls through both. This script produces a
coverage census answering:

1. How many library rows are comp-shelf (covered, in principle, by the V/A
   recall index) vs artist-shelf (covered by neither mechanism)? Uses the
   same ``wxyc_etl.text.is_compilation_artist`` classifier
   ``scripts/build_compilation_track_location.py`` gates its own indexing on,
   not a hand-rolled ``LIKE`` guess -- see ``naive_like_comp_count`` below for
   why that distinction matters.
2. Of the artist-shelf rows, how many resolve to a specific Discogs release
   *today*, via the two real seams: ``lml_cache.library_release_override``
   pins (prod-only data -- see the module note below) and an exact-artist
   Discogs match cross-checked against the same 80/80 floor
   ``clients/streaming/matching.py::find_best_typed_match`` already applies
   at request time for artwork resolution (LML#478).
3. Of those, how many have a cached tracklist (``release_track`` rows)
   available at all.
4. The headline: how many artist-shelf rows could gain track-level recall
   with **no new data collection**, vs. how many would need some (a new
   Discogs match, a new override pin, or new track data upstream).

TRAP (read before trusting any Discogs-side number this script prints): the
real production discogs-cache is *filtered* -- it admits a release only when
its credited artist name case-folds exactly against a WXYC library artist
name (mirroring ``scripts/build_filtered_discogs.py``'s own filter SQL). A
full, unscoped Discogs dump (``discogs_full`` locally) has no such filter, so
every count this script derives from ``--discogs-url`` is an **upper bound**
on what the real prod cache can currently see -- never presented as "today's
real coverage." The ``lml_cache.library_release_override`` pin table lives
only in the shared prod discogs-cache Postgres; this script does not query
prod (out of scope, needs explicit approval), so pin coverage is reported
from the documented code comment in ``identity/bulk_resolve.py`` --
NOT independently re-measured here.

Usage:
    uv run python -m scripts.measure_track_recall_gap \\
        --library-db /path/to/library.db \\
        --discogs-url postgresql://postgres@localhost:5432/discogs_full \\
        --out /tmp/lml_1264_census.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field

import asyncpg
from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import find_best_typed_match

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("measure_track_recall_gap")

DEFAULT_LIBRARY_DB_PATH = "library.db"

# Prod-only figure, quoted verbatim (not re-derived) from the LML#1138
# section comment in ``identity/bulk_resolve.py`` as of the 2026-08-08
# decision record on that ticket. Included here purely as reader context --
# this script has no local path to verify it (the pin table lives only in
# the shared prod discogs-cache Postgres) and does not attempt to.
DOCUMENTED_PIN_COVERAGE_NOTE = (
    "identity/bulk_resolve.py documents lml_cache.library_release_override pin "
    "coverage at ~29% of non-V/A library rows (v1, LML#1138's 2026-08-08 decision "
    "record), and scripts/discogs_rematch.py's re-match passes at ~77% "
    "non-compilation coverage. Both figures are quoted from committed code/prior "
    "work, NOT re-derived by this script -- verifying them directly needs prod "
    "discogs-cache access, which is out of this script's scope."
)

# The exact-match predicate mirrors scripts/build_filtered_discogs.py's own
# cache-build filter ("left(artist_name, 200)" truncation included) so this
# script's "artist known to Discogs" leg reproduces the real filter, not an
# approximation of it.
_EXACT_ARTIST_MATCH_SQL = """
    SELECT ca.name AS library_artist, r.id AS release_id, r.title AS release_title,
           ra.artist_name AS discogs_artist_name
    FROM census_artists ca
    JOIN release_artist ra
        ON ra.extra = 0
        AND lower(left(ra.artist_name, 200)) = lower(ca.name)
    JOIN release r ON r.id = ra.release_id
"""

_TRACKLIST_PRESENCE_SQL = """
    SELECT DISTINCT release_id FROM release_track WHERE release_id = ANY($1::int[])
"""


@dataclass(frozen=True)
class LibraryRow:
    """One ``library.db`` row: just the columns this census needs."""

    id: int
    artist: str
    title: str


@dataclass(frozen=True)
class ShelfCensus:
    """Item 1 of the census: comp-shelf vs artist-shelf, classifier-based."""

    total: int
    comp_shelf: list[LibraryRow]
    artist_shelf: list[LibraryRow]
    comp_shelf_naive_like_count: int


@dataclass(frozen=True)
class ReleaseCandidate:
    """One Discogs release credited (``extra=0``) to a name matching a library artist."""

    release_id: int
    title: str
    artist_name: str


@dataclass(frozen=True)
class GapCensusReport:
    """The full LML#1264 measurement, ready to render or serialize."""

    total_library_rows: int
    comp_shelf_count: int
    comp_shelf_naive_like_count: int
    artist_shelf_count: int
    artist_shelf_with_exact_artist_match: int
    artist_shelf_with_resolvable_release: int
    artist_shelf_with_cached_tracklist: int
    discogs_source: str | None
    pin_coverage_note: str = field(default=DOCUMENTED_PIN_COVERAGE_NOTE)

    @property
    def could_gain_recall_no_new_collection(self) -> int:
        """Upper-bound count of rows resolvable + tracklisted with zero new collection."""
        return self.artist_shelf_with_cached_tracklist

    @property
    def would_need_new_collection(self) -> int:
        """Artist-shelf rows this census could not resolve to a cached tracklist."""
        return self.artist_shelf_count - self.artist_shelf_with_cached_tracklist

    def to_dict(self) -> dict:
        out = asdict(self)
        out["could_gain_recall_no_new_collection"] = self.could_gain_recall_no_new_collection
        out["would_need_new_collection"] = self.would_need_new_collection
        return out


def load_library_rows(db_path: str) -> list[LibraryRow]:
    """Read every ``(id, artist, title)`` row out of ``library.db``."""
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute("SELECT id, artist, title FROM library")
        return [
            LibraryRow(id=row["id"], artist=row["artist"] or "", title=row["title"] or "")
            for row in cur.fetchall()
        ]
    finally:
        con.close()


# The naive heuristic a reader might reach for first -- and the one this
# ticket's own background section quotes as the comp-shelf count (5,738 rows
# in the 2026-07-19 prod snapshot). It only catches the two literal shelf
# prefixes; ``is_compilation_artist`` recognizes more forms (bare "V/A",
# lowercase "various", etc.) that the LIKE guess misses. Both are surfaced
# side by side deliberately -- LML#1264's own brief says "verify
# independently, don't just trust me."
_NAIVE_COMP_PREFIXES = ("Various Artists", "Soundtracks")


def naive_like_comp_count(rows: list[LibraryRow]) -> int:
    """The ``artist LIKE 'Various Artists%' OR 'Soundtracks%'`` heuristic count."""
    return sum(1 for row in rows if row.artist.startswith(_NAIVE_COMP_PREFIXES))


def split_shelf(rows: list[LibraryRow]) -> ShelfCensus:
    """Classify every row comp-shelf vs artist-shelf via the shared classifier."""
    comp_shelf: list[LibraryRow] = []
    artist_shelf: list[LibraryRow] = []
    for row in rows:
        (comp_shelf if is_compilation_artist(row.artist) else artist_shelf).append(row)
    return ShelfCensus(
        total=len(rows),
        comp_shelf=comp_shelf,
        artist_shelf=artist_shelf,
        comp_shelf_naive_like_count=naive_like_comp_count(rows),
    )


async def find_exact_artist_candidates(
    conn: asyncpg.Connection, artist_names: list[str]
) -> dict[str, list[ReleaseCandidate]]:
    """Batched exact-artist-name Discogs candidate lookup, one round trip.

    Mirrors ``scripts/build_filtered_discogs.py``'s Phase 1 cache-build
    filter (case-folded, 200-char-truncated exact match) -- the same
    predicate the real prod discogs-cache build uses, run here against
    whatever database ``conn`` is connected to. Grouped by the ORIGINAL
    library artist string (not lower-cased), so callers can look candidates
    up by ``row.artist`` directly.
    """
    if not artist_names:
        return {}
    await conn.execute("CREATE TEMP TABLE IF NOT EXISTS census_artists (name TEXT)")
    await conn.execute("DELETE FROM census_artists")
    await conn.copy_records_to_table(
        "census_artists", records=[(a,) for a in artist_names], columns=["name"]
    )
    rows = await conn.fetch(_EXACT_ARTIST_MATCH_SQL)
    grouped: dict[str, list[ReleaseCandidate]] = {}
    for row in rows:
        grouped.setdefault(row["library_artist"], []).append(
            ReleaseCandidate(
                release_id=row["release_id"],
                title=row["release_title"],
                artist_name=row["discogs_artist_name"],
            )
        )
    return grouped


def resolve_release_for_row(
    row: LibraryRow, candidates: list[ReleaseCandidate]
) -> ReleaseCandidate | None:
    """Pick the best-scoring candidate at the same 80/80 floor LML#478 uses.

    Reuses ``find_best_typed_match`` read-only (no matcher code is modified
    by this script) so "resolvable" means exactly what it means at request
    time for artwork resolution today -- not a newly-invented, more lenient
    bar that would overstate the gap's closability.
    """
    if not candidates:
        return None
    return find_best_typed_match(
        candidates,
        query_artist=row.artist,
        query_title=row.title,
        artist_fn=lambda c: c.artist_name,
        title_fn=lambda c: c.title,
        key_fn=lambda c: c.release_id,
    )


async def find_tracklist_release_ids(conn: asyncpg.Connection, release_ids: list[int]) -> set[int]:
    """Which of ``release_ids`` carry at least one ``release_track`` row."""
    if not release_ids:
        return set()
    rows = await conn.fetch(_TRACKLIST_PRESENCE_SQL, release_ids)
    return {row["release_id"] for row in rows}


async def measure_discogs_side(
    conn: asyncpg.Connection, artist_shelf: list[LibraryRow]
) -> tuple[int, int, int]:
    """Items 2+3 of the census: exact-artist / resolvable / tracklisted counts."""
    distinct_artists = sorted({row.artist for row in artist_shelf})
    log.info("Matching %d distinct artist-shelf artist names...", len(distinct_artists))
    candidates_by_artist = await find_exact_artist_candidates(conn, distinct_artists)
    exact_artist_match_count = sum(1 for row in artist_shelf if row.artist in candidates_by_artist)
    log.info(
        "%d/%d distinct artists have an exact-case-folded Discogs match",
        len(candidates_by_artist),
        len(distinct_artists),
    )

    resolved: dict[int, ReleaseCandidate] = {}
    for i, row in enumerate(artist_shelf, start=1):
        match = resolve_release_for_row(row, candidates_by_artist.get(row.artist, []))
        if match is not None:
            resolved[row.id] = match
        if i % 5000 == 0:
            log.info("Resolved %d/%d artist-shelf rows so far...", i, len(artist_shelf))
    log.info(
        "Resolved %d/%d artist-shelf rows to a specific release at the 80/80 floor",
        len(resolved),
        len(artist_shelf),
    )

    release_ids = sorted({match.release_id for match in resolved.values()})
    tracklisted_ids = await find_tracklist_release_ids(conn, release_ids)
    with_tracklist = sum(1 for match in resolved.values() if match.release_id in tracklisted_ids)
    log.info("%d/%d resolved releases carry a cached tracklist", with_tracklist, len(resolved))
    return exact_artist_match_count, len(resolved), with_tracklist


async def build_report(library_db: str, discogs_url: str | None) -> GapCensusReport:
    rows = load_library_rows(library_db)
    census = split_shelf(rows)
    log.info(
        "Library census: %d total, %d comp-shelf (%d via naive LIKE), %d artist-shelf",
        census.total,
        len(census.comp_shelf),
        census.comp_shelf_naive_like_count,
        len(census.artist_shelf),
    )

    if discogs_url is None:
        log.warning(
            "No --discogs-url / DATABASE_URL_DISCOGS given -- skipping the Discogs-side "
            "measurement (items 2-4). Reporting the library census only."
        )
        return GapCensusReport(
            total_library_rows=census.total,
            comp_shelf_count=len(census.comp_shelf),
            comp_shelf_naive_like_count=census.comp_shelf_naive_like_count,
            artist_shelf_count=len(census.artist_shelf),
            artist_shelf_with_exact_artist_match=0,
            artist_shelf_with_resolvable_release=0,
            artist_shelf_with_cached_tracklist=0,
            discogs_source=None,
        )

    conn = await asyncpg.connect(discogs_url)
    try:
        exact_match, resolvable, tracklisted = await measure_discogs_side(conn, census.artist_shelf)
    finally:
        await conn.close()

    return GapCensusReport(
        total_library_rows=census.total,
        comp_shelf_count=len(census.comp_shelf),
        comp_shelf_naive_like_count=census.comp_shelf_naive_like_count,
        artist_shelf_count=len(census.artist_shelf),
        artist_shelf_with_exact_artist_match=exact_match,
        artist_shelf_with_resolvable_release=resolvable,
        artist_shelf_with_cached_tracklist=tracklisted,
        discogs_source=discogs_url,
    )


def render_report(report: GapCensusReport) -> str:
    """Console-friendly summary. Every Discogs-sourced number is labeled
    an upper bound; the pin-coverage figure is labeled as documented,
    not measured."""
    lines = [
        "LML#1264 track-recall gap census",
        "=================================",
        f"Total library rows:        {report.total_library_rows:>8,}",
        f"Comp-shelf (classifier):   {report.comp_shelf_count:>8,}",
        f"  (naive LIKE heuristic:   {report.comp_shelf_naive_like_count:>8,})",
        f"Artist-shelf:               {report.artist_shelf_count:>8,}",
        "",
    ]
    if report.discogs_source is None:
        lines.append("Discogs-side measurement SKIPPED (no --discogs-url given).")
    else:
        lines.extend(
            [
                f"Discogs source: {report.discogs_source}",
                "UPPER BOUND -- computed against an unfiltered Discogs dump, not the",
                "filtered prod discogs-cache; see the module docstring's TRAP note.",
                f"  exact-artist-match:      {report.artist_shelf_with_exact_artist_match:>8,}",
                f"  resolvable (80/80 floor):{report.artist_shelf_with_resolvable_release:>8,}",
                f"  + cached tracklist:      {report.artist_shelf_with_cached_tracklist:>8,}",
                "",
                f"Headline (UPPER BOUND): could gain recall, no new collection: "
                f"{report.could_gain_recall_no_new_collection:,}",
                f"                         would need new collection:            "
                f"{report.would_need_new_collection:,}",
            ]
        )
    lines.append("")
    lines.append(f"Pin coverage (documented, NOT measured here): {report.pin_coverage_note}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure the LML#1264 non-V/A track-recall gap (measurement only).",
    )
    parser.add_argument(
        "--library-db",
        default=DEFAULT_LIBRARY_DB_PATH,
        help=f"Path to library.db (default: {DEFAULT_LIBRARY_DB_PATH})",
    )
    parser.add_argument(
        "--discogs-url",
        default=None,
        help=(
            "Discogs PostgreSQL URL (default: DATABASE_URL_DISCOGS env var). "
            "Omit to run the library-only census with the Discogs-side "
            "measurement skipped."
        ),
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Optional path to write the full report as JSON.",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> GapCensusReport:
    import os

    discogs_url = args.discogs_url or os.environ.get("DATABASE_URL_DISCOGS")
    try:
        report = await build_report(args.library_db, discogs_url)
    except Exception:
        log.exception("Census failed")
        raise
    print(render_report(report))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(report.to_dict(), f, indent=2)
        log.info("Wrote full report to %s", args.out)
    return report


def main() -> None:
    from dotenv import load_dotenv

    load_dotenv()
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
