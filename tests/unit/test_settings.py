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
