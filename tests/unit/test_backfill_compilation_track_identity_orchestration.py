"""Unit tests for the LML#1020 backfill's orchestration layer (slice 5).

Covers the fan-back grouping, the --incremental / --retry-misses selection
predicates (evaluated per (library_id, track_artist, track_title, source) —
not per credit, so a credit already resolved on Discogs but still missing a
MusicBrainz row is selected for the MB leg alone), and ``run_backfill`` end
to end over fakes (no HTTP, no real PG). The pg-marked fixture-scale drain
lives in ``tests/integration/test_backfill_compilation_track_identity_pg.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from wxyc_etl.text import to_match_form

from scripts.backfill_compilation_track_identity import (
    CtaCredit,
    filter_by_attempted_keys,
    group_credits_by_artist,
    run_backfill,
    select_credit_population,
)


def _credit(library_id: int, artist: str, title: str | None) -> CtaCredit:
    return CtaCredit(library_id=library_id, track_artist=artist, track_title=title)


class TestGroupCreditsByArtist:
    def test_groups_by_the_raw_credit_string(self):
        credits = [
            _credit(1, "Sessa", "Musica"),
            _credit(2, "Sessa", "Other Track"),
            _credit(3, "Juana Molina", "La Paradoja"),
        ]

        grouped = group_credits_by_artist(credits)

        assert set(grouped) == {"Sessa", "Juana Molina"}
        assert len(grouped["Sessa"]) == 2
        assert len(grouped["Juana Molina"]) == 1

    def test_preserves_insertion_order_of_distinct_artists(self):
        credits = [_credit(1, "B", "x"), _credit(2, "A", "y"), _credit(3, "B", "z")]
        grouped = group_credits_by_artist(credits)
        assert list(grouped) == ["B", "A"]


class TestFilterByAttemptedKeys:
    def test_excludes_credits_whose_normalized_key_is_already_attempted(self):
        credits = [
            _credit(1, "Sessa", "Musica"),
            _credit(2, "Juana Molina", "La Paradoja"),
        ]
        attempted = {(1, to_match_form("Sessa"), to_match_form("Musica"))}

        remaining = filter_by_attempted_keys(credits, attempted)

        assert remaining == [credits[1]]

    def test_none_title_normalizes_to_empty_string_in_the_key(self):
        credits = [_credit(1, "Sessa", None)]
        attempted = {(1, to_match_form("Sessa"), "")}

        remaining = filter_by_attempted_keys(credits, attempted)

        assert remaining == []

    def test_empty_attempted_set_keeps_everything(self):
        credits = [_credit(1, "Sessa", "Musica")]
        assert filter_by_attempted_keys(credits, set()) == credits


def _resolved(name, discogs_artist_id=1, method="api_search", cache_corroboration=None):
    return {
        "name": name,
        "discogs_artist_id": discogs_artist_id,
        "canonical_name": name,
        "method": method,
        "cache_corroboration": cache_corroboration or [],
        "unresolved_reason": None,
        "candidate_count": 1,
    }


def _unresolved(name, reason="not_found"):
    return {
        "name": name,
        "discogs_artist_id": None,
        "canonical_name": None,
        "method": None,
        "cache_corroboration": [],
        "unresolved_reason": reason,
        "candidate_count": 0 if reason == "not_found" else None,
    }


def _fake_post_batch(verdicts_by_name: dict[str, dict]):
    async def post_batch(names: list[str], dry_run: bool) -> list[dict]:
        return [verdicts_by_name[name] for name in names]

    return post_batch


@pytest.mark.asyncio
class TestRunBackfill:
    async def test_writes_a_row_per_fanned_back_credit(self):
        credits = [
            _credit(1, "Sessa", "Musica"),
            _credit(2, "Sessa", "Other Track"),
        ]
        post_batch = _fake_post_batch({"Sessa": _resolved("Sessa", discogs_artist_id=42)})
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        assert stats.rows_written == 2  # one discogs row per fanned-back credit
        assert stats.distinct_credits == 1
        assert pg.execute.await_count == 2

    async def test_verdicts_persist_page_by_page_so_an_abort_keeps_prior_pages(self):
        # The all-or-nothing defect: verdicts must be written as each page
        # resolves, NOT accumulated until both legs finish -- otherwise a
        # crash ten hours into a drain discards every verdict the shared
        # Discogs budget already paid for. 26 distinct artists = two pages
        # (PAGE_SIZE 25); the second page's POST dies with a transport error.
        # Page 1's 25 rows must already be in PG, and the error must end the
        # pass gracefully (run_drain's degrade-not-crash posture), not
        # propagate.
        credits = [_credit(i, f"Artist {i:02d}", "Track") for i in range(26)]
        verdicts = {f"Artist {i:02d}": _resolved(f"Artist {i:02d}") for i in range(26)}
        calls = []

        async def post_batch(names: list[str], dry_run: bool) -> list[dict]:
            calls.append(list(names))
            if len(calls) > 1:
                raise httpx.ConnectError("stream reset mid-drain")
            return [verdicts[name] for name in names]

        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        assert len(calls) == 2  # page 2 was attempted and aborted the pass
        assert stats.rows_written == 25  # page 1 persisted BEFORE page 2 ran
        assert pg.execute.await_count == 25

    async def test_invalid_credit_gets_a_musicbrainz_miss_row_not_eternal_reselection(self):
        # The MB leg applies the same structural gate as the Discogs leg: an
        # invalid credit gets a deterministic MB miss row without touching
        # the MB PG at all. Without the gate, the credit would raise inside
        # the PG bind, be classified "not consulted" (no row), and be
        # re-selected -- and re-paid -- by every --incremental run forever.
        credits = [_credit(1, "(2)", "Track")]
        post_batch = _fake_post_batch({})  # KeyErrors if any HTTP call happens
        mb_pg = AsyncMock()
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=mb_pg,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        mb_pg.fetchall.assert_not_awaited()
        sources_written = {call.args[4] for call in pg.execute.await_args_list}
        external_ids = {call.args[5] for call in pg.execute.await_args_list}
        assert sources_written == {"discogs", "musicbrainz"}
        assert external_ids == {None}  # both rows are misses -- attempted, resolved nothing
        assert stats.rows_written == 2

    async def test_library_id_binds_hold_the_library_db_id_space_verbatim(self):
        # F2's fixture proof: the id this pipeline stores is library.db's
        # library.id (the legacy MySQL LIBRARY_RELEASE_ID), flowing verbatim
        # from CtaCredit into the write binds with no remapping. 424242 is
        # deliberately unlike any small Backend serial -- a consumer that
        # joins it against wxyc_schema.library.id gets zero rows, which is
        # exactly why the sidecar comment names the legacy_release_id bridge.
        credits = [_credit(424242, "Sessa", "Grandeza")]
        post_batch = _fake_post_batch({"Sessa": _resolved("Sessa")})
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        library_id_binds = {call.args[1] for call in pg.execute.await_args_list}
        assert library_id_binds == {424242}

    async def test_dry_run_write_never_touches_pg(self):
        credits = [_credit(1, "Sessa", "Musica")]
        post_batch = _fake_post_batch({"Sessa": _resolved("Sessa")})
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg,
            dry_run_write=True,
            dry_run_http=True,
        )

        pg.execute.assert_not_awaited()
        assert stats.rows_written == 0

    async def test_writes_both_sources_when_mb_configured(self):
        credits = [_credit(1, "Sessa", "Musica")]
        post_batch = _fake_post_batch({"Sessa": _resolved("Sessa")})
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[{"id": "mb-1", "name": "Sessa", "score": 0.95}])
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=mb_pg,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        assert stats.rows_written == 2  # one discogs row + one musicbrainz row
        assert pg.execute.await_count == 2
        sources = {call.args[4] for call in pg.execute.await_args_list}
        assert sources == {"discogs", "musicbrainz"}

    async def test_mb_unconfigured_writes_no_musicbrainz_row(self):
        credits = [_credit(1, "Sessa", "Musica")]
        post_batch = _fake_post_batch({"Sessa": _resolved("Sessa")})
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        assert stats.rows_written == 1  # discogs only
        assert stats.musicbrainz_configured is False

    async def test_escalation_unavailable_writes_no_discogs_row(self):
        credits = [_credit(1, "Sessa", "Musica")]
        post_batch = _fake_post_batch({"Sessa": _unresolved("Sessa", "escalation_unavailable")})
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        pg.execute.assert_not_awaited()
        assert stats.rows_written == 0

    async def test_invalid_credit_never_reaches_http_and_still_writes_a_miss_row(self):
        credits = [_credit(1, "(2)", "Musica")]
        post_batch = _fake_post_batch({})
        pg = AsyncMock()
        pg.execute = AsyncMock(return_value="OK")

        stats = await run_backfill(
            credits=credits,
            post_batch=post_batch,
            mb_pg=None,
            pg=pg,
            dry_run_write=False,
            dry_run_http=True,
        )

        assert stats.rows_written == 1
        sql, *binds = pg.execute.await_args.args
        # binds: library_id, track_artist, track_title, source, external_id, ...
        assert binds[4] is None


@pytest.mark.asyncio
class TestSelectCreditPopulation:
    async def test_full_mode_selects_everything_for_both_sources(self):
        credits = [_credit(1, "Sessa", "Musica")]
        pg = AsyncMock()

        discogs_credits, mb_credits = await select_credit_population(
            pg, credits, mode="full", mb_configured=True
        )

        assert discogs_credits == credits
        assert mb_credits == credits
        pg.fetchall.assert_not_called()  # full mode never needs to read the table

    async def test_full_mode_skips_mb_when_unconfigured(self):
        credits = [_credit(1, "Sessa", "Musica")]
        pg = AsyncMock()

        _discogs_credits, mb_credits = await select_credit_population(
            pg, credits, mode="full", mb_configured=False
        )

        assert mb_credits == []

    async def test_incremental_excludes_already_attempted_discogs_credits(self):
        credits = [_credit(1, "Sessa", "Musica"), _credit(2, "Juana Molina", "La Paradoja")]
        pg = AsyncMock()
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "library_id": 1,
                    "track_artist": to_match_form("Sessa"),
                    "track_title": to_match_form("Musica"),
                }
            ]
        )

        discogs_credits, _mb = await select_credit_population(
            pg, credits, mode="incremental", mb_configured=False
        )

        assert discogs_credits == [credits[1]]

    async def test_incremental_selects_mb_leg_independently_of_discogs_attempt_state(self):
        # The plan's explicit slice-5 pin: a credit already resolved on
        # Discogs (in an earlier MB-unconfigured run) must still be selected
        # for the MB leg once DATABASE_URL_MUSICBRAINZ lands -- leaving it
        # out would make MB coverage unbackfillable forever.
        credits = [_credit(1, "Sessa", "Musica")]
        already_attempted_discogs_key = {
            "library_id": 1,
            "track_artist": to_match_form("Sessa"),
            "track_title": to_match_form("Musica"),
        }
        pg = AsyncMock()

        async def fetchall(sql, source):
            if source == "discogs":
                return [already_attempted_discogs_key]
            return []  # no musicbrainz row exists yet

        pg.fetchall = AsyncMock(side_effect=fetchall)

        discogs_credits, mb_credits = await select_credit_population(
            pg, credits, mode="incremental", mb_configured=True
        )

        assert discogs_credits == []  # already attempted -- skip re-asking Discogs
        assert mb_credits == credits  # never attempted on MB -- still a candidate

    async def test_retry_misses_selects_only_miss_rows(self):
        # The fake returns a FULL store row: the mode now drains the cohort
        # through get_compilation_track_identity_misses (the store helper is
        # the one definition of "miss"), not a script-local key query.
        credits = [_credit(1, "Sessa", "Musica"), _credit(2, "Juana Molina", "La Paradoja")]
        pg = AsyncMock()
        pg.fetchall = AsyncMock(
            return_value=[
                {
                    "library_id": 1,
                    "track_artist": to_match_form("Sessa"),
                    "track_title": to_match_form("Musica"),
                    "source": "discogs",
                    "external_id": None,
                    "confidence": None,
                    "method": None,
                    "resolved_artist_name": None,
                    "track_artist_raw": "Sessa",
                    "track_title_raw": "Musica",
                    "track_position": None,
                }
            ]
        )

        discogs_credits, _mb = await select_credit_population(
            pg, credits, mode="retry-misses", mb_configured=False
        )

        assert discogs_credits == [credits[0]]

    async def test_unknown_mode_raises(self):
        pg = AsyncMock()
        with pytest.raises(ValueError):
            await select_credit_population(pg, [], mode="bogus", mb_configured=False)
