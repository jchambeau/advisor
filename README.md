# Advisor: PEA PME ETI market watch

Personal tool to follow companies eligible to the French PEA PME ETI plan: building the
universe, filtering it by liquidity, then (next steps) news collection, macro indicators,
Obsidian notes and sector scenarios at 3 months, 1 year and 5 years.

> The output of this tool is an aid to personal thinking, not investment advice.

Version 1 runs locally. Every path and setting lives in `config.yaml` (required: the tool
stops if it cannot find it), so the tool can move to a VPS later without code changes.

## Setup (Fedora)

```bash
sudo dnf install -y python3 git
git clone https://github.com/jchambeau/advisor ~/advisor && cd ~/advisor
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,history]"
cp config.example.yaml config.yaml
cp overrides.example.yaml overrides.yaml
cp sources.example.yaml sources.yaml
```

## Step 1: universe and liquidity

### 1. Import the Euronext list

Download the PEA PME list published by Euronext (xlsx), drop it in `data/lists/`, then:

```bash
advisor import-list data/lists/liste_pea_pme_23_October_2026.xlsx
```

The tool shows the companies added, returned and removed compared with the previous list,
and asks for confirmation before applying (`--yes` to skip it, `--all` to list everything).
A company removed from the list stays in the database, flagged as removed. A list older than
the latest imported one is refused unless `--force` is given.

### 2. Manual inclusions and exclusions

`overrides.yaml` excludes a company from the universe or includes one missing from the list,
with a reason. These choices are kept from one import to the next.

### 3. Volumes

- `advisor fetch-volumes` saves today's session from Boursorama's "PEA PME quotes" page
  (close and volume). Run it every trading evening: this is what builds the history. The page
  only shows the latest session, so nothing is saved on a weekend, for a date other than
  today, or when most stocks show the same figures as the previous snapshot (holiday, run
  before the open); `--force` overrides these checks.
- `advisor backfill-history` seeds 6 months of history through yfinance, until enough
  Boursorama snapshots exist. For the same session, Boursorama takes precedence.

Both commands show their progress on stderr (page X/N, or company X/N with an estimate of the
time left); on a terminal the line updates in place, in the systemd journal a line is written
every few steps.

The quotes list does not show the ISIN, so stocks are matched by name first. A company listed
on several venues (e.g. Paris and Amsterdam) keeps the line of its own market. Stocks the
name match misses are then looked up on their own Boursorama page, which shows the ISIN (one
request each, at most `max_isin_lookups` per run; the page of a stock still unmatched is
read again after `isin_lookup_retry_days`). Stocks still unmatched are listed in
`data/exports/unmatched_boursorama.csv`, with the ISIN their page shows and why each one is
left out. Boursorama's PEA PME filter is wider than Euronext's official list, so many of them
(e.g. Accor) are simply not in the list and need nothing; those come last in the file. To fix one, create `mappings.csv` next to
`config.yaml` (or wherever `paths.mappings` points) from `mappings.example.csv` (header only): one line per
company, e.g. `FR0013341781;1rPAL2SI;AL2SI.PA`; any column but `isin` may be left empty. It always
wins over the automatic match.

### 4. Liquidity filter

```bash
advisor liquidity
```

Goal: an order of about EUR 1,000 should go through without trouble. Over the last 63
sessions (about 3 months), a company is **liquid** when:

- its median daily turnover reaches EUR 20,000 (the exit threshold is EUR 15,000 for a
  company already liquid, to avoid flip-flopping);
- at least 95% of sessions have a trade.

Only sessions up to the computation date count. The status is **unknown** with fewer than
20 known sessions, when the latest session is more than 10 days old, or when a stock is quoted
in a currency missing from `currencies`; the export says why. Every threshold is set in
`config.yaml`. Turnover of Oslo stocks is converted to euros with the rate set under
`currencies`. Details are exported to `data/exports/liquidity_<date>.csv`.

### Daily run

```bash
advisor daily     # Boursorama volumes, liquidity, then news collection
advisor status    # database summary
```

