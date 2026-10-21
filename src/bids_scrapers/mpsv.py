"""MPSV (Ministry of Labour) unemployment open data.

The municipality-level series is split in two:

  * nezamestnanost_obce_2014-2024/  frozen monthly JSON files, 03/2014-12/2024 (data.mpsv.cz/od).
        Fields: obec, okres, rok, mesic, uchazeciOZamestnani, dosazitelniUchazeci,
        obyvatelstvo15_64, podilNezamestnanychOsob (%), volnaPracovnaMista.
  * nezamestnanost_obce/YYYY-MM.csv  new portal, 01/2025 onwards, one file per month.
        UTF-8 with BOM, ';' separated, decimal comma. Two rows per municipality (uchazec_pohlavi
        M/Ž) that have to be summed; `pno` is a fraction (0.048), not a percentage.
        *_trvala columns (permanent address) continue the 2014-2024 series.

Vacancies are no longer published per municipality since 2025, only per labour-office district:

  * volna_mista_okresy/YYYY-MM.csv  vacancies by district, ISCO, NACE, education (portal).

  * ciselniky/  MPSV code lists of municipalities, districts and regions.

Everything is nationwide; filter to Jihomoravský kraj with kraj_kod 116 / okres 37xx.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .http import PoliteClient
from .opendata import DownloadError, Store

log = logging.getLogger(__name__)

LEGACY_URL = "https://data.mpsv.cz/od/soubory/nezamestnanost-v-obcich/nezamestnanost-v-obcich-{month:02d}-{year}.json.gz"
LEGACY_FIRST, LEGACY_LAST = (2014, 3), (2024, 12)
PORTAL_URL = "https://data.mpsv.cz/portal/api/reports/by-table/{table}/data/{fmt}"
# local folder -> portal table. "<table>_vp" is the same table with only the latest month.
PORTAL_TABLES = {
    "nezamestnanost_obce": "evid_pno_adresa_agr_frz_odata",
    "volna_mista_okresy": "vm_stav_vm_stat_agr_frz_odata",
}
CODELIST_URL = "https://data.mpsv.cz/od/soubory/ciselniky/{name}"
CODELISTS = ["obce.json", "okresy.json", "kraje.json"]


def _months(first: tuple[int, int], last: tuple[int, int]):
    year, month = first
    while (year, month) <= last:
        yield year, month
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _latest_portal_month(client: PoliteClient, table: str) -> str | None:
    resp = client.get(
        PORTAL_URL.format(table=table, fmt="json"),
        params={"page": 0, "size": 1, "sort": "rozhodne_datum,desc"},
    )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["rozhodne_datum"][:7] if rows else None


def _split_by_month(store: Store, src: Path, folder: str, url: str, expected_rows: int | None = None) -> dict[str, str]:
    """Split a portal CSV (first column rozhodne_datum = YYYY-MM-DD) into folder/YYYY-MM.csv files,
    keeping the original bytes of every line. expected_rows (the portal's x-total-count) guards
    against a silently truncated download."""
    out_dir = store.path(folder)
    out_dir.mkdir(parents=True, exist_ok=True)
    parts: dict[str, object] = {}
    n_rows = 0
    with open(src, "r", encoding="utf-8-sig", newline="") as fh:
        header = fh.readline()
        for line in fh:
            month = line[:7]
            if len(month) != 7 or month[4] != "-":
                continue
            n_rows += 1
            if month not in parts:
                part = open(out_dir / f".{month}.csv.part", "w", encoding="utf-8-sig", newline="")
                part.write(header)
                parts[month] = part
            parts[month].write(line)
    if expected_rows is not None and n_rows != expected_rows:
        for month, part in parts.items():
            part.close()
            (out_dir / f".{month}.csv.part").unlink()
        src.unlink()
        raise DownloadError(f"{url}: got {n_rows} rows, the portal announced {expected_rows}")
    results = {}
    for month, part in sorted(parts.items()):
        part.close()
        rel = f"{folder}/{month}.csv"
        results[rel] = store.put(rel, out_dir / f".{month}.csv.part", {"url": url})
    src.unlink()
    return results


def _sync_portal_table(client: PoliteClient, store: Store, folder: str, table: str) -> dict[str, str]:
    latest = _latest_portal_month(client, table)
    if latest is None:
        log.warning("MPSV %s: portal returned no rows", table)
        return {}
    local = sorted(p.stem for p in store.path(folder).glob("[0-9][0-9][0-9][0-9]-[0-9][0-9].csv"))
    if latest in local:
        log.info("MPSV %s: up to date (%s)", folder, latest)
        return {}
    first = local[0] if local else None
    missing = []
    if first:
        y1, m1 = map(int, first.split("-"))
        y2, m2 = map(int, latest.split("-"))
        missing = [f"{y}-{m:02d}" for y, m in _months((y1, m1), (y2, m2)) if f"{y}-{m:02d}" not in local]
    # Only the newest month missing -> the small "_vp" table; otherwise (first run / gap) the full table.
    name = f"{table}_vp" if missing == [latest] else table
    url = PORTAL_URL.format(table=name, fmt="csv")
    log.info("MPSV %s: downloading %s (latest month %s)", folder, name, latest)
    tmp, headers = store.download_temp(client, url, params={"fileName": name, "unbounded": "true"})
    total = headers.get("x-total-count")
    return _split_by_month(store, tmp, folder, url, int(total) if total and total.isdigit() else None)


def collect(client: PoliteClient, store: Store) -> dict[str, str]:
    results = {}
    for name in CODELISTS:
        results[f"ciselniky/{name}"] = store.fetch(client, CODELIST_URL.format(name=name), f"ciselniky/{name}")

    # Frozen series: download each month once, never re-check.
    for year, month in _months(LEGACY_FIRST, LEGACY_LAST):
        rel = f"nezamestnanost_obce_2014-2024/nezamestnanost-v-obcich-{month:02d}-{year}.json.gz"
        if not store.has(rel):
            url = LEGACY_URL.format(year=year, month=month)
            results[rel] = store.fetch(client, url, rel, conditional=False, decode=False)

    for folder, table in PORTAL_TABLES.items():
        results.update(_sync_portal_table(client, store, folder, table))
    return results
