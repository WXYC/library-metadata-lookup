"""Unit tests for core/telemetry.py."""

import pytest

from core.telemetry import (
    RequestTelemetry,
    get_cache_stats,
    init_cache_stats,
    record_api_time,
    record_discogs_api_call,
    record_memory_cache_hit,
    record_pg_cache_hit,
    record_pg_cache_miss,
    record_pg_time,
)

# ---------------------------------------------------------------------------
# RequestTelemetry
# ---------------------------------------------------------------------------


class TestRequestTelemetry:
    def test_track_step_records_duration(self):
        t = RequestTelemetry()
        with t.track_step("test_step"):
            pass
        assert "test_step" in t.steps
        assert t.steps["test_step"].duration_ms >= 0
        assert t.steps["test_step"].success is True

    def test_track_step_records_exception(self):
        t = RequestTelemetry()
        with pytest.raises(ValueError):
            with t.track_step("failing_step"):
                raise ValueError("boom")
        assert t.steps["failing_step"].success is False
        assert t.steps["failing_step"].error_type == "ValueError"

    def test_record_api_call_known_service(self):
        t = RequestTelemetry()
        t.record_api_call("discogs")
        assert t.api_calls["discogs"] == 1
        t.record_api_call("discogs")
        assert t.api_calls["discogs"] == 2

    def test_record_api_call_unknown_service(self):
        t = RequestTelemetry()
        t.record_api_call("unknown_service")
        assert "unknown_service" not in t.api_calls

    def test_get_total_duration_ms(self):
        t = RequestTelemetry()
        duration = t.get_total_duration_ms()
        assert duration >= 0

    def test_get_step_timings(self):
        t = RequestTelemetry()
        with t.track_step("step_a"):
            pass
        with t.track_step("step_b"):
            pass
        timings = t.get_step_timings()
        assert "step_a_ms" in timings
        assert "step_b_ms" in timings

    def test_send_to_posthog_step_events(self, mock_posthog_client):
        t = RequestTelemetry()
        with t.track_step("my_step"):
            pass
        t.send_to_posthog(mock_posthog_client)

        calls = mock_posthog_client.capture.call_args_list
        step_call = calls[0]
        assert step_call[1]["event"] == "lookup_my_step"
        assert step_call[1]["properties"]["step"] == "my_step"

    def test_send_to_posthog_summary_event(self, mock_posthog_client):
        t = RequestTelemetry()
        with t.track_step("s"):
            pass
        t.send_to_posthog(mock_posthog_client, {"extra": "data"})

        calls = mock_posthog_client.capture.call_args_list
        summary_call = calls[-1]
        assert summary_call[1]["event"] == "lookup_completed"
        assert "extra" in summary_call[1]["properties"]

    def test_send_to_posthog_with_cache_stats(self, mock_posthog_client):
        init_cache_stats()
        record_memory_cache_hit()

        t = RequestTelemetry()
        with t.track_step("s"):
            pass
        t.send_to_posthog(mock_posthog_client)

        calls = mock_posthog_client.capture.call_args_list
        summary_props = calls[-1][1]["properties"]
        assert summary_props["cache"]["memory_hits"] == 1

    def test_send_to_posthog_without_cache_stats(self, mock_posthog_client):
        # Ensure no cache stats initialized (ContextVar default)
        t = RequestTelemetry()
        with t.track_step("s"):
            pass
        t.send_to_posthog(mock_posthog_client)

        calls = mock_posthog_client.capture.call_args_list
        summary_props = calls[-1][1]["properties"]
        assert summary_props["cache"]["memory_hits"] == 0


# ---------------------------------------------------------------------------
# ContextVar cache stats
# ---------------------------------------------------------------------------


class TestCacheStats:
    def test_get_cache_stats_before_init(self):
        assert get_cache_stats() is None

    def test_init_cache_stats(self):
        init_cache_stats()
        stats = get_cache_stats()
        assert stats is not None
        assert stats["memory_hits"] == 0
        assert stats["pg_hits"] == 0
        assert stats["pg_misses"] == 0
        assert stats["api_calls"] == 0

    def test_record_memory_cache_hit(self):
        init_cache_stats()
        record_memory_cache_hit()
        record_memory_cache_hit()
        assert get_cache_stats()["memory_hits"] == 2

    def test_record_pg_cache_hit(self):
        init_cache_stats()
        record_pg_cache_hit()
        assert get_cache_stats()["pg_hits"] == 1

    def test_record_pg_cache_miss(self):
        init_cache_stats()
        record_pg_cache_miss()
        assert get_cache_stats()["pg_misses"] == 1

    def test_record_discogs_api_call(self):
        init_cache_stats()
        record_discogs_api_call()
        assert get_cache_stats()["api_calls"] == 1

    def test_record_pg_time(self):
        init_cache_stats()
        record_pg_time(5.0)
        record_pg_time(3.0)
        assert get_cache_stats()["pg_time_ms"] == 8.0

    def test_record_api_time(self):
        init_cache_stats()
        record_api_time(10.0)
        assert get_cache_stats()["api_time_ms"] == 10.0

    def test_record_functions_noop_without_init(self):
        """Record functions should be no-ops when stats not initialized."""
        # These should not raise
        record_memory_cache_hit()
        record_pg_cache_hit()
        record_pg_cache_miss()
        record_discogs_api_call()
        record_pg_time(1.0)
        record_api_time(1.0)
