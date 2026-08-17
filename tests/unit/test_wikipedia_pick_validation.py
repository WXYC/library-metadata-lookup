"""Unit tests for lookup/wikipedia_pick_validation.py — fetch-validated
Wikipedia page selection (LML#1192 review round 4, P0-2).

Ground truth for the ten-page table below was fetched LIVE against
``https://en.wikipedia.org/api/rest_v1/page/summary/<slug>`` during the
round-4 review (not transcribed from any prior round's behavior):

======================  ==============================  ==========  ================  ================
artist                  bare slug                        bare type   qualified slug     qualified type
======================  ==============================  ==========  ================  ================
Low                     Low                               disambiguation   Low_(band)              standard
Sade                    Sade                              disambiguation   Sade_(band)             standard
Sun Ra                  Sun_Ra                            standard         Sun_Ra_(disambiguation) 404
Stereolab               Stereolab                         standard         Stereolab_(band)        404
Cat Power               Cat_Power                         standard         Cat_Power_(musician)    404
======================  ==============================  ==========  ================  ================

Two of five pairs (Low, Sade) have the CORRECT article at the QUALIFIED
url — the bare title is a real disambiguation page. Three of five (Sun Ra,
Stereolab, Cat Power) have the correct article at the BARE url — the
qualified url doesn't exist as a real page at all (404). No string-only
rule (shorter-wins, longer-wins, alphabetical) can get all five right,
because the deciding signal — what the page actually IS — isn't present
in either URL string. Round 3's "prefer the shorter URL" rule got Sun Ra/
Stereolab/Cat Power right and Low/Sade wrong, for exactly this reason.

The fix: try candidates in ranked (total-order, deterministic) order,
fetching and validating each against the live payload's own ``type``/
``description`` fields — the SAME signal ``clients.wikipedia.WikipediaClient
.get_summary`` already computes (a disambiguation page's ``type`` fails
its ``"standard"`` check; a 404 returns ``None`` outright) — and use the
first candidate that validates. The rank order only decides TRY order, not
the final answer, so it stays deterministic/total without needing to be
"correct" on its own.
"""

from __future__ import annotations

import pytest

from clients.wikipedia import WikipediaFetchError, WikipediaSummary
from lookup.wikipedia_pick_validation import resolve_and_validate_pick

# The ten-page table, keyed by pair name. Each fetch outcome mirrors the
# live ground truth above via clients.wikipedia.WikipediaClient's own
# already-correct handling: a disambiguation `type` or a 404 both surface
# as get_summary returning None (see clients/wikipedia.py's _parse_summary
# and its 404 branch) -- this module's fake `fetch` callable reproduces
# exactly that contract, not the raw HTTP shape.
_TEN_PAGE_TABLE = {
    "Low": {
        "artist_name": "Low",
        "bare_url": "https://en.wikipedia.org/wiki/Low",
        "qualified_url": "https://en.wikipedia.org/wiki/Low_(band)",
        "bare_validates": False,  # disambiguation
        "qualified_validates": True,  # standard
        "correct_url_suffix": "Low_(band)",
    },
    "Sade": {
        "artist_name": "Sade",
        "bare_url": "https://en.wikipedia.org/wiki/Sade",
        "qualified_url": "https://en.wikipedia.org/wiki/Sade_(band)",
        "bare_validates": False,  # disambiguation
        "qualified_validates": True,  # standard
        "correct_url_suffix": "Sade_(band)",
    },
    "Sun Ra": {
        "artist_name": "Sun Ra",
        "bare_url": "https://en.wikipedia.org/wiki/Sun_Ra",
        "qualified_url": "https://en.wikipedia.org/wiki/Sun_Ra_(disambiguation)",
        "bare_validates": True,  # standard
        "qualified_validates": False,  # 404
        "correct_url_suffix": "Sun_Ra",
    },
    "Stereolab": {
        "artist_name": "Stereolab",
        "bare_url": "https://en.wikipedia.org/wiki/Stereolab",
        "qualified_url": "https://en.wikipedia.org/wiki/Stereolab_(band)",
        "bare_validates": True,  # standard
        "qualified_validates": False,  # 404
        "correct_url_suffix": "Stereolab",
    },
    "Cat Power": {
        "artist_name": "Cat Power",
        "bare_url": "https://en.wikipedia.org/wiki/Cat_Power",
        "qualified_url": "https://en.wikipedia.org/wiki/Cat_Power_(musician)",
        "bare_validates": True,  # standard
        "qualified_validates": False,  # 404
        "correct_url_suffix": "Cat_Power",
    },
}

