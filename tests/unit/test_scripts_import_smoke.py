"""Import-smoke the scripts that adopted scripts/_lib helpers.

Guards against the new ``from scripts._lib...`` import lines silently breaking
the four scripts, and confirms each script's module-level ``_shutdown`` handle is
wired to the correct variant (audit findings #7 + #8).
"""

from __future__ import annotations

import importlib

import pytest

from scripts._lib.signals import ShutdownFlag

_VARIANTS = [
    pytest.param("scripts.streaming_availability.__main__", "batch", True, id="streaming-avail"),
    pytest.param("scripts.track_streaming.__main__", "batch", True, id="track-streaming"),
    pytest.param("scripts.discogs_rematch", "item", False, id="discogs-rematch"),
    pytest.param("scripts.search_unmatched_compilations", "item", False, id="search-comps"),
]


@pytest.mark.parametrize("module_name, unit, log_force_quit", _VARIANTS)
def test_script_imports_with_expected_shutdown_variant(
    module_name: str, unit: str, log_force_quit: bool
) -> None:
    module = importlib.import_module(module_name)
    shutdown = module._shutdown
    assert isinstance(shutdown, ShutdownFlag)
    assert shutdown.requested is False
    assert shutdown._unit == unit
    assert shutdown._log_force_quit is log_force_quit
