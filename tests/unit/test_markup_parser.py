"""Tests for Discogs markup parser.

Faithful translation of iOS DiscogsMarkupParserTests.swift.
Covers: artist links, bold/italic/underline formatting, label links,
URL links, release/master links, edge cases, async entity resolution.
"""

import pytest

from discogs.markup_parser import (
    _ArtistId,
    _ArtistName,
    _PlainText,
    parse,
    parse_async,
    resolve,
    strip_disambiguation_suffix,
    tokenize,
)
from discogs.models import (
    ArtistLinkToken,
    BoldToken,
    ItalicToken,
    LabelNameToken,
    MasterLinkToken,
    PlainTextToken,
    ReleaseLinkToken,
    ResolvedToken,
    UnderlineToken,
    UrlLinkToken,
)

# MARK: - Test Helpers


class MockEntityResolver:
    """Mock resolver for testing async entity resolution."""

    def __init__(
        self,
        artists: dict[int, str] | None = None,
        releases: dict[int, str] | None = None,
        masters: dict[int, str] | None = None,
        should_throw: bool = False,
    ):
        self.artists = artists or {}
        self.releases = releases or {}
        self.masters = masters or {}
        self.should_throw = should_throw

    async def resolve_artist(self, id: int) -> str | None:
        if self.should_throw:
            raise RuntimeError("resolution failed")
        return self.artists.get(id)

    async def resolve_release(self, id: int) -> str | None:
        if self.should_throw:
            raise RuntimeError("resolution failed")
        return self.releases.get(id)

    async def resolve_master(self, id: int) -> str | None:
        if self.should_throw:
            raise RuntimeError("resolution failed")
        return self.masters.get(id)


def text_content(tokens: list[ResolvedToken]) -> str:
    """Extract display text from resolved tokens."""
    parts: list[str] = []
    for t in tokens:
        match t:
            case PlainTextToken():
                parts.append(t.text)
            case ArtistLinkToken():
                parts.append(t.display_name)
            case LabelNameToken():
                parts.append(t.name)
            case ReleaseLinkToken():
                parts.append(t.title)
            case MasterLinkToken():
                parts.append(t.title)
            case BoldToken():
                parts.append(t.content)
            case ItalicToken():
                parts.append(t.content)
            case UnderlineToken():
                parts.append(t.content)
            case UrlLinkToken():
                parts.append(t.content)
    return "".join(parts)


# MARK: - Artist Tag Tests


class TestArtistTags:
    def test_parses_artist_link_with_name(self):
        result = parse("[a=The Beatles]")
        assert text_content(result) == "The Beatles"

    def test_parses_artist_link_with_special_characters(self):
        result = parse("[a=Guns N' Roses]")
        assert text_content(result) == "Guns N' Roses"

    def test_skips_artist_link_by_id_in_sync_mode(self):
        result = parse("[a12345]")
        assert text_content(result) == ""

    def test_skips_artist_link_with_large_id(self):
        result = parse("[a9999999]")
        assert text_content(result) == ""

    def test_preserves_text_around_artist_id_link(self):
        result = parse("See [a12345] for more")
        assert text_content(result) == "See  for more"

    def test_artist_link_has_correct_url(self):
        result = parse("[a=Test Artist]")
        artist_token = next(t for t in result if isinstance(t, ArtistLinkToken))
        assert "discogs.com/search" in artist_token.url
        assert "type=artist" in artist_token.url


# MARK: - Bold Tag Tests


class TestBoldTags:
    def test_parses_bold_text(self):
        result = parse("[b]bold text[/b]")
        assert text_content(result) == "bold text"
        assert any(isinstance(t, BoldToken) and t.content == "bold text" for t in result)

    def test_parses_bold_with_surrounding_content(self):
        result = parse("This is [b]bold[/b] text")
        assert text_content(result) == "This is bold text"

    def test_skips_orphaned_bold_closing_tag(self):
        result = parse("text [/b] more")
        assert text_content(result) == "text  more"

    def test_handles_unclosed_bold_tag(self):
        result = parse("[b]no closing tag")
        assert text_content(result) == "no closing tag"

    def test_handles_empty_bold_content(self):
        result = parse("[b][/b]")
        assert text_content(result) == ""


# MARK: - Italic Tag Tests


