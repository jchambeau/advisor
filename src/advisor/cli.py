"""Command line: advisor <command>."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

import anthropic

from . import (boursorama, collect, documents, euronext, history, liquidity, mappings, overrides, signals,
               sources, vault)
from .config import load
from .db import connect
from .progress import Progress


def _context(args):
    cfg = load(args.config)
    return cfg, connect(cfg.database)


def _day(text: str | None) -> date:
    return date.fromisoformat(text) if text else date.today()


def cmd_import_list(args) -> int:
    cfg, conn = _context(args)
    new_list = euronext.read(args.file)
    print(f"{Path(args.file).name}: list of {new_list.list_date or 'unknown date'}, "
          f"{len(new_list.companies)} companies")
    for line_no, reason in new_list.rejected_rows:
        print(f"  line {line_no} skipped: {reason}")

    latest = euronext.latest_list_date(conn)
    if latest and new_list.list_date and new_list.list_date.isoformat() < latest and not args.force:
        print(f"This list is older than the latest imported one ({latest}): importing it would flag "
              "newer companies as removed. Nothing changed (--force to import anyway).")
        return 1

    changes = euronext.compare(conn, new_list)
    print(f"Added: {len(changes.added)}  Returned: {len(changes.returned)}  "
          f"Removed: {len(changes.removed)}  Unchanged: {changes.unchanged}")
    limit = None if args.all else 15
    groups = (
        ("Added", "+", [(c.isin, c.name) for c in changes.added]),
        ("Returned", "+", [(c.isin, c.name) for c in changes.returned]),
        ("Removed", "-", [(r["isin"], r["name"]) for r in changes.removed]),
    )
    for title, sign, items in groups:
        if items:
            print(f"\n{title}:")
            for isin, name in items[:limit]:
                print(f"  {sign} {isin}  {name}")
            if limit and len(items) > limit:
                print(f"  ... and {len(items) - limit} more (--all to list everything)")

    if not args.yes:
        answer = input("\nApply these changes? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing was changed.")
            return 1
    euronext.apply(conn, new_list, changes)
    overrides.apply_inclusions(conn, overrides.load(cfg.overrides))
    print("List imported.")
    return 0


def cmd_fetch_volumes(args) -> int:
    cfg, conn = _context(args)
    day = _day(args.date)
    # The page only shows the latest session: saving it under another date would duplicate it.
    # Market closed (weekend, holiday) is a normal skip and returns 0; real problems return 1.
    if day.weekday() >= 5 and not args.force:
        print(f"{day} is a weekend day: nothing saved (--force to save anyway).")
        return 0
    if day != date.today() and not args.force:
        print(f"The page only shows the latest session, not {day}: nothing saved (--force to save anyway).")
        return 1
    companies = overrides.universe(conn, overrides.load(cfg.overrides))
    if not companies:
        print("Empty universe: import a list first (advisor import-list).")
        return 1

    progress = Progress("Boursorama pages", every=5)

    def on_page(page: int, stocks: int, total_pages: int | None) -> None:
        progress.total = total_pages
        progress.update(page, f"{stocks} stocks read")

    rows = []
    try:
        rows, error = boursorama.fetch(cfg.boursorama, on_page=on_page)
    finally:
        progress.done(f"{len(rows)} stocks read")
    print(f"Boursorama: {len(rows)} stocks read")
    if error:
        print(f"Warning: crawl stopped early ({error}); saving the pages already read.")
    if not rows:
        print("No stock read: the page layout may have changed. Nothing saved.")
        return 1

    matched, unmatched = mappings.match(companies, {r.code: r.name for r in rows})
    mappings.save(conn, matched, mappings.load_manual(cfg.mappings))
    codes = mappings.isin_by_code(conn)
    unmatched = [(c, n) for c, n in unmatched if c not in codes]
    repeated = boursorama.repeated_share(conn, rows, codes, day)
    if repeated is not None and repeated >= 0.8 and not args.force:
        print(f"{repeated:.0%} of stocks show the same close and volume as the previous snapshot: "
              "the market was probably closed. Nothing saved (--force to save anyway).")
        return 0
    currency_by_isin = {
        r["isin"]: liquidity.default_currency(r["market"]) for r in conn.execute("SELECT isin, market FROM company")
    }
    n = boursorama.save(conn, rows, codes, currency_by_isin, day)
    if unmatched:
        # After the session is saved, so a slow or failing lookup never costs the day's volumes.
        try:
            _look_up_on_pages(cfg, conn, companies, [c for c, _ in unmatched])
        except Exception as exc:
            print(f"Warning: ISIN lookup on stock pages failed: {exc!r}")
        codes = mappings.isin_by_code(conn)
        unmatched = [(c, nm) for c, nm in unmatched if c not in codes]
        n = boursorama.save(conn, rows, codes, currency_by_isin, day)
    print(f"Session of {day} saved for {n} companies")
    # Written even when empty, so an old list of stocks to fix never lingers.
    path, nothing_to_do = mappings.export_unmatched(
        conn, cfg.exports / "unmatched_boursorama.csv", unmatched, {c["isin"] for c in companies}
    )
    if unmatched:
        print(f"{len(unmatched)} stocks not matched, {nothing_to_do} of them outside the list or "
              f"secondary lines (nothing to do): see {path}; "
              f"complete {cfg.mappings.name} for the {len(unmatched) - nothing_to_do} others")
    return 0


def _look_up_on_pages(cfg, conn, companies, unmatched_codes: list[str]) -> None:
    """Read the ISIN on the own page of stocks the name match missed."""
    today = date.today()
    to_check = mappings.codes_to_look_up(conn, unmatched_codes, today, int(cfg.boursorama["isin_lookup_retry_days"]))
    to_check = to_check[: int(cfg.boursorama["max_isin_lookups"])]
    if to_check:
        known = {c["isin"] for c in companies}
        progress = Progress("ISIN lookups", total=len(to_check), every=20)
        read = found = 0
        try:
            for code, isin in boursorama.look_up_isins(cfg.boursorama, to_check):
                # Recorded one by one, so an interrupted run keeps the pages already read.
                mappings.record_lookup(conn, code, isin, today)
                read += 1
                found += isin in known
                progress.update(read, code)
        finally:
            progress.done(f"{found} of {read} pages read show an ISIN of the list")
    mappings.save_page_matches(conn, mappings.matches_from_lookups(conn, companies, unmatched_codes))


def cmd_backfill_history(args) -> int:
    cfg, conn = _context(args)
    mappings.save(conn, {}, mappings.load_manual(cfg.mappings))
    companies = overrides.universe(conn, overrides.load(cfg.overrides))
    if args.limit:
        companies = companies[: args.limit]
    progress = Progress("Yahoo history", total=len(companies))
    try:
        summary = history.backfill(conn, companies, args.period, on_company=progress.update)
    finally:
        progress.done(f"{len(companies)} companies processed")
    found = sum(1 for n in summary.values() if n)
    print(f"Yahoo history: {found}/{len(summary)} companies with data")
    return 0


def cmd_liquidity(args) -> int:
    cfg, conn = _context(args)
    day = _day(args.date)
    companies = overrides.universe(conn, overrides.load(cfg.overrides))
    results = liquidity.compute(conn, companies, cfg.liquidity, cfg.currencies, day)
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r.status] = counts.get(r.status, 0) + 1
    print(f"Liquidity on {day} for {len(companies)} companies: "
          + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    path = liquidity.export(cfg.exports / f"liquidity_{day.isoformat()}.csv", companies, results)
    print(f"Details: {path}")
    return 0


def _liquid_isins(conn) -> set[str]:
    return {
        r["isin"]
        for r in conn.execute(
            "SELECT isin FROM liquidity WHERE status = 'liquid' "
            "AND computed_on = (SELECT MAX(computed_on) FROM liquidity)"
        )
    }


def cmd_collect(args) -> int:
    cfg, conn = _context(args)
    today = date.today()
    companies = overrides.universe(conn, overrides.load(cfg.overrides))
    summary = collect.Summary()
    collect.take_inbox(conn, cfg, today, summary)
    collect.take_pending(conn, cfg, today, summary)
    _step(f"Inbox: {summary.inbox} added, {summary.completed} completed, {summary.withdrawn} withdrawn")
    with collect.client_for(cfg) as client:
        feeds = collect.enabled_feeds(cfg)
        progress = Progress("Feeds", total=len(feeds), every=10)
        try:
            collect.collect_feeds(conn, cfg, client, today, summary, on_feed=progress.update)
        finally:
            progress.done(f"{summary.added} new articles")
        news = cfg.collect["boursorama_news"]
        if news["enabled"] and not args.no_news_pages:
            isins = _liquid_isins(conn) if news["liquid_only"] else {c["isin"] for c in companies}
            if not isins:
                summary.problems.append("no liquid company yet: stock news pages skipped (run advisor liquidity)")
            before = summary.added
            progress = Progress("Stock news pages", total=min(len(isins), int(news["pages_per_run"])), every=25)
            try:
                collect.collect_news_pages(conn, cfg, client, isins, today, summary, on_page=progress.update)
            finally:
                progress.done(f"{summary.news_pages} pages read, {summary.added - before} new articles")
        waiting = conn.execute(
            "SELECT COUNT(*) AS n FROM document WHERE status = 'new' OR (status = 'fetch_failed' AND attempts < ?)",
            (collect.MAX_ATTEMPTS,),
        ).fetchone()["n"]
        progress = Progress("Article texts", total=min(waiting, int(cfg.collect["max_article_fetches"])), every=10)
        try:
            collect.fetch_texts(conn, cfg, client, today, summary, on_doc=progress.update)
        finally:
            progress.done(f"{summary.ready} ready, {summary.pending} waiting for you, {summary.failed} failed")
    progress = Progress("Company names", total=len(summary.touched), every=100)
    try:
        collect.link_names(conn, companies, summary.touched, on_doc=progress.update)
    finally:
        progress.done(f"{len(summary.touched)} articles checked")
    path = vault.write_digest(cfg.vault, today, collect.digest_lines(conn, today, summary))
    print(f"Inbox: {summary.inbox} added, {summary.completed} completed, {summary.withdrawn} withdrawn")
    print(f"Collected: {summary.added} new articles; {summary.ready} ready for analysis, "
          f"{summary.pending} waiting for their text, {summary.too_old} too old, {summary.failed} failed")
    for problem in summary.problems:
        print(f"Warning: {problem}")
    print(f"Digest: {path}")
    return 0


def _step(text: str) -> None:
    """A finished step without a progress bar, on stderr next to the bars."""
    print(text, file=sys.stderr, flush=True)


def cmd_add(args) -> int:
    cfg, conn = _context(args)
    today = date.today()
    entries = []
    if args.file:
        for n, line in enumerate(Path(args.file).read_text(encoding="utf-8").splitlines(), 1):
            parts = line.split()
            if not parts or parts[0].startswith("#"):
                continue
            entries.append((parts[0], parts[1] if len(parts) > 1 else None, n))
    if args.url:
        entries.append((args.url, args.date, 0))
    if not entries:
        print("Give a URL or --file.")
        return 1
    added = 0
    for url, published, line_no in entries:
        where = f"line {line_no}: " if line_no else ""
        if not documents.valid_url(url):
            print(f"{where}not a URL: {url}")
            continue
        try:
            day = date.fromisoformat(published) if published else None
        except ValueError:
            print(f"{where}bad date {published!r} (expected YYYY-MM-DD)")
            continue
        if collect.add_manual(conn, url, today, day) is None:
            print(f"{where}already in the database: {url}")
        else:
            added += 1
    print(f"{added} article(s) added; their text is fetched by the next `advisor collect`.")
    return 0


def cmd_sources_check(args) -> int:
    cfg, _ = _context(args)
    feeds = sources.load(cfg.sources)
    if not feeds:
        print(f"No feed in {cfg.sources} (copy sources.example.yaml to sources.yaml).")
        return 1
    failures = 0
    with collect.client_for(cfg) as client:
        for feed in feeds:
            if not feed.enabled:
                print(f"{feed.name}: disabled (not checked)")
                continue
            try:
                response = client.get(feed.url)
                response.raise_for_status()
                items = sources.parse_feed(response.content)
            except Exception as exc:  # report every feed, whatever goes wrong with one
                items, error = [], f"{type(exc).__name__}: {exc}"
            else:
                error = None if items else "no article read (not a feed?)"
            failures += error is not None
            state = "ERROR " + error if error else "ok"
            print(f"{feed.name}: {state}, {len(items)} articles" + (f", latest: {items[0].title}" if items else ""))
    return 1 if failures else 0


def cmd_extract(args) -> int:
    cfg, conn = _context(args)
    today = date.today()
    llm = cfg.llm
    size = signals.run_size(llm, args.limit)
    if args.dry_run:
        docs = signals.to_extract(conn, size)
        chars = sum(min(len(d["text"] or ""), int(llm["max_text_chars"])) for d in docs)
        tokens = chars // 3 + 1500 * len(docs)  # rough: ~3 characters a token, plus prompt and schema
        print(f"{len(docs)} articles would be analysed with {llm['model']}, about {tokens:,} input tokens, "
              f"about {signals.cost(llm, tokens, 600 * len(docs)):.3f} USD.")
        for doc in docs[:20]:
            print(f"  {doc['id']}: {doc['title'] or doc['url']}")
        return 0
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print("ANTHROPIC_API_KEY is not set: signal extraction skipped (see the README).")
        return 0 if args.command == "daily" else 1
    run = signals.RunSummary()
    progress = Progress("Analysis", total=min(size, signals.waiting_count(conn)), every=10)
    try:
        with anthropic.Anthropic(max_retries=3) as client:
            signals.run(conn, llm, client, today, limit=args.limit, on_doc=progress.update, summary=run)
    finally:
        progress.done()
        # Also after an interruption: what was analysed gets its notes and its place in the digest.
        for doc_id in run.touched:
            doc = conn.execute("SELECT * FROM document WHERE id = ?", (doc_id,)).fetchone()
            vault.write_source(cfg.vault, doc, signals.source_lines(conn, doc))
        path = vault.write_digest(cfg.vault, today, signals.digest_lines(conn, today, run), block="signals")
    print(f"Analysed: {run.extracted} articles, {run.signals} signals ({run.dropped} dropped), "
          f"{run.failed} failed, about {run.cost_usd:.3f} USD")
    if run.stopped:
        print(f"Stopped early: {run.stopped}")
    print(f"Digest: {path}")
    return 1 if run.stopped and not run.stopped.startswith("daily budget") else 0


def cmd_daily(args) -> int:
    """Evening run: volume snapshot, liquidity update, news collection, then signal extraction."""
    try:
        fetched = cmd_fetch_volumes(args)
    except Exception as exc:  # network error, bad mappings.csv...: still refresh liquidity
        print(f"Volume fetch failed: {exc!r}")
        fetched = 1
    computed = cmd_liquidity(args)  # refreshed even when no session was saved
    try:
        collected = cmd_collect(args)
    except Exception as exc:
        print(f"News collection failed: {exc!r}")
        collected = 1
    args.limit, args.dry_run = None, False
    try:
        extracted = cmd_extract(args)
    except Exception as exc:
        print(f"Signal extraction failed: {exc!r}")
        extracted = 1
    return fetched or computed or collected or extracted


def cmd_status(args) -> int:
    cfg, conn = _context(args)
    ov = overrides.load(cfg.overrides)
    print(f"Database: {cfg.database}")
    print(f"Latest imported list: {euronext.latest_list_date(conn) or 'none'}")
    print(f"Tracked universe: {len(overrides.universe(conn, ov))} companies "
          f"({len(ov.exclude)} excluded, {len(ov.include)} manually included)")
    r = conn.execute("SELECT COUNT(DISTINCT date) AS d, MAX(date) AS last FROM daily_volume").fetchone()
    print(f"Volume sessions stored: {r['d']} (latest: {r['last'] or 'none'})")
    r = conn.execute("SELECT MAX(computed_on) AS d FROM liquidity").fetchone()
    if r["d"]:
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM liquidity WHERE computed_on = ? GROUP BY status", (r["d"],)
        ):
            print(f"  {row['status']}: {row['n']} (computed on {r['d']})")
    counts = conn.execute("SELECT status, COUNT(*) AS n FROM document GROUP BY status ORDER BY status").fetchall()
    print("Documents: " + (", ".join(f"{c['status']} {c['n']}" for c in counts) or "none"))
    r = conn.execute("SELECT COUNT(*) AS n FROM signal").fetchone()
    spent = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS c, MIN(day) AS since FROM llm_usage").fetchone()
    print(f"Signals: {r['n']}" + (f" (language model: {spent['c']:.2f} USD since {spent['since']})"
                                    if spent["since"] else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="advisor", description="PEA PME ETI market watch")
    p.add_argument("--config", help="configuration file (default: config.yaml or ADVISOR_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("import-list", help="import a Euronext PEA PME list (xlsx)")
    s.add_argument("file")
    s.add_argument("--yes", action="store_true", help="apply without asking for confirmation")
    s.add_argument("--all", action="store_true", help="list every added and removed company")
    s.add_argument("--force", action="store_true", help="import even a list older than the latest one")
    s.set_defaults(func=cmd_import_list)

    s = sub.add_parser("fetch-volumes", help="save today's session from Boursorama")
    s.add_argument("--date", help="session date (YYYY-MM-DD), default: today")
    s.add_argument("--force", action="store_true", help="save even on a weekend, another date or a repeated page")
    s.set_defaults(func=cmd_fetch_volumes)

    s = sub.add_parser("backfill-history", help="seed the volume history through yfinance")
    s.add_argument("--period", default="6mo")
    s.add_argument("--limit", type=int, help="only process the first N companies (testing)")
    s.set_defaults(func=cmd_backfill_history)

    s = sub.add_parser("liquidity", help="compute the liquidity filter")
    s.add_argument("--date", help="computation date (YYYY-MM-DD), default: today")
    s.set_defaults(func=cmd_liquidity)

    s = sub.add_parser("collect", help="collect news: vault Inbox, RSS feeds, stock news pages, article texts")
    s.add_argument("--no-news-pages", action="store_true", help="skip Boursorama stock news pages")
    s.set_defaults(func=cmd_collect)

    s = sub.add_parser("add", help="add an article you found yourself")
    s.add_argument("url", nargs="?")
    s.add_argument("--date", help="publication date (YYYY-MM-DD) if the page does not give it")
    s.add_argument("--file", help="text file with one URL per line, optionally followed by a date")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("sources-check", help="test every feed of sources.yaml")
    s.set_defaults(func=cmd_sources_check)

    s = sub.add_parser("extract", help="analyse the ready articles with the language model (signals)")
    s.add_argument("--limit", type=int, help="analyse at most N articles")
    s.add_argument("--dry-run", action="store_true", help="show what would be analysed and its cost, call nothing")
    s.set_defaults(func=cmd_extract)

    s = sub.add_parser("daily", help="evening run: volumes, liquidity, news collection, signal extraction")
    s.add_argument("--date", help="session date (YYYY-MM-DD), default: today")
    s.add_argument("--force", action="store_true", help="see fetch-volumes")
    s.add_argument("--no-news-pages", action="store_true", help="see collect")
    s.set_defaults(func=cmd_daily)

    s = sub.add_parser("status", help="summary of the database state")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
