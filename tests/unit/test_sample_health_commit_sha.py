"""Unit tests for ``scripts/sample_health_commit_sha.sh`` (LML#1231).

The reconciliation path added for LML#1231 needs a *pre-upload* reading of
``/health``'s ``commit_sha`` -- captured before ``railway up`` even runs -- so
that a later "did it recover" check can require a genuine transition instead of
trivially matching a SHA that was already live. This script is that one-shot,
best-effort sampler: a CI step calls it once per deploy job, before the Railway
CLI runs, and stores its stdout as a step output for
``scripts/reconcile_deploy_via_health.sh`` to consume later.

It is deliberately never allowed to fail the calling step: an unreachable or
malformed ``/health`` is exactly the kind of thing this whole feature exists to
tolerate, so the script swallows every such case and prints the sentinel
``unreachable`` instead of a SHA. The only failure this script reports is a
programming error at the call site (missing URL argument).
"""

import os
import subprocess
from pathlib import Path

from tests.health_curl_stub import health_curl_call_argv, install_health_curl_stub

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "sample_health_commit_sha.sh"

_URL = "https://library-metadata-lookup-staging.up.railway.app/health"


def _run(tmp_path: Path, args: list[str], env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_prints_the_commit_sha_from_a_healthy_response(tmp_path):
    install_health_curl_stub(tmp_path, ['{"status": "healthy", "commit_sha": "abc123"}'])

    result = _run(tmp_path, [_URL])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "abc123"


def test_unreachable_health_prints_the_sentinel_and_still_exits_zero(tmp_path):
    """A curl failure (network error, timeout, non-2xx) must not fail the calling
    step -- the pre-sample is advisory, and the reconciliation script is the one
    that decides what an unreadable baseline means for the job's outcome."""
    install_health_curl_stub(tmp_path, [None])

    result = _run(tmp_path, [_URL])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unreachable"


def test_missing_commit_sha_field_prints_the_sentinel(tmp_path):
    """A 200 with a body that has no commit_sha (or a null one) is not a usable
    baseline either -- treat it the same as unreachable rather than comparing
    against a Python-truthy-but-meaningless empty string later."""
    install_health_curl_stub(tmp_path, ['{"status": "healthy", "commit_sha": null}'])

    result = _run(tmp_path, [_URL])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unreachable"


def test_malformed_json_prints_the_sentinel(tmp_path):
    install_health_curl_stub(tmp_path, ["not json at all"])

    result = _run(tmp_path, [_URL])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unreachable"


def test_missing_url_argument_is_a_usage_error(tmp_path):
    install_health_curl_stub(tmp_path, ["irrelevant"])

    result = _run(tmp_path, [])

    assert result.returncode == 2
    assert "usage" in result.stderr.lower()


def test_curl_timeout_env_var_reaches_the_curl_invocation(tmp_path):
    """Pin that the knob actually reaches curl's ``--max-time`` rather than a
    typo'd env var name silently falling back to the default on every run."""
    install_health_curl_stub(tmp_path, ['{"commit_sha": "abc123"}'])

    result = _run(tmp_path, [_URL], {"HEALTH_SAMPLE_CURL_TIMEOUT_SECONDS": "1"})

    assert result.returncode == 0, result.stderr
    argv = health_curl_call_argv(tmp_path, 1)
    assert "--max-time" in argv
    assert argv[argv.index("--max-time") + 1] == "1"
