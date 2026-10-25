import io

from advisor.progress import Progress


class Tty(io.StringIO):
    def isatty(self):
        return True


def test_terminal_rewrites_one_line():
    out = Tty()
    p = Progress("Pages", total=4, stream=out)
    for i in range(1, 5):
        p.update(i, f"{i * 25} stocks")
    p.done("4 pages")
    text = out.getvalue()
    assert text.count("\r") == 4 and text.count("\x1b[K") == 4
    assert "[####################] 4/4 - 100 stocks" in text
    assert "Pages: 4 pages in " in text


def test_terminal_line_is_cut_to_width(monkeypatch):
    import os
    import shutil

    monkeypatch.setattr(shutil, "get_terminal_size", lambda *a, **k: os.terminal_size((40, 24)))
    out = Tty()
    Progress("Yahoo history", total=480, stream=out).update(1, "Compagnie Generale des Etablissements Michelin")
    line = out.getvalue().split("\r")[1].replace("\x1b[K", "")
    assert len(line) == 39


def test_log_output_is_throttled():
    out = io.StringIO()
    p = Progress("Yahoo history", total=120, every=50, stream=out)
    for i in range(1, 121):
        p.update(i)
    lines = out.getvalue().splitlines()
    assert [line.split("]")[1].split()[0] for line in lines] == ["1/120", "50/120", "100/120", "120/120"]
    assert "\r" not in out.getvalue()


def test_no_eta_on_first_update_and_estimate_from_first_step(monkeypatch):
    import advisor.progress as progress

    clock = iter([0.0, 100.0, 101.0])  # start, then 100 s of startup before the first update, 1 s per step
    monkeypatch.setattr(progress.time, "monotonic", lambda: next(clock))
    out = io.StringIO()
    p = Progress("History", total=11, every=1, stream=out)
    p.update(1)
    p.update(2)
    first, second = out.getvalue().splitlines()
    assert "left" not in first and second.endswith("~9s left")


def test_exceeded_total_falls_back_to_a_counter():
    out = io.StringIO()
    p = Progress("Pages", total=5, every=1, stream=out)
    p.update(6, "150 stocks read")
    assert out.getvalue() == "Pages 6 - 150 stocks read\n"


def test_unknown_total_shows_a_counter():
    out = io.StringIO()
    p = Progress("Pages", every=1, stream=out)
    p.update(3, "75 stocks read")
    assert out.getvalue() == "Pages 3 - 75 stocks read\n"
