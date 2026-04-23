"""Discogs markup parser.

Parses Discogs-style markup (links, formatting) to structured tokens.
Faithful Python translation of iOS DiscogsMarkupParser.swift.

Three-phase architecture:
1. Tokenize — parse markup into a flat token list
2. Resolve — resolve ID-based tokens (sync mode skips them)
3. Output — return structured ResolvedToken models for API serialization

Supported formats:
- ``[a=Artist Name]`` — artist link (displays the artist name)
- ``[a12345]`` — artist link by ID (resolved via EntityResolver)
- ``[l=Label Name]`` — label name (plain text)
- ``[b]text[/b]`` — bold text
- ``[i]text[/i]`` — italic text
- ``[u]text[/u]`` — underlined text
- ``[url=http://example.com]Link Text[/url]`` — URL link
- ``[r12345]`` or ``[r=12345]`` — release link by ID
- ``[m123]`` or ``[m=123]`` — master link by ID
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlparse

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

logger = logging.getLogger(__name__)

# MARK: - Regex Patterns

ARTIST_NAME_PATTERN = re.compile(r"^a=(.+)$")
ARTIST_ID_PATTERN = re.compile(r"^a(\d+)$")
RELEASE_ID_PATTERN = re.compile(r"^r=?(\d+)$")
MASTER_ID_PATTERN = re.compile(r"^m=?(\d+)$")
LABEL_NAME_PATTERN = re.compile(r"^l=(.+)$")
URL_OPEN_PATTERN = re.compile(r"^url=(.+)$")
CLOSING_TAG_PATTERN = re.compile(r"^/(.+)$")
DISAMBIGUATION_SUFFIX = re.compile(r" \(\d+\)$")

# MARK: - Raw Token Types (internal)


@dataclass(frozen=True)
class _PlainText:
    text: str


@dataclass(frozen=True)
class _ArtistName:
    name: str


@dataclass(frozen=True)
class _ArtistId:
    id: int


@dataclass(frozen=True)
class _ReleaseId:
    id: int


@dataclass(frozen=True)
class _MasterId:
    id: int


@dataclass(frozen=True)
class _LabelName:
    name: str


@dataclass(frozen=True)
class _Bold:
    content: str


@dataclass(frozen=True)
class _Italic:
    content: str


@dataclass(frozen=True)
class _Underline:
    content: str


@dataclass(frozen=True)
class _Url:
    href: str
    content: str


_DiscogsToken = (
    _PlainText
    | _ArtistName
    | _ArtistId
    | _ReleaseId
    | _MasterId
    | _LabelName
    | _Bold
    | _Italic
    | _Underline
    | _Url
)

# MARK: - Utility


def strip_disambiguation_suffix(name: str) -> str:
    """Remove Discogs disambiguation suffix like " (8)" from artist names.

    e.g., "Salamanda (8)" becomes "Salamanda"
    """
    return DISAMBIGUATION_SUFFIX.sub("", name)


# MARK: - Phase 1: Tokenize


def _find_closing_tag(text: str, start_index: int, tag: str) -> tuple[str, int] | None:
    """Find a closing tag like [/tag] in the text, handling same-type nesting.

    Returns (content_between_tags, index_after_closing_bracket) or None.
    """
    search_start = start_index
    depth = 1

    while search_start < len(text):
        open_bracket = text.find("[", search_start)
        if open_bracket == -1:
            break

        close_bracket = text.find("]", open_bracket)
        if close_bracket == -1:
            break

        tag_content = text[open_bracket + 1 : close_bracket]

        if tag_content == tag:
            depth += 1
        elif tag_content == f"/{tag}":
            depth -= 1
            if depth == 0:
                content = text[start_index:open_bracket]
                return (content, close_bracket + 1)

        search_start = close_bracket + 1

    return None


def _classify_tag(tag: str, text: str, pos_after_tag: int) -> tuple[_DiscogsToken, int] | None:
    """Classify a tag and return the appropriate token with updated position."""
    # Artist name: [a=Name]
    match = ARTIST_NAME_PATTERN.match(tag)
    if match:
        return (_ArtistName(name=match.group(1)), pos_after_tag)

    # Artist ID: [a12345]
    match = ARTIST_ID_PATTERN.match(tag)
    if match:
        return (_ArtistId(id=int(match.group(1))), pos_after_tag)

    # Release ID: [r12345] or [r=12345]
    match = RELEASE_ID_PATTERN.match(tag)
    if match:
        return (_ReleaseId(id=int(match.group(1))), pos_after_tag)

    # Master ID: [m123] or [m=123]
    match = MASTER_ID_PATTERN.match(tag)
    if match:
        return (_MasterId(id=int(match.group(1))), pos_after_tag)

    # Label name: [l=Name]
    match = LABEL_NAME_PATTERN.match(tag)
    if match:
        return (_LabelName(name=match.group(1)), pos_after_tag)

    # URL: [url=...]...[/url]
    match = URL_OPEN_PATTERN.match(tag)
    if match:
        url_string = match.group(1)
        closing = _find_closing_tag(text, pos_after_tag, "url")
        if closing:
            content, end_index = closing
            return (_Url(href=url_string, content=content), end_index)
        else:
            # No closing tag — show URL and remaining text combined
            remaining = text[pos_after_tag:]
            return (_PlainText(text=url_string + remaining), len(text))

    # Formatting tags: [b]...[/b], [i]...[/i], [u]...[/u]
    if tag in ("b", "i", "u"):
        closing = _find_closing_tag(text, pos_after_tag, tag)
        if closing:
            content, end_index = closing
            if tag == "b":
                return (_Bold(content=content), end_index)
            elif tag == "i":
                return (_Italic(content=content), end_index)
            else:
                return (_Underline(content=content), end_index)
        else:
            # No closing tag — skip
            return None

    # Orphaned closing tags — skip
    if CLOSING_TAG_PATTERN.match(tag):
        return None

    # Unknown tag — skip
    return None


def tokenize(text: str) -> list[_DiscogsToken]:
    """Tokenize Discogs markup into a flat list of raw tokens."""
    tokens: list[_DiscogsToken] = []
    pos = 0

    while pos < len(text):
        bracket_index = text.find("[", pos)
        if bracket_index == -1:
            tokens.append(_PlainText(text=text[pos:]))
            break

        # Add plain text before the bracket
        if bracket_index > pos:
            tokens.append(_PlainText(text=text[pos:bracket_index]))

        # Find closing bracket
        closing_bracket = text.find("]", bracket_index)
        if closing_bracket == -1:
            # No closing bracket — append from opening bracket onward as plain text
            tokens.append(_PlainText(text=text[bracket_index:]))
            break

        tag_content = text[bracket_index + 1 : closing_bracket]
        pos = closing_bracket + 1

        if len(tag_content) == 0:
            continue

        # Classify and handle the tag
        result = _classify_tag(tag_content, text, pos)
        if result:
            token, pos = result
            tokens.append(token)
        # Unknown/orphaned/empty tags are silently skipped

    return tokens


# MARK: - Phase 2: Resolve


def _resolve_token(token: _DiscogsToken, resolved_ids: dict[str, str]) -> ResolvedToken | None:
    """Resolve a single raw token to a ResolvedToken using the resolved ID map."""
    match token:
        case _PlainText(text=text):
            return PlainTextToken(text=text)

        case _ArtistName(name=name):
            display_name = strip_disambiguation_suffix(name)
            encoded = quote(name, safe="")
            url = f"https://www.discogs.com/search/?q={encoded}&type=artist"
            return ArtistLinkToken(name=name, display_name=display_name, url=url)

        case _ArtistId(id=id):
            resolved_name = resolved_ids.get(f"artist-{id}")
            if resolved_name is None:
                return None
            display_name = strip_disambiguation_suffix(resolved_name)
            url = f"https://www.discogs.com/artist/{id}"
            return ArtistLinkToken(name=resolved_name, display_name=display_name, url=url)

        case _ReleaseId(id=id):
            title = resolved_ids.get(f"release-{id}")
            if title is None:
                return None
            url = f"https://www.discogs.com/release/{id}"
            return ReleaseLinkToken(title=title, url=url)

        case _MasterId(id=id):
            title = resolved_ids.get(f"master-{id}")
            if title is None:
                return None
            url = f"https://www.discogs.com/master/{id}"
            return MasterLinkToken(title=title, url=url)

        case _LabelName(name=name):
            return LabelNameToken(name=name)

        case _Bold(content=content):
            return BoldToken(content=content)

        case _Italic(content=content):
            return ItalicToken(content=content)

        case _Underline(content=content):
            return UnderlineToken(content=content)

        case _Url(href=href, content=content):
            parsed = urlparse(href)
            valid_href = href if parsed.scheme else None
            return UrlLinkToken(href=valid_href, content=content)


def resolve(
    tokens: list[_DiscogsToken],
    resolved_ids: dict[str, str] | None = None,
) -> list[ResolvedToken]:
    """Resolve raw tokens to ResolvedTokens (sync, skips unresolved ID tokens)."""
    ids = resolved_ids or {}
    return [r for t in tokens if (r := _resolve_token(t, ids)) is not None]


def parse(text: str) -> list[ResolvedToken]:
    """Parse Discogs markup and return resolved tokens (sync, no entity resolution).

    ID-based tokens ([a12345], [r12345], [m123]) are silently dropped.
    """
    return resolve(tokenize(text))


# MARK: - Async Entity Resolution


class EntityResolver(Protocol):
    """Protocol for resolving Discogs entity IDs to names."""

    async def resolve_artist(self, id: int) -> str | None: ...
    async def resolve_release(self, id: int) -> str | None: ...
    async def resolve_master(self, id: int) -> str | None: ...


async def _safe_resolve(coro, key: str) -> tuple[str, str | None]:
    """Wrap a resolver coroutine, catching all exceptions."""
    try:
        result = await coro
        return (key, result)
    except Exception:
        logger.warning("Failed to resolve entity %s", key)
        return (key, None)


async def resolve_async(
    tokens: list[_DiscogsToken],
    resolver: EntityResolver,
) -> list[ResolvedToken]:
    """Resolve raw tokens with async entity resolution.

    Collects unique entity IDs, resolves them concurrently, then rebuilds tokens.
    """
    # Collect unique IDs
    artist_ids: set[int] = set()
    release_ids: set[int] = set()
    master_ids: set[int] = set()

    for token in tokens:
        match token:
            case _ArtistId(id=id):
                artist_ids.add(id)
            case _ReleaseId(id=id):
                release_ids.add(id)
            case _MasterId(id=id):
                master_ids.add(id)

    # Resolve all IDs concurrently
    tasks = []
    for id in artist_ids:
        tasks.append(_safe_resolve(resolver.resolve_artist(id), f"artist-{id}"))
    for id in release_ids:
        tasks.append(_safe_resolve(resolver.resolve_release(id), f"release-{id}"))
    for id in master_ids:
        tasks.append(_safe_resolve(resolver.resolve_master(id), f"master-{id}"))

    results = await asyncio.gather(*tasks)

    # Build resolved IDs map (skip None values)
    resolved_ids: dict[str, str] = {key: value for key, value in results if value is not None}

    # Rebuild with resolved values
    return resolve(tokens, resolved_ids)


async def parse_async(
    text: str,
    resolver: EntityResolver,
) -> list[ResolvedToken]:
    """Parse Discogs markup with async entity resolution.

    Resolves [a12345], [r12345], [m123] tags via the provided resolver.
    """
    return await resolve_async(tokenize(text), resolver)


# MARK: - DiscogsService Adapter


class DiscogsServiceResolver:
    """Adapts DiscogsService methods to the EntityResolver protocol."""

    def __init__(self, service) -> None:
        self._service = service

    async def resolve_artist(self, id: int) -> str | None:
        try:
            details = await self._service.get_artist_details(id)
            return details.name if details else None
        except Exception:
            return None

    async def resolve_release(self, id: int) -> str | None:
        try:
            release = await self._service.get_release(id)
            return release.title if release else None
        except Exception:
            return None

    async def resolve_master(self, id: int) -> str | None:
        try:
            master = await self._service.get_master(id)
            return master.title if master else None
        except Exception:
            return None
