"""Live Bandcamp smoke for ``BandcampClient.find_album_match_via_search`` (LML#1069).

One ``external_api``-marked test pinning the LML#1069 measurement's golden
repro end-to-end against the real autocomplete API: George Theodorakis's
"The Rules of the Game" (library row id 56319) is hosted on a reissue
label's own subdomain (``into-the-light.bandcamp.com``), not the artist's
own page (``georgerakis.bandcamp.com``) -- so only the album-first search
path added by this PR, not the pre-existing artist-first
``find_album_match``, can locate it. See
``docs/plans/lml-1069-bandcamp-album-first.md``'s Appendix for the fixture this
was captured from.

Skip-vs-fail follows the live Discogs smoke's split
(tests/integration/test_search_artists_live.py): a request-layer failure
(``BandcampSearchUnavailableError`` -- network error, non-200, exhausted 429
backoff) is "couldn't ask" and skips so the smoke's real signal isn't
drowned by transient flakiness. A successful request that finds no
floor-clearing match is "asked and the contract moved" (Bandcamp's index
dropped the release, or the doubled-URL/response-shape quirk changed) and
fails loudly -- that drift is exactly what this smoke exists to catch (the
LML#1069 plan's "Unofficial API drift" risk). No API token is required;
Bandcamp's autocomplete endpoint is unauthenticated.

Run with: ``pytest -m external_api tests/integration/test_bandcamp_album_search_live.py``
"""

from __future__ import annotations

import pytest

from clients.bandcamp import BandcampClient, BandcampSearchUnavailableError

GOLDEN_REPRO_ARTIST = "George Theodorakis"
GOLDEN_REPRO_TITLE = "The Rules of the Game: Original Studio Recordings (1978-1996)"
GOLDEN_REPRO_HOST = "into-the-light.bandcamp.com"


@pytest.mark.external_api
class TestBandcampAlbumSearchLive:
    @pytest.mark.asyncio
    async def test_golden_repro_finds_label_hosted_album(self):
        client = BandcampClient()
        try:
            try:
                match = await client.find_album_match_via_search(
                    GOLDEN_REPRO_ARTIST, GOLDEN_REPRO_TITLE
                )
            except BandcampSearchUnavailableError:
                pytest.skip(
                    "Bandcamp autocomplete probe unanswerable this run (network/rate-limit)"
                )
        finally:
            await client.close()

        if match is None:
            pytest.fail(
                "Bandcamp autocomplete answered but found no floor-clearing match "
                f"for the golden repro ({GOLDEN_REPRO_ARTIST!r}, {GOLDEN_REPRO_TITLE!r}). "
                "If Bandcamp's index dropped this release, pick a new stable "
                "label-hosted golden repro for this smoke."
            )

        assert GOLDEN_REPRO_HOST in match.url, (
            f"Expected the reissue-label host {GOLDEN_REPRO_HOST!r} in the matched "
            f"URL, got {match.url!r} -- response-shape drift in the autocomplete "
            "API (e.g. the doubled-url quirk changed shape)."
        )
        assert match.confidence >= 80.0
