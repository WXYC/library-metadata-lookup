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
