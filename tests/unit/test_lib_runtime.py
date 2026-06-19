"""Unit tests for scripts/_lib/runtime.py."""

from __future__ import annotations

import logging
import signal

import pytest

from scripts._lib.runtime import set_up_script_runtime
from scripts._lib.signals import ShutdownFlag


@pytest.fixture(autouse=True)
def restore_signals():
    """Snapshot SIGINT/SIGTERM handlers and restore them after the test."""
    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace load_dotenv with a no-op so the test does not read the real .env."""
    monkeypatch.setattr("scripts._lib.runtime.load_dotenv", lambda: None)


def _reset_root_logging() -> None:
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)


def test_returns_shutdown_flag() -> None:
    _reset_root_logging()
    flag = set_up_script_runtime(logger=logging.getLogger("rt_return"))
    assert isinstance(flag, ShutdownFlag)


def test_calls_load_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_root_logging()
    called: list[bool] = []
    monkeypatch.setattr("scripts._lib.runtime.load_dotenv", lambda: called.append(True))
    set_up_script_runtime(logger=logging.getLogger("rt_dotenv"))
    assert called == [True]


@pytest.mark.parametrize(
    "verbose, expected",
    [(False, logging.INFO), (True, logging.DEBUG)],
)
def test_verbose_sets_root_level(verbose: bool, expected: int) -> None:
    _reset_root_logging()
    set_up_script_runtime(logger=logging.getLogger("rt_level"), verbose=verbose)
    assert logging.getLogger().level == expected


def test_quiet_httpx_true_silences_httpx_logger() -> None:
    _reset_root_logging()
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    set_up_script_runtime(logger=logging.getLogger("rt_httpx_on"), quiet_httpx=True)
    assert logging.getLogger("httpx").level == logging.WARNING


def test_quiet_httpx_false_leaves_httpx_logger_untouched() -> None:
    _reset_root_logging()
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    set_up_script_runtime(logger=logging.getLogger("rt_httpx_off"), quiet_httpx=False)
    assert logging.getLogger("httpx").level == logging.NOTSET


@pytest.mark.parametrize(
    "include_logger_name, name_in_format",
    [(True, True), (False, False)],
)
def test_include_logger_name_toggles_formatter(
    include_logger_name: bool, name_in_format: bool
) -> None:
    _reset_root_logging()
    set_up_script_runtime(
        logger=logging.getLogger("rt_fmt"),
        include_logger_name=include_logger_name,
    )
    handler = logging.getLogger().handlers[0]
    assert handler.formatter is not None
    fmt = handler.formatter._fmt or ""
    assert ("%(name)s" in fmt) is name_in_format


@pytest.mark.parametrize(
    "shutdown_unit, log_force_quit",
    [
        pytest.param("batch", True, id="variant-a"),
        pytest.param("item", False, id="variant-b"),
    ],
)
def test_threads_shutdown_variant(shutdown_unit: str, log_force_quit: bool) -> None:
    _reset_root_logging()
    flag = set_up_script_runtime(
        logger=logging.getLogger("rt_variant"),
        shutdown_unit=shutdown_unit,
        log_force_quit=log_force_quit,
    )
    assert flag._unit == shutdown_unit
    assert flag._log_force_quit is log_force_quit
    assert signal.getsignal(signal.SIGINT) == flag.handle
    assert signal.getsignal(signal.SIGTERM) == flag.handle
