"""Sentry error tracking integration."""

import logging
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration

logger = logging.getLogger(__name__)


def init_sentry(
    dsn: str | None,
    environment: str = "production",
    release: str | None = None,
) -> None:
    """Initialize Sentry SDK with FastAPI integration.

    Args:
        dsn: Sentry DSN (Data Source Name). If None, Sentry is not initialized.
        environment: Deployment environment (e.g., "production", "staging", "development")
        release: Optional release version string
    """
    if dsn is None:
        logger.info("Sentry DSN not configured, skipping initialization")
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        integrations=[
            FastApiIntegration(),
        ],
        # Capture 100% of transactions for performance monitoring
        traces_sample_rate=1.0,
        # Send all errors
        sample_rate=1.0,
    )

    logger.info(f"Sentry initialized (environment: {environment})")


def add_discogs_breadcrumb(
    operation: str,
    data: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Add a breadcrumb for a Discogs operation.

    Breadcrumbs are recorded events leading up to an error.

    Args:
        operation: Name of the operation (e.g., "search_releases_by_track", "get_release")
        data: Optional dictionary of contextual data
        level: Severity level ("debug", "info", "warning", "error")
    """
    sentry_sdk.add_breadcrumb(
        category="discogs",
        message=operation,
        data=data or {},
        level=level,
    )


def capture_exception(
    error: Exception,
    context: dict[str, Any] | None = None,
) -> None:
    """Capture an exception and send it to Sentry.

    Args:
        error: The exception to capture
        context: Optional dictionary of contextual data to attach
    """
    if context:
        sentry_sdk.set_context("discogs", context)

    sentry_sdk.capture_exception(error)
