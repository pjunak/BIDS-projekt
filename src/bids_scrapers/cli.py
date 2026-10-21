from __future__ import annotations

import argparse
import logging
import signal
import sys
from collections import Counter
from pathlib import Path

import requests

from .config import Config, load_config
from .http import Blocked, PoliteClient
from .util import run_lock, setup_logging

log = logging.getLogger("bids_scrapers")

OPEN_DATA_SOURCES = ("csu", "mpsv", "policie", "ruian")


def run_open_data(cfg: Config, sources: list[str], rebuild_extract: bool) -> int:
    from . import csu, mpsv, policie, ruian
    from .opendata import Store

    failed = []
    for source in sources:
        store = Store(cfg.data_dir, source)
        with PoliteClient(cfg.http, cfg.user_agent) as client:
            try:
                if source == "csu":
                    results = csu.collect(client, store)
                elif source == "mpsv":
                    results = mpsv.collect(client, store)
                elif source == "ruian":
                    results = ruian.collect(client, store, Store(cfg.data_dir, "csu"), rebuild_extract=rebuild_extract)
                else:
                    results = policie.collect(client, store, Store(cfg.data_dir, "csu"), rebuild_extract=rebuild_extract)
            except Blocked as exc:
                log.error("%s refused our requests: %s. Stopping this source.", source, exc)
                failed.append(source)
                continue
            except Exception:
                log.exception("%s failed", source)
                failed.append(source)
                continue
        summary = ", ".join(f"{n} {k}" for k, n in Counter(results.values()).most_common())
        log.info("%s: %d requests; files: %s", source, client.n_requests, summary or "nothing to do")
        if "failed" in results.values():
            failed.append(source)
    return 1 if failed else 0


def ping(url: str, ok: bool) -> None:
    """Report the run outcome to a monitoring URL; never let monitoring break the run."""
    if not url:
        return
    try:
        requests.get(url if ok else url.rstrip("/") + "/fail", timeout=10)
    except requests.RequestException as exc:
        log.warning("Monitoring ping failed: %s", exc)


def geocode_listings(cfg: Config, *, redo: bool = False) -> bool:
    """Give Sreality listings their kod_obec / kod_momc; a failure only logs, so the caller carries on."""
    from . import ruian

    try:
        ruian.geocode(cfg, redo=redo)
    except ruian.NoBoundaries as exc:
        log.warning("%s; kod_obec stays empty for now", exc)
        return False
    except Exception:
        log.exception("Geocoding failed")
        return False
    return True


def _raise_interrupt(signum, frame):
    # systemd stops a service with SIGTERM; turn it into KeyboardInterrupt so runs get recorded
    # as interrupted and partial files are cleaned up.
    raise KeyboardInterrupt(f"signal {signum}")


def open_data_status(cfg: Config) -> str:
    from .opendata import Store

    lines = []
    for source in OPEN_DATA_SOURCES:
        if not (cfg.data_dir / "raw" / source / "manifest.json").exists():
            lines.append(f"{source}: no data yet")
            continue
        entries = Store(cfg.data_dir, source).entries
        size = sum(e.get("size", 0) for e in entries.values())
        last_change = max((e.get("changed_at", "") for e in entries.values()), default="")
        last_check = max((e.get("checked_at", "") for e in entries.values()), default="")
        lines.append(
            f"{source}: {len(entries)} files, {size / 1e6:.0f} MB, last change {last_change[:10]}, "
            f"last check {last_check[:16]}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bids-scrape", description="Data collection for the BIDS project")
    parser.add_argument("-c", "--config", type=Path, help="TOML config (default: ./config.toml if present)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sreality", help="scan Sreality listings + fetch missing details")
    p.add_argument("--details-only", action="store_true", help="skip the search scan, only fetch details")
    p.add_argument("--max-details", type=int, help="override max_details_per_run for this run")
    p.add_argument("--force", action="store_true", help="ignore the pause after the site refused us")

    p = sub.add_parser("opendata", help="download new/changed ČSÚ, MPSV, Policie ČR and ČÚZK RÚIAN files")
    p.add_argument("sources", nargs="*", metavar="SOURCE", help=f"any of {', '.join(OPEN_DATA_SOURCES)} (default: all)")
    p.add_argument(
        "--rebuild-extract", action="store_true", help="re-create the derived files (Policie JMK rows, RÚIAN boundaries)"
    )

    p = sub.add_parser("geocode", help="assign kód obce / Brno městská část to Sreality listings from their GPS")
    p.add_argument("--all", action="store_true", help="re-match every listing, not only new or changed ones")

    sub.add_parser("export", help="write Sreality CSVs to data/export/sreality/")
    sub.add_parser("status", help="show what has been collected so far")

    args = parser.parse_args(argv)
    if args.command == "opendata" and (unknown := set(args.sources) - set(OPEN_DATA_SOURCES)):
        parser.error(f"unknown source(s): {', '.join(sorted(unknown))}")
    cfg = load_config(args.config)
    setup_logging(cfg.data_dir, args.verbose)
    signal.signal(signal.SIGTERM, _raise_interrupt)

    if args.command == "sreality":
        from .sreality import SrealityCollector

        with run_lock(cfg.data_dir, "sreality"):
            status = SrealityCollector(cfg).run(
                details_only=args.details_only, max_details=args.max_details, force=args.force
            )
            geocode_listings(cfg)
        ok = status in ("ok", "details")
        ping(cfg.ping.sreality, ok)
        return 0 if ok else 1

    if args.command == "opendata":
        with run_lock(cfg.data_dir, "opendata"):
            code = run_open_data(cfg, args.sources or list(OPEN_DATA_SOURCES), args.rebuild_extract)
        ping(cfg.ping.opendata, code == 0)
        return code

    if args.command == "geocode":
        with run_lock(cfg.data_dir, "sreality"):
            return 0 if geocode_listings(cfg, redo=args.all) else 1

    if args.command == "export":
        from .sreality import export_csv

        geocode_listings(cfg)
        for path in export_csv(cfg):
            print(path)
        return 0

    if args.command == "status":
        from . import sreality

        print(sreality.status(cfg))
        print(open_data_status(cfg))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
