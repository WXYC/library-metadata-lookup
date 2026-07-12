"""LML#769: unit runs must be hermetic — no real credentials from any .env.

``main.py``'s module-level ``load_dotenv()`` used python-dotenv's default
upward search, so a pytest run from a git worktree nested under the main
checkout (``.worktrees/<name>/``, ``.claude/worktrees/<name>/``) climbed out
of the worktree and loaded the PARENT checkout's ``.env`` — leaking its real
``DISCOGS_TOKEN`` and live ``DATABASE_URL_DISCOGS`` into the test process.
Any unit test whose dependency surface wasn't fully overridden could then
build real clients: ``get_discogs_service`` opened a REAL asyncpg pool
against the developer's live discogs-cache, and that pool outliving its
event loop is what produced the long-standing
``test_no_module_level_asyncio_lock_in_dependencies`` teardown error
(``RuntimeError: Event loop is closed``).

Three layers, each pinned by one test below:

1. The end-to-end invariant: a fresh ``get_settings()`` inside a unit run
   carries no credentials, wherever the run was invoked from.
2. The source pin: ``main.py`` must never regress to a bare ``load_dotenv()``
   (the upward-searching form).
3. The conftest scrub: the session-wide fixture in ``tests/unit/conftest.py``
   blanks every credential-bearing variable, which also shadows values pydantic
   would otherwise read from a CWD-relative ``.env`` via ``env_file``.
"""

import ast
import os
from pathlib import Path

import main
from config.settings import Settings, get_settings
from tests.unit.conftest import CREDENTIAL_ENV_VARS

#: Settings fields that carry credentials or live connection strings. Kept in
#: lockstep with ``CREDENTIAL_ENV_VARS`` (pydantic maps env vars to fields
#: case-insensitively, so the pairing is just a lower-casing away).
CREDENTIAL_SETTINGS_FIELDS = tuple(var.lower() for var in CREDENTIAL_ENV_VARS)


def test_credential_env_vars_cover_all_credential_settings_fields():
    """Every credential-bearing Settings field must be in the scrub list.

    A new credential field added to ``Settings`` without a matching entry in
    ``CREDENTIAL_ENV_VARS`` would silently re-open the leak for that field.
    """
    for field in CREDENTIAL_SETTINGS_FIELDS:
        assert field in Settings.model_fields, (
            f"CREDENTIAL_ENV_VARS entry {field.upper()!r} has no matching "
            f"Settings field {field!r} — fix the scrub list in "
            "tests/unit/conftest.py."
        )


def test_unit_run_sees_no_real_credentials():
    """A fresh ``get_settings()`` in a unit run must carry no credentials.

    ``import main`` (at module scope above) forces the module-level dotenv
    load, so this test observes the post-load environment no matter which
    subset of the suite is running. Before LML#769 this failed from any
    nested worktree whose parent checkout had a populated ``.env``.
    """
    get_settings.cache_clear()
    try:
        settings = get_settings()
        for field in CREDENTIAL_SETTINGS_FIELDS:
            # Reduce to a bool BEFORE asserting so pytest's assertion
            # introspection can never print a real leaked credential.
            leaked = bool(getattr(settings, field))
            assert not leaked, (
                f"Unit run is not hermetic: settings.{field} carries a value "
                f"(length {len(str(getattr(settings, field)))}). A real credential "
                "leaked in from a .env file or the shell environment — see "
                "WXYC/library-metadata-lookup#769."
            )
    finally:
        get_settings.cache_clear()


def test_main_load_dotenv_is_pinned_to_checkout_root():
    """``main.py`` must not call the bare, upward-searching ``load_dotenv()``.

    ``load_dotenv()`` without a path walks ancestor directories for a
    ``.env``, which is exactly the cross-checkout leak this file guards
    against. The call must pass an explicit path anchored on
    ``Path(__file__)`` so each checkout (worktrees included) loads only its
    own ``.env``. Source-level pin in the spirit of
    ``test_no_module_level_asyncio_lock_in_dependencies``, but AST-based so
    prose in comments can't false-positive the scan.
    """
    tree = ast.parse(Path(main.__file__).read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "load_dotenv")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "load_dotenv")
        )
    ]
    assert calls, "main.py no longer loads .env at all — dev runs would lose their config."
    for call in calls:
        assert call.args, (
            f"main.py line {call.lineno} calls bare load_dotenv(), which does "
            "python-dotenv's upward .env search and can load an ANCESTOR "
            "checkout's .env from a nested worktree. Pin it to this checkout's "
            'root: load_dotenv(Path(__file__).resolve().parent / ".env"). '
            "See WXYC/library-metadata-lookup#769."
        )


def test_unit_session_scrubs_credential_env():
    """The session-wide conftest scrub must blank every credential env var.

    The blank ("") values do double duty: they shadow anything pydantic-
    settings would read from a CWD-relative ``.env`` (env vars outrank the
    ``env_file`` source), and — because ``load_dotenv``'s default is
    ``override=False`` — they also inoculate the process against later
    module-level ``load_dotenv()`` calls re-polluting the environment
    mid-session (e.g. lazily imported scripts).
    """
    for var in CREDENTIAL_ENV_VARS:
        # Reduce to a bool BEFORE asserting so pytest's assertion
        # introspection can never print a real leaked credential.
        is_blank = os.environ.get(var) == ""
        assert is_blank, (
            f"{var} is {'unset' if var not in os.environ else 'non-blank'} in a "
            "unit run — the session-wide credential scrub in "
            "tests/unit/conftest.py did not run or was bypassed. See "
            "WXYC/library-metadata-lookup#769."
        )
