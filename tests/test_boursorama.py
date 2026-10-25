from datetime import date

import httpx

from advisor import boursorama

# Built from the columns visible on the page; the site's exact HTML is still to be confirmed.
PAGE = """
<html><body>
<table class="c-table">
  <thead><tr>
    <th>Libellé</th><th>Dernier</th><th>Var.</th><th>Ouv</th><th>+Haut</th><th>+Bas</th><th>Var/1Janv</th><th>Vol.</th>
  </tr></thead>
  <tbody>
    <tr><td><a href="/cours/1rPAL2SI/" title="2CRSI">2CRSI</a> <span>SRD</span></td>
        <td>26,620 (c)</td><td>-2,42%</td><td>27,120</td><td>27,200</td><td>26,060</td><td>+132,29%</td><td>117 988</td></tr>
    <tr><td><a href="/cours/1rPSFT/">74SOFTWARE</a></td>
        <td>38,200 (c)</td><td>+1,33%</td><td>37,800</td><td>38,200</td><td>37,600</td><td>-7,95%</td><td>12595</td></tr>
    <tr><td><a href="/cours/1rPALXYZ/">NO TRADE</a></td>
        <td>1,000 (c)</td><td>0,00%</td><td>-</td><td>-</td><td>-</td><td>+0,00%</td><td>-</td></tr>
  </tbody>
</table>
</body></html>
"""


def test_parse_number():
    assert boursorama.parse_number("26,620 (c)") == 26.62
    assert boursorama.parse_number("1 234 567") == 1234567
    assert boursorama.parse_number("-") is None
    assert boursorama.parse_number("") is None


def test_parse():
    rows = boursorama.parse(PAGE)
    assert [(r.code, r.name) for r in rows] == [
        ("1rPAL2SI", "2CRSI"), ("1rPSFT", "74SOFTWARE"), ("1rPALXYZ", "NO TRADE"),
    ]
    assert rows[0].close == 26.62 and rows[0].volume == 117988
    assert rows[2].volume == 0


def test_parse_ignores_other_tables():
    assert boursorama.parse("<table><tr><th>Other</th></tr></table>") == []


def test_page_url():
    base = "https://www.boursorama.com/bourse/actions/cotations/"
    assert boursorama.page_url(base, "PEAPME", 1).endswith("/cotations/?quotation_az_filter%5Bmarket%5D=PEAPME")
    assert boursorama.page_url(base, "PEAPME", 3).endswith("/cotations/page-3?quotation_az_filter%5Bmarket%5D=PEAPME")


def test_fetch_stops_when_nothing_new():
    calls = []

    def respond(request):
        calls.append(str(request.url))
        # past the last page the site serves the same stocks again
        return httpx.Response(200, text=PAGE)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    rows, error = boursorama.fetch(
        {"url": "https://example.test/cotations/", "market_filter": "PEAPME", "max_pages": 10, "pause_seconds": 0},
        client,
    )
    assert len(rows) == 3 and len(calls) == 2 and error is None


def test_fetch_keeps_pages_read_before_an_error():
    def respond(request):
        if "page-2" in str(request.url):
            return httpx.Response(502)
        return httpx.Response(200, text=PAGE)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    rows, error = boursorama.fetch(
        {"url": "https://example.test/cotations/", "market_filter": "PEAPME", "max_pages": 10, "pause_seconds": 0},
        client,
    )
    assert len(rows) == 3 and "page 2" in error


def test_fetch_first_page_error_raises():
    import pytest

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(httpx.HTTPError):
        boursorama.fetch({"url": "https://example.test/", "market_filter": "PEAPME", "pause_seconds": 0}, client)


def test_save_with_market_currency(conn):
    rows = boursorama.parse(PAGE)
    codes = {"1rPAL2SI": "FR0013341781", "1rPSFT": "NO0010000000"}
    n = boursorama.save(conn, rows, codes, {"NO0010000000": "NOK"}, date(2026, 10, 23))
    assert n == 2
    saved = {r["isin"]: r for r in conn.execute("SELECT * FROM daily_volume")}
    r = saved["FR0013341781"]
    assert (r["date"], r["volume"], r["currency"], r["source"]) == ("2026-10-23", 117988, "EUR", "boursorama")
    assert saved["NO0010000000"]["currency"] == "NOK"


