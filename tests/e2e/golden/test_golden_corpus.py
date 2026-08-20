"""The LML#1233 Layer 3 golden corpus: per-commit regression attribution.

Layers 1 and 2 of `docs/plans/lml-1233-lookup-miss-monitoring.md` make a
production miss legible and alert when the attributable miss rate moves. Both
are lagging and confounded -- they answer *did something break*, hours later,
against a traffic mix that moves on its own. This tier answers *which change
broke it*, deterministically, before merge.

Each case freezes one query against a checked-in catalog and a checked-in
Discogs universe, and records the verdict LML produces today. A change that
moves a verdict fails the case that moved, and the failing case names the
commit. There is no base-commit re-run and no diff against production: the
expectation IS the baseline, which is what makes re-baselining an explicit,
reviewable commit rather than a silent drift.

See `docs/testing.md` ("Golden corpus") for the case format, the stratification
table, and the re-baselining workflow. The verdict vocabulary is
`lookup/miss_kind.py`'s -- deliberately shared with the Layer 1 telemetry, so a
production alert and a failing corpus case describe the same thing.

This module runs in the **default** pytest job (no marker, no service, no
network, no `library.db`), which is the whole point: a tier that needs
provisioning would not run per-PR.
"""

import json

import pytest

from tests.e2e.golden import corpus

# ---------------------------------------------------------------------------
# Corpus integrity -- the accessories every sweep-and-assert suite in this repo
# carries (see docs/testing.md, "Meta-test conventions"). A corpus that has
# quietly stopped covering a shape is worse than no corpus, because it still
# reports green.
# ---------------------------------------------------------------------------


def test_corpus_is_not_vacuous():
    """The corpus is loadable, non-trivial, and every declared stratum is populated.

    The vacuity guard. A loader that silently returned `[]` -- a bad path, a
    renamed key, a JSON file truncated by a bad merge -- would make every case
    below pass by not existing.
    """
    cases = corpus.load_cases()
    assert len(cases) >= corpus.MIN_CASES, (
        f"corpus has {len(cases)} cases, below the {corpus.MIN_CASES} floor. "
        "Deleting cases is allowed, but dropping below the floor means the "
        "stratification no longer covers production's query shapes."
    )
    by_shape = corpus.count_by_shape(cases)
    for shape, floor in corpus.SHAPE_FLOORS.items():
        assert by_shape.get(shape, 0) >= floor, (
            f"shape {shape!r} has {by_shape.get(shape, 0)} cases, below its {floor} floor. "
            f"Production shape mix: {corpus.SHAPE_FLOORS}"
        )


def test_case_ids_are_unique():
    """Duplicate ids would make one case's expectation shadow another's in a rebaseline."""
    ids = [c.id for c in corpus.load_cases()]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate case ids: {duplicates}"


def test_frozen_cases_cite_an_issue():
    """Every frozen case names the regression it pins.

    The exemption roster, inverted: frozen cases are the ones the rebaseline
    tool refuses to rewrite, so each has to justify that status by pointing at
    a specific historical failure rather than being frozen by habit.
    """
    for case in corpus.load_cases():
        if case.frozen:
            assert case.issue, f"{case.id} is frozen but cites no issue"
            assert case.note, f"{case.id} is frozen but has no note"


def test_frozen_cases_reference_seeded_rows():
    """A frozen case whose catalog rows are absent would pass vacuously.

    LML#801 turns on *Richard D. James Album* sitting eighth in an eighteen-row
    Aphex Twin shelf. Drop the shelf and the case still runs, still passes, and
    pins nothing at all. Each frozen case therefore declares the rows it needs
    and this asserts they are present in `library.json`.
    """
    seeded = {(r["artist"].lower(), r["title"].lower()) for r in corpus.load_library_rows()}
    for case in corpus.load_cases():
        for artist, title in case.requires_rows:
            assert (artist.lower(), title.lower()) in seeded, (
                f"{case.id} requires catalog row {artist!r} / {title!r}, which is not in "
                "library.json. The case would pass vacuously; re-run "
                "scripts/build_golden_corpus.py or fix the case."
            )


