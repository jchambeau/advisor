"""Price and volume history through yfinance, to seed the 3-month window.

Unofficial source with uneven coverage of small caps: it complements the Boursorama
snapshots, which take precedence for the same session.
"""

from __future__ import annotations

import logging
import sqlite3
import warnings
from collections.abc import Callable

from .liquidity import default_currency


def _yfinance():
    try:
        import yfinance
    except ImportError as exc:  # optional dependency
        raise SystemExit("yfinance is not installed: pip install -e '.[history]'") from exc
    # Unresolved tickers are expected (ISIN fallback) and counted in the summary: keep yfinance quiet.
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    # yfinance's own pandas calls raise deprecation warnings we cannot act on (e.g. Pandas4Warning
    # about `inplace=` with pandas 3.1); our own code is not affected by this filter.
    warnings.filterwarnings("ignore", category=Warning, module=r"yfinance(\.|$)")
    return yfinance


def backfill(
    conn: sqlite3.Connection,
    companies: list[sqlite3.Row],
    period: str = "6mo",
    on_company: Callable[[int, str], None] | None = None,
) -> dict[str, int]:
    """Return, per ISIN, the number of sessions saved (0 when nothing was found).

    `on_company(index, name)` is called after each company, index starting at 1.
    """
    yf = _yfinance()
    tickers = {
        r["isin"]: r["yahoo_ticker"]
        for r in conn.execute("SELECT isin, yahoo_ticker FROM mapping WHERE yahoo_ticker IS NOT NULL")
    }
    summary = {}
    for i, c in enumerate(companies, start=1):
        summary[c["isin"]] = _backfill_one(conn, yf, c, tickers.get(c["isin"], c["isin"]), period)
        if on_company:
            on_company(i, c["name"])
    return summary


def _backfill_one(conn: sqlite3.Connection, yf, company: sqlite3.Row, symbol: str, period: str) -> int:
    """Save the history of one company; `symbol` may be an ISIN, which yfinance can sometimes resolve."""
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=period, interval="1d", auto_adjust=False)
    except Exception:
        return 0
    try:
        currency = ticker.fast_info["currency"] or default_currency(company["market"])
    except Exception:
        currency = default_currency(company["market"])
    n = 0
    with conn:
        for day, row in hist.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO daily_volume (isin, date, close, volume, currency, source)
                VALUES (?, ?, ?, ?, ?, 'yahoo')
                """,
                (company["isin"], day.date().isoformat(), float(row["Close"]), float(row["Volume"]), currency),
            )
            n += 1
    return n
