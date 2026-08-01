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
from collections.abc import Callable
from dataclasses import dataclass

from release.url_parser import parse_url

# Strict positive-decimal-integer match. Forbids leading sign / whitespace /
# underscores / leading zeros — bare ``int()`` quietly accepts ``" 12 "``,
# ``"+12"`` and ``"12_000"`` (PEP 515), which would coerce to a different
# Discogs release ID than the caller meant and the reconciliation log row
# would carry the verbatim non-canonical string. ``[1-9][0-9]*`` also
# rejects ``"0"`` so the dedicated sentinel error message below stays the
# only path that explains the Discogs ``0`` placeholder. Re-used by the
# ``apple_music_album`` rule below — Apple Music album IDs share the same
# digits-only positive-integer shape (the URL parser admits 6+ digits;
# the validator keeps the bare positive-integer floor since the column
# is TEXT and the bind value stays str).
_DISCOGS_POSITIVE_INT_RE = re.compile(r"[1-9][0-9]*")

# Spotify album IDs are exactly 22 base62 characters (the ID embedded in
# ``open.spotify.com/album/<id>``). Opaque, not ordinal — no zero sentinel.
_SPOTIFY_ALBUM_ID_RE = re.compile(r"[0-9A-Za-z]{22}")


@dataclass(frozen=True)
class ReleaseSourceConfig:
    """Per-source identity-mint dispatch for ``entity.release_identity``.

    Holds *only* identity concerns — the column the source's external ID
    lands in, the sentinel ``validator`` run before any DB write, and the
    ``coercer`` that produces the asyncpg bind type. Cache/post-process
    concerns (TTL, probe timeout, URL parsing) live in the separate
    ``lookup.streaming_url_postprocess.StreamingUrlCacheConfig`` registry so
    Discogs entries (identity-only) don't carry Optional cache fields. The
    two registries have different cardinality on purpose — see
    ``RELEASE_SOURCE_CONFIG`` below.

    ``validator`` takes ``(source, external_id)`` (uniform across sources;
    the ``source`` arg lets one validator body serve multiple keys, e.g. the
    two Discogs entries) and returns the canonical external_id or raises
    ``InvalidReleaseExternalIdError``. ``coercer`` takes the post-validation
    external_id and returns its bind value (``int`` for INTEGER columns, the
    string verbatim for TEXT columns).
    """

    identity_column: str
    validator: Callable[[str, str], str]
    coercer: Callable[[str], int | str]


class InvalidReleaseExternalIdError(ValueError):
    """Raised when ``(source, external_id)`` fails per-source sentinel rules.

    The handler converts this to HTTP 422. The exception message is safe to
    surface to API clients — it only describes the input shape, not anything
    about the entity store.
    """


def _make_positive_int_validator(
    *, zero_sentinel_reason: str, doc: str | None = None
) -> Callable[[str, str], str]:
    """Build a positive-decimal-integer validator with a source-specific zero explanation.

    Shared by the Discogs (release/master) and Apple Music album ID rules —
    both matched ``_DISCOGS_POSITIVE_INT_RE.fullmatch`` and reported the same
    malformed-shape error text; the only thing that varied between the two
    hand-written functions was *why* ``0`` is rejected. ``zero_sentinel_reason``
    fills that clause verbatim (it follows ``"{source} external_id must be > 0; "``
    in the raised message), so each call site keeps its own wording.

    ``doc``, if given, becomes the returned validator's ``__doc__`` so a
    module-level assignment like
    ``_validate_discogs_positive_int = _make_positive_int_validator(...)``
    keeps a docstring for ``help()`` / IDE tooltips at the call site.
    """

    def validator(source: str, external_id: str) -> str:
        if external_id == "0":
            raise InvalidReleaseExternalIdError(
                f"{source} external_id must be > 0; {zero_sentinel_reason}"
            )
        if not _DISCOGS_POSITIVE_INT_RE.fullmatch(external_id):
            raise InvalidReleaseExternalIdError(
                f"{source} external_id must be a positive decimal integer "
                f"(digits only, no leading sign / whitespace / underscores / "
                f"leading zeros), got {external_id!r}"
            )
        return external_id

    if doc is not None:
        validator.__doc__ = doc
    return validator


_validate_discogs_positive_int = _make_positive_int_validator(
    zero_sentinel_reason="0 is the Discogs unknown-release sentinel.",
    doc="""Discogs release/master IDs: positive decimal integer, rejecting ``0``.

    Discogs uses ``0`` to mark the unknown-release / unknown-master placeholder.
    Negative IDs are never valid. The match against ``[1-9][0-9]*`` is strict —
    leading signs (``"+12"``), whitespace (``" 12 "``), and PEP 515 underscore
    separators (``"12_000"``) are all rejected, because bare ``int()`` would
    silently accept them and coerce to a *different* Discogs release than the
    caller meant. Returns the input verbatim on success.
    """,
)

_validate_apple_music_album_id = _make_positive_int_validator(
    zero_sentinel_reason="0 is not a valid Apple Music album ID.",
    doc="""Apple Music album IDs: positive decimal integer.

    Apple's URLs of the form
    ``music.apple.com/<locale>/album/<slug>/<id>`` use a numeric album ID
    (6+ digits in practice — see ``release.apple_music_url_parser.APPLE_ALBUM_ID_RE``).
    The validator keeps the same strict positive-integer posture as the
    Discogs rule: rejects ``"0"``, negatives, leading signs / whitespace /
    underscores / leading zeros. The 6-digit floor stays in the URL
    parser (a soft floor — admits Apple's historical catalog); the
    validator accepts any positive integer-shaped string so an upstream
    that hands a shorter ID is rejected on its own merits, not on a
    digit-count mismatch with the parser.
    """,
)


