"""Unit tests for ``scripts/measure_track_recall_gap.py`` (LML#1264).

Pure-logic coverage: shelf classification, the library pair index that mirrors
the production cache filter, artist-variant derivation, the 80/80 resolution
call, and the report's derived figures. The Postgres leg -- the full-catalogue
title scan, the temp-table COPY and the credit sweep -- has no unit coverage at
all and is exercised only in
``tests/integration/test_measure_track_recall_gap_pg.py``. That the pair index
really is the production rule, and not the in-repo dev tool that looks like it,
is pinned in ``tests/unit/test_measure_track_recall_gap_filter_parity.py``.
"""

from __future__ import annotations

import sqlite3

import pytest

from scripts.measure_track_recall_gap import (
    ADMISSION_MODEL_NOTE,
    DERIVED_HEADLINE_KEYS,
    DOCUMENTED_PIN_COVERAGE_NOTE,
    TRACKLIST_CHECK_CAVEAT,
    DiscogsLegCensus,
    GapCensusReport,
    LibraryPairIndex,
    LibraryRow,
    ReleaseCandidate,
    artist_variants,
    load_library_rows,
    naive_like_comp_count,
    render_report,
    resolve_release_for_row,
    split_shelf,
)


def _write_library_db(tmp_path, rows: list[tuple[int, str, str, str]]):
    """Write a ``library``-shaped SQLite table. Rows are ``(id, artist, title, alt)``."""
    db_path = tmp_path / "library.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE library (id INTEGER PRIMARY KEY, artist TEXT, title TEXT, "
        "alternate_artist_name TEXT)"
    )
    con.executemany(
        "INSERT INTO library (id, artist, title, alternate_artist_name) VALUES (?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return str(db_path)


class TestLoadLibraryRows:
    def test_reads_every_row(self, tmp_path):
        db_path = _write_library_db(
            tmp_path,
            [
                (1, "Stereolab", "Aluminum Tunes", None),
                (2, "Various Artists - Reggae", "The Sound of Dub", None),
            ],
        )

        rows = load_library_rows(db_path)

        assert rows == [
            LibraryRow(id=1, artist="Stereolab", title="Aluminum Tunes"),
            LibraryRow(id=2, artist="Various Artists - Reggae", title="The Sound of Dub"),
        ]

    def test_reads_the_alternate_artist_name(self, tmp_path):
        """The alternate name is load-bearing for the RESOLVE leg, not admission.

        LML's own runtime matcher reads ``alternate_artist_name`` (see
        ``artist_matches_item`` in ``lookup/orchestrator.py``), so a census that
        dropped the column would understate what LML can resolve out of the
        cache. The production cache *filter* does not read it -- that asymmetry
        is the census's central finding, and keeping the two legs on different
        artist sets is how the script states it. See ``artist_variants``.
        """
        db_path = _write_library_db(
            tmp_path, [(1, "Company Flow", "The Cold Vein", "Company Flow & Cannibal Ox")]
        )

        assert load_library_rows(db_path) == [
            LibraryRow(
                id=1,
                artist="Company Flow",
                title="The Cold Vein",
                alternate_artist="Company Flow & Cannibal Ox",
            )
        ]

    def test_null_columns_become_empty_strings(self, tmp_path):
        db_path = _write_library_db(tmp_path, [(1, None, None, None)])

        rows = load_library_rows(db_path)

        assert rows == [LibraryRow(id=1, artist="", title="", alternate_artist="")]


class TestArtistVariants:
    def test_bare_row_is_just_its_artist(self):
        assert artist_variants(LibraryRow(1, "Stereolab", "Aluminum Tunes")) == ["Stereolab"]

    def test_alternate_name_is_included(self):
        row = LibraryRow(1, "Common", "Resurrection", alternate_artist="Common Sense")

        assert artist_variants(row) == ["Common", "Common Sense"]

    def test_blank_or_duplicate_alternate_name_adds_nothing(self):
        assert artist_variants(LibraryRow(1, "Sessa", "x", alternate_artist="   ")) == ["Sessa"]
        assert artist_variants(LibraryRow(2, "Sessa", "x", alternate_artist="Sessa")) == ["Sessa"]

    def test_compilation_alternate_name_is_dropped(self):
        """An alternate that names a V/A shelf bucket is a shelving label, not
        a credit LML would ever match a release against, so it must not widen
        the resolve leg's artist set. (The pair index excludes nothing --
        ``LibraryPairs::from_db`` has no compilation branch -- because there the
        artist is only ever half of a pair, so a shelf label cannot admit a
        release on its own.)
        """
        row = LibraryRow(1, "Sessa", "x", alternate_artist="Various Artists - Brazil")

        assert artist_variants(row) == ["Sessa"]


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


class TestLibraryPairIndex:
    """The Python mirror of ``discogs-xml-converter``'s ``LibraryPairs``.

    Parity with the real rule is pinned separately, behaviourally, in
    ``test_measure_track_recall_gap_filter_parity.py``. These are the mirror's
    own mechanics.
    """

    def test_index_is_keyed_by_normalized_title(self):
        index = LibraryPairIndex.from_library_rows(
            [
                LibraryRow(1, "Autechre", "Confield"),
                LibraryRow(2, "Autechre", "Amber"),
                LibraryRow(3, "Stereolab", "Aluminum Tunes"),
            ]
        )

        assert len(index) == 3
        assert index.artists_for_title("confield") == frozenset({"autechre"})

    def test_several_artists_collapse_under_one_title(self):
        """The inverted index is title -> *set* of artists, which is what makes
        a colliding title (``untitled``, ``7``) cheap to reject per credit
        rather than per library row.
        """
        index = LibraryPairIndex.from_library_rows(
            [LibraryRow(1, "Various Artists", "Untitled"), LibraryRow(2, "Zapp", "Untitled")]
        )

        assert len(index) == 1
        assert index.artists_for_title("untitled") == frozenset({"various artists", "zapp"})
        assert index.pair_count == 2

    def test_duplicate_rows_collapse(self):
        index = LibraryPairIndex.from_library_rows(
            [LibraryRow(i, "Stereolab", "Aluminum Tunes") for i in range(1, 4)]
        )

        assert index.pair_count == 1

    def test_diacritics_fold_on_both_sides(self):
        """``to_match_form`` on both the library and the release side is the
        whole reason a diacritic-mismatched pair still collides -- the property
        ``library_pairs.rs`` pins as ``normalize_title_parity_with_artist``.
        """
        index = LibraryPairIndex.from_library_rows([LibraryRow(1, "Nilüfer Yanya", "PAINLESS")])

        assert index.admits("painless", ["Nilufer Yanya"])
        assert index.admits("Painless", ["Nilüfer Yanya"])

    def test_empty_artist_or_title_rows_are_skipped(self):
        index = LibraryPairIndex.from_library_rows(
            [
                LibraryRow(1, "", "Confield"),
                LibraryRow(2, "Autechre", ""),
                LibraryRow(3, "   ", "  "),
                LibraryRow(4, "Autechre", "Amber"),
            ]
        )

        assert len(index) == 1
        assert index.artists_for_title("amber") == frozenset({"autechre"})

    def test_admits_when_any_credited_artist_matches(self):
        """A Discogs release carries several credits; the rule needs one hit.

        This is also the shape that rescues a compound-credited release whose
        *first* credit is not the library's artist.
        """
        index = LibraryPairIndex.from_library_rows(
            [LibraryRow(1, "Field, The", "From Here We Go Sublime")]
        )

        assert index.admits("From Here We Go Sublime", ["Some Producer", "Field, The"])

    def test_an_unknown_title_short_circuits(self):
        index = LibraryPairIndex.from_library_rows([LibraryRow(1, "Autechre", "Confield")])

        assert not index.admits("Some Other Album", ["Autechre"])
        assert index.artists_for_title("some other album") == frozenset()

    def test_an_empty_title_is_never_admitted(self):
        index = LibraryPairIndex.from_library_rows([LibraryRow(1, "Autechre", "Confield")])

        assert not index.admits("", ["Autechre"])

    def test_the_reproducer_pair_is_rejected(self):
        """LML#1264's own row, at the substrate rather than the matcher.

        Library 3835 is credited plain "Zapp"; every Discogs pressing of that
        title is credited "Zapp & Roger". The pair rule never admits one, so
        the release is not in the cache to be matched -- which is why no
        threshold or fold in LML can reach it. Wall 6, stated as a test.
        """
        index = LibraryPairIndex.from_library_rows(
            [LibraryRow(3835, "Zapp", "All the Greatest Hits")]
        )

        assert index.admits("All The Greatest Hits", ["Zapp"])
        assert not index.admits("All The Greatest Hits", ["Zapp & Roger"])


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

    def test_resolves_through_the_alternate_artist_name(self):
        """The complement of the Zapp case above: where the library *does*
        record the compound credit as its alternate name, the real prod cache
        admits the release under that name, so the census must resolve it
        rather than counting the row as unreachable. The variant set goes to
        ``find_best_typed_match``'s existing ``query_artist`` iterable support
        -- no matcher change, no fabricated bridge.
        """
        row = LibraryRow(
            1, "Company Flow", "The Cold Vein", alternate_artist="Company Flow & Cannibal Ox"
        )
        candidate = ReleaseCandidate(
            release_id=200, title="The Cold Vein", artist_name="Company Flow & Cannibal Ox"
        )

        assert resolve_release_for_row(row, [candidate]) == candidate

    def test_picks_best_of_several_candidates(self):
        row = LibraryRow(1, "Large Professor", "1st Class")
        wrong = ReleaseCandidate(release_id=1, title="Class Act", artist_name="Large Professor")
        right = ReleaseCandidate(release_id=2, title="1st Class", artist_name="Large Professor")

        result = resolve_release_for_row(row, [wrong, right])

        assert result == right


def _leg(
    *,
    pair_admitted: int = 60,
    pair_admitted_and_resolvable: int = 58,
    resolvable: int = 62,
    with_cached_tracklist: int = 55,
) -> DiscogsLegCensus:
    """A measured Discogs leg with plausible fixed counts."""
    return DiscogsLegCensus(
        source="postgresql://localhost/discogs_full",
        admitted_release_count=1234,
        pair_admitted=pair_admitted,
        pair_admitted_and_resolvable=pair_admitted_and_resolvable,
        resolvable=resolvable,
        with_cached_tracklist=with_cached_tracklist,
    )


def _report(discogs: DiscogsLegCensus | None) -> GapCensusReport:
    """A census report with fixed library counts, varying only the Discogs leg.

    The leg is positional and has no default: ``None`` means "the Discogs
    measurement was skipped", which is a distinct case these tests assert on
    heavily, and a defaulted keyword would let a caller mean it by accident.
    """
    return GapCensusReport(
        total_library_rows=100,
        comp_shelf_count=10,
        comp_shelf_naive_like_count=9,
        artist_shelf_count=90,
        discogs=discogs,
    )


class TestGapCensusReport:
    def test_to_dict_derives_every_headline_figure(self):
        """The five derived figures, read through the serializer that emits them.

        "Would need new collection" is three different problems wanting three
        different remedies, and the split is the point of the census. A row the
        pair filter never admits has no candidate to score, so no threshold or
        fold inside LML reaches it -- only a wider cache filter, or new data
        upstream, does. A row that IS admitted but clears no title floor is the
        opposite case: the release is right there, and a matcher change moves
        it. Deriving the split here means a reader never has to subtract, and
        can't subtract wrong.
        """
        # 90 artist-shelf; 60 pair-admitted, 58 of those resolvable; 62
        # resolvable in total (so 4 rescued by a sibling row's admission); 55
        # tracklisted.
        as_dict = _report(_leg()).to_dict()

        assert as_dict["could_gain_recall_no_new_collection"] == 55
        assert as_dict["would_need_new_collection"] == 35  # 90 - 55
        assert as_dict["artist_shelf_not_pair_admitted"] == 30  # 90 - 60
        assert as_dict["artist_shelf_pair_admitted_but_below_floor"] == 2  # 60 - 58
        assert as_dict["artist_shelf_resolvable_without_pair_admission"] == 4  # 62 - 58

    def test_the_leg_is_flattened_into_the_serialized_report(self):
        """The measured Discogs figures have to reach the artifact under their
        own names, not nested behind a key a reader has to know to open."""
        as_dict = _report(_leg()).to_dict()

        assert as_dict["discogs_measurement"] == "measured"
        assert as_dict["discogs_source"] == "postgresql://localhost/discogs_full"
        assert as_dict["artist_shelf_pair_admitted"] == 60
        assert as_dict["artist_shelf_with_resolvable_release"] == 62
        assert as_dict["artist_shelf_with_cached_tracklist"] == 55
        assert as_dict["admitted_release_count"] == 1234

    def test_to_dict_carries_the_caveats(self):
        """A serialized report must travel with the reasons its figures cannot
        be read at face value -- a JSON file outlives the console run that
        produced it.
        """
        as_dict = _report(_leg()).to_dict()

        assert as_dict["admission_model_note"] == ADMISSION_MODEL_NOTE
        assert as_dict["tracklist_check_caveat"] == TRACKLIST_CHECK_CAVEAT
        assert as_dict["pin_coverage_note"] == DOCUMENTED_PIN_COVERAGE_NOTE

    @pytest.mark.parametrize("key", DERIVED_HEADLINE_KEYS)
    def test_a_skipped_run_serializes_no_headline(self, key):
        """A library-only run measured none of this, and the artifact must say
        so rather than serialize a zero that reads as a finding. Post-correction
        this is structural -- there is no Discogs leg to read a zero out of --
        but it is pinned behaviourally anyway, parametrized over the whole
        roster so a sixth derived figure cannot be added outside the guarantee.
        """
        as_dict = _report(None).to_dict()

        assert as_dict["discogs_measurement"] == "skipped"
        assert as_dict[key] is None

    @pytest.mark.parametrize(
        "key",
        [
            "discogs_source",
            "admitted_release_count",
            "artist_shelf_pair_admitted",
            "artist_shelf_with_resolvable_release",
            "artist_shelf_with_cached_tracklist",
            "tracklist_check_caveat",
            "admission_model_note",
        ],
    )
    def test_a_skipped_run_serializes_no_discogs_figure_at_all(self, key):
        """The mirror of the rule above, and the reason the leg is a separate
        optional structure rather than a handful of zeroed fields: no number,
        and no reading instructions for a number that isn't there. A caveat
        about a check that never ran implies a check that ran.
        """
        assert key not in _report(None).to_dict()


class TestRenderReport:
    def test_names_the_admission_model_it_simulated(self):
        text = render_report(_report(_leg()))

        assert ADMISSION_MODEL_NOTE in text
        assert "postgresql://localhost/discogs_full" in text
        assert DOCUMENTED_PIN_COVERAGE_NOTE in text

    def test_marks_the_tracklist_line_non_discriminating(self):
        """The tracklist check cannot fail against a full Discogs dump -- nearly
        every release in one carries ``release_track`` rows -- so its figure must
        never be rendered as a bare coverage finding.
        """
        text = render_report(_report(_leg(with_cached_tracklist=62)))

        assert "NON-DISCRIMINATING" in text
        assert TRACKLIST_CHECK_CAVEAT in text

    def test_notes_skip_when_no_discogs_source(self):
        text = render_report(_report(None))

        assert "SKIPPED" in text
        assert ADMISSION_MODEL_NOTE not in text
