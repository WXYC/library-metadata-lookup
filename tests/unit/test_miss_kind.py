"""Unit tests for the ``lookup.miss_kind`` outcome taxonomy (LML#1233).

The taxonomy exists to make a lookup miss *attributable*. Production
telemetry carried only ``results_count``, so a recall regression and a
Discogs outage produced an identical zero-result series -- see
``docs/plans/lml-1233-lookup-miss-monitoring.md`` Finding 2. ``miss_kind``
separates them using fields already on ``LookupResponse``, and only
``miss_clean`` is attributable to the matching algorithm.

The precedence assertions below are the load-bearing ones: an
infrastructure cause must always win over the residual bucket, or
``miss_clean`` silently absorbs sheds and the Layer 2 alert starts paging
for upstream outages -- the exact inversion the plan exists to prevent.
"""

from __future__ import annotations

import pytest

from generated.api_models import DegradedReason
from lookup.miss_kind import (
    MISS_CLEAN,
    MISS_DEGRADED,
    MISS_TIMEOUT,
    OUTCOME_HIT,
    derive_miss_kind,
    miss_telemetry_properties,
)
from lookup.models import LookupResponse, LookupResultItem
from tests.factories import make_catalog_item, make_match_result


def _resp(
    *,
    results_count: int = 0,
    timeout: bool = False,
    degraded: bool = False,
    degraded_reason: DegradedReason | None = None,
    song_not_found: bool = False,
    found_on_compilation: bool = False,
) -> LookupResponse:
    """Build a real ``LookupResponse`` carrying just the fields under test.

    A hand-rolled stub was tried first and rejected: it drifted the moment
    ``miss_telemetry_properties`` began reading two more fields than
    ``derive_miss_kind``, and it diverged from the real type under mypy.
    Constructing the genuine model costs a factory call and cannot go stale.

    ``results`` only needs to be truthy for the taxonomy, but the rows are
    well-formed anyway so the model validates the way production does.
    """
    return LookupResponse(
        results=[
            LookupResultItem(
                library_item=make_catalog_item(id=i + 1),
                artwork=make_match_result(release_id=1000 + i),
            )
            for i in range(results_count)
        ],
        search_type="direct",
        timeout=timeout,
        degraded=degraded,
        degraded_reason=degraded_reason,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
    )


class TestTaxonomyRows:
    """One case per row of the plan's taxonomy table."""

    def test_results_present_is_a_hit(self) -> None:
        assert derive_miss_kind(_resp(results_count=3)) == OUTCOME_HIT

    def test_empty_and_undegraded_is_miss_clean(self) -> None:
        """The only algorithm-attributable outcome.

        Matches the ``degraded`` field's own documented contract: a genuine
        no-match is empty ``results`` WITH ``degraded: false`` and
        ``timeout: false``.
        """
        assert derive_miss_kind(_resp(results_count=0)) == MISS_CLEAN

    def test_empty_and_timed_out_is_miss_timeout(self) -> None:
        assert derive_miss_kind(_resp(results_count=0, timeout=True)) == MISS_TIMEOUT

    def test_empty_and_degraded_is_miss_degraded(self) -> None:
        assert (
            derive_miss_kind(
                _resp(
                    results_count=0,
                    degraded=True,
                    degraded_reason=DegradedReason.upstream_unavailable,
                )
            )
            == MISS_DEGRADED
        )


class TestPrecedence:
    """Infrastructure causes outrank the residual bucket, always."""

    def test_timeout_outranks_degraded(self) -> None:
        """Both flags set -> ``miss_timeout``.

        ``_build_timed_out_response`` is not the only producer of a
        timed-out response; a spine-deadline trip can coincide with a shed
        tail. Whichever wins, it must not be ``miss_clean``.
        """
        assert (
            derive_miss_kind(
                _resp(
                    results_count=0,
                    timeout=True,
                    degraded=True,
                    degraded_reason=DegradedReason.deadline_exceeded,
                )
            )
            == MISS_TIMEOUT
        )

    @pytest.mark.parametrize(
        "timeout,degraded",
        [(True, False), (False, True), (True, True)],
        ids=["timeout", "degraded", "both"],
    )
    def test_a_signalled_infrastructure_outcome_is_never_miss_clean(
        self, timeout: bool, degraded: bool
    ) -> None:
        """Whenever a flag IS set, the outcome leaves the attributable bucket.

        Scope note, so this is not misread as stronger than it is: this varies
        the two flags, not the upstream failures that ought to set them. It
        cannot prove "no infrastructure-caused empty response is ever
        ``miss_clean``" -- and that stronger claim is currently FALSE, because
        a non-breaker Discogs failure sets neither flag (LML#1236; see the
        module docstring's "Known gap"). Closing that gap is a change to the
        response, not to this function, so the test that proves it belongs
        with that fix.
        """
        assert (
            derive_miss_kind(_resp(results_count=0, timeout=timeout, degraded=degraded))
            != MISS_CLEAN
        )

    def test_results_present_outranks_both_flags(self) -> None:
        """A degraded response that still returned rows is not a miss.

        ``degraded`` means the enrichment tail was shed, not that the search
        failed -- the returned data is trustworthy but incomplete. Counting
        it as a miss would inflate the numerator every time a caller budget
        got tight.
        """
        assert derive_miss_kind(_resp(results_count=2, timeout=True, degraded=True)) == OUTCOME_HIT


class TestVocabulary:
    """The emitted strings are a telemetry contract; pin them."""

    def test_values_are_the_documented_strings(self) -> None:
        """PostHog insights and the runbook filter on these literals.

        Renaming one silently breaks the Layer 2 alert's ``WHERE`` clause
        while every test still passes, so the strings are pinned here rather
        than only flowing through as constants.
        """
        assert (OUTCOME_HIT, MISS_CLEAN, MISS_TIMEOUT, MISS_DEGRADED) == (
            "hit",
            "miss_clean",
            "miss_timeout",
            "miss_degraded",
        )


class TestTelemetryProperties:
    """The emit-site fragment, so `lookup/router.py` stays one line wider.

    `miss_telemetry_properties` exists because the router sits at its
    module-budget ceiling: building this dict inline pushed it over, and this
    repo's policy is to extract rather than raise the ceiling. Testing the
    fragment directly (not only through the HTTP emit-site tests) keeps the
    key names pinned in one place -- they are a telemetry contract that
    PostHog insights filter on.
    """

    def test_returns_exactly_the_seven_documented_keys(self) -> None:
        props = miss_telemetry_properties(_resp(results_count=0), commit_sha="abc")
        assert set(props) == {
            "miss_kind",
            "timeout",
            "degraded",
            "degraded_reason",
            "song_not_found",
            "found_on_compilation",
            "commit_sha",
        }

    def test_degraded_reason_is_the_wire_string(self) -> None:
        """An insight filters on `'cache_only'`, not on an enum repr."""
        props = miss_telemetry_properties(
            _resp(
                results_count=0,
                degraded=True,
                degraded_reason=DegradedReason.cache_only,
            ),
            commit_sha=None,
        )
        assert props["degraded_reason"] == "cache_only"

    def test_degraded_reason_none_when_unset(self) -> None:
        props = miss_telemetry_properties(_resp(results_count=1), commit_sha=None)
        assert props["degraded_reason"] is None

    def test_commit_sha_key_present_even_when_none(self) -> None:
        """A key that appears only on some deploys makes the breakdown unreadable."""
        props = miss_telemetry_properties(_resp(results_count=1), commit_sha=None)
        assert "commit_sha" in props and props["commit_sha"] is None
