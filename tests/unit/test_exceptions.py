"""Unit tests for core/exceptions.py."""

import pytest

from core.exceptions import (
    ArtworkNotFoundError,
    ConfigurationError,
    LibrarySearchError,
    LookupServiceError,
    ServiceInitializationError,
)


class TestLookupServiceError:
    """Tests for the base exception class."""

    def test_message_attribute(self):
        err = LookupServiceError("something went wrong")
        assert err.message == "something went wrong"

    def test_str_output(self):
        err = LookupServiceError("something went wrong")
        assert str(err) == "something went wrong"

    def test_details_default_empty(self):
        err = LookupServiceError("msg")
        assert err.details == {}

    def test_details_provided(self):
        err = LookupServiceError("msg", details={"key": "val"})
        assert err.details == {"key": "val"}

    def test_inherits_from_exception(self):
        err = LookupServiceError("msg")
        assert isinstance(err, Exception)


SUBCLASSES = [
    ArtworkNotFoundError,
    LibrarySearchError,
    ServiceInitializationError,
    ConfigurationError,
]


@pytest.mark.parametrize("cls", SUBCLASSES, ids=lambda c: c.__name__)
class TestExceptionSubclasses:
    """All subclasses inherit from LookupServiceError and carry message/details."""

    def test_inherits_from_base(self, cls):
        err = cls("test")
        assert isinstance(err, LookupServiceError)

    def test_message_and_details(self, cls):
        err = cls("detail msg", details={"a": 1})
        assert err.message == "detail msg"
        assert err.details == {"a": 1}
        assert str(err) == "detail msg"