def test_no_stale_absent_artists():
    """Miss cases must query artists the catalog genuinely lacks.

    The reverse/stale check. A miss case whose artist got seeded later stops
    testing the false-positive direction and starts asserting that a real
    shelf is invisible -- a wrong expectation that would then be "fixed" by
    rebaselining the correct behavior away.
    """
    seeded = {r["artist"].lower() for r in corpus.load_library_rows()}
    for case in corpus.load_cases():
        for artist in case.requires_absent_artists:
            assert artist.lower() not in seeded, (
                f"{case.id} assumes {artist!r} is absent from the catalog, but it is seeded. "
                "The case no longer tests what it claims."
            )


def test_discogs_fixture_has_no_orphan_routes():
    """Every routed release id resolves to a release in the fixture.

    A route pointing at a deleted release silently degrades to "Discogs found
    nothing", which reads as a legitimate miss.
    """
    fixture = corpus.load_discogs_fixture()
    known = {r["release_id"] for r in fixture["releases"]}
    for route_kind in ("track_searches", "album_searches", "album_title_searches"):
        for key, release_ids in fixture.get(route_kind, {}).items():
            missing = [rid for rid in release_ids if rid not in known]
            assert not missing, f"{route_kind}[{key!r}] points at unknown releases {missing}"


def test_case_settings_name_real_flags():
    """A `settings` block naming a field `Settings` does not have does nothing.

    `pinned_environment` raises on an unknown key, so a typo would surface as
    a setup error on one case. Checking the whole corpus up front names every
    offender at once and keeps the failure legible after a `Settings` field is
    renamed.
    """
    from config.settings import Settings

    for case in corpus.load_cases():
        unknown = sorted(set(case.settings) - set(Settings.model_fields))
        assert not unknown, f"{case.id} overrides unknown Settings field(s): {unknown}"


def test_required_routes_exist_in_the_fixture():
    """A `requires_routes` entry must name a route the fixture actually has.

    The stale half of that guard: at run time the assertion is "this route
    served a candidate", which a route deleted from `discogs.json` also fails
    -- but only for the one case, and only with a runtime message. Checking the
    declaration against the fixture up front says *the corpus is inconsistent*
    rather than *this case regressed*.
    """
    fixture = corpus.load_discogs_fixture()
    known = set()
    for prefix, table in (
        ("track:", "track_searches"),
        ("search:", "album_searches"),
        ("album_title:", "album_title_searches"),
    ):
        known.update(f"{prefix}{key}" for key in fixture.get(table, {}))
    for case in corpus.load_cases():
        unknown = sorted(set(case.requires_routes) - known)
        assert not unknown, (
            f"{case.id} requires route(s) {unknown}, which are not in discogs.json. "
            "Re-run scripts/build_golden_corpus.py, or fix the case."
        )


def test_every_case_has_a_recorded_expectation():
    """`expect: null` is a build artifact, never a committed state.

    `scripts/build_golden_corpus.py` emits new cases with no expectation and
    `scripts/rebaseline_golden_corpus.py` fills them in. A null that reaches a
    commit is a half-finished regeneration, and without this guard it would
    read as a case that simply asserts nothing.
    """
    unrecorded = [case.id for case in corpus.load_cases() if case.expect is None]
    assert not unrecorded, (
        f"{len(unrecorded)} case(s) have no recorded verdict: {unrecorded}. "
        "Run `uv run python -m scripts.rebaseline_golden_corpus`."
    )


def test_suspect_cases_explain_themselves():
    """A case pinning behavior believed wrong has to say why, in the case.

    Otherwise the flag is an unexplained asterisk and the next reader has to
    guess whether a change to it is a fix or a regression.
    """
    for case in corpus.load_cases():
        if case.suspect:
            assert "SUSPECT" in case.note, (
                f"{case.id} is marked suspect but its note does not explain what is "
                "wrong with the recorded verdict"
            )


