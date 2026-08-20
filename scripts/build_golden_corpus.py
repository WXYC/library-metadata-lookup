"""Regenerate the LML#1233 golden corpus fixtures from local production data.

**Not part of the test run.** `tests/e2e/golden/` reads only the three JSON
files this writes; the corpus itself touches no database and no network. This
script exists so those files can be widened or refreshed without hand-typing
catalog rows and tracklists, and so the sampling that produced them is
reviewable rather than folklore.

It needs two local sources, both read-only, neither available in CI:

- `library.db` -- the WXYC catalog SQLite (gitignored; see discogs-etl's
  `export_to_sqlite.py`).
- a full Discogs dump in PostgreSQL -- the `release` / `release_artist` /
  `release_track` / `release_track_artist` shape that discogs-cache builds.

```bash
uv run python -m scripts.build_golden_corpus \\
    --library-db library.db \\
    --discogs-dsn 'postgresql://localhost:5432/discogs_full'
```

**Why real Discogs rows rather than synthesized ones.** The corpus asserts what
LML's matching gates do with a tracklist, and those gates key on token overlap
between a query and track titles. Synthesized tracklists (`"<album> (part 1)"`
and friends) have a degenerate token distribution -- every track shares the
album's tokens -- which makes the coverage veto in `discogs/matching.py` behave
in ways real data never produces. Real tracklists are the only inputs that
exercise those gates honestly. Fixture drift against live Discogs is accepted
and expected (LML#1233, "Risks"): this tier tests *this repo's algorithm given
fixed inputs*, which is what per-commit attribution requires.

**Expectations are not written here.** This writes case *skeletons* with
`"expect": null` for anything new and leaves every existing expectation
untouched. Recording verdicts is `scripts/rebaseline_golden_corpus.py`'s job,
and it refuses to touch frozen cases -- see `docs/testing.md`.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wxyc_etl.schema import library_columns

logger = logging.getLogger("build_golden_corpus")

REPO_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_DIR = REPO_ROOT / "tests" / "e2e" / "golden"

#: Fixed so a regeneration reproduces the same sample. Changing it reshuffles
#: the whole corpus, which is a reviewable diff, not a silent one.
SAMPLE_SEED = 12331

#: Artists whose entire shelf is seeded. WXYC is a freeform station, so these
#: come from the station's own play history rather than from whatever the
#: catalog holds most of. The last four are here because a named regression
#: case needs their exact rows, not because they were sampled.
CORPUS_ARTISTS: tuple[str, ...] = (
    "A Tribe Called Quest",
    "Alex G.",
    "Ali Farka Toure",
    "Angel Olsen",
    "Animal Collective",
    "Anne Gillis",
    "Aphex Twin",
    "Arthur Russell",
    "Autechre",
    "Beach House",
    "Big Thief",
    "Billie Holiday",
    "Blood Orange",
    "Boards of Canada",
    "Brian Eno",
    "Broadcast",
    "Burial",
    "Caetano Veloso",
    "Carmen Villain",
    "Cat Power",
    "Charles Mingus",
    "Chuquimamani-Condori",
    "Cocteau Twins",
    "Colin Stetson",
    "Colleen",
    "Dam-Funk",
    "Damien Jurado",
    "Jessica Pratt",
    "Juana Molina",
    "Stereolab",
    # --- rows the named regression cases turn on ---
    "Arabian Prince",  # LML#1184: two shelved rows a comp hit must not erase
    "Galaxy 2 Galaxy",  # LML#717: the album-title fuzz collision target
    "Lone",  # LML#717: the artist whose non-library album was preempted
    "Population One",  # LML#1225: the row the coverage gate must stop returning
)

#: Artists with no row in the seeded catalog, so the whole query must miss.
#: The generator asserts each is genuinely absent -- a "miss" case for a seeded
#: artist would fuzzy-match a neighbouring album, which is correct behavior and
#: would make the case wrong rather than the code. Diacritics are deliberate
#: (Nilüfer, Gutiérrez, Csillagrablók): they exercise the Unicode
#: normalization path on the way to a miss.
ABSENT_ARTIST_QUERIES: tuple[tuple[str, str], ...] = (
    ("Nilüfer Yanya", "Painless"),
    ("Hermanos Gutiérrez", "El Bueno Y El Malo"),
    ("Csillagrablók", "Nem Tudom"),
    ("Sessa", "Estrela Acesa"),
    ("Large Professor", "1st Class"),
    ("Duke Ellington & John Coltrane", "In A Sentimental Mood"),
    ("Klein", "Harmattan"),
    ("Ana Roxanne", "Because Of A Flower"),
)

#: Seeded artists queried with an album they do not have. The shape is a real
#: production one (a DJ types an album WXYC does not shelve for an artist it
#: does), and it does not uniformly land in one place: the third element says
#: which of three things this particular query actually reaches, verified
#: against the built corpus rather than assumed (LML#1233 review corrected a
#: prior note here that claimed the whole stratum pinned the artist-only
#: fallback, when five of the original seven cleanly missed and the other two
#: hit through a different, narrower mechanism -- see each category below).
#:
#: - `"clean_miss"` -- the floor case. `search_library_with_fallback`'s
#:   album-match rapidfuzz floor (`_ALBUM_MATCH_FLOOR`, `lookup/matching.py`,
#:   80.0) correctly rejects the typed album against every real title on the
#:   shelf, so the query misses.
#: - `"fuzzy_collision"` -- NOT the artist-only fallback, despite looking
#:   similar. A short real album title (e.g. "Syro") is a near-substring of
#:   the typed one ("Syro Deluxe Edition") and gets caught by the LOOSER
#:   per-album word-overlap filter inside `search_library_with_fallback`'s own
#:   per-album loop (`search_one_album`, `lookup/strategies/artist_plus_album.py`)
#:   -- before the artist-only leg is ever reached. The response is a hit on
#:   that one unrelated album, with `song_not_found=True`.
#: - `"artist_fallback"` -- the genuine artist-only fallback
#:   (`search_library_with_fallback`'s `if not all_results and lib_artist:`
#:   branch): the typed album shares too few whole words with any real title
#:   to be caught by the per-album loop, but clears the rapidfuzz floor once
#:   every shelved title is checked unfiltered, so the shelf rows carrying the
#:   matched word come back with `song_not_found=True`. Only one query here is
#:   built to land here on purpose -- Brian Eno / "Airports" matches both
#:   "Ambient 1 - Music for Airports" and "Bang on a Can Does Music for
#:   Airports" (each token_set_ratio 100.0 against "airports") but shares only
#:   one significant word with either, below the per-album loop's two-word
#:   floor.
ABSENT_ALBUM_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("Cat Power", "Sun Rays Over Nothing", "fuzzy_collision"),
    ("Broadcast", "Spell Blanket", "clean_miss"),
    ("Burial", "Antidawn", "clean_miss"),
    ("Big Thief", "Double Infinity", "clean_miss"),
    ("Aphex Twin", "Syro Deluxe Edition", "fuzzy_collision"),
    ("Brian Eno", "Reflection", "clean_miss"),
    ("Colleen", "Le jour et la nuit", "clean_miss"),
    ("Brian Eno", "Airports", "artist_fallback"),
)

_ABSENT_ALBUM_NOTES: dict[str, str] = {
    "clean_miss": (
        "Seeded artist, album WXYC does not shelve. The album-match rapidfuzz "
        "floor (_ALBUM_MATCH_FLOOR, lookup/matching.py) correctly rejects every "
        "real title on the shelf against the typed album, so this cleanly misses."
    ),
    "fuzzy_collision": (
        "Seeded artist, album WXYC does not shelve -- but this one does NOT "
        "miss. A short real album title fuzz-collides as a near-substring of "
        "the typed one and is caught by the looser per-album word-overlap "
        "filter inside search_library_with_fallback's own per-album loop, "
        "before the artist-only fallback is ever reached. The response is a "
        "hit on that one unrelated album, with song_not_found=True -- a "
        "narrower and more surprising hazard than the artist-only fallback, "
        "and not the same mechanism."
    ),
    "artist_fallback": (
        "Seeded artist, album WXYC does not shelve, built specifically to "
        "reach the genuine artist-only fallback (search_library_with_fallback's "
        "`if not all_results and lib_artist:` branch): the typed album shares "
        "too few whole words with any real title to be caught by the earlier "
        "per-album loop, but clears the album-match floor once the whole shelf "
        "is checked unfiltered. song_not_found=True is the load-bearing half "
        "-- the shelf rows carrying the matched word come back, not a miss."
    ),
}

#: Tracks that exist nowhere -- not on the shelf, not in the Discogs fixture.
#: The miss direction for the track-bearing lanes, which the sampled cases
#: (all built from real tracklists) cannot cover: every one of those is a hit
#: by construction, so without these the corpus would only ever notice recall
#: getting worse, never the matcher getting looser.
#: Third element is a suspect-note: non-empty when the recorded verdict is
#: believed to be wrong, in which case the case is marked `suspect` (see
#: `tests/e2e/golden/corpus.py::Case.suspect`).
NONSENSE_TRACK_QUERIES: tuple[tuple[str | None, str, str], ...] = (
    (None, "Qqzzx Vulpine Escarpment", ""),
    (None, "Thrattlebone Nine Nine", ""),
    (
        "Stereolab",
        "Zzyzx Marginal Fanfare",
        "SUSPECT (found by this corpus on its first run, 2026-08-20): the recorded "
        "verdict is a confident, uncaveated hit -- found_on_compilation=True, "
        'song_not_found=False, context \'Found "Zzyzx Marginal Fanfare" by Stereolab '
        "on: Peng!' -- for a track that exists nowhere. TRACK_ON_COMPILATION's "
        "last-resort branch (`if not results and keyword_matches and not "
        "discogs_found_releases`) surfaces the best library keyword hit when Discogs "
        "returns nothing, and the compilation Outcome labels it confirmed. This is "
        "the LML#1225 user-visible failure -- a confident row for a song that does "
        "not exist -- reached through a path #1225's tracklist-gate fix does not "
        "cover, because no tracklist is ever consulted.",
    ),
    ("Cocteau Twins", "Grebulon Overture Nine", ""),
)

#: Artists whose ENTIRE shelf gets a Discogs release extracted and routed by
#: artist+album. LML#801's mechanism is the artist-only fallback returning the
#: whole shelf and track validation only ever seeing the truncated top of it,
#: so reproducing it needs every shelf row to be independently resolvable --
#: which in production is what `filter_results_by_track_validation` does, one
#: `search()` per candidate row.
SHELF_RELEASE_ARTISTS: tuple[str, ...] = ("Aphex Twin",)

#: How many seeded albums get a real Discogs release extracted for them. Each
#: one yields an artist+album+song case and an artist+song case, so this is the
#: knob that sizes the track-bearing half of the corpus.
TRACK_RELEASE_COUNT = 26

#: Sample sizes per stratum. See `tests/e2e/golden/corpus.py`'s SHAPE_FLOORS
#: for the production mix these are drawn against and why the small shapes are
#: over-represented.
SAMPLE_PLAN = {
    "artist_album": 66,
    "artist_only": 12,
    "artist_album_song": 18,
    "artist_song": 14,
    "song_only": 7,
}


@dataclass(frozen=True)
class ReleaseSpec:
    """One Discogs release to extract, resolved by exact artist+title."""

    key: str
    artist: str
    title: str
    #: Pinned when an exact artist+title match would be ambiguous or when the
    #: issue being pinned cites a specific release.
    release_id: int | None = None
    is_compilation: bool = False
    #: The seeded catalog row this release corresponds to, when there is one.
    library_row: tuple[str, str] | None = None


#: Releases the frozen regression cases turn on. Each release id is the real
#: Discogs release the issue cites (or, where the issue cites none, the
#: highest-track-count pressing), so a reader can open discogs.com and check.
FROZEN_RELEASES: tuple[ReleaseSpec, ...] = (
    # LML#801: "Milkman" is track 11. The point of the case is that this album
    # is eighth on an eighteen-row Aphex Twin shelf, past the truncation that
    # dropped it in production.
    ReleaseSpec(
        key="lml801-rdj",
        artist="Aphex Twin",
        title="Richard D. James Album",
        release_id=43694,
        library_row=("Aphex Twin", "Richard D. James Album"),
    ),
    # LML#717: the non-library release whose correct resolution was preempted
    # by an album-title fuzz collision with a wrong-artist library row.
    # "Lone (2)" is Discogs' own disambiguated credit -- kept verbatim, because
    # `discogs/matching.py::strip_discogs_suffix` exists to handle exactly that
    # and a fixture that pre-strips it would test a string LML never sees.
    ReleaseSpec(
        key="lml717-galaxy-garden",
        artist="Lone (2)",
        title="Galaxy Garden",
        release_id=3573363,
    ),
    # LML#1225: track B2 "Battle For Space" is what the containment-only title
    # gate accepted for the query "Space Lizzard Battle Star hell cat".
    ReleaseSpec(
        key="lml1225-population-one",
        artist="Population One",
        title="Theater Of A Confused Mind",
        release_id=6194406,
        library_row=("Population One", "Theater of a confused Mind"),
    ),
    # LML#1184: the Various-Artists compilation WXYC does not shelve. Track 16
    # is "Strange Life", credited to Arabian Prince.
    ReleaseSpec(
        key="lml1184-chrome-children-2",
        artist="Various",
        title="Chrome Children Vol. 2",
        release_id=894252,
        is_compilation=True,
    ),
)

#: Routes the frozen cases need on top of the ones derived from sampled
#: releases. A route says what an upstream search returned; it never says what
#: LML should do with it.
FROZEN_TRACK_ROUTES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Milkman", "Aphex Twin", ("lml801-rdj",)),
    ("Crystal Caverns 1991", "Lone", ("lml717-galaxy-garden",)),
    ("Space Lizzard Battle Star", "", ("lml1225-population-one",)),
    ("Strange Life", "Arabian Prince", ("lml1184-chrome-children-2",)),
)

FROZEN_ALBUM_TITLE_ROUTES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Galaxy Garden", ("lml717-galaxy-garden",)),
)

FROZEN_ALBUM_ROUTES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Lone", "Galaxy Garden", ("lml717-galaxy-garden",)),
    # LML#801's cold-cache case turns on this row being independently
    # resolvable: the widened track validation searches Discogs once per shelf
    # candidate, and *Richard D. James Album* is the only one carrying the
    # track. Routed here rather than by `resolve_shelf_releases` because the
    # frozen pressing (43694, the one the issue cites) already claims the row.
    ("Aphex Twin", "Richard D. James Album", ("lml801-rdj",)),
)


@dataclass
class Fixture:
    """Accumulates the Discogs half of the corpus as it is resolved."""

    releases: dict[str, dict[str, Any]] = field(default_factory=dict)
    track_searches: dict[str, list[int]] = field(default_factory=dict)
    album_searches: dict[str, list[int]] = field(default_factory=dict)
    album_title_searches: dict[str, list[int]] = field(default_factory=dict)

    def route(
        self,
        table: dict[str, list[int]],
        key: str,
        keys: tuple[str, ...],
        *,
        override: bool = False,
    ) -> None:
        """Record one route, refusing to silently clobber a conflicting one.

        Two different releases can resolve to the same route key -- a bare
        track title shared across pressings, or a frozen route deliberately
        reusing a key sampling also produced. The first is a fixture bug
        (LML#1233 review): the loser's case would silently be answered by the
        winner's release, with nothing in the diff to say so (already latent:
        four track titles in the sampled fixture are shared across releases,
        and `route_key("untitled", None)` resolves to whichever of two
        releases wrote it last). The second is intentional -- callers building
        `FROZEN_*_ROUTES` pass `override=True` to say so explicitly, in the
        reviewed diff rather than in silence. A same-value re-route (the two
        specs happen to resolve to the identical release id) is never a
        conflict, override or not.
        """
        value = [self.releases[k]["release_id"] for k in keys]
        if not override and key in table and table[key] != value:
            raise SystemExit(
                f"route {key!r} already maps to release id(s) {table[key]!r}; refusing to "
                f"silently overwrite with {value!r}. If this replacement is deliberate "
                "(e.g. a FROZEN_*_ROUTES entry), route it with override=True."
            )
        table[key] = value

    def to_json(self) -> dict[str, Any]:
        return {
            "releases": sorted(self.releases.values(), key=lambda r: r["release_id"]),
            "track_searches": dict(sorted(self.track_searches.items())),
            "album_searches": dict(sorted(self.album_searches.items())),
            "album_title_searches": dict(sorted(self.album_title_searches.items())),
        }


def route_key(*parts: str | None) -> str:
    """Must stay byte-identical to `tests/e2e/golden/corpus.py::_route_key`."""
    return "|".join((p or "").strip().lower() for p in parts)


# ---------------------------------------------------------------------------
# library.db
# ---------------------------------------------------------------------------

#: The code under test's own column list (`library/db.py` imports the same
#: name to validate `library.db`'s schema; `tests/e2e/golden/corpus.py`'s
#: `LIBRARY_COLUMNS` imports it too), not a hand-copied second tuple that can
#: drift from it silently (LML#1233 review).
LIBRARY_FIELDS = tuple(library_columns())


def fetch_library_rows(db_path: Path) -> list[dict[str, Any]]:
    """Every catalog row for `CORPUS_ARTISTS`, with production ids preserved.

    Ids are **not** renumbered. LML#801 is a story about rowid ordering -- the
    artist-only fallback returned the first five Aphex Twin rows by rowid and
    *Richard D. James Album* was the eighth -- so renumbering would rewrite the
    exact property the case exists to pin. Keeping production ids also means a
    fixture row can be checked against the catalog by id.
    """
    if not db_path.exists():
        raise SystemExit(f"library.db not found at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows: list[dict[str, Any]] = []
        cols = ", ".join(LIBRARY_FIELDS)
        for artist in CORPUS_ARTISTS:
            cursor = conn.execute(
                f"SELECT {cols} FROM library WHERE artist = ? COLLATE NOCASE ORDER BY id",
                (artist,),
            )
            for record in cursor.fetchall():
                row = {key: record[key] for key in LIBRARY_FIELDS}
                # The catalog stores the literal string "NULL" and empty
                # strings in this optional column; normalize so the fixture
                # does not encode one export's quirk as though it meant
                # something.
                if not row["alternate_artist_name"] or row["alternate_artist_name"] == "NULL":
                    row["alternate_artist_name"] = ""
                rows.append(row)
    finally:
        conn.close()
    if not rows:
        raise SystemExit("no catalog rows matched CORPUS_ARTISTS -- wrong library.db?")
    rows.sort(key=lambda r: (r["artist"].lower(), r["id"]))
    seeded = {r["artist"].lower() for r in rows}
    clashes = [a for a, _ in ABSENT_ARTIST_QUERIES if a.lower() in seeded]
    if clashes:
        raise SystemExit(
            f"{clashes} are listed as absent-artist miss cases but are seeded. "
            "Those cases would assert that a real shelf is invisible."
        )
    return rows


# ---------------------------------------------------------------------------
# Discogs dump
# ---------------------------------------------------------------------------

_RESOLVE_SQL = """
SELECT r.id, ra.artist_name, r.title, r.release_year,
       (SELECT count(*) FROM release_track rt WHERE rt.release_id = r.id) AS track_count