def _validate_bandcamp_album_url(source: str, external_id: str) -> str:
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


def _validate_spotify_album_id(source: str, external_id: str) -> str:
    """Spotify album IDs: exactly 22 base62 characters.

    Spotify's album open-graph URL (``open.spotify.com/album/<id>``) carries
    a 22-character base62 ID. The validator pins that exact shape so a
    malformed ID — surfaced by the looser URL parsers upstream, or handed in
    by a future caller — never lands in
    ``entity.release_identity.spotify_album_id``. Base62 IDs are opaque, not
    ordinal, so there is no zero-sentinel concept (unlike the Discogs / Apple
    rules). Returns the input verbatim on success; the column is TEXT.
    """
    if not _SPOTIFY_ALBUM_ID_RE.fullmatch(external_id):
        raise InvalidReleaseExternalIdError(
            f"{source} external_id must be a 22-character base62 Spotify album ID "
            f"(letters and digits only), got {external_id!r}"
        )
    return external_id


# Identity-mint dispatch registry. Each entry binds a source key to its
# ``entity.release_identity`` column, sentinel ``validator`` (uniform
# ``(source, external_id) -> str`` signature), and bind-type ``coercer``.
# Discogs columns are INTEGER (coerce to ``int``); Bandcamp / Apple / Spotify
# are TEXT (the string binds verbatim).
#
# This registry is deliberately a *superset* of
# ``lookup.streaming_url_postprocess.STREAMING_URL_CACHE_CONFIG``: Discogs
# release/master are identity-mintable but never URL-cached (they aren't
# resolved from a live streaming probe), and Bandcamp is identity-mintable
# but only joins the cache registry in PR-3. This registry stays keyed by the
# bare mint-source string (LML#1037 did not fold it into the
# ``streaming.service.StreamingService`` enum — it mixes two Discogs
# identity-only sources into an otherwise streaming-service-shaped key set,
# which is exactly the kind of asymmetric membership the enum's three
# vocabularies don't have). The parity test asserts the image of
# ``STREAMING_URL_CACHE_CONFIG``'s enum keys under
# ``streaming.service.ALBUM_CACHE_KEYS`` ⊆ ``RELEASE_SOURCE_CONFIG.keys()``.
# ``musicbrainz_release`` is intentionally absent — it is read-only today
# (no write helper / sentinel rule), readable via the store's
# ``_RELEASE_IDENTITY_COLUMN_TO_SOURCE`` tuple but not mintable here.
RELEASE_SOURCE_CONFIG: dict[str, ReleaseSourceConfig] = {
    "discogs_release": ReleaseSourceConfig(
        identity_column="discogs_release_id",
        validator=_validate_discogs_positive_int,
        coercer=int,
    ),
    "discogs_master": ReleaseSourceConfig(
        identity_column="discogs_master_id",
        validator=_validate_discogs_positive_int,
        coercer=int,
    ),
    "bandcamp": ReleaseSourceConfig(
        identity_column="bandcamp_album_url",
        validator=_validate_bandcamp_album_url,
        coercer=lambda external_id: external_id,
    ),
    "apple_music_album": ReleaseSourceConfig(
        identity_column="apple_music_album_id",
        # apple_music_album_id is TEXT — Apple's numeric IDs can grow past
        # INT32, and the column type matches Bandcamp's TEXT posture. The
        # bind value stays str so the INSERT, the conflict-fallthrough
        # SELECT, and the reconciliation log all see the same canonical form.
        validator=_validate_apple_music_album_id,
        coercer=lambda external_id: external_id,
    ),
    "spotify_album": ReleaseSourceConfig(
        identity_column="spotify_album_id",
        validator=_validate_spotify_album_id,
        coercer=lambda external_id: external_id,
    ),
}

# Backward-compat derived view: ``source -> column``. All existing call sites
# (entity/store.py, tests) read this; it stays in lockstep with the registry
# automatically. Do NOT hand-maintain it separately.
RELEASE_SOURCE_COLUMN: dict[str, str] = {
    source: cfg.identity_column for source, cfg in RELEASE_SOURCE_CONFIG.items()
}


def validate_and_canonicalize_external_id(source: str, external_id: str) -> str:
    """Run per-source sentinel rules and return the canonical external_id form.

    Args:
        source: One of the keys in ``RELEASE_SOURCE_CONFIG``.
        external_id: The raw input from the request body.

    Returns:
        The canonical form of ``external_id`` — Bandcamp URLs are
        URL-canonicalised; Discogs / Apple / Spotify IDs are returned verbatim
        (already canonically shaped after validation).

    Raises:
        InvalidReleaseExternalIdError: If ``source`` is unknown or the input fails
            its per-source sentinel rule.
    """
    cfg = RELEASE_SOURCE_CONFIG.get(source)
    if cfg is None:
        raise InvalidReleaseExternalIdError(f"unknown release-identity source: {source!r}")
    return cfg.validator(source, external_id)


def coerce_external_id(source: str, external_id: str) -> int | str:
    """Coerce a (post-validation) external_id to its asyncpg bind type.

    Discogs columns are ``INTEGER``; Bandcamp / Apple Music / Spotify are
    ``TEXT``. asyncpg's column-type binding rejects a string passed to an
    INTEGER column, so the store must hand it the right Python type up front.

    Assumes the input already passed ``validate_and_canonicalize_external_id``.
    Raises ``KeyError`` on an unknown source (programmer error — pydantic and
    validation both block this upstream).
    """
    return RELEASE_SOURCE_CONFIG[source].coercer(external_id)
