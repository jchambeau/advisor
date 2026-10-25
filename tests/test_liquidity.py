from datetime import date, timedelta

import pytest

from advisor.config import LiquidityRules
from advisor.liquidity import ILLIQUID, LIQUID, UNKNOWN, Session, compute, evaluate, stored_sessions

RULES = LiquidityRules(
    window_sessions=63, min_sessions=20, median_turnover_entry=20000,
    median_turnover_exit=15000, min_active_session_ratio=0.95,
)
CURRENCIES = {"EUR": 1.0, "NOK": 0.1}


def series(n, close, volume, currency="EUR", no_trade=0):
    start = date(2026, 6, 1)
    return [
        Session((start + timedelta(days=i)).isoformat(), close, 0 if i < no_trade else volume, currency)
        for i in range(n)
    ]


def test_liquid():
    r = evaluate(series(63, 10.0, 3000), RULES, CURRENCIES)  # EUR 30,000 a day
    assert r.status == LIQUID and r.median_turnover == 30000 and r.active_session_ratio == 1


def test_turnover_too_low():
    assert evaluate(series(63, 10.0, 1000), RULES, CURRENCIES).status == ILLIQUID


def test_too_many_sessions_without_trade():
    r = evaluate(series(63, 10.0, 5000, no_trade=10), RULES, CURRENCIES)
    assert r.active_session_ratio < 0.95 and r.status == ILLIQUID


def test_not_enough_sessions():
    assert evaluate(series(10, 10.0, 5000), RULES, CURRENCIES).status == UNKNOWN
    assert evaluate([], RULES, CURRENCIES).status == UNKNOWN


def test_hysteresis():
    s = series(63, 10.0, 1700)  # EUR 17,000: between the exit and entry thresholds
    assert evaluate(s, RULES, CURRENCIES, previous_status=None).status == ILLIQUID
    assert evaluate(s, RULES, CURRENCIES, previous_status=LIQUID).status == LIQUID


def test_window_keeps_latest_sessions():
    old = series(100, 10.0, 0)
    recent = [Session((date(2026, 12, 1) + timedelta(days=i)).isoformat(), 10.0, 3000) for i in range(63)]
    assert evaluate(old + recent, RULES, CURRENCIES).status == LIQUID


def test_currency_conversion():
    r = evaluate(series(63, 100.0, 2500, currency="NOK"), RULES, CURRENCIES)  # NOK 250,000 = EUR 25,000
    assert r.median_turnover == pytest.approx(25000) and r.status == LIQUID
    with pytest.raises(ValueError):
        evaluate(series(63, 1.0, 1, currency="USD"), RULES, CURRENCIES)


def test_boursorama_priority_and_stored_result(conn):
    conn.execute("INSERT INTO company (isin, name) VALUES ('FR0013341781', '2CRSI')")
    for source, volume in (("yahoo", 1), ("boursorama", 3000)):
        conn.execute(
            "INSERT INTO daily_volume VALUES ('FR0013341781', '2026-10-23', 10.0, ?, 'EUR', ?)",
            (volume, source),
        )
    (s,) = stored_sessions(conn, "FR0013341781")
    assert s.volume == 3000

    companies = conn.execute("SELECT * FROM company").fetchall()
    res = compute(conn, companies, RULES, CURRENCIES, date(2026, 10, 24))
    assert res["FR0013341781"].status == UNKNOWN
    row = conn.execute("SELECT * FROM liquidity").fetchone()
    assert row["computed_on"] == "2026-10-24" and row["session_count"] == 1
