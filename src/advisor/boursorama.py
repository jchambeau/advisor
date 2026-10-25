"""Evening snapshot of Boursorama's "PEA PME quotes" page (close and volume of the session).

The page only shows one session: the history is built by saving a snapshot every trading
evening. Check that the site's terms of use allow this collection before scheduling it.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date

import httpx
from bs4 import BeautifulSoup

CODE_RE = re.compile(r"/cours/([^/?#]+)/?")
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) advisor-watch/0.1",
    "Accept-Language": "fr-FR,fr;q=0.9",
}


@dataclass
class Row:
    code: str
    name: str
    close: float | None
    volume: float | None


def parse_number(text: str | None) -> float | None:
    """French number format: '26,620 (c)' -> 26.62 ; '1 234 567' -> 1234567 ; '-' -> None."""
    if not text:
        return None
    t = re.sub(r"\(.*?\)", "", text)
    t = t.replace(" ", "").replace("\xa0", "").replace(" ", "").replace("%", "")
    t = t.replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group()) if m else None


def parse(html: str) -> list[Row]:
    """Extract the rows of the table whose header has "Libellé", "Dernier" and "Vol." (name, last, volume)."""
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        headers = [" ".join(th.get_text(" ", strip=True).split()).lower() for th in table.find_all("th")]
        if not headers:
            continue
        try:
            i_name = next(i for i, h in enumerate(headers) if h.startswith("libell"))
            i_close = next(i for i, h in enumerate(headers) if h.startswith("dernier"))
            i_vol = next(i for i, h in enumerate(headers) if h.startswith("vol"))
        except StopIteration:
            continue
        rows = []
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if len(cells) <= max(i_name, i_close, i_vol):
                continue
            link = cells[i_name].find("a", href=CODE_RE)
            if link is None:
                continue
            code = CODE_RE.search(link["href"]).group(1)
            name = link.get("title") or link.get_text(" ", strip=True)
            rows.append(
                Row(
                    code=code,
                    name=" ".join(name.split()),
                    close=parse_number(cells[i_close].get_text(" ", strip=True)),
                    volume=parse_number(cells[i_vol].get_text(" ", strip=True)) or 0.0,
                )
            )
        return rows
    return []


def page_url(base: str, market_filter: str, page: int) -> str:
    base = base.rstrip("/") + "/"
    path = base if page == 1 else f"{base}page-{page}"
    return f"{path}?quotation_az_filter%5Bmarket%5D={market_filter}"


def fetch(config: dict, client: httpx.Client | None = None) -> tuple[list[Row], str | None]:
    """Walk the pages until one of them brings no new stock.

    Returns the rows and, when a later page failed, the error that stopped the crawl
    (the rows already read are kept). A failure on the first page raises.
    """
    owned = client is None
    client = client or httpx.Client(headers=HTTP_HEADERS, timeout=30, follow_redirects=True)
    seen: dict[str, Row] = {}
    error = None
    try:
        for page in range(1, int(config.get("max_pages", 60)) + 1):
            try:
                response = client.get(page_url(config["url"], config["market_filter"], page))
                response.raise_for_status()
            except httpx.HTTPError as exc:
                if page == 1:
                    raise
                error = f"page {page}: {exc}"
                break
            new_rows = [r for r in parse(response.text) if r.code not in seen]
            if not new_rows:
                break
            seen.update({r.code: r for r in new_rows})
            time.sleep(float(config.get("pause_seconds", 2.0)))
    finally:
        if owned:
            client.close()
    return list(seen.values()), error


def repeated_share(conn: sqlite3.Connection, rows: list[Row], isin_by_code: dict[str, str], day: date) -> float | None:
    """Share of stocks whose close and volume equal their latest stored snapshot before `day`.

    A high share means the page still shows a session already saved (holiday, weekend,
    run before the open). Stocks with no trade in that snapshot are left out: an unchanged
    zero says nothing about the market being closed. Returns None when there is nothing
    to compare with.
    """
    same = compared = 0
    for r in rows:
        isin = isin_by_code.get(r.code)
        if isin is None or r.close is None:
            continue
        last = conn.execute(
            """
            SELECT close, volume FROM daily_volume
            WHERE isin = ? AND source = 'boursorama' AND date < ? ORDER BY date DESC LIMIT 1
            """,
            (isin, day.isoformat()),
        ).fetchone()
        if last is None or not last["volume"]:
            continue
        compared += 1
        same += last["close"] == r.close and last["volume"] == r.volume
    return same / compared if compared else None


def save(
    conn: sqlite3.Connection,
    rows: list[Row],
    isin_by_code: dict[str, str],
    currency_by_isin: dict[str, str],
    day: date,
) -> int:
    n = 0
    with conn:
        for r in rows:
            isin = isin_by_code.get(r.code)
            if isin is None or r.close is None:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_volume (isin, date, close, volume, currency, source)
                VALUES (?, ?, ?, ?, ?, 'boursorama')
                """,
                (isin, day.isoformat(), r.close, r.volume, currency_by_isin.get(isin, "EUR")),
            )
            n += 1
    return n
