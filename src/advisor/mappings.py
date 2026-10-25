"""Mapping between the ISIN (Euronext list) and Boursorama / Yahoo codes.

The Boursorama quotes list does not show the ISIN: rows are matched by name first. Stocks
left unmatched are looked up on their own Boursorama page, which shows the ISIN (source
'page', which wins over a name match). The mappings.csv file (columns
isin;boursorama_code;yahoo_ticker) always wins over both.
"""

from __future__ import annotations

import csv
import re
import sqlite3
import unicodedata
from datetime import date, timedelta
from pathlib import Path

# A company listed on several Euronext venues shows up once per venue in the quotes list:
# the code prefix tells the venue, and the one matching the company's market is kept.
VENUE_PREFIXES = {"PARIS": "1rP", "AMSTERDAM": "1rA"}
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


def pick_code(codes: list[str], market: str, strict: bool = False) -> str | None:
    """The code to keep among the Boursorama codes pointing to one ISIN, or None if unclear.

    With `strict`, a lone code on another venue than the company's (when that venue's prefix is
    known) is refused too: its venue's own line may simply not have been looked up yet.
    """
    prefixes = [prefix for city, prefix in VENUE_PREFIXES.items() if city in (market or "").upper()]
    if len(codes) == 1 and not (strict and prefixes):
        return codes[0]
    on_venue = [c for c in codes if any(c.startswith(p) for p in prefixes)]
    return on_venue[0] if len(on_venue) == 1 else None


def _keep_one_per_isin(found: dict[str, str], market: dict[str, str], strict: bool = False) -> tuple[dict[str, str], list[str]]:
    """Keep one code per ISIN (see pick_code). Returns (kept code -> isin, codes left unresolved)."""
    codes_per_isin: dict[str, list[str]] = {}
    for code, isin in found.items():
        codes_per_isin.setdefault(isin, []).append(code)
    kept, unresolved = {}, []
    for isin, codes in codes_per_isin.items():
        keep = pick_code(codes, market[isin], strict)
        if keep:
            kept[keep] = isin  # the other codes are secondary listings of the same company
        else:
            unresolved.extend(codes)
    return kept, unresolved


