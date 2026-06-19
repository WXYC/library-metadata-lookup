"""Unit tests for scripts/_lib/signals.py."""

from __future__ import annotations

import logging
import signal

import pytest

from scripts._lib.signals import ShutdownFlag, install_shutdown_handler


@pytest.fixture
def restore_signals():
    """Snapshot SIGINT/SIGTERM handlers and restore them after the test."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_requested_false_on_construction() -> None:
    flag = ShutdownFlag(logger=logging.getLogger("t"))
    assert flag.requested is False


@pytest.mark.parametrize("unit", ["batch", "item"])
def test_first_signal_sets_flag_and_logs_unit(unit: str, caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger(f"test_first_{unit}")
    flag = ShutdownFlag(logger=logger, unit=unit)

    with caplog.at_level(logging.INFO, logger=logger.name):
        flag.handle(signal.SIGINT, None)

    assert flag.requested is True
    assert f"finishing current {unit}" in caplog.text
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_second_signal_force_quits_with_warning_when_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test_force_warn")
    flag = ShutdownFlag(logger=logger, unit="batch", log_force_quit=True)
    flag.handle(signal.SIGINT, None)  # first: latch

    with caplog.at_level(logging.WARNING, logger=logger.name):
        with pytest.raises(SystemExit) as exc:
            flag.handle(signal.SIGINT, None)

    assert exc.value.code == 1
    assert "Force quit requested" in caplog.text


def test_second_signal_force_quits_silently_when_disabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test_force_silent")
    flag = ShutdownFlag(logger=logger, unit="item", log_force_quit=False)
    flag.handle(signal.SIGINT, None)  # first: latch

    with caplog.at_level(logging.DEBUG, logger=logger.name):
        caplog.clear()
        with pytest.raises(SystemExit) as exc:
            flag.handle(signal.SIGINT, None)

    assert exc.value.code == 1
    assert "Force quit requested" not in caplog.text


def test_install_registers_sigint_and_sigterm(restore_signals: None) -> None:
    logger = logging.getLogger("test_install")
    flag = install_shutdown_handler(logger=logger)

    assert isinstance(flag, ShutdownFlag)
    assert signal.getsignal(signal.SIGINT) == flag.handle
    assert signal.getsignal(signal.SIGTERM) == flag.handle


@pytest.mark.parametrize(
    "unit, log_force_quit",
    [
        pytest.param("batch", True, id="variant-a"),
        pytest.param("item", False, id="variant-b"),
    ],
)
def test_install_threads_variant_params(
    unit: str, log_force_quit: bool, restore_signals: None
) -> None:
    logger = logging.getLogger("test_variant")
    flag = install_shutdown_handler(logger=logger, unit=unit, log_force_quit=log_force_quit)

    assert flag._unit == unit
    assert flag._log_force_quit is log_force_quit