class TestItalicTags:
    def test_parses_italic_text(self):
        result = parse("[i]italic text[/i]")
        assert text_content(result) == "italic text"
        assert any(isinstance(t, ItalicToken) and t.content == "italic text" for t in result)

    def test_parses_italic_with_surrounding_content(self):
        result = parse("This is [i]italic[/i] text")
        assert text_content(result) == "This is italic text"

    def test_skips_orphaned_italic_closing_tag(self):
        result = parse("text [/i] more")
        assert text_content(result) == "text  more"

    def test_handles_unclosed_italic_tag(self):
        result = parse("[i]no closing tag")
        assert text_content(result) == "no closing tag"


# MARK: - Underline Tag Tests


class TestUnderlineTags:
    def test_parses_underlined_text(self):
        result = parse("[u]underlined text[/u]")
        assert text_content(result) == "underlined text"
        assert any(isinstance(t, UnderlineToken) and t.content == "underlined text" for t in result)

    def test_parses_underline_with_surrounding_content(self):
        result = parse("This is [u]underlined[/u] text")
        assert text_content(result) == "This is underlined text"

    def test_skips_orphaned_underline_closing_tag(self):
        result = parse("text [/u] more")
        assert text_content(result) == "text  more"

    def test_handles_unclosed_underline_tag(self):
        result = parse("[u]no closing tag")
        assert text_content(result) == "no closing tag"


# MARK: - Label Tag Tests


class TestLabelTags:
    def test_parses_label_link(self):
        result = parse("[l=Blue Note Records]")
        assert text_content(result) == "Blue Note Records"

    def test_parses_label_link_with_special_characters(self):
        result = parse("[l=4AD]")
        assert text_content(result) == "4AD"

    def test_parses_label_link_with_surrounding_text(self):
        result = parse("Released on [l=Motown] in 1965")
        assert text_content(result) == "Released on Motown in 1965"


# MARK: - URL Tag Tests


class TestUrlTags:
    def test_parses_url_link(self):
        result = parse("[url=https://example.com]Click here[/url]")
        assert text_content(result) == "Click here"
        url_token = next(t for t in result if isinstance(t, UrlLinkToken))
        assert url_token.href == "https://example.com"

    def test_parses_url_link_has_href(self):
        result = parse("[url=https://example.com]Link[/url]")
        url_token = next(t for t in result if isinstance(t, UrlLinkToken))
        assert url_token.href is not None

    def test_handles_url_without_closing_tag(self):
        result = parse("[url=https://example.com]orphaned")
        assert text_content(result) == "https://example.comorphaned"

    def test_handles_invalid_url_gracefully(self):
        result = parse("[url=not a valid url]text[/url]")
        assert text_content(result) == "text"
        url_token = next(t for t in result if isinstance(t, UrlLinkToken))
        assert url_token.href is None

    def test_parses_url_with_complex_query_string(self):
        result = parse("[url=https://example.com/path?query=value&other=123]Link[/url]")
        assert text_content(result) == "Link"
        url_token = next(t for t in result if isinstance(t, UrlLinkToken))
        assert url_token.href == "https://example.com/path?query=value&other=123"


# MARK: - ID-based Tag Tests


class TestIdTags:
    def test_skips_release_link_by_id(self):
        assert text_content(parse("[r12345]")) == ""

    def test_skips_master_link_by_id(self):
        assert text_content(parse("[m123]")) == ""

    def test_skips_release_link_with_equals(self):
        assert text_content(parse("[r=621811]")) == ""

    def test_skips_master_link_with_equals(self):
        assert text_content(parse("[m=199386]")) == ""

    def test_preserves_text_around_release_id(self):
        result = parse("See release [r99999] for details")
        assert text_content(result) == "See release  for details"

    def test_preserves_text_around_master_id(self):
        result = parse("Master [m456] version")
        assert text_content(result) == "Master  version"


# MARK: - Edge Cases


