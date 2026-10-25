"""SQLite database: schema and connection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS company (
    isin         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    market       TEXT,
    segment      TEXT,
    country      TEXT,
    listed       INTEGER NOT NULL DEFAULT 1,  -- present in the latest imported Euronext list
    added_on     TEXT,                        -- date of the list where it first appeared
    removed_on   TEXT,                        -- date of the list where it disappeared
    list_date    TEXT                         -- date of the latest list that contained it
);

CREATE TABLE IF NOT EXISTS list_import (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file          TEXT NOT NULL,
    list_date     TEXT,
    imported_at   TEXT NOT NULL,
    company_count INTEGER NOT NULL,
    added_count   INTEGER NOT NULL,
    removed_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS mapping (
    isin            TEXT PRIMARY KEY,
    boursorama_code TEXT,
    yahoo_ticker    TEXT,
    source          TEXT NOT NULL DEFAULT 'auto'   -- 'auto' (name), 'page' (ISIN on the stock's page) or 'manual'
);

CREATE TABLE IF NOT EXISTS isin_lookup (
    boursorama_code TEXT PRIMARY KEY,
    isin            TEXT,          -- ISIN of the universe shown on the stock's page, NULL if none
    checked_on      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_volume (
    isin      TEXT NOT NULL,
    date      TEXT NOT NULL,
    close     REAL,
    volume    REAL,          -- number of shares traded
    currency  TEXT NOT NULL DEFAULT 'EUR',
    source    TEXT NOT NULL, -- 'boursorama' or 'yahoo'
    PRIMARY KEY (isin, date, source)
);

CREATE TABLE IF NOT EXISTS liquidity (
    isin                  TEXT NOT NULL,
    computed_on           TEXT NOT NULL,
    session_count         INTEGER NOT NULL,
    median_turnover       REAL,     -- EUR per day
    active_session_ratio  REAL,
    status                TEXT NOT NULL,  -- liquid, illiquid, unknown
    PRIMARY KEY (isin, computed_on)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
