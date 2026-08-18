"""Unit tests for the authenticated download path of ``scripts/generate_api_models.sh`` (LML#1205).

The Codegen Freshness CI job fetches ``api.yaml`` from raw.githubusercontent.com,
which rate-limits anonymous requests per source IP -- and Actions runners share
IP pools, so the job intermittently 429s. The fix authenticates the download
when an ambient ``GH_TOKEN`` / ``GITHUB_TOKEN`` is set, with three safety
properties pinned here:

- the token travels on curl's **stdin** (``-H @-``), never argv (visible in
  ``ps``) and never stdout/stderr;
- a failed authenticated attempt retries once anonymously -- GitHub 404s (not
  401s) raw requests carrying a stale token, so without the fallback an expired
  ambient token would turn previously-working local runs into hard 404s;
- the token is dropped from the environment at the top of the script, before
  the source-resolution branch, so no child process inherits it on EITHER arm
  -- the sibling-checkout arm never downloads, so a scrub placed inside the
  download branch misses the default local invocation entirely.

See the script's header comment for the canonical rationale; this module pins
the properties, it does not restate the reasoning.

Following the ``test_check_plan_links.py`` precedent, each test runs the real
script via subprocess -- hermetically: a stub ``curl`` first on ``PATH``
records each invocation's argv, stdin, and token-bearing environment (so no
network), ``--ref`` forces the download branch past any sibling wxyc-shared
checkout (one test instead copies the script under ``tmp_path`` to exercise the
unpinned ``main`` arm), and ``--download-only`` stops the script before the
codegen/ruff stages, which are out of scope here.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import TypedDict

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_api_models.sh"

_TOKEN = "ghs_lml1205_fake_token_for_tests"

# The org-pinned curl idiom for this exact download (mirrors wxyc-shared's
# generate-python-models.sh): --retry also covers 429/5xx with Retry-After,
# which directly serves #1205's goal.
_CURL_COMMON = ["-sSfL", "--max-time", "30", "--retry", "3"]


def _url(ref: str) -> str:
    return f"https://raw.githubusercontent.com/WXYC/wxyc-shared/{ref}/api.yaml"


def _install_curl_stub(tmp_path: Path) -> None:
    """Install a fake ``curl`` in ``tmp_path/bin`` that records every invocation.

    Call *n* writes ``curl_calls/<n>.argv`` (one argv element per line -- a
    header value like ``Authorization: Bearer x`` is a single element, spaces
    and all), ``<n>.stdin`` (whatever was piped in), and ``<n>.env`` (any
    ``GITHUB_TOKEN``/``GH_TOKEN`` lines in the child environment). Appending a
    new numbered record per call, rather than truncating one file, is what
    makes the auth-then-anonymous-fallback retry assertable as two distinct
    calls.

    Failure knob: if ``curl_fail_remaining`` exists, calls up to and including
    the number it contains exit 22 (curl's HTTP-error code) after recording --
    write ``1`` to fail only the first call, ``2`` to fail both. Compared
    against the call ordinal the stub already computes, so the marker file is
    read-only state rather than a decrementing counter.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'calls_dir="{tmp_path / "curl_calls"}"\n'
        f'fail_marker="{tmp_path / "curl_fail_remaining"}"\n'
        'mkdir -p "$calls_dir"\n'
        "n=$(find \"$calls_dir\" -name '*.argv' | wc -l | tr -d ' ')\n"
        "n=$((n + 1))\n"
        ': > "$calls_dir/$n.argv"\n'
        'for arg in "$@"; do printf "%s\\n" "$arg" >> "$calls_dir/$n.argv"; done\n'
        'cat > "$calls_dir/$n.stdin"\n'
        "env | grep -E '^(GITHUB_TOKEN|GH_TOKEN)=' > \"$calls_dir/$n.env\" || true\n"
        '[ -e "$fail_marker" ] && [ "$n" -le "$(cat "$fail_marker")" ] && exit 22\n'
        "exit 0\n"
    )
    stub.chmod(0o755)


