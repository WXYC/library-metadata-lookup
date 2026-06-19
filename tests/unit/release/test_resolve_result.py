"""Pins the consolidated ``ResolveResult`` shape (LML#609).

``BandcampResolveResult`` and ``DiscogsResolveResult`` were byte-for-byte
identical 3-field dataclasses. They are consolidated into a single
``release.models.ResolveResult``; the two old names must be gone (clean
rename, no back-compat aliases).
"""

from __future__ import annotations

import release.bandcamp_resolver as bandcamp_resolver
import release.discogs_resolver as discogs_resolver
from release.models import CanonicalRelease, ReleaseIdentifiers, ResolveResult


def test_resolve_result_lives_in_release_models():
    result = ResolveResult(
        canonical=CanonicalRelease(artist="Juana Molina", title="DOGA"),
        identifiers=ReleaseIdentifiers(),
        warnings=[],
    )
    assert result.canonical is not None
    assert result.canonical.artist == "Juana Molina"
    assert result.identifiers == ReleaseIdentifiers()
    assert result.warnings == []


def test_resolve_result_accepts_none_canonical():
    result = ResolveResult(canonical=None, identifiers=ReleaseIdentifiers(), warnings=["nope"])
    assert result.canonical is None
    assert result.warnings == ["nope"]


def test_old_result_type_names_are_gone():
    assert not hasattr(bandcamp_resolver, "BandcampResolveResult")
    assert not hasattr(discogs_resolver, "DiscogsResolveResult")
