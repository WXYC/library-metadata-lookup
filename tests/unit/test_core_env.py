"""Unit tests for core/env.py — the shared boolean env-var reader (LML#1204 item 3).

Lives beside its own leaf module rather than in ``test_search.py``: the reader
moved out of ``core/search.py`` so that adopters (and, in future,
``discogs/*``) can read a flag without importing the Discogs stack. See
``core/env.py``'s module docstring for the cycle this avoids.
"""

import logging

import pytest

from core.env import (
    _FALSE_BOOL_ENV_VALUES,
    _TRUE_BOOL_ENV_VALUES,
    resolve_bool_env,
)


class TestResolveBoolEnv:
    """LML#1204 item 3: one shared boolean env reader beside
    ``core.search.resolve_positive_int_env``, replacing the three ``_TRUE_FLAG_VALUES``
    copies (``lookup/admission.py``, ``lookup/wikipedia_url.py``,
    ``lookup/enrichment/wikipedia_bio.py``) and ``lookup/artist_resolution.py``'s
    inverse-polarity ``_FALSE_FLAG_VALUES`` — whose accept-set was asymmetric
    (``disabled`` disabled a default-ON flag but ``enabled`` did NOT enable a
    default-OFF one). One vocabulary, symmetric spellings."""

    _ENV_VAR = "LML_TEST_BOOL_FLAG"

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        from core import env as env_module

        monkeypatch.delenv(self._ENV_VAR, raising=False)
        # The junk WARN is memoized per env-var name for the process's life
        # (see TestResolveBoolEnvWarnMemoization) — reset so each test case
        # observes its own first read.
        env_module._warned_bool_env_vars.clear()
        yield
        env_module._warned_bool_env_vars.clear()

    @pytest.mark.parametrize("default", [True, False])
    def test_unset_returns_the_default(self, default):
        assert resolve_bool_env(self._ENV_VAR, default=default) is default

    @pytest.mark.parametrize("default", [True, False])
    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_behaves_like_unset(self, monkeypatch, value, default):
        """Env vars outrank the .env source, and empty behaves like unset
        throughout this codebase — matching every replaced copy, where an
        empty string fell through to the site's default."""
        monkeypatch.setenv(self._ENV_VAR, value)
        assert resolve_bool_env(self._ENV_VAR, default=default) is default

    @pytest.mark.parametrize("default", [True, False])
    @pytest.mark.parametrize("value", ["1", "true", "True", "yes", "YES", "on", " on ", "enabled"])
    def test_true_spellings_enable_regardless_of_default(self, monkeypatch, value, default):
        monkeypatch.setenv(self._ENV_VAR, value)
        assert resolve_bool_env(self._ENV_VAR, default=default) is True

    @pytest.mark.parametrize("default", [True, False])
    @pytest.mark.parametrize(
        "value", ["0", "false", "False", "no", "NO", "off", " off ", "disabled"]
    )
    def test_false_spellings_disable_regardless_of_default(self, monkeypatch, value, default):
        monkeypatch.setenv(self._ENV_VAR, value)
        assert resolve_bool_env(self._ENV_VAR, default=default) is False

    @pytest.mark.parametrize("default", [True, False])
    def test_junk_falls_back_to_the_default_with_a_warn(self, monkeypatch, caplog, default):
        """Same operator-typo contract as ``core.search.resolve_positive_int_env``: junk
        must not 500 anything, and must not silently flip a flag either way."""
        monkeypatch.setenv(self._ENV_VAR, "garbage")
        with caplog.at_level(logging.WARNING, logger="core.env"):
            assert resolve_bool_env(self._ENV_VAR, default=default) is default
        assert any(self._ENV_VAR in record.getMessage() for record in caplog.records)

    def test_the_accept_vocabulary_is_symmetric(self):
        """Every true spelling has a false counterpart (1/0, true/false,
        yes/no, on/off, enabled/disabled) — the asymmetry this consolidation
        retires from ``lookup/artist_resolution.py``."""
        pairs = [
            ("1", "0"),
            ("true", "false"),
            ("yes", "no"),
            ("on", "off"),
            ("enabled", "disabled"),
        ]
        assert _TRUE_BOOL_ENV_VALUES == frozenset(t for t, _ in pairs)
        assert _FALSE_BOOL_ENV_VALUES == frozenset(f for _, f in pairs)


class TestResolveBoolEnvWarnMemoization:
    """LML#1204 review: adopters sit on per-request/per-item hot paths, so a
    typo'd Railway var must warn once per process per env-var name — not
    ~100+ times per bulk request, indefinitely (the replaced inline readers
    were silent; once is the observability win without the flood). The
    memoization is the ``_warned_prefixes`` idiom
    ``wxyc_fastapi.observability.posthog.get_posthog_client`` already uses."""

    _ENV_VAR = "LML_TEST_BOOL_FLAG_MEMO"
    _OTHER_ENV_VAR = "LML_TEST_BOOL_FLAG_MEMO_OTHER"

    @pytest.fixture(autouse=True)
    def _clean_state(self, monkeypatch):
        from core import env as env_module

        monkeypatch.delenv(self._ENV_VAR, raising=False)
        monkeypatch.delenv(self._OTHER_ENV_VAR, raising=False)
        env_module._warned_bool_env_vars.clear()
        yield
        env_module._warned_bool_env_vars.clear()

    def test_junk_warns_once_per_env_var_not_per_read(self, monkeypatch, caplog):
        monkeypatch.setenv(self._ENV_VAR, "ture")
        with caplog.at_level(logging.WARNING, logger="core.env"):
            for _ in range(5):
                assert resolve_bool_env(self._ENV_VAR, default=False) is False
        warns = [r for r in caplog.records if self._ENV_VAR in r.getMessage()]
        assert len(warns) == 1

    def test_a_different_env_var_still_gets_its_own_warn(self, monkeypatch, caplog):
        monkeypatch.setenv(self._ENV_VAR, "ture")
        monkeypatch.setenv(self._OTHER_ENV_VAR, "flase")
        with caplog.at_level(logging.WARNING, logger="core.env"):
            resolve_bool_env(self._ENV_VAR, default=False)
            resolve_bool_env(self._OTHER_ENV_VAR, default=True)
        messages = [r.getMessage() for r in caplog.records]
        assert any(self._ENV_VAR in m for m in messages)
        assert any(self._OTHER_ENV_VAR in m for m in messages)