FROM release r
JOIN release_artist ra ON ra.release_id = r.id AND ra.extra = 0
WHERE lower(f_unaccent(ra.artist_name)) LIKE %(artist_like)s
  AND lower(f_unaccent(r.title)) LIKE %(title_like)s
  AND lower(ra.artist_name) = %(artist)s
  AND lower(r.title) = %(title)s
ORDER BY track_count DESC, r.id ASC
LIMIT 1
"""
# The two LIKE predicates are redundant with the two equalities and are there
# only to hit the gin_trgm indexes on `release_artist.artist_name` and
# `release.title`. Without them PostgreSQL sequential-scans a 179M-row table
# per lookup. Keep both halves: the LIKEs make it fast, the equalities make it
# correct.

_TRACKS_SQL = """
SELECT rt.sequence, rt.position, rt.title,
       coalesce(
           (SELECT array_agg(rta.artist_name ORDER BY rta.artist_name)
            FROM release_track_artist rta
            WHERE rta.release_id = rt.release_id
              AND rta.track_sequence = rt.sequence
              AND rta.extra = 0),
           '{}'::text[]
       ) AS credits
FROM release_track rt
WHERE rt.release_id = %(release_id)s
ORDER BY rt.sequence
"""

_BY_ID_SQL = """
SELECT r.id, r.title, r.release_year,
       (SELECT ra.artist_name FROM release_artist ra
        WHERE ra.release_id = r.id AND ra.extra = 0
        ORDER BY ra.artist_id NULLS LAST LIMIT 1) AS artist_name
