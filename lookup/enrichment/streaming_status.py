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

from collections.abc import Mapping

from generated.api_models import StreamingResolution, StreamingResolutionStatus

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

    A service absent from ``StreamingResolution`` is DROPPED from all three
    inputs rather than raised on: the model is generated from wxyc-shared's
    ``api.yaml``, so ``StreamingService`` can carry a member whose wire field
    has not shipped yet (LML#1103's ``youtube_music`` today). A not-yet-
    modelled verdict must degrade to an omitted key, never a 500 on the lookup
    path — the LML#444 posture the rest of ``enrich_one`` holds.

    Returns ``None`` (not an empty object) when nothing was consulted at
    all — the never-consulted state is represented by key/field absence, per
    the wire contract, all the way up.
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
    return StreamingResolution(**status_map) if status_map else None
