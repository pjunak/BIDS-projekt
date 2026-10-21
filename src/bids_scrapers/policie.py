"""Policie ČR crime map open data (kriminalita.policie.gov.cz), 2013 onwards.

Raw (data/raw/policie/):
  * index.json            list of monthly files with their last regeneration time
  * mesice/YYYYMM.zip     nationwide month: YYYYMM.csv + types.csv, states.csv, relevance.csv
                          CSV columns: id, x, y (WGS84, snapped to a Voronoi cell centroid for
                          anonymity), mp, date, state, relevance, types. One row per (id, type code),
                          so count distinct id for the number of offences.
  * voronoi.gpkg.zip      the Voronoi cells with their municipality codes (static since 2022)

Extract (data/extract/policie/), derived from the raw files:
  * voronoi_body.csv      centroid -> kod_obec / kod_momc lookup (a 100 % exact match on x, y)
  * jmk/YYYYMM.csv        rows located in Jihomoravský kraj, with kod_obec and kod_momc added
  * jmk/types.csv, states.csv, relevance.csv   code lists

The site regenerates files nightly (offences get solved, records get corrected), so months from
the last `REFRESH_MONTHS` are re-checked (ETag) when the index says they changed.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import zipfile
from datetime import date
from pathlib import Path

from . import csu
from .http import PoliteClient
from .opendata import CHANGED, NEW, Store

log = logging.getLogger(__name__)

BASE = "https://kriminalita.policie.gov.cz"
INDEX_URL = f"{BASE}/api/v2/downloads"
MONTH_URL = f"{BASE}/api/v2/downloads/{{name}}.zip"
VORONOI_URL = f"{BASE}/voronoi.gpkg.zip"
FIRST_MONTH = "201301"  # 2012 files exist but are nearly empty
REFRESH_MONTHS = 12
CODELIST_FILES = ("types.csv", "states.csv", "relevance.csv")


def _month_cutoff(months_back: int) -> str:
    today = date.today()
    index = today.year * 12 + today.month - 1 - months_back
    return f"{index // 12}{index % 12 + 1:02d}"


def _key(x: str | float, y: str | float) -> tuple[str, str]:
    return f"{float(x):.6f}", f"{float(y):.6f}"


def build_voronoi_lookup(store: Store, out: Path) -> None:
    """Read the cell table from the GeoPackage (an SQLite file) without touching the geometry."""
    tmp_dir = store.root / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(store.path("voronoi.gpkg.zip")) as zf:
        member = next(n for n in zf.namelist() if n.endswith(".gpkg"))
        gpkg = Path(zf.extract(member, tmp_dir))
    try:
        db = sqlite3.connect(gpkg)
        table = db.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'features'").fetchone()[0]
        rows = db.execute(f'SELECT id, kod_obec, kod_momc, kod_zuj, def_bod_x, def_bod_y FROM "{table}"')
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["id", "kod_obec", "kod_momc", "kod_zuj", "def_bod_x", "def_bod_y"])
            n = 0
            for row in rows:
                writer.writerow(row)
                n += 1
        db.close()
        log.info("Voronoi lookup: %d cells -> %s", n, out)
    finally:
        gpkg.unlink(missing_ok=True)


def _load_lookup(path: Path) -> dict[tuple[str, str], tuple[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return {
            _key(r["def_bod_x"], r["def_bod_y"]): (r["kod_obec"], r["kod_momc"])
            for r in csv.DictReader(fh)
            if r["def_bod_x"] and r["def_bod_y"]
        }


def extract_month(zip_path: Path, out: Path, lookup, jmk: set[str]) -> tuple[int, int, int]:
    """Write the JMK rows of one month. Returns (rows read, rows kept, rows without a cell match)."""
    name = zip_path.stem
    n_in = n_out = n_unmatched = 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with zipfile.ZipFile(zip_path) as zf, zf.open(f"{name}.csv") as raw, open(tmp, "w", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
        writer = csv.writer(fh)
        columns = list(reader.fieldnames or [])
        writer.writerow(columns + ["kod_obec", "kod_momc"])
        for row in reader:
            n_in += 1
            match = lookup.get(_key(row["x"], row["y"]))
            if match is None:
                n_unmatched += 1
                continue
            if match[0] in jmk:
                writer.writerow([row[c] for c in columns] + list(match))
                n_out += 1
    tmp.replace(out)
    return n_in, n_out, n_unmatched


def collect(client: PoliteClient, store: Store, csu_store: Store, *, rebuild_extract: bool = False) -> dict[str, str]:
    results = {}
    results["index.json"] = store.fetch(client, INDEX_URL, "index.json", keep_history=False)
    index = json.loads(store.path("index.json").read_text(encoding="utf-8"))["data"]
    cutoff = _month_cutoff(REFRESH_MONTHS)

    for item in sorted(index, key=lambda i: i["name"]):
        name, updated = item["name"], item.get("updated")
        if not (name.isdigit() and len(name) == 6 and name >= FIRST_MONTH):
            continue
        rel = f"mesice/{name}.zip"
        if store.has(rel) and (store.entries[rel].get("updated") == updated or name < cutoff):
            continue
        # Older versions of monthly files are not kept: they would only duplicate ~3 MB each refresh.
        results[rel] = store.fetch(client, MONTH_URL.format(name=name), rel, keep_history=False)
        store.entries[rel]["updated"] = updated
        store.save()

    results["voronoi.gpkg.zip"] = store.fetch(client, VORONOI_URL, "voronoi.gpkg.zip")

    # ---- derived extract -------------------------------------------------------------------
    extract_dir = store.root.parent.parent / "extract" / "policie"
    lookup_path = extract_dir / "voronoi_body.csv"
    if rebuild_extract or results["voronoi.gpkg.zip"] in (NEW, CHANGED) or not lookup_path.exists():
        build_voronoi_lookup(store, lookup_path)
        rebuild_extract = True
    if not csu_store.has("ciselnik_obce_okresy.json"):
        csu_store.fetch(client, csu.CODELIST_URL, "ciselnik_obce_okresy.json", params={"kodUrovne": "OBEC"})
    jmk = set(csu.jmk_municipalities(csu_store))
    lookup = None
    months = sorted(p for p in store.path("mesice").glob("*.zip"))
    for zip_path in months:
        out = extract_dir / "jmk" / f"{zip_path.stem}.csv"
        if not (rebuild_extract or results.get(f"mesice/{zip_path.name}") in (NEW, CHANGED) or not out.exists()):
            continue
        if lookup is None:
            lookup = _load_lookup(lookup_path)
        try:
            n_in, n_out, n_unmatched = extract_month(zip_path, out, lookup, jmk)
        except (KeyError, zipfile.BadZipFile, csv.Error, UnicodeDecodeError) as exc:
            log.error("Policie %s: cannot extract (%s: %s); skipping this month", zip_path.stem, type(exc).__name__, exc)
            out.with_suffix(".part").unlink(missing_ok=True)
            results[f"extract/{zip_path.stem}"] = "failed"
            continue
        level = logging.WARNING if n_unmatched else logging.INFO
        log.log(level, "Policie %s: %d rows, %d in JMK, %d without municipality", zip_path.stem, n_in, n_out, n_unmatched)
    if months:
        with zipfile.ZipFile(months[-1]) as zf:
            for member in CODELIST_FILES:
                if member in zf.namelist():
                    (extract_dir / "jmk" / member).write_bytes(zf.read(member))
    return results
