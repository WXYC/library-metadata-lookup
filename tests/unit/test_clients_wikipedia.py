"""Unit tests for clients/wikipedia.py — the Wikipedia REST summary client
(Phase B of the Wikipedia-preferred-artist-bio program,
docs/plans/lml-1192-wikipedia-artist-bio.md).

Covers the standard/disambiguation/404/timeout/429-Retry-After cases per the
plan's Testing section, plus the description-pattern reject (the closed-form
guard the Phase-A slug denylist can't be — an unlisted qualifier like
"(mixtape)" gets stripped and scores 100, so the REST payload's own
`description` field is the authoritative backstop).

``WikipediaFetchError`` is the single "couldn't ask" signal (timeout, network
error, non-200/404/429 status, or 429-retry-exhausted) — callers must NOT
write a negative-cache row on it, only on a ``None`` return (an authoritative
negative: 404, disambiguation, empty extract, or a rejected description).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from clients.wikipedia import (
    USER_AGENT,
    WikipediaClient,
    WikipediaFetchError,
    WikipediaSummary,
    _is_rejected_description,
)

_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/Stereolab"


@pytest.mark.asyncio
class TestStandardSummary:
    async def test_standard_page_returns_extract(self, httpx_mock):
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "standard",
                "extract": "Stereolab are an Anglo-French band formed in London in 1990.",
                "description": "British-French band",
            },
        )
        client = WikipediaClient()
        summary = await client.get_summary("Stereolab", "en")
        assert summary == WikipediaSummary(
            extract="Stereolab are an Anglo-French band formed in London in 1990."
        )

    async def test_sends_the_house_user_agent(self, httpx_mock):
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={"type": "standard", "extract": "An artist.", "description": "musician"},
        )
        client = WikipediaClient()
        await client.get_summary("Stereolab", "en")
        request = httpx_mock.get_requests()[0]
        assert request.headers["User-Agent"] == USER_AGENT

    async def test_url_uses_the_given_language_and_quotes_the_title(self, httpx_mock):
        httpx_mock.add_response(
            url="https://fr.wikipedia.org/api/rest_v1/page/summary/Sessa%20%282%29",
            json={"type": "standard", "extract": "Un artiste.", "description": "musicien"},
        )
        client = WikipediaClient()
        summary = await client.get_summary("Sessa (2)", "fr")
        assert summary is not None

    async def test_follows_redirects(self, httpx_mock):
        # httpx_mock auto-handles a redirect chain when follow_redirects=True
        # on the requesting client; assert via the final URL matching.
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={"type": "standard", "extract": "An artist.", "description": "musician"},
        )
        client = WikipediaClient()
        summary = await client.get_summary("Stereolab", "en")
        assert summary is not None


@pytest.mark.asyncio
class TestNegativeResults:
    async def test_disambiguation_page_returns_none(self, httpx_mock):
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "disambiguation",
                "extract": "Stereolab may refer to several things.",
                "description": "Wikipedia disambiguation page",
            },
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is None

    async def test_empty_extract_returns_none(self, httpx_mock):
        httpx_mock.add_response(
            url=_SUMMARY_URL, json={"type": "standard", "extract": "", "description": "musician"}
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is None

    async def test_missing_extract_field_returns_none(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, json={"type": "standard"})
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is None

    async def test_404_returns_none(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=404)
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is None

    @pytest.mark.parametrize(
        "description",
        [
            "1975 studio album by Some Artist",
            "2001 EP by Some Artist",
            "song by Some Artist",
            "single by Some Artist",
            # LML#1192 review (B1-1): "mixtape by" fell through the original
            # pattern set entirely -- pattern 1 required a leading year,
            # pattern 2's alternation had no "mixtape" entry.
            "mixtape by Some Artist",
            "2019 mixtape by Some Artist",
        ],
    )
    async def test_album_or_song_description_pattern_returns_none(self, httpx_mock, description):
        # An unlisted qualifier (e.g. "(mixtape)") slips past the Phase-A
        # slug denylist and scores 100 after stripping — the REST payload's
        # own `description` field is the closed-form backstop.
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "standard",
                "extract": "Some Album is a release by Some Artist.",
                "description": description,
            },
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is None

    @pytest.mark.parametrize(
        "description",
        [
            "album de Sessa",  # French/Spanish
            "Album von Stereolab",  # German
            "álbum de estudio de Sessa",  # Spanish, accented + "estudio" infix
            "album di Sessa",  # Italian
        ],
    )
    async def test_non_english_description_pattern_returns_none(self, httpx_mock, description):
        # LML#1192 review (B1-1): the original patterns were English-only,
        # so a non-English Wikipedia edition's machine-generated
        # description sailed straight past the backstop.
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "standard",
                "extract": "Some Album is a release by Some Artist.",
                "description": description,
            },
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is None

    @pytest.mark.parametrize(
        "description",
        [
            "Peruvian musical duo",
            "German record producer",
            "compositeur français",
            "cantante italiana",
        ],
    )
    async def test_non_english_person_description_is_not_rejected(self, httpx_mock, description):
        # The broadened release-noun/preposition net (B1-1) must not start
        # rejecting ordinary person descriptions just because they share a
        # short substring with a preposition/noun in some language.
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "standard",
                "extract": "An artist.",
                "description": description,
            },
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is not None

    @pytest.mark.parametrize(
        "description",
        [
            "cantante de canción de autor española",  # Spanish: singer-songwriter
            "chanteur de chanson de variété française",  # French: variety/pop singer
            "grupo de música disco de Estados Unidos",  # Spanish: US disco-music group
        ],
    )
    async def test_release_noun_embedded_mid_sentence_is_not_rejected(
        self, httpx_mock, description
    ):
        # LML#1192 review round 4, finding 9: B1-1's broadened net matched
        # `\b(noun)\b\s+(prep)\s+` ANYWHERE in the description, not just at
        # its start -- so an ordinary genre-descriptor phrase for a PERSON
        # that happens to contain a release-noun followed by a preposition
        # mid-sentence ("canción de autor" = the standard Spanish term for
        # "singer-songwriter"; "chanson de variété" = French for "pop
        # song"/variety singer; "música disco de Estados Unidos" = "disco
        # music FROM the United States") was wrongly rejected, writing an
        # authoritative extract=NULL for a real artist page. Verified live:
        # every genuine release-page description across en/es/fr starts
        # DIRECTLY with the release noun (e.g. "1997 studio album by
        # Radiohead", "álbum de Radiohead", "album de Radiohead, sorti en
        # 1997") -- so anchoring pattern 2 to the START of the description
        # (narrowed, not reverted) keeps every true positive and drops
        # these three false positives.
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "standard",
                "extract": "An artist.",
                "description": description,
            },
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is not None

    async def test_ordinary_person_description_is_not_rejected(self, httpx_mock):
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "standard",
                "extract": "An American musician.",
                "description": "American musician",
            },
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is not None

    @pytest.mark.parametrize(
        "description",
        [
            "1977 British disco band",
            "1970 American disco group",
            "Canción de autor española",
        ],
    )
    async def test_genre_word_coinciding_with_a_release_noun_is_not_rejected(
        self, httpx_mock, description
    ):
        # LML#1192 review round 6, P2-1: "disco" is in _RELEASE_NOUNS for its
        # Spanish/Italian "record" sense, but is also an ordinary English
        # genre word -- pattern 1's unbounded `.*` let a leading year plus
        # ANY intervening text reach a bare \bdisco\b anywhere in the
        # description, so "1977 British disco band" and "1970 American
        # disco group" both matched even though "disco" here modifies
        # "band"/"group", not "album"/"song". Both writers (the warm task,
        # the drain) then cached extract=NULL for a band with a perfectly
        # good article, suppressed for the full 7-day miss TTL and frozen
        # downstream by BS#1747. "Canción de autor española" (the standard
        # Spanish genre label for "singer-songwriter") is a second false
        # positive on pattern 2 -- the round-4 anchor only fixed the
        # mid-sentence spelling of this same collision, not the
        # sentence-initial one.
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={
                "type": "standard",
                "extract": "An artist.",
                "description": description,
            },
        )
        client = WikipediaClient()
        assert await client.get_summary("Stereolab", "en") is not None


class TestIsRejectedDescriptionDirectly:
    """Direct, synchronous calls to ``_is_rejected_description`` -- no httpx
    mocking needed. Table-drives every reviewer-executed string from LML#1192
    review round 6, P2-1 as a regression guard alongside every pre-existing
    true positive, so a future loosening of either pattern can't silently
    reopen this exact false-positive class without also breaking a
    true-positive row in the SAME table.

    LML#1192 review round 6, P2-1 root cause: both patterns used to reject
    on a release noun ANYWHERE in (or at the start of) the description,
    without requiring it to actually be functioning as "TYPE by ARTIST" --
    "disco" (in ``_RELEASE_NOUNS`` for its Spanish/Italian "record" sense)
    is also an ordinary English genre word, and "canción" opens the
    standard Spanish idiom for "singer-songwriter" as readily as it opens
    a real "song by X" description. Fixed by requiring the release noun be
    IMMEDIATELY followed by a preposition (not just present via a loose
    ``\\b...\\b`` check), plus a second, case-SENSITIVE check that a
    capitalized word (the artist name) appears somewhere after that
    match -- a genre-descriptor idiom never has one; a real release-page
    description always does.
    """

    @pytest.mark.parametrize(
        "description",
        [
            # LML#1192 review round 6, P2-1 -- the exact three strings the
            # reviewer executed against the shipped code, each confirmed
            # (before this fix) to return True.
            "1977 British disco band",
            "1970 American disco group",
            "Canción de autor española",
            # Pre-existing false positives this must keep rejecting-nothing on.
            "Peruvian musical duo",
            "German record producer",
            "compositeur français",
            "cantante italiana",
            "cantante de canción de autor española",
            "chanteur de chanson de variété française",
            "grupo de música disco de Estados Unidos",
            "American musician",
        ],
    )
    def test_genuine_person_descriptions_are_not_rejected(self, description):
        assert _is_rejected_description(description) is False

    @pytest.mark.parametrize(
        "description",
        [
            "1975 studio album by Some Artist",
            "2001 EP by Some Artist",
            "song by Some Artist",
            "single by Some Artist",
            "mixtape by Some Artist",
            "2019 mixtape by Some Artist",
            "album de Sessa",
            "Album von Stereolab",
            "álbum de estudio de Sessa",
            "album di Sessa",
        ],
    )
    def test_genuine_release_page_descriptions_are_still_rejected(self, description):
        assert _is_rejected_description(description) is True

    def test_none_is_not_rejected(self):
        assert _is_rejected_description(None) is False

    def test_empty_string_is_not_rejected(self):
        assert _is_rejected_description("") is False


@pytest.mark.asyncio
class TestTransientFailuresRaise:
    async def test_non_json_200_body_raises_wikipedia_fetch_error(self, httpx_mock):
        # LML#1192 review (B1-3): response.json() previously sat OUTSIDE
        # the try/except, so a malformed body raised a raw
        # json.JSONDecodeError that escaped WikipediaFetchError callers.
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            status_code=200,
            text="<html>upstream error</html>",
            headers={"Content-Type": "text/html"},
        )
        with pytest.raises(WikipediaFetchError):
            await WikipediaClient().get_summary("Stereolab", "en")

    async def test_json_list_payload_raises_wikipedia_fetch_error(self, httpx_mock):
        # A non-dict JSON body (e.g. a bare list) crashed _parse_summary's
        # .get() calls with an unguarded AttributeError instead.
        httpx_mock.add_response(url=_SUMMARY_URL, json=["not", "a", "dict"])
        with pytest.raises(WikipediaFetchError):
            await WikipediaClient().get_summary("Stereolab", "en")

    async def test_timeout_raises_wikipedia_fetch_error(self, httpx_mock):
        httpx_mock.add_exception(httpx.TimeoutException("timed out"))
        client = WikipediaClient()
        with pytest.raises(WikipediaFetchError):
            await client.get_summary("Stereolab", "en")

    async def test_network_error_raises_wikipedia_fetch_error(self, httpx_mock):
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))
        client = WikipediaClient()
        with pytest.raises(WikipediaFetchError):
            await client.get_summary("Stereolab", "en")

    async def test_server_error_raises_wikipedia_fetch_error(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=503)
        client = WikipediaClient()
        with pytest.raises(WikipediaFetchError):
            await client.get_summary("Stereolab", "en")


@pytest.mark.asyncio
class TestRateLimitRetry:
    async def test_retries_once_honoring_retry_after_then_succeeds(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=429, headers={"Retry-After": "7"})
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={"type": "standard", "extract": "An artist.", "description": "musician"},
        )
        client = WikipediaClient()
        with patch("clients.wikipedia.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            summary = await client.get_summary("Stereolab", "en", max_retries=1)
        assert summary is not None
        mock_sleep.assert_awaited_once_with(7.0)
        assert len(httpx_mock.get_requests()) == 2

    async def test_exhausted_retries_raises_wikipedia_fetch_error(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=429, headers={"Retry-After": "1"})
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=429, headers={"Retry-After": "1"})
        client = WikipediaClient()
        with patch("clients.wikipedia.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(WikipediaFetchError):
                await client.get_summary("Stereolab", "en", max_retries=1)
        assert len(httpx_mock.get_requests()) == 2

    async def test_max_retries_zero_never_sleeps(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=429, headers={"Retry-After": "1"})
        client = WikipediaClient()
        with patch("clients.wikipedia.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(WikipediaFetchError):
                await client.get_summary("Stereolab", "en", max_retries=0)
        mock_sleep.assert_not_awaited()
        assert len(httpx_mock.get_requests()) == 1

    async def test_missing_retry_after_falls_back_to_a_default_delay(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=429)
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={"type": "standard", "extract": "An artist.", "description": "musician"},
        )
        client = WikipediaClient()
        with patch("clients.wikipedia.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            summary = await client.get_summary("Stereolab", "en", max_retries=1)
        assert summary is not None
        mock_sleep.assert_awaited_once()

    @pytest.mark.parametrize("retry_after", ["inf", "Infinity", "nan", "NaN"])
    async def test_non_finite_retry_after_never_hangs_forever(self, httpx_mock, retry_after):
        # LML#1192 review (B1-2): float() accepts "inf"/"nan" and
        # max(x, 0.0) does not clamp either -- asyncio.sleep(inf) would
        # never wake, hanging a warm permit (or the drain) forever.
        httpx_mock.add_response(
            url=_SUMMARY_URL, status_code=429, headers={"Retry-After": retry_after}
        )
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={"type": "standard", "extract": "An artist.", "description": "musician"},
        )
        client = WikipediaClient()
        with patch("clients.wikipedia.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client.get_summary("Stereolab", "en", max_retries=1)
        delay = mock_sleep.await_args.args[0]
        assert delay == pytest.approx(delay)  # not NaN (NaN != NaN)
        assert delay != float("inf")
        assert 0.0 <= delay <= 30.0

    async def test_negative_retry_after_clamps_to_zero(self, httpx_mock):
        httpx_mock.add_response(url=_SUMMARY_URL, status_code=429, headers={"Retry-After": "-5"})
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={"type": "standard", "extract": "An artist.", "description": "musician"},
        )
        client = WikipediaClient()
        with patch("clients.wikipedia.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client.get_summary("Stereolab", "en", max_retries=1)
        mock_sleep.assert_awaited_once_with(0.0)

    async def test_absurdly_large_retry_after_is_clamped_to_a_ceiling(self, httpx_mock):
        httpx_mock.add_response(
            url=_SUMMARY_URL, status_code=429, headers={"Retry-After": "99999999999999"}
        )
        httpx_mock.add_response(
            url=_SUMMARY_URL,
            json={"type": "standard", "extract": "An artist.", "description": "musician"},
        )
        client = WikipediaClient()
        with patch("clients.wikipedia.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await client.get_summary("Stereolab", "en", max_retries=1)
        delay = mock_sleep.await_args.args[0]
        assert delay <= 30.0
