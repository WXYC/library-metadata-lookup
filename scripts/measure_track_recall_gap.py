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
   at request time for artwork resolution (LML#478). Matching runs over the
   union of ``library.artist`` and ``library.alternate_artist_name``, exactly
   as the real cache-build filter does -- see ``artist_variants``, and do not
   narrow it to the ``artist`` column alone.
3. Of those, how many have a cached tracklist (``release_track`` rows)
   available at all. **Against an unfiltered Discogs source this check is
   non-discriminating** -- see the second TRAP below.
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

SECOND TRAP, specific to item 3: run against an unfiltered dump, the
tracklist-presence check has no discriminating power. Practically every
release in a full Discogs dump carries ``release_track`` rows (19,341,286 of
19,341,287 in the 2026-08 local ``discogs_full``), so item 3 will report ~100%
of item 2 no matter what the real state of track data is. That is not the
"tracklist availability is solved" finding it superficially resembles -- the
proposition was never tested. It is *not* a circular join (the item-2
candidate SQL never touches ``release_track``); the check is simply vacuous
at this source. The constraint that actually binds is whether a release is
present in the **filtered prod discogs-cache at all**, and this script
deliberately does not measure that -- prod access is out of scope. Read item
3 as "no upstream track-data gap blocks these rows," never as coverage.

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
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass

import asyncpg
from dotenv import load_dotenv
from wxyc_etl.text import is_compilation_artist

from clients.streaming.matching import find_best_typed_match

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

# Travels with the tracklist figure everywhere it is rendered or serialized,
# because that figure's most likely misreading ("~100%, so track data is
# solved") is also its most damaging one. See the module docstring's SECOND
# TRAP for the underlying count.
TRACKLIST_CHECK_CAVEAT = (
    "NON-DISCRIMINATING against an unfiltered Discogs source -- nearly every release in a "
    "full dump carries release_track rows, so this tracks item 2 whatever the real state of "
    "track data. Not a coverage finding. The constraint that actually binds is membership in "
    "the FILTERED prod discogs-cache, which this script does not measure."
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
    """One ``library.db`` row: just the columns this census needs.

    ``alternate_artist`` carries ``library.alternate_artist_name``, which is
    load-bearing rather than decorative here -- see ``artist_variants``.
    """

    id: int
    artist: str
    title: str
    alternate_artist: str = ""


@dataclass(frozen=True)
class ShelfCensus:
    """Item 1 of the census: comp-shelf vs artist-shelf, classifier-based."""

    total: int
    comp_shelf: list[LibraryRow]
    artist_shelf: list[LibraryRow]
    comp_shelf_naive_like_count: int


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """One Discogs release credited (``extra=0``) to a name matching a library artist.

    ``slots=True`` is not incidental. This library's artist-shelf names match
    3.8M candidate rows against the full dump, all held live for the whole
    resolve pass; slotting plus the ``sys.intern`` on ``artist_name`` in
    ``find_exact_artist_candidates`` takes the structure from ~940 MB to
    ~620 MB. This is the only structure in the script that scales with the
    dump rather than with the library.
    """

    release_id: int
    title: str
    artist_name: str


#: The report's derived figures, in the order a reader meets them. One roster
#: so ``to_dict`` cannot forget a property and the tests have a single list to
#: parametrize over -- a fifth derived figure is added here and nowhere else.
DERIVED_HEADLINE_KEYS = (
    "could_gain_recall_no_new_collection",
    "would_need_new_collection",
    "artist_shelf_without_exact_artist_match",
    "artist_shelf_matched_but_below_floor",
)


@dataclass(frozen=True)
class GapCensusReport:
    """The full LML#1264 measurement, ready to render or serialize.

    Measured counts only. The methodology caveats
    (``DOCUMENTED_PIN_COVERAGE_NOTE``, ``TRACKLIST_CHECK_CAVEAT``) are not
    fields: they describe how to read the numbers rather than being numbers,
    and ``to_dict`` attaches them alongside the other derived annotations.
    """

    total_library_rows: int
    comp_shelf_count: int
    comp_shelf_naive_like_count: int
    artist_shelf_count: int
    artist_shelf_with_exact_artist_match: int
    artist_shelf_with_resolvable_release: int
    artist_shelf_with_cached_tracklist: int
    discogs_source: str | None

    @property
    def discogs_leg_ran(self) -> bool:
        """Whether items 2-4 were measured at all, or skipped for want of a source."""
        return self.discogs_source is not None

    @property
    def could_gain_recall_no_new_collection(self) -> int | None:
        """Upper-bound count of rows resolvable + tracklisted with zero new collection."""
        return self.artist_shelf_with_cached_tracklist if self.discogs_leg_ran else None

    @property
    def would_need_new_collection(self) -> int | None:
        """Artist-shelf rows this census could not resolve to a cached tracklist."""
        if not self.discogs_leg_ran:
            return None
        return self.artist_shelf_count - self.artist_shelf_with_cached_tracklist

    @property
    def artist_shelf_without_exact_artist_match(self) -> int | None:
        """Rows with no Discogs release credited under ANY of their artist names.

        The first of the two populations inside ``would_need_new_collection``,
        and the one holding the compound-credit shape LML#1264 is about. No
        title-floor change reaches these rows -- there is no candidate to
        score.
        """
        if not self.discogs_leg_ran:
            return None
        return self.artist_shelf_count - self.artist_shelf_with_exact_artist_match

    @property
    def artist_shelf_matched_but_below_floor(self) -> int | None:
        """Rows that matched an artist but had no release title clear the 80/80 floor.

        The second population: candidates exist, none of them scored. This is
        the bucket a title-gate change could in principle move -- and the one
        where LML#1245 / LML#1250's wrong-artist hazards apply.
        """
        if not self.discogs_leg_ran:
            return None
        return self.artist_shelf_with_exact_artist_match - self.artist_shelf_with_resolvable_release

    def to_dict(self) -> dict:
        """Serialize the measurement together with the caveats that make it readable.

        Two things this deliberately will not do. It will not emit a headline
        the run did not measure: a library-only run (no ``--discogs-url``)
        leaves items 2-4 at zero, and serializing the derived headline off
        those zeroes would tell a reader who meets the JSON without the console
        output that NOTHING is resolvable, a measured-looking finding where
        nothing was tested. And it will not attach the tracklist caveat to a
        run that ran no tracklist check. The artifact outlives the run that
        produced it, so every number in it has to carry its own reading
        instructions -- and no reading instructions for a number that isn't
        there.
        """
        out = asdict(self)
        out["discogs_measurement"] = "measured" if self.discogs_leg_ran else "skipped"
        out.update({key: getattr(self, key) for key in DERIVED_HEADLINE_KEYS})
        out["pin_coverage_note"] = DOCUMENTED_PIN_COVERAGE_NOTE
        if self.discogs_leg_ran:
            out["tracklist_check_caveat"] = TRACKLIST_CHECK_CAVEAT
        return out


def load_library_rows(db_path: str) -> list[LibraryRow]:
    """Read every ``(id, artist, title, alternate_artist_name)`` row out of ``library.db``.

    ``alternate_artist_name`` is introspected rather than assumed, mirroring
    ``library/db.py::LibraryDB.connect``'s own ``PRAGMA table_info`` check (and
    ``scripts/measure_artwork_match_floor.py``, which does the same). Older ETL
    snapshots predate the column; against one of those the census degrades to
    an artist-column-only measurement -- narrower, and logged as such -- rather
    than dying on an ``OperationalError`` a long way into a run.
    """
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        columns = {row["name"] for row in con.execute("PRAGMA table_info(library)")}
        has_alternate = "alternate_artist_name" in columns
        if not has_alternate:
            log.warning(
                "library.db has no alternate_artist_name column -- matching on the artist "
                "column alone. This UNDERSTATES resolvable rows relative to the real prod "
                "cache filter; see artist_variants."
            )
        selected = "id, artist, title" + (", alternate_artist_name" if has_alternate else "")
        cur = con.execute(f"SELECT {selected} FROM library")
        return [
            LibraryRow(
                id=row["id"],
                artist=row["artist"] or "",
                title=row["title"] or "",
                alternate_artist=(row["alternate_artist_name"] or "") if has_alternate else "",
            )
            for row in cur.fetchall()
        ]
    finally:
        con.close()


def artist_variants(row: LibraryRow) -> list[str]:
    """Every library artist name a row can legitimately be matched under.

    Mirrors ``scripts/build_filtered_discogs.py::extract_library_artists``,
    which unions ``library.artist`` with ``library.alternate_artist_name`` --
    dropping compilation-artist strings from *both* columns -- before the
    exact-match join. That union is not cosmetic: 4,879 of the 64,815 rows in
    the 2026-07-19 prod snapshot carry an alternate name, and they skew hard
    toward the compound-credit shape LML#1264 is about ("Company Flow" /
    "Company Flow & Cannibal Ox", "Common" / "Common Sense"). Measuring on
    ``artist`` alone would file those rows under "no Discogs release credited
    under that artist at all" when the real prod cache admits them under the
    alternate name -- overstating the gap, and pointing the reader at the
    wrong remedy for it.
    """
    variants = [row.artist]
    alternate = row.alternate_artist.strip()
    if alternate and alternate not in variants and not is_compilation_artist(alternate):
        variants.append(alternate)
    return variants


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
    whatever database ``conn`` is connected to. ``artist_names`` should be the
    union of both library artist columns (see ``artist_variants``). Grouped by
    the ORIGINAL library artist string (not lower-cased), so callers can look
    candidates up by variant directly.

    Streams via a server-side cursor rather than ``conn.fetch``, which avoids
    buffering the whole result set before building the dict. Note this bounds
    the *transient* copy only: the returned dict itself holds every candidate
    (3.8M of them for this library against the full dump, ~620 MB after
    ``ReleaseCandidate.__slots__`` and the interning below), and nothing bounds
    that. Slicing ``artist_names`` is the seam if it ever needs bounding.

    ``sys.intern`` on the credit name is worth ~125 MB: asyncpg hands back a
    fresh ``str`` per row, but the 3.8M rows carry only ~20.5k distinct credits
    (187x repetition), and the interned name is what
    ``find_best_typed_match`` reads on every one of the resolve pass's
    scorings.
    """
    if not artist_names:
        return {}
    await conn.execute("CREATE TEMP TABLE IF NOT EXISTS census_artists (name TEXT)")
    await conn.execute("DELETE FROM census_artists")
    await conn.copy_records_to_table(
        "census_artists", records=[(a,) for a in artist_names], columns=["name"]
    )
    # Load-bearing, unlike the lower(name) index build_filtered_discogs.py
    # creates here: EXPLAIN against the full dump shows census_artists is
    # seq-scanned into the hash side either way (it is always the small
    # relation), so that index is dead -- but ANALYZE is what supplies the
    # row estimate that keeps it on the small side.
    await conn.execute("ANALYZE census_artists")
    grouped: dict[str, list[ReleaseCandidate]] = {}
    async with conn.transaction():
        async for row in conn.cursor(_EXACT_ARTIST_MATCH_SQL):
            grouped.setdefault(row["library_artist"], []).append(
                ReleaseCandidate(
                    release_id=row["release_id"],
                    title=row["release_title"],
                    artist_name=sys.intern(row["discogs_artist_name"]),
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
    bar that would overstate the gap's closability. The row's variant set
    goes in through that function's existing ``query_artist`` iterable
    support, which scores the candidate against each variant and keeps the
    max; the floor itself is untouched.
    """
    if not candidates:
        return None
    return find_best_typed_match(
        candidates,
        query_artist=artist_variants(row),
        query_title=row.title,
        artist_fn=lambda c: c.artist_name,
        title_fn=lambda c: c.title,
        key_fn=lambda c: c.release_id,
    )


async def find_tracklist_release_ids(conn: asyncpg.Connection, release_ids: list[int]) -> set[int]:
    """Which of ``release_ids`` carry at least one ``release_track`` row.

    Read the result through ``TRACKLIST_CHECK_CAVEAT``.
    """
    if not release_ids:
        return set()
    rows = await conn.fetch(_TRACKLIST_PRESENCE_SQL, release_ids)
    return {row["release_id"] for row in rows}


async def measure_discogs_side(
    conn: asyncpg.Connection, artist_shelf: list[LibraryRow]
) -> tuple[int, int, int]:
    """Items 2+3 of the census: exact-artist / resolvable / tracklisted counts.

    Matches on the union of both library artist columns, as the real prod
    cache-build filter does -- see ``artist_variants``.
    """
    variants_by_row = [artist_variants(row) for row in artist_shelf]
    distinct_artists = sorted({name for variants in variants_by_row for name in variants})
    log.info("Matching %d distinct artist-shelf artist names...", len(distinct_artists))
    candidates_by_artist = await find_exact_artist_candidates(conn, distinct_artists)
    log.info(
        "%d/%d distinct artists have an exact-case-folded Discogs match",
        len(candidates_by_artist),
        len(distinct_artists),
    )

    # One pass. A row is in ``candidates_by_artist`` under some variant exactly
    # when that variant yielded at least one candidate, so "has an exact artist
    # match" is just "this row's candidate list is non-empty" -- counting it
    # here rather than in a second sweep keeps the two from drifting apart.
    exact_artist_match_count = 0
    resolved_release_ids: list[int] = []
    for i, (row, variants) in enumerate(zip(artist_shelf, variants_by_row, strict=True), start=1):
        candidates = [c for name in variants for c in candidates_by_artist.get(name, [])]
        if candidates:
            exact_artist_match_count += 1
        match = resolve_release_for_row(row, candidates)
        if match is not None:
            resolved_release_ids.append(match.release_id)
        if i % 5000 == 0:
            log.info("Resolved %d/%d artist-shelf rows so far...", i, len(artist_shelf))
    log.info(
        "Resolved %d/%d artist-shelf rows to a specific release at the 80/80 floor",
        len(resolved_release_ids),
        len(artist_shelf),
    )

    tracklisted_ids = await find_tracklist_release_ids(conn, sorted(set(resolved_release_ids)))
    with_tracklist = sum(1 for rid in resolved_release_ids if rid in tracklisted_ids)
    log.info(
        "%d/%d resolved releases carry a cached tracklist -- %s",
        with_tracklist,
        len(resolved_release_ids),
        TRACKLIST_CHECK_CAVEAT,
    )
    return exact_artist_match_count, len(resolved_release_ids), with_tracklist


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


def _render_lines(lines: list[str | tuple[str, int | None]]) -> str:
    """Render prose lines as-is and ``(label, value)`` pairs into one aligned column.

    The column width is derived from the labels actually present, so adding or
    renaming a row can't silently misalign the table -- the failure mode of the
    hand-padded f-strings this replaced.
    """
    width = max((len(line[0]) for line in lines if isinstance(line, tuple)), default=0)
    return "\n".join(
        line if isinstance(line, str) else f"{line[0]:<{width + 2}}{line[1]:>8,}" for line in lines
    )


def render_report(report: GapCensusReport) -> str:
    """Console-friendly summary. Every Discogs-sourced number is labeled
    an upper bound; the pin-coverage figure is labeled as documented,
    not measured."""
    lines: list[str | tuple[str, int | None]] = [
        "LML#1264 track-recall gap census",
        "=================================",
        ("Total library rows:", report.total_library_rows),
        ("Comp-shelf (classifier):", report.comp_shelf_count),
        ("  same, by the naive LIKE heuristic:", report.comp_shelf_naive_like_count),
        ("Artist-shelf:", report.artist_shelf_count),
        "",
    ]
    if not report.discogs_leg_ran:
        lines.append("Discogs-side measurement SKIPPED (no --discogs-url given).")
    else:
        lines.extend(
            [
                f"Discogs source: {report.discogs_source}",
                "UPPER BOUND -- computed against an unfiltered Discogs dump, not the",
                "filtered prod discogs-cache; see the module docstring's TRAP note.",
                ("  exact-artist-match:", report.artist_shelf_with_exact_artist_match),
                ("  resolvable (80/80 floor):", report.artist_shelf_with_resolvable_release),
                ("  + cached tracklist:", report.artist_shelf_with_cached_tracklist),
                f"    ^ {TRACKLIST_CHECK_CAVEAT}",
                "",
                "Headline (UPPER BOUND):",
                (
                    "  could gain recall, no new collection:",
                    report.could_gain_recall_no_new_collection,
                ),
                ("  would need new collection:", report.would_need_new_collection),
                "",
                "...and those 'would need new collection' rows are two populations, two remedies:",
                (
                    "  no Discogs release under any of their artist names:",
                    report.artist_shelf_without_exact_artist_match,
                ),
                (
                    "  matched an artist, no title cleared the floor:",
                    report.artist_shelf_matched_but_below_floor,
                ),
            ]
        )
    lines.append("")
    lines.append(f"Pin coverage (documented, NOT measured here): {DOCUMENTED_PIN_COVERAGE_NOTE}")
    return _render_lines(lines)


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
    # Configured here, not at import time: importing this module for its pure
    # helpers (the unit suite does) must not reconfigure root logging.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    load_dotenv()
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
