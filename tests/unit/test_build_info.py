"""Unit tests for ``core.build_info`` (LML#1233).

The deployed-commit SHA was already resolved for ``/health``; LML#1233 needs
the same value on ``lookup_completed`` so a miss-rate movement can be broken
down by the commit that shipped it. This module is the extraction both call
sites share.

The load-bearing property here is that the function is **uncached and takes
its path explicitly**. ``/health`` calls it per request and the deploy gate
(``scripts/reconcile_deploy_via_health.sh``) reads that field, so a cached
value would be actively wrong there; and ``routers.health`` redirects the
path by monkeypatching its own module global, a seam that only survives if
the path is threaded through on every call rather than defaulted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.build_info import resolve_commit_sha


class TestResolutionOrder:
    """File beats env beats ``None`` -- the ``/health`` contract, unchanged."""

    def test_file_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The CI-baked file is authoritative even when the env var is set.

        The live deploy is a ``railway up`` source-deploy, which Railway does
        not tag with git metadata -- so the file is the only correct source
        when both are present.
        """
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "env0000")
        sha_file = tmp_path / "COMMIT_SHA"
        sha_file.write_text("abc123\n")
        assert resolve_commit_sha(sha_file) == "abc123"

    def test_env_used_when_file_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "env456")
        assert resolve_commit_sha(tmp_path / "COMMIT_SHA") == "env456"

    def test_none_when_neither(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        assert resolve_commit_sha(tmp_path / "COMMIT_SHA") is None


class TestDegradation:
    """A bad identity file must never raise -- ``/health`` cannot 500."""

    @pytest.mark.parametrize(
        "contents",
        ["", "   \n", "﻿\n"],
        ids=["empty", "whitespace", "bom-only"],
    )
    def test_blank_file_falls_through_to_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contents: str
    ) -> None:
        """Blank at any tier reads as absent.

        The documented contract is "null when unset", and downstream
        deploy-gate equality checks must never be fooled by ``""``.
        """
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "envfallback")
        sha_file = tmp_path / "COMMIT_SHA"
        sha_file.write_text(contents)
        assert resolve_commit_sha(sha_file) == "envfallback"

    def test_bom_is_stripped(self, tmp_path: Path) -> None:
        """A BOM-prefixed file must not break exact-equality deploy gates."""
        sha_file = tmp_path / "COMMIT_SHA"
        sha_file.write_bytes(b"\xef\xbb\xbfdeadbee\n")
        assert resolve_commit_sha(sha_file) == "deadbee"

    def test_directory_at_path_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSError (dir-at-path, permissions, missing) degrades, never raises."""
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        a_dir = tmp_path / "COMMIT_SHA"
        a_dir.mkdir()
        assert resolve_commit_sha(a_dir) is None

    def test_non_utf8_file_degrades(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A corrupt, non-UTF-8 file degrades to the env / None tiers."""
        monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
        sha_file = tmp_path / "COMMIT_SHA"
        sha_file.write_bytes(b"\xff\xfe\x00\x01")
        assert resolve_commit_sha(sha_file) is None


class TestUncached:
    """The property that keeps ``/health`` honest and the seam working."""

    def test_reflects_file_changes_between_calls(self, tmp_path: Path) -> None:
        """No caching inside the helper.

        ``/health`` calls this per request and the deploy gate reads the
        result; a value cached here would serve the pre-deploy SHA after a
        redeploy and quietly break that gate. Caching belongs at the lookup
        emit site, which binds once at module level by choice.
        """
        sha_file = tmp_path / "COMMIT_SHA"
        sha_file.write_text("first\n")
        assert resolve_commit_sha(sha_file) == "first"
        sha_file.write_text("second\n")
        assert resolve_commit_sha(sha_file) == "second"

    def test_path_is_required(self) -> None:
        """No default path.

        ``routers.health`` must pass its own module global through on every
        call, or its 18 ``monkeypatch.setattr(health, "COMMIT_SHA_PATH", ...)``
        sites would silently stop taking effect while still passing.
        """
        with pytest.raises(TypeError):
            resolve_commit_sha()  # type: ignore[call-arg]