class TestEdgeCases:
    def test_handles_plain_text(self):
        result = parse("Just plain text with no formatting")
        assert text_content(result) == "Just plain text with no formatting"

    def test_handles_empty_string(self):
        result = parse("")
        assert text_content(result) == ""

    def test_handles_multiple_consecutive_tags(self):
        result = parse("[a=Artist A][a=Artist B][a=Artist C]")
        assert text_content(result) == "Artist AArtist BArtist C"

    def test_handles_mixed_tags_and_text(self):
        result = parse("Check out [a=The Beatles] on [l=Apple Records]!")
        assert text_content(result) == "Check out The Beatles on Apple Records!"

    def test_handles_nested_formatting_tags(self):
        result = parse("[b]bold [i]and italic[/i][/b]")
        assert text_content(result) == "bold [i]and italic[/i]"

    def test_handles_unclosed_bracket(self):
        result = parse("Text with [unclosed bracket")
        assert text_content(result) == "Text with [unclosed bracket"

    def test_handles_unknown_tags(self):
        result = parse("[unknown]text[/unknown]")
        assert text_content(result) == "text"

    def test_handles_real_world_discogs_text(self):
        result = parse(
            "Written by [a=John Lennon] and [a=Paul McCartney]. "
            "Released on [l=Apple Records] in 1969. See [r123456] for more info."
        )
        assert text_content(result) == (
            "Written by John Lennon and Paul McCartney. "
            "Released on Apple Records in 1969. See  for more info."
        )

    def test_handles_text_with_only_brackets(self):
        result = parse("[]")
        assert text_content(result) == ""

    def test_handles_deeply_nested_same_type_tags(self):
        result = parse("[b]outer [b]inner[/b] outer[/b]")
        assert text_content(result) == "outer [b]inner[/b] outer"

    def test_handles_multiple_formatting_in_sequence(self):
        result = parse("[b]bold[/b] then [i]italic[/i] then [u]underline[/u]")
        assert text_content(result) == "bold then italic then underline"

    def test_does_not_confuse_url_with_u_tag(self):
        result = parse("[url=https://example.com]link[/url]")
        assert text_content(result) == "link"
        url_token = next(t for t in result if isinstance(t, UrlLinkToken))
        assert url_token.href is not None


# MARK: - Entity Resolution Tests


class TestEntityResolution:
    @pytest.mark.asyncio
    async def test_resolves_artist_id_to_name(self):
        resolver = MockEntityResolver(artists={8390436: "Salamanda (8)"})
        result = await parse_async("[a8390436]", resolver)
        assert text_content(result) == "Salamanda"
        artist = next(t for t in result if isinstance(t, ArtistLinkToken))
        assert artist.url == "https://www.discogs.com/artist/8390436"

    @pytest.mark.asyncio
    async def test_resolves_multiple_artist_ids(self):
        resolver = MockEntityResolver(artists={123: "Artist One", 456: "Artist Two"})
        result = await parse_async("Featuring [a123] and [a456]", resolver)
        assert text_content(result) == "Featuring Artist One and Artist Two"

    @pytest.mark.asyncio
    async def test_resolves_release_id_to_title(self):
        resolver = MockEntityResolver(releases={99999: "Abbey Road"})
        result = await parse_async("[r99999]", resolver)
        assert text_content(result) == "Abbey Road"
        release = next(t for t in result if isinstance(t, ReleaseLinkToken))
        assert release.url == "https://www.discogs.com/release/99999"

    @pytest.mark.asyncio
    async def test_resolves_master_id_to_title(self):
        resolver = MockEntityResolver(masters={12345: "Kind of Blue"})
        result = await parse_async("[m12345]", resolver)
        assert text_content(result) == "Kind of Blue"
        master = next(t for t in result if isinstance(t, MasterLinkToken))
        assert master.url == "https://www.discogs.com/master/12345"

    @pytest.mark.asyncio
    async def test_resolves_release_id_with_equals(self):
        resolver = MockEntityResolver(releases={621811: "The Cover Up"})
        result = await parse_async("[r=621811]", resolver)
        assert text_content(result) == "The Cover Up"
        release = next(t for t in result if isinstance(t, ReleaseLinkToken))
        assert release.url == "https://www.discogs.com/release/621811"

    @pytest.mark.asyncio
    async def test_resolves_master_id_with_equals(self):
        resolver = MockEntityResolver(masters={199386: "Out Of The Loop"})
        result = await parse_async("[m=199386]", resolver)
        assert text_content(result) == "Out Of The Loop"
        master = next(t for t in result if isinstance(t, MasterLinkToken))
        assert master.url == "https://www.discogs.com/master/199386"

    @pytest.mark.asyncio
    async def test_handles_mixed_resolved_and_named(self):
        resolver = MockEntityResolver(artists={999: "Yoko Ono"})
        result = await parse_async("[a=John Lennon] collaborated with [a999]", resolver)
        assert text_content(result) == "John Lennon collaborated with Yoko Ono"

    @pytest.mark.asyncio
    async def test_skips_unresolvable_ids(self):
        resolver = MockEntityResolver()
        result = await parse_async("See [a99999999] for more", resolver)
        assert text_content(result) == "See  for more"

    @pytest.mark.asyncio
    async def test_handles_resolver_errors(self):
        resolver = MockEntityResolver(should_throw=True)
        result = await parse_async("[a123] was great", resolver)
        assert text_content(result) == " was great"

    @pytest.mark.asyncio
    async def test_resolves_complex_real_world_text(self):
        resolver = MockEntityResolver(
            artists={5678: "George Martin"},
            releases={12345: "Sgt. Pepper's"},
        )
        result = await parse_async(
            "Written by [a=John Lennon] and [a=Paul McCartney]. "
            "Produced by [a5678]. See release [r12345] for credits.",
            resolver,
        )
        assert text_content(result) == (
            "Written by John Lennon and Paul McCartney. "
            "Produced by George Martin. See release Sgt. Pepper's for credits."
        )

    @pytest.mark.asyncio
    async def test_preserves_formatting_with_resolved_ids(self):
        resolver = MockEntityResolver(artists={100: "Test Artist"})
        result = await parse_async("[b]Bold[/b] by [a100]", resolver)
        assert text_content(result) == "Bold by Test Artist"
        assert any(isinstance(t, BoldToken) for t in result)

    @pytest.mark.asyncio
    async def test_resolves_all_entity_types(self):
        resolver = MockEntityResolver(
            artists={1: "The Artist"},
            releases={2: "The Album"},
            masters={3: "The Master"},
        )
        result = await parse_async("Artist [a1], Release [r2], Master [m3]", resolver)
        assert text_content(result) == "Artist The Artist, Release The Album, Master The Master"

    @pytest.mark.asyncio
    async def test_strips_disambiguation_suffix_from_artists(self):
        resolver = MockEntityResolver(
            artists={
                1: "Prince (2)",
                2: "Nirvana (123)",
                3: "The Beatles",
                4: "Blink-182",
                5: "Level 42",
                6: "Test (Band)",
            }
        )
        assert text_content(await parse_async("[a1]", resolver)) == "Prince"
        assert text_content(await parse_async("[a2]", resolver)) == "Nirvana"
        assert text_content(await parse_async("[a3]", resolver)) == "The Beatles"
        assert text_content(await parse_async("[a4]", resolver)) == "Blink-182"
        assert text_content(await parse_async("[a5]", resolver)) == "Level 42"
        assert text_content(await parse_async("[a6]", resolver)) == "Test (Band)"

    @pytest.mark.asyncio
    async def test_does_not_strip_suffix_from_releases_or_masters(self):
        resolver = MockEntityResolver(
            releases={1: "Album Title (2)"},
            masters={1: "Master Title (Remastered) (3)"},
        )
        result1 = await parse_async("[r1]", resolver)
        result2 = await parse_async("[m1]", resolver)
        assert text_content(result1) == "Album Title (2)"
        assert text_content(result2) == "Master Title (Remastered) (3)"


