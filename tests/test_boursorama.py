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
    rows = boursorama.fetch(
        {"url": "https://example.test/cotations/", "market_filter": "PEAPME", "max_pages": 10, "pause_seconds": 0},
        client,
    )
    assert len(rows) == 3 and len(calls) == 2


def test_save(conn):
    rows = boursorama.parse(PAGE)
    n = boursorama.save(conn, rows, {"1rPAL2SI": "FR0013341781"}, date(2026, 10, 23))
    assert n == 1
    r = conn.execute("SELECT * FROM daily_volume").fetchone()
    assert (r["isin"], r["date"], r["volume"], r["source"]) == ("FR0013341781", "2026-10-23", 117988, "boursorama")
