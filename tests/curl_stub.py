"""A recording ``curl`` stub on ``PATH``, shared by the download-hardening suites.

Two shell entry points in this repo download from raw.githubusercontent.com and
must do it authenticated, retried, and without ever putting the token somewhere
it can be read back -- ``scripts/generate_api_models.sh`` (LML#1205) and
``scripts/install_actionlint.sh`` (LML#1214). Both are tested the same way:
hermetically, by putting a fake ``curl`` first on ``PATH`` that records what it
was handed instead of touching the network.

The recording shape is what makes the interesting properties assertable:

- **argv** proves the token is *not* there (argv is world-readable via ``ps``),
  and proves ``--max-time``/``--retry`` are;
- **stdin** proves the ``Authorization`` header arrived by the one channel that
  is not world-readable (``-H @-``);
- **env** proves no child process inherited the token;
- **one numbered record per call** is what makes the authenticated-then-
  anonymous fallback assertable as two distinct calls rather than one blurred
  final state.

Lives at ``tests/`` root rather than under ``tests/unit/`` because it is a
helper module, not a suite -- the same placement as ``tests/charset_torture.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict


def install_curl_stub(tmp_path: Path) -> None:
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

    Payload knob: if ``curl_payload`` exists, its contents are written to the
    path following ``-o`` on a successful call. ``generate_api_models.sh``'s
    tests stop the script before anything reads the download
    (``--download-only``), but ``install_actionlint.sh`` immediately *executes*
    what it fetched, so that suite needs the stub to produce a real file.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'calls_dir="{tmp_path / "curl_calls"}"\n'
        f'fail_marker="{tmp_path / "curl_fail_remaining"}"\n'
        f'payload="{tmp_path / "curl_payload"}"\n'
        'mkdir -p "$calls_dir"\n'
        "n=$(find \"$calls_dir\" -name '*.argv' | wc -l | tr -d ' ')\n"
        "n=$((n + 1))\n"
        ': > "$calls_dir/$n.argv"\n'
        'for arg in "$@"; do printf "%s\\n" "$arg" >> "$calls_dir/$n.argv"; done\n'
        'cat > "$calls_dir/$n.stdin"\n'
        "env | grep -E '^(GITHUB_TOKEN|GH_TOKEN)=' > \"$calls_dir/$n.env\" || true\n"
        '[ -e "$fail_marker" ] && [ "$n" -le "$(cat "$fail_marker")" ] && exit 22\n'
        # -o's argument is the download target. Only written when the test asked
        # for a payload, so the no-payload suites see the pre-existing behavior.
        'if [ -e "$payload" ]; then\n'
        '    prev=""\n'
        '    for arg in "$@"; do\n'
        '        if [ "$prev" = "-o" ]; then cp "$payload" "$arg"; break; fi\n'
        '        prev="$arg"\n'
        "    done\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)


class CurlCall(TypedDict):
    argv: list[str]
    stdin: str
    env: str


def curl_calls(tmp_path: Path) -> list[CurlCall]:
    """Read back the stub's per-invocation records, in call order."""
    calls_dir = tmp_path / "curl_calls"
    if not calls_dir.is_dir():
        return []
    records: list[CurlCall] = []
    for argv_file in sorted(calls_dir.glob("*.argv"), key=lambda p: int(p.stem)):
        records.append(
            CurlCall(
                argv=argv_file.read_text().splitlines(),
                stdin=(calls_dir / f"{argv_file.stem}.stdin").read_text(),
                env=(calls_dir / f"{argv_file.stem}.env").read_text(),
            )
        )
    return records
