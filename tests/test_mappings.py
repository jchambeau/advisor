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
