"""Unit tests for ``scripts/reconcile_deploy_via_health.sh`` (LML#1231).

This is the reconciliation path for the one failure mode
``scripts/wait_for_railway_deployment.sh`` cannot see at all: a `railway up`
HTTP request that itself fails (a client-side network timeout on the upload) so
no ``deploymentId`` is ever produced. When that happens, ``Deploy to Railway``
exits non-zero and the wait step -- which needs that id -- gets skipped
entirely, red-lining a job even when Railway accepted the upload and the
revision went live a few minutes later. See the issue for the 2026-08-18
incident this reproduces (staging serving ``01a77f0`` about four minutes after
the CI step reported a network timeout).

**The trap this script exists to close, and the reason most of these tests
exist**: the naive fix -- "is the expected SHA live at /health?" -- passes
trivially on a re-run where that SHA is *already* serving from an earlier,
unrelated successful deploy. That would turn a genuinely failed upload green.
The check implemented here instead requires a *transition*: the caller samples
``/health``'s ``commit_sha`` *before* running ``railway up`` and passes that
reading in as ``pre-upload-sha``. Recovery is only reported when that pre-upload
reading differed from the expected SHA and later polling observes it become the
expected SHA. Two related "already inconclusive" cases fail closed without ever
touching the network: an already-matching pre-upload SHA (the trap itself), and
a pre-upload SHA that could not be sampled at all (the sentinel ``unreachable``
that ``scripts/sample_health_commit_sha.sh`` prints on its own failure) --
neither can prove a transition happened, so neither is allowed to pass.
"""

import os
import re
import subprocess
from pathlib import Path

from tests.health_curl_stub import health_curl_call_count, install_health_curl_stub

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "reconcile_deploy_via_health.sh"

_URL = "https://library-metadata-lookup-staging.up.railway.app/health"
_EXPECTED_SHA = "01a77f02fdd9b9833c9926582c7e9da4d88db3cb"
_OLD_SHA = "e67049c1a5e4c2d3b1a09876fedcba9876543210"

# Fast, deterministic bounds for tests: poll immediately (no real sleep) and give
# up quickly, since these scripts only care about *order* of responses, not
# wall-clock timing -- see health_curl_stub's docstring for why a short list of
# canned responses is enough to express "recovers on the Nth poll" or "never
# recovers" without a test needing to know the exact number of polls that occur.
_FAST_ENV = {
    "RECONCILE_HEALTH_TIMEOUT_SECONDS": "2",
    "RECONCILE_HEALTH_POLL_INTERVAL_SECONDS": "0",
}


def _health_body(sha: str) -> str:
    return f'{{"status": "healthy", "commit_sha": "{sha}"}}'


def _run(tmp_path: Path, args: list[str], env_extra: dict[str, str] | None = None):
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(_FAST_ENV)
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_recovered_deploy_reports_success_once_the_sha_transitions(tmp_path):
    """The core recovered-deploy case: /health was on the old SHA before the
    upload attempt, and later polling observes it flip to the expected one."""
    install_health_curl_stub(
        tmp_path, [_health_body(_OLD_SHA), _health_body(_OLD_SHA), _health_body(_EXPECTED_SHA)]
    )

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, _OLD_SHA])

    assert result.returncode == 0, result.stderr
    assert "RECOVERED" in result.stderr or "recovered" in result.stderr.lower()


def test_upload_that_never_landed_still_reports_failure(tmp_path):
    """/health never moves off the old SHA -- the upload genuinely never landed,
    and the reconciliation window must time out red rather than hang or guess."""
    install_health_curl_stub(tmp_path, [_health_body(_OLD_SHA)])

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, _OLD_SHA])

    assert result.returncode == 1
    assert health_curl_call_count(tmp_path) > 1, "must actually poll, not give up after one try"


def test_already_matching_pre_upload_sha_is_inconclusive_not_green(tmp_path):
    """The trap itself: if the expected SHA was ALREADY live before this deploy
    attempt (the typical shape of a re-run), a later '/health matches' reading
    proves nothing about whether *this* upload landed. Must fail closed, and
    must do so without spending the poll budget -- there is nothing to wait for
    when the starting state already can't distinguish success from no-op."""
    install_health_curl_stub(tmp_path, [_health_body(_EXPECTED_SHA)])

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, _EXPECTED_SHA])

    assert result.returncode == 1
    assert "INCONCLUSIVE" in result.stderr or "inconclusive" in result.stderr.lower()
    assert health_curl_call_count(tmp_path) == 0, (
        "an already-matching baseline is decidable from the arguments alone; "
        "polling /health at all would just burn CI time on a foregone conclusion"
    )


def test_unreachable_pre_upload_baseline_is_inconclusive_not_green(tmp_path):
    """If the pre-upload sample itself failed (the sentinel
    scripts/sample_health_commit_sha.sh prints on its own failure), there is no
    baseline to prove a transition against, so this must fail closed exactly
    like the already-matching case -- not silently treat 'unknown' as 'green
    light to poll and hope'."""
    install_health_curl_stub(tmp_path, [_health_body(_EXPECTED_SHA)])

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, "unreachable"])

    assert result.returncode == 1
    assert "INCONCLUSIVE" in result.stderr or "inconclusive" in result.stderr.lower()
    assert health_curl_call_count(tmp_path) == 0


