"""Price and volume history through yfinance, to seed the 3-month window.

Unofficial source with uneven coverage of small caps: it complements the Boursorama
snapshots, which take precedence for the same session.
"""

from __future__ import annotations

import sqlite3

from .liquidity import default_currency


def _yfinance():
    try:
        import yfinance
    except ImportError as exc:  # optional dependency
        raise SystemExit("yfinance is not installed: pip install -e '.[history]'") from exc
    return yfinance


def backfill(conn: sqlite3.Connection, companies: list[sqlite3.Row], period: str = "6mo") -> dict[str, int]:
    """Return, per ISIN, the number of sessions saved (0 when nothing was found)."""
    yf = _yfinance()
    tickers = {
        r["isin"]: r["yahoo_ticker"]
        for r in conn.execute("SELECT isin, yahoo_ticker FROM mapping WHERE yahoo_ticker IS NOT NULL")
    }
    summary = {}
    for c in companies:
        symbol = tickers.get(c["isin"], c["isin"])  # yfinance can sometimes resolve an ISIN
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period, interval="1d", auto_adjust=False)
        except Exception:
            summary[c["isin"]] = 0
            continue
        try:
            currency = ticker.fast_info["currency"] or default_currency(c["market"])
        except Exception:
            currency = default_currency(c["market"])
        n = 0
        with conn:
            for day, row in hist.iterrows():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO daily_volume (isin, date, close, volume, currency, source)
                    VALUES (?, ?, ?, ?, ?, 'yahoo')
                    """,
                    (c["isin"], day.date().isoformat(), float(row["Close"]), float(row["Volume"]), currency.upper()),
                )
                n += 1
        summary[c["isin"]] = n
    return summary
