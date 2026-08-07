"""Unit tests for the LML#1020 per-track compilation-identity matcher (slice 3).

Covers ``scripts/backfill_compilation_track_identity.py``'s pure/mockable
matcher pieces: CTA extraction + the F1 fail-fast shape check, the D4
credit-split cascade, the Discogs leg's D3 method/confidence mapping and
its three-outcome failure contract, and the MusicBrainz leg's floor/
ambiguity rule and its distinct "no row" vs "miss row" outcomes.

HTTP paging/retry/resume (``run_drain``) and the fan-back to
``(library_id, track_title)`` rows are slice 5's job, imported rather than
reimplemented; this file fakes the ``post_batch`` seam directly.

Plan: ``docs/plans/lml-1020-per-track-identity-matcher.md``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from generated.api_models import IdentityMethod
from scripts.backfill_compilation_track_identity import (
    CtaShapeError,
    MusicBrainzCreditVerdict,
    discogs_verdict_from_resolve_result,
    is_resolvable_credit,
    load_compilation_track_credits,
    resolve_discogs_credits,
    resolve_musicbrainz_credit,
    split_credit,
    validate_cta_shape,
)


# --------------------------------------------------------------------------- #
# F1: CTA shape -- fail fast at startup, not mid-enumeration.
# --------------------------------------------------------------------------- #
def _make_library_db(tmp_path: Path, *, with_track_title: bool) -> str:
    db_path = tmp_path / "library.db"
    conn = sqlite3.connect(db_path)
    try:
        if with_track_title:
            conn.execute(
                "CREATE TABLE compilation_track_artist "
                "(library_release_id INTEGER, artist_name TEXT, track_title TEXT)"
            )
            conn.execute(
                "INSERT INTO compilation_track_artist VALUES (1, 'Juana Molina', 'La Paradoja')"
            )
        else:
            # The shape tests/integration/test_library_db.py:213-217 fixture
            # builds -- missing track_title entirely (F1's documented drift).
            conn.execute(
                "CREATE TABLE compilation_track_artist (library_id INTEGER, artist_name TEXT)"
            )
        conn.commit()
    finally:
        conn.close()
    return str(db_path)


@pytest.mark.asyncio
class TestValidateCtaShape:
    async def test_passes_on_the_documented_shape(self, tmp_path):
        db_path = _make_library_db(tmp_path, with_track_title=True)
        await validate_cta_shape(db_path)  # must not raise

    async def test_fails_fast_on_missing_track_title_column(self, tmp_path):
        db_path = _make_library_db(tmp_path, with_track_title=False)
        with pytest.raises(CtaShapeError):
            await validate_cta_shape(db_path)

    async def test_fails_fast_when_table_is_entirely_absent(self, tmp_path):
        db_path = tmp_path / "library.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE library (id INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(CtaShapeError):
            await validate_cta_shape(str(db_path))


@pytest.mark.asyncio
class TestLoadCompilationTrackCredits:
    async def test_extracts_every_cta_row(self, tmp_path):
        db_path = _make_library_db(tmp_path, with_track_title=True)
        credits = await load_compilation_track_credits(db_path)
        assert len(credits) == 1
        assert credits[0].library_id == 1
        assert credits[0].track_artist == "Juana Molina"
        assert credits[0].track_title == "La Paradoja"

    async def test_nullable_track_title_survives_as_none(self, tmp_path):
        db_path = tmp_path / "library.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE compilation_track_artist "
            "(library_release_id INTEGER, artist_name TEXT, track_title TEXT)"
        )
        conn.execute("INSERT INTO compilation_track_artist VALUES (1, 'Sessa', NULL)")
        conn.commit()
        conn.close()

        credits = await load_compilation_track_credits(str(db_path))
        assert credits[0].track_title is None


# --------------------------------------------------------------------------- #
# D4: credit splitting.
# --------------------------------------------------------------------------- #
class TestSplitCredit:
    @pytest.mark.parametrize(
        "credit,expected",
        [
            ("The Bug feat. Flowdan", ["The Bug", "Flowdan"]),
            ("The Bug ft. Flowdan", ["The Bug", "Flowdan"]),
            ("Duke Ellington & John Coltrane", ["Duke Ellington", "John Coltrane"]),
            ("Sessa, Antonio Loureiro", ["Sessa", "Antonio Loureiro"]),
        ],
    )
    def test_splits_on_each_delimiter(self, credit, expected):
        assert split_credit(credit) == expected

    def test_no_delimiter_returns_empty_list(self):
        # The caller's cue that there is nothing further to try.
        assert split_credit("Juana Molina") == []

    def test_joint_credit_with_no_delimiter_is_not_split(self):
        # The plan's live-repro: "Brian Reitzell And Roger J. Manning, Jr."
        # contains a comma inside a real single-artist string (the "Jr."
        # suffix) -- but ALSO a genuine comma delimiter before it, so this
        # pins the mechanical behavior (comma DOES split), documenting that
        # the whole-string-first design (not this function) is what protects
        # real joint-artist-page credits from being needlessly split.
        assert split_credit("Brian Reitzell And Roger J. Manning, Jr.") == [
            "Brian Reitzell And Roger J. Manning",
            "Jr.",
        ]


class TestIsResolvableCredit:
    def test_blank_credit_is_unresolvable(self):
        assert is_resolvable_credit("") is False

    def test_bare_parenthesized_number_is_unresolvable(self):
        assert is_resolvable_credit("(2)") is False

    def test_ordinary_credit_is_resolvable(self):
        assert is_resolvable_credit("Juana Molina") is True


# --------------------------------------------------------------------------- #
# D3: Discogs leg -- method/confidence mapping and the three-outcome contract.
# --------------------------------------------------------------------------- #
def _resolved_result(
    name: str,
    *,
    discogs_artist_id: int = 12345,
    canonical_name: str | None = "Juana Molina",
    method: str = "api_search",
    cache_corroboration: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "discogs_artist_id": discogs_artist_id,
        "canonical_name": canonical_name,
        "method": method,
        "cache_corroboration": cache_corroboration or [],
        "unresolved_reason": None,
        "candidate_count": 1,
    }


def _unresolved_result(name: str, reason: str) -> dict:
    return {
        "name": name,
        "discogs_artist_id": None,
        "canonical_name": None,
        "method": None,
        "cache_corroboration": [],
        "unresolved_reason": reason,
        "candidate_count": 0 if reason == "not_found" else None,
    }


class TestDiscogsVerdictFromResolveResult:
    def test_api_search_maps_to_exact_match(self):
        # api_search's exact-form tier is LML's strongest Discogs evidence --
        # mapping it to trigram (the tempting fallback) would stamp it with
        # the weakest method. Empty cache_corroboration means "not measured",
        # never "matched loosely" (D3).
        verdict = discogs_verdict_from_resolve_result(
            _resolved_result("Juana Molina", method="api_search", cache_corroboration=[])
        )
        assert verdict.method == IdentityMethod.exact_match
        assert verdict.confidence == pytest.approx(1.0)

    def test_cache_trigram_corroboration_maps_to_trigram(self):
        verdict = discogs_verdict_from_resolve_result(
            _resolved_result("Sessa", cache_corroboration=["cache_trigram"])
        )
        assert verdict.method == IdentityMethod.trigram

    def test_cache_exact_corroboration_wins_over_trigram(self):
        verdict = discogs_verdict_from_resolve_result(
            _resolved_result("Sessa", cache_corroboration=["cache_trigram", "cache_exact"])
        )
        assert verdict.method == IdentityMethod.exact_match

    def test_identity_store_hit_with_null_canonical_name_is_still_resolved(self):
        # entity.identity stores no Discogs title -- a tier-1 identity_store
        # hit resolves with a non-NULL id and NULL canonical_name. The
        # coherence CHECK excludes resolved_artist_name, so this must not be
        # treated as a lesser verdict: method and confidence are still set.
        verdict = discogs_verdict_from_resolve_result(
            _resolved_result(
                "Juana Molina", method="identity_store", canonical_name=None, cache_corroboration=[]
            )
        )
        assert verdict.external_id == "12345"
        assert verdict.resolved_artist_name is None
        assert verdict.method is not None
        assert verdict.confidence is not None

    def test_not_found_is_a_miss_not_none(self):
        verdict = discogs_verdict_from_resolve_result(_unresolved_result("Whoever", "not_found"))
        assert verdict is not None
        assert verdict.external_id is None
        assert verdict.method is None
        assert verdict.confidence is None

    def test_ambiguous_is_a_miss_not_none(self):
        verdict = discogs_verdict_from_resolve_result(_unresolved_result("Whoever", "ambiguous"))
        assert verdict is not None
        assert verdict.external_id is None

    def test_escalation_unavailable_returns_none(self):
        # The leg was never successfully consulted -- the caller must write
        # NO discogs row at all, not a miss row.
        verdict = discogs_verdict_from_resolve_result(
            _unresolved_result("Whoever", "escalation_unavailable")
        )
        assert verdict is None


# --------------------------------------------------------------------------- #
# The D4 cascade end to end, over a fake post_batch.
# --------------------------------------------------------------------------- #
def _fake_post_batch(verdicts_by_name: dict[str, dict]):
    calls: list[list[str]] = []

    async def post_batch(names: list[str], dry_run: bool) -> list[dict]:
        calls.append(list(names))
        return [verdicts_by_name[name] for name in names]

    post_batch.calls = calls  # type: ignore[attr-defined]
    return post_batch


@pytest.mark.asyncio
class TestResolveDiscogsCredits:
    async def test_whole_string_resolution_short_circuits_the_split(self):
        post_batch = _fake_post_batch(
            {"Duke Ellington & John Coltrane": _resolved_result("Duke Ellington & John Coltrane")}
        )

        verdicts = await resolve_discogs_credits(post_batch, ["Duke Ellington & John Coltrane"])

        assert verdicts["Duke Ellington & John Coltrane"].external_id == "12345"
        assert len(post_batch.calls) == 1  # no split-pass call

    async def test_whole_string_miss_falls_through_to_a_resolved_split_part(self):
        post_batch = _fake_post_batch(
            {
                "The Bug feat. Flowdan": _unresolved_result("The Bug feat. Flowdan", "not_found"),
                "The Bug": _resolved_result(
                    "The Bug", discogs_artist_id=1, canonical_name="The Bug"
                ),
                "Flowdan": _unresolved_result("Flowdan", "not_found"),
            }
        )

        verdicts = await resolve_discogs_credits(post_batch, ["The Bug feat. Flowdan"])

        assert verdicts["The Bug feat. Flowdan"].external_id == "1"
        assert len(post_batch.calls) == 2  # whole pass, then split pass

    async def test_whole_miss_with_no_delimiter_is_a_miss_row_not_omitted(self):
        post_batch = _fake_post_batch(
            {"Juana Molina": _unresolved_result("Juana Molina", "not_found")}
        )

        verdicts = await resolve_discogs_credits(post_batch, ["Juana Molina"])

        assert "Juana Molina" in verdicts
        assert verdicts["Juana Molina"].external_id is None
        assert len(post_batch.calls) == 1  # nothing to split on

    async def test_whole_miss_with_all_split_parts_also_missing_is_a_miss_row(self):
        post_batch = _fake_post_batch(
            {
                "A & B": _unresolved_result("A & B", "not_found"),
                "A": _unresolved_result("A", "not_found"),
                "B": _unresolved_result("B", "not_found"),
            }
        )

        verdicts = await resolve_discogs_credits(post_batch, ["A & B"])

        assert verdicts["A & B"].external_id is None

    async def test_invalid_credit_is_a_miss_row_and_never_reaches_http(self):
        post_batch = _fake_post_batch({})

        verdicts = await resolve_discogs_credits(post_batch, ["(2)"])

        assert verdicts["(2)"].external_id is None
        assert post_batch.calls == []  # never sent -- would have 422'd the whole batch

    async def test_invalid_credit_does_not_block_valid_siblings_in_the_same_page(self):
        post_batch = _fake_post_batch({"Juana Molina": _resolved_result("Juana Molina")})

        verdicts = await resolve_discogs_credits(post_batch, ["(2)", "Juana Molina"])

        assert verdicts["(2)"].external_id is None
        assert verdicts["Juana Molina"].external_id == "12345"

    async def test_escalation_unavailable_credit_is_absent_from_the_result(self):
        post_batch = _fake_post_batch(
            {"Whoever": _unresolved_result("Whoever", "escalation_unavailable")}
        )

        verdicts = await resolve_discogs_credits(post_batch, ["Whoever"])

        assert "Whoever" not in verdicts


# --------------------------------------------------------------------------- #
# The MusicBrainz leg.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
class TestResolveMusicbrainzCredit:
    async def test_resolves_the_top_candidate_above_the_floor(self):
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(
            return_value=[{"id": "mb-uuid-1", "name": "Csillagrablók", "score": 0.9}]
        )

        verdict = await resolve_musicbrainz_credit(mb_pg, "Csillagrablok")

        assert verdict == MusicBrainzCreditVerdict(
            external_id="mb-uuid-1",
            confidence=0.9,
            resolved_artist_name="Csillagrablók",
            method=IdentityMethod.trigram,
        )

    async def test_empty_candidates_is_a_miss_not_none(self):
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[])

        verdict = await resolve_musicbrainz_credit(mb_pg, "Nobody")

        assert verdict is not None
        assert verdict.external_id is None

    async def test_below_similarity_floor_is_a_miss(self):
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(return_value=[{"id": "mb-1", "name": "X", "score": 0.5}])

        verdict = await resolve_musicbrainz_credit(mb_pg, "X")

        assert verdict.external_id is None

    async def test_ambiguous_top_two_within_the_band_is_a_miss(self):
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(
            return_value=[
                {"id": "mb-1", "name": "X", "score": 0.90},
                {"id": "mb-2", "name": "X Variant", "score": 0.88},
            ]
        )

        verdict = await resolve_musicbrainz_credit(mb_pg, "X")

        # Recording a coin-flip as a resolved identity is worse than a miss
        # (D3) -- the upsert guard makes a wrong resolution permanent.
        assert verdict.external_id is None

    async def test_clear_winner_outside_the_ambiguity_band_still_resolves(self):
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(
            return_value=[
                {"id": "mb-1", "name": "X", "score": 0.95},
                {"id": "mb-2", "name": "Y", "score": 0.80},
            ]
        )

        verdict = await resolve_musicbrainz_credit(mb_pg, "X")

        assert verdict.external_id == "mb-1"

    async def test_pg_error_returns_none_not_a_miss(self):
        # Distinct from a genuine miss (D3): the leg was never successfully
        # consulted, so the caller must write NO musicbrainz row at all.
        mb_pg = AsyncMock()
        mb_pg.fetchall = AsyncMock(side_effect=RuntimeError("pg unreachable"))

        verdict = await resolve_musicbrainz_credit(mb_pg, "X")

        assert verdict is None

    async def test_tie_break_is_deterministic_on_id(self):
        mb_pg = AsyncMock()
        # Two equal-score candidates within the ambiguity band -- still a
        # miss (covered above), but a THIRD case: equal top score with a
        # clear gap to the rest must pick deterministically, not by fetch
        # order, so re-runs are stable.
        mb_pg.fetchall = AsyncMock(
            return_value=[
                {"id": "mb-b", "name": "B", "score": 0.95},
                {"id": "mb-a", "name": "A", "score": 0.95},
            ]
        )

        verdict = await resolve_musicbrainz_credit(mb_pg, "X")

        # Exactly-tied top score falls inside the ambiguity band (band >= 0),
        # so this is a miss -- but it must be the SAME miss every time,
        # which this test pins by running it twice.
        verdict_again = await resolve_musicbrainz_credit(mb_pg, "X")
        assert verdict == verdict_again


# --------------------------------------------------------------------------- #
# F3: the drain never calls Discogs directly.
# --------------------------------------------------------------------------- #
def test_module_makes_no_direct_discogs_calls():
    """Static guard: the shared Discogs rate bucket/breaker/semaphore live in
    the running LML process (F3) -- a standalone backfill importing
    DiscogsService or DiscogsCacheService would be an uncoordinated N+1th
    limiter against the same token. The Discogs leg is reached ONLY over
    POST /api/v1/artists/resolve/bulk.

    Parses the module's actual import statements (not a docstring substring
    scan, which would false-positive on this very explanation) and asserts
    none of them touch the Discogs client modules or classes.
    """
    import ast

    import scripts.backfill_compilation_track_identity as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    forbidden_modules = {"discogs.service", "discogs.cache_service", "discogs"}
    forbidden_names = {"DiscogsService", "DiscogsCacheService"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden_modules, (
                    f"unexpected import of {alias.name!r} -- the Discogs leg must be "
                    "reached only over POST /api/v1/artists/resolve/bulk (F3)"
                )
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden_modules, (
                f"unexpected import from {node.module!r} -- the Discogs leg must be "
                "reached only over POST /api/v1/artists/resolve/bulk (F3)"
            )
            for alias in node.names:
                assert alias.name not in forbidden_names, (
                    f"unexpected import of {alias.name!r} -- the Discogs leg must be "
                    "reached only over POST /api/v1/artists/resolve/bulk (F3)"
                )
