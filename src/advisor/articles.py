"""Fetch an article page and extract its text, spotting paywalls before anything goes further."""

from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass
from datetime import date

import httpx
import trafilatura

HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) advisor-watch/0.1",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}


MARKER_TEXT_FACTOR = 3


class FetchError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class Article:
    title: str | None
    text: str
    published_on: date | None


def refuse_local(request: httpx.Request) -> None:
    """httpx request hook: refuse localhost and private, loopback or link-local IP addresses.

    Names are not resolved, so a public name pointing to a private address still gets through.
    """
    host = (request.url.host or "").strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise httpx.ConnectError(f"refused local address: {host}", request=request)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if not ip.is_global:
        raise httpx.ConnectError(f"refused non-public address: {host}", request=request)


def fetch_html(client: httpx.Client, url: str, max_bytes: int) -> str:
    """GET a page as text, refusing anything that is not HTML or is larger than `max_bytes`."""
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            kind = response.headers.get("content-type", "")
            if kind and "html" not in kind:
                raise FetchError(f"not an HTML page ({kind.split(';')[0]})")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise FetchError(f"page larger than {max_bytes} bytes")
            return body.decode(response.encoding or "utf-8", errors="replace")
    except httpx.HTTPStatusError as exc:
        raise FetchError(f"HTTP {exc.response.status_code}", exc.response.status_code) from exc
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, ValueError) as exc:
        raise FetchError(str(exc) or type(exc).__name__) from exc


def extract(html: str, url: str) -> Article:
    raw = trafilatura.extract(html, url=url, output_format="json", with_metadata=True, include_comments=False)
    if not raw:
        return Article(title=None, text="", published_on=None)
    data = json.loads(raw)
    published = None
    if data.get("date"):
        try:
            published = date.fromisoformat(data["date"][:10])
        except ValueError:
            pass
    return Article(title=data.get("title"), text=(data.get("text") or "").strip(), published_on=published)


def paywall_reason(text: str, min_chars: int, markers: list[str]) -> str | None:
    """Why the extracted text looks incomplete, or None when it can go to analysis.

    A marker only counts in a fairly short text: a long article can end with "Déjà abonné ?"
    in a leftover of the page and still be complete.
    """
    if len(text) < min_chars:
        return f"text too short ({len(text)} characters)"
    if len(text) < MARKER_TEXT_FACTOR * max(min_chars, 500):
        lowered = text.lower()
        for marker in markers:
            if marker.lower() in lowered:
                return f"paywall marker: {marker!r}"
    return None
