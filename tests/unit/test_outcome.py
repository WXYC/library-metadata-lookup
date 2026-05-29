"""Tests for the ``Outcome`` value type and ``_apply`` helper (#399 step 2).

``Outcome`` replaces the heterogeneous ``(list, Any)`` tuples each strategy
returned in step 1. The runner has exactly one ``_apply(state, outcome)``
write site; cancellation safety becomes structural (a cancelled ``attempt()``
never returns an ``Outcome``, so no commit happens, period).

The ``Outcome`` named constructors encode the four real effect-shapes that
step-1's tuple second-elements expressed inline:

    - ``empty()``                 -- no-op
    - ``found(items)``            -- direct hit, song matched
    - ``artist_fallback(items)``  -- fell through to artist-only / artist+song
    - ``compilation(items, titles)`` -- TRACK_ON_COMPILATION's signal
    - ``track_match(items, hints)``  -- SONG_AS_TRACK's per-id hint set

These tests pin the value semantics. The runner / strategy tests live in
``test_search.py`` next to the rest of the pipeline tests.
"""

import pytest

from core.search import Outcome, SearchState, _apply
from generated.api_models import TrackMatchHint, TrackMatchSource
from tests.factories import make_library_item as _item

# ---------------------------------------------------------------------------
# Outcome constructors
# ---------------------------------------------------------------------------


class TestOutcomeConstructors:
    """The five named constructors encode the four real effect-shapes plus the no-op."""

    def test_empty_is_no_op(self):
        """``Outcome.empty()`` carries no items and signals no state writes."""
        o = Outcome.empty()
        assert o.items == []
        assert o.song_not_found_after is None
        assert o.found_on_compilation_after is False
        assert o.preserve_prior_results_as_fallback is False
        assert o.discogs_titles is None
        assert o.matched_via_by_id is None

    def test_found_clears_song_not_found_flag(self):
        """``Outcome.found(items)`` is the 90% case: direct match, song matched."""
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")
        o = Outcome.found([item])
        assert o.items == [item]
        assert o.song_not_found_after is False
        assert o.found_on_compilation_after is False
        assert o.preserve_prior_results_as_fallback is False

    def test_album_match_leaves_song_not_found_alone(self):
        """``Outcome.album_match(items)`` writes items but doesn't touch the flag.

        ARTIST_PLUS_ALBUM's direct path: it matched the artist's album by name
        but can't vouch for the song's presence on it. Used when
        ``resolve_albums_for_track`` already set ``song_not_found=True``
        (Discogs returned only VA releases) — the flag must propagate so
        TRACK_ON_COMPILATION downstream still considers the compilation path.
        """
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")
        o = Outcome.album_match([item])
        assert o.items == [item]
        assert o.song_not_found_after is None  # leave-alone signal
        assert o.found_on_compilation_after is False
        assert o.preserve_prior_results_as_fallback is False

    def test_artist_fallback_sets_song_not_found_flag(self):
        """``Outcome.artist_fallback(items)`` records that the song wasn't matched.

        Mirrors ARTIST_PLUS_ALBUM's `fallback_used=True` from step 1: results
        came from the artist-only / artist+song fallback path, so the song
        title didn't get a direct hit. Downstream TRACK_ON_COMPILATION keys
        on ``state.song_not_found`` to decide whether to run.
        """
        item = _item(id=1, artist="Cat Power", title="Moon Pix")
        o = Outcome.artist_fallback([item])
        assert o.items == [item]
        assert o.song_not_found_after is True

    def test_artist_fallback_with_empty_items_signals_flag_only(self):
        """``Outcome.artist_fallback([])`` carries the flag without items.

        ARTIST_PLUS_ALBUM can return `([], True)` when the artist+song search
        falls through to artist-only and the artist isn't in the library at
        all — flag must propagate even though no items were found.
        """
        o = Outcome.artist_fallback([])
        assert o.items == []
        assert o.song_not_found_after is True

    def test_compilation_sets_titles_and_stash_signal(self):
        """``Outcome.compilation(items, titles)`` carries TRACK_ON_COMPILATION's full signal.

        Three writes in one outcome:
          - the compilation library items themselves
          - the Discogs titles map (for artwork lookup)
          - `preserve_prior_results_as_fallback=True` (stash any prior artist
            fallback before replacing — `_apply` honors the condition)
          - `found_on_compilation_after=True`
          - `song_not_found_after=False` (we DID match the song, just on a compilation)
        """
        item = _item(id=46602, artist="Various", title="Trax Records 20th Anniversary")
        titles = {46602: "Trax Records 20th Anniversary"}
        o = Outcome.compilation([item], discogs_titles=titles)
        assert o.items == [item]
        assert o.song_not_found_after is False
        assert o.found_on_compilation_after is True
        assert o.preserve_prior_results_as_fallback is True
        assert o.discogs_titles == titles

    def test_track_match_carries_matched_via_hints(self):
        """``Outcome.track_match(items, matched_via_by_id)`` is SONG_AS_TRACK's shape.

        Track-driven match: the song was found by cross-referencing the song
        against Discogs's tracklist index. Each library row carries a
        TrackMatchHint mapping it back to the release that surfaced it.
        """
        item = _item(id=60359, artist="Autechre", title="Confield")
        hint = TrackMatchHint(
            title="vi scose poise",
            artist_credit=None,
            position="3",
            confidence=0.92,
            source=TrackMatchSource.discogs_release,
        )
        matched_via = {60359: [hint]}
        o = Outcome.track_match([item], matched_via_by_id=matched_via)
        assert o.items == [item]
        assert o.song_not_found_after is False
        assert o.matched_via_by_id == matched_via


