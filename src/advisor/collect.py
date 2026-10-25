"""News collection: vault Inbox, RSS feeds and Boursorama stock news pages, then article texts.

Nothing here calls a language model. An article whose text looks incomplete (paywall) waits in
Inbox/To-complete/ until its text is pasted or it is withdrawn, so it never reaches analysis.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.parse import quote

import httpx

from . import articles, documents, sources, vault
from .config import Config

ACCESS_REFUSED = {401, 402, 403}  # usually a paywall or a site refusing robots: ask for the text
MAX_ATTEMPTS = 3
MIN_TEXT_PAGES = 200
WITHOUT_TEXT = ("new", "fetch_failed", "pending")  # stock news pages and manual adds: only an (almost) empty text is suspicious


@dataclass
class Summary:
    inbox: int = 0
    completed: int = 0
    withdrawn: int = 0
    added: int = 0
    ready: int = 0
    pending: int = 0
    failed: int = 0
    too_old: int = 0
    news_pages: int = 0
    problems: list[str] = field(default_factory=list)
    touched: set[int] = field(default_factory=set)


def _date(text: str | None) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10]) if text else None
    except ValueError:
        return None


def client_for(cfg: Config) -> httpx.Client:
    # Article links come from third-party feeds: never let one reach this machine or the local
    # network, including through a redirect.
    return httpx.Client(headers=articles.HTTP_HEADERS, timeout=float(cfg.collect["timeout_seconds"]),
                        follow_redirects=True, event_hooks={"request": [articles.refuse_local]})


def add_manual(conn: sqlite3.Connection, url: str, today: date, published: date | None = None,
               text: str | None = None) -> int | None:
    """An article you found yourself: same path as the collected ones. Returns None if already known."""
    has_text = bool(text and len(text) >= MIN_TEXT_PAGES)
    return documents.add(conn, url, "manual", today, published_on=published,
                         text=text if has_text else None, status="ready" if has_text else "new")


def take_inbox(conn: sqlite3.Connection, cfg: Config, today: date, summary: Summary) -> None:
    for note in vault.inbox_notes(cfg.vault):
        if not documents.valid_url(note.url):
            summary.problems.append(f"Inbox note without a URL left in place: {note.path.name}")
            continue
        doc_id = add_manual(conn, note.url, today, _date(note.published), note.text)
        if doc_id is None:
            existing = documents.find_duplicate(conn, note.url, None, None)
            row = conn.execute("SELECT status FROM document WHERE id = ?", (existing,)).fetchone()
            # Your text wins over a fetch that has not happened, failed or hit a paywall.
            if row and row["status"] in WITHOUT_TEXT and len(note.text) >= MIN_TEXT_PAGES:
                documents.set_status(conn, existing, "ready", "text from an Inbox note", day=today, text=note.text)
                summary.completed += 1
                summary.touched.add(existing)
        else:
            summary.inbox += 1
            summary.touched.add(doc_id)
        vault.done_with(cfg.vault, note.path)


def take_pending(conn: sqlite3.Connection, cfg: Config, today: date, summary: Summary) -> None:
    for note in vault.pending_notes(cfg.vault):
        row = conn.execute("SELECT status FROM document WHERE id = ?", (note.document_id,)).fetchone()
        if row is None or row["status"] != "pending":
            vault.done_with(cfg.vault, note.path)  # already handled (e.g. text sent through Inbox/)
        elif note.withdraw:
            documents.set_status(conn, note.document_id, "withdrawn", "withdrawn by you")
            summary.withdrawn += 1
            vault.done_with(cfg.vault, note.path)
        elif note.text:
            documents.set_status(conn, note.document_id, "ready", "text pasted by you", day=today, text=note.text)
            summary.completed += 1
            summary.touched.add(note.document_id)
            vault.done_with(cfg.vault, note.path)


def collect_feeds(conn: sqlite3.Connection, cfg: Config, client: httpx.Client, today: date,
                  summary: Summary) -> None:
    oldest = today - timedelta(days=int(cfg.collect["max_age_days"]))
    for source in sources.load(cfg.sources):
        if not source.enabled:
            continue
        try:
            response = client.get(source.url)
            response.raise_for_status()
            items = sources.parse_feed(response.content)
        except httpx.HTTPError as exc:
            summary.problems.append(f"feed {source.name}: {exc}")
            continue
        if not items:
            summary.problems.append(f"feed {source.name}: no article read (wrong URL or not a feed?)")
        for item in items:
            if (item.published_on and item.published_on < oldest) or not documents.valid_url(item.url):
                continue
            doc_id = documents.add(conn, item.url, source.name, today, item.title, item.summary, item.published_on)
            if doc_id:
                summary.added += 1
                summary.touched.add(doc_id)
        time.sleep(float(cfg.collect["pause_seconds"]))


def news_page_targets(conn: sqlite3.Connection, isins: set[str], limit: int) -> list[tuple[str, str, bool]]:
    """(isin, Boursorama code, first visit) for the stocks whose news page was read longest ago."""
    rows = conn.execute(
        """
        SELECT m.isin, m.boursorama_code, v.visited_on
        FROM mapping m LEFT JOIN news_page_visit v ON v.boursorama_code = m.boursorama_code
        WHERE m.boursorama_code IS NOT NULL
        ORDER BY v.visited_on IS NOT NULL, v.visited_on, m.isin
        """
    ).fetchall()
    return [(r["isin"], r["boursorama_code"], r["visited_on"] is None) for r in rows if r["isin"] in isins][:limit]


def collect_news_pages(conn: sqlite3.Connection, cfg: Config, client: httpx.Client, isins: set[str],
                       today: date, summary: Summary, on_page: Callable[[int, str], None] | None = None) -> None:
    settings = cfg.collect["boursorama_news"]
    targets = news_page_targets(conn, isins, int(settings["pages_per_run"]))
    for i, (isin, code, first_visit) in enumerate(targets, 1):
        url = settings["url"].format(code=quote(code, safe=""))
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            summary.problems.append(f"news page {code}: {exc}")
        else:
            items = sources.parse_news_page(response.text, url)
            if first_visit:  # the page goes back years: only the latest items on a first visit
                items = items[: int(settings["first_visit_items"])]
            for item in items:
                doc_id = documents.add(conn, item.url, "boursorama", today, item.title)
                if doc_id:
                    summary.added += 1
                    summary.touched.add(doc_id)
                else:  # already known, possibly under another URL with the same title
                    doc_id = documents.find_duplicate(conn, item.url, item.title, None)
                if doc_id:
                    documents.link(conn, doc_id, isin, "page")
            with conn:
                conn.execute("INSERT OR REPLACE INTO news_page_visit VALUES (?, ?)", (code, today.isoformat()))
            summary.news_pages += 1
        if on_page:
            on_page(i, code)
        time.sleep(float(cfg.collect["pause_seconds"]))


def fetch_texts(conn: sqlite3.Connection, cfg: Config, client: httpx.Client, today: date, summary: Summary,
                on_doc: Callable[[int, str], None] | None = None, limit: int | None = None) -> None:
    settings = cfg.collect
    feeds = {s.name: s for s in sources.load(cfg.sources)}
    rows = conn.execute(
        "SELECT * FROM document WHERE status = 'new' OR (status = 'fetch_failed' AND attempts < ?) ORDER BY id LIMIT ?",
        (MAX_ATTEMPTS, limit if limit is not None else int(settings["max_article_fetches"])),
    ).fetchall()
    for i, doc in enumerate(rows, 1):
        summary.touched.add(doc["id"])
        _fetch_one(conn, cfg, client, doc, feeds.get(doc["source"]), today, summary)
        if on_doc:
            on_doc(i, doc["title"] or doc["url"])
        if i < len(rows):
            time.sleep(float(settings["pause_seconds"]))


def _fetch_one(conn, cfg: Config, client, doc, feed: sources.Source | None, today: date, summary: Summary) -> None:
    settings = cfg.collect
    oldest = today - timedelta(days=int(settings["max_age_days"]))
    try:
        html = articles.fetch_html(client, doc["url"], int(settings["max_response_bytes"]))
    except articles.FetchError as exc:
        if exc.status in ACCESS_REFUSED:
            _pending_or_summary(conn, cfg, doc, feed, f"access refused (HTTP {exc.status})", today, summary)
        else:
            documents.set_status(conn, doc["id"], "fetch_failed", str(exc), attempts=doc["attempts"] + 1)
            summary.failed += 1
        return
    article = articles.extract(html, doc["url"])
    published = _date(doc["published_on"]) or article.published_on
    fields = {"published_on": published.isoformat() if published else None}
    if not doc["title"] and article.title:
        fields["title"] = article.title
    if doc["source"] != "manual" and published and published < oldest:
        documents.set_status(conn, doc["id"], "too_old", None, **fields)
        summary.too_old += 1
        return
    min_chars = int(settings["min_text_chars"]) if feed else MIN_TEXT_PAGES
    reason = articles.paywall_reason(article.text, min_chars, settings["paywall_markers"])
    if reason:
        documents.set_status(conn, doc["id"], doc["status"], None, **fields)
        _pending_or_summary(conn, cfg, doc, feed, reason, today, summary)
        return
    documents.set_status(conn, doc["id"], "ready", None, day=today, text=article.text, **fields)
    summary.ready += 1


def _pending_or_summary(conn, cfg: Config, doc, feed: sources.Source | None, reason: str, today: date,
                        summary: Summary) -> None:
    if feed and feed.on_paywall == "summary" and doc["summary"]:
        documents.set_status(conn, doc["id"], "ready", f"summary only ({reason})", day=today, text=doc["summary"])
        summary.ready += 1
        return
    documents.set_status(conn, doc["id"], "pending", reason)
    vault.write_pending(cfg.vault, conn.execute("SELECT * FROM document WHERE id = ?", (doc["id"],)).fetchone())
    summary.pending += 1


def link_names(conn: sqlite3.Connection, companies: list[sqlite3.Row], doc_ids: set[int]) -> None:
    index = documents.NameIndex(companies)
    for doc_id in sorted(doc_ids):
        doc = conn.execute("SELECT title, summary, text FROM document WHERE id = ?", (doc_id,)).fetchone()
        if doc is None:
            continue
        for isin in index.find("\n".join(filter(None, (doc["title"], doc["summary"], doc["text"])))):
            documents.link(conn, doc_id, isin, "name")


def digest_lines(conn: sqlite3.Connection, today: date, summary: Summary) -> list[str]:
    lines = [f"# Digest {today.isoformat()}", "",
             "_Written by `advisor collect`: this block is rewritten on each run, write outside it._", ""]
    pending = conn.execute("SELECT * FROM document WHERE status = 'pending' ORDER BY id").fetchall()
    lines.append(f"## Waiting for you ({len(pending)})")
    lines.append("")
    if pending:
        lines.append("Paste the text, or set `action: withdraw`, in each note:")
        for doc in pending:
            lines.append(f"- [[Inbox/To-complete/{vault.pending_name(doc)}|{_md(doc['title'] or doc['url'])}]]"
                         f" ({doc['source']}, {doc['note']})")
    else:
        lines.append("Nothing to complete.")
    new = conn.execute(
        """
        SELECT d.*, GROUP_CONCAT(DISTINCT c.name) AS companies
        FROM document d
        LEFT JOIN document_company dc ON dc.document_id = d.id
        LEFT JOIN company c ON c.isin = dc.isin
        WHERE d.ready_on = ? AND d.status = 'ready'
        GROUP BY d.id ORDER BY companies IS NULL, companies, d.published_on DESC
        """,
        (today.isoformat(),),
    ).fetchall()
    lines += ["", f"## Ready for analysis today ({len(new)})", ""]
    for doc in new:
        who = f"**{_md(doc['companies'])}**: " if doc["companies"] else ""
        lines.append(f"- {who}[{_md(doc['title'] or doc['url'])}]({_url(doc['url'])}) ({doc['source']})")
    if not new:
        lines.append("No new article ready for analysis.")
    failed = conn.execute("SELECT COUNT(*) AS n FROM document WHERE status = 'fetch_failed'").fetchone()["n"]
    problems = summary.problems + ([f"{failed} articles could not be fetched (retried up to {MAX_ATTEMPTS} times)"]
                                   if failed else [])
    if problems:
        lines += ["", "## Problems", ""] + [f"- {_md(p)}" for p in problems]
    return lines


def _md(text: str) -> str:
    """Keep external text from breaking the note's links and lists."""
    text = str(text).replace("<!--", "").replace("-->", "")
    return " ".join(text.replace("[", "(").replace("]", ")").replace("|", "/").split())


def _url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")