FROM release r WHERE r.id = %(release_id)s
"""


def _like(text: str) -> str:
    return f"%{text.lower()}%"


def resolve_release(cursor: Any, spec: ReleaseSpec) -> dict[str, Any] | None:
    """Resolve one spec to a real release plus its real tracklist."""
    if spec.release_id is not None:
        cursor.execute(_BY_ID_SQL, {"release_id": spec.release_id})
        row = cursor.fetchone()
        if row is None:
            raise SystemExit(f"pinned release {spec.release_id} ({spec.key}) is not in the dump")
        release_id, title, year, artist_name = row[0], row[1], row[2], row[3]
    else:
        cursor.execute(
            _RESOLVE_SQL,
            {
                "artist_like": _like(spec.artist),
                "title_like": _like(spec.title),
                "artist": spec.artist.lower(),
                "title": spec.title.lower(),
            },
        )
        row = cursor.fetchone()
        if row is None:
            return None
        release_id, artist_name, title, year = row[0], row[1], row[2], row[3]

    cursor.execute(_TRACKS_SQL, {"release_id": release_id})
    tracklist = [
        {"position": position or "", "title": track_title, "artists": list(credits or [])}
        for _seq, position, track_title, credits in cursor.fetchall()
        # Discogs models headings ("The First Side") as untitled-position rows.
        # They are not tracks and would give the matcher phantom titles.
        if position and track_title
    ]
    if not tracklist:
        return None
    return {
        "release_id": release_id,
        "title": title,
        "artist": artist_name,
        "year": year,
        "is_compilation": spec.is_compilation,
        "tracklist": tracklist,
    }


def pick_track_releases(
    cursor: Any, rows: list[dict[str, Any]], rng: random.Random, wanted: int
) -> list[tuple[ReleaseSpec, dict[str, Any]]]:
    """Resolve seeded catalog rows to real Discogs releases, deterministically.

    Walks the distinct (artist, title) pairs in a stable shuffled order and
    keeps the first `wanted` that resolve to a release with enough tracks to
    sample from, at most one per artist so the track-bearing strata are not
    all one shelf.
    """
    # A row a frozen release already covers is skipped: two Discogs pressings
    # of one album in the fixture would give the same query two possible
    # answers depending on which route won, and the frozen pressing is the one
    # its issue cites.
    claimed = {
        (spec.library_row[0].lower(), spec.library_row[1].lower())
        for spec in FROZEN_RELEASES
        if spec.library_row is not None
    }
    pairs = sorted(
        {
            (r["artist"], r["title"])
            for r in rows
            if (r["artist"].lower(), r["title"].lower()) not in claimed
        }
    )
    rng.shuffle(pairs)
    picked: list[tuple[ReleaseSpec, dict[str, Any]]] = []
    seen_artists: set[str] = set()
    for artist, title in pairs:
        if len(picked) >= wanted:
            break
        if artist in seen_artists:
            continue
        spec = ReleaseSpec(
            key=f"seeded-{slugify(artist)}-{slugify(title)}",
            artist=artist,
            title=title,
            library_row=(artist, title),
        )
        resolved = resolve_release(cursor, spec)
        if resolved is None or len(resolved["tracklist"]) < 4:
            continue
        seen_artists.add(artist)
        picked.append((spec, resolved))
        logger.info(
            "resolved %s / %s -> discogs %s (%d tracks)",
            artist,
            title,
            resolved["release_id"],
            len(resolved["tracklist"]),
        )
    if len(picked) < wanted:
        logger.warning("only %d of %d wanted releases resolved", len(picked), wanted)
    return picked


# ---------------------------------------------------------------------------
# Case construction
# ---------------------------------------------------------------------------


def resolve_shelf_releases(
    cursor: Any, rows: list[dict[str, Any]]
) -> list[tuple[ReleaseSpec, dict[str, Any]]]:
    """Resolve every seeded row of :data:`SHELF_RELEASE_ARTISTS` to a release.

    Rows that do not resolve are simply skipped, and that is faithful rather
    than a gap: the WXYC catalog carries typo'd titles ("Polyson Window",
    "Windolicker") that Discogs does not match either, so a shelf with holes in
    it is what the production validator actually walks.
    """
    out: list[tuple[ReleaseSpec, dict[str, Any]]] = []
    claimed = {
        (spec.library_row[0].lower(), spec.library_row[1].lower())
        for spec in FROZEN_RELEASES
        if spec.library_row is not None
    }
    seen: set[tuple[str, str]] = set()
    for row in rows:
        artist, title = row["artist"], row["title"]
        key = (artist.lower(), title.lower())
        if artist not in SHELF_RELEASE_ARTISTS or key in claimed or key in seen:
            continue
        seen.add(key)
        spec = ReleaseSpec(
            key=f"shelf-{slugify(artist)}-{slugify(title)}",
            artist=artist,
            title=title,
            library_row=(artist, title),
        )
        resolved = resolve_release(cursor, spec)
        if resolved is None:
            logger.info("shelf row %s / %s has no Discogs match; left unrouted", artist, title)
            continue
        out.append((spec, resolved))
    logger.info("resolved %d shelf releases", len(out))
    return out


def slugify(text: str) -> str:
    out = "".join(character.lower() if character.isalnum() else "-" for character in text)
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")[:44]


def _case(
    case_id: str,
    shape: str,
    note: str,
    query: dict[str, str],
    **extra: Any,
) -> dict[str, Any]:
    case: dict[str, Any] = {"id": case_id, "shape": shape, "frozen": False}
    case.update({k: v for k, v in extra.items() if k in ("issue", "settings")})
    if extra.get("suspect"):
        case["suspect"] = True
    case["note"] = note
    case["query"] = query
    for key in ("requires_discogs", "requires_routes", "requires_rows", "requires_absent_artists"):
        if extra.get(key):
            case[key] = extra[key]
    case["expect"] = None
    return case


#: Track titles too generic to identify a release. A case built on one of
#: these pins a coincidence: "Untitled" appears on thousands of releases, so
#: which library row comes back is decided by fuzzy-match tie-breaking rather
#: than by anything the matcher is supposed to be getting right.
_GENERIC_TRACK_TITLES = frozenset(
    {
        "untitled",
        "intro",
        "outro",
        "interlude",
        "reprise",
        "prelude",
        "theme",
        "bonus track",
        "hidden track",
        "instrumental",
    }
)


def distinctive_tracks(tracklist: list[dict[str, Any]], album: str) -> list[dict[str, Any]]:
    """Tracks usable as a query, in tracklist order.

    Two exclusions, both about the case pinning the thing it claims to:

    - A title that is a near-copy of the album title makes an artist+song case
      indistinguishable from an artist+album one, so it would pin the wrong
      lane.
    - A generic or very short title (see :data:`_GENERIC_TRACK_TITLES`) has no
      discriminating power, so which row comes back is a tie-break artifact.
    """
    album_lower = album.lower()
    out = []
    for track in tracklist:
        title = track["title"].strip()
        lowered = title.lower()
        if len(title) < 7 or lowered in _GENERIC_TRACK_TITLES:
            continue
        if lowered in album_lower or album_lower in lowered:
            continue
        out.append(track)
    return out


def build_sampled_cases(
    rows: list[dict[str, Any]],
    track_releases: list[tuple[ReleaseSpec, dict[str, Any]]],
    rng: random.Random,
) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    # --- artist + album: 76% of production traffic, and the shape a recall
    # regression breaks first. Sampled over DISTINCT artist+title pairs, not
    # rows: the catalog shelves the same album in several formats (Cat Power's
    # *Moon Pix* on CD and vinyl), which are one query and would otherwise
    # produce two cases with the same id.
    pool = sorted({(r["artist"], r["title"]) for r in rows})
    for artist, title in rng.sample(pool, min(SAMPLE_PLAN["artist_album"], len(pool))):
        cases.append(
            _case(
                f"ab-{slugify(artist)}-{slugify(title)}",
                "artist_album",
                "Artist+album against a real catalog row -- the dominant production shape.",
                {"artist": artist, "album": title},
                requires_rows=[[artist, title]],
            )
        )

    # --- artist + album, artist absent entirely: the false-positive direction.
    for index, (artist, album) in enumerate(ABSENT_ARTIST_QUERIES):
        cases.append(
            _case(
                f"ab-miss-{slugify(artist)}-{index}",
                "artist_album",
                "Artist has no shelf presence at all. If this starts returning rows the "
                "matcher has loosened -- a corpus of hits alone cannot see that.",
                {"artist": artist, "album": album},
                requires_absent_artists=[artist],
            )
        )

    # --- artist + album where only the album is absent: the fallback lane.
    shelved = {(row["artist"].lower(), row["title"].lower()) for row in rows}
    mis_declared = [
        (artist, album)
        for artist, album, _category in ABSENT_ALBUM_QUERIES
        if (artist.lower(), album.lower()) in shelved
    ]
    if mis_declared:
        raise SystemExit(
            f"{mis_declared} are declared as albums WXYC does not shelve, but they are "
            "seeded. Those cases would pin an ordinary direct hit while claiming to pin "
            "an absent-album lane."
        )
    for artist, album, category in ABSENT_ALBUM_QUERIES:
        cases.append(
            _case(
                f"ab-nofallback-{slugify(artist)}-{slugify(album)}",
                "artist_album",
                _ABSENT_ALBUM_NOTES[category],
                {"artist": artist, "album": album},
            )
        )

    # --- tracks that exist nowhere.
    for artist, song, suspect_note in NONSENSE_TRACK_QUERIES:
        query = {"song": song} if artist is None else {"artist": artist, "song": song}
        note = (
            "A track that is on no shelf and in no Discogs fixture. The miss direction "
            "for the track lanes: if this starts returning a confident row, a "
            "validation gate has stopped gating."
        )
        cases.append(
            _case(
                f"track-miss-{slugify(artist or 'anon')}-{slugify(song)}",
                "song_only" if artist is None else "artist_song",
                note if not suspect_note else f"{note} {suspect_note}",
                query,
                suspect=bool(suspect_note),
            )
        )

    # --- artist only.
    by_artist = sorted({row["artist"] for row in rows})
    for artist in rng.sample(by_artist, min(SAMPLE_PLAN["artist_only"], len(by_artist))):
        cases.append(
            _case(
                f"artist-{slugify(artist)}",
                "artist_only",
                "Artist-only lookup returns that artist's shelf. Pins the shelf ordering, "
                "which a ranking change would otherwise reorder silently.",
                {"artist": artist},
            )
        )

    # --- the track-bearing shapes, all built from real tracklists.
    usable = [
        (spec, resolved, tracks)
        for spec, resolved in track_releases
        if len(tracks := distinctive_tracks(resolved["tracklist"], resolved["title"])) >= 3
    ]
    rng.shuffle(usable)
    # Each shape takes a *different* track off the same release, so three
    # shapes over 26 releases are 78 distinct queries rather than 26 repeated
    # three ways.
    plan = (
        ("artist_album_song", SAMPLE_PLAN["artist_album_song"], 0),
        ("artist_song", SAMPLE_PLAN["artist_song"], 1),
        ("song_only", SAMPLE_PLAN["song_only"], 2),
    )
    for shape, count, track_index in plan:
        for spec, _resolved, tracks in usable[:count]:
            assert spec.library_row is not None
            artist, album = spec.library_row
            song = tracks[track_index]["title"].strip()
            if shape == "artist_album_song":
                query = {"artist": artist, "album": album, "song": song}
                note = (
                    "Artist+album+song against a real Discogs tracklist. Pins that the "
                    "track is confirmed on the shelved album, not merely that the album "
                    "was found."
                )
            elif shape == "artist_song":
                query = {"artist": artist, "song": song}
                note = (
                    "Artist+song with no album -- the shape LML#801 broke on. The album "
                    "has to be recovered from the tracklist."
                )
            else:
                query = {"song": song}
                note = (
                    "Song only, no artist. SONG_AS_TRACK's lane, where LML#1225 showed "
                    "the title gate carrying validation alone."
                )
            cases.append(
                _case(
                    f"{shape.replace('_', '-')}-{slugify(artist)}-{slugify(song)}",
                    shape,
                    note,
                    query,
                    requires_discogs=True,
                    requires_rows=[[artist, album]],
                )
            )
    return cases


def build_routes(
    fixture: Fixture, track_releases: list[tuple[ReleaseSpec, dict[str, Any]]]
) -> None:
    """Derive the routing tables for the sampled releases.

    Each seeded album is routed three ways -- by track+artist, by track alone,
    and by artist+album -- because the three sampled shapes reach the upstream
    through three different methods.
    """
    for spec, resolved in track_releases:
        assert spec.library_row is not None
        artist, album = spec.library_row
        fixture.route(fixture.album_searches, route_key(artist, album), (spec.key,))
        for track in resolved["tracklist"]:
            title = track["title"].strip()
            if not title:
                continue
            fixture.route(fixture.track_searches, route_key(title, artist), (spec.key,))
            fixture.route(fixture.track_searches, route_key(title, None), (spec.key,))


# ---------------------------------------------------------------------------
# Frozen cases
# ---------------------------------------------------------------------------


def frozen_cases() -> list[dict[str, Any]]:
    """Hand-authored cases, each pinning a failure that reached production.

    These are not sampled: each needs a specific relationship between the
    query and the catalog that random sampling would never produce. Their
    expectations are hand-written and `scripts/rebaseline_golden_corpus.py`
    refuses to rewrite them -- see that script and `docs/testing.md`.

    `expect: null` here means "author it by hand and commit it"; the builder
    never fills these in, so a frozen case added without an expectation fails
    the corpus loudly rather than silently recording whatever today does.
    """
    return [
        {
            "id": "lml801-aphex-twin-milkman",
            "shape": "artist_song",
            "frozen": True,
            "issue": "LML#801 / LML#802",
            "note": (
                "Artist+track must surface the one shelved album that carries the track, "
                "even though it sits eighth on an eighteen-row shelf. In production the "
                "artist-only fallback truncated to the first five rows by rowid and "
                "*Richard D. James Album* never entered the returned set. "
                "song_not_found=False is the load-bearing half: it asserts the track was "
                "CONFIRMED, not that the shelf happened to rank right."
            ),
            "query": {"artist": "Aphex Twin", "song": "Milkman"},
            "requires_discogs": True,
            "requires_routes": ["track:milkman|aphex twin"],
            "requires_rows": [["Aphex Twin", "Richard D. James Album"]],
            "expect": None,
        },
        {
            "id": "lml801-aphex-twin-fingerbib-cold-cache",
            "shape": "artist_song",
            "frozen": True,
            "issue": "LML#801 / LML#802",
            "note": (
                "The same recall gap as the case above, through the mechanism that "
                "actually broke. There is deliberately NO track route for 'Fingerbib', "
                "so the Discogs track search comes back empty -- which is exactly what "
                "LML#802's CTE prune did in production, and what a cold or degraded "
                "cache does generally. ARTIST_PLUS_ALBUM then falls through to the "
                "artist-only leg, which returns the whole eighteen-row Aphex Twin shelf, "
                "and *Richard D. James Album* sits eighth. Before LML#808 track "
                "validation only ever saw the top MAX_SEARCH_RESULTS rows by rowid, so "
                "the album carrying the track was dropped before anything could confirm "
                "it and the caller got five unrelated releases with song_not_found=true. "
                "Every shelf row is independently resolvable in the Discogs fixture "
                "(SHELF_RELEASE_ARTISTS), because that per-row search is what the "
                "widened validation walks."
            ),
            "query": {"artist": "Aphex Twin", "song": "Fingerbib"},
            "requires_discogs": True,
            "requires_routes": ["search:aphex twin|richard d. james album"],
            "requires_rows": [["Aphex Twin", "Richard D. James Album"]],
            "expect": None,
        },
        {
            "id": "lml717-lone-galaxy-garden",
            "shape": "artist_album_song",
            "frozen": True,
            "issue": "LML#717",
            "note": (
                "A non-library release plus an album title that fuzz-collides with a "
                "WRONG-ARTIST library row (Lone / 'Galaxy Garden' vs Galaxy 2 Galaxy / "
                "'Galaxy to Galaxy', token_set_ratio 80). The wrong row must not surface: "
                "in production it did, and because it was a non-empty result it "
                "structurally preempted the non-library resolver. Note what the verdict "
                "has to carry to see this: before the fix the response was ALSO a hit "
                "(one row, Galaxy 2 Galaxy / 'Galaxy to Galaxy', found_on_compilation "
                "true), so miss_kind is identical on both sides and only the result "
                "identity distinguishes them."
            ),
            "query": {
                "artist": "Lone",
                "album": "Galaxy Garden",
                "song": "Crystal Caverns 1991",
            },
            "requires_discogs": True,
            "requires_routes": ["search:lone|galaxy garden"],
            "requires_rows": [["Galaxy 2 Galaxy", "Galaxy to Galaxy"]],
            "expect": None,
        },
        {
            "id": "lml1225-space-lizzard-battle-star",
            "shape": "song_only",
            "frozen": True,
            "issue": "LML#1225",
            "note": (
                "The query-coverage gate must REJECT a tracklist entry that is a "
                "sub-phrase of the typed query. Discogs release 6194406 carries B2 "
                "'Battle For Space'; token_set_ratio against 'space lizzard battle star "
                "hell cat' is 85.71, over the 85 fuzzy threshold, and the shelved "
                "Population One row came back with song_not_found=false -- an uncaveated "
                "confident hit for a song that does not exist."
            ),
            "query": {"song": "Space Lizzard Battle Star"},
            "requires_discogs": True,
            "requires_routes": ["track:space lizzard battle star|"],
            "requires_rows": [["Population One", "Theater of a confused Mind"]],
            "expect": None,
        },
        {
            "id": "lml1184-arabian-prince-strange-life",
            "shape": "artist_song",
            "frozen": True,
            "issue": "LML#1184",
            "note": (
                "A track that resolves to a compilation WXYC does not shelve must not "
                "erase the artist's own shelved albums. In production the row-less "
                "compilation item REPLACED both Arabian Prince rows, so the caller got a "
                "single id=0 result pointing at a record nobody can pull off the shelf. "
                "miss_kind is 'hit' before and after the fix -- only the result list "
                "moved, which is why the verdict has to carry it."
            ),
            "query": {"artist": "Arabian Prince", "song": "Strange Life"},
            "settings": {"lml_resolve_nonlibrary_release": True},
            "requires_discogs": True,
            "requires_routes": ["track:strange life|arabian prince"],
            "requires_rows": [
                ["Arabian Prince", "Brother Arab"],
                ["Arabian Prince", "Innovative Life: The Anthology 1984-1989"],
            ],
            "expect": None,
        },
        {
            "id": "lml1184-arabian-prince-nonsense-track",
            "shape": "artist_song",
            "frozen": True,
            "issue": "LML#1184",
            "note": (
                "The control for the case above, and the diagnostic the issue itself "
                "used: when Discogs finds nothing, the artist fallback works and both "
                "shelved rows come back. Frozen alongside it so a change that 'fixes' the "
                "compilation case by disabling the fallback altogether cannot pass."
            ),
            "query": {"artist": "Arabian Prince", "song": "Qqzzx Nonexistent Track"},
            "requires_rows": [
                ["Arabian Prince", "Brother Arab"],
                ["Arabian Prince", "Innovative Life: The Anthology 1984-1989"],
            ],
            "expect": None,
        },
    ]


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def merge_expectations(
    new_cases: list[dict[str, Any]], existing_path: Path
) -> list[dict[str, Any]]:
    """Carry every existing expectation forward onto the regenerated skeletons.

    A regeneration must never silently re-record verdicts: that would make
    `build_golden_corpus.py` a way to launder a regression into the baseline.
    Cases that already exist keep their expectation; genuinely new cases come
    out with `expect: null` and fail until rebaselined.
    """
    if not existing_path.exists():
        return new_cases
    with existing_path.open(encoding="utf-8") as handle:
        previous = {case["id"]: case for case in json.load(handle)}
    carried = 0
    for case in new_cases:
        prior = previous.get(case["id"])
        if prior and prior.get("expect") is not None:
            case["expect"] = prior["expect"]
            carried += 1
    logger.info("carried %d existing expectations forward", carried)
    dropped = sorted(set(previous) - {c["id"] for c in new_cases})
    if dropped:
        logger.warning(
            "%d case(s) no longer generated and will be dropped: %s", len(dropped), dropped
        )
    return new_cases


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    logger.info("wrote %s", path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the LML#1233 golden corpus fixtures.")
    parser.add_argument(
        "--library-db",
        type=Path,
        default=REPO_ROOT / "library.db",
        help="Production-shaped library.db to sample catalog rows from (read-only).",
    )
    parser.add_argument(
        "--discogs-dsn",
        default="postgresql://localhost:5432/discogs_full",
        help="DSN for a full Discogs dump (release/release_artist/release_track...).",
    )
    parser.add_argument("--out-dir", type=Path, default=GOLDEN_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        import psycopg
    except ImportError:  # pragma: no cover - developer-machine dependency
        raise SystemExit(
            "psycopg is required to regenerate the corpus: `uv run --with psycopg[binary] ...`"
        ) from None

    rows = fetch_library_rows(args.library_db)
    logger.info("seeded %d catalog rows across %d artists", len(rows), len(CORPUS_ARTISTS))

    fixture = Fixture()
    rng = random.Random(SAMPLE_SEED)
    with psycopg.connect(args.discogs_dsn) as connection, connection.cursor() as cursor:
        for spec in FROZEN_RELEASES:
            resolved = resolve_release(cursor, spec)
            if resolved is None:
                raise SystemExit(
                    f"frozen release {spec.key} did not resolve; corpus would be vacuous"
                )
            fixture.releases[spec.key] = resolved
        track_releases = pick_track_releases(cursor, rows, rng, TRACK_RELEASE_COUNT)
        for spec, resolved in track_releases:
            fixture.releases[spec.key] = resolved
        shelf_releases = resolve_shelf_releases(cursor, rows)
        for spec, resolved in shelf_releases:
            fixture.releases[spec.key] = resolved

    build_routes(fixture, track_releases)
    for spec, _resolved in shelf_releases:
        assert spec.library_row is not None
        fixture.route(fixture.album_searches, route_key(*spec.library_row), (spec.key,))
    # These three tables are the deliberate exception the `route()` conflict
    # guard exists to name: a frozen regression case's route is authoritative
    # over anything sampling independently derived for the same key.
    for track, artist, keys in FROZEN_TRACK_ROUTES:
        fixture.route(fixture.track_searches, route_key(track, artist), keys, override=True)
    for album, keys in FROZEN_ALBUM_TITLE_ROUTES:
        fixture.route(fixture.album_title_searches, route_key(album), keys, override=True)
    for artist, album, keys in FROZEN_ALBUM_ROUTES:
        fixture.route(fixture.album_searches, route_key(artist, album), keys, override=True)

    cases = build_sampled_cases(rows, track_releases, rng) + frozen_cases()
    cases.sort(key=lambda case: (not case["frozen"], case["id"]))
    cases = merge_expectations(cases, args.out_dir / "cases.json")

    write_json(args.out_dir / "library.json", rows)
    write_json(args.out_dir / "discogs.json", fixture.to_json())
    write_json(args.out_dir / "cases.json", cases)
    unrecorded = [case["id"] for case in cases if case["expect"] is None]
    logger.info("corpus: %d cases, %d awaiting a recorded verdict", len(cases), len(unrecorded))
    if unrecorded:
        logger.info("run `uv run python -m scripts.rebaseline_golden_corpus` to record them")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