class TestOutcomeIsImmutable:
    """``Outcome`` is a frozen dataclass — attempts to mutate must raise."""

    def test_cannot_reassign_items(self):
        o = Outcome.empty()
        with pytest.raises((AttributeError, TypeError)):
            o.items = [1]  # type: ignore[misc]

    def test_cannot_reassign_flag(self):
        o = Outcome.found([_item()])
        with pytest.raises((AttributeError, TypeError)):
            o.song_not_found_after = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _apply -- the single runner write site
# ---------------------------------------------------------------------------


class TestApplyEmpty:
    """``Outcome.empty()`` produces no writes."""

    def test_empty_is_a_noop(self):
        state = SearchState(results=[], song_not_found=False)
        _apply(state, Outcome.empty())
        assert state.results == []
        assert state.song_not_found is False
        assert state.found_on_compilation is False
        assert state.discogs_titles == {}
        assert state.matched_via_by_id == {}

    def test_empty_does_not_touch_prior_results(self):
        """An empty outcome leaves a prior strategy's results intact."""
        prior = _item(id=1, artist="Stereolab", title="Dots and Loops")
        state = SearchState(results=[prior], song_not_found=True)
        _apply(state, Outcome.empty())
        assert state.results == [prior]
        assert state.song_not_found is True


class TestApplyFound:
    """``Outcome.found(items)`` writes results and clears song_not_found."""

    def test_writes_results_and_clears_flag(self):
        item = _item(id=1, artist="Jessica Pratt", title="On Your Own Love Again")
        state = SearchState(results=[], song_not_found=True)
        _apply(state, Outcome.found([item]))
        assert state.results == [item]
        assert state.song_not_found is False

    def test_replaces_prior_results(self):
        """A later ``found`` overwrites earlier (e.g. fallback) results."""
        old = _item(id=1, artist="Stereolab", title="Mars Audiac Quintet")
        new = _item(id=2, artist="Stereolab", title="Dots and Loops")
        state = SearchState(results=[old], song_not_found=True)
        _apply(state, Outcome.found([new]))
        assert state.results == [new]
        assert state.song_not_found is False


class TestApplyAlbumMatch:
    """``Outcome.album_match(items)`` writes results without touching the flag."""

    def test_preserves_prior_song_not_found_true(self):
        """Items written; song_not_found stays True from album_resolution."""
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")
        state = SearchState(results=[], song_not_found=True)
        _apply(state, Outcome.album_match([item]))
        assert state.results == [item]
        assert state.song_not_found is True  # preserved

    def test_preserves_prior_song_not_found_false(self):
        item = _item(id=1, artist="Stereolab", title="Dots and Loops")
        state = SearchState(results=[], song_not_found=False)
        _apply(state, Outcome.album_match([item]))
        assert state.results == [item]
        assert state.song_not_found is False  # also preserved


class TestApplyArtistFallback:
    """``Outcome.artist_fallback(items)`` writes results AND sets song_not_found=True."""

    def test_with_items(self):
        item = _item(id=1, artist="Cat Power", title="Moon Pix")
        state = SearchState(results=[], song_not_found=False)
        _apply(state, Outcome.artist_fallback([item]))
        assert state.results == [item]
        assert state.song_not_found is True

    def test_empty_still_sets_flag(self):
        """ARTIST_PLUS_ALBUM's ``([], True)`` case: flag flips, results unchanged.

        Production scenario: artist+song search falls through to artist-only,
        artist isn't in the library at all. The flag must still propagate
        because TRACK_ON_COMPILATION downstream keys on ``song_not_found``.
        """
        state = SearchState(results=[], song_not_found=False)
        _apply(state, Outcome.artist_fallback([]))
        assert state.results == []
        assert state.song_not_found is True


