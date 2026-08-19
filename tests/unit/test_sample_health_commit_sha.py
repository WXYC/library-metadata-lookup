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


def test_a_commit_sha_containing_whitespace_prints_the_sentinel(tmp_path):
    """The caller writes this script's stdout into ``$GITHUB_OUTPUT`` as
    ``commit_sha=<value>``, and that file's format is line-oriented: a value
    carrying an embedded newline either breaks the step outright or smuggles a
    second, attacker-chosen ``name=value`` line into the step's outputs. The
    value comes from a remote HTTP response body, so "it's our own /health, it
    would never" is not a guarantee this script gets to rely on -- especially
    since it only runs when the deploy path is already misbehaving.

    A real commit SHA has no whitespace in it, so refusing any value that does
    costs nothing and makes the single-line guarantee the script's own property
    rather than the call site's assumption. It fails to the same sentinel as
    every other unusable reading: no baseline, decided downstream."""
    install_health_curl_stub(tmp_path, ['{"commit_sha": "abc123\\nmalicious=1"}'])

    result = _run(tmp_path, [_URL])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unreachable"
    assert "\n" not in result.stdout.rstrip("\n"), "stdout must be exactly one line"


def test_a_503_from_a_degraded_service_is_still_a_usable_baseline(tmp_path):
    """``/health`` answers **503 with a fully-populated body** whenever a core dependency is down
    -- ``routers/health.py`` ends with ``status_code = 200 if status in ("healthy", "degraded")
    else 503``, and ``CORE_SERVICES = {"database"}``, so a library.db that hasn't finished loading
    from the bucket is enough to trip it. The ``commit_sha`` in that body is exactly as accurate as
    the one in a 200.

    Discarding it would make this whole feature inert precisely when it is needed: a Railway
    incident that times out ``railway up`` is the same kind of event that leaves the live service
    degraded, and a sampler that returns ``unreachable`` there sends the reconciliation script
    straight down its unconditional fail-closed branch. The deploy identity is what is being read,
    not the service's health -- and the smoke-test job downstream is what judges health."""
    install_health_curl_stub(tmp_path, [(503, '{"status": "unhealthy", "commit_sha": "abc123"}')])

    result = _run(tmp_path, [_URL])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "abc123"


def test_an_unexpected_http_status_prints_the_sentinel(tmp_path):
    """502/504 from an edge proxy is genuinely "could not read /health" -- there is no application
    body behind it, so unlike a 503 it carries no deploy identity. Only the two statuses the app
    itself emits are trusted; everything else falls back to the sentinel."""
    install_health_curl_stub(tmp_path, [(502, "<html>Bad Gateway</html>")])

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
