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

CREATE TABLE IF NOT EXISTS stock_page_isin (
    boursorama_code TEXT PRIMARY KEY,
    isin            TEXT,          -- ISIN shown on the stock's page, in the universe or not; NULL if none
    checked_on      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    url          TEXT NOT NULL UNIQUE,  -- canonical URL (no fragment, no tracking parameters)
    title_key    TEXT,                  -- normalized title, to spot one story under another URL
    source       TEXT NOT NULL,         -- feed name from sources.yaml, 'boursorama' or 'manual'
    title        TEXT,
    summary      TEXT,
    text         TEXT,
    published_on TEXT,                  -- publication date, places the document in the history
    added_on     TEXT NOT NULL,         -- date it entered the database
    ready_on     TEXT,                  -- date its text became available for analysis
    status       TEXT NOT NULL,         -- new, ready, pending, withdrawn, too_old, fetch_failed
    attempts     INTEGER NOT NULL DEFAULT 0,
    note         TEXT                   -- why pending or failed
);
CREATE INDEX IF NOT EXISTS document_title_key ON document (title_key);
CREATE INDEX IF NOT EXISTS document_status ON document (status);

CREATE TABLE IF NOT EXISTS document_company (
    document_id INTEGER NOT NULL REFERENCES document (id),
    isin        TEXT NOT NULL,
    how         TEXT NOT NULL,          -- 'page' (stock news page), 'name' (name found in the text)
    PRIMARY KEY (document_id, isin)
);

CREATE TABLE IF NOT EXISTS news_page_visit (
    boursorama_code TEXT PRIMARY KEY,
    visited_on      TEXT NOT NULL
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
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    # isin_lookup (first version) only kept ISINs of the universe: keep those, read the others again.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'isin_lookup'").fetchone():
        with conn:
            conn.execute("INSERT OR IGNORE INTO stock_page_isin SELECT * FROM isin_lookup WHERE isin IS NOT NULL")
            conn.execute("DROP TABLE isin_lookup")
