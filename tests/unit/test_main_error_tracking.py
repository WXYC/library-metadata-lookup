"""Wiring of wxyc-fastapi's PostHog error tracking beside ``init_sentry`` (LML#1470).

``main._init_error_tracking`` is the one call site; the library does the rest
(logging handler, excepthooks, request-id middleware). ``daily_ceiling`` is a
required keyword at wxyc-fastapi 1.6.0 -- 50 per process in staging, 300
otherwise (the wxyc-fastapi#43 contract).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import FastAPI

import main
from config.settings import Settings


@pytest.mark.parametrize(
    ("environment", "ceiling"),
    [
        ("staging", 50),
        ("Staging", 50),
        ("stage", 50),
        ("production", 300),
        ("prod", 300),
        ("development", 300),
    ],
)
def test_daily_ceiling_is_50_in_staging_and_300_elsewhere(environment, ceiling):
    assert main._error_tracking_daily_ceiling(environment) == ceiling


def test_init_error_tracking_passes_the_service_identity_and_the_app():
    app = FastAPI()
    settings = Settings(environment="staging", app_version="1.2.3")

    with patch("main.init_error_tracking") as mock_init:
        main._init_error_tracking(app, settings)

    mock_init.assert_called_once_with(
        service="library-metadata-lookup",
        environment="staging",
        daily_ceiling=50,
        release="1.2.3",
        app=app,
    )


def test_unset_posthog_errors_key_leaves_the_app_untouched(monkeypatch):
    """With POSTHOG_ERRORS_API_KEY unset the library returns None and installs
    nothing: no middleware, no exception handler."""
    monkeypatch.delenv("POSTHOG_ERRORS_API_KEY", raising=False)
    app = FastAPI()
    handlers_before = dict(app.exception_handlers)

    main._init_error_tracking(app, Settings(environment="production"))

    assert app.user_middleware == []
    assert app.exception_handlers == handlers_before
