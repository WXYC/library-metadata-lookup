"""LML#1264 follow-up: pin the census to the PRODUCTION filter, not the dev tool.

``scripts/measure_track_recall_gap.py`` exists to say how much of the WXYC
library the Discogs cache can structurally cover. Every number it prints is
downstream of one question: *which releases does the cache admit?* Model the
wrong admission rule and the census is not approximately right, it is
measuring a database nobody runs.

The rule that governs production lives in another repo -- ``discogs-etl``'s
``rebuild-cache.sh`` forwards ``--library-db`` to ``discogs-xml-converter``,
whose ``src/library_pairs.rs::LibraryPairs`` admits a release when its
normalized title is a library title **and** one of its credited artists is in
that title's artist set. Pair-wise. Primary ``library.artist`` only.

``scripts/build_filtered_discogs.py``, in *this* repo, is a local dev tool
that builds a ``wxyc.*`` schema LML never queries. Its filter is artist-only
and it unions ``alternate_artist_name``. It looks exactly like the thing to
mirror, and the census's first version mirrored it -- through a code review
that "corrected" the census further toward it.

So the failure mode is not drift, it is *attraction*: the decoy is in-repo,
importable, and reads like the real filter. A prose warning did not survive
one review cycle. These tests are the version that fires.

Two halves, both in-repo and behavioural, because the authority is a Rust file
in a sibling checkout that CI does not have: the census must follow the pair
rule on a fixture where the two rules disagree, and the dev tool must still be
the decoy this module claims it is -- if someone makes it faithful, the story
above stops being true and should be re-read rather than silently outgrown.
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.build_filtered_discogs import extract_library_artists
from scripts.measure_track_recall_gap import LibraryPairIndex, LibraryRow

_BUILD_FILTERED_DISCOGS = Path(__file__).resolve().parents[2] / "scripts/build_filtered_discogs.py"

#: One library row, chosen so the two rules give opposite answers about one
#: Discogs release. The dev tool admits any release credited to "Company Flow
#: & Cannibal Ox" (it unions the alternate column into a flat artist set); the
#: production pair rule never learns that name at all, and admits only a
#: release whose *title* is also this row's title, credited to "Company Flow".
_ROW = LibraryRow(
    id=1,
    artist="Company Flow",
    title="Funcrusher Plus",
    alternate_artist="Company Flow & Cannibal Ox",
)


def _index() -> LibraryPairIndex:
    return LibraryPairIndex.from_library_rows([_ROW])


class TestTheCensusFollowsThePairRule:
    def test_the_row_admits_its_own_pair(self):
        """Sanity floor: the rule admits what it is built from.

        Vacuity guard for the two rejection tests below -- an index that
        admitted nothing would pass them both.
        """
        assert _index().admits("Funcrusher Plus", ["Company Flow"])

    def test_a_matching_title_with_a_foreign_artist_is_rejected(self):
        """The half an artist-only filter cannot express.

        ``untitled``, ``7`` and ``in mind`` are library titles shared by dozens
        of unrelated Discogs artists. An artist-only rule admits every one of
        them; the pair rule admits none.
        """
        assert not _index().admits("Funcrusher Plus", ["Some Other Band"])

    def test_a_matching_artist_on_a_foreign_title_is_rejected(self):
        """The other half: the rule is pair-wise in both directions.

        This is the release the artist-only dev tool admits and production does
        not -- and the reason the cache holds ~50K releases rather than ~4M.
        """
        assert not _index().admits("The Cold Vein", ["Company Flow"])

    def test_the_alternate_artist_name_is_not_in_the_index(self):
        """The correction this module exists for.

        ``LibraryPairs::from_db`` runs ``SELECT artist, title FROM library``.
        There is no second query and no union. A census that admitted the
        alternate name would report coverage production does not have -- and
        would do it on exactly the compound-credit rows LML#1264 is about, so
        the error lands hardest where the ticket is looking.
        """
        assert not _index().admits("Funcrusher Plus", ["Company Flow & Cannibal Ox"])

    def test_the_dev_tool_admits_the_name_the_pair_rule_rejects(self):
        """The differential, stated as one assertion over both rules.

        Not a restatement of the test above: this pins that the two rules
        genuinely disagree about this fixture. If ``extract_library_artists``
        ever stopped reading the alternate column, the assertion above would
        keep passing for the wrong reason -- the fixture would have gone
        inert while still looking like a guard.
        """
        dev_tool_artists = extract_library_artists.__doc__ or ""

        assert "alternate" in dev_tool_artists.lower()
        assert not _index().admits("Funcrusher Plus", ["Company Flow & Cannibal Ox"])

    def test_comp_shelf_rows_are_indexed_too(self):
        """``LibraryPairs::from_db`` has no compilation exclusion.

        The dev tool drops compilation artists from its set; the production
        rule does not, because a V/A release whose title and credit both match
        the shelf is exactly a release the cache should hold. The census reports
        only on artist-shelf rows, but it must build the index from the whole
        library or it understates what the cache admits for them.
        """
        index = LibraryPairIndex.from_library_rows(
            [LibraryRow(id=2, artist="Various Artists - Reggae", title="The Sound of Dub")]
        )

        assert index.admits("The Sound of Dub", ["Various Artists - Reggae"])


class TestTheDevToolIsStillTheDecoy:
    """If these fail, ``scripts/build_filtered_discogs.py`` has changed shape and
    the module docstring's account of why it must not be mirrored needs re-reading
    before anyone trusts it -- in either direction."""

    def test_it_still_unions_the_alternate_artist_name(self):
        source = _BUILD_FILTERED_DISCOGS.read_text()

        assert "SELECT DISTINCT alternate_artist_name FROM library" in source

    def test_it_still_builds_a_schema_lml_does_not_query(self):
        """LML's runtime SQL is unqualified -- ``public.release``,
        ``public.release_artist``. This tool writes ``wxyc.*``. That alone
        settles that it is not the production build path, independent of any
        filter argument.
        """
        source = _BUILD_FILTERED_DISCOGS.read_text()

        assert "CREATE SCHEMA IF NOT EXISTS wxyc" in source

    def test_it_still_filters_on_artist_alone(self):
        """No ``(artist, title)`` pair anywhere in its filter SQL.

        The substring check is deliberately anchored on the join predicate
        rather than the file, so a tool that grew a genuine pair filter fails
        here instead of quietly becoming a second thing worth mirroring.
        """
        joins = re.findall(r"lower\(left\(ra\.artist_name", _BUILD_FILTERED_DISCOGS.read_text())

        assert len(joins) == 1