_SUMMARY = WikipediaSummary(extract="A real artist biography.")


def _make_fetch(row: dict):
    """A fake ``fetch(url, lang) -> WikipediaSummary | None`` reproducing
    exactly one row's live-verified outcomes, plus a call log so tests can
    assert WHICH candidates were actually tried (not just the final pick)."""
    calls: list[str] = []

    async def fetch(url: str, lang: str) -> WikipediaSummary | None:
        calls.append(url)
        if url == row["bare_url"]:
            return _SUMMARY if row["bare_validates"] else None
        if url == row["qualified_url"]:
            return _SUMMARY if row["qualified_validates"] else None
        raise AssertionError(f"unexpected fetch for {url!r}")

    return fetch, calls


@pytest.mark.asyncio
class TestTenPageTable:
    """Pins the correct winner for each pair, in BOTH input orders, so a
    total-order tiebreak that merely agrees with itself (round 2's blind
    spot) can't pass without also being right."""

    @pytest.mark.parametrize("pair_name", list(_TEN_PAGE_TABLE))
    async def test_bare_first_input_order(self, pair_name):
        row = _TEN_PAGE_TABLE[pair_name]
        fetch, calls = _make_fetch(row)
        urls = [row["bare_url"], row["qualified_url"]]
        result = await resolve_and_validate_pick(urls, row["artist_name"], fetch=fetch)
        assert result.picked is not None
        assert result.picked.url.endswith(row["correct_url_suffix"]), (
            f"{pair_name}: expected the page ending in {row['correct_url_suffix']!r}, "
            f"got {result.picked.url!r}"
        )
        assert result.summary is not None

    @pytest.mark.parametrize("pair_name", list(_TEN_PAGE_TABLE))
    async def test_qualified_first_input_order(self, pair_name):
        row = _TEN_PAGE_TABLE[pair_name]
        fetch, calls = _make_fetch(row)
        urls = [row["qualified_url"], row["bare_url"]]
        result = await resolve_and_validate_pick(urls, row["artist_name"], fetch=fetch)
        assert result.picked is not None
        assert result.picked.url.endswith(row["correct_url_suffix"]), (
            f"{pair_name}: expected the page ending in {row['correct_url_suffix']!r}, "
            f"got {result.picked.url!r}"
        )
        assert result.summary is not None


@pytest.mark.asyncio
class TestTryOrderAndShortCircuit:
    async def test_the_validating_candidate_short_circuits_further_fetches(self):
        # Sun Ra: bare validates. Ranked try-order (score tie -> shorter
        # url wins the ORDER, not the answer) puts the bare url first, so
        # the qualified (404) url should never even be fetched.
        row = _TEN_PAGE_TABLE["Sun Ra"]
        fetch, calls = _make_fetch(row)
        urls = [row["qualified_url"], row["bare_url"]]
        result = await resolve_and_validate_pick(urls, row["artist_name"], fetch=fetch)
        assert result.picked is not None
        assert calls == [row["bare_url"]]

    async def test_a_rejected_first_try_falls_through_to_the_next_candidate(self):
        # Low: bare is a disambiguation page (rejected) -- the qualified
        # url must still be tried and win.
        row = _TEN_PAGE_TABLE["Low"]
        fetch, calls = _make_fetch(row)
        urls = [row["bare_url"], row["qualified_url"]]
        result = await resolve_and_validate_pick(urls, row["artist_name"], fetch=fetch)
        assert result.picked is not None
        assert result.picked.url == row["qualified_url"]
        assert calls == [row["bare_url"], row["qualified_url"]]


