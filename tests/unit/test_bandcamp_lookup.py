"""Unit tests for scripts/bandcamp_lookup.py."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from scripts.bandcamp_lookup import fetch_bandcamp_albums


@pytest.mark.asyncio
async def test_extracts_albums_with_titles():
    html = (
        '<a href="/album/confield"><p class="title">Confield</p></a>'
        '<a href="/album/draft-7-30"><p class="title">Draft 7.30</p></a>'
    )
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            text=html,
            request=httpx.Request("GET", "https://autechre.bandcamp.com/music"),
        )
    )

    albums = await fetch_bandcamp_albums(client, "autechre", asyncio.Semaphore(1))

    assert len(albums) == 2
    assert albums[0]["title"] == "Confield"
    assert albums[0]["url"] == "https://autechre.bandcamp.com/album/confield"


@pytest.mark.asyncio
async def test_returns_empty_on_404():
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=httpx.Response(
            404,
            request=httpx.Request("GET", "https://nope.bandcamp.com/music"),
        )
    )

    albums = await fetch_bandcamp_albums(client, "nope", asyncio.Semaphore(1))

    assert albums == []


@pytest.mark.asyncio
async def test_forces_utf8_when_no_charset_in_content_type():
    # httpx defaults to ISO-8859-1 when Content-Type omits charset=; force UTF-8.
    title = "Csillagrablók"
    body = b'<a href="/album/csillagrablok"><p class="title">' + title.encode("utf-8") + b"</p></a>"
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            content=body,
            default_encoding="iso-8859-1",
            request=httpx.Request("GET", "https://csillagrablok.bandcamp.com/music"),
        )
    )

    albums = await fetch_bandcamp_albums(client, "csillagrablok", asyncio.Semaphore(1))

    assert len(albums) == 1
    assert albums[0]["title"] == title
