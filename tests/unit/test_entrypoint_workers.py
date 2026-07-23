"""LML#747: the container entrypoint must honor ``UVICORN_WORKERS`` (default 1).

Wiring the worker count as a Railway env var makes horizontal scaling a
no-redeploy lever, while the ``:-1`` default keeps today's single-worker behavior
byte-for-byte until the var is set. The prereqs for setting it >1 (the shared
Discogs 60/min token: enable ``DISCOGS_RATE_BUCKET_ENABLED`` + divide
``DISCOGS_RATE_LIMIT`` to ``floor(50/N)``) live in the horizontal-scaling runbook
(``docs/deployment.md``); this test only guards that the wiring itself exists and
defaults to 1, so a future edit can't silently drop it or change the default.
"""

from pathlib import Path

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def test_entrypoint_honors_uvicorn_workers_defaulting_to_one():
    script = _ENTRYPOINT.read_text()
    # uvicorn's worker count is driven by UVICORN_WORKERS, defaulting to 1 —
    # the exact expansion is load-bearing: a blank/absent default would change
    # single-worker behavior on merge.
    assert "--workers ${UVICORN_WORKERS:-1}" in script