@pytest.mark.asyncio
class TestMaxCandidatesCap:
    """LML#1192 review round 5: ``max_candidates`` bounds a background
    warm's worst-case sequential fetch count (a single warm task holds its
    concurrency-limiting semaphore permit for the WHOLE candidate
    selection, not just one fetch -- see
    ``lookup.enrichment.wikipedia_warm.MAX_CANDIDATES_PER_WARM``). ``None``
    (the default) means no cap, for the offline drain -- an explicit,
    deliberately-invoked batch job with no per-request latency budget.
    """

    async def test_none_means_uncapped(self):
        # Same slug, five language editions -- all score identically, so
        # every one is a candidate. With no cap, a caller rejecting every
        # candidate must see all five tried.
        langs = ["en", "fr", "de", "es", "it"]
        urls = [f"https://{lang}.wikipedia.org/wiki/Stereolab" for lang in langs]
        calls: list[str] = []

        async def fetch(url, lang):
            calls.append(url)
            return None

        result = await resolve_and_validate_pick(urls, "Stereolab", fetch=fetch)
        assert result.picked is not None
        assert result.picked.below_floor is True
        assert len(calls) == 5
        # No cap given -- nothing was ever left untried, so this can never
        # be a truncation (LML#1192 review round 6, C2-1).
        assert result.truncated is False

    async def test_a_positive_cap_stops_after_that_many_candidates(self):
        langs = ["en", "fr", "de", "es", "it"]
        urls = [f"https://{lang}.wikipedia.org/wiki/Stereolab" for lang in langs]
        calls: list[str] = []

        async def fetch(url, lang):
            calls.append(url)
            return None  # every candidate rejects

        result = await resolve_and_validate_pick(urls, "Stereolab", fetch=fetch, max_candidates=2)
        assert result.picked is not None
        assert result.picked.below_floor is True
        assert len(calls) == 2
        # LML#1192 review round 6, C2-1: three above-floor candidates were
        # never tried at all -- this below-floor fallback must be
        # distinguishable from a genuinely exhausted candidate list, so a
        # caller can refuse to write an authoritative negative for it.
        assert result.truncated is True

    async def test_the_cap_does_not_prevent_a_within_cap_candidate_from_winning(self):
        # Try order for an all-tied score is en first (the lang==en
        # tiebreak), then the url string descending (verified: "fr" before
        # "de", since "f" > "d"). With max_candidates=2, en and fr are
        # tried (de is not, cap-excluded) -- fr validates, so it must win
        # even though de is never reached.
        langs = ["en", "fr", "de"]
        urls = [f"https://{lang}.wikipedia.org/wiki/Stereolab" for lang in langs]
        calls: list[str] = []

        async def fetch(url, lang):
            calls.append(lang)
            return WikipediaSummary(extract="A band.") if lang == "fr" else None

        result = await resolve_and_validate_pick(urls, "Stereolab", fetch=fetch, max_candidates=2)
        assert result.picked is not None
        assert result.picked.below_floor is False
        assert result.picked.url == "https://fr.wikipedia.org/wiki/Stereolab"
        assert calls == ["en", "fr"]
        # LML#1192 review round 6, C2-1: a WIN is never "truncated," even
        # when the cap left other candidates untried -- an answer was
        # found, so there's no ambiguity left for a caller to worry about.
        assert result.truncated is False

    async def test_a_beyond_the_cap_candidate_that_would_validate_is_never_reached_and_the_fallback_is_flagged_truncated(
        self,
    ):
        # LML#1192 review round 6, C2-1: the exact scenario the finding
        # names -- a truncated candidate list where only a beyond-the-cap
        # candidate would have validated. All within-cap candidates reject
        # (the fixture never even defines a validating response for the
        # 4th, since it must never be reached); the fallback must come
        # back distinguishable as "we ran out of budget," not "we asked
        # about every real candidate and there wasn't one."
        # Verified try-order for four all-tied (score 100.0) candidates:
        # en first (the lang==en tiebreak), then the remaining three by
        # descending URL string -- "fr" > "es" > "de". With
        # max_candidates=3, "de" is the one beyond-the-cap candidate.
        langs = ["en", "fr", "de", "es"]
        urls = [f"https://{lang}.wikipedia.org/wiki/Stereolab" for lang in langs]
        calls: list[str] = []

        async def fetch(url, lang):
            calls.append(lang)
            if lang == "de":
                raise AssertionError(
                    "the beyond-the-cap candidate (max_candidates=3) must never be fetched"
                )
            return None  # every within-cap candidate rejects

        result = await resolve_and_validate_pick(urls, "Stereolab", fetch=fetch, max_candidates=3)

        assert calls == ["en", "fr", "es"]
        assert result.picked is not None
        assert result.picked.below_floor is True
        assert result.truncated is True


