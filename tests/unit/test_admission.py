"""Unit tests for lookup/admission.py — the LML#930 PR2 load-adaptive
admission shed for low-priority lookup enrichment.

Covers the pure predicate + env resolvers (Phase 1), the telemetry projection
(Phase 2), and the orchestration function (Phase 3). Router wiring
(``record_low_priority_tag`` + the two handler call sites) lives in
``tests/unit/test_traffic_class_observability.py`` — the home of the sibling
endpoint_family/caller_reason wiring tests. Orchestrator gate integration and
the admission-control AC test live in ``tests/unit/test_orchestrator.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

from generated.api_models import DegradedReason
from lookup.admission import (
    ADMISSION_LOOP_LAG_SHED_ENV_VAR,
    ADMISSION_SHED_ENFORCE_ENV_VAR,
    ADMISSION_WOULD_SHED_STAT_KEY,
    DEFAULT_ADMISSION_LOOP_LAG_SHED_MS,
    evaluate_admission_shed,
    is_shed_enforced,
    project_admission_shed_telemetry,
    resolve_shed_threshold_ms,
    should_shed_low_priority_tail,
)

# ---------------------------------------------------------------------------
# Phase 1 — pure predicate + resolvers
# ---------------------------------------------------------------------------


class TestShouldShedLowPriorityTail:
    def test_low_priority_and_lag_over_threshold_sheds(self):
        assert should_shed_low_priority_tail(True, 600.0, 500) is True

    def test_interactive_never_sheds_even_at_high_lag(self):
        assert should_shed_low_priority_tail(False, 9000.0, 500) is False

    def test_lag_none_never_sheds(self):
        assert should_shed_low_priority_tail(True, None, 500) is False

    def test_lag_at_or_below_threshold_does_not_shed(self):
        assert should_shed_low_priority_tail(True, 500.0, 500) is False
        assert should_shed_low_priority_tail(True, 100.0, 500) is False


class TestResolveShedThresholdMs:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv(ADMISSION_LOOP_LAG_SHED_ENV_VAR, raising=False)
        assert resolve_shed_threshold_ms() == DEFAULT_ADMISSION_LOOP_LAG_SHED_MS

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(ADMISSION_LOOP_LAG_SHED_ENV_VAR, "1200")
        assert resolve_shed_threshold_ms() == 1200

    def test_junk_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(ADMISSION_LOOP_LAG_SHED_ENV_VAR, "not-a-number")
        assert resolve_shed_threshold_ms() == DEFAULT_ADMISSION_LOOP_LAG_SHED_MS

    def test_non_positive_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(ADMISSION_LOOP_LAG_SHED_ENV_VAR, "0")
        assert resolve_shed_threshold_ms() == DEFAULT_ADMISSION_LOOP_LAG_SHED_MS


class TestIsShedEnforced:
    def test_default_off_when_unset(self, monkeypatch):
        monkeypatch.delenv(ADMISSION_SHED_ENFORCE_ENV_VAR, raising=False)
        assert is_shed_enforced() is False

    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES", "on", " on "])
    def test_true_flag_values_enable(self, monkeypatch, value):
        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, value)
        assert is_shed_enforced() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "disabled", "garbage", ""])
    def test_everything_else_stays_off(self, monkeypatch, value):
        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, value)
        assert is_shed_enforced() is False


# ---------------------------------------------------------------------------
# Phase 2 — telemetry projection
# ---------------------------------------------------------------------------


class TestProjectAdmissionShedTelemetry:
    def test_sets_tags_and_measurement_on_would_shed(self):
        txn = MagicMock()
        with (
            patch("lookup.admission.sentry_sdk.set_tag") as set_tag,
            patch(
                "lookup.admission.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=txn),
            ),
        ):
            project_admission_shed_telemetry(enforced=True, lag_ms=734.5)

        tags = {c.args[0]: c.args[1] for c in set_tag.call_args_list}
        assert tags["lml.admission.would_shed"] == "true"
        assert tags["lml.admission.enforced"] == "true"
        txn.set_measurement.assert_called_once_with("lml.admission.loop_lag_ms", 734.5)

    def test_enforced_false_tags_correctly(self):
        with (
            patch("lookup.admission.sentry_sdk.set_tag") as set_tag,
            patch(
                "lookup.admission.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=None),
            ),
        ):
            project_admission_shed_telemetry(enforced=False, lag_ms=600.0)

        tags = {c.args[0]: c.args[1] for c in set_tag.call_args_list}
        assert tags["lml.admission.enforced"] == "false"

    def test_bumps_would_shed_cache_stats_counter(self):
        init_cache_stats(extra_keys=(ADMISSION_WOULD_SHED_STAT_KEY,))
        with (
            patch("lookup.admission.sentry_sdk.set_tag"),
            patch(
                "lookup.admission.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=None),
            ),
        ):
            project_admission_shed_telemetry(enforced=False, lag_ms=600.0)

        assert get_cache_stats()[ADMISSION_WOULD_SHED_STAT_KEY] == 1

    def test_swallows_sentry_errors_and_still_records_cache_stats(self):
        """Observability must never break /lookup — an injected Sentry SDK
        error must not prevent the independently-best-effort cache-stats
        counter from recording."""
        init_cache_stats(extra_keys=(ADMISSION_WOULD_SHED_STAT_KEY,))
        with patch("lookup.admission.sentry_sdk.set_tag", side_effect=RuntimeError("boom")):
            project_admission_shed_telemetry(enforced=False, lag_ms=600.0)  # must not raise

        assert get_cache_stats()[ADMISSION_WOULD_SHED_STAT_KEY] == 1

    def test_swallows_cache_stats_recorder_errors(self):
        """The inverse: a broken cache-stats recorder must not prevent the
        Sentry tags from being attempted."""
        with (
            patch("lookup.admission.sentry_sdk.set_tag") as set_tag,
            patch(
                "lookup.admission.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=None),
            ),
            patch(
                "lookup.admission.get_cache_stats_recorder",
                side_effect=RuntimeError("recorder boom"),
            ),
        ):
            project_admission_shed_telemetry(enforced=True, lag_ms=600.0)  # must not raise

        assert ("lml.admission.would_shed", "true") in [c.args for c in set_tag.call_args_list]


# ---------------------------------------------------------------------------
# Phase 3 — evaluate_admission_shed orchestration
# ---------------------------------------------------------------------------


class TestEvaluateAdmissionShed:
    @pytest.fixture(autouse=True)
    def _threshold(self, monkeypatch):
        monkeypatch.setenv(ADMISSION_LOOP_LAG_SHED_ENV_VAR, "500")

    def test_would_shed_and_enforce_returns_cache_only_and_tags_enforced_true(self, monkeypatch):
        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, "true")
        with patch("lookup.admission.project_admission_shed_telemetry") as project:
            result = evaluate_admission_shed(low_priority=True, lag_ms=900.0)

        assert result == DegradedReason.cache_only
        project.assert_called_once_with(enforced=True, lag_ms=900.0)

    def test_would_shed_and_shadow_returns_none_and_tags_enforced_false(self, monkeypatch):
        monkeypatch.delenv(ADMISSION_SHED_ENFORCE_ENV_VAR, raising=False)
        with patch("lookup.admission.project_admission_shed_telemetry") as project:
            result = evaluate_admission_shed(low_priority=True, lag_ms=900.0)

        assert result is None
        project.assert_called_once_with(enforced=False, lag_ms=900.0)

    @pytest.mark.parametrize(
        "low_priority,lag_ms",
        [
            (False, 9000.0),  # interactive traffic, however saturated
            (True, None),  # no gauge sample
            (True, 500.0),  # at the threshold, not over it
            (True, 10.0),  # well under the threshold
        ],
        ids=["interactive", "unsampled", "at-threshold", "under-threshold"],
    )
    def test_predicate_false_returns_none_with_no_telemetry(
        self, monkeypatch, low_priority, lag_ms
    ):
        monkeypatch.setenv(ADMISSION_SHED_ENFORCE_ENV_VAR, "true")
        with patch("lookup.admission.project_admission_shed_telemetry") as project:
            result = evaluate_admission_shed(low_priority=low_priority, lag_ms=lag_ms)

        assert result is None
        project.assert_not_called()