def test_repeated_share(conn):
    rows = boursorama.parse(PAGE)
    codes = {"1rPAL2SI": "FR0013341781", "1rPSFT": "FR0011040500"}
    assert boursorama.repeated_share(conn, rows, codes, date(2026, 10, 23)) is None
    boursorama.save(conn, rows, codes, {}, date(2026, 10, 23))
    # same page on the next day (a holiday): every stock is a repeat
    assert boursorama.repeated_share(conn, rows, codes, date(2026, 10, 24)) == 1.0
    rows[0].volume += 1
    assert boursorama.repeated_share(conn, rows, codes, date(2026, 10, 24)) == 0.5


def test_repeated_share_ignores_stocks_without_trade(conn):
    rows = boursorama.parse(PAGE)
    codes = {"1rPAL2SI": "FR0013341781", "1rPALXYZ": "FR0011040500"}  # second one has no trade
    boursorama.save(conn, rows, codes, {}, date(2026, 10, 23))
    rows[0].volume += 1
    assert boursorama.repeated_share(conn, rows, codes, date(2026, 10, 24)) == 0.0


def test_fetch_reports_progress_and_last_page():
    pagination = '<a href="/bourse/actions/cotations/page-2?quotation_az_filter%5Bmarket%5D=PEAPME">2</a>'
    page = PAGE.replace("</table>", "</table>" + pagination)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    seen = []
    boursorama.fetch(
        {"url": "https://example.test/cotations/", "market_filter": "PEAPME", "pause_seconds": 0},
        client,
        on_page=lambda *args: seen.append(args),
    )
    assert seen == [(1, 3, 2)]  # page 2 brought nothing new, so no callback for it


def test_last_page():
    html = (
        '<a href="/cotations/page-2?quotation_az_filter%5Bmarket%5D=PEAPME">2</a>'
        '<a href="/cotations/page-29?quotation_az_filter%5Bmarket%5D=PEAPME">29</a>'
        '<a href="/bourse/actualites/page-120">news</a>'  # other listings do not count
    )
    assert boursorama.last_page(html) == 29
    assert boursorama.last_page("<p>no pagination</p>") is None


def test_last_page_capped_at_max_pages():
    pagination = '<a href="/c/page-80?quotation_az_filter%5Bmarket%5D=PEAPME">80</a>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=PAGE + pagination)))
    seen = []
    boursorama.fetch(
        {"url": "https://example.test/c/", "market_filter": "PEAPME", "max_pages": 60, "pause_seconds": 0},
        client,
        on_page=lambda *args: seen.append(args),
    )
    assert seen == [(1, 3, 60)]


def test_page_isin_is_the_first_valid_one_in_the_text():
    html = """<script>var x = "FR0013341781";</script>
    <h2>FR0012821891 bad check digit</h2><h2>FR0012821890 ALDUX</h2>
    <div>Same sector: FR0013341781</div>"""
    assert boursorama.page_isin(html) == "FR0012821890"
    assert boursorama.page_isin("<p>no code</p>") is None


def test_stock_url_stays_on_the_site():
    base = "https://www.boursorama.com/bourse/actions/cotations/"
    assert boursorama.stock_url(base, "1rPALDUX") == "https://www.boursorama.com/cours/1rPALDUX/"
    assert boursorama.stock_url(base, "//evil.example").startswith("https://www.boursorama.com/cours/")


def _client(pages):
    def respond(request):
        status, text = pages.get(request.url.path, (503, ""))
        return httpx.Response(status, text=text)
    return httpx.Client(transport=httpx.MockTransport(respond))


CONFIG = {"url": "https://www.boursorama.com/bourse/actions/cotations/", "pause_seconds": 0}


def test_look_up_isins():
    client = _client({
        "/cours/1rPALDUX/": (200, "<h2>FR0012821890 ALDUX</h2>"),
        "/cours/1rPNONE/": (200, "<h2>no code</h2>"),
        "/cours/1rPGONE/": (404, ""),
    })
    found = list(boursorama.look_up_isins(CONFIG, ["1rPALDUX", "1rPNONE", "1rPDOWN", "1rPGONE"], client=client))
    # A failed page yields nothing (tried again later); a missing one is recorded as empty.
    assert found == [("1rPALDUX", "FR0012821890"), ("1rPNONE", None), ("1rPGONE", None)]


def test_look_up_isins_stops_after_failures_in_a_row():
    seen = []
    def respond(request):
        seen.append(request.url.path)
        return httpx.Response(429)
    client = httpx.Client(transport=httpx.MockTransport(respond))
    assert list(boursorama.look_up_isins(CONFIG, [f"1rP{i}" for i in range(10)], client=client)) == []
    assert len(seen) == 3