def _install_env_recording_stub(tmp_path: Path, name: str) -> None:
    """Install a stub ``name`` on ``PATH`` that records its own environment.

    Used for the codegen/ruff stages, which the curl stub can't stand in for:
    the sibling-checkout arm never runs curl at all, so the token-scrub
    property has to be observed at the tools that actually run after it.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / name
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"env | grep -E '^(GITHUB_TOKEN|GH_TOKEN)=' > \"{tmp_path}/{name}.env\" || true\n"
        "exit 0\n"
    )
    stub.chmod(0o755)


def _set_up_sibling_checkout(tmp_path: Path) -> Path:
    """Lay out a repo + sibling ``wxyc-shared/api.yaml`` and return the script copy.

    The script resolves the sibling from its own location via
    ``git rev-parse --git-common-dir``, so the copy needs a real repo around
    it for the local-checkout arm to be selected.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    script_copy = repo / "scripts" / "generate_api_models.sh"
    shutil.copy(_SCRIPT, script_copy)
    sibling = tmp_path / "wxyc-shared"
    sibling.mkdir()
    (sibling / "api.yaml").write_text("openapi: 3.0.0\n")
    return script_copy


class _CurlCall(TypedDict):
    argv: list[str]
    stdin: str
    env: str


def _calls(tmp_path: Path) -> list[_CurlCall]:
    """Read back the stub's per-invocation records, in call order."""
    calls_dir = tmp_path / "curl_calls"
    if not calls_dir.is_dir():
        return []
    records: list[_CurlCall] = []
    for argv_file in sorted(calls_dir.glob("*.argv"), key=lambda p: int(p.stem)):
        records.append(
            _CurlCall(
                argv=argv_file.read_text().splitlines(),
                stdin=(calls_dir / f"{argv_file.stem}.stdin").read_text(),
                env=(calls_dir / f"{argv_file.stem}.env").read_text(),
            )
        )
    return records


