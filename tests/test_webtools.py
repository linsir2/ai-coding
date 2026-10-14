"""TDD (r17/T3): webfetch guards + Tavily search shaping.

No network in these tests: both tools take an injected ``opener``, the same seam
production passes a ``urllib``-based fetch through.  The SSRF guard and the
"content is untrusted" header are the two things that must not be left to
convention.
"""

from __future__ import annotations

import json

import pytest

from ai_coding.tools.webfetch_tool import WebfetchTool
from ai_coding.tools.websearch_tool import WebsearchTool


def _resp(content_type: str, body: bytes) -> tuple[str, bytes]:
    return content_type, body


@pytest.mark.asyncio
async def test_webfetch_rejects_private_hosts_and_non_http_schemes():
    tool = WebfetchTool()

    urls = (
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "http://localhost:8080/",
        "file:///etc/passwd",
    )
    for url in urls:
        result = await tool.execute({"url": url})
        assert result.success is False, url


@pytest.mark.asyncio
async def test_webfetch_converts_html_and_carries_the_untrusted_header():
    def opener(url, timeout):
        return _resp("text/html; charset=utf-8", b"<html><body><p>hello</p></body></html>")

    tool = WebfetchTool(opener=opener)

    result = await tool.execute({"url": "https://example.com/doc"})

    assert result.success
    assert result.output.startswith("Fetched https://example.com/doc")
    assert "不可信" in result.output
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_webfetch_passes_plain_text_through():
    tool = WebfetchTool(opener=lambda url, timeout: _resp("text/plain", b"raw body"))

    result = await tool.execute({"url": "https://example.com/robots.txt"})

    assert "raw body" in result.output


@pytest.mark.asyncio
async def test_websearch_renders_structured_results():
    def opener(url, timeout, data):
        assert b"api_key" in data and b"query" in data  # Tavily's shape
        return _resp(
            "application/json",
            json.dumps(
                {
                    "results": [
                        {"title": "T", "url": "https://e.com", "content": "snippet"},
                        {"title": "U", "url": "https://f.com", "content": "more"},
                    ]
                }
            ).encode(),
        )

    tool = WebsearchTool(api_key="k", opener=opener)

    result = await tool.execute({"query": "asyncio"})

    assert result.success
    assert "[T](https://e.com)" in result.output
    assert "snippet" in result.output


@pytest.mark.asyncio
async def test_websearch_without_a_key_says_how_to_configure():
    result = await WebsearchTool(api_key="").execute({"query": "q"})

    assert result.success is False
    assert "websearch_api_key" in result.output


@pytest.mark.asyncio
async def test_websearch_surfaces_a_provider_error():
    tool = WebsearchTool(
        api_key="k",
        opener=lambda url, timeout, data: _resp("application/json", b'{"error":"bad key"}'),
    )

    result = await tool.execute({"query": "q"})

    assert result.success is False
    assert "bad key" in result.output
