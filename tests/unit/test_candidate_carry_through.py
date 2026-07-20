"""Unit tests for the LML#867 request-scoped candidate carry-through.

A single artist+song lookup used to issue the *same* artist-filtered Discogs
track search twice:

- Step 2 (``resolve_albums_for_track`` -> ``lookup_releases_by_track`` ->
  ``search_releases_by_track(track, artist)``) — the ``artist=`` field filter.
- Step 3 (``TRACK_ON_COMPILATION`` -> ``search_compilations_for_track``) Wave A —
  ``search_releases_by_track(song_search, artist_for_probes)``, the *same*
  ``artist=`` filter.

#867 threads step 2's candidate set (releases + per-candidate validation
verdicts, keyed by ``(track, artist)``) through the request in a
:class:`~lookup.candidate_memo.TrackCandidateMemo` so the strategy reuses it as
Wave A's seed instead of re-searching. The wider Wave B probe
(``artist_as_keyword=True`` -> ``q=`` keyword + ``format=Compilation``) is the
recall-critical arm and MUST stay available — it is only skipped when Wave A's
seed already holds a true-V/A compilation (exactly the case
``merge_wave_b_compilations`` discards it), so the skip is provably
non-narrowing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from discogs.lookup import lookup_releases_by_track
from discogs.models import ReleaseInfo, TrackReleasesResponse
from lookup.candidate_memo import TrackCandidateMemo
from lookup.matching import WAVE_A_SEARCH_LIMIT
from lookup.models import LookupRequest
from lookup.orchestrator import perform_lookup
from lookup.strategies.track_on_compilation import search_compilations_for_track
from services.parser import ParsedRequest
from tests.conftest import make_lml_telemetry
from tests.factories import make_library_item

SONG = "Sycamore Trees"
ARTIST = "Jimmy Scott"


def _release(
    *, album: str, artist: str, release_id: int, is_compilation: bool = False
) -> ReleaseInfo:
    return ReleaseInfo(
        album=album,
        artist=artist,
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        is_compilation=is_compilation,
    )


def _counting_service(
    *,
    wave_a: list[ReleaseInfo],
    wave_b: list[ReleaseInfo],
) -> AsyncMock:
    """A DiscogsService stub whose ``search_releases_by_track`` records every call.

    Returns ``wave_a`` for the ``artist=`` field probe (``artist_as_keyword``
    False) and ``wave_b`` for the ``q=``/``format=Compilation`` probe
    (``artist_as_keyword`` True). Every candidate validates True.
    """
    service = AsyncMock()
    service.cache_service = None

    async def _track_releases(track, artist=None, limit=20, artist_as_keyword=False, **_):
        releases = wave_b if artist_as_keyword else wave_a
        return TrackReleasesResponse(
            track=track,
            artist=artist,
            releases=list(releases),
            total=len(releases),
        )

    service.search_releases_by_track = AsyncMock(side_effect=_track_releases)
    service.validate_track_on_release = AsyncMock(return_value=True)
    return service


def _artist_filtered_calls(service: AsyncMock) -> int:
    """Count ``search_releases_by_track`` calls made with the ``artist=`` filter
    (``artist_as_keyword`` False) — the shape step 2 and step-3 Wave A duplicate."""
    n = 0
    for call in service.search_releases_by_track.call_args_list:
        if not call.kwargs.get("artist_as_keyword", False):
            n += 1
    return n


def _keyword_calls(service: AsyncMock) -> int:
    """Count the wider ``q=``/``format=Compilation`` probes (``artist_as_keyword`` True)."""
    return sum(
        1
        for call in service.search_releases_by_track.call_args_list
        if call.kwargs.get("artist_as_keyword", False)
    )


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.exact_title = AsyncMock(return_value=[])

    async def _search(query, limit=None, **_):
        # The producer's library-membership gate queries the artist; return a
        # library row so validation runs (and the artist counts as a library
        # artist). Keyword queries in the consumer return nothing — the count
        # assertions don't depend on a surfaced library row.
        if ARTIST.lower() in (query or "").lower():
            return [make_library_item(artist=ARTIST, title="Falling in Love Is Wonderful")]
        return []

    db.search = AsyncMock(side_effect=_search)
    return db


async def _run_lookup(memo: TrackCandidateMemo | None) -> AsyncMock:
    """Drive step 2 then step-3 TRACK_ON_COMPILATION over one shared service."""
    service = _counting_service(
        wave_a=[
            _release(album="Falling in Love Is Wonderful", artist=ARTIST, release_id=101),
            _release(album="The Source", artist=ARTIST, release_id=102),
        ],
        wave_b=[],
    )
    db = _make_db()
    parsed = ParsedRequest(
        artist=ARTIST,
        song=SONG,
        raw_message=f"{SONG} by {ARTIST}",
    )

    # Step 2 (producer): populates the memo for (SONG, ARTIST). Searches at the
    # shared Wave A page size (as prod's step 2 does) so the seed qualifies for reuse.
    await lookup_releases_by_track(
        SONG,
        ARTIST,
        limit=WAVE_A_SEARCH_LIMIT,
        service=service,
        db=db,
        library_artist=ARTIST,
        memo=memo,
    )
    # Step 3 (consumer): TRACK_ON_COMPILATION reuses the memo as Wave A's seed.
    await search_compilations_for_track(db, parsed, discogs_service=service, candidate_memo=memo)
    return service


class TestCandidateCarryThrough:
    @pytest.mark.asyncio
    async def test_at_most_one_artist_filtered_search_with_memo(self):
        """AC#1/AC#4: with the memo, the artist+song lookup issues the ``artist=``
        track search exactly once (step 2's), and the count drops by >=1 vs. the
        no-memo control."""
        with_memo = await _run_lookup(TrackCandidateMemo())
        without_memo = await _run_lookup(None)

        with_count = _artist_filtered_calls(with_memo)
        without_count = _artist_filtered_calls(without_memo)

        assert with_count == 1, (
            f"artist+song lookup must issue at most one artist-filtered track "
            f"search with the memo, got {with_count}"
        )
        assert with_count <= without_count - 1, (
            f"the duplicated artist-filtered search must drop by >=1 (api_calls "
            f"reduction): with_memo={with_count}, without_memo={without_count}"
        )

    @pytest.mark.asyncio
    async def test_memo_carries_candidates_and_verdicts(self):
        """AC#1: step-2 candidates (with verdicts) are visible in the memo."""
        memo = TrackCandidateMemo()
        service = _counting_service(
            wave_a=[_release(album="The Source", artist=ARTIST, release_id=102)],
            wave_b=[],
        )
        db = _make_db()
        await lookup_releases_by_track(
            SONG,
            ARTIST,
            limit=WAVE_A_SEARCH_LIMIT,
            service=service,
            db=db,
            library_artist=ARTIST,
            memo=memo,
        )

        entry = memo.get(SONG, ARTIST)
        assert entry is not None
        assert [r.release_id for r in entry.releases] == [102]
        assert entry.verdicts.get(102) is True
        assert entry.validated is True
        # The seed records the page it was fetched at, so the consumer can gate
        # reuse on it being at least as wide as a fresh Wave A.
        assert entry.search_limit == WAVE_A_SEARCH_LIMIT


class TestWiderSearchStillFires:
    @pytest.mark.asyncio
    async def test_non_va_seed_still_fires_wider_search(self):
        """AC#2: a memo seed with no true-V/A comp is 'insufficient' for the
        compilation purpose, so the wider ``q=``/``format=Compilation`` probe
        STILL fires (no recall narrowing)."""
        service = await _run_lookup(TrackCandidateMemo())
        assert _keyword_calls(service) == 1, (
            "the wider keyword/format=Compilation probe must still fire when the "
            "reused seed holds no V/A compilation"
        )

    @pytest.mark.asyncio
    async def test_va_seed_skips_redundant_wider_search(self):
        """AC#2 trigger condition: when the reused seed already holds a true-V/A
        compilation, the wider probe is skipped — ``merge_wave_b_compilations``
        discards Wave B in exactly that case, so the skip cannot narrow recall."""
        memo = TrackCandidateMemo()
        # Seed Wave A with a true-V/A compilation (Various-credited + is_compilation).
        service = _counting_service(
            wave_a=[
                _release(
                    album="Twin Peaks (Soundtrack)",
                    artist="Various",
                    release_id=201,
                    is_compilation=True,
                ),
            ],
            wave_b=[
                _release(
                    album="Some Other Comp",
                    artist="Various",
                    release_id=202,
                    is_compilation=True,
                ),
            ],
        )
        db = _make_db()
        parsed = ParsedRequest(artist=ARTIST, song=SONG, raw_message=f"{SONG} by {ARTIST}")

        # Populate the memo directly with the V/A seed (as step 2 would carry it),
        # at the shared Wave A page size so it qualifies for reuse.
        memo.record(
            SONG,
            ARTIST,
            [
                _release(
                    album="Twin Peaks (Soundtrack)",
                    artist="Various",
                    release_id=201,
                    is_compilation=True,
                )
            ],
            verdicts={201: True},
            validated=True,
            search_limit=WAVE_A_SEARCH_LIMIT,
        )

        await search_compilations_for_track(
            db, parsed, discogs_service=service, candidate_memo=memo
        )

        assert _artist_filtered_calls(service) == 0, "Wave A must be reused from the memo"
        assert _keyword_calls(service) == 0, (
            "the wider probe must be skipped when the seed already holds a V/A comp "
            "(merge_wave_b_compilations would discard it)"
        )

    @pytest.mark.asyncio
    async def test_empty_seed_fires_wider_search_and_surfaces_va_only_hit(self):
        """No recall narrowing: an empty/insufficient reused seed still fires the
        wider probe, which surfaces a V/A-only compilation hit."""
        va_item = make_library_item(id=58610, artist="Various Artists", title="Disco Not Disco")
        memo = TrackCandidateMemo()
        # Empty but complete seed at the shared Wave A page size: reusable (an empty
        # Wave A), and being non-V/A it still fires the wider probe.
        memo.record(SONG, ARTIST, [], validated=True, search_limit=WAVE_A_SEARCH_LIMIT)

        service = _counting_service(
            wave_a=[],
            wave_b=[
                _release(
                    album="Disco Not Disco", artist="Various", release_id=301, is_compilation=True
                )
            ],
        )
        db = AsyncMock()
        db.exact_title = AsyncMock(return_value=[])

        async def _search(query, limit=None, **_):
            if "disco not disco" in (query or "").lower():
                return [va_item]
            return []

        db.search = AsyncMock(side_effect=_search)
        parsed = ParsedRequest(artist=ARTIST, song=SONG, raw_message=f"{SONG} by {ARTIST}")

        items, _titles = await search_compilations_for_track(
            db, parsed, discogs_service=service, candidate_memo=memo
        )

        assert _artist_filtered_calls(service) == 0, "empty Wave A seed reused from memo"
        assert _keyword_calls(service) == 1, "wider probe must fire on an insufficient seed"
        assert {i.title for i in items} == {"Disco Not Disco"}


class TestOrchestratorThreadsOneMemo:
    """The orchestrator must create ONE request-scoped memo and thread the same
    instance from step 2's producer to step 3's TRACK_ON_COMPILATION consumer."""

    @pytest.mark.asyncio
    async def test_single_memo_flows_from_step2_to_compilation(
        self, mock_library_db, mock_discogs_service
    ):
        # No artist correction, non-two-part raw_message: only TRACK_ON_COMPILATION
        # fires among the Discogs-aware strategies (SWAPPED needs an ambiguous
        # two-part shape; SONG_AS_* require no artist), so the memo is threaded
        # exactly once from producer to consumer.
        request = LookupRequest(
            artist=ARTIST,
            song=SONG,
            raw_message=f"dj played {SONG.lower()}",
        )
        telemetry = make_lml_telemetry()
        captured: dict[str, object] = {}

        async def _fake_producer(*_args, **kwargs):
            captured["producer_memo"] = kwargs.get("memo")
            return []

        async def _fake_consumer(_db, _parsed, **kwargs):
            captured["consumer_memo"] = kwargs.get("candidate_memo")
            return [], {}

        with (
            patch(
                "lookup.orchestrator.lookup_releases_by_track",
                new=AsyncMock(side_effect=_fake_producer),
            ),
            patch(
                "lookup.orchestrator.search_compilations_for_track",
                new=AsyncMock(side_effect=_fake_consumer),
            ),
        ):
            await perform_lookup(request, mock_library_db, mock_discogs_service, telemetry)

        producer_memo = captured.get("producer_memo")
        consumer_memo = captured.get("consumer_memo")
        assert isinstance(producer_memo, TrackCandidateMemo), (
            "step 2 must receive a request-scoped TrackCandidateMemo"
        )
        assert producer_memo is consumer_memo, (
            "the same memo instance must reach TRACK_ON_COMPILATION (one memo per request)"
        )


class TestNarrowerSeedNoRecallNarrowing:
    """A seed fetched at a page narrower than the consumer's Wave A is not provably
    the complete artist-filtered set — a fresh, wider search can surface releases the
    narrow page missed. Crucially this holds even when the narrow seed came back
    SHORT of its own page: ``search_releases_by_track`` runs a keyword supplement
    (``per_page`` = the same limit) when the ``artist=`` primary finds < 3, and that
    supplement's page scales with the limit, so a wider limit yields strictly more.
    Reusing the narrow seed would drop a library pressing that is NOT a V/A
    compilation (so Wave B's ``format=Compilation`` arm cannot rescue it) — the
    LML#267/#560/#801 rescue this strategy exists for. The memo must suppress the
    redundant search only when reuse is a provable superset."""

    NARROW_LIMIT = 10  # < WAVE_A_SEARCH_LIMIT

    def _breadth_scaling_service(
        self, *, narrow: list[ReleaseInfo], wide: list[ReleaseInfo], wave_b: list[ReleaseInfo]
    ) -> AsyncMock:
        """A service that returns MORE for a wider ``artist=`` page (``wide`` for
        ``limit >= WAVE_A_SEARCH_LIMIT``, else ``narrow``) — modeling the keyword
        supplement whose page scales with the limit — and ``wave_b`` for the
        ``q=``/``format=Compilation`` probe."""
        service = AsyncMock()
        service.cache_service = None

        async def _track_releases(track, artist=None, limit=20, artist_as_keyword=False, **_):
            if artist_as_keyword:
                releases = wave_b
            elif limit >= WAVE_A_SEARCH_LIMIT:
                releases = wide
            else:
                releases = narrow
            return TrackReleasesResponse(
                track=track, artist=artist, releases=list(releases), total=len(releases)
            )

        service.search_releases_by_track = AsyncMock(side_effect=_track_releases)
        service.validate_track_on_release = AsyncMock(return_value=True)
        return service

    @pytest.mark.asyncio
    async def test_narrower_seed_falls_back_to_fresh_wider_wave_a(self):
        """No recall narrowing: a seed fetched at a narrower page than the consumer's
        Wave A must NOT be reused — even when it is short of its own page — so the
        consumer re-issues a fresh wider Wave A that surfaces the library pressing the
        narrow page missed."""
        # The narrow (limit=10) artist search comes back SHORT — 6 same-artist
        # releases, none library-matching — so a len>=limit "truncated" heuristic
        # would (wrongly) call it complete. A wider (limit=20) search returns those 6
        # plus the sole library pressing: a plain reissue (not a compilation), so
        # Wave B cannot rescue it.
        narrow = [
            _release(album=f"Bootleg Vol. {i}", artist=ARTIST, release_id=i) for i in range(1, 7)
        ]
        target = _release(album="The Rare Reissue", artist=ARTIST, release_id=999)
        service = self._breadth_scaling_service(narrow=narrow, wide=narrow + [target], wave_b=[])

        target_item = make_library_item(id=999, artist=ARTIST, title="The Rare Reissue")
        db = AsyncMock()

        async def _exact_title(title, **_):
            return [target_item] if (title or "").lower() == "the rare reissue" else []

        async def _search(query, limit=None, **_):
            # Library-membership gate (producer) + keyword leg hit on the artist.
            if ARTIST.lower() in (query or "").lower():
                return [target_item]
            return []

        db.exact_title = AsyncMock(side_effect=_exact_title)
        db.search = AsyncMock(side_effect=_search)

        memo = TrackCandidateMemo()
        parsed = ParsedRequest(artist=ARTIST, song=SONG, raw_message=f"{SONG} by {ARTIST}")

        # Step 2 (producer) at the narrow page: seed is short of its own cap.
        await lookup_releases_by_track(
            SONG,
            ARTIST,
            limit=self.NARROW_LIMIT,
            service=service,
            db=db,
            library_artist=ARTIST,
            memo=memo,
        )
        seed = memo.get(SONG, ARTIST)
        assert seed is not None
        assert seed.search_limit == self.NARROW_LIMIT
        assert len(seed.releases) < self.NARROW_LIMIT, (
            "precondition: seed is SHORT of its page — a len>=limit heuristic would "
            "wrongly treat it as complete"
        )

        # Step 3 (consumer): must re-issue a fresh wider Wave A and surface the
        # library pressing the narrow page missed.
        items, _titles = await search_compilations_for_track(
            db, parsed, discogs_service=service, candidate_memo=memo
        )
        assert {i.title for i in items} == {"The Rare Reissue"}, (
            "a narrower-page seed must not suppress the fresh wider Wave A that "
            "surfaces the library album beyond the narrow page"
        )
