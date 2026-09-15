"""The wxyc-shared#428 codegen pin, ported into ``scripts/generate_api_models.sh`` (LML#1299).

wxyc-shared#428 annotated the five streaming URL fields with ``format: uri``.
``datamodel-code-generator`` maps that annotation to pydantic ``AnyUrl``, which
validates at **decode** time — so a regen without a pin promotes those fields
and turns already-persisted malformed ``streaming_links`` rows into hard decode
failures on read. The pin holds them at ``str``.

The pin is two mechanisms, not one, and the second is the point. The
substitution is line-anchored on ``fieldname: AnyUrl``, which is only one of
the shapes the generator can emit: add ``--use-annotated`` and a field with a
description — which #428 gives all five — and it emits
``fieldname: Annotated[AnyUrl | None, Field(...)]`` instead, which the sed
never matches and silently no-ops past. That is the pin failing **open**, and
it is exactly what shipped malformed rows into a decode error. So the
post-condition is AST-level: walk every ``AnnAssign`` and ask whether
``AnyUrl`` appears anywhere in the field's annotation, rather than whether the
annotation looks like some anticipated shape.

Following the ``test_generate_api_models_auth.py`` precedent, each test runs
the real script via subprocess against stub ``datamodel-codegen`` and ``ruff``
binaries on ``PATH`` — so the generator's output shape is the test input, and
no network, real codegen, or real spec is involved.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_api_models.sh"

PINNED_FIELDS = (
    "spotify_url",
    "apple_music_url",
    "youtube_music_url",
    "bandcamp_url",
    "soundcloud_url",
)

# The three unrelated ``format: uri`` fields in the same spec. #428's decision
# does not cover them and they carry no malformed stored data, so they must keep
# whatever the generator gives them — the pin's scope is the five names above.
UNPINNED_URI_FIELDS = ("url", "verification_uri", "verification_uri_complete")

_PLAIN_SHAPE = """\
from pydantic import AnyUrl, BaseModel, Field


class StreamingLinks(BaseModel):
{pinned}


class DeviceCodeResponse(BaseModel):
{unpinned}
"""

_ANNOTATED_SHAPE = """\
from typing import Annotated

from pydantic import AnyUrl, BaseModel, Field


class StreamingLinks(BaseModel):
{pinned}


class DeviceCodeResponse(BaseModel):
{unpinned}
"""


def _generated_source(*, annotated: bool) -> str:
    """The generator's output for the five pinned + three unpinned uri fields.

    ``annotated=True`` is the ``--use-annotated`` shape that defeats a
    line-anchored substitution — the regression the AST post-condition exists
    for.
    """
    if annotated:
        pinned = "\n".join(
            f'    {f}: Annotated[\n        AnyUrl | None,\n        Field(description="{f}"),\n    ]'
            for f in PINNED_FIELDS
        )
        unpinned = "\n".join(
            f'    {f}: Annotated[\n        AnyUrl | None,\n        Field(description="{f}"),\n    ]'
            for f in UNPINNED_URI_FIELDS
        )
        return _ANNOTATED_SHAPE.format(pinned=pinned, unpinned=unpinned)
    pinned = "\n".join(f"    {f}: AnyUrl | None = None" for f in PINNED_FIELDS)
    unpinned = "\n".join(f"    {f}: AnyUrl | None = None" for f in UNPINNED_URI_FIELDS)
    return _PLAIN_SHAPE.format(pinned=pinned, unpinned=unpinned)


def _install_stub_toolchain(tmp_path: Path, *, annotated: bool) -> None:
    """Stub ``datamodel-codegen`` (writes the shape under test) and ``ruff`` (no-op)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    source = _generated_source(annotated=annotated)
    payload = bin_dir / "generated_source.py.txt"
    payload.write_text(source)

    codegen = bin_dir / "datamodel-codegen"
    # Parse --output out of argv and write the payload there; --version is
    # probed by the script's non-uv fallback arm before it generates.
    codegen.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--version" ]]; then echo "0.56.1"; exit 0; fi\n'
        'out=""\n'
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "--output" ]]; then out="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        'mkdir -p "$(dirname "$out")"\n'
        f'cp "{payload}" "$out"\n'
    )
    codegen.chmod(0o755)

    ruff = bin_dir / "ruff"
    ruff.write_text("#!/usr/bin/env bash\nexit 0\n")
    ruff.chmod(0o755)


