"""Centralized logging configuration."""

import logging
import logging.handlers
import os
import sys
from pathlib import Path

#: Bound each worker's log file at 10 MB before rotating, keeping 3 backups
#: (~40 MB ceiling per worker). Overridable per call via setup_logging's
#: max_bytes/backup_count; not currently env-tunable -- add an env knob if an
#: operator needs to change this from Railway without a redeploy.
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 3


def worker_log_path(log_file: Path) -> Path:
    """Return the per-worker variant of a requested log file path.

    LML#1124 Phase 4: ``entrypoint.sh`` execs ``uvicorn ... --workers
    ${uvicorn_workers}`` (``UVICORN_WORKERS``, the LML#747 horizontal-scaling
    lever with its own runbook in docs/deployment.md), so N sibling processes
    may call ``setup_logging`` concurrently against what would otherwise be
    the same path. ``logging.handlers.RotatingFileHandler`` is not
    multi-process-safe -- two workers rotating the same underlying file race
    the rename, and a worker mid-write to the pre-rotation file descriptor
    loses records.

    Suffixing the filename with the calling worker's PID makes concurrent
    rollover structurally impossible rather than merely discouraged, and the
    scheme is correct at any worker count, including today's default of 1
    (where it costs nothing but a PID in the filename). The alternatives were
    considered and rejected: ``WatchedFileHandler`` + external ``logrotate``
    needs a logrotate that does not exist in the Railway image, and a
    single-worker-only guarantee on a shared-file ``RotatingFileHandler``
    would constrain ``UVICORN_WORKERS``, a real, documented product lever,
    rather than the log handler adapting to it. Proliferation across restarts
    is bounded: post-LML#834 there is no mounted volume, so ``/app/logs`` is
    ephemeral per container, and old per-worker files vanish with the
    container instead of accumulating.
    """
    return log_file.with_name(f"{log_file.stem}.{os.getpid()}{log_file.suffix}")


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    format_string: str | None = None,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> None:
    """Configure application-wide logging.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional path to log file. If provided, logs to both file and console.
            The file actually written is the per-worker variant of this path
            (see worker_log_path) -- concurrent uvicorn workers never share a
            RotatingFileHandler's rollover.
        format_string: Custom log format string. Uses default if not provided
        max_bytes: Rotate a worker's log file after it exceeds this size, in bytes.
        backup_count: Number of rotated backups to retain per worker.
    """
    if format_string is None:
        format_string = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    # Create handlers
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    worker_file: Path | None = None
    if log_file:
        # Ensure log directory exists
        log_file.parent.mkdir(parents=True, exist_ok=True)
        worker_file = worker_log_path(log_file)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                worker_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
            )
        )

    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=format_string,
        handlers=handlers,
        force=True,  # Override any existing configuration
    )

    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured at {level} level")
    if worker_file:
        # Name the file this process actually opened, not the caller's bare
        # argument -- under the per-worker scheme that argument is a path
        # nothing ever writes to, and every worker would claim the same one.
        logger.info(f"Logging to file: {worker_file}")


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for a module.

    Args:
        name: Logger name (typically __name__)

    Returns:
        logging.Logger: Logger instance
    """
    return logging.getLogger(name)
