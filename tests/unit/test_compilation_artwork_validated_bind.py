"""A found-on-compilation row must display the *track-validated* release.

Repro of the prod bug behind ``lookup "i only have eyes for you by the
flamingos"``: the TRACK_ON_COMPILATION strategy found and track-validated a real
release for the library row "Various - Greatest hits of the 50s & 60s" and
carried it on the ``discogs_titles`` seam, but the artwork-binding step
re-derived the Discogs release with a title+artist search whose ``album_variants``
include the library row's own title. For a generic V/A comp title that search
binds a *same-titled* release that does NOT carry the track (Plaza House's
"Greatest Hits Of The 50s & 60s", release 13332759), at confidence 1.0 —
diverging display from what validation actually confirmed.

The carried release is already validated (``validate_release_for_track`` in
``search_compilations_for_track``), so binding it costs no extra Discogs fan-out
and must win over the unvalidated title-search pick. This holds *independent* of
``lml_resolve_compilation_release`` — that flag was OFF at prod runtime, which is
exactly when the divergence surfaced (with it on, the LML#604 trust-bind already
binds the carried release for every row).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from discogs.models import DiscogsSearchResponse, ReleaseMetadataResponse
from lookup.artwork import fetch_artwork_for_items
from lookup.release_resolution import ResolvedRelease
from tests.factories import make_discogs_result, make_library_item

# The library row (a Various-Artists compilation whose title is generic enough
# that Discogs holds a *different* same-titled comp).
_LIBRARY_ID = 58775
_LIBRARY_TITLE = "Greatest hits of the 50s & 60s"

# The release TRACK_ON_COMPILATION validated the track on and carried on the seam.
_VALIDATED_RELEASE_ID = 605487
_VALIDATED_ALBUM_TITLE = "Greatest Hits Of The 50's"

# The wrong, same-titled release the artwork title-search would bind: its album
# is an exact match to the library row's own title, so it clears the 80/80 floor
# via the ``item.title`` album-variant even though it lacks the track.
_WRONG_SEARCH_RELEASE_ID = 13332759


def _svc_binding_validated_release() -> AsyncMock:
    """A Discogs service whose ``get_release`` resolves the *validated* release.

    ``_bind_resolved_release`` fetches artwork for the bound release via
    ``_resolve_fallback_artwork`` -> ``get_release``. ``search`` returns the
    WRONG same-titled release so a test can prove the carried validated release
    wins over the title-search pick.
    """
    svc = AsyncMock()
    svc.cache_service = None
    svc.get_release = AsyncMock(
        return_value=ReleaseMetadataResponse(
            release_id=_VALIDATED_RELEASE_ID,
            title=_VALIDATED_ALBUM_TITLE,
            artist="Various",
            release_url=f"https://www.discogs.com/release/{_VALIDATED_RELEASE_ID}",
            artwork_url="https://i.discogs.com/validated.jpg",
        )
    )
    # The title search, if it ran, would bind this WRONG same-titled release —
    # album matches the library row's own title exactly.
    wrong = make_discogs_result(
        release_id=_WRONG_SEARCH_RELEASE_ID,
        album=_LIBRARY_TITLE,
        artist="Various",
        artwork_url="https://example.com/wrong.jpg",
    )
    svc.search = AsyncMock(return_value=DiscogsSearchResponse(results=[wrong]))
    return svc


def _carried_validated_release() -> ResolvedRelease:
    return ResolvedRelease(
        release_id=_VALIDATED_RELEASE_ID,
        release_url=f"https://www.discogs.com/release/{_VALIDATED_RELEASE_ID}",
        is_compilation=True,
        album_title=_VALIDATED_ALBUM_TITLE,
    )


def _comp_row() -> object:
    return make_library_item(
        id=_LIBRARY_ID,
        artist="Various Artists - Rock - G",
        title=_LIBRARY_TITLE,
        format="vinyl",
    )


@pytest.mark.asyncio
class TestCarriedValidatedReleaseWins:
    async def test_flag_off_binds_carried_release_not_same_title_search(self, monkeypatch):
        # The prod-at-runtime condition: the compilation trust-bind flag is OFF.
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "false")
        from config.settings import get_settings

        get_settings.cache_clear()
        try:
            svc = _svc_binding_validated_release()

            results = await fetch_artwork_for_items(
                [_comp_row()],
                svc,
                {_LIBRARY_ID: _carried_validated_release()},  # discogs_titles seam
                song="I Only Have Eyes for You",
                found_on_compilation=True,
            )

            bound = results[0][1]
            assert bound is not None
            # Displays the track-validated release, not the same-titled title-search pick.
            assert bound.release_id == _VALIDATED_RELEASE_ID
            # Binding an already-carried release skips the title re-search entirely.
            svc.search.assert_not_awaited()
        finally:
            get_settings.cache_clear()

    async def test_no_carried_release_still_falls_through_to_search(self, monkeypatch):
        # Guard: the new bind is scoped to rows that actually carry a validated
        # release. With nothing on the seam, a found-on-compilation row re-searches
        # exactly as before (no regression, no crash).
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "false")
        from config.settings import get_settings

        get_settings.cache_clear()
        try:
            svc = _svc_binding_validated_release()

            results = await fetch_artwork_for_items(
                [_comp_row()],
                svc,
                {},  # no carried release for this id
                song="I Only Have Eyes for You",
                found_on_compilation=True,
            )

            bound = results[0][1]
            assert bound is not None
            assert bound.release_id == _WRONG_SEARCH_RELEASE_ID
            svc.search.assert_awaited()
        finally:
            get_settings.cache_clear()

    async def test_non_compilation_row_unchanged_uses_search(self, monkeypatch):
        # Guard: the new bind is scoped to the compilation path. A row NOT flagged
        # ``found_on_compilation`` re-searches by title exactly as before, even
        # with a release on the seam and the flag off.
        monkeypatch.setenv("LML_RESOLVE_COMPILATION_RELEASE", "false")
        from config.settings import get_settings

        get_settings.cache_clear()
        try:
            svc = _svc_binding_validated_release()

            results = await fetch_artwork_for_items(
                [_comp_row()],
                svc,
                {_LIBRARY_ID: _carried_validated_release()},
                song="I Only Have Eyes for You",
                found_on_compilation=False,
            )

            bound = results[0][1]
            assert bound is not None
            assert bound.release_id == _WRONG_SEARCH_RELEASE_ID
            svc.search.assert_awaited()
        finally:
            get_settings.cache_clear()