def _set_up_repo(tmp_path: Path) -> Path:
    """Lay out a repo + sibling ``wxyc-shared/api.yaml`` and return the script copy.

    The script resolves the sibling checkout from its own location, so the copy
    needs a real repo around it for the local-spec arm to be selected (no
    download, no network).
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    script_copy = repo / "scripts" / "generate_api_models.sh"
    shutil.copy(_SCRIPT, script_copy)
    sibling = tmp_path / "wxyc-shared"
    sibling.mkdir()
    (sibling / "api.yaml").write_text("openapi: 3.0.0\n")
    return script_copy


def _run(tmp_path: Path, script: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    # Ahead of any real .venv/bin on PATH, and of `uv`, so the stubs win.
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )


def _annotations(output: Path) -> list[tuple[str, str, str]]:
    """Every annotated class attribute as ``(class, field, annotation source)``.

    Deliberately not a ``{field: annotation}`` dict. Each of the five pinned
    names is defined in *five* different classes in the committed snapshot
    (``AlbumMetadata``, ``StreamingLinks``, ``FlowsheetEntryFields``,
    ``FlowsheetV2TrackEntry``, ``DiscogsMatchResult``), and a dict keyed on the
    bare name keeps only the last one parsed — so a snapshot where
    ``AlbumMetadata.spotify_url`` carried ``AnyUrl`` while the last-defined
    class did not would read green (LML#1312 review). The production
    post-condition collects every occurrence; so does this.
    """
    import ast

    tree = ast.parse(output.read_text(), filename=str(output))
    found: list[tuple[str, str, str]] = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for node in cls.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.append((cls.name, node.target.id, ast.unparse(node.annotation)))
    return found


def _annotations_for(output: Path, field: str) -> list[tuple[str, str]]:
    """``(class, annotation)`` for every definition of ``field``."""
    return [(cls, ann) for cls, name, ann in _annotations(output) if name == field]


def test_pin_holds_the_five_streaming_url_fields_at_str(tmp_path):
    """The whole point: a ``format: uri`` field generated as ``AnyUrl`` lands as ``str``.

    Without this, a regen against api.yaml >= 1.50.2 makes every malformed
    ``streaming_links`` row already in the database a decode error on read.
    """
    _install_stub_toolchain(tmp_path, annotated=False)
    script = _set_up_repo(tmp_path)

    result = _run(tmp_path, script)

    assert result.returncode == 0, result.stderr
    output = script.parents[1] / "generated" / "api_models.py"
    for field in PINNED_FIELDS:
        occurrences = _annotations_for(output, field)
        assert occurrences, field
        for cls, annotation in occurrences:
            assert "AnyUrl" not in annotation, f"{cls}.{field}"
            assert "str" in annotation, f"{cls}.{field}"


def test_pin_leaves_unrelated_uri_fields_alone(tmp_path):
    """Scope: the archive presigned-GET ``url`` and the two OAuth device-flow
    fields are ``format: uri`` too, but carry no malformed stored data and are
    outside #428's decision. Pinning them would be an unrelated contract change
    smuggled in by a regex."""
    _install_stub_toolchain(tmp_path, annotated=False)
    script = _set_up_repo(tmp_path)

    result = _run(tmp_path, script)

    assert result.returncode == 0, result.stderr
    output = script.parents[1] / "generated" / "api_models.py"
    for field in UNPINNED_URI_FIELDS:
        occurrences = _annotations_for(output, field)
        assert occurrences, field
        for cls, annotation in occurrences:
            assert "AnyUrl" in annotation, f"{cls}.{field}"


def test_annotated_shape_defeats_the_substitution_and_fails_loudly(tmp_path):
    """The failure mode the AST post-condition exists for, driven end to end.

    ``--use-annotated`` plus a field description — what #428 gives all five —
    emits ``Annotated[AnyUrl | None, Field(...)]``. The line-anchored sed sees
    ``Annotated[`` where it expects ``AnyUrl``, matches nothing, and exits 0.
    The script must not: a silent no-op here is the pin failing open, which
    ships the decode failures the pin exists to prevent.
    """
    _install_stub_toolchain(tmp_path, annotated=True)
    script = _set_up_repo(tmp_path)

    result = _run(tmp_path, script)

    assert result.returncode != 0
    assert "#428 pin did not apply" in result.stderr
    for field in PINNED_FIELDS:
        assert field in result.stderr, field


def test_post_condition_fires_when_the_substitution_is_stubbed_out(tmp_path):
    """The defeat test proper: neuter the substitution, keep everything else.

    Guards the post-condition itself. A future edit that makes the sed a no-op
    for any reason — a renamed field, a changed anchor, a shape nobody
    anticipated — has to fail this, because the check asks "is AnyUrl still
    here" rather than "did the sed look like it ran".
    """
    _install_stub_toolchain(tmp_path, annotated=False)
    script = _set_up_repo(tmp_path)
    source = script.read_text()
    assert "pin_streaming_url_fields_to_str() {" in source
    neutered = source.replace(
        "pin_streaming_url_fields_to_str() {",
        "pin_streaming_url_fields_to_str() {\n    return 0",
        1,
    )
    script.write_text(neutered)

    result = _run(tmp_path, script)

    assert result.returncode != 0
    assert "#428 pin did not apply" in result.stderr


@pytest.mark.parametrize("field", PINNED_FIELDS)
def test_committed_snapshot_carries_the_pin(field):
    """The snapshot in the repo is the artifact consumers import — assert the pin
    on it directly, not only on the script that produces it. A regen run without
    the pin would land here and nowhere else."""
    committed = Path(__file__).resolve().parents[2] / "generated" / "api_models.py"
    occurrences = _annotations_for(committed, field)
    # Every class that declares it, not just the last one parsed.
    assert len(occurrences) >= 1
    for cls, annotation in occurrences:
        assert "AnyUrl" not in annotation, f"{cls}.{field}"


_ROOTMODEL_SHAPE = """\
from pydantic import AnyUrl, BaseModel, Field, RootModel


class StreamingUrl(RootModel[AnyUrl]):
    root: AnyUrl


class StreamingLinks(BaseModel):
{pinned}


class DeviceCodeResponse(BaseModel):
{unpinned}
"""


def _install_rootmodel_stub(tmp_path: Path) -> None:
    """Stub the generator into the named-``$ref`` shape: a RootModel wrapper.

    The five fields never mention ``AnyUrl`` themselves — they reference a
    generated wrapper class that does.
    """
    pinned = "\n".join(f"    {f}: StreamingUrl | None = None" for f in PINNED_FIELDS)
    unpinned = "\n".join(f"    {f}: AnyUrl | None = None" for f in UNPINNED_URI_FIELDS)
    _install_stub_toolchain(tmp_path, annotated=False)
    (tmp_path / "bin" / "generated_source.py.txt").write_text(
        _ROOTMODEL_SHAPE.format(pinned=pinned, unpinned=unpinned)
    )


def test_named_rootmodel_wrapper_does_not_slip_past_the_post_condition(tmp_path):
    """The indirection a denylist cannot see (LML#1312 review).

    Give a field a named ``$ref`` schema and the generator emits
    ``class StreamingUrl(RootModel[AnyUrl])`` plus ``field: StreamingUrl | None``.
    The sed does not match, and the token ``AnyUrl`` is absent from the field's
    own annotation — so a check asking "is AnyUrl present here" passes while all
    five fields validate URLs at decode time regardless, which is the entire
    failure the pin exists to prevent.

    Not hypothetical: this generator emits ``RootModel`` wrappers for named
    schemas today (the regen this pin unblocked landed ``class Url(RootModel[...])``),
    and ``AlbumMetadata``'s own docstring says these five fields "will be migrated
    to use `$ref` in a future version". Asserting the annotation IS ``str``,
    rather than is-not-``AnyUrl``, is what closes it.
    """
    _install_rootmodel_stub(tmp_path)
    script = _set_up_repo(tmp_path)

    result = _run(tmp_path, script)

    assert result.returncode != 0
    assert "#428 pin did not apply" in result.stderr
    for field in PINNED_FIELDS:
        assert field in result.stderr, field


def test_a_renamed_field_fails_loudly_instead_of_pinning_fewer(tmp_path):
    """An offenders-only check is vacuous under a rename.

    Drop one pinned field from the generator's output — what an upstream rename
    looks like from here. The sed no-ops on the missing name and the walk finds
    nothing wrong, so a check that only ever reports offenders exits 0 having
    pinned four of five, silently. The post-condition asserts every field was
    found, so the rename surfaces as a diagnosable failure naming the field.
    """
    _install_stub_toolchain(tmp_path, annotated=False)
    dropped, *kept = PINNED_FIELDS
    pinned = "\n".join(f"    {f}: AnyUrl | None = None" for f in kept)
    unpinned = "\n".join(f"    {f}: AnyUrl | None = None" for f in UNPINNED_URI_FIELDS)
    (tmp_path / "bin" / "generated_source.py.txt").write_text(
        _PLAIN_SHAPE.format(pinned=pinned, unpinned=unpinned)
    )
    script = _set_up_repo(tmp_path)

    result = _run(tmp_path, script)

    assert result.returncode != 0
    assert "were not found" in result.stderr
    assert dropped in result.stderr
