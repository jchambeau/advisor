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
        day = (date(2026, 8, 22) + timedelta(days=i)).isoformat()  # up to 2026-10-23
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


def test_missing_config_is_an_error(tmp_path):
    import pytest

    with pytest.raises(SystemExit):
        main(["--config", str(tmp_path / "nope.yaml"), "status"])


def test_inclusion_added_after_import_is_tracked(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("paths:\n  database: data/test.db\n", encoding="utf-8")
    config = str(tmp_path / "config.yaml")
    file = write_list(tmp_path / "list.xlsx", [
        ("2CRSI", "FR0013341781", "Euronext Growth Paris", "Euronext Growth", "France"),
    ])
    assert main(["--config", config, "import-list", str(file), "--yes"]) == 0
    (tmp_path / "overrides.yaml").write_text("include:\n  - isin: FR0000120271\n    name: LATE ADD\n", encoding="utf-8")
    assert main(["--config", config, "liquidity", "--date", "2026-10-24"]) == 0
    assert "for 2 companies" in capsys.readouterr().out


def test_fetch_volumes_refuses_another_date(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("paths:\n  database: data/test.db\n", encoding="utf-8")
    assert main(["--config", str(tmp_path / "config.yaml"), "fetch-volumes", "--date", "2026-10-21"]) == 1
    assert "nothing saved" in capsys.readouterr().out


def test_older_list_is_refused_without_force(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("paths:\n  database: data/test.db\n", encoding="utf-8")
    config = str(tmp_path / "config.yaml")
    row = ("2CRSI", "FR0013341781", "Euronext Growth Paris", "Euronext Growth", "France")
    new = write_list(tmp_path / "new.xlsx", [row], date_text="01/10/2026")
    old = write_list(tmp_path / "old.xlsx", [], date_text="01/09/2026")
    assert main(["--config", config, "import-list", str(new), "--yes"]) == 0
    assert main(["--config", config, "import-list", str(old), "--yes"]) == 1
    assert "older than the latest" in capsys.readouterr().out


def test_empty_config_section_keeps_defaults(tmp_path):
    from advisor.config import load

    (tmp_path / "config.yaml").write_text("liquidity:\n#  window_sessions: 40\npaths:\n", encoding="utf-8")
    cfg = load(tmp_path / "config.yaml")
    assert cfg.liquidity.window_sessions == 63 and cfg.database.name == "advisor.db"


def test_daily_still_computes_liquidity_when_fetch_fails(tmp_path, capsys, monkeypatch):
    from datetime import date as real_date

    from advisor import boursorama, cli

    (tmp_path / "config.yaml").write_text("paths:\n  database: data/test.db\n", encoding="utf-8")
    config = str(tmp_path / "config.yaml")
    file = write_list(tmp_path / "list.xlsx", [
        ("2CRSI", "FR0013341781", "Euronext Growth Paris", "Euronext Growth", "France"),
    ])
    assert main(["--config", config, "import-list", str(file), "--yes"]) == 0

    def boom(_config):
        raise RuntimeError("network down")

    class Monday(real_date):
        @classmethod
        def today(cls):
            return real_date(2026, 10, 26)

    monkeypatch.setattr(boursorama, "fetch", boom)
    monkeypatch.setattr(cli, "date", Monday)
    assert main(["--config", config, "daily"]) == 1
    out = capsys.readouterr().out
    assert "Volume fetch failed" in out and "Liquidity on 2026-10-26" in out
