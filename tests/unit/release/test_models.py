"""Tests for release/models.py — the request validator's oneof rule is the
only thing worth exercising here; the rest is plain Pydantic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from release.models import ReleaseResolveRequest


class TestReleaseResolveRequestValidation:
    def test_accepts_url_only(self):
        ReleaseResolveRequest(url="https://www.discogs.com/release/1")

    def test_accepts_explicit_source_and_id(self):
        ReleaseResolveRequest(source="discogs_release", id="123")

    def test_rejects_neither(self):
        with pytest.raises(ValidationError):
            ReleaseResolveRequest()

    def test_rejects_both(self):
        with pytest.raises(ValidationError):
            ReleaseResolveRequest(
                url="https://www.discogs.com/release/1",
                source="discogs_release",
                id="123",
            )

    def test_rejects_blank_url(self):
        # An empty string is not a url; the form should be told to use (source, id).
        with pytest.raises(ValidationError):
            ReleaseResolveRequest(url="   ")

    def test_rejects_partial_explicit(self):
        # source without id (or vice versa) is ambiguous.
        with pytest.raises(ValidationError):
            ReleaseResolveRequest(source="discogs_release")
        with pytest.raises(ValidationError):
            ReleaseResolveRequest(id="123")
