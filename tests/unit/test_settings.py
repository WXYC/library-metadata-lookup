"""Unit tests for config/settings.py."""

from pathlib import Path

from config.settings import Settings, get_settings


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
