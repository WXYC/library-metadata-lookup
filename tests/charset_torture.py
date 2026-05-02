"""Loader for the cross-repo charset-torture corpus.

The canonical JSON lives in ``@wxyc/shared`` (`src/test-utils/charset-torture.json`).
Each consumer vendors a copy at ``tests/fixtures/charset-torture.json`` pinned to
``tests/fixtures/charset-torture.json.sha256``; the M3.2 drift-guard workflow
(`.github/workflows/charset-corpus-drift.yml`) fails CI when the published
corpus moves. See WX-1 plan in WXYC/docs#15.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypedDict, cast

_CORPUS_PATH = Path(__file__).parent / "fixtures" / "charset-torture.json"


class CharsetTortureEntry(TypedDict):
    category: str
    input: str
    expected_storage: str
    expected_match_form: str | None
    expected_ascii_form: str | None
    notes: str


def load_corpus() -> dict[str, Any]:
    """Return the parsed corpus JSON."""
    return json.loads(_CORPUS_PATH.read_text(encoding="utf-8"))


def iter_entries() -> Iterator[CharsetTortureEntry]:
    """Yield every entry across every category, tagged with its category name."""
    corpus = load_corpus()
    for category, entries in corpus["categories"].items():
        for entry in entries:
            yield cast(CharsetTortureEntry, {**entry, "category": category})


def entry_id(entry: CharsetTortureEntry) -> str:
    """Stable parametrize id: ``<category>:<truncated input>``."""
    truncated = entry["input"][:24].replace("\n", "\\n")
    return f"{entry['category']}:{truncated}"
