"""Measure ``find_similar_artist`` on both axes it trades between (LML#1245).

Committed rather than run ad hoc, because the number this produces is the
whole argument for the change and a reviewer has to be able to re-derive it.
Five measurement designs in this repo have now been wrong in the same way --
a population that structurally could not contain the failure it was built to
catch -- so this script's first job is to declare where each side of every
comparison comes from.

**Sources, per axis, separately.** This is the property that matters:

* *Precision* -- the catalog side is half of ``library.db`` (partitioned by
  md5 of the name, so the split is deterministic and reproducible); the query
  side is real artist names from the **other** half. Every probe is therefore
  an artist the pool genuinely does not contain, and any non-``None`` answer
  is a false correction. Sampling both sides from the same half is the defect
  that made PR #1249's original sweep report "Regressions: 0" -- with the true
  match always present, the failure class cannot occur.
* *Recall* -- the catalog side is that same half; the query side is
  **synthetic**: one keyboard-adjacent substitution, deletion, or
  transposition applied to a catalog name of 6+ characters, discarding any
  mutation that collides with a real catalog name. This is a modelling
  assumption, not an observation, and it is the weakest part of the
  measurement. Production carries no query text -- ``lookup_completed`` has
  ``had_artist``/``had_album``/``had_song`` as booleans and no string, not
  even a hash -- so the real typo distribution is unobservable today. Read
  every recall figure as "under this corpus", never as "in production".

Both axes run the real ``LibraryDB.find_similar_artist``, over a real SQLite
catalog built from the sampled half. Nothing here forks a production
predicate, so there is no copy to drift: the parity test that ticket LML#1275
asks of a forked harness is unnecessary by construction, which is the better
answer to it.

Usage::

    python -m scripts.measure_artist_correction --library-db path/to/library.db
    python -m scripts.measure_artist_correction --sweep     # margin x edit-cap grid

Exit code is always 0; this reports, it does not gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import random
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import library.db as library_db
from library.db import LIBRARY_FTS_CREATE_SQL, LibraryDB, clear_library_caches

_HAS_GUARDS = hasattr(library_db, "ARTIST_CORRECTION_MARGIN_POINTS")
"""Whether the module under test carries the LML#1245 guards at all.

