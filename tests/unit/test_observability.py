"""Tests for ``core/observability.py`` -- the shared "observability must not
break the request path" helpers.

Pins the three primitives this module introduces:

- ``observability_guard`` -- swallow-and-warn context manager (the try/except
  boilerplate duplicated at ~30 sites).
- ``project_transaction`` -- attach a dict of fields onto the active Sentry
  transaction, mirroring ``core.search._log_hard_cap_fired`` /
  ``_log_search_budget_exceeded``'s bodies.
- ``project_capped`` -- the "in-flight cap engaged" tag+measurement pair
  duplicated across ``lookup/router.py``, ``streaming/router.py``, and
  ``core/bulk_concurrency.py``.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock, patch

import pytest


class TestObservabilityGuard:
    """The shared try/except-and-warn context manager."""

    def test_reraises_nothing_on_success(self):
        from core.observability import observability_guard

        logger = Mock(spec=logging.Logger)
        ran = False
        with observability_guard("do the thing", logger):
            ran = True
        assert ran is True
        logger.warning.assert_not_called()

    def test_swallows_exception_and_warns_with_exact_message_shape(self):
        """Pins the message shape several call sites' tests assert on:
        ``logger.warning("Failed to %s: %s", label, e)`` -- NOT an f-string,
        so the label and the exception are separate ``%s`` args.
        """
        from core.observability import observability_guard

        logger = Mock(spec=logging.Logger)
        boom = RuntimeError("boom")
        with observability_guard("project widgets onto Sentry", logger):
            raise boom

        logger.warning.assert_called_once_with(
            "Failed to %s: %s", "project widgets onto Sentry", boom
        )

    def test_only_swallows_exception_not_other_baseexceptions(self):
        """``except Exception`` -- a KeyboardInterrupt/SystemExit must still propagate.

        Observability guards protect the request path from SDK/telemetry bugs,
        not from process-level control flow.
        """
        from core.observability import observability_guard

        logger = Mock(spec=logging.Logger)
        with pytest.raises(SystemExit):
            with observability_guard("do the thing", logger):
                raise SystemExit(1)
        logger.warning.assert_not_called()

    def test_usable_as_a_decorator(self):
        """``contextlib.contextmanager`` results are also ``ContextDecorator``s."""
        from core.observability import observability_guard

        logger = Mock(spec=logging.Logger)

        @observability_guard("decorate the thing", logger)
        def raises():
            raise RuntimeError("boom")

        raises()  # must not raise
        logger.warning.assert_called_once()


class TestProjectTransaction:
    """``set_data`` (+ optional ``set_measurement``) fan-out onto the active transaction."""

    def test_noop_when_no_active_transaction(self):
        from core.observability import project_transaction

        mock_scope = Mock()
        mock_scope.transaction = None
        with patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope):
            # Must not raise.
            project_transaction({"hard_cap_fired": True})

    def test_sets_data_for_every_entry_by_default(self):
        """Mirrors ``_log_hard_cap_fired`` / ``_log_search_budget_exceeded``'s
        bodies: every entry gets ``set_data``, including non-numeric values
        (bools, lists) -- there is no numeric filter unless ``measurements=True``.
        """
        from core.observability import project_transaction

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction
        data = {"hard_cap_fired": True, "hard_cap_skipped_strategies": ["a", "b"]}

        with patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope):
            project_transaction(data)

        calls = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert calls == data
        mock_transaction.set_measurement.assert_not_called()

    def test_prefix_is_prepended_to_every_key(self):
        from core.observability import project_transaction

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope):
            project_transaction({"api_calls": 2}, prefix="lml.cache.")

        mock_transaction.set_data.assert_called_once_with("lml.cache.api_calls", 2)

    def test_measurements_true_also_sets_measurement_for_numeric_entries(self):
        from core.observability import project_transaction

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction
        data = {"api_calls": 2, "pg_time_ms": 12.5, "weird_string": "nope"}

        with patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope):
            project_transaction(data, measurements=True)

        measured = {c.args[0]: c.args[1] for c in mock_transaction.set_measurement.call_args_list}
        assert measured == {"api_calls": 2, "pg_time_ms": 12.5}
        # Non-numeric entries still get set_data even though they're not measured.
        projected = {c.args[0]: c.args[1] for c in mock_transaction.set_data.call_args_list}
        assert projected == data

    def test_measurements_true_excludes_bool_despite_being_an_int_subclass(self):
        from core.observability import project_transaction

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction

        with patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope):
            project_transaction({"flag": True}, measurements=True)

        mock_transaction.set_measurement.assert_not_called()
        mock_transaction.set_data.assert_called_once_with("flag", True)


class TestProjectCapped:
    """The in-flight-cap tag+measurement pair, parameterized by key."""

    def test_noop_measurement_when_no_active_transaction(self):
        from core.observability import project_capped

        mock_scope = Mock()
        mock_scope.transaction = None
        set_tag = Mock()
        with (
            patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope),
            patch("core.observability.sentry_sdk.set_tag", set_tag),
        ):
            # Must not raise even with no active transaction.
            project_capped("lml.lookup.inflight_capped", "lml.lookup.inflight_wait_ms", 42.0)

        set_tag.assert_called_once_with("lml.lookup.inflight_capped", "true")

    def test_sets_tag_and_measurement_and_data_by_default(self):
        """Default shape matches ``lookup.router``'s original
        ``_project_inflight_capped`` -- tag + measurement + set_data.
        """
        from core.observability import project_capped

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction
        set_tag = Mock()

        with (
            patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope),
            patch("core.observability.sentry_sdk.set_tag", set_tag),
        ):
            project_capped("lml.lookup.inflight_capped", "lml.lookup.inflight_wait_ms", 123.5)

        set_tag.assert_called_once_with("lml.lookup.inflight_capped", "true")
        mock_transaction.set_measurement.assert_called_once_with(
            "lml.lookup.inflight_wait_ms", 123.5
        )
        mock_transaction.set_data.assert_called_once_with("lml.lookup.inflight_wait_ms", 123.5)

    def test_also_set_data_false_skips_set_data(self):
        """Matches ``streaming.router``'s original ``_project_inflight_capped``,
        which only sets the measurement, not ``set_data``.
        """
        from core.observability import project_capped

        mock_transaction = Mock()
        mock_scope = Mock()
        mock_scope.transaction = mock_transaction
        set_tag = Mock()

        with (
            patch("core.observability.sentry_sdk.get_current_scope", return_value=mock_scope),
            patch("core.observability.sentry_sdk.set_tag", set_tag),
        ):
            project_capped(
                "lml.streaming_check.inflight_capped",
                "lml.streaming_check.inflight_wait_ms",
                77.0,
                also_set_data=False,
            )

        mock_transaction.set_measurement.assert_called_once_with(
            "lml.streaming_check.inflight_wait_ms", 77.0
        )
        mock_transaction.set_data.assert_not_called()

    def test_does_not_swallow_exceptions(self):
        """``project_capped`` has no internal try/except -- callers compose it
        with ``observability_guard`` for that. Pins the composition contract.
        """
        from core.observability import project_capped

        with patch(
            "core.observability.sentry_sdk.set_tag", side_effect=RuntimeError("sdk exploded")
        ):
            with pytest.raises(RuntimeError):
                project_capped("some.tag", "some.measurement", 1.0)


class TestObservabilityGuardComposesWithProjectCapped:
    """Integration: the documented composition pattern call sites use."""

    def test_sdk_failure_inside_guarded_project_capped_is_swallowed(self):
        from core.observability import observability_guard, project_capped

        logger = Mock(spec=logging.Logger)
        with patch(
            "core.observability.sentry_sdk.set_tag", side_effect=RuntimeError("sdk exploded")
        ):
            with observability_guard("project inflight_capped onto Sentry transaction", logger):
                project_capped("some.tag", "some.measurement", 1.0)

        logger.warning.assert_called_once()
        args = logger.warning.call_args.args
        assert args[0] == "Failed to %s: %s"
        assert args[1] == "project inflight_capped onto Sentry transaction"
