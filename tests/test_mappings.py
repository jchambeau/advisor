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
