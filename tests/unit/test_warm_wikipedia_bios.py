"""Unit tests for scripts/warm_wikipedia_bios.py — the offline pre-warm
drain for lml_cache.artist_wikipedia_bio (Phase C, LML#513/#1192;
docs/plans/lml-1192-wikipedia-artist-bio.md's primary population mechanism).

PG interaction is mocked with ``AsyncMock(spec=PgSource)`` for
``process_candidate`` (mirroring ``tests/unit/test_artist_wikipedia_bio.py``);
the SQL-selection functions use the fake-cursor pattern from
``tests/unit/test_cache_miss_provenance.py``. ``process_candidate`` exercises
the REAL ``lookup.wikipedia_pick_validation.resolve_and_validate_pick``
(LML#1192 review round 4, P0-2) end to end, mocking only
``client.get_summary`` -- the picker's own dedicated test module
(``tests/unit/test_wikipedia_pick_validation.py``) pins the ten-page
tiebreak table; this file only needs to pin how ``process_candidate`` turns
a ``ValidatedPick`` into an outcome label and a write. Data-safety rule
pinned throughout: a transient fetch failure writes NOTHING (stays
resumable), non-repick candidates never touch an already-successful row,
and a repick/refresh candidate can never turn a positive extract into NULL
(LML#1192 review round 3, P0-3).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from clients.wikipedia import WikipediaFetchError, WikipediaSummary
from entity.sources import PgSource
from scripts.warm_wikipedia_bios import (
    ArtistCandidate,
    _build_rate_limiter,
    fetch_candidates,
    main,
    process_candidate,
    run_drain,
)

_ABOVE_FLOOR_CANDIDATE = ArtistCandidate(
    artist_id=99, artist_name="Stereolab", urls=["https://en.wikipedia.org/wiki/Stereolab"]
)
_BELOW_FLOOR_CANDIDATE = ArtistCandidate(
    artist_id=1,
    artist_name="Sessa",
    urls=["https://en.wikipedia.org/wiki/Completely_Unrelated_Page"],
)


@pytest.mark.asyncio
class TestProcessCandidate:
    async def test_above_floor_positive_summary_writes_extract(self):
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=WikipediaSummary(extract="A band."))
        with (
            patch(
                "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "scripts.warm_wikipedia_bios.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            outcome = await process_candidate(pg, client, _ABOVE_FLOOR_CANDIDATE, max_retries=5)
        assert outcome == "positive"
        mock_set.assert_awaited_once_with(
            pg,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/Stereolab",
            slug_score=pytest.approx(100.0),
            lang="en",
            extract="A band.",
        )
        # LML#1192 review round 4, P0-8: a real write means the artist is
        # no longer a write-nothing candidate -- no attempt record needed.
        mock_record.assert_not_awaited()

    async def test_above_floor_rejected_summary_writes_null_extract_as_negative(self):
        # LML#1192 review round 4, P0-2: a candidate that clears the score
        # floor but is REJECTED by the live fetch (client.get_summary
        # returns None -- a disambiguation page, or a 404) is a live
        # outcome distinct from "declined" (no candidate ever tried) --
        # "negative" is failure-ish (see TestRunDrain), "declined" isn't.
        # With only one wikipedia.org URL there's no next candidate to try,
        # so resolve_and_validate_pick falls through to the heuristic
        # (same, only-listed) URL with below_floor=True.
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=None)
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ) as mock_set:
            outcome = await process_candidate(pg, client, _ABOVE_FLOOR_CANDIDATE, max_retries=5)
        assert outcome == "negative"
        assert mock_set.await_args.kwargs["extract"] is None

    async def test_transient_fetch_error_writes_nothing(self):
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(side_effect=WikipediaFetchError("timed out"))
        with (
            patch(
                "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "scripts.warm_wikipedia_bios.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            outcome = await process_candidate(pg, client, _ABOVE_FLOOR_CANDIDATE, max_retries=5)
        assert outcome == "fetch_error"
        mock_set.assert_not_awaited()
        # LML#1192 review round 4, P0-8: a write-nothing outcome still needs
        # a durable attempt record, or this candidate resurfaces at the
        # front of every future incremental run forever.
        mock_record.assert_awaited_once_with(pg, discogs_artist_id=99, outcome="fetch_error")

    async def test_unresolvable_also_records_an_attempt(self):
        # LML#1192 review round 4, P0-8: unresolvable is defensive (the
        # seed's own wikipedia.org-match guarantee somehow didn't hold) but
        # still writes nothing to the content table, so it needs the same
        # durable attempt record as fetch_error.
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        candidate = ArtistCandidate(
            artist_id=13, artist_name="Nobody", urls=["https://example.com/not-wikipedia"]
        )
        with (
            patch(
                "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "scripts.warm_wikipedia_bios.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            outcome = await process_candidate(pg, client, candidate, max_retries=5)
        assert outcome == "unresolvable"
        mock_set.assert_not_awaited()
        mock_record.assert_awaited_once_with(pg, discogs_artist_id=13, outcome="unresolvable")

    async def test_below_floor_declines_without_calling_the_client(self):
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ) as mock_set:
            outcome = await process_candidate(pg, client, _BELOW_FLOOR_CANDIDATE, max_retries=5)
        assert outcome == "declined"
        client.get_summary.assert_not_called()
        assert mock_set.await_args.kwargs["extract"] is None
        assert mock_set.await_args.kwargs["wikipedia_url"] == _BELOW_FLOOR_CANDIDATE.urls[0]

    async def test_repick_candidate_with_unchanged_pick_refreshes_content_via_a_live_fetch(self):
        # LML#1192 review round 4, P0-2/P0-3: the OLD "skip the fetch
        # entirely when the freshly-computed pick equals the stored URL"
        # optimization is incompatible with fetch-validated picking --
        # knowing the pick is unchanged REQUIRES the same live fetch that
        # (for free) also refreshes the row's content, closing P0-3 (a
        # stale positive row could never be refreshed past its TTL under
        # any existing mode: incremental excludes it because a row exists,
        # retry_misses excludes it because extract IS NOT NULL, and the OLD
        # repick short-circuited to a bare last_checked_at touch that never
        # advanced fetched_at). The accepted cost: repick/refresh
        # candidates now always pay for a live fetch, even on a
        # confirmed-unchanged pick -- the whole point of re-running one of
        # these modes after this fix ships is to catch previously-wrong
        # picks a cheap pre-check would miss.
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=WikipediaSummary(extract="Refreshed text."))
        candidate = ArtistCandidate(
            artist_id=99,
            artist_name="Stereolab",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
            stored_wikipedia_url="https://en.wikipedia.org/wiki/Stereolab",
        )
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ) as mock_set:
            outcome = await process_candidate(pg, client, candidate, max_retries=5)
        assert outcome == "refreshed"
        client.get_summary.assert_awaited_once()
        mock_set.assert_awaited_once_with(
            pg,
            discogs_artist_id=99,
            wikipedia_url="https://en.wikipedia.org/wiki/Stereolab",
            slug_score=pytest.approx(100.0),
            lang="en",
            extract="Refreshed text.",
        )

    async def test_forwards_rate_limiter_to_get_summary(self):
        # LML#1192 review round 2, C4: run_drain no longer acquires the
        # limiter itself around the whole call -- it hands the SAME limiter
        # instance down so clients.wikipedia.WikipediaClient.get_summary can
        # acquire it once per actual HTTP attempt (see
        # test_clients_wikipedia.py::TestRateLimiterPerRequest for the
        # per-request behavior this enables).
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=WikipediaSummary(extract="A band."))
        fake_limiter = AsyncMock()
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ):
            await process_candidate(
                pg,
                client,
                _ABOVE_FLOOR_CANDIDATE,
                max_retries=5,
                rate_limiter=fake_limiter,
            )
        client.get_summary.assert_awaited_once_with(
            "Stereolab", "en", max_retries=5, rate_limiter=fake_limiter
        )

    async def test_repick_candidate_rewrites_when_pick_diverges_to_another_positive(self):
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=WikipediaSummary(extract="Updated text."))
        candidate = ArtistCandidate(
            artist_id=99,
            artist_name="Stereolab",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
            stored_wikipedia_url="https://en.wikipedia.org/wiki/Some_Stale_Pick",
        )
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ) as mock_set:
            outcome = await process_candidate(pg, client, candidate, max_retries=5)
        assert outcome == "positive"
        mock_set.assert_awaited_once()
        assert (
            mock_set.await_args.kwargs["wikipedia_url"] == "https://en.wikipedia.org/wiki/Stereolab"
        )

    async def test_first_ranked_candidate_rejected_falls_through_to_the_next(self):
        # LML#1192 review round 4, P0-2's central behavior: a rejected
        # above-floor candidate (disambiguation page / 404 / no extract)
        # must not end the search when a lower-ranked candidate exists --
        # the drain tries it too, and its validated content wins. The bare
        # "Sun_Ra" slug scores highest against the artist name "Sun Ra" (an
        # exact match) and is tried FIRST -- side_effect's first None
        # rejects it, and the drain falls through to the qualified
        # "Sun_Ra_(musician)" candidate, which validates.
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(
            side_effect=[None, WikipediaSummary(extract="The real page.")]
        )
        candidate = ArtistCandidate(
            artist_id=7,
            artist_name="Sun Ra",
            urls=[
                "https://en.wikipedia.org/wiki/Sun_Ra_(musician)",
                "https://en.wikipedia.org/wiki/Sun_Ra",
            ],
        )
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ) as mock_set:
            outcome = await process_candidate(pg, client, candidate, max_retries=5)
        assert outcome == "positive"
        assert client.get_summary.await_count == 2
        assert (
            mock_set.await_args.kwargs["wikipedia_url"]
            == "https://en.wikipedia.org/wiki/Sun_Ra_(musician)"
        )
        assert mock_set.await_args.kwargs["extract"] == "The real page."

    async def test_all_ranked_candidates_rejected_writes_negative_with_the_heuristic_url(self):
        # When every above-floor candidate is tried and rejected,
        # resolve_and_validate_pick falls back to the FIRST-listed
        # (heuristic) URL -- this is what gets written, distinguishing this
        # case (a live attempt happened) from a genuine below-floor decline
        # (no attempt at all).
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=None)
        candidate = ArtistCandidate(
            artist_id=8,
            artist_name="Sun Ra",
            urls=[
                "https://en.wikipedia.org/wiki/Sun_Ra",
                "https://en.wikipedia.org/wiki/Sun_Ra_(musician)",
            ],
        )
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ) as mock_set:
            outcome = await process_candidate(pg, client, candidate, max_retries=5)
        assert outcome == "negative"
        assert client.get_summary.await_count == 2
        assert mock_set.await_args.kwargs["extract"] is None
        assert mock_set.await_args.kwargs["wikipedia_url"] == candidate.urls[0]


@pytest.mark.asyncio
class TestRepickNeverDestroysAPositiveExtract:
    """LML#1192 review round 3, P0-3: a repick candidate is SELECTED only
    when the existing row already has ``extract IS NOT NULL``
    (``stored_wikipedia_url`` is populated ONLY by
    ``fetch_candidates(mode="repick")``, whose own WHERE clause guarantees
    that). If the fresh pick diverges from the stored URL into anything
    that would write ``extract=None`` -- a below-floor decline, or every
    candidate tried and rejected by a live fetch -- the write must be
    refused and the existing positive extract preserved. LML#1192 review
    round 4, P0-8 (partial): the refusal still advances the row's
    ``last_checked_at`` so a cursor-ordered repick/refresh session doesn't
    re-select the same divergent-but-protected row forever.
    This repo's standing data-safety rule (never overwrite/reset
    successfully collected data without explicit approval) applies here
    just as much as to any other cache.

    LML#1192 review round 6, C1-1/C1-2: the refusal now marks
    ``mark_artist_wikipedia_bio_refused`` (advances BOTH ``last_refused_at``
    and ``last_checked_at``) rather than the narrower
    ``touch_artist_wikipedia_bio_last_checked_at`` -- see
    ``entity/artist_wikipedia_bio.py`` for why a third clock exists at all.
    """

    async def test_diverged_below_floor_pick_keeps_the_existing_positive_extract(self):
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        candidate = ArtistCandidate(
            artist_id=1,
            artist_name="Sessa",
            urls=["https://en.wikipedia.org/wiki/Completely_Unrelated_Page"],
            stored_wikipedia_url="https://en.wikipedia.org/wiki/Some_Old_Correct_Pick",
        )
        with (
            patch(
                "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "scripts.warm_wikipedia_bios.mark_artist_wikipedia_bio_refused",
                new_callable=AsyncMock,
            ) as mock_mark_refused,
        ):
            outcome = await process_candidate(pg, client, candidate, max_retries=5)
        assert outcome == "repick_kept_existing"
        mock_set.assert_not_awaited()
        client.get_summary.assert_not_called()
        # LML#1192 review round 4, P0-8 (partial): a refused write must
        # still advance the drain's own progress cursor, or a
        # last_checked_at-ordered repick/refresh session would re-select
        # this same divergent-but-protected row forever instead of moving
        # on to the next stalest candidate. Round 6, C1-1/C1-2: it must ALSO
        # advance last_refused_at, so --refresh-stale's own ordering (which
        # reads that clock, not last_checked_at) doesn't starve behind it.
        mock_mark_refused.assert_awaited_once_with(pg, discogs_artist_id=1)

    async def test_diverged_pick_that_fetches_negative_keeps_the_existing_positive_extract(self):
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=None)  # 404 / disambiguation / no extract
        candidate = ArtistCandidate(
            artist_id=99,
            artist_name="Stereolab",
            urls=["https://en.wikipedia.org/wiki/Stereolab"],
            stored_wikipedia_url="https://en.wikipedia.org/wiki/Some_Stale_Pick",
        )
        with (
            patch(
                "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ) as mock_set,
            patch(
                "scripts.warm_wikipedia_bios.mark_artist_wikipedia_bio_refused",
                new_callable=AsyncMock,
            ) as mock_mark_refused,
        ):
            outcome = await process_candidate(pg, client, candidate, max_retries=5)
        assert outcome == "repick_kept_existing"
        mock_set.assert_not_awaited()
        mock_mark_refused.assert_awaited_once_with(pg, discogs_artist_id=99)

    async def test_non_repick_candidate_negative_writes_normally(self):
        # The guard is keyed on stored_wikipedia_url, which is None outside
        # repick/refresh -- default/--retry-misses candidates never carry
        # an existing positive extract (their own seed WHERE clauses
        # exclude rows that have one), so a negative write there is
        # legitimate and must still go through.
        pg = AsyncMock(spec=PgSource)
        client = AsyncMock()
        client.get_summary = AsyncMock(return_value=None)
        with patch(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", new_callable=AsyncMock
        ) as mock_set:
            outcome = await process_candidate(pg, client, _ABOVE_FLOOR_CANDIDATE, max_retries=5)
        assert outcome == "negative"
        mock_set.assert_awaited_once()


@pytest.mark.asyncio
class TestFetchCandidates:
    """LML#1192 review round 3, finding 12: the three near-identical
    ``fetch_*_candidates`` wrappers collapsed into one ``fetch_candidates(pg,
    *, mode, library_artist_names, limit)``. Finding 6 (P0): every mode now
    intersects with ``library_artist_names`` -- the discogs-cache
    ``artist``/``artist_url`` tables are NOT library-exclusive in prod
    (``LML_RESOLVE_NONLIBRARY_RELEASE`` and the bulk artist-resolve drain
    both accumulate non-library artists into them), so an un-intersected
    seed is a silently growing superset of the plan's target population.

    LML#1192 review round 4, P0-4: a bare ``lower(a.name) = ANY($1::text[])``
    silently drops two real cohorts -- a Discogs artist name carrying a
    disambiguation suffix (``"Sessa (2)"``, minted when multiple Discogs
    artists share a bare name) never string-equals library.db's bare
    ``"Sessa"``, and an accent-encoding mismatch between library.db and
    discogs-cache (bare ``.lower()`` doesn't normalize Unicode NFC/NFD forms
    or strip diacritics) drops accented artists outright -- three of this
    repo's own canonical fixture artists (Nilüfer Yanya, Csillagrablók,
    Hermanos Gutiérrez) carry diacritics. Library names are now bound RAW
    (no Python-side ``.lower()``) and normalization happens entirely in SQL
    via ``lower(f_unaccent(...))`` on both sides, mirroring the pattern used
    throughout the rest of this codebase (``clients/streaming/matching.py``,
    ``discogs/cache_service.py``) rather than inventing a new scheme.
    """

    async def test_incremental_groups_urls_per_artist_and_binds_library_names(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {"artist_id": 99, "name": "Stereolab", "urls": ["https://en.wikipedia.org/wiki/A"]},
                {"artist_id": 100, "name": "Sessa", "urls": ["https://en.wikipedia.org/wiki/B"]},
            ]
        )
        candidates = await fetch_candidates(
            pg, mode="incremental", library_artist_names=["Stereolab", "Sessa"], limit=None
        )
        assert candidates == [
            ArtistCandidate(
                artist_id=99, artist_name="Stereolab", urls=["https://en.wikipedia.org/wiki/A"]
            ),
            ArtistCandidate(
                artist_id=100, artist_name="Sessa", urls=["https://en.wikipedia.org/wiki/B"]
            ),
        ]
        # LML#1192 review round 4, P0-4: bound RAW -- normalization
        # (lower + unaccent) now happens entirely in SQL, on both sides of
        # the comparison, so a Python-side .lower() here would only be
        # redundant, not load-bearing.
        bound_names = pg.fetchall.await_args.args[1]
        assert set(bound_names) == {"Stereolab", "Sessa"}

    async def test_incremental_honors_limit(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="incremental", library_artist_names=["X"], limit=500)
        sql = pg.fetchall.await_args.args[0]
        assert "LIMIT" in sql
        assert pg.fetchall.await_args.args[-1] == 500

    async def test_all_four_modes_filter_on_library_artist_names(self):
        for mode in ("incremental", "retry_misses", "repick", "refresh"):
            pg = AsyncMock(spec=PgSource)
            pg.fetchall = AsyncMock(return_value=[])
            await fetch_candidates(pg, mode=mode, library_artist_names=["Stereolab"], limit=None)
            sql = pg.fetchall.await_args.args[0]
            assert "$1::text[]" in sql, f"mode={mode} seed query doesn't intersect library artists"
            assert pg.fetchall.await_args.args[1] == ["Stereolab"]

    async def test_all_four_modes_unaccent_and_strip_the_discogs_disambiguation_suffix(self):
        for mode in ("incremental", "retry_misses", "repick", "refresh"):
            pg = AsyncMock(spec=PgSource)
            pg.fetchall = AsyncMock(return_value=[])
            await fetch_candidates(pg, mode=mode, library_artist_names=["Sessa"], limit=None)
            sql = pg.fetchall.await_args.args[0]
            assert "f_unaccent(a.name" not in sql or "f_unaccent(regexp_replace(a.name" in sql, (
                f"mode={mode}: a.name must be disambiguation-stripped before unaccenting"
            )
            assert "f_unaccent(regexp_replace(a.name" in sql, (
                f"mode={mode} seed query doesn't strip a Discogs disambiguation suffix"
            )
            assert "f_unaccent(x)" in sql, (
                f"mode={mode} seed query doesn't unaccent the bound names"
            )

    async def test_retry_misses_reads_from_the_same_shape(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {"artist_id": 5, "name": "X", "urls": ["https://en.wikipedia.org/wiki/X"]},
            ]
        )
        candidates = await fetch_candidates(
            pg, mode="retry_misses", library_artist_names=["X"], limit=None
        )
        assert candidates == [
            ArtistCandidate(artist_id=5, artist_name="X", urls=["https://en.wikipedia.org/wiki/X"])
        ]

    async def test_incremental_excludes_artist_ids_with_an_attempt_record(self):
        # LML#1192 review round 4, P0-8: fetch_error/unresolvable/
        # unexpected_error leave no row in lml_cache.artist_wikipedia_bio,
        # so without this second exclusion the same artist ids resurface,
        # in the SAME fixed au.artist_id order, on every future incremental
        # run -- a permanent abort trap once they're all that's left.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="incremental", library_artist_names=["X"], limit=None)
        sql = pg.fetchall.await_args.args[0]
        assert "lml_cache.artist_wikipedia_bio_attempt" in sql
        assert "NOT EXISTS" in sql

    async def test_retry_misses_also_surfaces_attempt_only_artists(self):
        # LML#1192 review round 4, P0-8: an artist with ONLY an attempt
        # record (no lml_cache.artist_wikipedia_bio row at all) must be
        # reachable via --retry-misses -- the sole way a write-nothing
        # outcome ever gets another try.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="retry_misses", library_artist_names=["X"], limit=None)
        sql = pg.fetchall.await_args.args[0]
        assert "lml_cache.artist_wikipedia_bio_attempt" in sql
        assert "LEFT JOIN" in sql

    async def test_retry_misses_orders_by_staleness_not_artist_id(self):
        # LML#1192 review round 2, C2 (round 3, finding 13: last_checked_at,
        # not fetched_at -- the drain's own progress cursor, distinct from
        # content-age). ORDER BY au.artist_id made `--retry-misses --limit N`
        # re-select the exact same lowest-N artist ids forever (a
        # declined/negative row's extract stays NULL after every
        # re-attempt, so it never drops out of the WHERE clause).
        # last_checked_at ASC progresses instead, since a re-attempted
        # row's last_checked_at is bumped by set_cached_artist_wikipedia_bio's
        # UPSERT on every write, pushing it to the back of the queue.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="retry_misses", library_artist_names=["X"], limit=None)
        sql = pg.fetchall.await_args.args[0]
        # LML#1192 review round 6, C1-3: GREATEST, not COALESCE. A
        # write-nothing outcome (fetch_error) only ever advances
        # ba.attempted_at; a content-table outcome (negative/refused
        # refresh) only ever advances b.last_checked_at. For an artist with
        # BOTH rows, COALESCE always preferred b.last_checked_at even when
        # ba.attempted_at was the more recent re-check -- sorting the
        # failing artist ahead of successfully re-checked ones and, under a
        # sustained outage, re-pinning the same rows at the head of every
        # --limit-bounded session. GREATEST is NULL-ignoring in Postgres
        # (verified live), so it correctly picks whichever clock is
        # actually more recent.
        assert "ORDER BY GREATEST(b.last_checked_at, ba.attempted_at) ASC" in sql
        assert "ORDER BY au.artist_id" not in sql

    async def test_repick_candidates_carry_the_stored_url(self):
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "artist_id": 5,
                    "name": "X",
                    "urls": ["https://en.wikipedia.org/wiki/X"],
                    "stored_url": "https://en.wikipedia.org/wiki/Old_X",
                },
            ]
        )
        candidates = await fetch_candidates(
            pg, mode="repick", library_artist_names=["X"], limit=None
        )
        assert candidates == [
            ArtistCandidate(
                artist_id=5,
                artist_name="X",
                urls=["https://en.wikipedia.org/wiki/X"],
                stored_wikipedia_url="https://en.wikipedia.org/wiki/Old_X",
            )
        ]

    async def test_repick_orders_by_staleness_not_artist_id(self):
        # LML#1192 review round 2, C2 (round 3, finding 13): same
        # starvation risk as --retry-misses. LML#1192 review round 4, P0-2:
        # every repick candidate now writes -- either a fresh extract
        # ("positive"/"refreshed", advancing last_checked_at via the
        # UPSERT) or, on a refused divergence, a bare last_checked_at touch
        # (P0-8 partial) -- so this ordering is what lets successive
        # --repick --limit N runs actually progress through the table
        # instead of re-checking the same already-verified rows forever.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="repick", library_artist_names=["X"], limit=None)
        sql = pg.fetchall.await_args.args[0]
        assert "ORDER BY b.last_checked_at ASC" in sql
        assert "ORDER BY au.artist_id" not in sql

    async def test_incremental_and_retry_misses_never_select_a_stored_url_column(self):
        # Only --repick/--refresh-stale candidates carry stored_wikipedia_url
        # -- it's the signal TestRepickNeverDestroysAPositiveExtract's guard
        # keys off of, so it must never leak a non-None value from the other
        # modes.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {"artist_id": 5, "name": "X", "urls": ["https://en.wikipedia.org/wiki/X"]}
            ]
        )
        for mode in ("incremental", "retry_misses"):
            candidates = await fetch_candidates(
                pg, mode=mode, library_artist_names=["X"], limit=None
            )
            assert candidates[0].stored_wikipedia_url is None

    async def test_refresh_candidates_carry_the_stored_url(self):
        # LML#1192 review round 4, P0-3: --refresh-stale reuses the SAME
        # repick/refresh candidate shape (stored_wikipedia_url populated) so
        # process_candidate treats it identically -- fetch-validate, and
        # write a fresh extract via _write_bio whether the pick lands
        # unchanged ("refreshed") or diverges ("positive"/"repick_kept_existing").
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "artist_id": 5,
                    "name": "X",
                    "urls": ["https://en.wikipedia.org/wiki/X"],
                    "stored_url": "https://en.wikipedia.org/wiki/X",
                },
            ]
        )
        candidates = await fetch_candidates(
            pg, mode="refresh", library_artist_names=["X"], limit=None
        )
        assert candidates == [
            ArtistCandidate(
                artist_id=5,
                artist_name="X",
                urls=["https://en.wikipedia.org/wiki/X"],
                stored_wikipedia_url="https://en.wikipedia.org/wiki/X",
            )
        ]

    async def test_refresh_binds_the_default_success_ttl_cutoff_at_the_second_param(self):
        # LML#1192 review round 4, P0-3: a stale positive row could never be
        # refreshed under any other mode -- incremental excludes it because
        # a row already exists, retry_misses excludes it because
        # extract IS NOT NULL, and (pre-round-4) --repick short-circuited an
        # unchanged pick to a bare last_checked_at touch that never advanced
        # fetched_at. --refresh-stale is scoped to ONLY genuinely-stale rows
        # (unlike a full --repick sweep, which now re-fetches every positive
        # row regardless of freshness) via a fetched_at < now() - TTL
        # predicate.
        from entity.artist_wikipedia_bio import DEFAULT_SUCCESS_TTL

        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="refresh", library_artist_names=["X"], limit=None)
        sql, names, cutoff = pg.fetchall.await_args.args
        assert "b.fetched_at <" in sql
        assert "b.extract IS NOT NULL" in sql
        assert names == ["X"]
        assert cutoff == DEFAULT_SUCCESS_TTL

    async def test_refresh_honors_limit_at_the_third_param(self):
        # Unlike every other mode, refresh already binds a second param (the
        # TTL cutoff), so LIMIT must bind at $3, not $2 -- fetch_candidates
        # can't reuse the generic _with_limit helper for this mode.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="refresh", library_artist_names=["X"], limit=250)
        sql = pg.fetchall.await_args.args[0]
        assert "LIMIT $3" in sql
        assert pg.fetchall.await_args.args[-1] == 250

    async def test_refresh_orders_by_fetched_at_not_last_checked_at(self):
        # The whole point of --refresh-stale is content freshness, not
        # attempt-cursor progression -- it should refresh the OLDEST content
        # first, not the least-recently-touched row.
        #
        # LML#1192 review round 6, C1-2: COALESCE(b.last_refused_at,
        # b.fetched_at), not fetched_at alone. A refused refresh (the P0-5
        # guard fired) advances ONLY last_refused_at, never fetched_at --
        # ordering on fetched_at alone left a refused row's content-age
        # clock frozen forever, so it kept re-sorting to the head of every
        # --refresh-stale session: once ten of these accumulate, the
        # consecutive-failure guard trips and starves every stale row
        # behind them. COALESCE prefers the more-recent refusal when one
        # exists, falling back to content age when it doesn't -- WHERE
        # eligibility (which rows are stale enough to be a candidate at
        # all) is unchanged, only the ORDER moves.
        pg = AsyncMock(spec=PgSource)
        pg.fetchall = AsyncMock(return_value=[])
        await fetch_candidates(pg, mode="refresh", library_artist_names=["X"], limit=None)
        sql = pg.fetchall.await_args.args[0]
        assert "ORDER BY COALESCE(b.last_refused_at, b.fetched_at) ASC" in sql


@pytest.mark.asyncio
class TestBuildRateLimiter:
    """LML#1192 review round 3, P0-2: ``AsyncLimiter(max(rate, 0.01), 1)``
    requires ``acquire(1) <= max_rate`` -- any rate below 1.0 (an operator
    deliberately throttling down under 429 pressure, e.g. ``--rate-per-second
    0.5``) raised ``ValueError`` on the very first acquisition, before any
    HTTP attempt, and that ``ValueError`` isn't a ``WikipediaFetchError`` so
    it fell into the bare ``except Exception`` as ``unexpected_error`` --
    exactly the operator action most likely to be needed during an outage
    would have killed the run instead of slowing it down. This went
    untested because every prior ``TestRunDrain`` case used rate 100.0 *and*
    patched out ``process_candidate``, so the limiter was constructed but
    never actually acquired.
    """

    @pytest.mark.parametrize("rate", [3.0, 1.0, 0.9, 0.5, 0.1, 0.01])
    async def test_any_positive_rate_can_actually_be_acquired(self, rate):
        limiter = _build_rate_limiter(rate)
        await limiter.acquire()  # must not raise, for any positive rate

    async def test_zero_or_negative_rate_falls_back_to_a_floor_not_a_crash(self):
        for rate in (0.0, -5.0):
            limiter = _build_rate_limiter(rate)
            await limiter.acquire()


@pytest.mark.asyncio
class TestRunDrain:
    """LML#1192 review round 2, C3 (second half): a single candidate's
    unexpected exception used to propagate straight out of ``run_drain``,
    killing a multi-hour session and skipping ``report.print_summary()``
    entirely (called only after ``run_drain`` returns, in ``_run``). Every
    candidate is now wrapped so one failure can't take down the whole run,
    and C4's consecutive-failure abort (a real, non-transient outage
    shouldn't grind through the remaining candidates making zero progress)
    still returns a normal, printable report rather than raising.

    LML#1192 review round 3, finding 4: the abort guard now covers every
    failure-ish outcome (``fetch_error``, ``unexpected_error``,
    ``negative``, ``repick_kept_existing``) -- not just the original two --
    and ``DrainReport.aborted`` lets ``main()`` return non-zero when a
    session stopped early having made no further progress.

    LML#1192 review round 4, P0-2: ``"unparseable"`` no longer exists as an
    outcome -- ``resolve_and_validate_pick``'s ranked candidates are already
    filtered through the exact same URL regex ``wikipedia_title_from_url``
    uses, so any candidate that reaches a live fetch attempt is guaranteed
    parseable. ``"unchanged"`` no longer exists either -- see
    ``TestProcessCandidate.test_repick_candidate_with_unchanged_pick_refreshes_content_via_a_live_fetch``;
    its replacement, ``"refreshed"``, is a success outcome like
    ``"positive"``, not a failure-ish one.
    """

    def _candidates(self, n: int) -> list[ArtistCandidate]:
        return [
            ArtistCandidate(
                artist_id=i, artist_name=f"Artist {i}", urls=[f"https://en.wikipedia.org/wiki/A{i}"]
            )
            for i in range(1, n + 1)
        ]

    async def test_one_unexpected_exception_does_not_abort_the_session(self):
        candidates = self._candidates(3)
        pg = AsyncMock(spec=PgSource)

        async def fake_process(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            if candidate.artist_id == 2:
                raise RuntimeError("boom")
            return "positive"

        with (
            patch(
                "scripts.warm_wikipedia_bios.process_candidate", side_effect=fake_process
            ) as mock_process,
            patch(
                "scripts.warm_wikipedia_bios.record_artist_wikipedia_bio_attempt",
                new_callable=AsyncMock,
            ) as mock_record,
        ):
            report = await run_drain(
                pg,
                candidates,
                rate_per_second=100.0,
                max_retries=1,
            )

        assert mock_process.await_count == 3
        assert report.total == 3
        assert report.counts["positive"] == 2
        assert report.counts["unexpected_error"] == 1
        assert report.aborted is False
        # LML#1192 review round 4, P0-8: an unhandled exception writes
        # nothing to the content table either -- it needs the same durable
        # attempt record as fetch_error/unresolvable, or it also resurfaces
        # at the front of every future incremental run forever.
        mock_record.assert_awaited_once_with(pg, discogs_artist_id=2, outcome="unexpected_error")

    async def test_sub_one_rps_does_not_crash_the_real_limiter(self):
        # LML#1192 review round 3, P0-2: unlike every other TestRunDrain
        # case, this one does NOT patch out process_candidate -- it lets the
        # REAL rate_per_second=0.5 flow all the way down to
        # clients.wikipedia.WikipediaClient.get_summary's actual acquire(),
        # only stubbing the client itself (no real HTTP). Before the fix,
        # AsyncLimiter(max(0.5, 0.01), 1) raised ValueError on the first
        # acquisition -- an operator throttling down under 429 pressure
        # would have killed the run instead of slowing it down.
        # Slug must MATCH the artist name (unlike self._candidates's
        # "Artist N" / "A{N}" pairing) so the pick clears the floor and
        # actually reaches client.get_summary -- otherwise this test
        # exercises the below-floor "declined" branch, which never touches
        # the rate limiter at all, and would pass even with the bug.
        candidates = [
            ArtistCandidate(
                artist_id=i,
                artist_name=f"Artist{i}",
                urls=[f"https://en.wikipedia.org/wiki/Artist{i}"],
            )
            for i in range(1, 3)
        ]
        fake_client = AsyncMock()
        fake_client.get_summary = AsyncMock(return_value=WikipediaSummary(extract="A bio."))
        with (
            patch("scripts.warm_wikipedia_bios.WikipediaClient", return_value=fake_client),
            patch(
                "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio",
                new_callable=AsyncMock,
            ),
        ):
            report = await run_drain(
                AsyncMock(spec=PgSource),
                candidates,
                rate_per_second=0.5,
                max_retries=1,
            )
        assert report.total == 2
        assert report.counts.get("positive") == 2
        assert fake_client.get_summary.await_count == 2

    async def test_consecutive_failures_abort_the_session_early(self, monkeypatch):
        monkeypatch.setattr("scripts.warm_wikipedia_bios._MAX_CONSECUTIVE_FAILURES", 3)
        candidates = self._candidates(10)

        async def always_fails(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            return "fetch_error"

        with patch("scripts.warm_wikipedia_bios.process_candidate", side_effect=always_fails):
            report = await run_drain(
                AsyncMock(spec=PgSource),
                candidates,
                rate_per_second=100.0,
                max_retries=1,
            )

        # Aborts after exactly 3 consecutive failures -- doesn't burn
        # through all 10 candidates making zero progress.
        assert report.total == 3
        assert report.counts["fetch_error"] == 3
        assert report.aborted is True

    async def test_a_success_resets_the_consecutive_failure_counter(self, monkeypatch):
        monkeypatch.setattr("scripts.warm_wikipedia_bios._MAX_CONSECUTIVE_FAILURES", 3)
        candidates = self._candidates(9)

        async def fails_then_succeeds_every_other(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            # Two failures, one success, repeating -- consecutive-failure
            # count never reaches 3, so the whole batch should complete.
            return "fetch_error" if candidate.artist_id % 3 != 0 else "positive"

        with patch(
            "scripts.warm_wikipedia_bios.process_candidate",
            side_effect=fails_then_succeeds_every_other,
        ):
            report = await run_drain(
                AsyncMock(spec=PgSource),
                candidates,
                rate_per_second=100.0,
                max_retries=1,
            )

        assert report.total == 9
        assert report.counts["positive"] == 3
        assert report.counts["fetch_error"] == 6
        assert report.aborted is False

    @pytest.mark.parametrize("failure_outcome", ["repick_kept_existing"])
    async def test_abort_guard_covers_every_failure_ish_outcome_not_just_fetch_error(
        self, monkeypatch, failure_outcome
    ):
        # LML#1192 review round 3, finding 4: the ORIGINAL guard only
        # counted fetch_error/unexpected_error -- a storm of
        # repick_kept_existing conflicts completed "normally" despite being
        # just as clear a signal that something systemic is wrong (a
        # pick-quality regression) as a fetch_error storm. "negative" was
        # REMOVED from this list in review round 6, C2-2 -- see
        # test_negative_never_counts_toward_the_abort below.
        monkeypatch.setattr("scripts.warm_wikipedia_bios._MAX_CONSECUTIVE_FAILURES", 3)
        candidates = self._candidates(10)

        async def always_this_outcome(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            return failure_outcome

        with patch(
            "scripts.warm_wikipedia_bios.process_candidate", side_effect=always_this_outcome
        ):
            report = await run_drain(
                AsyncMock(spec=PgSource),
                candidates,
                rate_per_second=100.0,
                max_retries=1,
            )

        assert report.total == 3
        assert report.aborted is True

    async def test_negative_never_counts_toward_the_abort(self, monkeypatch):
        # LML#1192 review round 6, C2-2: "negative" is not a failure -- it's
        # a healthy, correctly-cached outcome, and blocked the flip. Ten
        # consecutive negatives used to abort with a non-zero exit even
        # though every one was a genuine, correctly-recorded "asked, no
        # page" result. Structurally worst for --retry-misses, whose
        # candidate set is BY CONSTRUCTION previously-negative rows that
        # deterministically re-return the same outcome -- a guard firing on
        # that expected steady state is a hard blocker on running the drain
        # to completion before the prod flag flip.
        monkeypatch.setattr("scripts.warm_wikipedia_bios._MAX_CONSECUTIVE_FAILURES", 3)
        candidates = self._candidates(10)

        async def always_negative(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            return "negative"

        with patch("scripts.warm_wikipedia_bios.process_candidate", side_effect=always_negative):
            report = await run_drain(
                AsyncMock(spec=PgSource),
                candidates,
                rate_per_second=100.0,
                max_retries=1,
            )

        assert report.total == 10
        assert report.aborted is False

    async def test_declined_and_refreshed_never_count_toward_the_abort(self, monkeypatch):
        # "declined" (no candidate ever cleared the floor -- never touches
        # the network) and "refreshed" (a repick/refresh candidate whose
        # validated pick matched the stored URL -- a SUCCESS outcome, like
        # "positive") are both legitimate, high-frequency outcomes; neither
        # may trip the outage-detection guard.
        monkeypatch.setattr("scripts.warm_wikipedia_bios._MAX_CONSECUTIVE_FAILURES", 3)
        candidates = self._candidates(10)

        async def alternating(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            return "declined" if candidate.artist_id % 2 else "refreshed"

        with patch("scripts.warm_wikipedia_bios.process_candidate", side_effect=alternating):
            report = await run_drain(
                AsyncMock(spec=PgSource),
                candidates,
                rate_per_second=100.0,
                max_retries=1,
            )

        assert report.total == 10
        assert report.aborted is False


class TestMainExitCode:
    """LML#1192 review round 3, finding 4: ``main()`` always returned 0,
    even when the session aborted having written little or nothing --
    indistinguishable from a clean completion to any caller checking the
    process exit code (a cron wrapper, an operator's shell script).

    Deliberately NOT ``@pytest.mark.asyncio``/``async def`` -- ``main()``
    calls ``asyncio.run()`` internally, which raises if invoked from
    inside an already-running event loop (exactly what an async test
    function would put it in).
    """

    @pytest.fixture(autouse=True)
    def _reset_settings_cache(self):
        # config.settings.get_settings is @lru_cache'd and blanked
        # session-wide by tests/unit/conftest.py's scrub_credential_env
        # (DATABASE_URL_DISCOGS included) -- clear the cache so THIS test's
        # monkeypatch.setenv actually reaches a freshly-built Settings
        # instead of returning whatever got cached by an earlier test in
        # the full-suite run order.
        from config.settings import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_returns_zero_on_normal_completion(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL_DISCOGS", "postgresql://fake/discogs")
        library_db = tmp_path / "library.db"
        library_db.touch()
        with (
            patch("scripts.warm_wikipedia_bios.asyncpg.create_pool", new_callable=AsyncMock),
            patch(
                "scripts.warm_wikipedia_bios.set_up_artist_wikipedia_bio_schema",
                new_callable=AsyncMock,
            ),
            patch(
                "scripts.warm_wikipedia_bios.set_up_artist_wikipedia_bio_attempt_schema",
                new_callable=AsyncMock,
            ),
            patch("scripts.warm_wikipedia_bios._load_library_artist_names", return_value=["X"]),
            patch(
                "scripts.warm_wikipedia_bios.fetch_candidates", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "scripts.warm_wikipedia_bios.run_drain", new_callable=AsyncMock
            ) as mock_run_drain,
        ):
            candidate = ArtistCandidate(
                artist_id=1, artist_name="X", urls=["https://en.wikipedia.org/wiki/X"]
            )
            mock_fetch.return_value = [candidate]
            mock_run_drain.return_value = _report(aborted=False, counts={"positive": 1})
            exit_code = main(["--library-db", str(library_db)])
        assert exit_code == 0

    def test_returns_nonzero_when_the_session_aborted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL_DISCOGS", "postgresql://fake/discogs")
        library_db = tmp_path / "library.db"
        library_db.touch()
        with (
            patch("scripts.warm_wikipedia_bios.asyncpg.create_pool", new_callable=AsyncMock),
            patch(
                "scripts.warm_wikipedia_bios.set_up_artist_wikipedia_bio_schema",
                new_callable=AsyncMock,
            ),
            patch(
                "scripts.warm_wikipedia_bios.set_up_artist_wikipedia_bio_attempt_schema",
                new_callable=AsyncMock,
            ),
            patch("scripts.warm_wikipedia_bios._load_library_artist_names", return_value=["X"]),
            patch(
                "scripts.warm_wikipedia_bios.fetch_candidates", new_callable=AsyncMock
            ) as mock_fetch,
            patch(
                "scripts.warm_wikipedia_bios.run_drain", new_callable=AsyncMock
            ) as mock_run_drain,
        ):
            candidate = ArtistCandidate(
                artist_id=1, artist_name="X", urls=["https://en.wikipedia.org/wiki/X"]
            )
            mock_fetch.return_value = [candidate]
            mock_run_drain.return_value = _report(aborted=True, counts={"fetch_error": 10})
            exit_code = main(["--library-db", str(library_db)])
        assert exit_code != 0


def _report(*, aborted: bool, counts: dict[str, int]):
    from scripts.warm_wikipedia_bios import DrainReport

    report = DrainReport()
    report.counts.update(counts)
    report.aborted = aborted
    return report


class _ScriptedShutdownFlag:
    """ShutdownFlagProtocol stub: answers ``requested`` from a script, then
    holds True forever -- models an operator's first Ctrl-C landing between
    two candidates."""

    def __init__(self, answers: list[bool]) -> None:
        self._answers = iter(answers)

    @property
    def requested(self) -> bool:
        return next(self._answers, True)


@pytest.mark.asyncio
class TestShutdownFlag:
    """LML#1192 cross-PR review, round 7: the drain was the family's only
    long-running script WITHOUT the scripts/_lib two-stage graceful Ctrl-C
    -- SIGINT landed as a raw KeyboardInterrupt inside asyncio.run, killing
    a multi-hour session with no summary and an exit indistinguishable from
    a crash, while every sibling drain has trained the operator that Ctrl-C
    is safe. run_drain now takes the same optional ShutdownFlagProtocol the
    LML#1020 backfill threads through, checked at the candidate boundary."""

    def _candidates(self, n: int) -> list[ArtistCandidate]:
        return [
            ArtistCandidate(
                artist_id=i, artist_name=f"Artist {i}", urls=[f"https://en.wikipedia.org/wiki/A{i}"]
            )
            for i in range(1, n + 1)
        ]

    async def test_shutdown_stops_at_the_candidate_boundary(self, monkeypatch):
        candidates = self._candidates(3)
        pg = AsyncMock(spec=PgSource)

        async def fake_process(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            return "positive"

        monkeypatch.setattr("scripts.warm_wikipedia_bios.process_candidate", fake_process)
        report = await run_drain(
            pg,
            candidates,
            rate_per_second=100,
            max_retries=1,
            shutdown=_ScriptedShutdownFlag([False, True]),
        )
        # Candidate 1 processed, then the flag stops the loop BEFORE
        # candidate 2 -- a clean, resumable stop, not an abort.
        assert report.counts == {"positive": 1}
        assert report.aborted is False

    async def test_no_flag_processes_everything(self, monkeypatch):
        candidates = self._candidates(2)
        pg = AsyncMock(spec=PgSource)

        async def fake_process(
            pg, client, candidate, *, max_retries, rate_limiter=None, dry_run=False
        ):
            return "positive"

        monkeypatch.setattr("scripts.warm_wikipedia_bios.process_candidate", fake_process)
        report = await run_drain(pg, candidates, rate_per_second=100, max_retries=1)
        assert report.counts == {"positive": 2}


@pytest.mark.asyncio
class TestDryRun:
    """LML#1192 cross-PR review, round 7: every sibling drain takes a
    no-write preview mode; this one's --repick/--refresh-stale rewrite every
    positive row they visit, which is exactly the pass an operator most
    wants to preview. Dry-run runs the full picker + live fetches and
    tallies outcomes but writes NOTHING -- neither content rows nor durable
    attempt records."""

    async def test_positive_outcome_writes_nothing(self, monkeypatch):
        from lookup.wikipedia_pick_validation import ValidatedPick
        from lookup.wikipedia_url import PickedWikiUrl

        pg = AsyncMock(spec=PgSource)
        write_content = AsyncMock()
        write_refusal = AsyncMock()
        write_attempt = AsyncMock()
        monkeypatch.setattr(
            "scripts.warm_wikipedia_bios.set_cached_artist_wikipedia_bio", write_content
        )
        monkeypatch.setattr(
            "scripts.warm_wikipedia_bios.mark_artist_wikipedia_bio_refused", write_refusal
        )
        monkeypatch.setattr(
            "scripts.warm_wikipedia_bios.record_artist_wikipedia_bio_attempt", write_attempt
        )

        async def fake_resolve(urls, artist_name, *, fetch, max_candidates=None):
            return ValidatedPick(
                picked=PickedWikiUrl(
                    url="https://en.wikipedia.org/wiki/Stereolab",
                    lang="en",
                    slug_score=100.0,
                    below_floor=False,
                ),
                summary=WikipediaSummary(extract="Stereolab are an Anglo-French band."),
            )

        monkeypatch.setattr("scripts.warm_wikipedia_bios.resolve_and_validate_pick", fake_resolve)
        outcome = await process_candidate(
            pg, AsyncMock(), _ABOVE_FLOOR_CANDIDATE, max_retries=1, dry_run=True
        )
        assert outcome == "positive"
        write_content.assert_not_awaited()
        write_refusal.assert_not_awaited()
        write_attempt.assert_not_awaited()

    async def test_fetch_error_records_no_attempt(self, monkeypatch):
        pg = AsyncMock(spec=PgSource)
        write_attempt = AsyncMock()
        monkeypatch.setattr(
            "scripts.warm_wikipedia_bios.record_artist_wikipedia_bio_attempt", write_attempt
        )

        async def fake_resolve(urls, artist_name, *, fetch, max_candidates=None):
            raise WikipediaFetchError("timeout")

        monkeypatch.setattr("scripts.warm_wikipedia_bios.resolve_and_validate_pick", fake_resolve)
        outcome = await process_candidate(
            pg, AsyncMock(), _ABOVE_FLOOR_CANDIDATE, max_retries=1, dry_run=True
        )
        assert outcome == "fetch_error"
        write_attempt.assert_not_awaited()


class TestRateFlagAlias:
    """LML#1192 cross-PR review, round 7: the drain family's flag for the
    identical concept is --rate (ytm_coverage_drain); --rate-per-second
    stays as an alias so neither spelling breaks."""

    def test_rate_is_the_canonical_spelling(self):
        from scripts.warm_wikipedia_bios import _build_arg_parser

        args = _build_arg_parser().parse_args(["--rate", "2.5"])
        assert args.rate == 2.5

    def test_rate_per_second_still_works_as_an_alias(self):
        from scripts.warm_wikipedia_bios import _build_arg_parser

        args = _build_arg_parser().parse_args(["--rate-per-second", "1.5"])
        assert args.rate == 1.5

    def test_dry_run_flag_exists_and_defaults_off(self):
        from scripts.warm_wikipedia_bios import _build_arg_parser

        assert _build_arg_parser().parse_args([]).dry_run is False
        assert _build_arg_parser().parse_args(["--dry-run"]).dry_run is True