False when this script is copied into a checkout that predates them -- which
is how the ``main`` baseline row is produced. Measuring the old code with the
same corpus, on the same catalog, is the only comparison that means anything;
quoting a figure measured elsewhere against a differently-shaped pool is how
three PRs in this window ended up contradicting each other.
"""

logger = logging.getLogger("measure_artist_correction")

# Keyboard adjacency for the substitution mutation. QWERTY neighbours only --
# a random letter models a different error than a slipped finger, and the
# latter is what "typo" is meant to mean here.
_ADJACENT = {
    "a": "qwsz",
    "b": "vghn",
    "c": "xdfv",
    "d": "serfcx",
    "e": "wsdr",
    "f": "drtgvc",
    "g": "ftyhbv",
    "h": "gyujnb",
    "i": "ujko",
    "j": "huikmn",
    "k": "jiolm",
    "l": "kop",
    "m": "njk",
    "n": "bhjm",
    "o": "iklp",
    "p": "ol",
    "q": "wa",
    "r": "edft",
    "s": "awedxz",
    "t": "rfgy",
    "u": "yhji",
    "v": "cfgb",
    "w": "qase",
    "x": "zsdc",
    "y": "tghu",
    "z": "asx",
}

_MIN_TYPO_LENGTH = 6
"""Below this, a single edit is proportionally huge and the short-name
effective threshold refuses the correction by design (LML#626's "Plug").
Including such names would measure that guard, not this one."""


@dataclass(frozen=True)
class AxisResult:
    probes: int
    hits: int

    @property
    def rate_per_thousand(self) -> float:
        return 1000.0 * self.hits / self.probes if self.probes else 0.0

    @property
    def percent(self) -> float:
        return 100.0 * self.hits / self.probes if self.probes else 0.0


def _catalog_names(db_path: Path) -> list[str]:
    """Every distinct artist name the pool would draw from, in the same shape.

    Reads the same source columns ``LibraryDB._artist_name_sources`` gates on,
    so the sampled universe matches the pool under test rather than a subset
    of it.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(library)")]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        queries = ["SELECT DISTINCT artist FROM library WHERE artist IS NOT NULL"]
        if "alternate_artist_name" in columns:
            queries.append(
                "SELECT DISTINCT alternate_artist_name FROM library "
                "WHERE alternate_artist_name IS NOT NULL"
            )
        if "album_artist" in columns:
            queries.append(
                "SELECT DISTINCT album_artist FROM library WHERE album_artist IS NOT NULL"
            )
        if "compilation_track_artist" in tables:
            queries.append(
                "SELECT DISTINCT artist_name FROM compilation_track_artist "
                "WHERE artist_name IS NOT NULL"
            )
        seen: dict[str, None] = {}
        for sql in queries:
            for (name,) in conn.execute(sql):
                if isinstance(name, str) and name.strip():
                    seen.setdefault(name, None)
        return list(seen)
    finally:
        conn.close()


def _partition(names: list[str]) -> tuple[list[str], list[str]]:
    """Split deterministically by md5 parity so a re-run measures the same halves."""
    pool: list[str] = []
    holdout: list[str] = []
    for name in names:
        digest = hashlib.md5(name.encode("utf-8")).digest()[0]
        (pool if digest % 2 == 0 else holdout).append(name)
    return pool, holdout


def _write_catalog(names: list[str], directory: Path) -> Path:
    """A real library.db holding exactly ``names``, so the real code path runs."""
    db_path = directory / "pool.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE library (id INTEGER PRIMARY KEY, title TEXT, artist TEXT, "
        "call_letters TEXT, artist_call_number INTEGER, release_call_number INTEGER, "
        "genre TEXT, format TEXT)"
    )
    conn.execute(LIBRARY_FTS_CREATE_SQL)
    conn.executemany(
        "INSERT INTO library VALUES (?, ?, ?, 'A', ?, ?, 'Rock', 'LP')",
        [(i, f"Album {i}", name, i, i) for i, name in enumerate(names, start=1)],
    )
    conn.commit()
    conn.close()
    return db_path


def _make_typo(name: str, rng: random.Random) -> str | None:
    """One keyboard-adjacent substitution, deletion, or transposition."""
    positions = [i for i, ch in enumerate(name) if ch.lower() in _ADJACENT]
    if not positions:
        return None
    kind = rng.choice(("substitute", "delete", "transpose"))
    chars = list(name)
    if kind == "substitute":
        i = rng.choice(positions)
        chars[i] = rng.choice(_ADJACENT[chars[i].lower()])
    elif kind == "delete":
        del chars[rng.choice(positions)]
    else:
        candidates = [i for i in positions if i + 1 < len(chars) and chars[i] != chars[i + 1]]
        if not candidates:
            return None
        i = rng.choice(candidates)
        chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


async def _connect_for_measurement(db_path: Path) -> LibraryDB:
    """Connect, and refuse to measure a pool that did not fully build.

    An unusable pool suppresses fuzzy correction entirely, and this script
    runs at ``logging.ERROR``, so the build's warning never prints. Left
    unchecked, a schema drift in ``_write_catalog`` or
    ``LibraryDB._artist_name_sources`` reads as 0.0 false corrections at
    0.0% typo recall on every grid row -- perfect-looking garbage a reviewer
    re-deriving the PR's numbers has no way to spot. A degraded pool aborts
    the run instead. Pre-pool checkouts have no ``_artist_name_pool_usable``
    attribute and connect unchecked, which is also correct: their LIKE path
    never reads a pool.
    """
    clear_library_caches()
    db = LibraryDB(db_path=db_path)
    await db.connect()
    if getattr(db, "_artist_name_pool_usable", True) is False:
        await db.close()
        raise RuntimeError(
            "artist-name pool failed to build against the measurement catalog; "
            "every figure this run printed would be 0.0. Fix the catalog schema "
            "(or _artist_name_sources drift) before measuring."
        )
    return db


