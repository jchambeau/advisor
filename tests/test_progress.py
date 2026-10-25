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
    p.done("finished")
    text = out.getvalue()
    assert text.count("\r") == 4 and "[####################] 4/4 - 100 stocks" in text
    assert text.rstrip().endswith("s)") and "Pages finished" in text


def test_log_output_is_throttled():
    out = io.StringIO()
    p = Progress("Yahoo history", total=120, every=50, stream=out)
    for i in range(1, 121):
        p.update(i)
    lines = out.getvalue().splitlines()
    assert [line.split()[-3 if "left" in line else -1] for line in lines] == ["1/120", "50/120", "100/120", "120/120"]
    assert "\r" not in out.getvalue()


def test_unknown_total_shows_a_counter():
    out = io.StringIO()
    p = Progress("Pages", every=1, stream=out)
    p.update(3, "75 stocks read")
    assert out.getvalue() == "Pages 3 - 75 stocks read\n"
