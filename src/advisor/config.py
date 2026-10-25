"""Configuration loading (YAML file + environment variables)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULTS = {
    "paths": {
        "database": "data/advisor.db",
        "lists": "data/lists",
        "overrides": "overrides.yaml",
        "mappings": "mappings.csv",
        "exports": "data/exports",
    },
    "liquidity": {
        "window_sessions": 63,
        "min_sessions": 20,
        "median_turnover_entry": 20000,
        "median_turnover_exit": 15000,
        "min_active_session_ratio": 0.95,
        "max_staleness_days": 10,
    },
    "currencies": {"EUR": 1.0},
    "boursorama": {
        "url": "https://www.boursorama.com/bourse/actions/cotations/",
        "market_filter": "PEAPME",
        "max_pages": 60,
        "pause_seconds": 2.0,
        "max_isin_lookups": 200,
        "isin_lookup_retry_days": 30,
    },
}


@dataclass
class LiquidityRules:
    window_sessions: int = 63
    min_sessions: int = 20
    median_turnover_entry: float = 20000
    median_turnover_exit: float = 15000
    min_active_session_ratio: float = 0.95
    max_staleness_days: int = 10


@dataclass
class Config:
    root: Path
    database: Path
    lists: Path
    overrides: Path
    mappings: Path
    exports: Path
    liquidity: LiquidityRules
    currencies: dict[str, float] = field(default_factory=dict)
    boursorama: dict = field(default_factory=dict)


def _merge(defaults: dict, values: dict) -> dict:
    result = dict(defaults)
    for key, value in (values or {}).items():
        if value is None and isinstance(defaults.get(key), dict):
            continue  # a section left empty (all lines commented out) keeps its defaults
        if isinstance(value, dict) and isinstance(defaults.get(key), dict):
            result[key] = _merge(defaults[key], value)
        else:
            result[key] = value
    return result


def load(path: str | Path | None = None) -> Config:
    """Load config.yaml (or ADVISOR_CONFIG). Missing keys take their default values."""
    path = Path(path or os.environ.get("ADVISOR_CONFIG", "config.yaml")).resolve()
    if not path.exists():
        # Fail loudly: silently falling back would create an empty database elsewhere.
        raise SystemExit(f"Configuration file not found: {path} (copy config.example.yaml to config.yaml)")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data = _merge(DEFAULTS, raw)
    root = path.parent

    def resolve(value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else root / p

    paths = data["paths"]
    return Config(
        root=root,
        database=resolve(os.environ.get("ADVISOR_DATABASE", paths["database"])),
        lists=resolve(paths["lists"]),
        overrides=resolve(paths["overrides"]),
        mappings=resolve(paths["mappings"]),
        exports=resolve(paths["exports"]),
        liquidity=LiquidityRules(**data["liquidity"]),
        currencies={k: float(v) for k, v in data["currencies"].items()},
        boursorama=data["boursorama"],
    )
