"""Unit tests for config/settings.py."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from config import settings as settings_module
from config.settings import Settings, get_settings


@pytest.fixture
def reset_empty_path_warning(monkeypatch):
    """Clear the once-per-process guard on the empty-path fallback WARNING.

    The guard is module state, so without this the test that asserts the
    warning fires would depend on which test in the session reached the
    fallback branch first.
    """
    monkeypatch.setattr(settings_module, "_empty_library_db_path_warned", False)


class TestResolvedLibraryDbPath:
    def test_default_path(self):
        s = Settings(library_db_path=Path("library.db"))
        assert s.resolved_library_db_path == Path("library.db")

    def test_dot_path_resolves_to_default(self):
        s = Settings(library_db_path=Path("."))
        assert s.resolved_library_db_path == Path("library.db")

    def test_valid_custom_path(self):
        s = Settings(library_db_path=Path("/data/my_library.db"))
        assert s.resolved_library_db_path == Path("/data/my_library.db")

    def test_empty_value_fallback_logs_warning(self, caplog, reset_empty_path_warning):
        """LML#1132: the set-but-empty-env-var fallback (`.` after pydantic's
        Path coercion) must warn loudly rather than silently serve a
        different catalog than the one an operator thinks they configured."""
        s = Settings(library_db_path=Path("."))
        with caplog.at_level("WARNING", logger="config.settings"):
            result = s.resolved_library_db_path
        assert result == Path("library.db")
        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any("library_db_path" in m.lower() or "library.db" in m for m in warnings), (
            f"expected a WARNING naming the fallback path, got: {warnings}"
        )

    def test_empty_value_fallback_warns_once_per_process(self, caplog, reset_empty_path_warning):
        """The fallback branch is reached from ``get_library_db``, which is a
        per-request FastAPI dependency (``core/dependencies.py``), plus the
        boot-fetch (``main.py``) and ``get_object_store``. An unguarded
        ``logger.warning`` there emits one identical line **per request** for
        the whole life of a misconfigured deploy -- burying the signal it
        exists to raise. Measured against the built image before this guard:
        6 lines for 5 ``GET /health`` calls. Warn once per process instead."""
        s = Settings(library_db_path=Path("."))
        with caplog.at_level("WARNING", logger="config.settings"):
            for _ in range(5):
                assert s.resolved_library_db_path == Path("library.db")
        warnings = [r for r in caplog.records if r.name == "config.settings"]
        assert len(warnings) == 1, (
            f"expected exactly one WARNING across 5 property reads, got {len(warnings)}: "
            f"{[r.getMessage() for r in warnings]}"
        )

    def test_valid_custom_path_does_not_warn(self, caplog):
        """The warning is scoped to the fallback branch only -- a real,
        non-empty path must never trigger it."""
        s = Settings(library_db_path=Path("/data/my_library.db"))
        with caplog.at_level("WARNING", logger="config.settings"):
            _ = s.resolved_library_db_path
        assert not caplog.records


class TestStreamingUrlPersistFlags:
    """LML#573 AND-gate: a service persists only when the master AND its
    per-service flag are both true. Master defaults True, per-service default
    False, so the feature is OFF out of the box (Railway supplies the
    per-service True values)."""

    def test_master_defaults_true(self):
        assert Settings().lml_persist_streaming_urls is True

    def test_per_service_flags_default_false(self):
        s = Settings()
        assert s.lml_persist_streaming_url_apple_music is False
        assert s.lml_persist_streaming_url_spotify is False

    def test_master_true_per_service_false_means_feature_off(self):
        # The subtle AND-gate: master defaulting True alone does NOT enable
        # any service. Both gates must be open.
        s = Settings()
        assert s.lml_persist_streaming_urls is True
        assert s.lml_persist_streaming_url_apple_music is False
        assert s.lml_persist_streaming_url_spotify is False


class TestResolveCompilationReleaseFlag:
    """LML#604: gate the artwork binding step's lazy release-resolution
    fallback (and carried-release trust-and-bind) behind a default-off flag so
    flag-off behavior stays byte-identical to pre-PR2."""

    def test_defaults_false(self):
        assert Settings().lml_resolve_compilation_release is False


class TestResolveNonlibraryReleaseFlag:
    """LML#628: gate the A1 row-less carry-through (TRACK_ON_COMPILATION,
    SONG_AS_TRACK, SWAPPED_INTERPRETATION surface a resolvable non-library
    release as LibraryItem(id=0)) behind a default-off flag so flag-off behavior
    stays today's empty/sentinel."""

    def test_defaults_false(self):
        assert Settings().lml_resolve_nonlibrary_release is False

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("LML_RESOLVE_NONLIBRARY_RELEASE", "true")
        assert Settings().lml_resolve_nonlibrary_release is True