# MARK: - Utility Tests


class TestStripDisambiguationSuffix:
    def test_removes_numeric_suffix(self):
        assert strip_disambiguation_suffix("Artist (2)") == "Artist"
        assert strip_disambiguation_suffix("Artist (123)") == "Artist"

    def test_preserves_non_numeric_parentheses(self):
        assert strip_disambiguation_suffix("Artist (Band)") == "Artist (Band)"
        assert strip_disambiguation_suffix("Level 42") == "Level 42"

    def test_handles_no_suffix(self):
        assert strip_disambiguation_suffix("Artist") == "Artist"


# MARK: - Tokenizer Unit Tests


class TestTokenize:
    def test_tokenizes_plain_text(self):
        tokens = tokenize("hello world")
        assert tokens == [_PlainText(text="hello world")]

    def test_tokenizes_artist_name(self):
        tokens = tokenize("[a=Autechre]")
        assert tokens == [_ArtistName(name="Autechre")]

    def test_tokenizes_artist_id(self):
        tokens = tokenize("[a41]")
        assert tokens == [_ArtistId(id=41)]

    def test_tokenizes_mixed_content(self):
        tokens = tokenize("By [a=Rob Brown] and [a=Sean Booth]")
        assert len(tokens) == 4
        assert tokens[0] == _PlainText(text="By ")
        assert tokens[1] == _ArtistName(name="Rob Brown")
        assert tokens[2] == _PlainText(text=" and ")
        assert tokens[3] == _ArtistName(name="Sean Booth")


# MARK: - Resolve Unit Tests


