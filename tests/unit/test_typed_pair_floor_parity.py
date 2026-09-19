"""LML#1321 — the ARTIST_PLUS_ALBUM match class has ONE implementation, enforced.

Two callers admit the typed ``(artist, album)`` pair: the LML#583 step-3a
library-miss probe (``lookup/strategies/library_miss.py``) and the LML#1318
album-level degrade (``lookup/album_level_match.py``). The degrade's docstring
claims it "admits exactly the ARTIST_PLUS_ALBUM match class and nothing wider"
and "sees the same candidate set the album-level lookup path would" — claims
that, before this suite, were enforced by a comment next to a verbatim copy of
the other caller's floor call, self-titled swap, and cache-row mapping.

This file is the enforcement. Every test here fails if either caller's
**floor**, **limit**, **self-titled swap**, or **cache-row mapping** forks from
the other:

* :class:`TestFloorVerdictParity` drives both callers over one table of
  candidate rows — including the LML#1206 suffix-widened credit and its
  exact-credit tie-break, the LML#784 self-titled swap, and a floor reject —
  and asserts they pick the same release (or both decline). The library-miss
  side runs the **real** ``DiscogsService.search`` PG arm, so the comparison is
  between two productions rather than between one production and a hand-rolled
  restatement of the other.
* :class:`TestCandidateOrderingIsImmaterial` pins the ordering decision. The PG
  arm confidence-sorts its candidates before flooring them; the degrade floors
  the rows in SQL order. That is deliberate and safe, not an oversight:
  ``find_best_typed_match`` is handed a ``key_fn``
  (``artist_variant_tie_break_key`` -> ``(exact_credit_rank, release_id)``), and
  ``search_releases`` returns ``SELECT DISTINCT ON (r.id)`` rows, so the keys
  are a total order over the candidate set and the winner is independent of
  input order. These tests assert exactly that — over every permutation — so
  the day a caller drops ``key_fn`` (or the cache starts returning duplicate
  release ids) the equivalence stops being free and this suite says so.
* :class:`TestSharedImplementationReachability` pins that both callers actually
  route through the shared helpers — not merely import them — and that the two
  cache-row-to-candidate mappings are one function.
* :class:`TestCandidateLimitParity` ties the degrade's explicit probe limit to
  the search seam's default page size, so widening one widens both.
"""

from __future__ import annotations

import inspect
from itertools import permutations
from unittest.mock import AsyncMock

import pytest

from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import clear_all_caches
from discogs.models import DISCOGS_SEARCH_PAGE_LIMIT, DiscogsSearchResult
from discogs.service import DiscogsService
from lookup import album_level_match, typed_pair_floor
from lookup.album_level_match import resolve_typed_album_level_match
from lookup.strategies import library_miss
from lookup.strategies.library_miss import _library_miss_discogs_search
from tests.factories import make_parsed_request

# ---------------------------------------------------------------------------
# Candidate rows in ``DiscogsCacheService.search_releases``' shape — the one
# input both callers read. Keys are hard-indexed by the mapper, so a column
# rename breaks this file too (deliberately: the mapping is part of the class).
# ---------------------------------------------------------------------------


def _row(
    release_id: int, title: str, artist_name: str, *, credits: list[str] | None = None
) -> dict:
    return {
        "release_id": release_id,
        "title": title,
        "artist_name": artist_name,
        "artist_credits": credits if credits is not None else [artist_name],
        "artwork_url": f"https://i.discogs.com/{release_id}.jpg",
    }