async def _measure(
    db_path: Path, probes: list[str], expect_correction: bool
) -> tuple[AxisResult, list[tuple[str, str]]]:
    """Run ``probes`` through the real correction path; count the outcomes."""
    db = await _connect_for_measurement(db_path)
    try:
        hits = 0
        examples: list[tuple[str, str]] = []
        for probe in probes:
            answer = await db.find_similar_artist(probe)
            if expect_correction:
                if answer is not None:
                    hits += 1
            elif answer is not None:
                hits += 1
                if len(examples) < 12:
                    examples.append((probe, answer))
        return AxisResult(len(probes), hits), examples
    finally:
        await db.close()


async def _run_variant(
    pool_db: Path,
    holdout_probes: list[str],
    typo_probes: list[tuple[str, str]],
) -> tuple[AxisResult, AxisResult, list[tuple[str, str]]]:
    precision, examples = await _measure(pool_db, holdout_probes, expect_correction=False)
    db = await _connect_for_measurement(pool_db)
    try:
        recovered = 0
        for typo, truth in typo_probes:
            if await db.find_similar_artist(typo) == truth:
                recovered += 1
    finally:
        await db.close()
    return precision, AxisResult(len(typo_probes), recovered), examples


def _build_probes(
    pool_names: list[str], holdout: list[str], seed: int, precision_n: int, recall_n: int
) -> tuple[list[str], list[tuple[str, str]]]:
    rng = random.Random(seed)
    precision_probes = rng.sample(holdout, min(precision_n, len(holdout)))
    pool_lookup = {name.lower() for name in pool_names}
    eligible = [n for n in pool_names if len(n) >= _MIN_TYPO_LENGTH]
    rng.shuffle(eligible)
    typo_probes: list[tuple[str, str]] = []
    for name in eligible:
        if len(typo_probes) >= recall_n:
            break
        typo = _make_typo(name, rng)
        # Discard a mutation that lands on another real catalog name: the
        # right answer there is ambiguous, and scoring it either way measures
        # the corpus rather than the matcher.
        if typo and typo.lower() != name.lower() and typo.lower() not in pool_lookup:
            typo_probes.append((typo, name))
    return precision_probes, typo_probes


