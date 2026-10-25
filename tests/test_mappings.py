from advisor import mappings as m


def _companies(conn, rows):
    conn.executemany("INSERT INTO company (isin, name) VALUES (?, ?)", rows)
    return conn.execute("SELECT * FROM company").fetchall()


def test_normalize():
    assert m.normalize("Lectra SA") == m.normalize("LECTRA")
    assert m.normalize("Société Générale") == "SOCIETEGENERALE"
    assert m.normalize("Bénéteau S.A.") == "BENETEAU"
    assert m.normalize("SA") == "SA"  # a name made only of a suffix stays as is


def test_yahoo_ticker():
    assert m.yahoo_ticker_from_boursorama("1rPAL2SI") == "AL2SI.PA"
    assert m.yahoo_ticker_from_boursorama("1zXYZ") is None


def test_match_and_ambiguity(conn):
    companies = _companies(conn, [
        ("FR0013341781", "2CRSI"),
        ("FR0011040500", "ABC"),
        ("IT0004171440", "ABC SPA"),  # same normalized name: ambiguous
    ])
    matched, unmatched = m.match(companies, {"1rPAL2SI": "2CRSI SA", "1rPABC": "ABC", "1rPX": "Unknown"})
    assert matched == {"1rPAL2SI": "FR0013341781"}
    assert sorted(unmatched) == [("1rPABC", "ABC"), ("1rPX", "Unknown")]


def test_manual_wins(conn, tmp_path):
    file = tmp_path / "mappings.csv"
    file.write_text("isin;boursorama_code;yahoo_ticker\nfr0013341781;1rPMANUAL;MAN.PA\n", encoding="utf-8")
    m.save(conn, {}, m.load_manual(file))
    m.save(conn, {"1rPAL2SI": "FR0013341781"}, [])  # a later automatic match does not overwrite it
    r = conn.execute("SELECT * FROM mapping").fetchone()
    assert (r["boursorama_code"], r["yahoo_ticker"], r["source"]) == ("1rPMANUAL", "MAN.PA", "manual")
    assert m.isin_by_code(conn) == {"1rPMANUAL": "FR0013341781"}


def test_manual_code_takes_over_a_wrong_automatic_match(conn, tmp_path):
    m.save(conn, {"1rPX": "FR0013341781"}, [])  # wrong automatic match
    m.save(conn, {"1rPX": "FR0013341781"}, [{"isin": "FR0011040500", "boursorama_code": "1rPX"}])
    assert m.isin_by_code(conn) == {"1rPX": "FR0011040500"}


def test_manual_ticker_only_keeps_automatic_code(conn):
    m.save(conn, {}, [{"isin": "FR0013341781", "boursorama_code": "", "yahoo_ticker": "AL2SI.PA"}])
    m.save(conn, {"1rPAL2SI": "FR0013341781"}, [])
    assert m.isin_by_code(conn) == {"1rPAL2SI": "FR0013341781"}
    r = conn.execute("SELECT * FROM mapping").fetchone()
    assert r["yahoo_ticker"] == "AL2SI.PA" and r["source"] == "manual"


def test_load_manual_with_bom(tmp_path):
    file = tmp_path / "mappings.csv"
    file.write_text("\ufeffisin;boursorama_code;yahoo_ticker\nFR0013341781;1rPAL2SI;\n", encoding="utf-8")
    assert m.load_manual(file)[0]["isin"] == "FR0013341781"


def test_load_manual_rejects_bad_header(tmp_path):
    import pytest

    file = tmp_path / "mappings.csv"
    file.write_text("code,isin\n", encoding="utf-8")
    with pytest.raises(ValueError):
        m.load_manual(file)


def test_two_codes_for_one_isin_are_ambiguous(conn):
    companies = _companies(conn, [("FR0013341781", "ATOS")])
    matched, unmatched = m.match(companies, {"1rPA": "ATOS", "1rPB": "Atos SE"})
    assert matched == {} and sorted(c for c, _ in unmatched) == ["1rPA", "1rPB"]


def test_auto_code_moving_to_another_isin(conn):
    m.save(conn, {"1rPX": "FR0013341781"}, [])
    m.save(conn, {"1rPX": "FR0011040500"}, [])
    rows = conn.execute("SELECT isin FROM mapping WHERE boursorama_code = '1rPX'").fetchall()
    assert [r["isin"] for r in rows] == ["FR0011040500"]


def test_load_manual_with_extra_columns(tmp_path):
    file = tmp_path / "mappings.csv"
    file.write_text("isin;boursorama_code;yahoo_ticker\nFR0013341781;1rPAL2SI;;note\n", encoding="utf-8")
    assert m.load_manual(file) == [{"isin": "FR0013341781", "boursorama_code": "1rPAL2SI", "yahoo_ticker": ""}]


def test_secondary_listing_keeps_the_company_venue(conn):
    conn.execute("INSERT INTO company (isin, name, market) VALUES ('FR0012821890', 'ADUX', 'Euronext Growth Paris')")
    companies = conn.execute("SELECT * FROM company").fetchall()
    matched, unmatched = m.match(companies, {"1rPALDUX": "ADUX", "1rAADUX": "ADUX"})
    assert matched == {"1rPALDUX": "FR0012821890"} and unmatched == []


def test_several_codes_off_venue_stay_unmatched(conn):
    conn.execute("INSERT INTO company (isin, name, market) VALUES ('FR0012821890', 'ADUX', 'Euronext Growth Paris')")
    companies = conn.execute("SELECT * FROM company").fetchall()
    matched, unmatched = m.match(companies, {"1rAADUX": "ADUX", "FF11-ADUX": "ADUX"})
    assert matched == {} and len(unmatched) == 2


