"""LML#747: the container entrypoint must safely honor ``UVICORN_WORKERS``.

Wiring the worker count as a Railway env var makes horizontal scaling a
no-redeploy lever, while defaulting to 1 keeps today's single-worker behavior
byte-for-byte until the var is set. The prereqs for setting it >1 (the shared
Discogs 60/min token: enable ``DISCOGS_RATE_BUCKET_ENABLED`` + divide or bound
``DISCOGS_RATE_LIMIT`` for the fail-open window) live in the horizontal-scaling
runbook (``docs/deployment.md``).

The entrypoint is the last line before uvicorn binds a port, so this test runs
the shell script with ``su`` stubbed: a malformed ``UVICORN_WORKERS`` must fall
back to 1 with a WARN instead of crash-looping the container before bind.
"""

import os
import subprocess
from pathlib import Path

import pytest

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def _run_entrypoint(tmp_path: Path, workers: str | None) -> tuple[subprocess.CompletedProcess, str]:
    """Run entrypoint.sh with a stubbed ``su`` and return (process, su args)."""
    su_args_path = tmp_path / "su-args.txt"
    su_stub = tmp_path / "su"
    su_stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$SU_ARGS_OUT"\n')
    su_stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"
    env["PORT"] = "8123"
    env["SU_ARGS_OUT"] = str(su_args_path)
    if workers is None:
        env.pop("UVICORN_WORKERS", None)
    else:
        env["UVICORN_WORKERS"] = workers

    result = subprocess.run(
        ["/bin/sh", str(_ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, su_args_path.read_text()


def test_entrypoint_defaults_uvicorn_workers_to_one(tmp_path):
    result, su_args = _run_entrypoint(tmp_path, workers=None)

    assert result.returncode == 0
    assert "--workers 1" in su_args
    assert "UVICORN_WORKERS" not in result.stderr


def test_entrypoint_honors_positive_integer_uvicorn_workers(tmp_path):
    result, su_args = _run_entrypoint(tmp_path, workers="3")

    assert result.returncode == 0
    assert "--workers 3" in su_args
    assert "UVICORN_WORKERS" not in result.stderr


@pytest.mark.parametrize("bad_value", ["auto", "2.0", " 2", "2 ", "0", "-1"])
def test_entrypoint_falls_back_to_one_for_invalid_uvicorn_workers(tmp_path, bad_value):
    result, su_args = _run_entrypoint(tmp_path, workers=bad_value)

    assert result.returncode == 0
    assert "--workers 1" in su_args
    assert "WARN: UVICORN_WORKERS must be a positive integer" in result.stderr
    assert f"got '{bad_value}'" in result.stderr