def match(companies: list[sqlite3.Row], boursorama_names: dict[str, str]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Map Boursorama code -> ISIN by normalized name.

    `boursorama_names`: code -> displayed name. Returns (code -> isin, [(code, name) left unmatched]).
    A normalized name shared by several ISINs is never matched automatically. When several
    codes match one ISIN, the one on the company's venue is kept and the others are dropped
    (secondary listings); if no single code is on that venue, all of them stay unmatched.
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
    market = {c["isin"]: c["market"] or "" for c in companies}
    matched, unresolved = _keep_one_per_isin(matched, market)
    unmatched += [(code, boursorama_names[code]) for code in unresolved]
    return matched, unmatched


def load_manual(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig") as f:  # tolerate the BOM Excel adds
        reader = csv.DictReader(f, delimiter=";")
        if "isin" not in (reader.fieldnames or []):
            raise ValueError(f"{path.name}: expected header isin;boursorama_code;yahoo_ticker, got {reader.fieldnames}")
        return [
            {k: (v or "").strip() for k, v in row.items() if k is not None}  # extra columns go under None
            for row in reader
            if (row.get("isin") or "").strip()
        ]


def save(conn: sqlite3.Connection, automatic: dict[str, str], manual: list[dict]) -> None:
    manual_codes = {m["boursorama_code"] for m in manual if m.get("boursorama_code")}
    page_codes = {r[0] for r in conn.execute("SELECT boursorama_code FROM mapping WHERE source = 'page'")}
    with conn:
        for code, isin in automatic.items():
            if code in manual_codes or code in page_codes:
                continue  # a manual line or the stock's own page already says which company it is
            # The code now belongs to this ISIN: release it from any other automatic row.
            conn.execute(
                "UPDATE mapping SET boursorama_code = NULL WHERE boursorama_code = ? AND isin <> ? AND source = 'auto'",
                (code, isin),
            )
            conn.execute(
                """
                INSERT INTO mapping (isin, boursorama_code, yahoo_ticker, source)
                VALUES (?, ?, ?, 'auto')
                ON CONFLICT(isin) DO UPDATE SET
                    boursorama_code = CASE WHEN mapping.source = 'auto' OR mapping.boursorama_code IS NULL
                                           THEN excluded.boursorama_code ELSE mapping.boursorama_code END,
                    yahoo_ticker = COALESCE(mapping.yahoo_ticker, excluded.yahoo_ticker),
                    source = CASE WHEN mapping.source = 'page' AND mapping.boursorama_code IS NULL
                                  THEN 'auto' ELSE mapping.source END
                """,
                (isin, code, yahoo_ticker_from_boursorama(code)),
            )
        for code in manual_codes:
            conn.execute(
                "UPDATE mapping SET boursorama_code = NULL WHERE boursorama_code = ? AND source <> 'manual'", (code,)
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
    # Manual rows come last, then page rows, so they win if a code still appears twice.
    return {
        r["boursorama_code"]: r["isin"]
        for r in conn.execute(
            "SELECT isin, boursorama_code FROM mapping WHERE boursorama_code IS NOT NULL "
            "ORDER BY CASE source WHEN 'manual' THEN 2 WHEN 'page' THEN 1 ELSE 0 END, isin"
        )
    }


def codes_to_look_up(conn: sqlite3.Connection, codes: list[str], today: date, retry_days: int) -> list[str]:
    """Codes never looked up on their page, or last looked up `retry_days` ago or more.

    Codes already mapped never get here, so a page is read again only while its stock stays
    unmatched (e.g. its ISIN joins a later list, or the page showed nothing).
    """
    cutoff = (today - timedelta(days=retry_days)).isoformat()
    checked = {
        r["boursorama_code"]
        for r in conn.execute("SELECT boursorama_code FROM stock_page_isin WHERE checked_on > ?", (cutoff,))
    }
    return [c for c in codes if c not in checked]


def record_lookup(conn: sqlite3.Connection, code: str, isin: str | None, today: date) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO stock_page_isin (boursorama_code, isin, checked_on) VALUES (?, ?, ?)",
            (code, isin, today.isoformat()),
        )


def matches_from_lookups(conn: sqlite3.Connection, companies: list[sqlite3.Row], codes: list[str]) -> dict[str, str]:
    """code -> ISIN for the given codes whose page showed an ISIN of the universe not mapped yet."""
    market = {c["isin"]: c["market"] or "" for c in companies}
    mapped = set(isin_by_code(conn).values())
    marks = ",".join("?" * len(codes))
    found = {
        r["boursorama_code"]: r["isin"]
        for r in conn.execute(f"SELECT boursorama_code, isin FROM stock_page_isin WHERE boursorama_code IN ({marks})", codes)
        if r["isin"] in market and r["isin"] not in mapped
    }
    return _keep_one_per_isin(found, market, strict=True)[0]


def save_page_matches(conn: sqlite3.Connection, found: dict[str, str]) -> None:
    """Store matches read on the stocks' own pages; they win over name matches, not over manual lines."""
    with conn:
        for code, isin in found.items():
            conn.execute(
                "UPDATE mapping SET boursorama_code = NULL WHERE boursorama_code = ? AND isin <> ? AND source <> 'manual'",
                (code, isin),
            )
            conn.execute(
                """
                INSERT INTO mapping (isin, boursorama_code, yahoo_ticker, source)
                VALUES (?, ?, ?, 'page')
                ON CONFLICT(isin) DO UPDATE SET
                    boursorama_code = CASE WHEN mapping.source = 'manual' AND mapping.boursorama_code IS NOT NULL
                                           THEN mapping.boursorama_code ELSE excluded.boursorama_code END,
                    yahoo_ticker = COALESCE(mapping.yahoo_ticker, excluded.yahoo_ticker),
                    source = CASE WHEN mapping.source = 'manual' THEN 'manual' ELSE 'page' END
                """,
                (isin, code, yahoo_ticker_from_boursorama(code)),
            )


NOT_LISTED = "not in the Euronext list: nothing to do"
OTHER_VENUE = "other line of a matched company (another venue): nothing to do"
NOTHING_TO_DO = {NOT_LISTED, OTHER_VENUE}


def export_unmatched(
    conn: sqlite3.Connection, path: Path, unmatched: list[tuple[str, str]], universe: set[str]
) -> tuple[Path, int]:
    """Write the unmatched stocks with why each one is left out; those to act on come first.

    Returns the path and the number of stocks that need nothing (their page shows an ISIN
    outside the Euronext list: Boursorama's PEA PME filter is wider than the official list).
    """
    on_page = {
        r["boursorama_code"]: r["isin"] for r in conn.execute("SELECT boursorama_code, isin FROM stock_page_isin")
    }
    known = {r["isin"] for r in conn.execute("SELECT isin FROM company")}
    mapped = set(isin_by_code(conn).values())
    lines = []
    for code, name in unmatched:
        isin = on_page.get(code)
        if code not in on_page:
            note = "page not read yet"
        elif isin is None:
            note = "no ISIN on the page"
        elif isin in mapped:
            note = OTHER_VENUE
        elif isin in universe:
            note = "ISIN of the list not matched yet: next run, or add it to mappings.csv"
        elif isin in known:
            note = "excluded in overrides.yaml or removed from the list"
        else:
            note = NOT_LISTED
        lines.append((code, name, isin or "", note))
    nothing_to_do = sum(1 for line in lines if line[3] in NOTHING_TO_DO)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["boursorama_code", "boursorama_name", "isin_on_page", "note"])
        w.writerows(sorted(lines, key=lambda r: (r[3] in NOTHING_TO_DO, r[3], r[1])))
    return path, nothing_to_do
