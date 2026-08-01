"""LML#1053: resolve the per-service streaming resolution verdict.

Merges ``enrich_one``'s own per-service signals (the librarian
``library_db.streaming_links`` override + the Apple in-item.py probe) with
``apply_streaming_url_postprocess``'s per-service verdict into
``DiscogsSearchResult.streaming_status`` — the field that disambiguates a
null ``*_url`` as ``absent`` (consulted, no match, terminal) vs.
``unresolved`` (probe attempted but inconclusive, transient) vs. omitted
entirely (never consulted at all). See LML#1053 / BS#1819.

Extracted out of ``lookup/enrichment/item.py`` (LML module-budget guardrail,
``tests/unit/test_module_budgets.py``) rather than left inline — the merge
logic is a pure function with no dependency on the rest of ``enrich_one``'s
control flow, so it is also independently unit-testable.
"""

from __future__ import annotations

from generated.api_models import StreamingResolution, StreamingResolutionStatus


def resolve_streaming_status(
    *,
    apple_music_override: str | None,
    apple_probe_status: StreamingResolutionStatus | None,
    spotify_url: str | None,
    bandcamp_url: str | None,
    postprocess_status: dict[str, StreamingResolutionStatus] | None,
) -> StreamingResolution | None:
    """Build the final ``StreamingResolution`` (or ``None``) for one item.

    Override precedence mirrors the final URL assignment in ``enrich_one``
    (``apple_music_override or apple_music_url or None``): a librarian
    override always wins the URL slot, so it always wins "verified" too,
    regardless of what the Apple probe concluded (or whether it ran at all —
    ``enrich_one`` short-circuits the probe precisely when an override is
    already going to win). Spotify/Bandcamp have no in-``enrich_one`` probe
    leg — only the librarian override — so a present URL there is always
    ``verified``; otherwise their verdict is left to ``postprocess_status``.

    ``postprocess_status`` (the ``apply_streaming_url_postprocess`` return
    value) is authoritative for any service it actually consulted: only keys
    present there are merged in, and they OVERRIDE this function's own
    per-service verdicts — e.g. a completed Apple probe's ``absent`` stands
    unless the post-process's REQUEST-scoped cache separately resolves it.
    Accepts a non-dict (e.g. a loosely-configured test double) as "nothing to
    merge" rather than raising.

    Returns ``None`` (not an empty object) when nothing was consulted at
    all — the never-consulted state is represented by key/field absence, per
    the wire contract, all the way up.
    """
    apple_status = (
        StreamingResolutionStatus.verified if apple_music_override else apple_probe_status
    )
    spotify_status = StreamingResolutionStatus.verified if spotify_url else None
    bandcamp_status = StreamingResolutionStatus.verified if bandcamp_url else None

    status_map: dict[str, StreamingResolutionStatus] = {
        service: status
        for service, status in {
            "apple_music": apple_status,
            "spotify": spotify_status,
            "bandcamp": bandcamp_status,
        }.items()
        if status is not None
    }
    if isinstance(postprocess_status, dict):
        status_map.update(postprocess_status)
    return StreamingResolution(**status_map) if status_map else None
