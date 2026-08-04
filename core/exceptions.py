"""Custom exception classes for the library metadata lookup service."""


class LookupServiceError(Exception):
    """Base exception for all lookup service errors."""

    def __init__(self, message: str, details: dict | None = None):
        self.message = message
        self.details = details or {}
        super().__init__(self.message)


class ArtworkNotFoundError(LookupServiceError):
    """Raised when artwork cannot be found for a song/album."""

    pass


class LibrarySearchError(LookupServiceError):
    """Raised when a library search operation fails."""

    pass


class ServiceInitializationError(LookupServiceError):
    """Raised when a service fails to initialize."""

    pass


class ConfigurationError(LookupServiceError):
    """Raised when there's a configuration error."""

    pass


class BreakerOpenError(Exception):
    """Shared base for every saturation-breaker shed (LML#1118).

    ``DiscogsBreakerOpenError`` (``discogs/breaker.py``, LML#755) and
    ``BandcampBreakerOpenError`` (``clients/bandcamp_breaker.py``, LML#1106)
    both mean the identical thing: **"couldn't ask, try later -- never a
    confirmed miss."** Callers must not treat either as a genuine negative
    result -- no negative-cache write, no ``None`` release-id pin, no
    definitive "not on this release" verdict.

    Lives here rather than in ``discogs/`` or ``clients/`` so neither module
    has to import the other's package to share the type: this module has no
    dependents in either direction, so importing it from both is cycle-free.

    Deliberately a plain ``Exception`` subclass rather than a
    ``LookupServiceError`` one -- see ``BreakerOpenError``'s own subclasses
    for why: ``LookupServiceError`` requires a ``message`` argument, and both
    concrete breaker errors are raised bare at some call sites.

    A single ``except BreakerOpenError`` leg covers every present and future
    breaker, so adding a new one (YouTube Music, Spotify) costs no new
    call-site audit at genuinely breaker-generic boundaries. Most of today's
    call sites intentionally keep catching a concrete subclass instead --
    see the per-site comments at each ``except DiscogsBreakerOpenError`` for
    why that specificity is worth keeping there.
    """
