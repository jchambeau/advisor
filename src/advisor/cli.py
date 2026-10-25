"""Command line: advisor <command>."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from . import boursorama, euronext, history, liquidity, mappings, overrides
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


def cmd_daily(args) -> int:
    """Evening run: volume snapshot, then liquidity update."""
    try:
        fetched = cmd_fetch_volumes(args)
    except Exception as exc:  # network error, bad mappings.csv...: still refresh liquidity
        print(f"Volume fetch failed: {exc!r}")
        fetched = 1
    computed = cmd_liquidity(args)  # refreshed even when no session was saved
    return fetched or computed


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

    s = sub.add_parser("daily", help="evening run: Boursorama volumes, then liquidity")
    s.add_argument("--date", help="session date (YYYY-MM-DD), default: today")
    s.add_argument("--force", action="store_true", help="see fetch-volumes")
    s.set_defaults(func=cmd_daily)

    s = sub.add_parser("status", help="summary of the database state")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
