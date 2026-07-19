"""Shared CSV id parsing for the one-shot seeding scripts.

Both ``seed_library_release_overrides.py`` and ``resolve_master_overrides.py``
feed the same ``lml_cache.library_release_override`` table, so they must agree
on exactly which raw CSV cells count as a usable id. Keeping the rule in one
place stops the two scripts drifting (e.g. one accepting a value the other
drops). Import-light on purpose: no DB or config imports, so unit suites that
only need the parser stay fast.
"""

from __future__ import annotations


def parse_positive_int(raw: str | None) -> int | None:
    """Parse a raw CSV cell into a positive int, or ``None`` if unusable.

    Returns ``None`` for missing, blank, non-integer, zero, or negative values
    (a non-positive id can never be a real ``card_catalog_id`` or Discogs id).
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None