def test_page_lookup_matches_and_wins_over_name(conn):
    from datetime import date

    conn.execute("INSERT INTO company (isin, name, market) VALUES ('FR0012821890', 'ADUX', 'Euronext Growth Paris')")
    companies = conn.execute("SELECT * FROM company").fetchall()
    today = date(2026, 10, 26)
    assert m.codes_to_look_up(conn, ["1rPALDUX", "1rPX"], today, 30) == ["1rPALDUX", "1rPX"]
    m.record_lookup(conn, "1rPALDUX", "FR0012821890", today)
    m.record_lookup(conn, "1rPX", None, today)
    m.save_page_matches(conn, m.matches_from_lookups(conn, companies, ["1rPALDUX", "1rPX"]))
    assert m.isin_by_code(conn) == {"1rPALDUX": "FR0012821890"}
    m.save(conn, {"1rPOTHER": "FR0012821890"}, [])  # a later name match does not replace it
    assert m.isin_by_code(conn) == {"1rPALDUX": "FR0012821890"}
    # Pages are read again only after the retry delay (mapped codes are never passed in).
    assert m.codes_to_look_up(conn, ["1rPALDUX", "1rPX"], date(2026, 11, 1), 30) == []
    assert m.codes_to_look_up(conn, ["1rPALDUX", "1rPX"], date(2026, 12, 1), 30) == ["1rPALDUX", "1rPX"]


def test_manual_line_wins_over_page_match(conn):
    m.save_page_matches(conn, {"1rPX": "FR0013341781"})
    m.save(conn, {}, [{"isin": "FR0011040500", "boursorama_code": "1rPX"}])
    assert m.isin_by_code(conn) == {"1rPX": "FR0011040500"}


def test_lone_off_venue_lookup_waits_for_the_venue_line(conn):
    from datetime import date

    conn.execute("INSERT INTO company (isin, name, market) VALUES ('FR0012821890', 'ADUX', 'Euronext Growth Paris')")
    companies = conn.execute("SELECT * FROM company").fetchall()
    m.record_lookup(conn, "1rAADUX", "FR0012821890", date(2026, 10, 26))
    assert m.matches_from_lookups(conn, companies, ["1rAADUX"]) == {}
    m.record_lookup(conn, "1rPALDUX", "FR0012821890", date(2026, 10, 27))
    assert m.matches_from_lookups(conn, companies, ["1rAADUX", "1rPALDUX"]) == {"1rPALDUX": "FR0012821890"}


def test_name_match_on_a_page_row_that_lost_its_code_is_auto_again(conn):
    m.save_page_matches(conn, {"1rPX": "FR0013341781"})
    m.save(conn, {}, [{"isin": "FR0011040500", "boursorama_code": "1rPX"}])  # manual line takes the code
    m.save(conn, {"1rPY": "FR0013341781"}, [])
    r = conn.execute("SELECT * FROM mapping WHERE isin = 'FR0013341781'").fetchone()
    assert (r["boursorama_code"], r["source"]) == ("1rPY", "auto")


def test_export_explains_each_unmatched_stock(conn, tmp_path):
    from datetime import date

    conn.execute("INSERT INTO company (isin, name, market) VALUES ('FR0012821890', 'ADUX', 'Euronext Growth Paris')")
    conn.execute("INSERT INTO company (isin, name, market) VALUES ('FR0013341781', '2CRSI', 'Euronext Growth Paris')")
    m.save(conn, {"1rPAL2SI": "FR0013341781"}, [])
    day = date(2026, 10, 26)
    m.record_lookup(conn, "1rPAC", "FR0000120404", day)  # Accor: not in the PEA PME list
    m.record_lookup(conn, "1rPNONE", None, day)
    m.record_lookup(conn, "1rAADUX", "FR0012821890", day)
    m.record_lookup(conn, "1rAAL2SI", "FR0013341781", day)  # second venue of a matched company
    unmatched = [("1rPAC", "ACCOR"), ("1rPNONE", "NONE"), ("1rAADUX", "ADUX"), ("1rPNEW", "NEW"), ("1rAAL2SI", "2CRSI")]
    path, nothing_to_do = m.export_unmatched(conn, tmp_path / "u.csv", unmatched, {"FR0012821890", "FR0013341781"})
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "boursorama_code;boursorama_name;isin_on_page;note"
    assert [line.split(";")[0] for line in lines[1:]] == ["1rAADUX", "1rPNONE", "1rPNEW", "1rPAC", "1rAAL2SI"]
    assert lines[4] == "1rPAC;ACCOR;FR0000120404;not in the Euronext list: nothing to do"
    assert nothing_to_do == 2


def test_old_lookup_table_is_migrated(tmp_path):
    import sqlite3

    from advisor.db import connect

    db = tmp_path / "a.db"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE isin_lookup (boursorama_code TEXT PRIMARY KEY, isin TEXT, checked_on TEXT NOT NULL)")
    old.execute("INSERT INTO isin_lookup VALUES ('1rPALDUX', 'FR0012821890', '2026-10-25'), ('1rPAC', NULL, '2026-10-25')")
    old.commit()
    old.close()
    conn = connect(db)
    assert [tuple(r) for r in conn.execute("SELECT * FROM stock_page_isin")] == [("1rPALDUX", "FR0012821890", "2026-10-25")]
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'isin_lookup'").fetchone()
