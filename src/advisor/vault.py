"""Obsidian vault: the Inbox notes you write, the notes waiting for an article's text, the digest.

Layout used here:
    Inbox/              notes you add by hand (a URL, or a clipped/pasted article)
    Inbox/To-complete/  articles behind a paywall, waiting for their text or `action: withdraw`
    Inbox/Done/         notes already taken into the database (kept, never deleted)
    Journal/            the daily digest; its auto blocks are rewritten on each run of the day
    Sources/            one note per analysed article: summary and signals (auto block)
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

TEXT_HEADING = "## Text"
TEXT_HINT = "<!-- Paste the full article text below this line, then save the note. -->"
WITHDRAW_WORDS = {"withdraw", "retirer"}
AUTO_START = "<!-- auto -->"
AUTO_END = "<!-- /auto -->"
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


@dataclass
class InboxNote:
    path: Path
    url: str | None
    published: str | None
    text: str


@dataclass
class PendingNote:
    path: Path
    document_id: int | None
    withdraw: bool
    text: str


def slug(text: str, limit: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:limit].strip("-") or "note"


def split_note(content: str) -> tuple[dict, str]:
    """Frontmatter (as a dict) and body of a Markdown note."""
    match = FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), content[match.end():]


def _note(meta: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + body


def _move_to_done(vault: Path, path: Path) -> None:
    done = vault / "Inbox" / "Done"
    done.mkdir(parents=True, exist_ok=True)
    target = done / path.name
    n = 1
    while target.exists():
        n += 1
        target = done / f"{path.stem}-{n}{path.suffix}"
    shutil.move(str(path), target)


def inbox_notes(vault: Path) -> list[InboxNote]:
    """Notes dropped directly in Inbox/: `url` (or Web Clipper's `source`) in the frontmatter,
    optional `published` date, and the article text (clipped or pasted) as body."""
    notes = []
    for path in sorted((vault / "Inbox").glob("*.md")):
        meta, body = split_note(path.read_text(encoding="utf-8"))
        url = meta.get("url") or meta.get("source")
        if not url:
            first = body.strip().splitlines()[0].strip() if body.strip() else ""
            url = first if first.startswith(("http://", "https://")) else None
            body = body.strip()[len(first):] if url else body
        published = meta.get("published")
        notes.append(InboxNote(path=path, url=str(url) if url else None,
                               published=str(published)[:10] if published else None, text=body.strip()))
    return notes


def done_with(vault: Path, path: Path) -> None:
    _move_to_done(vault, path)


def pending_name(doc: sqlite3.Row) -> str:
    return f"{doc['id']}-{slug(doc['title'] or doc['url'])}"


def write_pending(vault: Path, doc: sqlite3.Row) -> Path:
    folder = vault / "Inbox" / "To-complete"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pending_name(doc)}.md"
    if path.exists():
        return path
    meta = {
        "document_id": doc["id"],
        "url": doc["url"],
        "source": doc["source"],
        "published": doc["published_on"],
        "reason": doc["note"],
        "action": "",
    }
    body = (
        f"# {doc['title'] or doc['url']}\n\n"
        f"{doc['summary'] or ''}\n\n"
        "Paste the article text under the heading below, or write `action: withdraw` in the\n"
        "properties to drop it. Nothing goes to analysis before one of the two.\n\n"
        f"{TEXT_HEADING}\n\n{TEXT_HINT}\n"
    )
    path.write_text(_note(meta, body), encoding="utf-8")
    return path


def pending_notes(vault: Path) -> list[PendingNote]:
    notes = []
    for path in sorted((vault / "Inbox" / "To-complete").glob("*.md")):
        meta, body = split_note(path.read_text(encoding="utf-8"))
        text = body.split(TEXT_HEADING, 1)[1] if TEXT_HEADING in body else ""
        text = text.replace(TEXT_HINT, "").strip()
        try:
            doc_id = int(meta.get("document_id"))
        except (TypeError, ValueError):
            doc_id = None
        action = str(meta.get("action") or "").strip().lower()
        notes.append(PendingNote(path=path, document_id=doc_id, withdraw=action in WITHDRAW_WORDS, text=text))
    return notes


def write_digest(vault: Path, day: date, lines: list[str], block: str = "auto") -> Path:
    """Write the digest between the markers of `block`; anything you wrote outside them is kept.
    The collection block opens the note, the other blocks follow it."""
    folder = vault / "Journal"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{day.isoformat()}.md"
    write_block(path, lines, block, first=block == "auto")
    return path


def write_block(path: Path, lines: list[str], block: str = "auto", first: bool = True) -> None:
    start_marker, end_marker = (AUTO_START, AUTO_END) if block == "auto" else (f"<!-- {block} -->", f"<!-- /{block} -->")
    text = f"{start_marker}\n" + "\n".join(lines) + f"\n{end_marker}"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    start = old.find(start_marker)
    end = old.rfind(end_marker)
    if start != -1 and end > start:
        content = old[:start] + text + old[end + len(end_marker):]
    elif not old:
        content = text + "\n"
    elif first:
        content = text + "\n\n" + old
    else:
        content = old.rstrip("\n") + "\n\n" + text + "\n"
    path.write_text(content, encoding="utf-8")


def source_name(doc: sqlite3.Row) -> str:
    return f"{doc['published_on'] or doc['added_on']}-{doc['id']}-{slug(doc['title'] or doc['url'], 50)}"


def write_source(vault: Path, doc: sqlite3.Row, lines: list[str]) -> Path:
    folder = vault / "Sources"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{source_name(doc)}.md"
    write_block(path, lines)
    return path


def md_text(text: str) -> str:
    """Keep external text from breaking the note's links and lists."""
    text = str(text).replace("<!--", "").replace("-->", "")
    return " ".join(text.replace("[", "(").replace("]", ")").replace("|", "/").split())


def md_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")
