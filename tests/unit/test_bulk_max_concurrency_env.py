"""Unit tests for ``core.bulk_concurrency.max_concurrency_from_env`` (LML#1215).

``LML_BULK_MAX_CONCURRENT`` used to be resolved by a hand-rolled reader whose
zero/negative handling diverged, silently, from the one every other runtime int
knob in this repo uses. ``max(1, int(raw))`` clamped ``0`` and negatives to 1
with no WARN, while ``core.search.resolve_positive_int_env`` -- which serves
``LML_LOOKUP_MAX_CONCURRENT``, ``LML_BULK_GLOBAL_MAX_CONCURRENT``,
``LML_DISCOGS_POOL_MAX_SIZE``, ``LML_STREAMING_WARM_CONCURRENCY`` and the search
budget/hard-timeout pair -- treats both as misconfiguration and falls back to
the caller's default with a WARN. ``docs/env-vars.md`` advertised the shared
contract as the family convention while this knob quietly did something else,
and had no entry of its own saying so.

#1215 took option (a): delegate, and own the behavior change. ``0`` and
negatives now warn and resolve to the caller's default rather than silently
serializing the batch. The can't-hang-the-gather guarantee the clamp existed
for survives unchanged, because every caller's default is positive -- which is
what :func:`test_the_bound_is_always_positive_whatever_the_operator_typed`
pins directly, rather than trusting the callers to keep being careful.

The parameterized bad-value set deliberately includes the empty string. Under
the old reader ``if not raw`` made ``LML_BULK_MAX_CONCURRENT=""`` a silent
default; under the shared one it is an unparseable value and warns, matching
every sibling knob. That is a real (if small) behavior change and is pinned
here so it is a decision rather than an accident.
"""

import logging

import pytest

from core.bulk_concurrency import max_concurrency_from_env

_ENV_VAR = "LML_BULK_MAX_CONCURRENT"

# Every input the shared reader treats as operator misconfiguration: garbage,
# zero, a negative, and an explicitly-empty value.
_BAD_VALUES = ["not-an-int", "0", "-1", "", "3.5"]


def test_unset_env_returns_the_callers_default_without_warning(monkeypatch, caplog):
    """The common case: no env var, no log line, the caller's default."""
    monkeypatch.delenv(_ENV_VAR, raising=False)

    with caplog.at_level(logging.WARNING):
        assert max_concurrency_from_env(10) == 10

    assert caplog.records == []


@pytest.mark.parametrize("raw,expected", [("1", 1), ("4", 4), ("25", 25)])
def test_valid_positive_value_is_honored(monkeypatch, caplog, raw, expected):
    """A well-formed positive value wins over the default, silently."""
    monkeypatch.setenv(_ENV_VAR, raw)

    with caplog.at_level(logging.WARNING):
        assert max_concurrency_from_env(10) == expected

    assert caplog.records == []


@pytest.mark.parametrize("bad_value", _BAD_VALUES)
def test_misconfigured_value_warns_and_falls_back_to_the_default(monkeypatch, caplog, bad_value):
    """Garbage, zero, negatives and empty all WARN and resolve to the default.

    The WARN is the point: before #1215, ``=0`` silently serialized every bulk
    batch in the process, which looks like a performance mystery rather than a
    typo.
    """
    monkeypatch.setenv(_ENV_VAR, bad_value)

    with caplog.at_level(logging.WARNING):
        assert max_concurrency_from_env(10) == 10

    assert any(_ENV_VAR in rec.getMessage() for rec in caplog.records), (
        f"expected a WARN naming {_ENV_VAR} for {bad_value!r}"
    )


@pytest.mark.parametrize("bad_value", _BAD_VALUES)
def test_the_bound_is_always_positive_whatever_the_operator_typed(monkeypatch, bad_value):
    """The guarantee the old clamp existed for, pinned directly.

    ``asyncio.Semaphore(0)`` deadlocks every item in the batch forever, so the
    resolved bound must never be non-positive. The clamp is gone, but the
    guarantee is not -- it now comes from the fallback being the caller's
    default. One default suffices: the reader returns ``default`` verbatim on
    every misconfigured input, so the bound tracks the caller's argument rather
    than varying with it.
    """
    monkeypatch.setenv(_ENV_VAR, bad_value)

    assert max_concurrency_from_env(1) >= 1