#: ``(case_id, typed_artist, typed_album, rows, expected_release_id)``.
FLOOR_CASES: list[tuple[str, str, str, list[dict], int | None]] = [
    (
        "plain_typed_pair",
        "Jessica Pratt",
        "On Your Own Love Again",
        [_row(6730991, "On Your Own Love Again", "Jessica Pratt")],
        6730991,
    ),
    (
        "wrong_album_floor_reject",
        "Juana Molina",
        "DOGA",
        [_row(1140883, "Segundo", "Juana Molina")],
        None,
    ),
    (
        "wrong_artist_floor_reject",
        "Sessa",
        "Pequena Vertigem de Amor",
        [_row(2280551, "Pequena Vertigem de Amor", "Stereolab")],
        None,
    ),
    (
        # LML#1206: a bare-name query against Discogs' numeric disambiguation
        # suffix clears only via the suffix-stripped artist variant.
        "suffix_widened_credit",
        "Mavi",
        "Laughing so Hard, it Hurts",
        [_row(24941022, "Laughing so Hard, it Hurts", "Mavi (12)")],
        24941022,
    ),
    (
        # LML#1206 review finding 2: the exact raw credit must outrank the
        # suffix-widened one on a 100/100 tie, whichever release_id sorts
        # first. Rows are ordered so the confidence sort the PG arm applies
        # REVERSES them — if either caller leaned on input order instead of
        # the tie-break key, the two would disagree here.
        "exact_credit_beats_suffix_on_tie",
        "Mavi",
        "Let the Sun Talk",
        [
            _row(14388444, "Let the Sun Talk", "Mavi (12)"),
            _row(24941099, "Let the Sun Talk", "Mavi"),
        ],
        24941099,
    ),
    (
        # LML#784 category 4: a query-side self-titled placeholder can never
        # match a real cache title, so both callers swap the artist in.
        "self_titled_placeholder_swap",
        "Chuquimamani-Condori",
        "S/T",
        [_row(30229731, "Chuquimamani-Condori", "Chuquimamani-Condori")],
        30229731,
    ),
    (
        # LML#784: the PG arm presents a multi-artist release as the aggregate
        # credit plus per-credit names; a single-credit query clears via the
        # ``artist_credits`` variants on both callers.
        "single_credit_query_on_joined_release",
        "Duke Ellington",
        "Duke Ellington & John Coltrane",
        [
            _row(
                1379394,
                "Duke Ellington & John Coltrane",
                "Duke Ellington, John Coltrane",
                credits=["Duke Ellington", "John Coltrane"],
            )
        ],
        1379394,
    ),
    ("empty_candidate_set", "Cat Power", "Moon Pix", [], None),
]


async def _library_miss_pick(rows: list[dict], artist: str, album: str) -> int | None:
    """The release the step-3a probe admits, through the real PG arm.

    A real :class:`DiscogsService` over a stub cache, so the candidate build
    (``DiscogsService.search``'s ``_pg_read``: row mapping + confidence ranking)
    is production code rather than a restatement. ``allow_api_escalation=False``
    confines the probe to that cache arm — the same cache-only posture the
    degrade has by construction — and the live leg is stubbed to a degraded
    ``None`` so no branch can reach the network.
    """
    cache = AsyncMock()
    cache.search_releases = AsyncMock(return_value=list(rows))
    service = DiscogsService(token="lml-1321-parity", cache_service=cache)
    service._request_with_retry = AsyncMock(return_value=None)  # type: ignore[method-assign]
    clear_all_caches()
    try:
        found = await _library_miss_discogs_search(
            make_parsed_request(artist, album), service, allow_api_escalation=False
        )
    finally:
        clear_all_caches()
    return None if found is None else found[1].release_id


async def _album_degrade_pick(rows: list[dict], artist: str, album: str) -> int | None:
    """The release the LML#1318 album-level degrade admits, ``pg=None`` (unpinned)."""
    service = AsyncMock()
    service.cache_service = AsyncMock()
    service.cache_service.search_releases = AsyncMock(return_value=list(rows))
    resolved = await resolve_typed_album_level_match(service, None, artist=artist, album=album)
    return None if resolved is None else resolved.release_id


class TestFloorVerdictParity:
    """Both callers admit the same release from the same candidate rows."""

    @pytest.mark.parametrize(
        ("artist", "album", "rows", "expected"),
        [pytest.param(*case[1:], id=case[0]) for case in FLOOR_CASES],
    )
    @pytest.mark.asyncio
    async def test_both_callers_agree(self, artist, album, rows, expected):
        probe = await _library_miss_pick(rows, artist, album)
        degrade = await _album_degrade_pick(rows, artist, album)
        assert probe == expected, (
            f"step-3a library-miss probe picked {probe!r}, expected {expected!r} — "
            "the ARTIST_PLUS_ALBUM floor moved"
        )
        assert degrade == probe, (
            f"the LML#1318 album-level degrade picked {degrade!r} where the step-3a probe "
            f"picked {probe!r}. The two must admit exactly the same match class "
            "(lookup/typed_pair_floor.py); see LML#1321."
        )


class TestCandidateOrderingIsImmaterial:
    """The ordering decision: unranked SQL order == the PG arm's confidence order.

    Safe only while the floor's ``key_fn`` is a total order over the candidate
    set. If that ever stops holding, these fail rather than letting a tie-break
    divergence surface as two different match results for identical rows.
    """

    @pytest.mark.parametrize(
        ("artist", "album", "rows", "expected"),
        [pytest.param(*case[1:], id=case[0]) for case in FLOOR_CASES if len(case[3]) > 1],
    )
    @pytest.mark.asyncio
    async def test_degrade_verdict_survives_every_row_permutation(
        self, artist, album, rows, expected
    ):
        for permuted in permutations(rows):
            assert await _album_degrade_pick(list(permuted), artist, album) == expected

    def test_tie_break_key_is_a_total_order_over_the_candidate_set(self):
        """``(exact_credit_rank, release_id)`` separates every pair of candidates.

        ``search_releases`` is ``SELECT DISTINCT ON (r.id)``, so release ids are
        unique per result set — which is what makes the key total and the
        ordering immaterial. A duplicate key would restore first-wins (input
        order) semantics inside the tie.
        """
        _, artist, album, rows, _ = next(c for c in FLOOR_CASES if len(c[3]) > 1)
        candidates = [DiscogsSearchResult.from_cache_row(row) for row in rows]
        keys = [typed_pair_floor.floor_tie_break_key(artist, c) for c in candidates]
        assert len(set(keys)) == len(keys)


