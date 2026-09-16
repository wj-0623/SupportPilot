from __future__ import annotations

import asyncio
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree  # type: ignore[import-untyped]

from app.connectors.generic_rest import validate_connector_url

MAX_DOCUMENT_BYTES = 1_000_000
MAX_SITEMAP_URLS = 20


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        del attrs
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


def _validate_url(url: str, allowed_hosts: list[str]) -> None:
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("knowledge URL has no hostname")
    validate_connector_url(f"{parsed.scheme}://{parsed.netloc}", allowed_hosts)


async def _fetch(client: httpx.AsyncClient, url: str, allowed_hosts: list[str]) -> str:
    _validate_url(url, allowed_hosts)
    response = await client.get(url)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not any(item in content_type for item in ("text/", "application/json", "xml")):
        raise ValueError("knowledge source must be text, JSON or XML")
    raw = response.content
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("knowledge document exceeds one megabyte")
    text = response.text
    if "html" in content_type:
        parser = _TextExtractor()
        parser.feed(text)
        return parser.text()
    return text.strip()


async def sync_knowledge(location: str, source_type: str, allowed_hosts: list[str]) -> str:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10),
        follow_redirects=False,
        headers={"User-Agent": "SupportPilot-Knowledge-Sync/3.0"},
    ) as client:
        if source_type != "sitemap":
            return await _fetch(client, location, allowed_hosts)
        xml = await _fetch(client, location, allowed_hosts)
        if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
            raise ValueError("sitemap declarations and entities are forbidden")
        root = ElementTree.fromstring(xml)
        urls = [
            element.text.strip()
            for element in root.iter()
            if element.tag.endswith("loc") and element.text
        ][:MAX_SITEMAP_URLS]
        if not urls:
            raise ValueError("sitemap contains no URLs")
        documents = await asyncio.gather(*(_fetch(client, url, allowed_hosts) for url in urls))
        return "\n\n".join(
            f"Source: {url}\n{content}" for url, content in zip(urls, documents, strict=True)
        )
