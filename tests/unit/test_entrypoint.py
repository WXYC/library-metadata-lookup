"""Source-invariant guards on entrypoint.sh (LML#706).

entrypoint.sh runs `uvicorn main:app` with a single event loop per replica.
The LML#706 cold-tail regression was event-loop starvation: one loop serviced
every cold `/lookup` (trigram match, streaming-URL cache read, identity mint,
inline external fan-out) so even trivial spans inflated to seconds. Giving the
replica more than one worker process — each with its own loop + pools — is the
highest-leverage relief. These grep-style guards (mirroring
`test_no_module_level_asyncio_lock_in_dependencies`) pin the worker count to an
env knob so the lever can be flipped from Railway without a redeploy, and pin
the default to 1 so merging the lever is inert until an operator tunes it on
staging.
"""

from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def test_entrypoint_workers_are_env_tunable():
    """uvicorn worker count is sourced from UVICORN_WORKERS, not hardcoded."""
    body = ENTRYPOINT.read_text()
    assert "UVICORN_WORKERS" in body, (
        "entrypoint.sh must source the uvicorn worker count from UVICORN_WORKERS "
        "so the LML#706 loop-starvation relief can be tuned without a redeploy"
    )
    assert "--workers" in body


def test_entrypoint_worker_default_is_one():
    """The default is 1 — inert, identical to the historical single-loop run —
    until an operator sets UVICORN_WORKERS on staging and measures."""
    body = ENTRYPOINT.read_text()
    assert "${UVICORN_WORKERS:-1}" in body, (
        "the UVICORN_WORKERS default must be 1 so this change is inert by default"
    )