async def main_async(args: argparse.Namespace) -> int:
    if args.distance and _HAS_GUARDS:
        from rapidfuzz.distance import OSA, Levenshtein

        library_db._ARTIST_CORRECTION_DISTANCE = OSA if args.distance == "osa" else Levenshtein

    names = _catalog_names(args.library_db)
    pool_names, holdout = _partition(names)
    precision_probes, typo_probes = _build_probes(
        pool_names, holdout, args.seed, args.precision_probes, args.recall_probes
    )

    conn = sqlite3.connect(f"file:{args.library_db}?mode=ro", uri=True)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(library)")]
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    sources = ["library.artist"]
    if "alternate_artist_name" in columns:
        sources.append("library.alternate_artist_name")
    if "album_artist" in columns:
        sources.append("library.album_artist")
    if "compilation_track_artist" in tables:
        sources.append("compilation_track_artist.artist_name")

    print(f"catalog:            {args.library_db}")
    print(f"sources present:    {len(sources)} of 4 -- {', '.join(sources)}")
    if "compilation_track_artist" not in tables:
        print(
            "  WARNING: no compilation_track_artist table. Production ships ~58k CTA\n"
            "  credits, and V/A shelf credits are the densest neighbourhoods in the\n"
            "  catalog -- exactly what stresses the margin guard hardest. Every number\n"
            "  below is therefore an OPTIMISTIC bound on the margin guard's workload.\n"
            "  LML#1245 pre-merge condition 1 is NOT discharged by this run."
        )
    print(f"distinct names:     {len(names)}")
    print(f"pool half:          {len(pool_names)}   holdout half: {len(holdout)}")
    print(f"precision probes:   {len(precision_probes)} real names, none in the pool")
    print(f"recall probes:      {len(typo_probes)} synthetic single-edit typos (seed {args.seed})")
    if _HAS_GUARDS:
        print(f"edit-cap distance:  {library_db._ARTIST_CORRECTION_DISTANCE.__name__}")
    print()

    grid: list[tuple[int | None, int | None]]
    if not _HAS_GUARDS:
        # A pre-LML#1245 checkout: nothing to vary, one row, and it is the
        # baseline every other row in this report should be compared against.
        grid = [(None, None)]
    elif args.sweep:
        grid = [(m, d) for m in (0, 4, 6, 8, 10) for d in (6, 8, 10, 12, None)]
    else:
        grid = [
            (0, None),
            (0, library_db.ARTIST_CORRECTION_EDIT_CAP_DIVISOR),
            (
                library_db.ARTIST_CORRECTION_MARGIN_POINTS,
                library_db.ARTIST_CORRECTION_EDIT_CAP_DIVISOR,
            ),
        ]

    original_margin = getattr(library_db, "ARTIST_CORRECTION_MARGIN_POINTS", None)
    original_divisor = getattr(library_db, "ARTIST_CORRECTION_EDIT_CAP_DIVISOR", None)
    original_cap_fn = getattr(library_db, "_within_edit_cap", None)

    with tempfile.TemporaryDirectory() as tmp:
        pool_db = _write_catalog(pool_names, Path(tmp))
        header = f"{'margin':>7} {'edit-cap':>9} {'false/1k':>9} {'typo recall':>12}"
        print(header)
        print("-" * len(header))
        all_examples: list[tuple[str, str]] = []
        try:
            for margin, divisor in grid:
                if _HAS_GUARDS:
                    library_db.ARTIST_CORRECTION_MARGIN_POINTS = margin  # type: ignore[assignment]
                    # `divisor is None` means NO cap. It cannot be expressed as
                    # a huge divisor: the cap has a floor of 1, so a divisor of
                    # 10**9 yields a cap of ONE edit -- the tightest setting
                    # there is, not the loosest. That inversion silently turned
                    # an unguarded control row into the strictest variant on
                    # this script's first run, and it matters because
                    # Levenshtein scores a transposition as two edits, so a
                    # cap of 1 refuses a whole typo class.
                    if divisor is None:
                        library_db._within_edit_cap = lambda *_args: True  # type: ignore[assignment]
                    else:
                        library_db._within_edit_cap = original_cap_fn  # type: ignore[assignment]
                        library_db.ARTIST_CORRECTION_EDIT_CAP_DIVISOR = divisor
                precision, recall, examples = await _run_variant(
                    pool_db, precision_probes, typo_probes
                )
                if not _HAS_GUARDS:
                    margin_label, cap_label = "n/a", "n/a"
                else:
                    cap_label = "none" if divisor is None else f"len/{divisor}"
                    margin_label = "none" if margin == 0 else f">= {margin}"
                print(
                    f"{margin_label:>7} {cap_label:>9} "
                    f"{precision.rate_per_thousand:>9.1f} {recall.percent:>11.1f}%"
                )
                if not _HAS_GUARDS or (margin, divisor) == (original_margin, original_divisor):
                    all_examples = examples
        finally:
            if _HAS_GUARDS:
                library_db.ARTIST_CORRECTION_MARGIN_POINTS = original_margin  # type: ignore[assignment]
                library_db.ARTIST_CORRECTION_EDIT_CAP_DIVISOR = original_divisor  # type: ignore[assignment]
                library_db._within_edit_cap = original_cap_fn  # type: ignore[assignment]

    if all_examples:
        print("\nsurviving false corrections at the shipped settings:")
        for probe, answer in all_examples:
            print(f"  {probe!r} -> {answer!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-db", type=Path, default=Path("library.db"))
    parser.add_argument("--precision-probes", type=int, default=3000)
    parser.add_argument("--recall-probes", type=int, default=1500)
    parser.add_argument("--seed", type=int, default=1245)
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="grid over margin x edit-cap instead of the three headline variants",
    )
    parser.add_argument(
        "--distance",
        choices=("osa", "levenshtein"),
        default=None,
        help=(
            "override the edit-cap distance metric. The default is whatever the "
            "module ships (OSA). Pass 'levenshtein' to reproduce the "
            "transposition penalty that makes recall cap-dependent -- the "
            "measurement behind the choice of metric."
        ),
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
