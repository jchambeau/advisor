"""Collected documents: storage, duplicate detection and links to companies."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from functools import lru_cache
from datetime import date, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .mappings import SUFFIXES, normalize

TRACKING_PARAMS = re.compile(r"^(utm_.*|xtor|xtref|fbclid|gclid|mc_cid|mc_eid|at_.*|ns_.*|cmp|ref)$", re.I)
WORD_RE = re.compile(r"[^\W_]+(?:[.&'’-][^\W_]+)*", re.UNICODE)
MAX_NAME_WORDS = 4
MIN_NAME_KEY = 4  # shorter names (e.g. "ABC") match too many unrelated words
DUPLICATE_TITLE_DAYS = 3


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), ""))


def valid_url(url: str | None) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        parts = urlsplit(url)
        parts.port  # raises on a malformed port
    except ValueError:
        return False
    return bool(parts.netloc)


def title_key(title: str | None) -> str | None:
    if not title:
        return None
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    key = " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())
    return key or None


def find_duplicate(conn: sqlite3.Connection, url: str, title: str | None, published_on: date | None) -> int | None:
    """Id of a document already holding this URL, or the same title published within a few days."""
    row = conn.execute("SELECT id FROM document WHERE url = ?", (canonical_url(url),)).fetchone()
    if row:
        return row["id"]
    key = title_key(title)
    if not key or len(key) < 20:  # short titles ("Résultats annuels") are shared by many stories
        return None
    day = published_on or date.today()
    row = conn.execute(
        "SELECT id FROM document WHERE title_key = ? AND COALESCE(published_on, added_on) BETWEEN ? AND ?",
        (key, (day - timedelta(days=DUPLICATE_TITLE_DAYS)).isoformat(),
         (day + timedelta(days=DUPLICATE_TITLE_DAYS)).isoformat()),
    ).fetchone()
    return row["id"] if row else None


def add(
    conn: sqlite3.Connection,
    url: str,
    source: str,
    today: date,
    title: str | None = None,
    summary: str | None = None,
    published_on: date | None = None,
    text: str | None = None,
    status: str = "new",
) -> int | None:
    """Insert a document; returns its id, or None when it is a duplicate."""
    if find_duplicate(conn, url, title, published_on) is not None:
        return None
    with conn:
        cur = conn.execute(
            """
            INSERT INTO document (url, title_key, source, title, summary, text, published_on, added_on, status,
                                  ready_on)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (canonical_url(url), title_key(title), source, title, summary or None, text,
             published_on.isoformat() if published_on else None, today.isoformat(), status,
             today.isoformat() if status == "ready" else None),
        )
    return cur.lastrowid


def set_status(conn: sqlite3.Connection, doc_id: int, status: str, note: str | None = None, *,
               day: date | None = None, **fields) -> None:
    """Change a document's status; `day` (default today) is recorded when it becomes ready."""
    columns = {"status": status, "note": note, **fields}
    if status == "ready":
        columns["ready_on"] = (day or date.today()).isoformat()
    if "title" in fields:
        columns["title_key"] = title_key(fields["title"])
    assignments = ", ".join(f"{name} = ?" for name in columns)
    with conn:
        conn.execute(f"UPDATE document SET {assignments} WHERE id = ?", (*columns.values(), doc_id))


def link(conn: sqlite3.Connection, doc_id: int, isin: str, how: str) -> None:
    with conn:
        conn.execute("INSERT OR IGNORE INTO document_company (document_id, isin, how) VALUES (?, ?, ?)", (doc_id, isin, how))


class NameIndex:
    """Finds company names in a text, by the same normalization as the Boursorama name match."""

    def __init__(self, companies: list[sqlite3.Row]):
        by_key: dict[str, list[str]] = {}
        for c in companies:
            key = normalize(c["name"])
            if len(key) >= MIN_NAME_KEY:
                by_key.setdefault(key, []).append(c["isin"])
        # A name shared by several companies says nothing about which one is meant.
        self.isin_by_key = {k: v[0] for k, v in by_key.items() if len(v) == 1}

    def find(self, text: str) -> set[str]:
        words = WORD_RE.findall(text or "")
        keys = [_word_key(w) for w in words]  # normalized once per word, not once per chunk
        found = set()
        for i, word in enumerate(words):
            # A company name starts with a capital or a digit (2CRSI): "lectra" or "orange se"
            # in lower case are more likely ordinary words.
            if not (word[0].isupper() or word[0].isdigit()):
                continue
            for n in range(1, MAX_NAME_WORDS + 1):
                chunk = keys[i:i + n]
                if len(chunk) < n:
                    break
                while len(chunk) > 1 and chunk[-1] in SUFFIXES:  # as normalize() drops "SA", "SE"...
                    chunk = chunk[:-1]
                isin = self.isin_by_key.get("".join(chunk))
                if isin:
                    found.add(isin)
        return found


@lru_cache(maxsize=50_000)
def _word_key(word: str) -> str:
    return normalize(word)