def test_expectations_are_well_formed():
    """Every expectation uses the verdict vocabulary and nothing else."""
    for case in corpus.load_cases():
        assert case.expect is not None, case.id
        assert case.expect.miss_kind in corpus.KNOWN_MISS_KINDS, (
            f"{case.id} expects miss_kind {case.expect.miss_kind!r}, "
            f"not one of {sorted(corpus.KNOWN_MISS_KINDS)}"
        )
        # Hit-vs-any-miss, not clean-vs-not-clean: `verdict_from_payload` derives
        # `miss_timeout` and `miss_degraded` from an empty `results` list too
        # (corpus.py), so a case expecting either of those with no results is
        # well-formed, not a contradiction (LML#1233 review).
        if case.expect.miss_kind == corpus.OUTCOME_HIT:
            assert case.expect.results, (
                f"{case.id} expects a hit but lists no results -- contradictory"
            )
        else:
            assert not case.expect.results, (
                f"{case.id} expects a miss but also lists results -- contradictory"
            )
        assert corpus.verdict_shape_is_consistent(case.expect), (
            f"{case.id}: verdict_shape_is_consistent disagrees with the check above"
        )
        assert len(case.expect.results) <= corpus.MAX_RECORDED_RESULTS, (
            f"{case.id} records {len(case.expect.results)} results, above the "
            f"{corpus.MAX_RECORDED_RESULTS} cap. The response cap moved, or the "
            "expectation was hand-edited."
        )


def test_cases_file_is_canonically_formatted():
    """`cases.json` round-trips through the writer the rebaseline tool uses.

    Keeps a hand-edit from producing a file whose next machine rewrite is a
    thousand-line whitespace diff that hides the one changed verdict.
    """
    raw = corpus.CASES_PATH.read_text(encoding="utf-8")
    assert raw == corpus.dump_cases(json.loads(raw)), (
        "tests/e2e/golden/cases.json is not canonically formatted. Run "
        "`uv run python -m scripts.rebaseline_golden_corpus --format-only`."
    )


def test_max_recorded_results_is_the_production_cap():
    """`MAX_RECORDED_RESULTS` must track the real cap, not a hand-copied literal.

    The two had already drifted -- `MAX_RECORDED_RESULTS` was 8, the real
    `/lookup` cap (`lookup.matching.MAX_SEARCH_RESULTS`) was 5 -- which means
    raising the real cap by up to 3 would have passed this corpus silently,
    the exact regression the guard exists to catch (LML#1233 review).
    """
    from lookup.matching import MAX_SEARCH_RESULTS

    assert corpus.MAX_RECORDED_RESULTS == MAX_SEARCH_RESULTS


@pytest.mark.parametrize(
    "miss_kind, results, expected",
    [
        pytest.param(corpus.OUTCOME_HIT, ("#1 A — B",), True, id="hit-with-results"),
        pytest.param(corpus.OUTCOME_HIT, (), False, id="hit-with-no-results-is-inconsistent"),
        pytest.param(corpus.MISS_CLEAN, (), True, id="clean-miss"),
        pytest.param(corpus.MISS_TIMEOUT, (), True, id="timeout-miss"),
        pytest.param(corpus.MISS_DEGRADED, (), True, id="degraded-miss"),
        pytest.param(
            corpus.MISS_TIMEOUT, ("#1 A — B",), False, id="timeout-with-results-is-inconsistent"
        ),
    ],
)
def test_verdict_shape_is_consistent_for_every_miss_kind(miss_kind, results, expected):
    """`results` is non-empty iff `miss_kind` is a hit -- for EVERY miss kind.

    A prior version of this invariant special-cased `miss_clean` only, so a
    `miss_timeout` or `miss_degraded` verdict with (correctly) no results read
    as contradictory -- the opposite of the truth, and the exact shape
    `verdict_from_payload` always produces for those two kinds (LML#1233
    review).
    """
    verdict = corpus.Verdict(
        miss_kind=miss_kind, song_not_found=False, found_on_compilation=False, results=results
    )
    assert corpus.verdict_shape_is_consistent(verdict) is expected


def test_pinned_environment_covers_non_settings_search_knobs():
    """The pipeline's non-`Settings` env knobs must be pinned, not inherited.

    `Settings` fields are pinned automatically; these are read straight off
    `os.environ` by `core.search.resolve_positive_int_env` and cousins, so
    they need an explicit entry in `corpus._PINNED_ENV`. Reproduction for the
    one that is provably verdict-relevant: `LML_SEARCH_BUDGET_MS=1` starves
    the strategy cascade and flips `lml1184-arabian-prince-strange-life` from
    a hit to a clean miss (LML#1233 review) -- run
    `LML_SEARCH_BUDGET_MS=1 uv run pytest tests/e2e/golden` to see it fail
    without this pin.
    """
    from lookup.admission import ADMISSION_LOOP_LAG_SHED_ENV_VAR, ADMISSION_SHED_ENFORCE_ENV_VAR
    from lookup.timeouts import (
        APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
        BANDCAMP_LIVE_PROBE_TIMEOUT_ENV_VAR,
    )

    env = corpus.pinned_environment()
    for name in (
        "LML_SEARCH_BUDGET_MS",
        "LML_SEARCH_HARD_TIMEOUT_MS",
        ADMISSION_LOOP_LAG_SHED_ENV_VAR,
        ADMISSION_SHED_ENFORCE_ENV_VAR,
        "LML_DISCOGS_POOL_MAX_SIZE",
        APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
        BANDCAMP_LIVE_PROBE_TIMEOUT_ENV_VAR,
    ):
        assert name in env, f"{name} is read at request time but not pinned by corpus.py"