Schedule it every trading evening with the systemd user timer in `deploy/systemd/`
(Fedora has no cron by default; the same units work on a VPS):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/advisor-daily.* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now advisor-daily.timer
systemctl --user list-timers advisor-daily.timer   # next run
journalctl --user -u advisor-daily.service         # output of past runs
```

The timer runs Monday to Friday at 19:00 Paris time, and again at 22:00 to catch an evening
when the machine was asleep or offline (the second run only rewrites the same session). It only fires while your user session
is open; to run it when you are logged out, enable lingering once with
`sudo loginctl enable-linger $USER`. The units assume the repository lives in `~/advisor`.

## Step 2: news collection

```bash
advisor sources-check   # test every feed of sources.yaml (do this first)
advisor collect         # vault Inbox, RSS feeds, stock news pages, then article texts
```

`advisor collect` gathers articles into the database without calling any language model
(signal extraction is described below). In order, it:

1. takes the notes you dropped in `vault/Inbox/` (see below) and the articles you completed
   or withdrew in `vault/Inbox/To-complete/`;
2. reads the RSS feeds listed in `sources.yaml` (articles older than `max_age_days` are skipped);
3. reads the Boursorama news page of the stocks of the liquid universe, a share of them per run
   (`pages_per_run`), the least recently visited first; a first visit only takes the latest items;
4. fetches each new article and extracts its text; the text is never treated as instructions;
5. links articles to companies: those found on a stock's news page, and those whose name
   appears in the text;
6. writes the digest of the day in `vault/Journal/<date>.md`.

Duplicates are detected by URL (tracking parameters removed) and by title within a few days.

**Paywalled articles.** When the text looks incomplete (too short for a feed article, a marker
such as "réservé aux abonnés", or access refused), the article is not analysed. A note is
written in `vault/Inbox/To-complete/` and listed at the top of the digest. Either paste the
text under its `## Text` heading, or set `action: withdraw` in its properties; the next run
takes it into account and moves the note to `Inbox/Done/`. A feed with `on_paywall: summary` in
`sources.yaml` skips this and keeps the feed summary instead.

**Articles you find yourself** follow the same path, with three ways in:

- a note in `vault/Inbox/` with the URL in its properties (`url:`, or `source:` as the Obsidian
  Web Clipper writes it) or on its first line, and the article text as body if you have it
  (clipped or pasted: then the page is not fetched); an optional `published:` date;
- `advisor add <url> [--date YYYY-MM-DD]`;
- `advisor add --file urls.txt`, one URL per line, optionally followed by a date, to catch up
  on history at once.

Each document keeps its publication date and the date it was added. Old articles you add are
never skipped for their age.

`advisor daily` now runs the collection after the liquidity update. Collecting reads other
sites' pages: check that their terms of use allow it before scheduling it.

### Signal extraction

```bash
export ANTHROPIC_API_KEY=...    # from console.anthropic.com
advisor extract --dry-run       # what would be analysed, and the estimated cost
advisor extract [--limit N]     # analyse the ready articles, newest first
```

Each ready article (paywalled ones only once you completed them) is sent to Claude Haiku 5.5
with the companies it was linked to and the list of macro factors from `config.yaml`. The
model returns signals as JSON: a target (a company of the universe, a sector or a macro
factor), a type (results, guidance, orders and contracts, financing, prices and margins...),
a direction, an intensity from 1 to 3, a confidence, and the quote that supports it.

- A signal is kept only if its quote is found word for word in the article, and its company
  is one the article was linked to: an invented fact or company is dropped.
- The article text is passed as data; the model is told never to follow instructions in it.
- One note per article in `vault/Sources/` (summary and signals), and a "Signals today"
  section in the digest of the day. Anything you write outside the `<!-- ... -->` blocks of
  these notes is kept.
- Spending is recorded per article (`advisor status` shows the total); the run stops at
  `daily_budget_usd`. With `effort: low`, an article costs a fraction of a cent.
- An answer that cannot be used marks the article `extraction_failed`; it is retried on the
  next runs, up to 3 times. A refused key, a rate limit or an API outage stops the run and
  leaves the articles for the next one.

`advisor daily` runs the extraction last, when `ANTHROPIC_API_KEY` is set. For the systemd
timer, put the key in `~/.config/advisor/env`:

```bash
mkdir -p ~/.config/advisor
printf 'ANTHROPIC_API_KEY=%s\n' 'sk-ant-...' > ~/.config/advisor/env
chmod 600 ~/.config/advisor/env
```

The signals describe what articles say; they are not investment advice.

## Tests

```bash
pytest
ADVISOR_REAL_LIST=data/lists/<file>.xlsx pytest   # adds a test on a real list
```
