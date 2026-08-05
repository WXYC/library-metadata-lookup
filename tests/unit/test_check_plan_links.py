"""Unit tests for ``scripts/check_plan_links.sh`` (LML#1124).

Two false-greens in this script's own review history are the concrete
justification for locking its behavior down with tests rather than trusting
the regex by inspection: a non-greedy path prefix would let a docs-plans
citation be matched as a bare plans-only substring (breaking resolution for
any citing file outside ``docs/``), and a filesystem-based existence check
(``[ -f ]``) would let an untracked file on a developer's disk mask a real
broken citation. Both are covered below, alongside the URL-form and
cross-repo-doc skip lists and the ``generated/`` exclusion.

Fixture citation strings are assembled via :func:`_cite` rather than written
as contiguous literals, deliberately: this file is itself scanned by the
checker under test, and a literal "plans/<name>.md"-shaped string sitting in
tracked source here would register as a citation to a file that does not
really exist in this repo -- self-defeating for a script whose entire job is
flagging exactly that shape of string.

Each test runs the real script against a disposable ``git init`` repo in
``tmp_path`` -- ``git ls-files`` resolution only needs files staged with
``git add``, not committed.
"""

import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_plan_links.sh"

_PLANS_SEGMENT = "plans"


def _cite(name: str, *, under_docs: bool = True) -> str:
    """Build a ``docs/plans/<name>`` (or bare ``plans/<name>``) citation string.

    See the module docstring: kept out of literal form so this file doesn't
    trip the very checker it tests.
    """
    prefix = "docs/" if under_docs else ""
    return f"{prefix}{_PLANS_SEGMENT}/{name}"


def _init_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)


def _track(tmp_path: Path, relpath: str, content: str) -> None:
    """Write ``content`` to ``relpath`` under ``tmp_path`` and stage it."""
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    subprocess.run(["git", "add", "--", relpath], cwd=tmp_path, check=True)


def _write_untracked(tmp_path: Path, relpath: str, content: str = "") -> None:
    path = tmp_path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _run_checker(tmp_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_resolved_citation_passes(tmp_path):
    """Baseline: a citation to a tracked docs/plans/ file is not reported."""
    _init_repo(tmp_path)
    foo = _cite("foo.md")
    _track(tmp_path, foo, "# Foo\n")
    _track(tmp_path, "config/settings.py", f"# rationale: {foo}\n")

    result = _run_checker(tmp_path)

    assert result.returncode == 0
    assert "BROKEN" not in result.stdout


def test_broken_citation_is_reported(tmp_path):
    """A citation to a file that is tracked nowhere is a genuine break."""
    _init_repo(tmp_path)
    ghost = _cite("ghost.md")
    _track(tmp_path, "config/settings.py", f"# rationale: {ghost}\n")

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert f"BROKEN: config/settings.py:{ghost}" in result.stdout


def test_index_based_resolution_ignores_untracked_shadow_file(tmp_path):
    """An untracked file on disk must not mask a real break.

    Measured during LML#1124 round 8: a filesystem-based ``[ -f ]`` check
    reports 0 breaks against a working tree that has the cited file present
    but untracked; ``git ls-files --error-unmatch`` must still report it.
    """
    _init_repo(tmp_path)
    ghost = _cite("ghost.md")
    _track(tmp_path, "config/settings.py", f"# rationale: {ghost}\n")
    _write_untracked(tmp_path, ghost, "# Ghost\n")

    result = _run_checker(tmp_path)

    assert result.returncode == 1
    assert f"BROKEN: config/settings.py:{ghost}" in result.stdout


def _greedy_prefix_case():
    foo = _cite("foo.md")
    return pytest.param(
        "config/settings.py",
        f"# see {foo}\n",
        foo,
        id="greedy-prefix-captures-full-nested-path",
    )


def _url_form_case():
    bar_url = f"https://github.com/WXYC/discogs-etl/blob/main/{_cite('bar.md')}"
    return pytest.param(
        "notes.py",
        f"# see {bar_url}\n",
        None,
        id="url-form-citation-skipped",
    )


def _cross_repo_doc_case():
    test_patterns = _cite("test-patterns.md", under_docs=False)
    return pytest.param(
        "notes.py",
        f"# see {test_patterns}\n",
        None,
        id="cross-repo-doc-skipped",
    )


@pytest.mark.parametrize(
    "relpath,content,seed_target",
    [_greedy_prefix_case(), _url_form_case(), _cross_repo_doc_case()],
)
def test_guard_prevents_false_positive(tmp_path, relpath, content, seed_target):
    """Each guard keeps a legitimate, non-broken citation from being flagged.

    ``greedy-prefix``: without the greedy ``[A-Za-z0-9_/.-]*`` prefix, a
    citation to a nested ``docs/plans/`` file written from a file outside
    ``docs/`` (here, ``config/settings.py``) would match only the bare
    ``plans/<name>.md`` substring, and neither ``config/plans/<name>.md`` nor
    that bare substring resolves -- a false break against a perfectly good
    citation.

    ``url-form`` / ``cross-repo-doc``: both name files that are not tracked
    anywhere in this disposable repo; if the corresponding skip did not fire,
    the citation would be reported broken.
    """
    _init_repo(tmp_path)
    if seed_target is not None:
        _track(tmp_path, seed_target, "# Foo\n")
    _track(tmp_path, relpath, content)

    result = _run_checker(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "BROKEN" not in result.stdout


def test_generated_directory_is_excluded(tmp_path):
    """``generated/`` is codegen'd upstream; hand-editing it to fix a citation
    would break the ``codegen-check`` CI job, so the checker must not scan it
    at all -- not even to report a citation that would otherwise be broken.
    """
    _init_repo(tmp_path)
    ghost = _cite("ghost.md")
    _track(tmp_path, "generated/api_models.py", f"# see {ghost}\n")

    result = _run_checker(tmp_path)

    assert result.returncode == 0
    assert "BROKEN" not in result.stdout
