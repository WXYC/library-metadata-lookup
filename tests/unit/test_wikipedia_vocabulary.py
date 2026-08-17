"""One vocabulary for "this page is a work, not an artist" (LML#1192 cross-PR review, round 7).

Through round 6 the policy was encoded as two hand-maintained word lists
that had already drifted once inside a single review cycle (``mixtape``
was added to each separately, by different findings): the slug denylist in
``lookup/wikipedia_candidates.py`` and the description-reject nouns in
``clients/wikipedia.py``. The drift was not cosmetic. Round 6's P2-2
demoted the slug denylist to try-last for fetch-capable callers on the
explicit theory that the fetched payload is the authoritative check — but
the payload check's vocabulary was missing four of the denylist's ten
words (``film``, ``soundtrack``, ``tv series``, ``discography``), so an
artist whose Discogs URLs named only a film/soundtrack page could get
that page's synopsis written as their bio with no backstop at all.

These tests pin the fix: ``clients/wikipedia.py`` owns TWO public
vocabularies (``RELEASE_NOUNS`` — things credited to an artist via a
preposition — and ``NON_ARTIST_PAGE_TYPES`` — page kinds that are never
an artist, with their own head-position description grammar), the slug
denylist is DERIVED from their union rather than hand-maintained, and the
description backstop rejects page-type descriptions it previously passed.
Deriving the denylist also closes the reverse hole: non-English release
qualifiers (``Grace_(álbum)``) previously sailed through the
English-only slug list.
"""

from __future__ import annotations

from clients.wikipedia import (
    NON_ARTIST_PAGE_TYPES,
    RELEASE_NOUNS,
    _is_rejected_description,
)
from lookup.wikipedia_candidates import _HARD_REJECT_QUALIFIERS, is_hard_rejected_slug


class TestSharedVocabularyDerivation:
    def test_slug_denylist_is_exactly_the_shared_union(self):
        # Equality, not superset: a new qualifier must be added to the
        # shared vocabulary in clients/wikipedia.py (where the description
        # backstop derives its grammar too), never hand-appended here --
        # per-site additions are exactly how the two lists drifted apart
        # across review rounds 2-6.
        assert _HARD_REJECT_QUALIFIERS == frozenset(RELEASE_NOUNS) | frozenset(
            NON_ARTIST_PAGE_TYPES
        )

    def test_every_pre_round7_denylist_word_is_still_covered(self):
        # The round-6 denylist, verbatim -- the derivation must be a
        # strict widening, never a regression.
        assert {
            "album",
            "song",
            "single",
            "ep",
            "soundtrack",
            "film",
            "tv series",
            "discography",
            "mixtape",
            "compilation",
        } <= _HARD_REJECT_QUALIFIERS

    def test_non_english_release_qualifiers_now_reject(self):
        # The reverse hole: es/fr/de/it release qualifiers passed the
        # English-only list. (Sessa's "Grace" -- the review rounds' own
        # live-verified multilingual example set.)
        assert is_hard_rejected_slug("Grace (álbum)")
        assert is_hard_rejected_slug("Grace (canción)")
        assert is_hard_rejected_slug("Grace (chanson)")

    def test_artist_qualifiers_still_pass(self):
        assert not is_hard_rejected_slug("Low (band)")
        assert not is_hard_rejected_slug("Cat Power (musician)")
        assert not is_hard_rejected_slug("Stereolab")


class TestPageTypeDescriptionBackstop:
    def test_film_and_type_page_descriptions_reject(self):
        # The hole round 6's include_denylisted demotion left open: a
        # standard page whose description names a non-artist page TYPE in
        # head position. "Heat (film)"-shaped payloads must not become an
        # artist bio.
        assert _is_rejected_description("1995 American crime film directed by Michael Mann")
        assert _is_rejected_description("2019 film by Claire Denis")
        assert _is_rejected_description("American television series")
        assert _is_rejected_description("British TV series")
        assert _is_rejected_description("The discography of the American band Broadcast")

    def test_artist_descriptions_with_type_words_mid_phrase_still_pass(self):
        # The type noun must be the phrase HEAD -- people described via
        # film/TV work are artists, not pages about works.
        assert not _is_rejected_description("American film composer")
        assert not _is_rejected_description("British composer of film and television scores")
        assert not _is_rejected_description("American singer-songwriter and film director")

    def test_round_6_regression_fixtures_hold(self):
        # The round-6 P2-1 accept/reject set, re-pinned so the new
        # page-type pattern can't reintroduce the disco-band class.
        assert not _is_rejected_description("1977 British disco band")
        assert not _is_rejected_description("1970 American disco group")
        assert not _is_rejected_description("Canción de autor española")
        assert _is_rejected_description("álbum de Grace")
        assert _is_rejected_description("2015 studio album by Sufjan Stevens")
