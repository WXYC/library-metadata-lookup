"""Unit tests for core/sentry.py."""

from unittest.mock import MagicMock, patch

from core.sentry import add_discogs_breadcrumb, capture_exception, init_sentry


class TestInitSentry:
    @patch("core.sentry.sentry_sdk")
    def test_none_dsn_skips_init(self, mock_sdk):
        init_sentry(dsn=None)
        mock_sdk.init.assert_not_called()

    @patch("core.sentry.sentry_sdk")
    def test_valid_dsn_calls_init(self, mock_sdk):
        init_sentry(dsn="https://examplePublicKey@o0.ingest.sentry.io/0")
        mock_sdk.init.assert_called_once()
        call_kwargs = mock_sdk.init.call_args[1]
        assert call_kwargs["dsn"] == "https://examplePublicKey@o0.ingest.sentry.io/0"

    @patch("core.sentry.sentry_sdk")
    def test_environment_passed(self, mock_sdk):
        init_sentry(dsn="https://key@sentry.io/0", environment="staging")
        call_kwargs = mock_sdk.init.call_args[1]
        assert call_kwargs["environment"] == "staging"

    @patch("core.sentry.sentry_sdk")
    def test_release_passed(self, mock_sdk):
        init_sentry(dsn="https://key@sentry.io/0", release="1.0.0")
        call_kwargs = mock_sdk.init.call_args[1]
        assert call_kwargs["release"] == "1.0.0"


class TestAddDiscogsBreadcrumb:
    @patch("core.sentry.sentry_sdk")
    def test_adds_breadcrumb(self, mock_sdk):
        add_discogs_breadcrumb("search_releases_by_track", {"track": "Test"})
        mock_sdk.add_breadcrumb.assert_called_once_with(
            category="discogs",
            message="search_releases_by_track",
            data={"track": "Test"},
            level="info",
        )

    @patch("core.sentry.sentry_sdk")
    def test_default_data_is_empty(self, mock_sdk):
        add_discogs_breadcrumb("operation")
        call_kwargs = mock_sdk.add_breadcrumb.call_args[1]
        assert call_kwargs["data"] == {}

    @patch("core.sentry.sentry_sdk")
    def test_custom_level(self, mock_sdk):
        add_discogs_breadcrumb("op", level="warning")
        call_kwargs = mock_sdk.add_breadcrumb.call_args[1]
        assert call_kwargs["level"] == "warning"


class TestCaptureException:
    @patch("core.sentry.sentry_sdk")
    def test_captures_without_context(self, mock_sdk):
        err = ValueError("test")
        capture_exception(err)
        mock_sdk.set_context.assert_not_called()
        mock_sdk.capture_exception.assert_called_once_with(err)

    @patch("core.sentry.sentry_sdk")
    def test_captures_with_context(self, mock_sdk):
        err = ValueError("test")
        ctx = {"release_id": 123}
        capture_exception(err, context=ctx)
        mock_sdk.set_context.assert_called_once_with("discogs", ctx)
        mock_sdk.capture_exception.assert_called_once_with(err)
