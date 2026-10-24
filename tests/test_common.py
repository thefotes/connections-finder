"""Common HTTP helpers: redirect allow-listing."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from pipeline.common import _get_no_follow

START = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def _client(location: str) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url) == START:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, text=f"landed {req.url}")
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _get(location: str) -> httpx.Response:
    async with _client(location) as c:
        return await _get_no_follow(c, START)


def test_redirect_within_allow_list_is_followed():
    r = asyncio.run(_get("/v1/boards/acme/jobs?page=2"))
    assert r.status_code == 200 and "page=2" in r.text


@pytest.mark.parametrize("location", [
    "http://169.254.169.254/latest/meta-data/",
    "https://evil.example/x",
    "http://boards-api.greenhouse.io/v1/boards/acme/jobs",  # https only
])
def test_redirect_off_allow_list_raises(location):
    with pytest.raises(ValueError):
        asyncio.run(_get(location))
