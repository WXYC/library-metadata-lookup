"""Graceful-shutdown signal handling shared by the one-shot scripts.

The four streaming-pipeline scripts previously each carried a byte-identical
``_handle_signal`` callback plus a module-level ``_shutdown_requested`` flag, in
two behaviorally-distinct variants:

* **Variant A** (``unit="batch"``, ``log_force_quit=True``) — logs a warning on
  the force-quit (second) signal before exiting.
* **Variant B** (``unit="item"``, ``log_force_quit=False``) — exits silently on
  the force-quit signal.

``ShutdownFlag`` holds the latch as *instance state* behind a ``.requested``
property. Scripts read the flag many times via a module-level handle
(``_shutdown.requested``); because the latch lives on the instance rather than a
re-bound module global, every read observes the mutation. (A
``from ... import _shutdown_requested`` would have bound the boolean by value and
never seen it flip — the bug this design avoids.)
"""

from __future__ import annotations

import signal
import sys
from logging import Logger
from types import FrameType

_DEFAULT_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)


class ShutdownFlag:
    """A latch toggled by SIGINT/SIGTERM for cooperative graceful shutdown.

    The first signal sets :attr:`requested` (the running loop is expected to
    finish its current ``unit`` of work and stop). A second signal forces an
    immediate ``sys.exit(1)``, optionally logging a warning first.
    """

    def __init__(
        self,
        *,
        logger: Logger,
        unit: str = "batch",
        log_force_quit: bool = True,
    ) -> None:
        self._logger = logger
        self._unit = unit
        self._log_force_quit = log_force_quit
        self._requested = False

    @property
    def requested(self) -> bool:
        """Whether a graceful shutdown has been requested."""
        return self._requested

    def handle(self, signum: int, frame: FrameType | None) -> None:
        """Signal callback: latch on the first signal, force-quit on the second."""
        if self._requested:
            if self._log_force_quit:
                self._logger.warning("Force quit requested, exiting immediately")
            sys.exit(1)
        self._logger.info("Shutdown requested, finishing current %s...", self._unit)
        self._requested = True


def install_shutdown_handler(
    *,
    logger: Logger,
    unit: str = "batch",
    log_force_quit: bool = True,
    signals: tuple[signal.Signals, ...] = _DEFAULT_SIGNALS,
) -> ShutdownFlag:
    """Create a :class:`ShutdownFlag` and register it for ``signals``.

    Returns the flag so the caller can read ``flag.requested`` from its loop.
    """
    flag = ShutdownFlag(logger=logger, unit=unit, log_force_quit=log_force_quit)
    for sig in signals:
        signal.signal(sig, flag.handle)
    return flag
