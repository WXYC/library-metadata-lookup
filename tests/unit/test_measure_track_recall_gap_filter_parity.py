"""LML#1264: tripwire pinning the census against the cache filter it forks.

``scripts/measure_track_recall_gap.py`` reproduces, in its own SQL and its own
``artist_variants``, the artist-admission rule that
``scripts/build_filtered_discogs.py`` applies when it builds the real filtered
prod discogs-cache: case-folded, 200-char-truncated equality against the union
of ``library.artist`` and ``library.alternate_artist_name``, with compilation
strings excluded from both columns.

The fork is deliberate. ``extract_library_artists`` returns a flat deduped set
for a bulk ``INSERT ... SELECT``; the census needs per-row variants so it can
attribute a match back to a specific library row, and coupling a one-off
analysis script to a production ETL entry point to share a set-builder would be
the wrong trade. But a decision recorded in prose is held in agreement by
nothing -- LML#1244's own review found exactly such a fork had already drifted
unnoticed, and this one has a live drift path: ``build_filtered_discogs.py``
already advertises a trigram Phase 2 in its comments that the code below does
not implement. The day someone makes that comment true, the census would keep
printing numbers under a docstring claiming it mirrors the real filter.

Every Discogs-side number the census reports rests on that claim, so the claim
is pinned here rather than asserted in a docstring. Two halves: the artist set
(behavioural, over a real tmp ``library.db``) and the SQL predicate (textual,
because there is no way to execute one filter's join against the other).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from scripts.build_filtered_discogs import extract_library_artists
from scripts.measure_track_recall_gap import _EXACT_ARTIST_MATCH_SQL, artist_variants, split_shelf
from scripts.measure_track_recall_gap import load_library_rows as census_load

#: Rows chosen so every branch of ``extract_library_artists`` is exercised: a
#: plain artist, an artist carrying an alternate name, a comp-shelf row whose
#: artist string must be excluded, and an artist-shelf row whose *alternate*
#: names a comp bucket and must be excluded from the alternate column too.
_PARITY_ROWS = [
    (1, "Stereolab", "Aluminum Tunes", None),
    (2, "Company Flow", "The Cold Vein", "Company Flow & Cannibal Ox"),
    (3, "Various Artists - Reggae", "The Sound of Dub", None),
    (4, "Sessa", "Pequena Vertigem de Amor", "Various Artists - Brazil"),
    (5, "Juana Molina", "DOGA", "Juana Molina y los Hermanos"),
]

_BUILD_FILTERED_DISCOGS = Path(__file__).resolve().parents[2] / "scripts/build_filtered_discogs.py"

#: The two fragments that together *are* the admission rule. ``extra = 0`` picks
#: the primary credit; the ``lower(left(..., 200))`` equality is the fold. If
#: either changes in the real filter, the census's numbers stop describing what
#: the prod cache can see.
_PREDICATE_FRAGMENTS = (
    "ra.extra = 0",
    "lower(left(ra.artist_name, 200)) = lower(",
)


def _write_library_db(tmp_path) -> str:
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, "
        "alternate_artist_name TEXT)"
    )
    con.executemany(
        "INSERT INTO library (id, artist, title, alternate_artist_name) VALUES (?, ?, ?, ?)",
        _PARITY_ROWS,
    )
    con.commit()
    con.close()
    return str(db_path)


def _census_artist_set(db_path: str) -> set[str]:
    """Every name the census would send to the exact-match join."""
    census = split_shelf(census_load(db_path))
    return {name for row in census.artist_shelf for name in artist_variants(row)}


class TestArtistSetParity:
    def test_census_admits_exactly_what_the_real_filter_admits(self, tmp_path):
        """The census's artist set must equal the real filter's.

        Not a subset assertion: a census that admitted *fewer* names would
        undercount resolvable rows (the LML#1264 review's original defect --
        it read ``artist`` alone), and one that admitted more would overstate
        coverage the prod cache does not have. Both directions are wrong, so
        both are pinned.
        """
        db_path = _write_library_db(tmp_path)

        assert _census_artist_set(db_path) == set(extract_library_artists(db_path))

    def test_the_fixture_actually_exercises_the_rule(self, tmp_path):
        """Vacuity guard: two equal empty sets would pass the test above.

        Pins that the fixture really carries an alternate name that is admitted,
        a comp-shelf artist that is excluded, and a comp-shelf *alternate* that
        is excluded -- so a fold that silently stopped reading the alternate
        column, or stopped excluding compilations, fails loudly.
        """
        names = _census_artist_set(_write_library_db(tmp_path))

        assert "Company Flow & Cannibal Ox" in names  # alternate column is read
        assert "Various Artists - Reggae" not in names  # comp artist excluded
        assert "Various Artists - Brazil" not in names  # comp alternate excluded
        assert len(names) >= 5


class TestPredicateParity:
    def test_both_filters_carry_the_same_admission_predicate(self):
        source = _BUILD_FILTERED_DISCOGS.read_text()

        for fragment in _PREDICATE_FRAGMENTS:
            assert fragment in source, f"{fragment!r} no longer in build_filtered_discogs.py"
            assert fragment in _EXACT_ARTIST_MATCH_SQL, f"{fragment!r} no longer in the census SQL"

    def test_the_real_filter_still_has_exactly_one_such_join(self):
        """Vacuity guard for the textual half.

        A substring check passes happily against a file that has grown a second,
        differently-folded join -- which is precisely the drift this module
        exists to catch, since ``build_filtered_discogs.py`` already advertises
        an unwritten trigram Phase 2. Finding more than one artist-name join
        means the census now mirrors only part of the real filter.
        """
        joins = re.findall(r"lower\(left\(ra\.artist_name", _BUILD_FILTERED_DISCOGS.read_text())

        assert len(joins) == 1, (
            f"build_filtered_discogs.py now has {len(joins)} artist-name joins; the census "
            "mirrors one of them. Re-check scripts/measure_track_recall_gap.py before trusting "
            "any number it prints."
        )
