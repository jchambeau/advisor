import os
from datetime import date
from pathlib import Path

import pytest

from advisor import euronext

CRSI = ("2CRSI", "FR0013341781", "Euronext Growth Paris", "Euronext Growth", "France")
SOFT = ("74SOFTWARE", "FR0011040500", "Euronext Paris", "Compartment B", "France")
ZIGNAGO = ("ZIGNAGO VETRO", "IT0004171440", "Euronext Milan", "Compartment A", "Italy")


def test_is_valid_isin():
    assert euronext.is_valid_isin("FR0013341781")
    assert euronext.is_valid_isin("IT0004171440")
    assert not euronext.is_valid_isin("FR0013341782")  # wrong check digit
    assert not euronext.is_valid_isin("FR001334178")


def test_read_euronext_layout(make_list):
    file = make_list("list.xlsx", [
        CRSI,
        ("  74SOFTWARE ", "FR0011040500", "Euronext Growth Paris ", "Compartment B", "France"),
        ("BAD", "FR0000000001", "Euronext Paris", "Compartment C", "France"),
        CRSI,
        (None, None, None, None, None),
    ])
    new_list = euronext.read(file)
    assert new_list.list_date == date(2026, 10, 23)
    assert [c.isin for c in new_list.companies] == ["FR0013341781", "FR0011040500"]
    assert new_list.companies[1].name == "74SOFTWARE"
    assert new_list.companies[1].market == "Euronext Growth Paris"  # stray spaces removed
    assert len(new_list.rejected_rows) == 2


def test_read_without_header(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    wb.active["A1"] = "nothing"
    wb.save(tmp_path / "x.xlsx")
    with pytest.raises(ValueError):
        euronext.read(tmp_path / "x.xlsx")


def test_added_removed_returned(conn, make_list):
    l1 = euronext.read(make_list("l1.xlsx", [CRSI, SOFT], date_text="01/09/2026"))
    c1 = euronext.compare(conn, l1)
    assert len(c1.added) == 2 and not c1.removed
    euronext.apply(conn, l1, c1)

    l2 = euronext.read(make_list("l2.xlsx", [CRSI, ZIGNAGO], date_text="01/10/2026"))
    c2 = euronext.compare(conn, l2)
    assert [c.isin for c in c2.added] == ["IT0004171440"]
    assert [r["isin"] for r in c2.removed] == ["FR0011040500"]
    assert c2.unchanged == 1
    euronext.apply(conn, l2, c2)
    removed = conn.execute("SELECT * FROM company WHERE isin = 'FR0011040500'").fetchone()
    assert removed["listed"] == 0 and removed["removed_on"] == "2026-10-01"

    l3 = euronext.read(make_list("l3.xlsx", [CRSI, SOFT, ZIGNAGO], date_text="01/11/2026"))
    c3 = euronext.compare(conn, l3)
    assert [c.isin for c in c3.returned] == ["FR0011040500"] and not c3.added
    euronext.apply(conn, l3, c3)
    back = conn.execute("SELECT * FROM company WHERE isin = 'FR0011040500'").fetchone()
    assert back["listed"] == 1 and back["removed_on"] is None
    assert back["added_on"] == "2026-09-01"  # first entry date is kept
    assert euronext.latest_list_date(conn) == "2026-11-01"


REAL_LIST = os.environ.get("ADVISOR_REAL_LIST")


@pytest.mark.skipif(not REAL_LIST, reason="ADVISOR_REAL_LIST not set")
def test_real_list():
    new_list = euronext.read(Path(REAL_LIST))
    assert new_list.list_date is not None
    assert len(new_list.companies) > 500
    assert not new_list.rejected_rows
