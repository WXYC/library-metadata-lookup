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
from typing import Any
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


class TestDropFastPoolResetSpans:
    """The LML#1175 ``before_send_transaction`` filter.

    asyncpg issues one fixed statement every time a pooled connection is
    returned to the pool, and sentry-sdk's asyncpg instrumentation records
    each one as a ``db`` span. At 113,906/day that was 19% of the entire
    WXYC Sentry org's span consumption.

    The filter keeps resets at or above a 50ms floor: measured over 24h,
    only 35 of 113,906 (0.031%) cross it, and those are exactly the
    pool-contention fingerprint LML#803 relies on. Dropping only the fast
    ones sheds 99.97% of the volume while preserving the whole diagnostic
    tail.
    """

    RESET = "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;"

    @staticmethod
    def _span(description: str, duration_ms: float | None = 2.0, op: str = "db") -> dict[str, Any]:
        """Build a span dict shaped like ``sentry_sdk.tracing.Span.to_json()``.

        Note there is NO ``duration`` key -- the SDK emits ``start_timestamp``
        and ``timestamp`` as datetimes and leaves the subtraction to the
        consumer. ``TestPoolResetSpanShapeGuard`` pins that against the real SDK.
        """
        from datetime import UTC, datetime, timedelta

        span: dict = {"op": op, "description": description}
        if duration_ms is not None:
            start = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
            span["start_timestamp"] = start
            span["timestamp"] = start + timedelta(milliseconds=duration_ms)
        return span

    def test_drops_the_fast_reset_span_and_keeps_real_spans(self):
        """The headline acceptance criterion: reset span out, real spans and
        the transaction itself intact.
        """
        from core.observability import drop_fast_pool_reset_spans

        real_one = self._span("SELECT id FROM release WHERE id = $1")
        real_two = self._span("SELECT url FROM lml_cache.album_streaming_url_cache")
        event = {
            "type": "transaction",
            "transaction": "/api/v1/lookup",
            "spans": [real_one, self._span(self.RESET), real_two],
        }

        result = drop_fast_pool_reset_spans(event, None)

        assert result is not None, "the parent transaction is a real request; never drop it"
        assert result["spans"] == [real_one, real_two]
        assert result["transaction"] == "/api/v1/lookup"

    def test_returns_the_same_object_when_no_reset_spans_present(self):
        """Identity, not a rebuilt copy -- this runs on every transaction."""
        from core.observability import drop_fast_pool_reset_spans

        spans = [self._span("SELECT id FROM release WHERE id = $1")]
        event = {"type": "transaction", "spans": spans}

        result = drop_fast_pool_reset_spans(event, None)

        assert result is event
        assert result["spans"] is spans

    def test_keeps_a_slow_reset_span(self):
        """The 50ms floor. A slow reset is pool contention -- the LML#803
        fingerprint -- and is the entire reason this is a floor and not a
        flat drop.
        """
        from core.observability import drop_fast_pool_reset_spans

        slow = self._span(self.RESET, duration_ms=214.0)
        event = {"type": "transaction", "spans": [slow]}

        result = drop_fast_pool_reset_spans(event, None)

        assert result["spans"] == [slow]

    @pytest.mark.parametrize(
        ("duration_ms", "kept"),
        [
            (49.999, False),
            (50.0, True),
            (50.001, True),
        ],
    )
    def test_floor_boundary_is_inclusive(self, duration_ms: float, kept: bool):
        """``>= 50ms`` is kept, ``< 50ms`` is dropped. Pinned so a later
        refactor can't silently flip the comparison and take the tail with it.
        """
        from core.observability import drop_fast_pool_reset_spans

        span = self._span(self.RESET, duration_ms=duration_ms)
        event: Any = {"type": "transaction", "spans": [span]}

        result = drop_fast_pool_reset_spans(event, None)

        assert (result["spans"] == [span]) is kept

    @pytest.mark.parametrize(
        "description",
        [
            "SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL",
            "RESET ALL;",
            "SELECT foo FROM bar; RESET ALL;",
            "  SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;  ",
            "CLOSE ALL;",
        ],
    )
    def test_matches_the_statement_exactly_never_a_substring(self, description: str):
        """A genuine application statement that merely contains ``RESET ALL``
        (or is a near-miss of the pool reset) must survive. Matching loosely
        here would silently delete real query spans.
        """
        from core.observability import drop_fast_pool_reset_spans

        span = self._span(description)
        event: Any = {"type": "transaction", "spans": [span]}

        assert drop_fast_pool_reset_spans(event, None)["spans"] == [span]

    def test_keeps_a_reset_span_whose_duration_cannot_be_computed(self):
        """Fail open. An unclassifiable span costs one span of budget; dropping
        it would risk deleting the contention signal we just chose to keep.
        """
        from core.observability import drop_fast_pool_reset_spans

        span = self._span(self.RESET, duration_ms=None)
        event: Any = {"type": "transaction", "spans": [span]}

        assert drop_fast_pool_reset_spans(event, None)["spans"] == [span]

    def test_handles_epoch_float_timestamps(self):
        """Some SDK paths serialize timestamps as epoch floats rather than
        datetimes; both shapes must be measurable.
        """
        from core.observability import drop_fast_pool_reset_spans

        fast: dict[str, Any] = {
            "op": "db",
            "description": self.RESET,
            "start_timestamp": 1_754_838_000.000,
            "timestamp": 1_754_838_000.002,
        }
        slow: dict[str, Any] = {
            "op": "db",
            "description": self.RESET,
            "start_timestamp": 1_754_838_000.000,
            "timestamp": 1_754_838_000.200,
        }
        event: Any = {"type": "transaction", "spans": [fast, slow]}

        assert drop_fast_pool_reset_spans(event, None)["spans"] == [slow]

    @pytest.mark.parametrize("spans", [None, []], ids=["missing", "empty"])
    def test_transaction_without_spans_passes_through(self, spans):
        from core.observability import drop_fast_pool_reset_spans

        event: dict = {"type": "transaction"}
        if spans is not None:
            event["spans"] = spans

        assert drop_fast_pool_reset_spans(event, None) is event

    def test_trimmed_spans_sentinel_passes_through_without_warning(self):
        """When the SDK trims an oversized event it swaps the span list for an
        ``AnnotatedValue``, which is truthy but not iterable. That is a normal
        SDK state, not an error -- forward it silently rather than logging a
        warning on every trimmed transaction.
        """
        from sentry_sdk._types import AnnotatedValue

        from core.observability import drop_fast_pool_reset_spans

        event: Any = {"type": "transaction", "spans": AnnotatedValue(None, {"len": 1000})}

        with patch("core.observability.logger") as mock_logger:
            assert drop_fast_pool_reset_spans(event, None) is event

        mock_logger.warning.assert_not_called()

    def test_never_returns_none_even_on_a_malformed_event(self):
        """Returning ``None`` from ``before_send_transaction`` drops the whole
        transaction. A bug in this filter must cost spans, never requests.
        """
        from core.observability import drop_fast_pool_reset_spans

        event: Any = {"type": "transaction", "spans": ["not-a-span-dict"]}

        assert drop_fast_pool_reset_spans(event, None) is event

    def test_swallows_and_warns_when_filtering_raises(self):
        from core.observability import drop_fast_pool_reset_spans

        class Exploding(list):
            def __iter__(self):
                raise RuntimeError("sdk exploded")

        event = {"type": "transaction", "spans": Exploding([1])}

        with patch("core.observability.logger") as mock_logger:
            assert drop_fast_pool_reset_spans(event, None) is event

        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args.args[0] == "Failed to %s: %s"

    def test_hint_argument_is_optional(self):
        """sentry-sdk calls the hook as ``(event, hint)``; keeping ``hint``
        defaulted lets tests and any direct caller omit it.
        """
        from core.observability import drop_fast_pool_reset_spans

        event = {"type": "transaction", "spans": [self._span(self.RESET)]}

        assert drop_fast_pool_reset_spans(event)["spans"] == []


