"""Shared ``main()`` preamble for the one-shot scripts.

:func:`set_up_script_runtime` folds the boilerplate that the streaming-pipeline
scripts run before handing off to ``asyncio.run(run(...))``: load ``.env``,
configure root logging, optionally quiet the noisy ``httpx`` logger, and install
the graceful-shutdown signal handler. Argument parsing and ``asyncio.run`` stay
with the caller.

The flags exist to reproduce — not normalize — the small per-script differences:

* ``include_logger_name`` toggles ``%(name)s`` in the log format. The two
  variant-A scripts include it; the two variant-B scripts omit it.
* ``quiet_httpx`` raises the ``httpx`` logger to ``WARNING``. Variant A and
  ``search_unmatched_compilations`` do this; ``discogs_rematch`` does not.
* ``shutdown_unit`` / ``log_force_quit`` are threaded through to
  :func:`scripts._lib.signals.install_shutdown_handler` (variant A uses
  ``"batch"``/``True``; variant B uses ``"item"``/``False``).
"""

from __future__ import annotations

import logging
from logging import Logger

from dotenv import load_dotenv

from scripts._lib.signals import ShutdownFlag, install_shutdown_handler


def set_up_script_runtime(
    *,
    logger: Logger,
    verbose: bool = False,
    shutdown_unit: str = "batch",
    log_force_quit: bool = True,
    quiet_httpx: bool = True,
    include_logger_name: bool = True,
) -> ShutdownFlag:
    """Run the shared script preamble and return the installed shutdown flag.

    Performs ``load_dotenv`` → ``logging.basicConfig`` → optional ``httpx``
    quieting → ``install_shutdown_handler``. The caller still owns argparse and
    ``asyncio.run(run(args, shutdown))``.
    """
    load_dotenv()

    level = logging.DEBUG if verbose else logging.INFO
    if include_logger_name:
        fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    else:
        fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")

    if quiet_httpx:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    return install_shutdown_handler(
        logger=logger,
        unit=shutdown_unit,
        log_force_quit=log_force_quit,
    )
