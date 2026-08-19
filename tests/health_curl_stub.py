"""A scripted ``curl`` stub for the LML#1231 ``/health``-polling scripts.

``scripts/sample_health_commit_sha.sh`` and ``scripts/reconcile_deploy_via_health.sh``
both shell out to ``curl -sf ... <url>`` and read the JSON body it prints. That is a
different shape of double from ``tests/curl_stub.py`` -- that stub exists to prove
what a call to ``curl`` *carried* (argv/stdin/env, for the auth-header hardening
pair) and always exits 0 with empty stdout, which is exactly wrong here: these two
scripts branch entirely on the *response body* curl would have printed, across a
sequence of calls as a poll loop advances.

This stub is scripted instead: each successive invocation of the faked ``curl``
consumes the next entry from an ordered list of canned responses (a JSON body
string to print on stdout with exit 0, or ``None`` to simulate a network/HTTP
failure -- ``curl -f`` exits 22 on an HTTP error, and a closed connection or DNS
failure looks the same to a caller that only checks the exit code). Once the list
is exhausted, the stub keeps replaying the final entry -- so a "stuck on the old
SHA forever" scenario can be expressed with a single-element list rather than one
entry per poll a test cannot know the exact count of in advance. Each call's argv
is also recorded, so a test can pin that a timeout knob actually reaches curl
rather than trusting the env var name by inspection alone.
"""

from __future__ import annotations

from pathlib import Path


def install_health_curl_stub(tmp_path: Path, responses: list[str | None]) -> None:
    """Install a fake ``curl`` in ``tmp_path/bin`` that replays ``responses`` in order.

    ``responses[i]`` is either a response body (printed to stdout, exit 0) or
    ``None`` (nothing printed, exit 22 -- curl's own code for an HTTP failure
    under ``-f``, and a reasonable stand-in for "unreachable" generally). Calls
    past the end of the list replay ``responses[-1]`` forever, so a poll loop
    that outlives the scripted sequence still gets a well-defined answer instead
    of hitting a missing file.
    """
    if not responses:
        raise ValueError("responses must be non-empty")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    calls_dir = tmp_path / "health_calls"
    calls_dir.mkdir(exist_ok=True)
    responses_dir = tmp_path / "health_responses"
    responses_dir.mkdir(exist_ok=True)

    for i, response in enumerate(responses, start=1):
        if response is None:
            (responses_dir / f"{i}.fail").write_text("")
        else:
            (responses_dir / f"{i}.body").write_text(response)

    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'calls_dir="{calls_dir}"\n'
        f'responses_dir="{responses_dir}"\n'
        f"total={len(responses)}\n"
        'n=$(find "$calls_dir" -maxdepth 1 -name "*.marker" | wc -l | tr -d " ")\n'
        "n=$((n + 1))\n"
        'touch "$calls_dir/$n.marker"\n'
        ': > "$calls_dir/$n.argv"\n'
        'for arg in "$@"; do printf "%s\\n" "$arg" >> "$calls_dir/$n.argv"; done\n'
        "idx=$n\n"
        'if [ "$idx" -gt "$total" ]; then idx=$total; fi\n'
        'if [ -e "$responses_dir/$idx.fail" ]; then\n'
        "  exit 22\n"
        "fi\n"
        'cat "$responses_dir/$idx.body"\n'
        "exit 0\n"
    )
    stub.chmod(0o755)


def health_curl_call_count(tmp_path: Path) -> int:
    """How many times the stubbed ``curl`` has been invoked so far."""
    calls_dir = tmp_path / "health_calls"
    if not calls_dir.is_dir():
        return 0
    return len(list(calls_dir.glob("*.marker")))


def health_curl_call_argv(tmp_path: Path, call_number: int) -> list[str]:
    """The argv (one element per line) that the ``call_number``-th invocation received."""
    return (tmp_path / "health_calls" / f"{call_number}.argv").read_text().splitlines()
