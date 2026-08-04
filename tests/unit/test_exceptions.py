"""Unit tests for core/exceptions.py."""

import pytest

from core.exceptions import (
    ArtworkNotFoundError,
    BreakerOpenError,
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


class TestBreakerOpenError:
    """LML#1118: the shared base for every saturation-breaker shed.

    ``BreakerOpenError`` is deliberately a plain ``Exception`` subclass, not a
    ``LookupServiceError`` one: the two concrete breaker errors it replaces
    (``DiscogsBreakerOpenError``, ``BandcampBreakerOpenError``) are raised both
    bare (``BandcampBreakerOpenError()``, ``clients/bandcamp.py``) and with a
    message (``DiscogsBreakerOpenError(f"...")``, ``discogs/admission.py``).
    ``LookupServiceError.__init__`` requires a ``message`` positional argument,
    so subclassing it here would turn every bare raise into a ``TypeError`` --
    a real behavior change the LML#1118 "behavior-preserving" constraint rules
    out.
    """

    def test_inherits_from_exception(self):
        assert issubclass(BreakerOpenError, Exception)

    def test_not_a_lookup_service_error(self):
        # See the class docstring: LookupServiceError's required `message`
        # argument would break the no-arg raise sites this type replaces.
        assert not issubclass(BreakerOpenError, LookupServiceError)

    def test_bare_construction(self):
        # Mirrors `raise BandcampBreakerOpenError()` in clients/bandcamp.py.
        err = BreakerOpenError()
        assert isinstance(err, Exception)

    def test_construction_with_message(self):
        # Mirrors `raise DiscogsBreakerOpenError(f"...")` in discogs/admission.py.
        err = BreakerOpenError("Discogs saturation breaker open: GET /release/1")
        assert str(err) == "Discogs saturation breaker open: GET /release/1"


class TestConcreteBreakerErrorsInheritTheBase:
    """Both real breaker errors must reparent onto ``BreakerOpenError`` so a
    single ``except BreakerOpenError`` leg covers every present and future
    breaker (LML#1118)."""

    def test_discogs_breaker_open_error_is_a_breaker_open_error(self):
        from discogs.breaker import DiscogsBreakerOpenError

        assert issubclass(DiscogsBreakerOpenError, BreakerOpenError)

    def test_bandcamp_breaker_open_error_is_a_breaker_open_error(self):
        from clients.bandcamp_breaker import BandcampBreakerOpenError

        assert issubclass(BandcampBreakerOpenError, BreakerOpenError)

    def test_discogs_and_bandcamp_breaker_errors_stay_distinct(self):
        """Reparenting must not collapse the two concrete names into one --
        they carry real specificity in logs and at sites that want one and
        not the other (LML#1118)."""
        from clients.bandcamp_breaker import BandcampBreakerOpenError
        from discogs.breaker import DiscogsBreakerOpenError

        assert not issubclass(DiscogsBreakerOpenError, BandcampBreakerOpenError)
        assert not issubclass(BandcampBreakerOpenError, DiscogsBreakerOpenError)
