"""Tests for the streaming-report stats regenerator."""

from __future__ import annotations

import pytest

from scripts.regenerate_report_stats import (
    UnknownMarkerError,
    UnresolvedMarkerError,
    substitute_markers,
)


class TestSubstituteMarkers:
    def test_replaces_value_between_marker_delimiters(self) -> None:
        text = "Total: <!-- gen:foo -->old<!-- /gen --> rows."
        result = substitute_markers(text, {"foo": "new"})
        assert result == "Total: <!-- gen:foo -->new<!-- /gen --> rows."

    def test_replaces_multiple_markers(self) -> None:
        text = "A=<!-- gen:a -->1<!-- /gen --> B=<!-- gen:b -->2<!-- /gen -->"
        result = substitute_markers(text, {"a": "10", "b": "20"})
        assert result == "A=<!-- gen:a -->10<!-- /gen --> B=<!-- gen:b -->20<!-- /gen -->"

    def test_returns_text_unchanged_when_no_markers(self) -> None:
        text = "No markers here."
        assert substitute_markers(text, {}) == text

    def test_handles_values_with_commas_and_percentages(self) -> None:
        text = "Coverage: <!-- gen:cov -->45,505 (77.3%)<!-- /gen -->"
        result = substitute_markers(text, {"cov": "45,494 (77.2%)"})
        assert result == "Coverage: <!-- gen:cov -->45,494 (77.2%)<!-- /gen -->"

    def test_raises_when_marker_key_not_in_results(self) -> None:
        text = "X=<!-- gen:missing -->?<!-- /gen -->"
        with pytest.raises(UnresolvedMarkerError, match="missing"):
            substitute_markers(text, {})

    def test_raises_when_results_contain_unknown_key(self) -> None:
        # Protects against typos: a registered key with no marker in the doc is a bug.
        text = "X=<!-- gen:foo -->1<!-- /gen -->"
        with pytest.raises(UnknownMarkerError, match="ghost"):
            substitute_markers(text, {"foo": "2", "ghost": "3"})

    def test_marker_value_may_contain_html_lookalike_text(self) -> None:
        text = "<!-- gen:k -->less <than> tag<!-- /gen -->"
        result = substitute_markers(text, {"k": "new"})
        assert result == "<!-- gen:k -->new<!-- /gen -->"