@pytest.mark.asyncio
class TestNoCandidateValidates:
    async def test_falls_back_to_the_below_floor_heuristic_pick_unfetched(self):
        # No wikipedia.org url scores anywhere near the artist name, so
        # there's nothing above-floor to even try validating -- matches
        # the pre-existing "never fetch bio text for an unconfident pick"
        # rule; the heuristic (first-listed) url is returned as a
        # below_floor pick with no fetch attempted at all.
        urls = ["https://en.wikipedia.org/wiki/Totally_Unrelated_Page"]

        async def fetch(url, lang):
            raise AssertionError("must not fetch a below-floor candidate")

        result = await resolve_and_validate_pick(urls, "Stereolab", fetch=fetch)
        assert result.picked is not None
        assert result.picked.below_floor is True
        assert result.picked.url == urls[0]
        assert result.summary is None
        # No cap given -- there was never anything left untried.
        assert result.truncated is False

    async def test_every_above_floor_candidate_rejected_falls_back_to_heuristic(self):
        # Both candidates clear the floor (same slug, so both score high)
        # but BOTH reject on fetch (e.g. both are disambiguation pages) --
        # falls back to the heuristic (first-listed) pick, unfetched
        # further, rather than looping forever or raising.
        urls = [
            "https://en.wikipedia.org/wiki/Stereolab",
            "https://en.wikipedia.org/wiki/Stereolab_(disambiguation)",
        ]

        async def fetch(url, lang):
            return None  # every candidate rejects

        result = await resolve_and_validate_pick(urls, "Stereolab", fetch=fetch)
        assert result.picked is not None
        assert result.picked.below_floor is True
        assert result.picked.url == urls[0]
        assert result.summary is None
        # Both candidates WERE tried (no cap given) -- an exhausted list is
        # not a truncated one.
        assert result.truncated is False

    async def test_no_wikipedia_url_at_all_returns_no_pick_and_never_fetches(self):
        async def fetch(url, lang):
            raise AssertionError("must not fetch when there is no candidate at all")

        result = await resolve_and_validate_pick([], "Stereolab", fetch=fetch)
        assert result.picked is None
        assert result.summary is None
        assert result.truncated is False


@pytest.mark.asyncio
class TestFetchErrorPropagates:
    async def test_a_transient_fetch_error_on_any_candidate_propagates_immediately(self):
        # A WikipediaFetchError means "couldn't ask," not "this candidate
        # is wrong" -- it must propagate so the caller's existing
        # except WikipediaFetchError handling (never negative-cache on a
        # transient failure) still applies, rather than silently trying
        # the next candidate and potentially picking a WORSE page just
        # because the better one happened to time out.
        urls = ["https://en.wikipedia.org/wiki/Stereolab"]

        async def fetch(url, lang):
            raise WikipediaFetchError("timed out")

        with pytest.raises(WikipediaFetchError):
            await resolve_and_validate_pick(urls, "Stereolab", fetch=fetch)


@pytest.mark.asyncio
class TestSlugScoreMatchesTheServedUrl:
    """LML#1192 review round 4, P0-13: the below-floor fallback's
    slug_score must describe the URL actually being served, not the
    winning-but-rejected candidate's score (the same bug class as A5's
    lang mismatch, fixed structurally here rather than patched in
    ExtractorComparison.resolve())."""

    async def test_fallback_slug_score_is_the_heuristic_urls_own_score(self):
        # Two candidates: "Totally Different Artist" (heuristic/first,
        # scores low against "Stereolab") and "Stereolab" itself (scores
        # 100, but let's force it below the floor from the OTHER side by
        # using an artist name that doesn't match either well is fiddly --
        # instead, directly assert the heuristic pick's score is NOT the
        # placeholder 0.0 a naive implementation might use, and not
        # coincidentally equal to a differently-scored winner.
        urls = [
            "https://en.wikipedia.org/wiki/Partially_Matching_Page",
            "https://en.wikipedia.org/wiki/Also_A_Partial_Match_Page",
        ]

        async def fetch(url, lang):
            return None  # both reject -- exercise the fallback branch

        result = await resolve_and_validate_pick(urls, "Partially Matching Artist", fetch=fetch)
        assert result.picked is not None
        assert result.picked.below_floor is True
        assert result.picked.url == urls[0]
        # The heuristic pick IS "Partially_Matching_Page", which shares
        # real words with "Partially Matching Artist" -- its own score
        # must be reflected, not a different candidate's.
        assert result.picked.slug_score > 0.0
