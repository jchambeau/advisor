"""Manual inclusions and exclusions (overrides.yaml), kept from one import to the next."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .euronext import is_valid_isin


@dataclass
class Overrides:
    exclude: dict[str, str] = field(default_factory=dict)  # isin -> reason
    include: list[dict] = field(default_factory=list)


def load(path: Path) -> Overrides:
    if not path.exists():
        return Overrides()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    exclude = {}
    for e in raw.get("exclude") or []:
        isin = str(e["isin"]).strip().upper()
        if not is_valid_isin(isin):
            raise ValueError(f"overrides: invalid ISIN in exclude: {isin}")
        exclude[isin] = e.get("reason", "")
    include = []
    for a in raw.get("include") or []:
        isin = str(a["isin"]).strip().upper()
        if not is_valid_isin(isin):
            raise ValueError(f"overrides: invalid ISIN in include: {isin}")
        include.append({**a, "isin": isin})
    return Overrides(exclude, include)


def apply_inclusions(conn: sqlite3.Connection, ov: Overrides) -> int:
    """Create the manually included companies that are not in the database yet."""
    n = 0
    with conn:
        for a in ov.include:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO company (isin, name, market, segment, country, listed)
                VALUES (?, ?, ?, ?, ?, 0)
                """,
                (a["isin"], a.get("name") or a["isin"], a.get("market"), a.get("segment"), a.get("country")),
            )
            n += cur.rowcount
    return n


def universe(conn: sqlite3.Connection, ov: Overrides) -> list[sqlite3.Row]:
    """Tracked companies: current Euronext list + manual inclusions - exclusions."""
    included = {a["isin"] for a in ov.include}
    rows = conn.execute("SELECT * FROM company ORDER BY name").fetchall()
    return [
        r for r in rows
        if (r["listed"] or r["isin"] in included) and r["isin"] not in ov.exclude
    ]