class TestDiscogsBreakerTrialWatchdogMultiplier:
    """LML#787 review: the HALF_OPEN watchdog multiplier follows the three
    sibling breaker knobs into pydantic-settings, so operators can retune the
    watchdog window during an incident without a code deploy."""

    def test_defaults_to_20(self):
        assert Settings().discogs_breaker_trial_watchdog_multiplier == 20.0

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("DISCOGS_BREAKER_TRIAL_WATCHDOG_MULTIPLIER", "10.5")
        assert Settings().discogs_breaker_trial_watchdog_multiplier == 10.5


class TestBucketAddressingStyle:
    """LML#834: S3 addressing is configurable and defaults to virtual-hosted
    style — Railway Buckets' current default (only legacy buckets need
    path-style). Operators set LML_BUCKET_ADDRESSING_STYLE=path for a legacy
    bucket. The value is fed to botocore's ``Config(s3={"addressing_style": ...})``,
    so it is validated at load against the three values botocore accepts."""

    def test_defaults_to_virtual(self):
        assert Settings().lml_bucket_addressing_style == "virtual"

    def test_reads_env(self, monkeypatch):
        monkeypatch.setenv("LML_BUCKET_ADDRESSING_STYLE", "path")
        assert Settings().lml_bucket_addressing_style == "path"

    def test_accepts_auto(self):
        assert Settings(lml_bucket_addressing_style="auto").lml_bucket_addressing_style == "auto"

    def test_rejects_invalid_value(self):
        with pytest.raises(ValidationError):
            Settings(lml_bucket_addressing_style="bogus")


class TestDiscogsRateLimitBounds:
    """LML#841 review (Finding 4): ``discogs_rate_limit`` seeds the shared token
    bucket's ``refill_per_sec`` (rate / 60). A zero would make the bucket's
    ``retry_after_s = (1 - avail) / refill_per_sec`` divide by zero, so the knob
    is bounded ``ge=1`` at the real config surface."""

    def test_defaults_to_50(self):
        assert Settings().discogs_rate_limit == 50

    def test_rejects_zero(self):
        with pytest.raises(ValidationError):
            Settings(discogs_rate_limit=0)

    def test_rejects_negative(self):
        with pytest.raises(ValidationError):
            Settings(discogs_rate_limit=-5)


class TestDiscogsRateBucketTimeoutBounds:
    """LML#841 review (round 2, Finding 4): ``discogs_rate_bucket_timeout_s`` is the
    per-round-trip ``asyncio.wait_for`` deadline on a single bucket UPDATE. A value of
    ``0`` times out every acquire immediately and silently fails the gate open on every
    call — a footgun distinct from the intended ``discogs_rate_bucket_enabled=false``
    kill switch — so the knob is bounded ``gt=0`` rather than ``ge=0``."""

    def test_defaults_to_half_second(self):
        assert Settings().discogs_rate_bucket_timeout_s == 0.5

    def test_rejects_zero(self):
        with pytest.raises(ValidationError):
            Settings(discogs_rate_bucket_timeout_s=0)

    def test_rejects_negative(self):
        with pytest.raises(ValidationError):
            Settings(discogs_rate_bucket_timeout_s=-0.1)

    def test_accepts_small_positive(self):
        assert Settings(discogs_rate_bucket_timeout_s=0.01).discogs_rate_bucket_timeout_s == 0.01


class TestGetSettings:
    def test_returns_settings_instance(self):
        get_settings.cache_clear()
        s = get_settings()
        assert isinstance(s, Settings)

    def test_caches_result(self):
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
        get_settings.cache_clear()
