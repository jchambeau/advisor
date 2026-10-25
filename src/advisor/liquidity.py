"""Liquidity filter: can an order of about EUR 1,000 go through without trouble?

Criteria (configurable in config.yaml):
- median daily turnover over the window (about 3 months);
- share of sessions with at least one trade;
- hysteresis: the entry threshold is higher than the exit one, to avoid flip-flopping.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import median

from .config import LiquidityRules

LIQUID, ILLIQUID, UNKNOWN = "liquid", "illiquid", "unknown"

# Preferred source when two sources cover the same session.
SOURCE_PRIORITY = {"boursorama": 0, "yahoo": 1}

NOK_MARKETS = ("oslo",)


@dataclass
class Session:
    day: str
    close: float | None
    volume: float | None
    currency: str = "EUR"


@dataclass
class Result:
    session_count: int
    median_turnover: float | None
    active_session_ratio: float | None
    status: str
    note: str = ""


def default_currency(market: str | None) -> str:
    return "NOK" if market and any(m in market.lower() for m in NOK_MARKETS) else "EUR"


def evaluate(
    sessions: list[Session],
    rules: LiquidityRules,
    currencies: dict[str, float],
    previous_status: str | None = None,
    as_of: date | None = None,
) -> Result:
    """Evaluate the sessions dated on or before `as_of` (default: all of them)."""
    if as_of is not None:
        sessions = [s for s in sessions if s.day <= as_of.isoformat()]
    window = sorted(sessions, key=lambda s: s.day)[-rules.window_sessions :]
    n = len(window)
    if n == 0:
        return Result(0, None, None, UNKNOWN, "no data")

    turnover = []
    for s in window:
        rate = currencies.get(s.currency)  # exact match: "GBp" (pence) must not pass for "GBP"
        if rate is None:
            return Result(n, None, None, UNKNOWN, f"no exchange rate for {s.currency} in config.yaml")
        turnover.append((s.close or 0) * (s.volume or 0) * rate)
    median_turnover = median(turnover)
    active = sum(1 for s in window if (s.volume or 0) > 0) / n

    if as_of is not None:
        age = (as_of - date.fromisoformat(window[-1].day)).days
        if age > rules.max_staleness_days:
            return Result(n, median_turnover, active, UNKNOWN, f"latest session {age} days old")
    if n < rules.min_sessions:
        return Result(n, median_turnover, active, UNKNOWN, "not enough sessions")
    threshold = rules.median_turnover_exit if previous_status == LIQUID else rules.median_turnover_entry
    ok = median_turnover >= threshold and active >= rules.min_active_session_ratio
    return Result(n, median_turnover, active, LIQUID if ok else ILLIQUID)


def stored_sessions(conn: sqlite3.Connection, isin: str) -> list[Session]:
    """One session per date; when two sources cover it, the preferred one wins."""
    by_day: dict[str, tuple[int, Session]] = {}
    for r in conn.execute(
        "SELECT date, close, volume, currency, source FROM daily_volume WHERE isin = ?", (isin,)
    ):
        rank = SOURCE_PRIORITY.get(r["source"], 9)
        if r["date"] not in by_day or rank < by_day[r["date"]][0]:
            by_day[r["date"]] = (rank, Session(r["date"], r["close"], r["volume"], r["currency"]))
    return [s for _, s in by_day.values()]


def previous_status(conn: sqlite3.Connection, isin: str, before: str) -> str | None:
    r = conn.execute(
        # "unknown" says nothing about the stock: look past it so the hysteresis survives a gap.
        "SELECT status FROM liquidity WHERE isin = ? AND computed_on < ? AND status <> 'unknown' "
        "ORDER BY computed_on DESC LIMIT 1",
        (isin, before),
    ).fetchone()
    return r["status"] if r else None


def compute(
    conn: sqlite3.Connection,
    companies: list[sqlite3.Row],
    rules: LiquidityRules,
    currencies: dict[str, float],
    day: date | None = None,
) -> dict[str, Result]:
    day = day or date.today()
    day_iso = day.isoformat()
    results = {}
    with conn:
        for c in companies:
            res = evaluate(
                stored_sessions(conn, c["isin"]), rules, currencies,
                previous_status(conn, c["isin"], day_iso), as_of=day,
            )
            conn.execute(
                """
                INSERT OR REPLACE INTO liquidity
                    (isin, computed_on, session_count, median_turnover, active_session_ratio, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (c["isin"], day_iso, res.session_count, res.median_turnover, res.active_session_ratio, res.status),
            )
            results[c["isin"]] = res
    return results


def export(path: Path, companies: list[sqlite3.Row], results: dict[str, Result]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["isin", "name", "market", "status", "median_turnover_eur", "active_session_ratio", "session_count", "note"])
        for c in sorted(companies, key=lambda c: (results[c["isin"]].status, c["name"])):
            r = results[c["isin"]]
            w.writerow([
                c["isin"], c["name"], c["market"], r.status,
                "" if r.median_turnover is None else round(r.median_turnover),
                "" if r.active_session_ratio is None else round(r.active_session_ratio, 3),
                r.session_count,
                r.note,
            ])
    return path
