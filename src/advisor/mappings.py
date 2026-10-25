"""Mapping between the ISIN (Euronext list) and Boursorama / Yahoo codes.

Boursorama pages do not show the ISIN: rows are matched by name, then the mappings.csv
file (columns isin;boursorama_code;yahoo_ticker) takes over and always wins over the
automatic match.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import unicodedata
from pathlib import Path

SUFFIXES = {"SA", "SE", "SAS", "SCA", "NV", "SPA", "ASA", "AS", "AB", "PLC", "AG", "LTD", "INC", "REIT"}


def normalize(name: str) -> str:
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    text = text.replace("&", " ET ").replace(".", "")  # "S.A." -> "SA"
    words = re.sub(r"[^A-Z0-9]+", " ", text).split()
    while len(words) > 1 and words[-1] in SUFFIXES:
        words.pop()
    return "".join(words)


def yahoo_ticker_from_boursorama(code: str) -> str | None:
    """Paris listings have a Boursorama code of the form 1rP<ticker>."""
    if code and code.startswith("1rP") and len(code) > 3:
        return f"{code[3:]}.PA"
    return None


def match(companies: list[sqlite3.Row], boursorama_names: dict[str, str]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Map Boursorama code -> ISIN by normalized name.

    `boursorama_names`: code -> displayed name. Returns (code -> isin, [(code, name) left unmatched]).
    A normalized name shared by several ISINs is never matched automatically.
    """
    index: dict[str, list[str]] = {}
    for c in companies:
        index.setdefault(normalize(c["name"]), []).append(c["isin"])
    matched, unmatched = {}, []
    for code, name in boursorama_names.items():
        candidates = index.get(normalize(name), [])
        if len(candidates) == 1:
            matched[code] = candidates[0]
        else:
            unmatched.append((code, name))
    return matched, unmatched


def load_manual(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:  # tolerate the BOM Excel adds
        reader = csv.DictReader(f, delimiter=";")
        if "isin" not in (reader.fieldnames or []):
            raise ValueError(f"{path.name}: expected header isin;boursorama_code;yahoo_ticker, got {reader.fieldnames}")
        return [
            {k: (v or "").strip() for k, v in row.items()}
            for row in reader
            if (row.get("isin") or "").strip()
        ]


def save(conn: sqlite3.Connection, automatic: dict[str, str], manual: list[dict]) -> None:
    manual_codes = {m["boursorama_code"] for m in manual if m.get("boursorama_code")}
    with conn:
        for code, isin in automatic.items():
            if code in manual_codes:
                continue  # a manual line already says which company this code belongs to
            conn.execute(
                """
                INSERT INTO mapping (isin, boursorama_code, yahoo_ticker, source)
                VALUES (?, ?, ?, 'auto')
                ON CONFLICT(isin) DO UPDATE SET
                    boursorama_code = CASE WHEN mapping.source = 'auto' OR mapping.boursorama_code IS NULL
                                           THEN excluded.boursorama_code ELSE mapping.boursorama_code END,
                    yahoo_ticker = COALESCE(mapping.yahoo_ticker, excluded.yahoo_ticker)
                """,
                (isin, code, yahoo_ticker_from_boursorama(code)),
            )
        for code in manual_codes:
            conn.execute(
                "UPDATE mapping SET boursorama_code = NULL WHERE boursorama_code = ? AND source = 'auto'", (code,)
            )
        for m in manual:
            conn.execute(
                """
                INSERT INTO mapping (isin, boursorama_code, yahoo_ticker, source)
                VALUES (?, ?, ?, 'manual')
                ON CONFLICT(isin) DO UPDATE SET
                    boursorama_code = COALESCE(excluded.boursorama_code, mapping.boursorama_code),
                    yahoo_ticker = COALESCE(excluded.yahoo_ticker, mapping.yahoo_ticker),
                    source = 'manual'
                """,
                (m["isin"].upper(), m.get("boursorama_code") or None, m.get("yahoo_ticker") or None),
            )


def isin_by_code(conn: sqlite3.Connection) -> dict[str, str]:
    # Manual rows come last so they win if a code still appears twice.
    return {
        r["boursorama_code"]: r["isin"]
        for r in conn.execute(
            "SELECT isin, boursorama_code FROM mapping WHERE boursorama_code IS NOT NULL "
            "ORDER BY source = 'manual', isin"
        )
    }


def export_unmatched(path: Path, unmatched: list[tuple[str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["boursorama_code", "boursorama_name"])
        w.writerows(sorted(unmatched, key=lambda r: r[1]))
    return path
