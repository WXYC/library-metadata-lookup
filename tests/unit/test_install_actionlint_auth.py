"""Unit tests for ``scripts/install_actionlint.sh`` (LML#1214).

The Workflow Lint job installs actionlint by fetching rhysd/actionlint's own
install script from raw.githubusercontent.com -- the same surface, and the same
per-IP anonymous rate budget, that made the Codegen Freshness job intermittently
429 (LML#1205). The aggravating detail is that this job's ``paths`` filter fires
precisely on PRs touching ``.github/workflows/**``, so a 429 reads as "your CI
change is broken" rather than as runner-IP luck.

The fetch moved out of the workflow's inline ``run:`` block and into a script so
these properties could be pinned by tests at all, mirroring
``scripts/check_plan_links.sh`` (called from ``plan-links.yml``) and tested with
the same hermetic stub-``curl``-on-``PATH`` harness as
``tests/unit/test_generate_api_models_auth.py``.

Properties pinned here -- see the script's header for the reasoning, which this
module does not restate:

- the token travels on curl's **stdin** (``-H @-``), never argv (``ps``-visible)
  and never stdout/stderr;
- a failed authenticated attempt retries once anonymously, unconditionally, so
  pre-fix anonymous behavior stays the floor;
- the token is dropped from the environment before anything else runs -- which
  matters more here than in the codegen script, because the child process is a
  **third-party script fetched over the network moments earlier**, and it has no
  business seeing the job's ``GITHUB_TOKEN``;
- the ``ACTIONLINT_VERSION`` pin reaches both the fetched URL and the downloaded
  installer, so the script tag and the installed binary cannot drift apart.

One test also asserts the workflow still calls the script, so extracting the
logic cannot leave an orphan that nothing runs.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.curl_stub import curl_calls, install_curl_stub

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "install_actionlint.sh"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "actionlint.yml"

_TOKEN = "ghs_lml1214_fake_token_for_tests"
_VERSION = "1.7.12"

# The org-pinned curl idiom for a raw.githubusercontent.com download, shared
# with scripts/generate_api_models.sh: --retry also covers 429/5xx and honors
# Retry-After, which is the whole point of #1214.
_CURL_COMMON = ["-sSfL", "--max-time", "30", "--retry", "3", "--retry-max-time", "60"]


def _url(version: str = _VERSION) -> str:
    return (
        f"https://raw.githubusercontent.com/rhysd/actionlint/v{version}"
        "/scripts/download-actionlint.bash"
    )


def _install_stand_in_installer(tmp_path: Path) -> None:
    """Park a recording stand-in where the script will look for the installer.

    ``install_actionlint.sh`` executes what it fetched, so unlike the codegen
    suite this one cannot stop before the download is consumed. The stand-in
    records its own argv and environment, which is how the version-pin and
    token-scrub properties become observable at the child process.

    Parked up front rather than "downloaded" by the stub curl, so that a run
    whose fetches all failed still leaves a runnable file here. Otherwise
    test_failure_of_both_attempts_still_fails cannot fail: with nothing at this
    path, a script that had lost its error handling would die on `bash: no such
    file` and leave no installer.argv either, which is exactly what that test
    reads as success.
    """
    (tmp_path / "download-actionlint.bash").write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{tmp_path}/installer.argv"\n'
        f"env | grep -E '^(GITHUB_TOKEN|GH_TOKEN)=' > \"{tmp_path}/installer.env\" || true\n"
        "exit 0\n"
    )


def _installer_argv(tmp_path: Path) -> list[str]:
    return (tmp_path / "installer.argv").read_text().splitlines()


def _run_install(
    tmp_path: Path,
    env_extra: dict[str, str],
    args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    """Run the real script with the stub curl and a deterministic token env."""
    env = dict(os.environ)
    # CI itself exports GITHUB_TOKEN; strip both names so each test controls
    # exactly which token variables the script sees.
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env["RUNNER_TEMP"] = str(tmp_path)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(_SCRIPT), *(args if args is not None else [_VERSION])],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_argv(
    tmp_path: Path, argv: list[str], *, authenticated: bool, version: str = _VERSION
) -> None:
    """The full curl invocation, asserted as one expression.

    The anonymous form is the pre-#1214 command plus the org-pinned
    --max-time/--retry options; the authenticated form is that command plus
    ``-H @-``, which reads the header from stdin so the token never rides argv.
    Both end in ``-o`` paired with a download target.
    """
    *head, o_flag, out_path = argv
    assert head == [*_CURL_COMMON, *(["-H", "@-"] if authenticated else []), _url(version)]
    assert o_flag == "-o"
    # Not merely truthy: the RUNNER_TEMP default is why CI's checkout does not
    # end up with the installer script dropped in it, and nothing else pins it.
    assert out_path == str(tmp_path / "download-actionlint.bash")


@pytest.mark.parametrize("token_var", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_token_attaches_authorization_header_without_leaking(tmp_path, token_var):
    """With a token set, curl receives the Bearer header on stdin, the log gets a
    stderr advisory -- and the token value appears nowhere: not argv, not
    stdout, not stderr."""
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)

    result = _run_install(tmp_path, {token_var: _TOKEN})

    assert result.returncode == 0, result.stderr
    calls = curl_calls(tmp_path)
    assert len(calls) == 1
    _assert_argv(tmp_path, calls[0]["argv"], authenticated=True)
    assert calls[0]["stdin"] == f"Authorization: Bearer {_TOKEN}\n"
    assert not any(_TOKEN in arg for arg in calls[0]["argv"])
    assert "Authenticated download" in result.stderr
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr


def test_gh_token_takes_precedence_over_github_token(tmp_path):
    """When both are set, GH_TOKEN wins -- the precedence gh itself documents
    (``gh help environment``), matching scripts/generate_api_models.sh."""
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)
    other = "gho_lml1214_secondary_token"

    result = _run_install(tmp_path, {"GH_TOKEN": _TOKEN, "GITHUB_TOKEN": other})

    assert result.returncode == 0, result.stderr
    calls = curl_calls(tmp_path)
    assert len(calls) == 1
    assert calls[0]["stdin"] == f"Authorization: Bearer {_TOKEN}\n"
    assert other not in calls[0]["stdin"]
    assert not any(other in arg for arg in calls[0]["argv"])


def test_no_token_sends_no_authorization_header(tmp_path):
    """Unset-token runs behave as before: one anonymous download, no
    Authorization header anywhere, and a stderr advisory saying so.

    Without that advisory, dropping the token from the workflow step silently
    reverts CI to the anonymous path #1214 exists to leave, and the regression
    presents as the original intermittent 429 rather than as the configuration
    mistake it is.
    """
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)

    result = _run_install(tmp_path, {})

    assert result.returncode == 0, result.stderr
    calls = curl_calls(tmp_path)
    assert len(calls) == 1
    _assert_argv(tmp_path, calls[0]["argv"], authenticated=False)
    assert "Authorization" not in calls[0]["stdin"]
    assert not any("Authorization" in arg for arg in calls[0]["argv"])
    assert "Authenticated download" not in result.stdout + result.stderr
    assert "downloading anonymously" in result.stderr


def test_failed_authenticated_download_falls_back_to_anonymous(tmp_path):
    """A stale ambient token must not break a previously-working install: the
    script retries once anonymously and says so on stderr."""
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)
    (tmp_path / "curl_fail_remaining").write_text("1")

    result = _run_install(tmp_path, {"GH_TOKEN": _TOKEN})

    assert result.returncode == 0, result.stderr
    calls = curl_calls(tmp_path)
    assert len(calls) == 2
    _assert_argv(tmp_path, calls[0]["argv"], authenticated=True)
    _assert_argv(tmp_path, calls[1]["argv"], authenticated=False)
    assert "::warning::" in result.stderr, (
        "the fallback must reach the run summary; a stderr-only note in a "
        "sub-second advisory job is how a permanently-broken token stays green"
    )
    assert "falling back to the shared anonymous rate budget" in result.stderr
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr


def test_failure_of_both_attempts_still_fails(tmp_path):
    """The anonymous retry is a floor, not a mask: when it fails too, the script
    fails -- after exactly one fallback, no retry loop -- rather than running the
    lint step against a missing binary."""
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)
    (tmp_path / "curl_fail_remaining").write_text("2")

    result = _run_install(tmp_path, {"GH_TOKEN": _TOKEN})

    assert result.returncode != 0
    assert len(curl_calls(tmp_path)) == 2
    assert not (tmp_path / "installer.argv").exists(), "a failed fetch must not reach the installer"


def test_token_is_dropped_before_the_downloaded_installer_runs(tmp_path):
    """The token's scope is the fetch alone.

    Stronger than the codegen script's equivalent property: the child here is a
    third-party script fetched over the network seconds earlier, so handing it
    the job's GITHUB_TOKEN would widen the blast radius of any upstream
    compromise from "lints our workflows" to "holds our token".
    """
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)

    result = _run_install(tmp_path, {"GH_TOKEN": _TOKEN, "GITHUB_TOKEN": _TOKEN})

    assert result.returncode == 0, result.stderr
    assert curl_calls(tmp_path)[0]["env"] == ""
    assert (tmp_path / "installer.env").read_text() == ""


def test_version_pin_reaches_both_the_url_and_the_installer(tmp_path):
    """One version value drives the script tag and the installed binary, so the
    two cannot drift apart -- the property the workflow's ACTIONLINT_VERSION
    comment promises."""
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)
    pinned = "1.7.9"

    result = _run_install(tmp_path, {}, args=[pinned])

    assert result.returncode == 0, result.stderr
    _assert_argv(tmp_path, curl_calls(tmp_path)[0]["argv"], authenticated=False, version=pinned)
    assert _installer_argv(tmp_path) == [pinned]


@pytest.mark.parametrize(
    "bad_version",
    ["", "v1.7.12", "1.7", "latest", "1.7.12-beta"],
    ids=["empty", "leading_v", "two_part", "latest", "prerelease"],
)
def test_a_version_that_is_not_exact_major_minor_patch_is_refused(tmp_path, bad_version):
    """Refuse before spending a request, rather than 404ing four retries deep.

    An omitted or malformed version does NOT reach rhysd's "latest" -- that
    script has no network resolution at all; its v1.7.12 copy hardcodes
    version="1.7.11" as a manually-maintained default, and "latest" is a synonym
    for it. So the real hazard is quieter than an unpin: the workflow would
    declare one tag while a differently-pinned binary got installed. `v1.7.12`
    is the shape most likely to be typed, since that is how the release page
    displays it.
    """
    install_curl_stub(tmp_path)
    _install_stand_in_installer(tmp_path)

    result = _run_install(tmp_path, {}, args=[bad_version] if bad_version else [])

    assert result.returncode != 0
    assert curl_calls(tmp_path) == [], "a malformed version must not spend a request"
    assert "version" in result.stderr.lower()


def test_an_empty_download_does_not_pass_as_a_successful_install(tmp_path):
    """curl reports a 200 carrying an empty body as success. Executing that is a
    no-op that never writes the `executable` step output, so the failure lands
    at the lint step as `command not found` -- exactly the confusing symptom the
    fetch-to-file design exists to avoid."""
    install_curl_stub(tmp_path)
    (tmp_path / "download-actionlint.bash").write_text("")

    result = _run_install(tmp_path, {})

    assert result.returncode != 0
    assert "empty" in result.stderr.lower()


def test_workflow_invokes_the_script():
    """The extraction must not leave an orphan: Workflow Lint still has to run
    the script, passing its pinned version, and must not have kept a second
    inline fetch alongside it."""
    workflow = _WORKFLOW.read_text()
    # Comments legitimately discuss the download and its rationale; only
    # executable lines are evidence of a second fetch.
    executable = "\n".join(
        line for line in workflow.splitlines() if not line.lstrip().startswith("#")
    )

    assert "bash scripts/install_actionlint.sh" in executable
    assert re.search(r'ACTIONLINT_VERSION:\s*"?\d+\.\d+\.\d+"?', executable), (
        "the version pin must still live in the workflow"
    )
    assert "curl" not in executable, (
        "the raw.githubusercontent.com fetch belongs in the script, where it is tested"
    )


def test_the_installing_step_itself_receives_a_token():
    """Scoped to the step that runs the script, and accepting either token name.

    A whole-file substring match is both too loose and too brittle to pin this:
    it stays green when the `env:` block migrates to a neighbouring step (the
    exact silent revert to anonymous that #1214 exists to prevent), and it goes
    red on a rename to GITHUB_TOKEN, which the script accepts and which is the
    name the sibling codegen job already uses.

    Scraped rather than YAML-parsed because PyYAML is not a declared dependency
    of this repo -- it is importable only as a transitive one, so a parse here
    would rest on someone else's dependency tree.
    """
    lines = _WORKFLOW.read_text().splitlines()
    # The `run:` line specifically -- the two `paths:` entries and the block
    # comment above the step also name the script.
    starts = [
        i
        for i, line in enumerate(lines)
        if "install_actionlint.sh" in line and line.lstrip().startswith("run:")
    ]
    assert len(starts) == 1, f"expected exactly one step running the script, found {len(starts)}"

    indent = len(lines[starts[0]]) - len(lines[starts[0]].lstrip())
    step: list[str] = []
    for line in lines[starts[0] + 1 :]:
        stripped = line.strip()
        # A sibling step begins at the same indent with a list marker; the
        # step's own keys are indented at least as far.
        if stripped.startswith("- ") and len(line) - len(line.lstrip()) < indent:
            break
        step.append(line)

    assert re.search(
        r"^\s*(GH_TOKEN|GITHUB_TOKEN):\s*\$\{\{\s*github\.token\s*\}\}\s*$", "\n".join(step), re.M
    ), (
        "the step that installs actionlint no longer receives a token; CI is "
        "back on the shared anonymous rate budget (#1214)"
    )
