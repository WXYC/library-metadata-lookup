"""Tests for LIBRARY_CODE matching."""

import pytest

from scripts.va_disambiguate.library_lookup import (
    build_library_code_index,
    find_library_code_id,
)


class TestBuildLibraryCodeIndex:
    def test_builds_normalized_mapping(self):
        codes = [
            {"id": 1, "presentation_name": "Stereolab", "call_letters": "S-"},
            {"id": 2, "presentation_name": "Cat Power", "call_letters": "C-"},
        ]
        index = build_library_code_index(codes)
        assert index["stereolab"] == 1
        assert index["cat power"] == 2

    def test_skips_compilations(self):
        codes = [
            {"id": 1, "presentation_name": "various", "call_letters": "Z--"},
            {"id": 2, "presentation_name": "Stereolab", "call_letters": "S-"},
        ]
        index = build_library_code_index(codes)
        assert "various" not in index
        assert "stereolab" in index

    def test_empty_input(self):
        assert build_library_code_index([]) == {}


class TestFindLibraryCodeId:
    @pytest.fixture
    def index(self):
        codes = [
            {"id": 10, "presentation_name": "Koo Nimo", "call_letters": "K-"},
            {"id": 20, "presentation_name": "Stereolab", "call_letters": "S-"},
            {"id": 30, "presentation_name": "Juana Molina", "call_letters": "J-"},
            {"id": 40, "presentation_name": "Cat Power", "call_letters": "C-"},
            {"id": 50, "presentation_name": "Sessa", "call_letters": "S-"},
        ]
        return build_library_code_index(codes)

    def test_exact_match(self, index):
        assert find_library_code_id(index, "Koo Nimo") == 10

    def test_case_insensitive(self, index):
        assert find_library_code_id(index, "koo nimo") == 10

    def test_strips_discogs_suffix(self, index):
        assert find_library_code_id(index, "Koo Nimo (2)") == 10

    def test_no_match(self, index):
        assert find_library_code_id(index, "Unknown Artist") is None

    def test_fuzzy_match(self, index):
        # Close enough to "Stereolab" (typo)
        assert find_library_code_id(index, "Stereolap") == 20

    def test_rejects_weak_fuzzy(self, index):
        # Too different from anything in the index
        assert find_library_code_id(index, "Radiohead") is None
