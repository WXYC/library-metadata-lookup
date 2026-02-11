"""Unit tests for core/logging.py."""

import logging

from core.logging import get_logger, setup_logging


class TestSetupLogging:
    def test_default_format(self):
        setup_logging()
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_custom_level(self):
        setup_logging(level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_custom_format_string(self):
        setup_logging(format_string="%(message)s")
        root = logging.getLogger()
        assert root.handlers  # at least one handler configured

    def test_file_handler_created(self, tmp_path):
        log_file = tmp_path / "test.log"
        setup_logging(log_file=log_file)
        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) >= 1
        assert log_file.exists()

    def test_file_handler_creates_parent_dirs(self, tmp_path):
        log_file = tmp_path / "nested" / "deep" / "test.log"
        setup_logging(log_file=log_file)
        assert log_file.parent.exists()


class TestGetLogger:
    def test_returns_logger_with_name(self):
        logger = get_logger("test.module")
        assert logger.name == "test.module"
        assert isinstance(logger, logging.Logger)

    def test_returns_same_logger_for_same_name(self):
        logger1 = get_logger("same_name")
        logger2 = get_logger("same_name")
        assert logger1 is logger2
