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
2. Of the artist-shelf rows, how many can the Discogs cache hold a release for
   *at all*? This is the **structural** question, and it is answered by
   simulating the production cache filter (``LibraryPairIndex``) against the
   Discogs source -- not by asking whether a matcher would score something.
3. Of the releases that filter admits, how many artist-shelf rows resolve to a
   specific one at the same 80/80 floor ``clients/streaming/matching.py::
   find_best_typed_match`` already applies at request time for artwork
   resolution (LML#478); and how many of those carry a cached tracklist.
   **Against a full Discogs source the tracklist check is non-discriminating**
   -- see ``TRACKLIST_CHECK_CAVEAT``.
4. The headline: how many artist-shelf rows could gain track-level recall with
   **no new data collection**, vs. how many would need some -- with the latter
   split by *which* remedy would move it.

WHICH FILTER THIS MIRRORS, AND WHICH ONE IT DELIBERATELY DOES NOT
-----------------------------------------------------------------
The production Discogs cache is built by ``discogs-etl``'s
``rebuild-cache.sh``, which forwards ``--library-db`` to
``discogs-xml-converter``. That binary admits a release through
``src/library_pairs.rs::LibraryPairs``: an inverted index
``normalized_title -> {normalized_artist}`` built from ``SELECT artist, title
FROM library``, probed with the release's title and *every* credited artist
(``release.artists`` chained with ``release.extra_artists``, i.e. both
``extra = 0`` and ``extra = 1``). Pair-wise, both sides folded through
``wxyc_etl::text::to_match_form``, primary ``library.artist`` column only.
``LibraryPairIndex`` below is that rule, in Python.

``scripts/build_filtered_discogs.py``, in this repo, is **not** that filter.
It is a local dev tool: artist-only, unions ``alternate_artist_name``, and
writes a ``wxyc.*`` schema LML never queries (LML's runtime SQL is
unqualified). Mirroring it -- which the first version of this census did, and
which a code review then pushed it further toward -- measures a database
nobody runs, and overstates coverage by roughly the difference between a ~4M
release cache and a ~50K one. ``tests/unit/
test_measure_track_recall_gap_filter_parity.py`` holds that distinction in
place behaviourally, because prose did not.

THE ONE PLACE THE TWO LEGS DELIBERATELY DISAGREE
------------------------------------------------
Admission (item 2) reads ``library.artist`` alone, because the production
filter does. Resolution (item 3) reads ``artist_variants`` -- the union with
``library.alternate_artist_name`` -- because LML's own runtime matcher does
(``artist_matches_item``). That asymmetry is not an oversight to be tidied
away: it *is* the census's finding. The substrate that decides what LML can
see folds names more weakly than the consumer that searches it, which is the
wrong way round, and the two legs are kept on different artist sets so the
script states that rather than hiding it.

TRAP (read before trusting any Discogs-side number): the source here is a
full, unscoped Discogs dump (``discogs_full`` locally). Running the production
filter over it yields **what the filter would admit from this dump vintage**,
which is an upper bound on the real prod cache -- the cache is built from a
different dump vintage and is further pruned after the fact (``verify_cache``
drops releases on Discogs ANV mismatch). A row this census calls admissible
may still be absent from prod today. The direction of the error is safe for
the structural finding: a row the filter admits *nothing* for from the full
dump cannot be in any cache built from any vintage of it.

The ``lml_cache.library_release_override`` pin table lives only in the shared
prod discogs-cache Postgres; this script does not query prod (out of scope,
needs explicit approval), so pin coverage is reported from the documented code
comment in ``identity/bulk_resolve.py`` -- NOT independently re-measured here.

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
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

import asyncpg
from dotenv import load_dotenv
from wxyc_etl.text import is_compilation_artist, to_match_form

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

# Travels with every Discogs-side figure, because the question a reader will
# ask of any of them ("is this the real cache?") has the same answer.
ADMISSION_MODEL_NOTE = (
    "Admission simulates discogs-xml-converter's LibraryPairs pair-wise (artist, title) "
    "filter -- the rule that builds the prod cache -- against a FULL unscoped Discogs dump. "
    "It is NOT scripts/build_filtered_discogs.py, which is a local dev tool with a different "
    "(artist-only) rule and a schema LML never queries. Numbers are an upper bound on the real "
    "cache: prod is built from a different dump vintage and pruned afterwards by verify_cache."
)

# Travels with the tracklist figure everywhere it is rendered or serialized,
# because that figure's most likely misreading ("~100%, so track data is
# solved") is also its most damaging one.
TRACKLIST_CHECK_CAVEAT = (
    "NON-DISCRIMINATING against a full Discogs source -- 19,341,286 of 19,341,287 releases in "
    "the 2026-08 local dump carry release_track rows, so this tracks the resolvable count "
    "whatever the real state of track data. Not a coverage finding. The constraint that "
    "actually binds is admission, measured above."
)

_TITLE_SCAN_SQL = "SELECT id, title FROM release"

# Both credit tiers, deliberately. ``matches_filter`` in discogs-xml-converter's
# main.rs chains ``release.artists`` (extra=0) with ``release.extra_artists``
# (extra=1) before probing the pair index, so a release whose only
# library-matching credit is a producer or remixer IS in the cache. Restricting
# this to ``extra = 0`` -- the habit inherited from the artist-only filter --
# would narrow admission below production and overstate the structural gap.
_ADMISSION_CREDIT_SQL = """
    SELECT ra.release_id, ra.artist_name
    FROM census_release_ids ids
    JOIN release_artist ra ON ra.release_id = ids.release_id
"""

_TRACKLIST_PRESENCE_SQL = """
    SELECT DISTINCT release_id FROM release_track WHERE release_id = ANY($1::int[])
"""


@dataclass(frozen=True)
class LibraryRow:
    """One ``library.db`` row: just the columns this census needs.

    ``alternate_artist`` carries ``library.alternate_artist_name``. It feeds the
    resolve leg only, never admission -- see the module docstring.
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
    """One admitted Discogs release, seen through one of its credits.

    ``slots=True`` and the ``sys.intern`` in ``build_admitted_universe`` are not
    incidental: the admitted universe is held live for the whole resolve pass,
    and one release contributes one of these per credit.
    """

    release_id: int
    title: str
    artist_name: str


@dataclass(frozen=True)
class LibraryPairIndex:
    """The production cache filter, in Python.

    A faithful port of ``discogs-xml-converter``'s
    ``src/library_pairs.rs::LibraryPairs``: an inverted index
    ``normalized_title -> {normalized_artist}``, built from every library row's
    ``(artist, title)``, probed with a release's title and its credited artist
    names. Both sides fold through ``wxyc_etl.text.to_match_form``.

    Three properties of the real rule that are easy to "improve" and must not
    be: it reads ``library.artist`` only (never ``alternate_artist_name``); it
    excludes no row for being a compilation (a shelf label is only ever half of
    a pair, so it cannot admit a release on its own); and it drops rows whose
    artist or title folds to empty.
    """

    pairs: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def from_library_rows(cls, rows: Iterable[LibraryRow]) -> LibraryPairIndex:
        building: dict[str, set[str]] = {}
        for row in rows:
            n_title = to_match_form(row.title)
            n_artist = to_match_form(row.artist)
            if not n_title or not n_artist:
                continue
            building.setdefault(n_title, set()).add(n_artist)
        return cls(pairs={title: frozenset(artists) for title, artists in building.items()})

    def __len__(self) -> int:
        """Distinct normalized titles -- the index's key count."""
        return len(self.pairs)

    @property
    def pair_count(self) -> int:
        """Total ``(title, artist)`` pairs spanned. Diagnostic only."""
        return sum(len(artists) for artists in self.pairs.values())

    def artists_for_title(self, normalized_title: str) -> frozenset[str]:
        """The artist set for an already-folded title; empty if the title is unknown."""
        return self.pairs.get(normalized_title, frozenset())

    def admits(self, title: str, artist_names: Iterable[str]) -> bool:
        """Whether the prod cache filter would admit this release.

        ``title`` and ``artist_names`` are the raw values as they appear on the
        Discogs release; folding both sides here is what lets a
        diacritic-mismatched pair still collide.
        """
        library_artists = self.artists_for_title(to_match_form(title))
        if not library_artists:
            return False
        return any(to_match_form(name) in library_artists for name in artist_names)


@dataclass(frozen=True)
class AdmittedUniverse:
    """Every release the production filter admits, indexed for the census.

    ``by_artist`` is keyed by *folded credit name* and carries every credit of
    an admitted release, not only the one that won it admission -- a library row
    whose Discogs credit differs from the admitting one still needs something to
    score. ``admitted_pairs`` holds the folded ``(artist, title)`` pairs, which
    is how a row asks whether the cache can hold a release for **it**, as
    opposed to for some sibling shelf row.
    """

    by_artist: dict[str, list[ReleaseCandidate]]
    admitted_pairs: set[tuple[str, str]]
    release_count: int


@dataclass(frozen=True)
class DiscogsLegCensus:
    """Items 2-3, measured. Exists only when a Discogs source was given.

    Every figure here is a *measured* count, none derived by subtraction: the
    report's derived splits are differences of these, and a difference is only
    trustworthy when both operands were counted in the same pass over the same
    rows.
    """

    source: str
    admitted_release_count: int
    pair_admitted: int
    pair_admitted_and_resolvable: int
    resolvable: int
    with_cached_tracklist: int


#: The report's derived figures, in the order a reader meets them. One roster
#: so ``to_dict`` cannot forget a property and the tests have a single list to
#: parametrize over -- a sixth derived figure is added here and nowhere else.
DERIVED_HEADLINE_KEYS = (
    "could_gain_recall_no_new_collection",
    "would_need_new_collection",
    "artist_shelf_structural_and_unreached",
    "artist_shelf_pair_admitted_but_below_floor",
    "artist_shelf_resolvable_without_cached_tracklist",
    "artist_shelf_not_pair_admitted",
    "artist_shelf_resolvable_without_pair_admission",
)


@dataclass(frozen=True)
class GapCensusReport:
    """The full LML#1264 measurement, ready to render or serialize.

    The Discogs leg is one optional structure rather than a handful of fields
    that go to zero, so a library-only run cannot serialize an unmeasured zero
    as though it were a finding. The methodology caveats
    (``ADMISSION_MODEL_NOTE``, ``TRACKLIST_CHECK_CAVEAT``,
    ``DOCUMENTED_PIN_COVERAGE_NOTE``) are not fields either: they describe how
    to read the numbers rather than being numbers, and ``to_dict`` attaches them
    alongside the derived figures they qualify.
    """

    total_library_rows: int
    comp_shelf_count: int
    comp_shelf_naive_like_count: int
    artist_shelf_count: int
    discogs: DiscogsLegCensus | None

    @property
    def could_gain_recall_no_new_collection(self) -> int | None:
        """Artist-shelf rows resolvable to a tracklisted release, no new collection."""
        return None if self.discogs is None else self.discogs.with_cached_tracklist

    @property
    def would_need_new_collection(self) -> int | None:
        """Artist-shelf rows this census could not resolve to a cached tracklist."""
        if self.discogs is None:
            return None
        return self.artist_shelf_count - self.discogs.with_cached_tracklist

    @property
    def artist_shelf_not_pair_admitted(self) -> int | None:
        """Rows the cache can hold no release for under their OWN pair.

        The headline structural figure, and the one quoted into tickets. Note it
        is deliberately NOT one of the three remedy populations below: a handful
        of these rows still resolve against a release admitted under some other
        library row's pair, so this count overlaps
        ``could_gain_recall_no_new_collection`` by exactly
        ``artist_shelf_resolvable_without_pair_admission``. Subtracting that
        overlap gives ``artist_shelf_structural_and_unreached``, which is the
        one that partitions.
        """
        if self.discogs is None:
            return None
        return self.artist_shelf_count - self.discogs.pair_admitted

    @property
    def artist_shelf_structural_and_unreached(self) -> int | None:
        """Rows no cached release reaches at all -- the first remedy population.

        The largest and hardest of the three. No threshold, fold, or index
        inside LML reaches these rows: there is no candidate to score, because
        the filter admitted nothing under their pair and no sibling row's
        admission happened to cover them either. Only a wider cache filter (see
        discogs-etl#414) or new data upstream moves them.
        """
        if self.discogs is None:
            return None
        return (
            self.artist_shelf_count
            - self.discogs.pair_admitted
            - (self.discogs.resolvable - self.discogs.pair_admitted_and_resolvable)
        )

    @property
    def artist_shelf_pair_admitted_but_below_floor(self) -> int | None:
        """Rows whose release IS admitted, yet no candidate cleared the 80/80 floor.

        The opposite case, and the one a matcher change actually moves -- with
        the wrong-artist hazards LML#1245 / LML#1250 document. Expected to be
        small: admission requires the folded title and artist to match exactly,
        so an admitted row's own release normally scores at or near 100.
        """
        if self.discogs is None:
            return None
        return self.discogs.pair_admitted - self.discogs.pair_admitted_and_resolvable

    @property
    def artist_shelf_resolvable_without_pair_admission(self) -> int | None:
        """Rows rescued by a release admitted under some *other* library row's pair.

        A shelf row whose own pair is absent can still resolve fuzzily against a
        release the cache holds for a sibling row -- a different pressing title,
        or the alternate-name credit the admission leg refused to read. These are
        the rows where the substrate-stricter-than-consumer asymmetry pays off,
        and counting them is the only way to know the asymmetry matters.
        """
        if self.discogs is None:
            return None
        return self.discogs.resolvable - self.discogs.pair_admitted_and_resolvable

    @property
    def artist_shelf_resolvable_without_cached_tracklist(self) -> int | None:
        """Rows that resolve to a release carrying no tracklist -- the third remedy.

        Zero on a full-dump run (see ``TRACKLIST_CHECK_CAVEAT``), and the line
        exists anyway. Without it the remedy split only balances when
        ``with_cached_tracklist`` happens to equal ``resolvable``, which is an
        artifact of the source rather than a property of the census -- against a
        real filtered cache it would not hold, and the split would silently stop
        adding up.
        """
        if self.discogs is None:
            return None
        return self.discogs.resolvable - self.discogs.with_cached_tracklist

    def to_dict(self) -> dict:
        """Serialize the measurement together with the caveats that make it readable.

        A library-only run emits no Discogs figure and no caveat about one: the
        artifact outlives the run that produced it, so every number in it has to
        carry its own reading instructions -- and no reading instructions for a
        number that isn't there.
        """
        out = asdict(self)
        out.pop("discogs")
        out["discogs_measurement"] = "measured" if self.discogs else "skipped"
        out.update({key: getattr(self, key) for key in DERIVED_HEADLINE_KEYS})
        out["pin_coverage_note"] = DOCUMENTED_PIN_COVERAGE_NOTE
        if self.discogs is not None:
            out["discogs_source"] = self.discogs.source
            out["admitted_release_count"] = self.discogs.admitted_release_count
            out["artist_shelf_pair_admitted"] = self.discogs.pair_admitted
            out["artist_shelf_with_resolvable_release"] = self.discogs.resolvable
            out["artist_shelf_with_cached_tracklist"] = self.discogs.with_cached_tracklist
            out["admission_model_note"] = ADMISSION_MODEL_NOTE
            out["tracklist_check_caveat"] = TRACKLIST_CHECK_CAVEAT
        return out


def load_library_rows(db_path: str) -> list[LibraryRow]:
    """Read every ``(id, artist, title, alternate_artist_name)`` row out of ``library.db``.

    ``alternate_artist_name`` is introspected rather than assumed, mirroring
    ``library/db.py::LibraryDB.connect``'s own ``PRAGMA table_info`` check (and
    ``scripts/measure_artwork_match_floor.py``, which does the same). Older ETL
    snapshots predate the column; against one of those the census degrades to
    an artist-column-only measurement -- narrower on the resolve leg, identical
    on admission -- rather than dying on an ``OperationalError`` a long way into
    a run.
    """
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        columns = {row["name"] for row in con.execute("PRAGMA table_info(library)")}
        has_alternate = "alternate_artist_name" in columns
        if not has_alternate:
            log.warning(
                "library.db has no alternate_artist_name column -- the resolve leg will match "
                "on the artist column alone, UNDERSTATING what LML's runtime matcher can reach. "
                "Admission is unaffected; the prod filter never reads that column."
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
    """Every artist name LML's RESOLVE leg may match a row under.

    Models the consumer, not the substrate. ``artist_matches_item`` in
    ``lookup/orchestrator.py`` matches a library row on either its ``artist`` or
    its ``alternate_artist_name``, so the census's resolve leg does too --
    4,879 of the 64,815 rows in the 2026-07-19 prod snapshot carry an alternate
    name, and they skew hard toward the compound-credit shape LML#1264 is about
    ("Company Flow" / "Company Flow & Cannibal Ox", "Common" / "Common Sense").

    A compilation string in either column is dropped: it is a shelf label, not
    a credit any release would be matched against.

    This must NOT be used for admission. ``LibraryPairIndex`` reads
    ``library.artist`` alone because the production filter does; feeding it
    these variants would credit the cache with releases it does not hold.
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


async def find_title_matched_releases(
    conn: asyncpg.Connection, index: LibraryPairIndex
) -> dict[int, str]:
    """Pass 1 of admission: every release whose folded title is a library title.

    The production filter's inverted index is keyed by title, so this is the
    same first probe, run over the whole catalogue. Streaming all ~19.3M rows
    and folding each title in Python is deliberate: no SQL expression is
    equivalent to ``to_match_form`` (``unaccent()`` is neither a superset nor a
    subset of NFKD-plus-strip-combining), so any server-side prefilter would
    silently drop diacritic-differing pairs -- exactly the collisions the fold
    exists to make. ~60s against the 2026-08 local dump.

    Returns ``{release_id: raw title}``; the raw title is kept because the
    resolve leg scores against it, not against the folded form.
    """
    if not index.pairs:
        return {}
    matched: dict[int, str] = {}
    scanned = 0
    async with conn.transaction():
        async for row in conn.cursor(_TITLE_SCAN_SQL, prefetch=50_000):
            scanned += 1
            title = row["title"] or ""
            if to_match_form(title) in index.pairs:
                matched[row["id"]] = title
            if scanned % 5_000_000 == 0:
                log.info("Scanned %d releases, %d title matches so far...", scanned, len(matched))
    log.info("Title-match pass: %d/%d releases carry a library title", len(matched), scanned)
    return matched


async def build_admitted_universe(
    conn: asyncpg.Connection, index: LibraryPairIndex, title_matched: dict[int, str]
) -> AdmittedUniverse:
    """Pass 2 of admission: of the title matches, which the pair rule admits.

    Sweeps every credit of every title-matched release -- both ``extra`` tiers,
    see ``_ADMISSION_CREDIT_SQL`` -- and admits a release when any of them lands
    in its title's artist set. Credits arrive grouped by release only by
    accident of the join order, so they are collected first and judged after.

    ``credits`` is the largest structure in the script and the only one that
    scales with the dump rather than the library: it holds every credit of every
    **title-matched** release (2,252,690 of them against the 2026-08 local dump,
    3.3x the 683,740 that end up admitted), because a release's admission cannot
    be decided until all of its credits have been seen. Measured peak RSS for
    the whole census is 1.55 GB, which is where the run sits -- nothing bounds
    it, and a library with more high-collision titles (``untitled``, ``1``,
    ``live``, ``greatest hits``) would push it up. Chunking ``title_matched`` and
    accumulating ``by_artist`` across chunks is the seam if it ever needs
    bounding; it costs nothing but code, since chunks are independent.

    ``sys.intern`` is what keeps that structure affordable: asyncpg hands back a
    fresh ``str`` per credit row, and the ~5M credits carry far fewer distinct
    names. The interned name is also what ``find_best_typed_match`` reads on
    every scoring of the resolve pass, which holds ``by_artist`` live throughout.
    """
    if not title_matched:
        return AdmittedUniverse(by_artist={}, admitted_pairs=set(), release_count=0)

    await conn.execute("CREATE TEMP TABLE IF NOT EXISTS census_release_ids (release_id INTEGER)")
    await conn.execute("DELETE FROM census_release_ids")
    await conn.copy_records_to_table(
        "census_release_ids", records=[(rid,) for rid in title_matched], columns=["release_id"]
    )
    await conn.execute("ANALYZE census_release_ids")

    credits: dict[int, list[str]] = {}
    async with conn.transaction():
        async for row in conn.cursor(_ADMISSION_CREDIT_SQL, prefetch=50_000):
            credits.setdefault(row["release_id"], []).append(sys.intern(row["artist_name"] or ""))

    by_artist: dict[str, list[ReleaseCandidate]] = {}
    admitted_pairs: set[tuple[str, str]] = set()
    admitted = 0
    for release_id, names in credits.items():
        title = title_matched[release_id]
        n_title = to_match_form(title)
        library_artists = index.artists_for_title(n_title)
        folded = [(name, to_match_form(name)) for name in names]
        if not any(n_name in library_artists for _, n_name in folded):
            continue
        admitted += 1
        for name, n_name in folded:
            by_artist.setdefault(n_name, []).append(
                ReleaseCandidate(release_id=release_id, title=title, artist_name=name)
            )
            if n_name in library_artists:
                admitted_pairs.add((n_name, n_title))
    log.info(
        "Admission pass: %d/%d title-matched releases admitted, spanning %d (artist, title) pairs",
        admitted,
        len(title_matched),
        len(admitted_pairs),
    )
    return AdmittedUniverse(
        by_artist=by_artist, admitted_pairs=admitted_pairs, release_count=admitted
    )


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
    conn: asyncpg.Connection, source: str, rows: list[LibraryRow], artist_shelf: list[LibraryRow]
) -> DiscogsLegCensus:
    """Items 2-3: simulate the prod filter, then resolve the artist shelf into it.

    The index is built from **every** library row, not just the artist shelf,
    because the real filter is -- a comp-shelf row's pair admits releases too,
    and those releases are in the cache for an artist-shelf row to match
    against.
    """
    index = LibraryPairIndex.from_library_rows(rows)
    log.info(
        "Library pair index: %d distinct titles spanning %d (artist, title) pairs",
        len(index),
        index.pair_count,
    )

    title_matched = await find_title_matched_releases(conn, index)
    universe = await build_admitted_universe(conn, index, title_matched)

    pair_admitted = 0
    pair_admitted_and_resolvable = 0
    resolved_release_ids: list[int] = []
    for i, row in enumerate(artist_shelf, start=1):
        own_pair = (to_match_form(row.artist), to_match_form(row.title))
        admitted = own_pair in universe.admitted_pairs
        pair_admitted += admitted
        candidates = [
            c
            for name in artist_variants(row)
            for c in universe.by_artist.get(to_match_form(name), [])
        ]
        match = resolve_release_for_row(row, candidates)
        if match is not None:
            resolved_release_ids.append(match.release_id)
            pair_admitted_and_resolvable += admitted
        if i % 5000 == 0:
            log.info("Resolved %d/%d artist-shelf rows so far...", i, len(artist_shelf))
    log.info(
        "%d/%d artist-shelf rows are pair-admitted; %d resolve at the 80/80 floor",
        pair_admitted,
        len(artist_shelf),
        len(resolved_release_ids),
    )

    tracklisted_ids = await find_tracklist_release_ids(conn, sorted(set(resolved_release_ids)))
    with_tracklist = sum(1 for rid in resolved_release_ids if rid in tracklisted_ids)
    log.info(
        "%d/%d resolved releases carry a cached tracklist -- %s",
        with_tracklist,
        len(resolved_release_ids),
        TRACKLIST_CHECK_CAVEAT,
    )
    return DiscogsLegCensus(
        source=source,
        admitted_release_count=universe.release_count,
        pair_admitted=pair_admitted,
        pair_admitted_and_resolvable=pair_admitted_and_resolvable,
        resolvable=len(resolved_release_ids),
        with_cached_tracklist=with_tracklist,
    )


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
        leg = None
    else:
        conn = await asyncpg.connect(discogs_url)
        try:
            leg = await measure_discogs_side(conn, discogs_url, rows, census.artist_shelf)
        finally:
            await conn.close()

    return GapCensusReport(
        total_library_rows=census.total,
        comp_shelf_count=len(census.comp_shelf),
        comp_shelf_naive_like_count=census.comp_shelf_naive_like_count,
        artist_shelf_count=len(census.artist_shelf),
        discogs=leg,
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
    """Console-friendly summary. Every Discogs-sourced number carries the model
    it was simulated under; the pin-coverage figure is labeled as documented,
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
    if report.discogs is None:
        lines.append("Discogs-side measurement SKIPPED (no --discogs-url given).")
    else:
        lines.extend(
            [
                f"Discogs source: {report.discogs.source}",
                ADMISSION_MODEL_NOTE,
                ("Releases the prod filter admits:", report.discogs.admitted_release_count),
                "",
                "Artist-shelf rows:",
                ("  pair-admitted (a cached release CAN exist):", report.discogs.pair_admitted),
                ("  resolvable (80/80 floor, admitted universe):", report.discogs.resolvable),
                ("  + cached tracklist:", report.discogs.with_cached_tracklist),
                f"    ^ {TRACKLIST_CHECK_CAVEAT}",
                "",
                "Headline:",
                (
                    "  could gain recall, no new collection:",
                    report.could_gain_recall_no_new_collection,
                ),
                ("  would need new collection:", report.would_need_new_collection),
                "",
                "...and those 'would need new collection' rows partition three ways,",
                "three different remedies (the three below sum to it exactly):",
                (
                    "  STRUCTURAL -- no cached release reaches this row:",
                    report.artist_shelf_structural_and_unreached,
                ),
                (
                    "  a release IS admitted, no title cleared the floor:",
                    report.artist_shelf_pair_admitted_but_below_floor,
                ),
                (
                    "  resolves, but that release has no cached tracklist:",
                    report.artist_shelf_resolvable_without_cached_tracklist,
                ),
                "",
                "Structural gap counted the other way -- rows with no release under",
                "their OWN pair, including the few a sibling row's admission rescues:",
                ("  not pair-admitted:", report.artist_shelf_not_pair_admitted),
                (
                    "  of which rescued (so NOT in the split above):",
                    report.artist_shelf_resolvable_without_pair_admission,
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
