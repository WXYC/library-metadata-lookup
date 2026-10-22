"""Tests for ``core/observability.py`` -- the shared "observability must not
break the request path" helpers.

Pins the primitives this module provides:

- ``observability_guard`` -- swallow-and-warn context manager (the try/except
  boilerplate duplicated at ~30 sites).
- ``project_capped`` -- the "in-flight cap engaged" tag duplicated across
  ``lookup/router.py``, ``streaming/router.py``, and
  ``core/bulk_concurrency.py``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from wxyc_fastapi.testing import CountingPosthog

from config.settings import get_settings
from core import observability


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


class TestProjectCapped:
    """The in-flight-cap tag, parameterized by key."""

    def test_sets_only_the_tag(self):
        from core.observability import project_capped

        set_tag = Mock()
        with patch("core.observability.sentry_sdk.set_tag", set_tag):
            project_capped("lml.lookup.inflight_capped")

        set_tag.assert_called_once_with("lml.lookup.inflight_capped", "true")

    def test_does_not_swallow_exceptions(self):
        """``project_capped`` has no internal try/except -- callers compose it
        with ``observability_guard`` for that. Pins the composition contract.
        """
        from core.observability import project_capped

        with patch(
            "core.observability.sentry_sdk.set_tag", side_effect=RuntimeError("sdk exploded")
        ):
            with pytest.raises(RuntimeError):
                project_capped("some.tag")


class TestObservabilityGuardComposesWithProjectCapped:
    """Integration: the documented composition pattern call sites use."""

    def test_sdk_failure_inside_guarded_project_capped_is_swallowed(self):
        from core.observability import observability_guard, project_capped

        logger = Mock(spec=logging.Logger)
        with patch(
            "core.observability.sentry_sdk.set_tag", side_effect=RuntimeError("sdk exploded")
        ):
            with observability_guard("tag inflight_capped onto Sentry scope", logger):
                project_capped("some.tag")

        logger.warning.assert_called_once()
        args = logger.warning.call_args.args
        assert args[0] == "Failed to %s: %s"
        assert args[1] == "tag inflight_capped onto Sentry scope"


class TestSentryInitWiring:
    """``main.py`` must pin tracing off.

    ``wxyc_fastapi.init_sentry`` defaults ``traces_sample_rate`` to 1.0, so
    dropping the argument would silently turn tracing ON at 100% (BS#3393 D2).
    """

    def test_main_pins_traces_sample_rate_to_zero(self):
        import ast
        from pathlib import Path

        tree = ast.parse((Path(__file__).resolve().parents[2] / "main.py").read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "init_sentry"
        ]
        assert len(calls) == 1
        rates = [kw.value for kw in calls[0].keywords if kw.arg == "traces_sample_rate"]
        assert len(rates) == 1
        assert isinstance(rates[0], ast.Constant)
        assert rates[0].value == 0.0


class TestCaptureUnsampledCounter:
    """LML#1204 item 1: the detached-background-task PostHog counter shape
    previously copied byte-for-byte by ``discogs.service._capture_artist_breaker_shed``
    (LML#1049), ``discogs.ratelimit._capture_fail_open`` (LML#879),
    ``lookup.enrichment.bandcamp_probe._capture_shed``, the Wikipedia warm
    task's fetch-outcome counters (LML#513/#1192), and the two
    ``identity/router.py`` fail-open counters, consolidated into one shared
    helper. Per-site adoption behavior (which event fires on which trigger)
    stays pinned by each site's own tests. Uses the shared
    ``wxyc_fastapi.testing.CountingPosthog`` recording fake."""

    def _stub_settings(self, *, enable_telemetry: bool = True, environment: str = "unit-test-env"):
        return SimpleNamespace(enable_telemetry=enable_telemetry, environment=environment)

    def test_emits_with_service_distinct_id_prefix_and_merged_environment(self, monkeypatch):
        """One capture: the service-wide distinct id, the caller's event, and
        the caller's properties with ``environment`` merged in — the exact
        payload shape every pre-consolidation copy emitted."""
        fake = CountingPosthog()
        prefixes: list[str] = []

        def _accessor(event_prefix):
            prefixes.append(event_prefix)
            return fake

        monkeypatch.setattr(observability, "get_posthog_client", _accessor)
        observability.capture_unsampled_counter(
            "stereolab_probe",
            "stereolab_probe_shed",
            {"reason": "breaker_open"},
            settings=self._stub_settings(),
        )

        assert prefixes == ["stereolab_probe"]
        assert len(fake.captures) == 1
        captured = fake.captures[0]
        assert captured.distinct_id == observability.SERVICE_POSTHOG_DISTINCT_ID
        assert captured.event == "stereolab_probe_shed"
        assert captured.properties == {"reason": "breaker_open", "environment": "unit-test-env"}

    def test_omitted_properties_still_carry_environment(self, monkeypatch):
        """The wikipedia-warm shape: no caller properties at all, environment only."""
        fake = CountingPosthog()
        monkeypatch.setattr(observability, "get_posthog_client", lambda event_prefix: fake)
        observability.capture_unsampled_counter(
            "wikipedia_bio_warm", "wikipedia_bio_fetch_ok", settings=self._stub_settings()
        )

        assert fake.events == ["wikipedia_bio_fetch_ok"]
        assert fake.captures[0].properties == {"environment": "unit-test-env"}

    def test_disabled_telemetry_never_touches_the_accessor(self, monkeypatch):
        accessor = Mock()
        monkeypatch.setattr(observability, "get_posthog_client", accessor)
        observability.capture_unsampled_counter(
            "p", "e", settings=self._stub_settings(enable_telemetry=False)
        )
        accessor.assert_not_called()

    def test_none_client_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(observability, "get_posthog_client", lambda event_prefix: None)
        # Must simply return — nothing to assert beyond "no raise".
        observability.capture_unsampled_counter("p", "e", settings=self._stub_settings())

    def test_accessor_failure_is_swallowed_and_warned(self, monkeypatch, caplog):
        """Best-effort: a broken accessor logs the per-site message shape
        (``Failed to emit %s counter``) and never raises — a telemetry
        failure must not turn the event being counted into something worse."""

        def _exploding(event_prefix):
            raise RuntimeError("posthog accessor broken")

        monkeypatch.setattr(observability, "get_posthog_client", _exploding)
        with caplog.at_level(logging.WARNING, logger="core.observability"):
            observability.capture_unsampled_counter("p", "e", settings=self._stub_settings())

        assert any(
            record.getMessage() == "Failed to emit e counter" and record.exc_info
            for record in caplog.records
        )

    def test_settings_stub_missing_environment_still_emits(self, monkeypatch):
        """Duck-typing tolerance (LML#1204 review): the pre-consolidation
        bandcamp copy read both fields via getattr with defaults, so a
        settings-like stub missing ``environment`` must still emit (with
        ``environment: None``) rather than raise inside the guard and drop
        the counter with a spurious warning."""
        fake = CountingPosthog()
        monkeypatch.setattr(observability, "get_posthog_client", lambda event_prefix: fake)
        observability.capture_unsampled_counter(
            "p", "e", settings=SimpleNamespace(enable_telemetry=True)
        )

        assert fake.events == ["e"]
        assert fake.captures[0].properties == {"environment": None}

    def test_settings_stub_missing_enable_telemetry_skips(self, monkeypatch):
        """Same getattr default as the pre-consolidation copy: a stub with
        no ``enable_telemetry`` attribute reads as disabled, not an error."""
        accessor = Mock()
        monkeypatch.setattr(observability, "get_posthog_client", accessor)
        observability.capture_unsampled_counter("p", "e", settings=SimpleNamespace())
        accessor.assert_not_called()

    def test_omitted_settings_reads_the_cached_global_settings(self, monkeypatch):
        """Callers with no DI settings in hand (the discogs retry loop, the
        warm task) omit ``settings`` and get ``config.settings.get_settings()``."""
        monkeypatch.setenv("ENABLE_TELEMETRY", "true")
        monkeypatch.setenv("ENVIRONMENT", "unit-test-env")
        get_settings.cache_clear()
        fake = CountingPosthog()
        monkeypatch.setattr(observability, "get_posthog_client", lambda event_prefix: fake)
        try:
            observability.capture_unsampled_counter("p", "e")
        finally:
            get_settings.cache_clear()

        assert fake.events == ["e"]
        assert fake.captures[0].properties == {"environment": "unit-test-env"}
