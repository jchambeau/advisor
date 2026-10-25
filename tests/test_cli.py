from datetime import date, timedelta

from advisor.cli import main
from advisor.db import connect

from .conftest import write_list


def test_full_run(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("paths:\n  database: data/test.db\n", encoding="utf-8")
    (tmp_path / "overrides.yaml").write_text(
        "exclude:\n  - isin: IT0004171440\n    reason: test\n"
        "include:\n  - isin: FR0000120271\n    name: MANUAL ADD\n",
        encoding="utf-8",
    )
    file = write_list(tmp_path / "list.xlsx", [
        ("2CRSI", "FR0013341781", "Euronext Growth Paris", "Euronext Growth", "France"),
        ("ZIGNAGO VETRO", "IT0004171440", "Euronext Milan", "Compartment A", "Italy"),
    ])
    config = str(tmp_path / "config.yaml")

    assert main(["--config", config, "import-list", str(file), "--yes"]) == 0

    conn = connect(tmp_path / "data" / "test.db")
    for i in range(63):
        day = (date(2026, 7, 1) + timedelta(days=i)).isoformat()
        conn.execute("INSERT INTO daily_volume VALUES ('FR0013341781', ?, 20.0, 1500, 'EUR', 'yahoo')", (day,))
    conn.commit()
    conn.close()

    assert main(["--config", config, "liquidity", "--date", "2026-10-24"]) == 0
    out = capsys.readouterr().out
    assert "for 2 companies" in out  # 2CRSI + manual inclusion, Zignago excluded
    assert "liquid 1" in out and "unknown 1" in out
    export = (tmp_path / "data" / "exports" / "liquidity_2026-10-24.csv").read_text(encoding="utf-8")
    assert "FR0013341781;2CRSI;Euronext Growth Paris;liquid;30000" in export

    assert main(["--config", config, "status"]) == 0
    assert "Tracked universe: 2 companies" in capsys.readouterr().out
