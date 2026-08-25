"""LML#1053: resolve the per-service streaming resolution verdict.

Merges ``enrich_one``'s own per-service signals (the librarian
``library_db.streaming_links`` override + the inline live probes) with
``apply_streaming_url_postprocess``'s per-service verdict into
``DiscogsSearchResult.streaming_status`` — the field that disambiguates a
null ``*_url`` as ``absent`` (consulted, no match, terminal) vs.
``unresolved`` (probe attempted but inconclusive, transient) vs. omitted
entirely (never consulted at all). See LML#1053 / BS#1819.

Extracted out of ``lookup/enrichment/item.py`` (LML module-budget guardrail,
``tests/unit/test_module_budgets.py``) rather than left inline — the merge
logic is a pure function with no dependency on the rest of ``enrich_one``'s
control flow, so it is also independently unit-testable.

LML#1101 made the per-service signals MAPS. The prior signature took two
positional parameters per probing service (``apple_music_override`` +
``apple_probe_status``, then ``bandcamp_url`` + ``bandcamp_probe_status``), so
every new probe leg widened it — LML#1103's YouTube Music would have made
eight. Adding a probing service is now a new key at the call site and no
change here at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from generated.api_models import StreamingResolution, StreamingResolutionStatus

logger = logging.getLogger(__name__)

#: Services ``StreamingResolution`` actually models, as generated from
#: wxyc-shared's ``api.yaml``. Read off the model rather than hardcoded so a
#: field added upstream (LML#1103's ``youtube_music``) starts flowing through
#: on the regen alone, with no edit here.
_MODELLED_SERVICES = frozenset(StreamingResolution.model_fields)


def resolve_streaming_status(
    *,
    verified_urls: Mapping[str, str | None],
    probe_status: Mapping[str, StreamingResolutionStatus | None],
    postprocess_status: Mapping[str, StreamingResolutionStatus] | None,
) -> StreamingResolution | None:
    """Build the final ``StreamingResolution`` (or ``None``) for one item.

    Keys are catalog-key strings — pass ``StreamingService`` members, which
    are ``StrEnum`` and so hash and compare equal to the bare names the
    ``postprocess_status`` dict uses.

    ``verified_urls`` maps a service to the URL that, if present, FORCES a
    ``verified`` verdict for it. That is the URL which will win the slot in
    ``enrich_one``'s final assignment regardless of what the probe concluded:
    for Apple the librarian override alone (``apple_music_override or
    apple_music_url or None`` — ``enrich_one`` short-circuits the probe
    precisely when an override is already going to win, and a probe-resolved
    URL reports itself through ``probe_status``); for Spotify the librarian
    override alone (no in-``enrich_one`` probe leg exists); for Bandcamp the
    post-probe slot, since LML#1098's probe writes its resolved URL back there
    and reports ``verified`` alongside it. An empty string is not a URL.

    ``probe_status`` is the live probe's own verdict for a service — ``absent``
    on a sourced no-match, ``unresolved`` on a shed/timeout, ``None`` or a
    missing key when it never ran.

    ``postprocess_status`` (the ``apply_streaming_url_postprocess`` return
    value) is authoritative for any service it actually consulted: it is
    merged LAST and OVERRIDES both of the above — e.g. a completed Apple
    probe's ``absent`` stands unless the post-process's REQUEST-scoped cache
    separately resolves it. Accepts a non-Mapping (e.g. a loosely-configured
    test double) as "nothing to merge" rather than raising.

    Only URLs that PROVE resolution belong in ``verified_urls``. A templated
    search-URL fallback is not a resolution — see
    ``lookup/enrichment/item.py``'s ``_RESOLUTION_PROVING_URL_SERVICES`` for
    which slots qualify at the call site and why YouTube Music / SoundCloud do
    not.

    A service absent from ``StreamingResolution`` is DROPPED from all three
    inputs (at DEBUG) rather than raised on: the model is generated from
    wxyc-shared's ``api.yaml``, so ``StreamingService`` can carry a member
    whose wire field has not shipped yet (LML#1103's ``youtube_music`` today).
    A not-yet-modelled verdict must degrade to an omitted key, never a 500 on
    the lookup path — the LML#444 posture the rest of ``enrich_one`` holds. The
    log line is what makes that degrade diagnosable: without it, a probe leg
    that ships ahead of its wire field burns quota every request and its
    verdict vanishes indistinguishably from "never consulted".

    Returns ``None`` (not an empty object) when nothing was consulted at
    all — the never-consulted state is represented by key/field absence, per
    the wire contract, all the way up. Note this now also covers the case where
    the ONLY signals were for unmodelled services: pre-LML#1101 an unmodelled
    ``postprocess_status`` key left ``status_map`` non-empty and produced an
    all-null ``StreamingResolution()`` (pydantic's ``extra='ignore'`` swallowed
    the kwarg), which serializes as ``streaming_status: {}`` rather than an
    omitted key. **Reachable as of LML#1103**: ``apply_streaming_url_postprocess``
    now emits four ``STREAMING_URL_CACHE_CONFIG`` catalog keys, and
    ``youtube_music`` is not yet a ``StreamingResolution`` field. With only
    ``lml_persist_streaming_url_youtube_music`` enabled, the sole verdict is
    that unmodelled key, ``_MODELLED_SERVICES`` drops it, and ``status_map``
    lands empty — so this branch is what keeps that configuration returning an
    omitted key rather than ``{}``. The omitted-key shape is the correct one
    for a field whose contract is "absence means never-consulted".
    """
    status_map: dict[str, StreamingResolutionStatus] = {}
    for service in _MODELLED_SERVICES:
        status = (
            StreamingResolutionStatus.verified
            if verified_urls.get(service)
            else probe_status.get(service)
        )
        if status is not None:
            status_map[service] = status

    if isinstance(postprocess_status, Mapping):
        status_map.update(
            {
                service: status
                for service, status in postprocess_status.items()
                if service in _MODELLED_SERVICES
            }
        )

    dropped = sorted(
        (
            {s for s, u in verified_urls.items() if u}
            | {s for s, v in probe_status.items() if v is not None}
            | (set(postprocess_status) if isinstance(postprocess_status, Mapping) else set())
        )
        - _MODELLED_SERVICES
    )
    if dropped:
        logger.debug(
            "Dropping streaming_status verdicts for services StreamingResolution "
            "does not model: %s — the wxyc-shared api.yaml field has not shipped yet",
            ", ".join(dropped),
        )
    return StreamingResolution(**status_map) if status_map else None
