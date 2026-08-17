"""Unit tests for the authenticated download path of ``scripts/generate_api_models.sh`` (LML#1205).

The Codegen Freshness CI job fetches ``api.yaml`` from raw.githubusercontent.com,
which rate-limits anonymous requests per source IP -- and Actions runners share
IP pools, so the job intermittently 429s. The fix attaches a Bearer
``Authorization`` header when ``GITHUB_TOKEN`` (or ``GH_TOKEN``) is set, while
unset-token local runs must behave exactly as before and the token value must
never reach the script's output.

Following the ``test_check_plan_links.py`` precedent, each test runs the real
script via subprocess -- hermetically: a stub ``curl`` first on ``PATH`` records
its argv (so no network), ``--ref`` forces the download branch past any sibling
wxyc-shared checkout, and ``--download-only`` stops the script before the
codegen/ruff stages, which are out of scope here.
"""

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_api_models.sh"

_TOKEN = "ghs_lml1205_fake_token_for_tests"


def _write_curl_stub(tmp_path: Path) -> Path:
    """Install a fake ``curl`` in ``tmp_path/bin`` that records its argv.

    Each argv element lands on its own line of ``curl_argv.txt`` (a header
    value like ``Authorization: Bearer x`` is a single element, spaces and
    all), and a dummy payload is written to the ``-o`` target so the script's
    post-download steps see a file.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_file = tmp_path / "curl_argv.txt"
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'argv_file="{argv_file}"\n'
        ': > "$argv_file"\n'
        'out=""\n'
        'prev=""\n'
        'for arg in "$@"; do\n'
        '    printf "%s\\n" "$arg" >> "$argv_file"\n'
        '    if [ "$prev" = "-o" ]; then out="$arg"; fi\n'
        '    prev="$arg"\n'
        "done\n"
        'if [ -n "$out" ]; then printf "openapi: 3.0.0\\n" > "$out"; fi\n'
    )
    stub.chmod(0o755)
    return argv_file


def _run_download(tmp_path: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the script's download branch with the stub curl and a clean token env."""
    env = dict(os.environ)
    # CI itself exports GITHUB_TOKEN; strip both names so each test controls
    # exactly which token variables the script sees.
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(_SCRIPT), "--ref", "deadbeef", "--download-only"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("token_var", ["GITHUB_TOKEN", "GH_TOKEN"])
def test_token_attaches_authorization_header_without_leaking(tmp_path, token_var):
    """With a token set, curl gets the Bearer header and the log gets a note --
    but the token value itself never appears on stdout or stderr."""
    argv_file = _write_curl_stub(tmp_path)

    result = _run_download(tmp_path, {token_var: _TOKEN})

    assert result.returncode == 0, result.stderr
    argv = argv_file.read_text().splitlines()
    assert f"Authorization: Bearer {_TOKEN}" in argv
    assert argv[argv.index(f"Authorization: Bearer {_TOKEN}") - 1] == "-H"
    # Acceptance: the authenticated path is observable in the log...
    assert "Authenticated download" in result.stdout
    # ...and the token is never echoed.
    assert _TOKEN not in result.stdout
    assert _TOKEN not in result.stderr


def test_github_token_takes_precedence_over_gh_token(tmp_path):
    """When both variables are set, the issue's primary name (GITHUB_TOKEN) wins."""
    argv_file = _write_curl_stub(tmp_path)
    other = "gho_lml1205_secondary_token"

    result = _run_download(tmp_path, {"GITHUB_TOKEN": _TOKEN, "GH_TOKEN": other})

    assert result.returncode == 0, result.stderr
    argv = argv_file.read_text().splitlines()
    assert f"Authorization: Bearer {_TOKEN}" in argv
    assert not any(other in arg for arg in argv)


def test_no_token_sends_no_authorization_header(tmp_path):
    """Unset-token local runs behave exactly as before: anonymous download,
    no Authorization header, no authenticated-path note."""
    argv_file = _write_curl_stub(tmp_path)

    result = _run_download(tmp_path, {})

    assert result.returncode == 0, result.stderr
    argv = argv_file.read_text().splitlines()
    assert not any("Authorization" in arg for arg in argv)
    assert "Authenticated download" not in result.stdout