@pytest.mark.asyncio
async def test_fake_discogs_service_search_respects_limit():
    """`FakeDiscogsService.search` must truncate to `limit`, like its siblings.

    Previously ignored the argument entirely and returned every routed
    candidate regardless of what the caller asked for -- inconsistent with
    `search_releases_by_track` and `search_releases_by_album_title`, which
    both apply `ids[:limit]`, and a silent divergence from the production
    `DiscogsService.search`, which truncates (LML#1233 review). Latent
    against the checked-in fixture (every route carries at most one id
    today), so this constructs its own multi-id route rather than relying on
    `discogs.json`.
    """
    from discogs.models import DiscogsSearchRequest

    fixture = corpus.load_discogs_fixture()
    releases = list(fixture["releases"])
    assert len(releases) >= 3, "fixture needs at least 3 releases to prove truncation"
    ids = [r["release_id"] for r in releases[:3]]
    fixture = {**fixture, "album_searches": {"multi|route": ids}}
    fake = corpus.FakeDiscogsService(fixture)

    response = await fake.search(DiscogsSearchRequest(artist="multi", album="route"), limit=2)

    assert len(response.results) == 2
    assert response.total == 3


def test_build_library_db_raises_on_a_row_missing_a_column(tmp_path):
    """A row missing an expected column must raise, not silently insert NULL.

    `row.get(col)` previously swallowed a missing key into a NULL cell, which
    would let a stale `library.json` (or a `LIBRARY_COLUMNS` widened without
    regenerating the fixture) record a corpus verdict against silently
    truncated catalog rows (LML#1233 review).
    """
    incomplete_row = {"id": 1, "title": "T", "artist": "A"}
    assert set(corpus.LIBRARY_COLUMNS) - set(incomplete_row), "fixture row must be incomplete"
    with pytest.raises(KeyError):
        corpus.build_library_db(tmp_path / "library.db", [incomplete_row])


# ---------------------------------------------------------------------------
# The corpus itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("case", corpus.load_cases(), ids=lambda c: c.id)
async def test_golden_case(case, golden_client, golden_discogs):
    """Run one frozen query and compare its verdict against the recorded one.

    The verdict is `miss_kind` + `song_not_found` + `found_on_compilation` +
    the ordered result identities. Not the full response: enrichment, artwork,
    streaming URLs, timings and `context_message` are all excluded, because a
    corpus that fails on an unrelated enrichment change trains everyone to
    rebaseline without reading. Not `miss_kind` alone either -- three of the
    four regressions this tier is calibrated against (LML#801, LML#717,
    LML#1184) return rows both before and after the fix and differ only in
    *which* rows, in *what order*.
    """
    response = await golden_client.post("/api/v1/lookup", json=case.request_body())
    assert response.status_code == 200, response.text

    actual = corpus.verdict_from_payload(response.json())
    missing_routes = sorted(set(case.requires_routes) - golden_discogs.served_keys)
    assert not missing_routes, (
        f"{case.id} requires fixture route(s) {missing_routes} to have served a "
        f"candidate, and they did not (served: {sorted(golden_discogs.served_keys)}). "
        "The route drifted, so the case is no longer exercising the path it names."
    )
    if case.requires_discogs:
        assert golden_discogs.served_candidates, (
            f"{case.id} declares requires_discogs but no Discogs search returned a "
            f"candidate (calls: {golden_discogs.calls}). A route key drifted, and the "
            "case is now measuring an empty-upstream fallback rather than what it claims."
        )
    assert actual == case.expect, corpus.explain_mismatch(case, actual)
