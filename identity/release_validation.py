"""Validation and coercion for the release-identity resolve endpoint.

Pure functions — no I/O. Both the FastAPI handler
(``POST /api/v1/identity/resolve``) and the entity store's
``mint_or_get_release_identity`` call into this module so the
``(source, external_id)`` sentinel rules and column dispatch stay in one
place.

The flow is:

1. Handler runs ``validate_and_canonicalize_external_id(source, external_id)``;
   on ``InvalidReleaseExternalIdError`` returns 422 *before* any DB write — no
   poisoned identity rows ever land in ``entity.release_identity``.
2. Handler passes the canonical form to ``mint_or_get_release_identity``.
3. The store calls ``coerce_external_id(source, canonical)`` to get the bind
   value (int for Discogs sources, str for Bandcamp) and ``RELEASE_SOURCE_COLUMN[source]``
   for the column name. The resulting parameterised SQL is concurrency-safe
   via per-source ``UNIQUE`` constraints (see ``entity/release_identity.sql``).
"""

from __future__ import annotations

import re

from release.url_parser import parse_url

# Strict positive-decimal-integer match. Forbids leading sign / whitespace /
# underscores / leading zeros — bare ``int()`` quietly accepts ``" 12 "``,
# ``"+12"`` and ``"12_000"`` (PEP 515), which would coerce to a different
# Discogs release ID than the caller meant and the reconciliation log row
# would carry the verbatim non-canonical string. ``[1-9][0-9]*`` also
# rejects ``"0"`` so the dedicated sentinel error message below stays the
# only path that explains the Discogs ``0`` placeholder.
_DISCOGS_POSITIVE_INT_RE = re.compile(r"[1-9][0-9]*")

# Map a release-identity source key to the ``entity.release_identity`` column
# that holds its external ID. The set here is the v1 source list from LML#526;
# new sources (e.g. ``musicbrainz_release``, ``spotify_album``) get added in
# lockstep with their column on the table and a matching sentinel rule below.
RELEASE_SOURCE_COLUMN: dict[str, str] = {
    "discogs_release": "discogs_release_id",
    "discogs_master": "discogs_master_id",
    "bandcamp": "bandcamp_album_url",
}


class InvalidReleaseExternalIdError(ValueError):
    """Raised when ``(source, external_id)`` fails per-source sentinel rules.

    The handler converts this to HTTP 422. The exception message is safe to
    surface to API clients — it only describes the input shape, not anything
    about the entity store.
    """


def _validate_discogs_positive_int(source: str, external_id: str) -> str:
    """Discogs release/master IDs: positive decimal integer, rejecting ``0``.

    Discogs uses ``0`` to mark the unknown-release / unknown-master placeholder.
    Negative IDs are never valid. The match against ``[1-9][0-9]*`` is strict —
    leading signs (``"+12"``), whitespace (``" 12 "``), and PEP 515 underscore
    separators (``"12_000"``) are all rejected, because bare ``int()`` would
    silently accept them and coerce to a *different* Discogs release than the
    caller meant. Returns the input verbatim on success.
    """
    if external_id == "0":
        raise InvalidReleaseExternalIdError(
            f"{source} external_id must be > 0; 0 is the Discogs unknown-release sentinel."
        )
    if not _DISCOGS_POSITIVE_INT_RE.fullmatch(external_id):
        raise InvalidReleaseExternalIdError(
            f"{source} external_id must be a positive decimal integer "
            f"(digits only, no leading sign / whitespace / underscores / "
            f"leading zeros), got {external_id!r}"
        )
    return external_id


def _validate_bandcamp_album_url(external_id: str) -> str:
    """Bandcamp: must parse via ``release.url_parser.parse_url`` as a bandcamp album.

    Returns the parser's canonical form (trailing slash stripped, lowercased
    host) so two callers passing equivalent URLs collapse onto the same
    ``entity.release_identity`` row.
    """
    parsed = parse_url(external_id)
    if parsed is None or parsed.source != "bandcamp":
        raise InvalidReleaseExternalIdError(
            f"bandcamp external_id must be a Bandcamp album URL, got {external_id!r}"
        )
    return parsed.identifier


def validate_and_canonicalize_external_id(source: str, external_id: str) -> str:
    """Run per-source sentinel rules and return the canonical external_id form.

    Args:
        source: One of the keys in ``RELEASE_SOURCE_COLUMN``.
        external_id: The raw input from the request body.

    Returns:
        The canonical form of ``external_id`` — Bandcamp URLs are
        URL-canonicalised; Discogs IDs are returned verbatim (already
        integer-shaped after validation).

    Raises:
        InvalidReleaseExternalIdError: If ``source`` is unknown or the input fails
            its per-source sentinel rule.
    """
    if source not in RELEASE_SOURCE_COLUMN:
        raise InvalidReleaseExternalIdError(f"unknown release-identity source: {source!r}")
    if source in ("discogs_release", "discogs_master"):
        return _validate_discogs_positive_int(source, external_id)
    if source == "bandcamp":
        return _validate_bandcamp_album_url(external_id)
    # Defensive — would only fire on a future source added to the dict but
    # not wired here. Keeps the type-checker happy and surfaces gaps fast.
    raise InvalidReleaseExternalIdError(f"no sentinel rule registered for source: {source!r}")


def coerce_external_id(source: str, external_id: str) -> int | str:
    """Coerce a (post-validation) external_id to its asyncpg bind type.

    Discogs columns are ``INTEGER``; Bandcamp is ``TEXT``. asyncpg's
    column-type binding rejects a string passed to an INTEGER column, so the
    store must hand it the right Python type up front.

    Assumes the input already passed ``validate_and_canonicalize_external_id``.
    Raises ``KeyError`` on an unknown source (programmer error — pydantic and
    validation both block this upstream).
    """
    column = RELEASE_SOURCE_COLUMN[source]
    if source in ("discogs_release", "discogs_master"):
        return int(external_id)
    if column != "bandcamp_album_url":
        # A new TEXT-bound source was added to RELEASE_SOURCE_COLUMN but the
        # if-chain above and below were not extended. Raising explicitly
        # (rather than a bare `assert`) keeps the guard intact under
        # ``python -O``, which strips asserts. Programmer error — pydantic
        # and validate_and_canonicalize_external_id both block this upstream,
        # so end users never see it.
        raise RuntimeError(
            f"coerce_external_id has no branch for source={source!r} "
            f"(column={column!r}); add it alongside the RELEASE_SOURCE_COLUMN entry."
        )
    return external_id