def _run_download(
    tmp_path: Path,
    env_extra: dict[str, str],
    args: list[str] | None = None,
    script: Path = _SCRIPT,
) -> subprocess.CompletedProcess:
    """Run the script's download branch with the stub curl and a clean token env."""
    env = dict(os.environ)
    # CI itself exports GITHUB_TOKEN; strip both names so each test controls
    # exactly which token variables the script sees.
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(env_extra)
    return subprocess.run(
        [
            "bash",
            str(script),
            *(args if args is not None else ["--ref", "deadbeef", "--download-only"]),
        ],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_argv(argv: list[str], *, authenticated: bool, ref: str = "deadbeef") -> None:
    """The full curl invocation, asserted as one expression.

    The anonymous form is the pre-#1205 command plus the org-pinned
    --max-time/--retry options; the authenticated form is that command plus
    ``-H @-``, which reads the header from stdin so the token itself never
    rides argv. Both end in ``-o`` paired with a download target.
    """
    *head, o_flag, out_path = argv
    assert head == [*_CURL_COMMON, *(["-H", "@-"] if authenticated else []), _url(ref)]
    assert o_flag == "-o"
    assert out_path


@pytest.mark.parametrize("token_var", ["GH_TOKEN", "GITHUB_TOKEN"])
def test_token_attaches_authorization_header_without_leaking(tmp_path, token_var):
    """With a token set, curl receives the Bearer header on stdin, the log gets
    a stderr advisory (matching the script's other advisories) -- and the token
    value appears nowhere: not argv, not stdout, not stderr."""
    _install_curl_stub(tmp_path)

    result = _run_download(tmp_path, {token_var: _TOKEN})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    _assert_argv(calls[0]["argv"], authenticated=True)
    assert calls[0]["stdin"] == f"Authorization: Bearer {_TOKEN}\n"
    assert not any(_TOKEN in arg for arg in calls[0]["argv"])
    # Acceptance: the authenticated path is observable in the log...
    assert "Authenticated download" in result.stderr
    assert "Authenticated download" not in result.stdout
    # ...and the token is never echoed.
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr


def test_gh_token_takes_precedence_over_github_token(tmp_path):
    """When both variables are set, GH_TOKEN wins -- the same precedence gh
    itself documents (``gh help environment``)."""
    _install_curl_stub(tmp_path)
    other = "gho_lml1205_secondary_token"

    result = _run_download(tmp_path, {"GH_TOKEN": _TOKEN, "GITHUB_TOKEN": other})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    assert calls[0]["stdin"] == f"Authorization: Bearer {_TOKEN}\n"
    assert other not in calls[0]["stdin"]
    assert not any(other in arg for arg in calls[0]["argv"])


def test_no_token_sends_no_authorization_header(tmp_path):
    """Unset-token local runs behave exactly as before: one anonymous download,
    no Authorization header anywhere, no authenticated-path note.

    The anonymous arm still says so on stderr. Without that advisory, dropping
    ``GITHUB_TOKEN:`` from the workflow step would silently revert CI to the
    unauthenticated path #1205 exists to leave, presenting as the original
    intermittent 429 rather than as the configuration regression it is.
    """
    _install_curl_stub(tmp_path)

    result = _run_download(tmp_path, {})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    _assert_argv(calls[0]["argv"], authenticated=False)
    assert "Authorization" not in calls[0]["stdin"]
    assert not any("Authorization" in arg for arg in calls[0]["argv"])
    assert "Authenticated download" not in result.stdout + result.stderr
    assert "downloading anonymously" in result.stderr


def test_failed_authenticated_download_falls_back_to_anonymous(tmp_path):
    """A stale ambient token must not break previously-working runs: GitHub
    404s (not 401s) raw requests with any invalid Authorization header, so the
    script retries once anonymously and says so on stderr."""
    _install_curl_stub(tmp_path)
    (tmp_path / "curl_fail_remaining").write_text("1")

    result = _run_download(tmp_path, {"GH_TOKEN": _TOKEN})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 2
    _assert_argv(calls[0]["argv"], authenticated=True)
    _assert_argv(calls[1]["argv"], authenticated=False)
    assert "retrying anonymously" in result.stderr
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr


def test_failure_of_both_attempts_still_fails(tmp_path):
    """The anonymous retry is a floor, not a mask: when the anonymous attempt
    fails too (e.g. a genuinely bad --ref 404s either way), the script fails --
    after exactly one fallback, no retry loop."""
    _install_curl_stub(tmp_path)
    (tmp_path / "curl_fail_remaining").write_text("2")

    result = _run_download(tmp_path, {"GH_TOKEN": _TOKEN})

    assert result.returncode != 0
    assert len(_calls(tmp_path)) == 2


def test_unpinned_main_download_is_authenticated(tmp_path):
    """The unpinned arm -- REF defaulting to ``main``, the arm the Codegen
    Freshness job actually runs -- authenticates too; restricting auth to the
    pinned ``--ref`` arm would undo the fix where CI needs it.

    The sibling-checkout resolution keys off the script's own location, so the
    script is copied under ``tmp_path`` (with a ``git init`` making resolution
    deterministic) to force the download branch without ``--ref``.
    """
    _install_curl_stub(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_copy = scripts_dir / "generate_api_models.sh"
    shutil.copy(_SCRIPT, script_copy)

    result = _run_download(
        tmp_path, {"GH_TOKEN": _TOKEN}, args=["--download-only"], script=script_copy
    )

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    _assert_argv(calls[0]["argv"], authenticated=True, ref="main")
    assert calls[0]["stdin"] == f"Authorization: Bearer {_TOKEN}\n"


def test_token_is_dropped_from_child_process_environment(tmp_path):
    """The token's scope is the download alone: the script captures it into a
    shell variable and unsets both env names, so no child process -- curl here,
    standing in for the codegen/ruff stages that follow -- inherits it."""
    _install_curl_stub(tmp_path)

    result = _run_download(tmp_path, {"GH_TOKEN": _TOKEN, "GITHUB_TOKEN": _TOKEN})

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert len(calls) == 1
    assert calls[0]["env"] == ""


@pytest.mark.parametrize("tool", ["datamodel-codegen", "ruff"])
def test_token_is_dropped_on_the_sibling_checkout_path_too(tmp_path, tool):
    """The scrub is a property of the whole script, not of the download arm.

    docs/env-vars.md, docs/scripts.md and the ci.yml comment all state it
    unconditionally ("unsets both variables before the codegen toolchain
    runs"), and the default local invocation -- an ambient GH_TOKEN plus a
    sibling wxyc-shared checkout -- takes the arm that never downloads. If the
    capture/unset lives inside the download branch, that arm hands the token
    to datamodel-codegen, ruff, and their whole dependency tree.
    """
    _install_env_recording_stub(tmp_path, "datamodel-codegen")
    _install_env_recording_stub(tmp_path, "ruff")
    script_copy = _set_up_sibling_checkout(tmp_path)

    result = _run_download(
        tmp_path, {"GH_TOKEN": _TOKEN, "GITHUB_TOKEN": _TOKEN}, args=[], script=script_copy
    )

    assert result.returncode == 0, result.stderr
    assert "Using local api.yaml" in result.stdout, "fixture must exercise the sibling arm"
    assert (tmp_path / f"{tool}.env").read_text() == ""
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr
