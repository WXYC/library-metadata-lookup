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

    A plain ``Exception`` subclass rather than a ``LookupServiceError`` one --
    NOT because any raise site needs a bare (message-less) constructor. Every
    current raise site passes one (``clients/bandcamp.py``,
    ``discogs/admission.py``, ``discogs/service.py``,
    ``lookup/release_resolution.py``); joining ``LookupServiceError`` would
    cost nothing there. The real reason is that ``LookupServiceError`` has no
    consumers to inherit into: nothing in this service does ``except
    LookupServiceError``, no FastAPI handler is registered for it (``main.py``
    only registers ``StarletteHTTPException`` and ``RequestValidationError``),
    no Sentry ``before_send``/``ignore_errors``/fingerprint hook keys on its
    type, and nothing outside ``tests/unit/test_exceptions.py`` reads
    ``.message``/``.details``. An uncaught ``BreakerOpenError`` and an
    uncaught ``LookupServiceError`` both surface as an identical raw 500
    today, so there is no behavioral payoff to joining the hierarchy -- only
    a heavier constructor contract this type's own subclasses would inherit
    for nothing.

    A single ``except BreakerOpenError`` leg covers every present and future
    breaker, so adding a new one (YouTube Music, Spotify) costs no new
    call-site audit at genuinely breaker-generic boundaries. Most of today's
    call sites intentionally keep catching a concrete subclass instead --
    see the per-site comments at each ``except DiscogsBreakerOpenError`` for
    why that specificity is worth keeping there.
    """
