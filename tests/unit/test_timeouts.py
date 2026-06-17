"""Unit tests for ``lookup.timeouts``.

``probe_timeout_s_from_env`` is the generic per-call wall-clock ceiling
resolver: read an integer-ms env var, fall back to a default on
unset/unparseable/non-positive. ``apple_music_lookup_timeout_s`` is the
back-compat wrapper the in-line Apple probe uses; the streaming-URL
post-process resolves its per-service ceiling through the same helper
(LML#573) so ``LML_APPLE_MUSIC_LOOKUP_TIMEOUT_MS`` keeps tuning both Apple
call sites without a redeploy.
"""

from __future__ import annotations

import pytest

from lookup.timeouts import (
    APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR,
    apple_music_lookup_timeout_s,
    probe_timeout_s_from_env,
)

_ENV = "LML_TEST_PROBE_TIMEOUT_MS"


class TestProbeTimeoutFromEnv:
    def test_unset_returns_default(self, monkeypatch):
        monkeypatch.delenv(_ENV, raising=False)
        assert probe_timeout_s_from_env(_ENV, 4.0) == 4.0

    def test_valid_ms_overrides_default(self, monkeypatch):
        monkeypatch.setenv(_ENV, "1500")
        assert probe_timeout_s_from_env(_ENV, 4.0) == 1.5

    @pytest.mark.parametrize("raw", ["abc", "", "1.5", "1e3"])
    def test_unparseable_falls_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv(_ENV, raw)
        assert probe_timeout_s_from_env(_ENV, 4.0) == 4.0

    @pytest.mark.parametrize("raw", ["0", "-1", "-1000"])
    def test_non_positive_falls_back_to_default(self, monkeypatch, raw):
        monkeypatch.setenv(_ENV, raw)
        assert probe_timeout_s_from_env(_ENV, 4.0) == 4.0


class TestAppleMusicLookupTimeout:
    """Back-compat wrapper: same env var, same 4.0s default as pre-LML#573."""

    def test_default_is_four_seconds(self, monkeypatch):
        monkeypatch.delenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, raising=False)
        assert apple_music_lookup_timeout_s() == 4.0

    def test_env_override_honored(self, monkeypatch):
        monkeypatch.setenv(APPLE_MUSIC_LOOKUP_TIMEOUT_ENV_VAR, "2500")
        assert apple_music_lookup_timeout_s() == 2.5
