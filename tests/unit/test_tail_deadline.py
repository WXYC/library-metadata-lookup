"""Unit tests for lookup/tail_deadline.py — the caller-deadline enrichment-tail
shed policy (LML#930).

The deadline arithmetic itself lives in ``test_spine_deadline.py``
(``SpineDeadline.tail_exhausted``); these tests pin the policy layered on top:
``should_shed_tail`` maps an exhausted deadline to the degraded reason (and emits
the two-channel telemetry), and never lets an observability failure break the
request path.
"""

import time
from unittest.mock import MagicMock, patch

from generated.api_models import DegradedReason
from lookup.spine_deadline import LIMIT_CALLER_BUDGET, SpineDeadline
from lookup.tail_deadline import project_tail_shed_telemetry, should_shed_tail


def _fresh_deadline(budget_ms: int = 4000) -> SpineDeadline:
    return SpineDeadline(
        start=time.monotonic(),
        hard_cap_ms=25000,
        caller_budget_ms=budget_ms,
        effective_budget_ms=budget_ms - 200,
    )


def _exhausted_deadline() -> SpineDeadline:
    return SpineDeadline(
        start=time.monotonic() - 10.0,  # long past any caller budget
        hard_cap_ms=25000,
        caller_budget_ms=500,
        effective_budget_ms=300,
    )


class TestShouldShedTail:
    def test_budget_remaining_returns_none(self):
        assert should_shed_tail(_fresh_deadline(), "fetch_artwork", "raw msg") is None

    def test_exhausted_returns_deadline_exceeded(self):
        assert (
            should_shed_tail(_exhausted_deadline(), "fetch_artwork", "raw msg")
            == DegradedReason.deadline_exceeded
        )

    def test_exhausted_emits_step_limit_tags_and_measurements(self):
        txn = MagicMock()
        with (
            patch("lookup.tail_deadline.sentry_sdk.set_tag") as set_tag,
            patch(
                "lookup.tail_deadline.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=txn),
            ),
        ):
            should_shed_tail(_exhausted_deadline(), "enrich_metadata", "raw msg")
        tag_names = {c.args[0] for c in set_tag.call_args_list}
        assert "lml.deadline.tail_shed_step" in tag_names
        assert "lml.deadline.tail_shed_limit" in tag_names
        measurement_names = {c.args[0] for c in txn.set_measurement.call_args_list}
        assert "lml.deadline.tail_remaining_ms" in measurement_names
        assert "lml.deadline.tail_shed_ms" in measurement_names

    def test_not_exhausted_emits_no_telemetry(self):
        with patch("lookup.tail_deadline.project_tail_shed_telemetry") as project:
            should_shed_tail(_fresh_deadline(), "fetch_artwork", "raw msg")
        project.assert_not_called()


class TestProjectTailShedTelemetry:
    def test_no_active_transaction_is_noop(self):
        """No transaction → measurements skipped (tags still attempted), no raise."""
        with (
            patch("lookup.tail_deadline.sentry_sdk.set_tag"),
            patch(
                "lookup.tail_deadline.sentry_sdk.get_current_scope",
                return_value=MagicMock(transaction=None),
            ),
        ):
            project_tail_shed_telemetry(
                "fetch_artwork", -5.0, LIMIT_CALLER_BUDGET, _exhausted_deadline()
            )

    def test_swallows_sentry_errors(self):
        """Observability must never break /lookup — an SDK error is logged, not raised."""
        with patch("lookup.tail_deadline.sentry_sdk.set_tag", side_effect=RuntimeError("boom")):
            project_tail_shed_telemetry(
                "fetch_artwork", -5.0, LIMIT_CALLER_BUDGET, _exhausted_deadline()
            )
