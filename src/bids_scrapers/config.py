"""Configuration: defaults below, optionally overridden by a TOML file (see config.example.toml)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

from . import __version__


@dataclass
class HttpConfig:
    # Random pause between two consecutive requests to the same site, in seconds.
    min_delay: float = 2.5
    max_delay: float = 6.0
    # After this many requests take a longer break (spreads load over time).
    long_pause_every: int = 150
    long_pause_min: float = 30.0
    long_pause_max: float = 90.0
    timeout: float = 60.0
    max_retries: int = 4
    # If the server asks us (Retry-After) to wait longer than this, stop the run instead.
    max_retry_after: float = 900.0


@dataclass
class SrealityConfig:
    region_id: int = 14  # Jihomoravský kraj
    # (category_main_cb, category_type_cb): 1=byty, 2=domy / 1=prodej, 2=pronájem
    categories: list[list[int]] = field(default_factory=lambda: [[1, 1], [1, 2], [2, 1], [2, 2]])
    page_size: int = 60
    # Cap on detail requests per run. The first ~6k details are spread over several runs.
    max_details_per_run: int = 1500
    # Re-download details older than this many days (0 = never, details rarely change).
    detail_refresh_days: int = 0
    # The search scan must reach this share of the reported total, otherwise the run is "partial".
    min_completeness: float = 0.97


@dataclass
class PingConfig:
    # Optional dead-man's-switch URLs (e.g. https://hc-ping.com/<uuid> from healthchecks.io).
    # Pinged after every run; "<url>/fail" when the run failed. Empty = off.
    sreality: str = ""
    opendata: str = ""


@dataclass
class Config:
    data_dir: Path = Path("data")
    # E-mail or URL put into the User-Agent so site operators can reach you instead of blocking.
    contact: str = ""
    http: HttpConfig = field(default_factory=HttpConfig)
    sreality: SrealityConfig = field(default_factory=SrealityConfig)
    ping: PingConfig = field(default_factory=PingConfig)

    @property
    def user_agent(self) -> str:
        ua = f"bids-scrapers/{__version__} (university student project, Mendelu BIDS"
        if self.contact:
            ua += f"; contact: {self.contact}"
        return ua + ")"


def _apply(obj, values: dict, prefix: str = "") -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Unknown config option: {prefix}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            _apply(current, value, prefix=f"{prefix}{key}.")
        elif isinstance(current, Path):
            setattr(obj, key, Path(value))
        else:
            setattr(obj, key, value)


def load_config(path: Path | None) -> Config:
    cfg = Config()
    if path is None:
        default = Path("config.toml")
        path = default if default.exists() else None
    if path is not None:
        with open(path, "rb") as fh:
            _apply(cfg, tomllib.load(fh))
        if not cfg.data_dir.is_absolute():
            cfg.data_dir = (path.parent / cfg.data_dir).resolve()
    cfg.data_dir = cfg.data_dir.resolve()
    return cfg
