from datetime import date

import httpx
import pytest

from advisor import collect, documents, sources, vault
from advisor.cli import main

from .conftest import write_list

TODAY = date(2026, 10, 26)
LONG = "Le chiffre d'affaires progresse de 12 % sur le semestre. " * 30
ARTICLE = f"""<html><head><meta property="article:published_time" content="2026-10-24T08:00:00+02:00"></head>
<body><article><h1>AdUX : résultats</h1><p>{LONG}</p></article></body></html>"""
PAYWALLED = """<html><body><article><h1>Les petites valeurs</h1>
<p>""" + "Début de l'article seulement. " * 30 + """Cet article est réservé aux abonnés.</p></article></body></html>"""
FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>2CRSI signe un contrat</title><link>https://news.test/a?utm_source=rss</link>
<description>&lt;p&gt;Résumé&lt;/p&gt;</description><pubDate>Sat, 25 Oct 2026 10:00:00 +0200</pubDate></item>
<item><title>Vieille nouvelle</title><link>https://news.test/old</link><pubDate>Mon, 01 Jun 2026 10:00:00 +0200</pubDate></item>
<item><title>Abonnés seulement</title><link>https://news.test/paid</link><description>Le résumé</description></item>
</channel></rss>""".encode()
NEWS_PAGE = """<html><body><ul>
<li><a href="/bourse/actualites/adux-resultats-d59cb73cfbbc301d63feea80e3e02e54">AdUX : Résultats du premier semestre 2026</a></li>
<li><a href="/bourse/actualites/adux-rapport-575ba82a83be78f50445be590cf58a64">AdUX - Rapport annuel</a></li>
<li><a href="/bourse/actualites/adux-old-9ff6ee5aa5b243ea805876ec891bb2ab">AdUX - Ancien</a></li>
<li><a href="/bourse/actualites/">Toutes les actualités</a></li>
</ul></body></html>"""


def _cfg(tmp_path, **collect_overrides):
    from advisor.config import load

    text = "paths:\n  database: data/test.db\n"
    if collect_overrides:
        text += "collect:\n" + "".join(f"  {k}: {v}\n" for k, v in collect_overrides.items())
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    return load(tmp_path / "config.yaml")


def _client(pages):
    def respond(request):
        key = str(request.url).split("?")[0]
        status, body, kind = pages.get(key, (404, "", "text/html"))
        content = body if isinstance(body, bytes) else body.encode()
        return httpx.Response(status, content=content, headers={"content-type": kind})
    return httpx.Client(transport=httpx.MockTransport(respond))


@pytest.fixture(autouse=True)
def no_pause(monkeypatch):
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)


def test_canonical_url_and_duplicates(conn):
    assert documents.canonical_url("HTTPS://News.test/a?utm_source=x&id=3#top") == "https://news.test/a?id=3"
    first = documents.add(conn, "https://news.test/a?utm_medium=rss", "feed", TODAY,
                          "Lectra relève ses objectifs annuels", published_on=date(2026, 10, 25))
    assert first
    assert documents.add(conn, "https://news.test/a", "feed", TODAY, "x") is None  # same URL
    # Same long title two days later on another site: the same story.
    assert documents.add(conn, "https://other.test/b", "feed", TODAY, "Lectra relève ses objectifs annuels !",
                         published_on=date(2026, 10, 27)) is None
    # A short generic title is not enough to call it a duplicate.
    documents.add(conn, "https://news.test/c", "feed", TODAY, "Résultats annuels")
    assert documents.add(conn, "https://news.test/d", "feed", TODAY, "Résultats annuels")


def test_parse_feed_and_news_page():
    items = sources.parse_feed(FEED)
    assert items[0].url == "https://news.test/a?utm_source=rss" and items[0].summary == "Résumé"
    assert items[0].published_on == date(2026, 10, 25) and items[2].published_on is None
    news = sources.parse_news_page(NEWS_PAGE, "https://www.boursorama.com/cours/actualites/1rPALDUX/")
    assert [i.url.rsplit("-", 1)[1] for i in news][:1] == ["d59cb73cfbbc301d63feea80e3e02e54"]
    assert len(news) == 3 and news[0].url.startswith("https://www.boursorama.com/bourse/actualites/")


def test_sources_file_is_validated(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("feeds:\n  - name: a\n    url: https://x.test/rss\n    on_paywall: maybe\n", encoding="utf-8")
    with pytest.raises(ValueError, match="on_paywall"):
        sources.load(path)
    path.write_text("feeds:\n  - name: manual\n    url: https://x.test/rss\n", encoding="utf-8")
    with pytest.raises(ValueError, match="reserved"):
        sources.load(path)
    path.write_text("feeds:\n  - name: a\n    url: file:///etc/passwd\n", encoding="utf-8")
    with pytest.raises(ValueError, match="http"):
        sources.load(path)


def test_name_index(conn):
    conn.executemany("INSERT INTO company (isin, name) VALUES (?, ?)", [
        ("FR0013341781", "2CRSI"), ("FR0000065484", "LECTRA"), ("FR0011040500", "ABC ARBITRAGE"),
        ("FR0000000001", "ABC"),  # too short to look for
    ])
    index = documents.NameIndex(conn.execute("SELECT * FROM company").fetchall())
    assert index.find("2CRSI et Lectra SA progressent ; ABC Arbitrage aussi.") == {
        "FR0013341781", "FR0000065484", "FR0011040500"}
    assert index.find("une lectra de la bourse, abc") == set()


def test_inbox_and_pending_notes(tmp_path):
    root = tmp_path / "vault"
    (root / "Inbox").mkdir(parents=True)
    (root / "Inbox" / "clip.md").write_text(
        f"---\ntitle: Clip\nsource: https://news.test/clip\npublished: 2026-09-01\n---\n{LONG}", encoding="utf-8")
    (root / "Inbox" / "bare.md").write_text("https://news.test/bare\n", encoding="utf-8")
    notes = {n.path.name: n for n in vault.inbox_notes(root)}
    assert notes["clip.md"].url == "https://news.test/clip" and notes["clip.md"].published == "2026-09-01"
    assert len(notes["clip.md"].text) > 1000
    assert notes["bare.md"].url == "https://news.test/bare" and notes["bare.md"].text == ""

    doc = {"id": 7, "url": "https://news.test/paid", "source": "feed", "published_on": None,
           "title": "Les petites valeurs: [analyse]", "summary": "Résumé", "note": "text too short"}
    path = vault.write_pending(root, doc)
    assert path.name == "7-les-petites-valeurs-analyse.md"
    assert vault.pending_notes(root)[0].text == "" and not vault.pending_notes(root)[0].withdraw
    path.write_text(path.read_text(encoding="utf-8") + "\nLe texte complet.\n", encoding="utf-8")
    assert vault.pending_notes(root)[0].text == "Le texte complet."
    path.write_text(path.read_text(encoding="utf-8").replace("action: ''", "action: retirer"), encoding="utf-8")
    assert vault.pending_notes(root)[0].withdraw


def test_paywall_reason():
    from advisor.articles import paywall_reason

    markers = ["déjà abonné"]
    assert paywall_reason("x" * 100, 800, markers).startswith("text too short")
    assert "marker" in paywall_reason("x" * 1000 + " Déjà abonné ?", 800, markers)
    assert paywall_reason("x" * 6000 + " Déjà abonné ?", 800, markers) is None  # long: page leftover


def test_digest_keeps_your_own_text(tmp_path):
    root = tmp_path / "vault"
    path = vault.write_digest(root, TODAY, ["first"])
    path.write_text(path.read_text(encoding="utf-8") + "\nMy own note.\n", encoding="utf-8")
    vault.write_digest(root, TODAY, ["second <!-- /auto --> title"])
    vault.write_digest(root, TODAY, ["third"])
    text = path.read_text(encoding="utf-8")
    assert "third" in text and "second" not in text and text.count("My own note.") == 1


def test_fetch_texts_routes_each_article(conn, tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "sources.yaml").write_text(
        "feeds:\n  - name: feed\n    url: https://news.test/rss\n"
        "  - name: summary-feed\n    url: https://news.test/rss2\n    on_paywall: summary\n", encoding="utf-8")
    ids = {
        "ok": documents.add(conn, "https://news.test/ok", "feed", TODAY, "Ok"),
        "paid": documents.add(conn, "https://news.test/paid", "feed", TODAY, "Paid article", "Le résumé"),
        "paid2": documents.add(conn, "https://news.test/paid2", "summary-feed", TODAY, "Paid two", "Le résumé"),
        "refused": documents.add(conn, "https://news.test/refused", "feed", TODAY, "Refused"),
        "down": documents.add(conn, "https://news.test/down", "feed", TODAY, "Down"),
        "old": documents.add(conn, "https://news.test/old", "boursorama", TODAY, "Old"),
    }
    old_article = ARTICLE.replace("2026-10-24", "2025-01-02")
    client = _client({
        "https://news.test/ok": (200, ARTICLE, "text/html; charset=utf-8"),
        "https://news.test/paid": (200, PAYWALLED, "text/html"),
        "https://news.test/paid2": (200, PAYWALLED, "text/html"),
        "https://news.test/refused": (403, "", "text/html"),
        "https://news.test/down": (500, "", "text/html"),
        "https://news.test/old": (200, old_article, "text/html"),
    })
    summary = collect.Summary()
    collect.fetch_texts(conn, cfg, client, TODAY, summary)
    status = {k: conn.execute("SELECT * FROM document WHERE id = ?", (v,)).fetchone() for k, v in ids.items()}
    assert status["ok"]["status"] == "ready" and status["ok"]["published_on"] == "2026-10-24"
    assert status["paid"]["status"] == "pending" and "réservé aux abonnés" in status["paid"]["note"]
    assert status["paid2"]["status"] == "ready" and status["paid2"]["text"] == "Le résumé"
    assert status["refused"]["status"] == "pending" and "403" in status["refused"]["note"]
    assert status["down"]["status"] == "fetch_failed" and status["down"]["attempts"] == 1
    assert status["old"]["status"] == "too_old"
    pending = sorted(p.name for p in (cfg.vault / "Inbox" / "To-complete").glob("*.md"))
    assert pending == [f"{ids['paid']}-paid-article.md", f"{ids['refused']}-refused.md"]


def test_news_pages_link_companies_and_limit_first_visit(conn, tmp_path):
    cfg = _cfg(tmp_path, boursorama_news="{enabled: true, url: 'https://b.test/cours/actualites/{code}/', "
                                         "pages_per_run: 10, first_visit_items: 2, liquid_only: true}")
    conn.execute("INSERT INTO mapping (isin, boursorama_code, source) VALUES ('FR0012821890', '1rPALDUX', 'auto')")
    conn.execute("INSERT INTO mapping (isin, boursorama_code, source) VALUES ('FR0000000002', '1rPOTHER', 'auto')")
    client = _client({"https://b.test/cours/actualites/1rPALDUX/": (200, NEWS_PAGE, "text/html")})
    summary = collect.Summary()
    collect.collect_news_pages(conn, cfg, client, {"FR0012821890"}, TODAY, summary)
    assert summary.added == 2 and summary.news_pages == 1
    links = conn.execute("SELECT isin, how FROM document_company").fetchall()
    assert [tuple(r) for r in links] == [("FR0012821890", "page")] * 2
    # Next visit: every item not seen yet is taken.
    summary = collect.Summary()
    collect.collect_news_pages(conn, cfg, client, {"FR0012821890"}, TODAY, summary)
    assert summary.added == 1


def test_collect_command_end_to_end(tmp_path, monkeypatch, capsys):
    from advisor import cli

    (tmp_path / "config.yaml").write_text("paths:\n  database: data/test.db\n", encoding="utf-8")
    (tmp_path / "sources.yaml").write_text("feeds:\n  - name: feed\n    url: https://news.test/rss\n", encoding="utf-8")
    config = str(tmp_path / "config.yaml")
    file = write_list(tmp_path / "list.xlsx", [
        ("2CRSI", "FR0013341781", "Euronext Growth Paris", "Euronext Growth", "France"),
    ])
    assert main(["--config", config, "import-list", str(file), "--yes"]) == 0
    inbox = tmp_path / "vault" / "Inbox"
    inbox.mkdir(parents=True)
    (inbox / "mine.md").write_text(f"---\nurl: https://mine.test/x\n---\n2CRSI en forme. {LONG}", encoding="utf-8")

    class Today(date):
        @classmethod
        def today(cls):
            return TODAY

    pages = {
        "https://news.test/rss": (200, FEED, "application/rss+xml"),
        "https://news.test/a": (200, ARTICLE, "text/html"),
        "https://news.test/paid": (200, PAYWALLED, "text/html"),
    }
    monkeypatch.setattr(cli, "date", Today)
    monkeypatch.setattr(collect, "client_for", lambda cfg: _client(pages))
    assert main(["--config", config, "collect"]) == 0
    out = capsys.readouterr().out
    assert "Inbox: 1 added" in out and "2 new articles; 1 ready for analysis, 1 waiting for their text" in out
    assert "no liquid company yet" in out
    assert (inbox / "Done" / "mine.md").exists() and not (inbox / "mine.md").exists()
    digest = (tmp_path / "vault" / "Journal" / "2026-10-26.md").read_text(encoding="utf-8")
    assert "## Waiting for you (1)" in digest and "## Ready for analysis today (2)" in digest and "[[Inbox/To-complete/" in digest
    assert "**2CRSI**: [2CRSI signe un contrat](https://news.test/a?utm_source=rss)" not in digest
    assert "**2CRSI**: [2CRSI signe un contrat](https://news.test/a)" in digest

    # You paste the text of the paywalled article: it becomes ready on the next run.
    note = next((tmp_path / "vault" / "Inbox" / "To-complete").glob("*.md"))
    note.write_text(note.read_text(encoding="utf-8") + "\nTout le texte de l'article.\n", encoding="utf-8")
    assert main(["--config", config, "collect"]) == 0
    assert "1 completed" in capsys.readouterr().out
    assert main(["--config", config, "status"]) == 0
    assert "Documents: ready 3" in capsys.readouterr().out


def test_add_command(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text("paths:\n  database: data/test.db\n", encoding="utf-8")
    config = str(tmp_path / "config.yaml")
    urls = tmp_path / "urls.txt"
    urls.write_text("# history\nhttps://a.test/1 2025-03-04\nnot-a-url\nhttps://a.test/2 2025-13-01\n", encoding="utf-8")
    assert main(["--config", config, "add", "https://a.test/0", "--file", str(urls)]) == 0
    out = capsys.readouterr().out
    assert "2 article(s) added" in out and "not a URL" in out and "bad date" in out
    assert main(["--config", config, "add", "https://a.test/0"]) == 0
    assert "already in the database" in capsys.readouterr().out


def test_bad_urls_never_crash_a_run(conn, tmp_path):
    cfg = _cfg(tmp_path)
    assert not documents.valid_url("https://news.test:abc/a") and not documents.valid_url("http://[x")
    assert documents.valid_url("https://news.test/a")
    doc_id = documents.add(conn, "https://news.test:99999/a", "manual", TODAY, "Bad port")
    collect.fetch_texts(conn, cfg, _client({}), TODAY, collect.Summary())
    assert conn.execute("SELECT status FROM document WHERE id = ?", (doc_id,)).fetchone()["status"] == "fetch_failed"


def test_news_page_links_a_story_known_under_another_url(conn, tmp_path):
    cfg = _cfg(tmp_path, boursorama_news="{enabled: true, url: 'https://b.test/{code}/', pages_per_run: 5, "
                                         "first_visit_items: 1, liquid_only: true}")
    known = documents.add(conn, "https://feed.test/x", "feed", TODAY, "AdUX : Résultats du premier semestre 2026")
    conn.execute("INSERT INTO mapping (isin, boursorama_code, source) VALUES ('FR0012821890', '1rPALDUX', 'auto')")
    client = _client({"https://b.test/1rPALDUX/": (200, NEWS_PAGE, "text/html")})
    collect.collect_news_pages(conn, cfg, client, {"FR0012821890"}, TODAY, collect.Summary())
    assert conn.execute("SELECT document_id FROM document_company").fetchone()["document_id"] == known


def test_inbox_text_completes_an_article_not_fetched_yet(tmp_path):
    from advisor.db import connect

    cfg = _cfg(tmp_path)
    conn = connect(cfg.database)
    doc_id = documents.add(conn, "https://news.test/paid", "feed", TODAY, "Paid")
    (cfg.vault / "Inbox").mkdir(parents=True)
    (cfg.vault / "Inbox" / "clip.md").write_text(f"---\nsource: https://news.test/paid\n---\n{LONG}", encoding="utf-8")
    summary = collect.Summary()
    collect.take_inbox(conn, cfg, TODAY, summary)
    row = conn.execute("SELECT status, ready_on FROM document WHERE id = ?", (doc_id,)).fetchone()
    assert (row["status"], row["ready_on"], summary.completed) == ("ready", TODAY.isoformat(), 1)


def test_client_refuses_local_addresses():
    from advisor.articles import refuse_local

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<p>secret</p>")),
                          event_hooks={"request": [refuse_local]})
    for url in ("http://127.0.0.1/admin", "http://192.168.1.1/", "http://[::1]/", "http://localhost:8080/",
                "http://box.local/"):
        with pytest.raises(httpx.ConnectError):
            client.get(url)
    assert client.get("https://news.test/a").status_code == 200
