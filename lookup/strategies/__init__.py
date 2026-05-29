"""Search strategies for ``execute_search_pipeline``.

Each module in this package defines one ``@dataclass(frozen=True)`` strategy
class that satisfies the :class:`core.search.Strategy` Protocol. The classes
own three things:

    - ``name`` (``ClassVar[SearchStrategyType]``) — telemetry identifier
    - ``should_attempt(parsed, state, raw_message) -> bool`` — gating predicate
    - ``attempt(parsed, state, raw_message) -> Outcome`` — async body

Strategies never mutate :class:`core.search.SearchState` directly. They return
an :class:`core.search.Outcome` and the runner applies it via
:func:`core.search._apply`. This makes cancellation safety structural: a
cancelled ``attempt()`` never returns an Outcome, so no state commit happens.

The execute funcs that do the actual library / Discogs work live in
``lookup.orchestrator`` and are injected into each strategy class at
construction time, so this package has no dependency on the orchestrator
module (avoids the obvious circular import). Tests construct strategies with
``AsyncMock`` execute funcs to drive the pipeline through the public seam.

Production construction lives in :func:`lookup.orchestrator.perform_lookup`;
the :func:`build_strategies` factory below is the seam tests use.
"""

from core.search import Strategy
from library.db import LibraryDB

# Execute-func type aliases live next to the strategy class that consumes
# them (one file per strategy). Re-exported here so the factory's parameters
# are documented and ``orchestrator.py`` can annotate its call site without
# importing each strategy module individually.
from .artist_plus_album import ArtistPlusAlbum, ArtistPlusAlbumExecute
from .song_as_artist import SongAsArtist, SongAsArtistExecute
from .song_as_track import SongAsTrack, SongAsTrackExecute
from .swapped_interpretation import SwappedInterpretation, SwappedInterpretationExecute
from .track_on_compilation import TrackOnCompilation, TrackOnCompilationExecute


def build_strategies(
    db: LibraryDB,
    *,
    search_library_func: ArtistPlusAlbumExecute,
    search_alternative_func: SwappedInterpretationExecute,
    search_compilations_func: TrackOnCompilationExecute,
    search_song_as_artist_func: SongAsArtistExecute | None = None,
    search_song_as_track_func: SongAsTrackExecute | None = None,
) -> list[Strategy]:
    """Construct the ordered list of strategies for ``execute_search_pipeline``.

    Each strategy class wraps the corresponding execute func + the shared
    ``db`` handle, and adapts the func's tuple return into a uniform
    :class:`core.search.Outcome`. The two ``_song_as_*`` parameters are
    optional so callers that don't need those strategies (e.g. legacy
    tests) can omit them.

    Args:
        db: Library database handle. Held by each strategy and passed to its
            execute func at call time.
        search_library_func: Implements ARTIST_PLUS_ALBUM
            (``search_library_with_fallback`` in production).
        search_alternative_func: Implements SWAPPED_INTERPRETATION
            (``search_with_alternative_interpretation`` in production).
        search_compilations_func: Implements TRACK_ON_COMPILATION
            (``functools.partial(search_compilations_for_track,
            discogs_service=...)`` in production).
        search_song_as_artist_func: Optional. Implements SONG_AS_ARTIST.
        search_song_as_track_func: Optional. Implements SONG_AS_TRACK.

    Returns:
        Strategies in execution order. SONG_AS_TRACK is appended **after**
        SONG_AS_ARTIST so its condition (``SONG_AS_ARTIST in
        strategies_tried``) holds.
    """
    strategies: list[Strategy] = [
        ArtistPlusAlbum(db=db, execute=search_library_func),
        SwappedInterpretation(db=db, execute=search_alternative_func),
        TrackOnCompilation(db=db, execute=search_compilations_func),
    ]

    if search_song_as_artist_func is not None:
        strategies.append(SongAsArtist(db=db, execute=search_song_as_artist_func))

    # SONG_AS_TRACK must run AFTER SONG_AS_ARTIST — its condition keys on
    # ``SearchStrategyType.SONG_AS_ARTIST in state.strategies_tried``. Array
    # position enforces ordering; the condition does the runtime cross-check.
    if search_song_as_track_func is not None:
        strategies.append(SongAsTrack(db=db, execute=search_song_as_track_func))

    return strategies


__all__ = [
    "ArtistPlusAlbum",
    "ArtistPlusAlbumExecute",
    "SongAsArtist",
    "SongAsArtistExecute",
    "SongAsTrack",
    "SongAsTrackExecute",
    "SwappedInterpretation",
    "SwappedInterpretationExecute",
    "TrackOnCompilation",
    "TrackOnCompilationExecute",
    "build_strategies",
]
