"""A scripted ``curl`` stub for the LML#1231 ``/health``-polling scripts.

``scripts/sample_health_commit_sha.sh`` and ``scripts/reconcile_deploy_via_health.sh``
both shell out to ``curl -sf ... <url>`` and read the JSON body it prints. That is a
different shape of double from ``tests/curl_stub.py`` -- that stub exists to prove
what a call to ``curl`` *carried* (argv/stdin/env, for the auth-header hardening
pair) and always exits 0 with empty stdout, which is exactly wrong here: these two
scripts branch entirely on the *response body* curl would have printed, across a
sequence of calls as a poll loop advances.

This stub is scripted instead: each successive invocation of the faked ``curl``
consumes the next entry from an ordered list of canned responses, each carrying a
body *and* an HTTP status, because these scripts have to tell "could not read
/health" apart from "read it fine, the service is just unhealthy" -- ``/health``
answers 503 with a fully-populated body, and the deploy identity in it is exactly
as good as the one in a 200. Once the list is exhausted, the stub keeps replaying
the final entry -- so a "stuck on the old SHA forever" scenario can be expressed
with a single-element list rather than one entry per poll a test cannot know the
exact count of in advance. Each call's argv is also recorded, so a test can pin
that a timeout knob actually reaches curl rather than trusting the env var name
by inspection alone.
"""

from __future__ import annotations

from pathlib import Path


def install_health_curl_stub(tmp_path: Path, responses: list) -> None:
    """Install a fake ``curl`` in ``tmp_path/bin`` that replays ``responses`` in order.

    ``responses[i]`` is one of:

    - a body string -- served with HTTP 200;
    - ``(status_code, body)`` -- served with that status. ``/health`` answers **503 with a
      fully-populated body** (``commit_sha`` included) whenever a core dependency is down
      (``routers/health.py``: ``status_code = 200 if status in ("healthy", "degraded") else 503``),
      so "unreachable" and "readable but unhealthy" are genuinely different cases that the scripts
      under test have to tell apart. Expressing that needs a status code, not just a body;
    - ``None`` -- a transport-level failure: nothing printed, exit 7 (curl's "couldn't connect"),
      standing in for DNS failure, connection refused, or a timeout.

    Calls past the end of the list replay ``responses[-1]`` forever, so a poll loop that outlives
    the scripted sequence still gets a well-defined answer instead of hitting a missing file.

    The stub emulates the two flags that change what a caller can observe, rather than asserting
    about them: ``-w`` appends a newline and the status code after the body, and ``-f`` suppresses
    the body and exits 22 for any status >= 400 -- which is precisely how a naive ``curl -sf``
    throws away the 503 bodies above. Short options bundle, so ``-sf`` counts as ``-f``; matching
    only a bare ``-f`` argument would miss the exact spelling a caller is most likely to write.
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
        elif isinstance(response, tuple):
            status, body = response
            (responses_dir / f"{i}.body").write_text(body)
            (responses_dir / f"{i}.status").write_text(str(status))
        else:
            (responses_dir / f"{i}.body").write_text(response)
            (responses_dir / f"{i}.status").write_text("200")

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
        "  exit 7\n"
        "fi\n"
        # Scan for the two flags that change what a caller can observe: -w (append the status
        # code) and -f (suppress the body and exit 22 on an HTTP error). Short options bundle,
        # so `-sf` and `-fs` are both `-f` -- checking for a bare "-f" argument would miss the
        # exact spelling the scripts actually use.
        "write_out=0\n"
        "want_value=0\n"
        "fail_on_error=0\n"
        'for arg in "$@"; do\n'
        '  if [ "$want_value" = "1" ]; then want_value=0; continue; fi\n'
        '  case "$arg" in\n'
        "    -w|--write-out) write_out=1; want_value=1 ;;\n"
        "    --fail) fail_on_error=1 ;;\n"
        "    --*) ;;\n"
        '    -*) case "$arg" in *f*) fail_on_error=1 ;; esac ;;\n'
        "  esac\n"
        "done\n"
        'status="$(cat "$responses_dir/$idx.status")"\n'
        # Real curl -f: no body on stdout, exit 22, for any status >= 400.
        'if [ "$fail_on_error" = "1" ] && [ "$status" -ge 400 ]; then\n'
        "  exit 22\n"
        "fi\n"
        'cat "$responses_dir/$idx.body"\n'
        'if [ "$write_out" = "1" ]; then\n'
        '  printf "\\n%s" "$status"\n'
        "fi\n"
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