class TestResolve:
    def test_resolves_artist_name_to_link(self):
        tokens = tokenize("[a=Autechre]")
        resolved = resolve(tokens)
        assert len(resolved) == 1
        assert isinstance(resolved[0], ArtistLinkToken)
        assert resolved[0].display_name == "Autechre"

    def test_strips_disambiguation_suffix(self):
        tokens = tokenize("[a=Salamanda (8)]")
        resolved = resolve(tokens)
        assert isinstance(resolved[0], ArtistLinkToken)
        assert resolved[0].display_name == "Salamanda"

    def test_drops_id_tokens_in_sync_mode(self):
        tokens = tokenize("[a41]")
        resolved = resolve(tokens)
        assert len(resolved) == 0


# MARK: - CachedOnlyResolver Tests


class TestCachedOnlyResolver:
    """Adapter that consults DiscogsCacheService and never the API.

    Used on the lookup read path so bio tokenization can't blow up tail
    latency when an artist's profile mentions many entities. Cache misses
    must surface as None (never raise) so parse_async drops the unresolved
    token. Master IDs always resolve to None — the cache has no masters
    table.
    """

    @pytest.mark.asyncio
    async def test_resolves_artist_from_cache(self):
        from unittest.mock import AsyncMock

        from discogs.markup_parser import CachedOnlyResolver
        from discogs.models import ArtistDetails

        cache = AsyncMock()
        cache.get_artist_details.return_value = ArtistDetails(artist_id=42, name="Stereolab")

        resolver = CachedOnlyResolver(cache)
        assert await resolver.resolve_artist(42) == "Stereolab"

    @pytest.mark.asyncio
    async def test_resolves_release_from_cache(self):
        from unittest.mock import AsyncMock

        from discogs.markup_parser import CachedOnlyResolver
        from discogs.models import ReleaseMetadataResponse

        cache = AsyncMock()
        cache.get_release.return_value = ReleaseMetadataResponse(
            release_id=10154369,
            title="Aluminum Tunes",
            artist="Stereolab",
            release_url="https://discogs.com/release/10154369",
        )

        resolver = CachedOnlyResolver(cache)
        assert await resolver.resolve_release(10154369) == "Aluminum Tunes"

    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self):
        from unittest.mock import AsyncMock

        from discogs.markup_parser import CachedOnlyResolver

        cache = AsyncMock()
        cache.get_artist_details.return_value = None
        cache.get_release.return_value = None

        resolver = CachedOnlyResolver(cache)
        assert await resolver.resolve_artist(999) is None
        assert await resolver.resolve_release(999) is None

    @pytest.mark.asyncio
    async def test_swallows_cache_exceptions(self):
        """Observability-grade resilience: a cache outage on the read path
        cannot raise. The bio just renders without resolved entity links.
        """
        from unittest.mock import AsyncMock

        from discogs.markup_parser import CachedOnlyResolver

        cache = AsyncMock()
        cache.get_artist_details.side_effect = RuntimeError("PG connection lost")
        cache.get_release.side_effect = RuntimeError("PG connection lost")

        resolver = CachedOnlyResolver(cache)
        assert await resolver.resolve_artist(42) is None
        assert await resolver.resolve_release(99) is None

    @pytest.mark.asyncio
    async def test_master_always_none(self):
        """The discogs-cache has no masters table; always return None."""
        from unittest.mock import AsyncMock

        from discogs.markup_parser import CachedOnlyResolver

        cache = AsyncMock()
        resolver = CachedOnlyResolver(cache)
        assert await resolver.resolve_master(12345) is None
        # And must not have consulted the cache for masters either.
        cache.get_release.assert_not_called()
        cache.get_artist_details.assert_not_called()

    @pytest.mark.asyncio
    async def test_integrates_with_parse_async(self):
        """End-to-end: parse_async with CachedOnlyResolver produces typed
        tokens for cache hits and drops cache misses."""
        from unittest.mock import AsyncMock

        from discogs.markup_parser import CachedOnlyResolver, parse_async
        from discogs.models import ArtistDetails

        cache = AsyncMock()
        cache.get_artist_details.return_value = ArtistDetails(artist_id=42, name="Stereolab")
        cache.get_release.return_value = None  # unknown

        tokens = await parse_async("Featuring [a42] from [r999999]", CachedOnlyResolver(cache))

        kinds = [type(t).__name__ for t in tokens]
        assert "ArtistLinkToken" in kinds
        assert "ReleaseLinkToken" not in kinds