class TestPoolResetSpanShapeGuard:
    """Guards the filter against sentry-sdk changing its span serialization.

    Every assertion above builds span dicts by hand. If the SDK renamed
    ``description`` or started emitting a pre-computed duration, those tests
    would all still pass while the filter silently matched nothing in
    production -- the same class of no-op that nearly shipped in
    WXYC/Backend-Service#2089.
    """

    def test_real_sdk_span_json_is_recognized_and_dropped(self):
        from datetime import UTC, datetime, timedelta

        from sentry_sdk.tracing import Span

        from core.observability import ASYNCPG_POOL_RESET_STATEMENT, drop_fast_pool_reset_spans

        span = Span(op="db", description=ASYNCPG_POOL_RESET_STATEMENT)
        span.start_timestamp = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
        span.timestamp = span.start_timestamp + timedelta(milliseconds=2.3)
        serialized = span.to_json()

        assert serialized["description"] == ASYNCPG_POOL_RESET_STATEMENT
        assert "duration" not in serialized, (
            "the SDK now emits a duration -- simplify _span_duration_ms to read it"
        )

        event = {"type": "transaction", "spans": [serialized]}
        assert drop_fast_pool_reset_spans(event, None)["spans"] == []


class TestSentryInitWiring:
    """``main.py`` must actually hand the filter to ``init_sentry``.

    A filter nobody passes is a no-op that every unit test above still
    endorses, so the wiring gets its own assertion.
    """

    def test_main_passes_the_filter_as_before_send_transaction(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[2] / "main.py").read_text()

        assert "before_send_transaction=drop_fast_pool_reset_spans" in source

    def test_wxyc_fastapi_accepts_the_keyword(self):
        """Pins the LML#1175 dependency floor: the passthrough landed in
        wxyc-fastapi 1.5.0 (WXYC/wxyc-fastapi#40). An accidental downgrade
        below the pin fails here rather than at boot.
        """
        import inspect

        from wxyc_fastapi.observability import init_sentry

        parameter = inspect.signature(init_sentry).parameters["before_send_transaction"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
