"""Reproduce-or-refute for the #818 (C3) "masked API leg" finding.

Surfaced by the #802 artist-prefilter review (#807). The claim:
``discogs/fallthrough.py`` treats *any* non-empty ``search_releases_by_track``
PG read as a terminal hit and returns it, never consulting the API leg. A
#802-surfaced-but-incomplete PG set — the artist's *other* releases while the
``rta.extra = 0`` guard pruned the actual track-bearing comp (the C2/#817
shape) — would then terminate the lookup at the PG arm and mask the API leg
that would have returned the correct/complete set.

The seam mechanism is real and by design: a non-empty PG hit IS terminal (it
is the whole point of the L2 tier — no Discogs quota on a cache hit, per the
#818 "do not add an API call to the happy PG-hit path" constraint). What these
tests establish is that the mechanism is **harmless at every real consumer**.
The two *two-wave* ``search_releases_by_track`` consumers (``track_on_compilation``
and the row-less ``resolve_release_for_track``) probe in TWO independent waves —

* **Wave A** (``artist_as_keyword=False``): the artist-field probe, PG-read
  enabled. This is the only wave the C3 masking can touch.
* **Wave B** (``artist_as_keyword=True``): the keyword + ``format=Compilation``
  probe. Its ``pg_read_hook`` is ``None`` (``discogs/service.py`` disables the
  PG read for the keyword shape), so Wave B *always* reaches the API leg,
  independent of Wave A's PG state.

The two waves are complementary by construction. The only releases Wave A's PG
arm can miss while still returning non-empty are exactly the track-level /
compilation credits the ``rta.extra = 0`` guard prunes — and those are exactly
what Wave B's keyword/``Compilation`` API probe is built to surface. So a
C2-pruned comp that Wave A masks falls squarely into Wave B's domain and is
recovered by the merge (``merge_wave_b_compilations``).

The single-wave lookup-strategy consumer ``_match_track_releases_to_library``
(SONG_AS_TRACK / SWAPPED) has no Wave B, but the masking still can't harm it:
SONG_AS_TRACK passes ``artist=None`` (so ``_SEARCH_BY_TRACK_SQL`` never
artist-prunes — the comp is already in its Wave-A read) and SWAPPED re-filters to
the queried artist, dropping V/A comps. The two remaining direct seam callers
(``lookup_releases_by_track``'s album path, artist-filtered; and the
``get_track_releases`` diagnostic endpoint, off the ``/lookup`` correctness path)
are covered by the same reasoning. So these paths are safe by construction, not
by a recovery wave.

Verdict: **REFUTED** — no seam change; these tests pin the recovery so a future
wave-swap or PG-hit-predicate change can't silently reopen the hole.

Spine case (mirrors ``tests/unit/test_release_resolution.py``): A Guy Called
Gerald — "Message to Black Youth" on the Various-Artists compilation "When
There Is No Sun" (discogs release 36907527). The masking bait is one of the
artist's own solo releases surfacing from the PG arm while the comp is
reachable only via the API.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from discogs.models import ReleaseInfo
from discogs.service import DiscogsService

_TRACK = "Message to Black Youth"
_ARTIST = "A Guy Called Gerald"
_COMP_ALBUM = "When There Is No Sun"
_COMP_ID = 36907527
_SOLO_ALBUM = "Black Secret Technology"
_SOLO_ID = 1234


def _solo_release() -> ReleaseInfo:
    """The artist's own (non-V/A) release — the incomplete-PG-arm bait.

    This is the row the ``search_releases_by_track`` PG arm returns when the
    track-bearing comp was pruned by the ``rta.extra = 0`` guard: a real,
    non-empty hit that is nonetheless *not* the release the caller wants.
    """
    return ReleaseInfo(
        album=_SOLO_ALBUM,
        artist=_ARTIST,
        release_id=_SOLO_ID,
        release_url=f"https://www.discogs.com/release/{_SOLO_ID}",
        is_compilation=False,
    )


def _comp_api_response() -> MagicMock:
    """A Discogs search response whose sole result is the V/A comp.

    ``_process_search_result`` parses ``"Various - When There Is No Sun"`` and
    stamps ``is_compilation=True`` via ``is_compilation_artist("Various")``.
    """
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"results": [{"title": f"Various - {_COMP_ALBUM}", "id": _COMP_ID}]}
    return resp


def _empty_api_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"results": []}
    return resp


def _make_service_with_masking_pg() -> tuple[DiscogsService, list[dict]]:
    """Real ``DiscogsService`` wired so Wave A's PG arm masks its own API leg.

    Returns the service and the list into which the patched ``_request_with_retry``
    records every call's params, so tests can assert which legs actually fired.

    The PG arm (``cache.search_releases_by_track``) returns the solo release: a
    non-empty-but-incomplete set that trips the seam's terminal-PG-hit path for
    Wave A. The API is routed by params: a Wave-A artist-field call returns
    empty (and is recorded so a test can prove it was masked / never fired),
    while a Wave-B keyword call returns the comp.
    """
    fake_cache = AsyncMock()
    fake_cache.search_releases_by_track = AsyncMock(return_value=[_solo_release()])
    # Wave B consults the negative cache before its API leg; keep it a miss so
    # the API is reached, and record-on-empty is irrelevant (API is non-empty).
    fake_cache.lookup_negative_hit = AsyncMock(return_value=False)
    fake_cache.record_lookup_negative = AsyncMock()

    svc = DiscogsService(token="test-token", cache_service=fake_cache)

    api_calls: list[dict] = []

    async def _fake_request(method, url, params=None, **_kwargs):
        params = params or {}
        api_calls.append(dict(params))
        # Wave A's API leg uses the ``artist`` FIELD filter. If it ever fires,
        # the masking was defeated — return empty so the test can catch it by
        # inspecting ``api_calls`` rather than by a surfaced solo row.
        if "artist" in params:
            return _empty_api_response()
        # Wave B's keyword/``Compilation`` leg (and its supplementary keyword
        # search) use ``q``. The primary keyword+Compilation call surfaces the
        # comp; the supplement (no ``format``) can return empty — the comp is
        # already found and album-deduped.
        if params.get("format") == "Compilation":
            return _comp_api_response()
        return _empty_api_response()

    svc._request_with_retry = AsyncMock(side_effect=_fake_request)  # type: ignore[method-assign]
    return svc, api_calls


# ---------------------------------------------------------------------------
# 1. The seam mechanism (C3): a non-empty PG hit is terminal — API leg masked.
# ---------------------------------------------------------------------------


class TestSeamTerminalPgHit:
    @pytest.mark.asyncio
    async def test_nonempty_incomplete_pg_read_terminates_before_api_leg(self):
        """Wave A: a non-empty-but-incomplete PG read returns terminally and the
        API leg is never consulted. This is the C3 mechanism — and the L2-tier
        contract (#818: no Discogs quota on a happy PG hit). Pinned so the
        happy-path-never-calls-API invariant can't regress."""
        svc, api_calls = _make_service_with_masking_pg()

        result = await svc.search_releases_by_track(_TRACK, _ARTIST)

        # PG arm was consulted and its (incomplete) set came straight back.
        svc.cache_service.search_releases_by_track.assert_awaited_once()
        assert result.cached is True
        assert [r.release_id for r in result.releases] == [_SOLO_ID]
        # The whole point: the API leg was masked — zero Discogs calls.
        assert api_calls == []

    @pytest.mark.asyncio
    async def test_wave_b_keyword_leg_reaches_api_despite_pg_arm(self):
        """Wave B (``artist_as_keyword=True``) disables the PG read entirely
        (``pg_read_hook=None``), so it reaches the API leg regardless of what
        the PG arm would have said — the structural recovery path for C3."""
        svc, api_calls = _make_service_with_masking_pg()

        result = await svc.search_releases_by_track(_TRACK, _ARTIST, artist_as_keyword=True)

        # PG read was NOT consulted for the keyword shape.
        svc.cache_service.search_releases_by_track.assert_not_awaited()
        # The API leg surfaced the comp.
        assert result.cached is False
        assert [r.release_id for r in result.releases] == [_COMP_ID]
        assert any(c.get("format") == "Compilation" for c in api_calls)

    @pytest.mark.asyncio
    async def test_pruned_empty_pg_read_reaches_api_and_pins_no_negative(self):
        """C3 secondary shape: an *empty* (PG-pruned) read is not terminal — the
        seam falls through to the API leg, and because the API returns a result
        (not a confirmed absence) NO durable negative is pinned. So a PG prune
        can never, on its own, suppress a later API-findable result: the
        negative-cache write is gated on the *API* verdict, not the PG one."""
        fake_cache = AsyncMock()
        # PG-pruned empty: the ``rta.extra = 0`` guard dropped the only matching
        # credit, so the PG arm returns nothing.
        fake_cache.search_releases_by_track = AsyncMock(return_value=[])
        fake_cache.lookup_negative_hit = AsyncMock(return_value=False)
        fake_cache.record_lookup_negative = AsyncMock()
        svc = DiscogsService(token="test-token", cache_service=fake_cache)
        svc._request_with_retry = AsyncMock(return_value=_comp_api_response())  # type: ignore[method-assign]

        result = await svc.search_releases_by_track(_TRACK, _ARTIST)

        # The empty PG read fell through to the API, which surfaced the comp.
        assert [r.release_id for r in result.releases] == [_COMP_ID]
        assert result.cached is False
        # No durable negative was pinned — the API said "found", not "absent".
        fake_cache.record_lookup_negative.assert_not_called()

    @pytest.mark.asyncio
    async def test_pruned_empty_pg_read_with_unreachable_api_pins_no_negative(self):
        """C3 secondary shape, the harder vector: an empty (PG-pruned) read falls
        through to the API leg, but the API is UNREACHABLE — ``_request_with_retry``
        returns ``None`` (the httpx-error / retry-exhaustion / breaker-shed
        "couldn't ask" laundering, service.py FIX 1). ``_api_fetch`` sets
        ``any_call_absent`` and returns ``None`` rather than an empty response, so
        the seam's ``api_result is not None`` gate skips the durable negative-cache
        write. Without this, a transient Discogs outage striking during a PG-pruned
        read would durably suppress a later API-findable comp for the negative TTL.
        This locally pins the vector that otherwise only rides on the #755 suite."""
        fake_cache = AsyncMock()
        # PG-pruned empty: the ``rta.extra = 0`` guard dropped the only matching
        # credit, so the PG arm returns nothing and the seam falls through.
        fake_cache.search_releases_by_track = AsyncMock(return_value=[])
        fake_cache.lookup_negative_hit = AsyncMock(return_value=False)
        fake_cache.record_lookup_negative = AsyncMock()
        svc = DiscogsService(token="test-token", cache_service=fake_cache)
        # "Couldn't ask": every live leg returns ``None`` (NOT an empty 200 body).
        svc._request_with_retry = AsyncMock(return_value=None)  # type: ignore[method-assign]

        result = await svc.search_releases_by_track(_TRACK, _ARTIST)

        # Nothing surfaced, and crucially NO durable negative was pinned: an
        # "unknown" (couldn't ask) must never masquerade as a confirmed absence.
        assert [r.release_id for r in result.releases] == []
        fake_cache.record_lookup_negative.assert_not_called()


# ---------------------------------------------------------------------------
# 2. The refutation: the consumer recovers the masked comp via Wave B.
# ---------------------------------------------------------------------------


class TestConsumerRecoversThroughRealSeam:
    @pytest.mark.asyncio
    async def test_rowless_resolve_recovers_masked_comp_via_wave_b(self):
        """End-to-end through the REAL fallthrough seam for BOTH waves.

        Wave A's PG arm returns the artist's solo release (non-empty, incomplete)
        and the seam terminates before Wave A's API leg — the C3 masking. Yet
        ``resolve_release_for_track`` still returns the V/A comp because Wave B's
        keyword/``Compilation`` API probe (PG-disabled) surfaces it and the merge
        pulls it in. This is the coverage the existing
        ``test_wave_b_keyword_probe_surfaces_va_comp_when_wave_a_has_none`` can't
        provide: that test mocks ``search_releases_by_track`` directly, bypassing
        the seam, so it never exercises the terminal-PG-hit masking at all."""
        from lookup.release_resolution import resolve_release_for_track

        svc, api_calls = _make_service_with_masking_pg()
        # Only the comp validates the per-track credit; the solo bait does not.
        svc.validate_track_on_release = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda rid, song, artist: rid == _COMP_ID
        )

        resolved = await resolve_release_for_track(
            song=_TRACK,
            artist=_ARTIST,
            album=_COMP_ALBUM,
            discogs_service=svc,
        )

        # The consumer recovers the comp despite Wave A's seam masking its API leg.
        assert [r.release_id for r in resolved] == [_COMP_ID]
        # Prove the recovery ran through the masking, not around it: Wave A's PG
        # arm WAS consulted (the terminal hit), and Wave A's artist-field API leg
        # NEVER fired (masked) — the comp came only from Wave B's keyword probe.
        svc.cache_service.search_releases_by_track.assert_awaited_once()
        assert not any("artist" in c for c in api_calls), (
            "Wave A's artist-field API leg fired — the C3 masking did not hold, "
            "so this test would no longer be exercising the masked-leg scenario."
        )
        assert any(c.get("format") == "Compilation" for c in api_calls)
