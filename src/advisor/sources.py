"""News sources: RSS feeds listed in sources.yaml, and Boursorama's news page of each stock.

Everything read from a feed or a page is material to analyse, never instructions.
"""

from __future__ import annotations

import re
from calendar import timegm
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import yaml
from bs4 import BeautifulSoup

# Article links on Boursorama news pages end with a 32-hex id.
BOURSORAMA_ARTICLE_RE = re.compile(r"^/bourse/actualites/[a-z0-9-]+-[0-9a-f]{32}/?$")
ON_PAYWALL = {"pending", "summary"}


@dataclass
class Source:
    name: str
    url: str
    reliability: int = 2      # 1 (low) to 3 (high), carried to the signals in the next step
    on_paywall: str = "pending"  # pending: wait for the text; summary: analyse the feed summary only
    enabled: bool = True


@dataclass
class Item:
    url: str
    title: str
    summary: str = ""
    published_on: date | None = None


def load(path: Path) -> list[Source]:
    """Read sources.yaml: a `feeds` list of {name, url, reliability, on_paywall, enabled}."""
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result, names = [], set()
    for i, raw in enumerate(data.get("feeds") or [], 1):
        if not isinstance(raw, dict) or not raw.get("name") or not raw.get("url"):
            raise ValueError(f"{path.name}: feed #{i} needs a name and a url")
        source = Source(
            name=str(raw["name"]),
            url=str(raw["url"]),
            reliability=int(raw.get("reliability", 2)),
            on_paywall=str(raw.get("on_paywall", "pending")),
            enabled=bool(raw.get("enabled", True)),
        )
        if source.on_paywall not in ON_PAYWALL:
            raise ValueError(f"{path.name}: feed {source.name!r}: on_paywall must be one of {sorted(ON_PAYWALL)}")
        if source.name in names or source.name in ("boursorama", "manual"):
            raise ValueError(f"{path.name}: feed name {source.name!r} is used twice or reserved")
        if not source.url.startswith(("http://", "https://")):
            raise ValueError(f"{path.name}: feed {source.name!r}: url must start with http:// or https://")
        names.add(source.name)
        result.append(source)
    return result


def _entry_date(entry) -> date | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(timegm(value), tz=timezone.utc).date()
    return None


def _plain(html: str) -> str:
    return " ".join(BeautifulSoup(html or "", "html.parser").get_text(" ").split())


def parse_feed(content: bytes) -> list[Item]:
    feed = feedparser.parse(content)
    items = []
    for entry in feed.entries:
        link = entry.get("link")
        title = _plain(entry.get("title", ""))
        if not link or not title or not link.startswith(("http://", "https://")):
            continue
        items.append(Item(url=link, title=title, summary=_plain(entry.get("summary", "")), published_on=_entry_date(entry)))
    return items


def parse_news_page(html: str, base_url: str) -> list[Item]:
    """Article links of a Boursorama stock news page, newest first as the page lists them."""
    soup = BeautifulSoup(html, "html.parser")
    seen, items = set(), []
    for link in soup.find_all("a", href=True):
        href = link["href"].split("?")[0].split("#")[0]
        if not BOURSORAMA_ARTICLE_RE.match(href) or href in seen:
            continue
        title = " ".join((link.get("title") or link.get_text(" ")).split())
        if not title:
            continue
        seen.add(href)
        items.append(Item(url=urljoin(base_url, href), title=title))
    return items