def test_health_unreachable_throughout_the_window_reports_failure(tmp_path):
    """A legitimate pre-upload baseline exists, but /health cannot be reached at
    all during the reconciliation window -- must not be mistaken for recovery,
    and must not hang past the configured timeout."""
    install_health_curl_stub(tmp_path, [None])

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, _OLD_SHA])

    assert result.returncode == 1
    assert health_curl_call_count(tmp_path) > 1


def test_transient_health_errors_during_polling_are_tolerated(tmp_path):
    """A single dropped poll must not be fatal on its own -- only failing to
    ever observe the expected SHA within the timeout is. Mirrors
    wait_for_railway_deployment.sh's tolerance of transient polling blips."""
    install_health_curl_stub(tmp_path, [_health_body(_OLD_SHA), None, _health_body(_EXPECTED_SHA)])

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, _OLD_SHA])

    assert result.returncode == 0, result.stderr


def test_a_degraded_service_returning_503_still_proves_the_transition(tmp_path):
    """The polling half of the same point ``sample_health_commit_sha.sh`` makes: ``/health``
    answers 503 with a fully-populated body when a core dependency is down, and the ``commit_sha``
    in it is exactly as accurate as the one in a 200. A revision that has genuinely landed and is
    serving its own SHA while its database check is still failing HAS transitioned -- the upload
    landed, which is the only question this script is asked. Whether the service is healthy is the
    smoke-test job's call, downstream, and it makes that call on its own."""
    install_health_curl_stub(
        tmp_path,
        [
            _health_body(_OLD_SHA),
            (503, f'{{"status": "unhealthy", "commit_sha": "{_EXPECTED_SHA}"}}'),
        ],
    )

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, _OLD_SHA])

    assert result.returncode == 0, result.stderr


def test_an_empty_baseline_is_inconclusive_rather_than_a_usage_error(tmp_path):
    """The CI step that produces the baseline (`echo "commit_sha=$(...)" >> "$GITHUB_OUTPUT"`)
    always exits 0, so it can hand this script an *empty* third argument -- if the sampler script
    were missing from the checkout, say, or exited on its own usage error. That is a runtime
    condition with the same meaning as the ``unreachable`` sentinel (no baseline, so no provable
    transition), not a call-site programming error.

    Reporting it as exit 2 "usage" would be a lie about which of the script's two documented
    failure meanings applies, and would send whoever reads the log hunting for a malformed
    workflow step instead of an unreadable /health. The job is red either way; this is about the
    log telling the truth."""
    install_health_curl_stub(tmp_path, [_health_body(_OLD_SHA)])

    result = _run(tmp_path, [_URL, _EXPECTED_SHA, ""])

    assert result.returncode == 1
    assert "INCONCLUSIVE" in result.stderr or "inconclusive" in result.stderr.lower()
    assert health_curl_call_count(tmp_path) == 0


def test_missing_arguments_is_a_usage_error(tmp_path):
    install_health_curl_stub(tmp_path, ["irrelevant"])

    result = _run(tmp_path, [_URL, _EXPECTED_SHA])

    assert result.returncode == 2
    assert "usage" in result.stderr.lower()
    assert health_curl_call_count(tmp_path) == 0


def test_timeout_is_configurable(tmp_path):
    """A near-zero timeout must still make at least one attempt before giving
    up -- pins that the env var is actually read, not merely documented."""
    install_health_curl_stub(tmp_path, [_health_body(_OLD_SHA)])

    result = _run(
        tmp_path,
        [_URL, _EXPECTED_SHA, _OLD_SHA],
        {"RECONCILE_HEALTH_TIMEOUT_SECONDS": "0"},
    )

    assert result.returncode == 1
    assert health_curl_call_count(tmp_path) >= 1


def test_the_reconciliation_window_is_at_least_the_wait_scripts_deploy_budget():
    """Both scripts are waiting for the same physical event -- a Railway revision finishing its
    build and starting to serve -- so the reconciliation path must not give it a smaller budget
    than ``wait_for_railway_deployment.sh`` does.

    The reconciliation clock is in fact the *worse* positioned of the two. The wait script starts
    counting once the upload has already been accepted; this script starts counting from an upload
    that failed client-side, so its window has to cover the build and release that hadn't even been
    queued yet. Giving the harder case the smaller budget is backwards, and the failure it produces
    is a *timeout reported as "the upload never landed"* -- i.e. exactly the false red this whole
    feature exists to remove, just relocated. The one measurement available (the 2026-08-18
    incident) was ~240s from failure to the new SHA serving, which a 300s window clears by only 25%
    -- one slower-than-usual Docker build away from re-introducing the bug.

    Pinned as a static comparison of the two defaults rather than a timing test: the property that
    matters is "these two budgets agree about how long a deploy may legitimately take," and
    sleeping 900s in a unit test to prove it would be absurd.
    """
    reconcile = _SCRIPT.read_text()
    wait = (_REPO_ROOT / "scripts" / "wait_for_railway_deployment.sh").read_text()

    reconcile_default = re.search(r"RECONCILE_HEALTH_TIMEOUT_SECONDS:-(\d+)\}", reconcile)
    wait_default = re.search(r"RAILWAY_DEPLOY_TIMEOUT_SECONDS:-(\d+)\}", wait)
    assert reconcile_default and wait_default, "both scripts must declare a default timeout"

    assert int(reconcile_default.group(1)) >= int(wait_default.group(1)), (
        f"reconciliation budget {reconcile_default.group(1)}s is smaller than the wait script's "
        f"{wait_default.group(1)}s for the same event, from a strictly earlier starting line"
    )
