"""Unit tests for core/logging.py."""

import logging
import logging.handlers
import os

from core.logging import get_logger, setup_logging, worker_log_path


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
        assert worker_log_path(log_file).exists()

    def test_file_handler_creates_parent_dirs(self, tmp_path):
        log_file = tmp_path / "nested" / "deep" / "test.log"
        setup_logging(log_file=log_file)
        assert log_file.parent.exists()


class TestLogRotation:
    """LML#1124 Phase 4: RotatingFileHandler is not multi-process-safe, and
    entrypoint.sh execs `uvicorn ... --workers ${uvicorn_workers}` (UVICORN_WORKERS,
    the LML#747 horizontal-scaling lever). These pin the chosen posture -- a
    RotatingFileHandler per worker, keyed on the worker's own PID -- rather than
    merely checking that *some* FileHandler subclass got attached, which a
    single shared RotatingFileHandler would also satisfy while still being unsafe
    under N>1 workers.
    """

    def test_log_file_is_pid_suffixed_per_worker(self, tmp_path):
        """The file actually written carries the current process's PID, and the
        bare filename callers pass in is never written to directly -- so two
        sibling uvicorn workers calling setup_logging with the identical
        log_file argument never touch the same path on disk."""
        log_file = tmp_path / "library-metadata-lookup.log"

        setup_logging(log_file=log_file)

        expected = tmp_path / f"library-metadata-lookup.{os.getpid()}.log"
        assert expected.exists()
        assert not log_file.exists()

    def test_distinct_worker_pids_get_distinct_log_files(self, tmp_path, monkeypatch):
        """Simulates two sibling worker processes: each gets its own file, so
        neither's RotatingFileHandler can race the other's rollover."""
        log_file = tmp_path / "test.log"

        monkeypatch.setattr(os, "getpid", lambda: 11111)
        setup_logging(log_file=log_file)
        monkeypatch.setattr(os, "getpid", lambda: 22222)
        setup_logging(log_file=log_file)

        assert (tmp_path / "test.11111.log").exists()
        assert (tmp_path / "test.22222.log").exists()

    def test_file_handler_is_rotating_with_configured_limits(self, tmp_path):
        """The attached handler must actually be a RotatingFileHandler configured
        with the requested size/backup-count -- not merely some FileHandler
        subclass, which the old plain FileHandler's superclass check could not
        distinguish."""
        log_file = tmp_path / "test.log"

        setup_logging(log_file=log_file, max_bytes=1024, backup_count=2)

        root = logging.getLogger()
        rotating = [h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert len(rotating) == 1
        assert rotating[0].maxBytes == 1024
        assert rotating[0].backupCount == 2

    def test_rotation_actually_rolls_over_on_disk(self, tmp_path):
        """Behavioral proof, not a type check: write past the size ceiling and
        confirm a rotated backup file actually lands on disk. A plain
        (non-rotating) FileHandler would grow ``test.log`` unbounded forever and
        never produce a ``.1`` backup, which is the real-world symptom (LML#1124:
        `logs/library-metadata-lookup.log` grew to 20 MB unrotated)."""
        log_file = tmp_path / "test.log"
        setup_logging(log_file=log_file, max_bytes=200, backup_count=2)

        logger = get_logger("test_rotation_actually_rolls_over_on_disk")
        for _ in range(100):
            logger.info("x" * 50)

        worker_file = worker_log_path(log_file)
        backup_file = worker_file.with_name(worker_file.name + ".1")
        assert backup_file.exists()


class TestGetLogger:
    def test_returns_logger_with_name(self):
        logger = get_logger("test.module")
        assert logger.name == "test.module"
        assert isinstance(logger, logging.Logger)

    def test_returns_same_logger_for_same_name(self):
        logger1 = get_logger("same_name")
        logger2 = get_logger("same_name")
        assert logger1 is logger2
