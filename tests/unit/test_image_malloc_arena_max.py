"""LML#1399: the image must carry the glibc arena limit that bounds LML#1354's ramp.

Production memory grew without bound between restarts (LML#1354). The cause was
glibc's per-thread malloc arenas: the container reports 48 CPUs, asyncio's
default executor therefore grows to 32 threads, and each allocating thread gets
its own arena that retains freed memory. ``MALLOC_ARENA_MAX`` caps the arena
count; glibc reads it once at process start, so it has to be in the environment
before uvicorn starts.

It was first applied as a hand-set Railway variable, which nothing in the repo
records. These tests pin it in the image so a re-created service, a new
environment or a local container cannot silently run without it.
"""

from pathlib import Path

_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"

# glibc's own default ceiling is 8 x CPUs. Anything above 8 here would stop
# bounding the arena count in any useful way on a small service.
_MAX_USEFUL_ARENAS = 8


def _dockerfile_env() -> dict[str, str]:
    """Return the ``ENV`` assignments in the Dockerfile, last one winning."""
    env: dict[str, str] = {}
    for raw in _DOCKERFILE.read_text().splitlines():
        line = raw.strip()
        if not line.upper().startswith("ENV "):
            continue
        body = line[4:].strip()
        if "=" in body.split()[0]:
            for pair in body.split():
                key, _, value = pair.partition("=")
                env[key] = value.strip("\"'")
        else:
            key, _, value = body.partition(" ")
            env[key] = value.strip().strip("\"'")
    return env


def test_image_sets_malloc_arena_max():
    env = _dockerfile_env()

    assert "MALLOC_ARENA_MAX" in env, (
        "Dockerfile must set ENV MALLOC_ARENA_MAX (LML#1354): without it glibc "
        "creates one malloc arena per executor thread and RSS ramps until restart"
    )


def test_image_malloc_arena_max_is_a_bounding_value():
    value = _dockerfile_env().get("MALLOC_ARENA_MAX", "")

    assert value.isdigit(), f"MALLOC_ARENA_MAX must be a positive integer; got {value!r}"
    assert 1 <= int(value) <= _MAX_USEFUL_ARENAS
