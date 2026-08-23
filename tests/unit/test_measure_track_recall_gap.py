"""Unit tests for ``scripts/measure_track_recall_gap.py`` (LML#1264).

Pure-logic + mocked-DB coverage. The real batched-query round trip against
Postgres is exercised separately in
``tests/integration/test_measure_track_recall_gap_pg.py``.
"""

from __future__ import annotations

import sqlite3

from scripts.measure_track_recall_gap import (
    DOCUMENTED_PIN_COVERAGE_NOTE,
    TRACKLIST_CHECK_CAVEAT,
    GapCensusReport,
    LibraryRow,
    ReleaseCandidate,
    load_library_rows,
    naive_like_comp_count,
    render_report,
    resolve_release_for_row,
    split_shelf,
)


def _write_library_db(tmp_path, rows: list[tuple[int, str, str]]):
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT)")
    con.executemany("INSERT INTO library (id, artist, title) VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    return str(db_path)


class TestLoadLibraryRows:
    def test_reads_every_row(self, tmp_path):
        db_path = _write_library_db(
            tmp_path,
            [
                (1, "Stereolab", "Aluminum Tunes"),
                (2, "Various Artists - Reggae", "The Sound of Dub"),
            ],
        )

        rows = load_library_rows(db_path)

        assert rows == [
            LibraryRow(id=1, artist="Stereolab", title="Aluminum Tunes"),
            LibraryRow(id=2, artist="Various Artists - Reggae", title="The Sound of Dub"),
        ]

    def test_null_artist_or_title_becomes_empty_string(self, tmp_path):
        db_path = _write_library_db(tmp_path, [(1, None, None)])

        rows = load_library_rows(db_path)

        assert rows == [LibraryRow(id=1, artist="", title="")]


class TestNaiveLikeCompCount:
    def test_counts_only_the_two_literal_prefixes(self):
        rows = [
            LibraryRow(1, "Various Artists - Reggae", "x"),
            LibraryRow(2, "Soundtracks - L", "y"),
            LibraryRow(3, "V/A", "z"),
            LibraryRow(4, "Zapp", "w"),
        ]

        assert naive_like_comp_count(rows) == 2


class TestSplitShelf:
    def test_classifier_catches_more_than_the_naive_heuristic(self):
        """LML#1264's brief: verify the comp-shelf count independently. The
        shared ``is_compilation_artist`` classifier recognizes shelf forms
        ("V/A", lowercase "various") the naive ``LIKE`` guess misses -- this
        pins that divergence so a future classifier change can't silently
        make the two counts agree without anyone noticing.
        """
        rows = [
            LibraryRow(1, "Various Artists - Reggae", "x"),
            LibraryRow(2, "Soundtracks - L", "y"),
            LibraryRow(3, "V/A", "z"),
            LibraryRow(4, "Zapp", "w"),
            LibraryRow(5, "Stereolab", "v"),
        ]

        census = split_shelf(rows)

        assert census.total == 5
        assert [r.id for r in census.comp_shelf] == [1, 2, 3]
        assert [r.id for r in census.artist_shelf] == [4, 5]
        assert census.comp_shelf_naive_like_count == 2  # misses row 3, "V/A"

    def test_empty_library_is_all_zero(self):
        census = split_shelf([])

        assert census.total == 0
        assert census.comp_shelf == []
        assert census.artist_shelf == []
        assert census.comp_shelf_naive_like_count == 0


class TestResolveReleaseForRow:
    def test_no_candidates_is_no_match(self):
        row = LibraryRow(1, "Zapp", "All the Greatest Hits")

        assert resolve_release_for_row(row, []) is None

    def test_clean_exact_match_resolves(self):
        row = LibraryRow(1, "Stereolab", "Aluminum Tunes")
        candidate = ReleaseCandidate(
            release_id=100, title="Aluminum Tunes", artist_name="Stereolab"
        )

        result = resolve_release_for_row(row, [candidate])

        assert result == candidate

    def test_compound_artist_credit_fails_the_80_80_floor(self):
        """LML#1264's reproducer, wall 5: shelf row is credited plain "Zapp";
        the only Discogs release under that title is credited "Zapp & Roger".
        No fabricated bridge exists between the two at the SAME floor the
        artwork path already uses (LML#478) -- this must resolve to no match,
        not a false positive, and it must fail *because of* the artist axis
        (there's no legitimate title mismatch here to blame it on instead).
        """
        row = LibraryRow(3835, "Zapp", "All the Greatest Hits")
        candidate = ReleaseCandidate(
            release_id=134233, title="All The Greatest Hits", artist_name="Zapp & Roger"
        )

        assert resolve_release_for_row(row, [candidate]) is None

    def test_picks_best_of_several_candidates(self):
        row = LibraryRow(1, "Large Professor", "1st Class")
        wrong = ReleaseCandidate(release_id=1, title="Class Act", artist_name="Large Professor")
        right = ReleaseCandidate(release_id=2, title="1st Class", artist_name="Large Professor")

        result = resolve_release_for_row(row, [wrong, right])

        assert result == right


def _report(
    *,
    artist_shelf_with_cached_tracklist: int = 40,
    discogs_source: str | None = "postgresql://localhost/discogs_full",
) -> GapCensusReport:
    """A census report with plausible fixed counts, varying only what a test asserts on."""
    return GapCensusReport(
        total_library_rows=100,
        comp_shelf_count=10,
        comp_shelf_naive_like_count=9,
        artist_shelf_count=90,
        artist_shelf_with_exact_artist_match=60,
        artist_shelf_with_resolvable_release=50,
        artist_shelf_with_cached_tracklist=artist_shelf_with_cached_tracklist,
        discogs_source=discogs_source,
    )


class TestGapCensusReport:
    def test_headline_properties(self):
        report = _report()

        assert report.could_gain_recall_no_new_collection == 40
        assert report.would_need_new_collection == 50  # 90 - 40

    def test_to_dict_includes_derived_headline_fields(self):
        report = _report()

        as_dict = report.to_dict()

        assert as_dict["could_gain_recall_no_new_collection"] == 40
        assert as_dict["would_need_new_collection"] == 50
        assert as_dict["pin_coverage_note"] == DOCUMENTED_PIN_COVERAGE_NOTE

    def test_to_dict_carries_the_tracklist_caveat(self):
        """A serialized report must travel with the reason its tracklist figure
        cannot be read as coverage -- a JSON file outlives the console run that
        produced it, and a bare ``artist_shelf_with_cached_tracklist`` equal to
        ``artist_shelf_with_resolvable_release`` reads as "solved" otherwise.
        """
        assert _report().to_dict()["tracklist_check_caveat"] == TRACKLIST_CHECK_CAVEAT


class TestRenderReport:
    def test_labels_discogs_numbers_as_upper_bound(self):
        text = render_report(_report())

        assert "UPPER BOUND" in text
        assert "postgresql://localhost/discogs_full" in text
        assert DOCUMENTED_PIN_COVERAGE_NOTE in text

    def test_marks_the_tracklist_line_non_discriminating(self):
        """The tracklist check cannot fail against an unfiltered dump -- nearly
        every release in a full Discogs dump carries ``release_track`` rows --
        so its figure must never be rendered as a bare coverage finding. The
        rendered output has to say that, and has to name the constraint that
        really binds (membership in the filtered prod discogs-cache) as the one
        this script does not measure.
        """
        # The exact shape the real census produced: every resolvable row also
        # tracklisted, which is the reading most likely to be mistaken for
        # "solved" if it is rendered without the caveat.
        text = render_report(_report(artist_shelf_with_cached_tracklist=50))

        assert "NON-DISCRIMINATING" in text
        assert TRACKLIST_CHECK_CAVEAT in text

    def test_notes_skip_when_no_discogs_source(self):
        text = render_report(_report(artist_shelf_with_cached_tracklist=0, discogs_source=None))

        assert "SKIPPED" in text
        assert "UPPER BOUND" not in text