class TestSharedImplementationReachability:
    """Both callers reach the shared helpers at run time, not just at import."""

    def test_floor_helper_is_the_same_object_in_both_callers(self):
        assert library_miss.floor_best_typed_pair is typed_pair_floor.floor_best_typed_pair
        assert album_level_match.floor_best_typed_pair is typed_pair_floor.floor_best_typed_pair

    def test_self_titled_swap_is_the_same_object_in_both_callers(self):
        assert library_miss.typed_album_axis is typed_pair_floor.typed_album_axis
        assert album_level_match.typed_album_axis is typed_pair_floor.typed_album_axis

    @pytest.mark.asyncio
    async def test_both_callers_actually_invoke_the_shared_floor(self, monkeypatch):
        """A vestigial import would pass the identity checks above; this won't."""
        case = next(c for c in FLOOR_CASES if c[4] is not None)
        _, artist, album, rows, expected = case

        for module, pick in (
            (library_miss, _library_miss_pick),
            (album_level_match, _album_degrade_pick),
        ):
            calls: list[str] = []

            def _spy(candidates, *, artist, album, _seen=calls):
                _seen.append(album)
                return None

            monkeypatch.setattr(module, "floor_best_typed_pair", _spy)
            assert await pick(rows, artist, album) is None
            assert calls, f"{module.__name__} did not route through floor_best_typed_pair"
            monkeypatch.undo()

    @pytest.mark.asyncio
    async def test_both_arms_map_cache_rows_through_one_mapper(self, monkeypatch):
        """The row-to-candidate mapping is one function, so a cache column
        change (or a dropped ``artist_credits`` narrowing) cannot land on one
        arm only."""
        case = next(c for c in FLOOR_CASES if c[4] is not None)
        _, artist, album, rows, _ = case
        original = DiscogsSearchResult.from_cache_row

        for pick in (_library_miss_pick, _album_degrade_pick):
            seen: list[dict] = []

            def _spy(cls_row, *args, _seen=seen, **kwargs):
                _seen.append(cls_row)
                return original(cls_row, *args, **kwargs)

            monkeypatch.setattr(DiscogsSearchResult, "from_cache_row", _spy)
            await pick(rows, artist, album)
            assert seen == rows, f"{pick.__name__} did not build candidates via from_cache_row"
            monkeypatch.undo()


class TestCandidateLimitParity:
    """One named page size behind both callers' candidate sets."""

    def test_search_seam_defaults_are_the_named_constant(self):
        assert (
            inspect.signature(DiscogsService.search).parameters["limit"].default
            == DISCOGS_SEARCH_PAGE_LIMIT
        )
        assert (
            inspect.signature(DiscogsCacheService.search_releases).parameters["limit"].default
            == DISCOGS_SEARCH_PAGE_LIMIT
        )

    @pytest.mark.asyncio
    async def test_both_callers_probe_the_cache_with_the_same_limit(self):
        """Observed, not asserted from source: a widened service limit that
        left the degrade behind (or the reverse) fails here."""
        artist, album = "Jessica Pratt", "On Your Own Love Again"
        rows = [_row(6730991, album, artist)]

        probe_cache = AsyncMock()
        probe_cache.search_releases = AsyncMock(return_value=list(rows))
        service = DiscogsService(token="lml-1321-limit", cache_service=probe_cache)
        service._request_with_retry = AsyncMock(return_value=None)  # type: ignore[method-assign]
        clear_all_caches()
        try:
            await _library_miss_discogs_search(
                make_parsed_request(artist, album), service, allow_api_escalation=False
            )
        finally:
            clear_all_caches()

        degrade_service = AsyncMock()
        degrade_service.cache_service = AsyncMock()
        degrade_service.cache_service.search_releases = AsyncMock(return_value=list(rows))
        await resolve_typed_album_level_match(degrade_service, None, artist=artist, album=album)

        probe_limit = probe_cache.search_releases.await_args.kwargs["limit"]
        degrade_limit = degrade_service.cache_service.search_releases.await_args.kwargs["limit"]
        assert probe_limit == degrade_limit == DISCOGS_SEARCH_PAGE_LIMIT
