"""Live Wikipedia smoke for ``clients.wikipedia.WikipediaClient`` (LML#513/#1192).

One ``external_api``-marked suite pinning the REST ``/page/summary``
response-shape assumptions this client's parsing depends on, against
Wikipedia's real, keyless, unauthenticated endpoint. Runtime-skip-on-
unanswerable (the house pattern — ``tests/integration/test_search_artists_live.py``):
a transient :class:`~clients.wikipedia.WikipediaFetchError` (timeout,
network error, a transient non-200) skips rather than fails. This is
deliberately NOT an env-var opt-in gate (no environment would ever set one,
so the test would guard nothing) and NOT a credential skip (Wikipedia needs
no token, unlike the Discogs live smokes) — the CI External API lane runs
``-m external_api`` unconditionally in a serial job with no credential check
to skip on otherwise, so unanswerable-skip is what keeps this from being
flake there. Goes red only when Wikipedia's own response shape has moved —
the ``type``/``extract`` fields ``_parse_summary`` depends on, or the
``type: "disambiguation"`` convention ``_parse_summary`` rejects on.

Run with: ``pytest -m external_api tests/integration/test_wikipedia_client_live.py``
"""

from __future__ import annotations

import pytest

from clients.wikipedia import WikipediaClient, WikipediaFetchError


def _skip_if_unanswerable(exc: WikipediaFetchError) -> None:
    pytest.skip(f"Wikipedia probe unanswerable this run (network/rate-limit/transient): {exc}")


@pytest.mark.external_api
class TestWikipediaClientLive:
    @pytest.mark.asyncio
    async def test_standard_page_shape_assumptions(self):
        """Stereolab has a real, stable Wikipedia page (the same artist
        this repo's example-data convention uses throughout — see
        CLAUDE.md) — pins that a standard artist page still returns
        ``type: "standard"`` plus a non-empty ``extract``, the two fields
        ``_parse_summary`` depends on."""
        client = WikipediaClient()
        try:
            summary = await client.get_summary("Stereolab", "en")
        except WikipediaFetchError as exc:
            _skip_if_unanswerable(exc)
            return
        assert summary is not None, (
            "Wikipedia's REST API no longer returns a standard-page summary for "
            "Stereolab -- either the page was renamed/deleted, or the response "
            "shape (type/extract fields) has changed."
        )
        assert isinstance(summary.extract, str) and summary.extract.strip()

    @pytest.mark.asyncio
    async def test_disambiguation_page_returns_none(self):
        """A known disambiguation page still returns ``type:
        "disambiguation"`` — pins the type-based rejection
        ``_parse_summary`` depends on."""
        client = WikipediaClient()
        try:
            summary = await client.get_summary("Mercury", "en")
        except WikipediaFetchError as exc:
            _skip_if_unanswerable(exc)
            return
        assert summary is None, (
            "Wikipedia's REST API no longer marks 'Mercury' as a disambiguation "
            "page -- the type field this client rejects on may have changed shape."
        )

    @pytest.mark.asyncio
    async def test_nonexistent_page_returns_none(self):
        """A page that (almost certainly) does not exist returns 404 —
        pins the 404-as-negative branch."""
        client = WikipediaClient()
        try:
            summary = await client.get_summary(
                "Zzzz_Definitely_Not_A_Real_Wikipedia_Page_Zzzz_LML1192", "en"
            )
        except WikipediaFetchError as exc:
            _skip_if_unanswerable(exc)
            return
        assert summary is None
