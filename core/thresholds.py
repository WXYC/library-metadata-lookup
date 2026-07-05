"""Shared score-floor constants used across packages.

LEAF-MODULE CONSTRAINT: this module must never import from ``lookup/``,
``release/``, or ``discogs/`` — it stays at zero non-stdlib imports.
``lookup/`` already imports ``release.musicbrainz_resolver``, so a constant
shared by both packages cannot live in either without creating an import
cycle; this leaf is what keeps the shared floor cycle-proof.
"""

CANONICAL_ARTIST_SIMILARITY_FLOOR: float = 0.70
"""Trigram-similarity floor for swapping an inbound artist name with its canonical
Discogs form.

Provisional. Replaced by the offline calibration sweep produced by
``scripts.resolver_calibration`` against the WXYC discogs-cache; see
``docs/resolver-calibration/README.md`` for the chosen value and its FP-rate
tolerance (target ≤ 0.5%). See WXYC/library-metadata-lookup#318.
"""
