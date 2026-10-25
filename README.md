# Advisor: PEA PME ETI market watch

Personal tool to follow companies eligible to the French PEA PME ETI plan: building the
universe, filtering it by liquidity, then (next steps) news collection, macro indicators,
Obsidian notes and sector scenarios at 3 months, 1 year and 5 years.

> The output of this tool is an aid to personal thinking, not investment advice.

Version 1 runs locally. Every path and setting lives in `config.yaml`, so the tool can move
to a VPS later without code changes.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,history]"
cp config.example.yaml config.yaml
cp overrides.example.yaml overrides.yaml
```

## Step 1: universe and liquidity

### 1. Import the Euronext list

Download the PEA PME list published by Euronext (xlsx), drop it in `data/lists/`, then:

```bash
advisor import-list data/lists/liste_pea_pme_23_October_2026.xlsx
```

The tool shows the companies added, returned and removed compared with the previous list,
and asks for confirmation before applying (`--yes` to skip it, `--all` to list everything).
A company removed from the list stays in the database, flagged as removed.

### 2. Manual inclusions and exclusions

`overrides.yaml` excludes a company from the universe or includes one missing from the list,
with a reason. These choices are kept from one import to the next.

### 3. Volumes

- `advisor fetch-volumes` saves today's session from Boursorama's "PEA PME quotes" page
  (close and volume). Run it every trading evening: this is what builds the history.
- `advisor backfill-history` seeds 6 months of history through yfinance, until enough
  Boursorama snapshots exist. For the same session, Boursorama takes precedence.

Boursorama does not show the ISIN, so stocks are matched by name. Unmatched stocks are listed
in `data/exports/unmatched_boursorama.csv`; copy them into `mappings.csv`
(`isin;boursorama_code;yahoo_ticker`), which always wins over the automatic match.

### 4. Liquidity filter

```bash
advisor liquidity
```

Goal: an order of about EUR 1,000 should go through without trouble. Over the last 63
sessions (about 3 months), a company is **liquid** when:

- its median daily turnover reaches EUR 20,000 (the exit threshold is EUR 15,000 for a
  company already liquid, to avoid flip-flopping);
- at least 95% of sessions have a trade.

With fewer than 20 known sessions, the status is **unknown**. Every threshold is set in
`config.yaml`. Turnover of Oslo stocks is converted to euros with the rate set under
`currencies`. Details are exported to `data/exports/liquidity_<date>.csv`.

### Daily run

```bash
advisor daily     # Boursorama volumes, then liquidity
advisor status    # database summary
```

Schedule it every trading evening (local task scheduler, or `cron` / a `systemd` timer on a VPS).

## Tests

```bash
pytest
ADVISOR_REAL_LIST=data/lists/<file>.xlsx pytest   # adds a test on a real list
```
