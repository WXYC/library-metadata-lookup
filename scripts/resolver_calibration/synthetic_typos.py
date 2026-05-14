"""Synthetic listener-typo positive-class generator for resolver calibration.

The production use case for ``resolve_canonical_artist`` is listener-typed
artist names — someone hears an artist on the air and types a slightly-off
version into a request box. The artist_name_variation table on the Discogs
side covers aliases and AKAs ("Bob Dylan" / "Robert Zimmerman") but not
listener typos. Synthetic corruptions of the canonical ``artist`` table cover
the listener-typo distribution.

Four corruption classes are generated per canonical name, each mapping to a
real keyboard-error mode:

* **drop_letter** — single-character deletion. Maps to "Robotnik" / "Robotnick".
* **transpose** — adjacent-character swap. Maps to "Setreolab" / "Stereolab".
* **ascii_fold** — diacritic loss. Maps to "Nilufer Yanya" / "Nilüfer Yanya".
* **duplicate_letter** — single-character duplication. Maps to "Steereolab".

The generator is deterministic given a ``seed`` so calibration sweeps are
reproducible across runs. Determinism comes from a fixed candidate-enumeration
order plus ``random.Random(seed).sample`` for the final cap.

See WXYC/library-metadata-lookup#318 for the calibration motivation and
WXYC/discogs-etl#188 for the cache-rebuild dependency.
"""

from __future__ import annotations

import random

# Lowercase (diacritic, ascii) pairs. Uppercase variants are handled at
# substitution time via ``.upper()`` on both sides. Coverage targets the
# diacritics that appear in WXYC catalog artist names (Nilüfer Yanya,
# Hermanos Gutiérrez, Csillagrablók, Éliane Radigue, etc.) plus the common
# Romance-language vowel set.
DIACRITIC_PAIRS: list[tuple[str, str]] = [
    ("á", "a"),
    ("à", "a"),
    ("â", "a"),
    ("ä", "a"),
    ("ã", "a"),
    ("å", "a"),
    ("é", "e"),
    ("è", "e"),
    ("ê", "e"),
    ("ë", "e"),
    ("í", "i"),
    ("ì", "i"),
    ("î", "i"),
    ("ï", "i"),
    ("ó", "o"),
    ("ò", "o"),
    ("ô", "o"),
    ("ö", "o"),
    ("õ", "o"),
    ("ø", "o"),
    ("ú", "u"),
    ("ù", "u"),
    ("û", "u"),
    ("ü", "u"),
    ("ý", "y"),
    ("ÿ", "y"),
    ("ñ", "n"),
    ("ç", "c"),
    ("ß", "s"),
]


def _enumerate_drop_letter(name: str) -> set[str]:
    """All single-character deletions at interior alpha positions.

    First and last positions are skipped: dropping a leading or trailing
    letter often produces a name that's recognizable as a different artist
    rather than a typo of the same one.
    """
    out: set[str] = set()
    for i in range(1, len(name) - 1):
        if name[i].isalpha():
            out.add(name[:i] + name[i + 1 :])
    return out


def _enumerate_transpose(name: str) -> set[str]:
    """All adjacent-character swaps where both chars are alpha and differ.

    Identical-character swaps are skipped because they're indistinguishable
    from the original (same string). Case is preserved at each position.
    """
    out: set[str] = set()
    for i in range(len(name) - 1):
        a, b = name[i], name[i + 1]
        if not (a.isalpha() and b.isalpha()):
            continue
        if a.lower() == b.lower():
            continue
        out.add(name[:i] + b + a + name[i + 2 :])
    return out


def _enumerate_duplicate_letter(name: str) -> set[str]:
    """All single-character duplications at alpha positions.

    "Stereolab" → "Steereolab" (position 2 doubled). Listener typos commonly
    insert a redundant letter when typing fast.
    """
    out: set[str] = set()
    for i in range(len(name)):
        if name[i].isalpha():
            out.add(name[: i + 1] + name[i] + name[i + 1 :])
    return out


def _enumerate_ascii_fold(name: str) -> set[str]:
    """ASCII-fold each diacritic position independently.

    "Hermanos Gutiérrez" → ["Hermanos Gutierrez"] (single é-fold).
    "Éliane Radigue" → ["Eliane Radigue"] (uppercase variant).
    Multi-diacritic names produce one variant per individual fold rather than
    the cross-product; the cross-product would over-represent the class
    relative to the other three corruption types.
    """
    out: set[str] = set()
    lower_name = name.lower()
    for fancy, plain in DIACRITIC_PAIRS:
        if fancy not in lower_name:
            continue
        for i, ch in enumerate(name):
            if ch.lower() != fancy:
                continue
            repl = plain.upper() if ch.isupper() else plain
            out.add(name[:i] + repl + name[i + 1 :])
    return out


def generate_typos(
    name: str,
    seed: int | None = None,
    max_typos: int = 3,
) -> list[str]:
    """Generate up to ``max_typos`` synthetic listener-typed corruptions of ``name``.

    Sampling is stratified by corruption class: when ``max_typos`` is at least
    the number of non-empty classes, each class contributes at least one
    candidate. Remaining slots are filled from the union of all classes.
    Without stratification, rare classes (like ASCII-fold for a name with one
    diacritic) get drowned out by the larger classes and the calibration loses
    coverage of that mode.

    Parameters
    ----------
    name : str
        The canonical artist name to corrupt.
    seed : int | None
        Random seed for reproducible sampling. Different seeds produce
        different subsets of the corruption candidate set; the candidate set
        itself is fixed by ``name``.
    max_typos : int
        Hard cap on the returned list size.

    Returns
    -------
    list[str]
        Sorted list of synthetic corruptions, excluding the original ``name``.
        Empty list when ``name`` is too short to corrupt meaningfully.
    """
    if len(name) < 4:
        return []

    classes = [
        _enumerate_drop_letter(name),
        _enumerate_transpose(name),
        _enumerate_duplicate_letter(name),
        _enumerate_ascii_fold(name),
    ]
    for cs in classes:
        cs.discard(name)

    non_empty = [sorted(c) for c in classes if c]
    if not non_empty:
        return []

    rng = random.Random(seed)
    out: set[str] = set()

    if max_typos >= len(non_empty):
        # Guarantee coverage: one sample from each non-empty class.
        for cls in non_empty:
            out.add(rng.choice(cls))

    pool = sorted(set().union(*non_empty) - out)
    remaining = max_typos - len(out)
    if remaining > 0 and pool:
        out.update(rng.sample(pool, min(remaining, len(pool))))

    return sorted(out)
