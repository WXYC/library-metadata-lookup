"""Deployed-build identity shared by `/health` and lookup telemetry (LML#1233).

`routers/health.py` has resolved the deployed commit SHA since LML#509, for
the `/health` payload and the deploy-reconciliation gate that reads it.
LML#1233 needs the same value as a `lookup_completed` property, so a
miss-rate movement can be attributed to the commit that shipped it via a
PostHog breakdown rather than an eyeball correlation against deploy times.
This module is the one implementation both call sites share.

Layering: `core/` sits below `lookup/` and below `routers/`, and this module
imports neither -- it reads a path and an environment variable and returns a
string.

**Uncached, and the path is required.** Both properties are load-bearing:

- `/health` calls this per request (`routers/health.py`) and
  `scripts/reconcile_deploy_via_health.sh` reads the field during the deploy
  gate. A value cached in here would serve the pre-deploy SHA after a
  redeploy and quietly break that gate. A caller that wants caching does it
  at its own call site -- `lookup/router.py` binds once at module level,
  because it is on the hot path and a per-request file read there is the
  class of tax LML#949 removed.
- `routers.health` redirects the file location by monkeypatching its own
  module global `COMMIT_SHA_PATH`, which is read inside the function body at
  call time. If this function defaulted the path itself, those redirects
  would stop taking effect while every test still passed. Requiring the
  argument makes that failure impossible to introduce silently.
"""

from __future__ import annotations

import os
from pathlib import Path

# A blank ``COMMIT_SHA`` placeholder is tracked at the repo root; CI's "Record
# deployed commit SHA" step overwrites it with the deployed commit before
# ``railway up``. Two filters must NOT exclude it, or it silently never reaches
# the running image (re-null-ing commit_sha): ``.gitignore`` -- ``railway up``
# honors it and would drop the file from the upload tarball; and
# ``.dockerignore`` -- the Dockerfile build (``railway.toml``
# builder=DOCKERFILE, ``COPY . .``) honors it and would drop it from the image.
# See WXYC/library-metadata-lookup#509.
COMMIT_SHA_PATH = Path(__file__).resolve().parent.parent / "COMMIT_SHA"
"""The repo-root ``COMMIT_SHA`` location both call sites resolve against."""


def resolve_commit_sha(commit_sha_path: Path) -> str | None:
    """Resolve the deployed commit SHA, in priority order.

    Args:
        commit_sha_path: Location of the `COMMIT_SHA` file CI bakes into the
            image. Required -- see the module docstring for why there is no
            default. Callers pass their own module global so tests can
            redirect it.

    Returns:
        The resolved SHA, or `None` when neither source is populated.

    Resolution order:

    1. The `COMMIT_SHA` file baked into the image by CI before `railway up`.
       Authoritative: the deploy serving prod and staging is a `railway up`
       CLI source-deploy, which Railway does *not* tag with git metadata, so
       `RAILWAY_GIT_COMMIT_SHA` is absent there (LML#509).
    2. `RAILWAY_GIT_COMMIT_SHA` -- set only on Railway's git-native deploys.
       Load-bearing for the deploy race: a git-native deploy carries no baked
       file but does set this, so the SHA is still non-null.
    3. `None` -- local dev, CI, and tests.

    Empty / whitespace-only values at any tier are treated as absent, so the
    documented "null when unset" contract holds and downstream equality gates
    (e.g. WXYC/Backend-Service#1361) are never fooled by `""`.
    """
    try:
        # utf-8-sig drops a leading BOM if some editor or tool wrote one;
        # a plain `.strip()` would leave it and break exact-equality gates.
        file_sha = commit_sha_path.read_text(encoding="utf-8-sig").strip()
    except (OSError, ValueError):
        # OSError: missing file / dir-at-path / permission. ValueError covers
        # UnicodeDecodeError (a corrupt, non-UTF-8 file). `/health` is the
        # Railway healthcheck path and must never 500 on a bad identity file
        # -- degrade to the env / None tiers instead.
        file_sha = ""
    if file_sha:
        return file_sha
    env_sha = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    return env_sha or None