class TestApplyCompilation:
    """``Outcome.compilation(items, titles)`` is TRACK_ON_COMPILATION's full signal."""

    def test_writes_results_titles_flag(self):
        item = _item(id=46602, artist="Various", title="Trax Comp")
        titles = {46602: "Trax Records 20th"}
        state = SearchState(results=[], song_not_found=True)
        _apply(state, Outcome.compilation([item], discogs_titles=titles))
        assert state.results == [item]
        assert state.found_on_compilation is True
        assert state.song_not_found is False
        assert state.discogs_titles == titles

    def test_stashes_prior_artist_fallback_results(self):
        """When prior results exist and song_not_found=True, stash them first.

        Mirrors ``run_track_on_compilation``'s pre-#399 behavior: the artist
        fallback results are preserved so ``perform_lookup`` can validate
        them against Discogs tracklists and merge any confirmed matches
        back into the final results.
        """
        artist_fallback = _item(id=1, artist="Adonis", title="No Way Back")
        compilation = _item(id=46602, artist="Various", title="Trax Comp")
        state = SearchState(results=[artist_fallback], song_not_found=True)
        _apply(
            state,
            Outcome.compilation([compilation], discogs_titles={46602: "Trax Comp"}),
        )
        assert state.results == [compilation]
        assert state.artist_fallback_results == [artist_fallback]
        assert state.found_on_compilation is True
        # song_not_found is cleared on the compilation hit.
        assert state.song_not_found is False

    def test_stash_skips_when_prior_results_empty(self):
        """No prior results to stash → ``artist_fallback_results`` stays empty."""
        compilation = _item(id=46602, artist="Various", title="Trax Comp")
        state = SearchState(results=[], song_not_found=True)
        _apply(
            state,
            Outcome.compilation([compilation], discogs_titles={46602: "Trax Comp"}),
        )
        assert state.artist_fallback_results == []
        assert state.results == [compilation]

    def test_stash_skips_when_prior_song_not_found_false(self):
        """Prior results without the song_not_found flag don't qualify as fallback."""
        prior = _item(id=1, artist="Adonis", title="No Way Back")
        compilation = _item(id=46602, artist="Various", title="Trax Comp")
        state = SearchState(results=[prior], song_not_found=False)
        _apply(
            state,
            Outcome.compilation([compilation], discogs_titles={46602: "Trax Comp"}),
        )
        # Direct hit prior + compilation hit now: nothing to stash as "fallback".
        assert state.artist_fallback_results == []


class TestApplyTrackMatch:
    """``Outcome.track_match(items, matched_via_by_id)`` writes results + hints."""

    def test_writes_results_and_hints(self):
        item = _item(id=60359, artist="Autechre", title="Confield")
        hint = TrackMatchHint(
            title="vi scose poise",
            artist_credit=None,
            position="3",
            confidence=0.92,
            source=TrackMatchSource.discogs_release,
        )
        state = SearchState(results=[], song_not_found=True)
        _apply(state, Outcome.track_match([item], matched_via_by_id={60359: [hint]}))
        assert state.results == [item]
        assert state.matched_via_by_id == {60359: [hint]}
        assert state.song_not_found is False


class TestApplyOrderingInvariant:
    """The stash condition must read prior ``state.song_not_found`` before any update.

    Regression guard: a naive ``_apply`` that writes ``song_not_found_after``
    before checking the stash predicate would never stash, because the flag
    is set to False on every compilation hit. Pin the read-before-write order.
    """

    def test_compilation_stash_reads_prior_song_not_found(self):
        """Prior song_not_found=True must drive the stash even though the
        compilation outcome clears it to False."""
        artist_fallback = _item(id=1, artist="Adonis", title="No Way Back")
        compilation = _item(id=46602, artist="Various", title="Trax Comp")
        state = SearchState(results=[artist_fallback], song_not_found=True)
        _apply(state, Outcome.compilation([compilation], discogs_titles={}))
        # The stash fired on the OLD song_not_found, then song_not_found flipped.
        assert state.artist_fallback_results == [artist_fallback]
        assert state.song_not_found is False
