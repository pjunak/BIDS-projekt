"""Sreality.cz collector.

Uses the same JSON API that the sreality.cz web frontend calls:
  search: GET https://www.sreality.cz/api/v1/estates/search?category_main_cb=..&category_type_cb=..&locality_region_id=..
  detail: GET https://www.sreality.cz/api/v1/estates/{hash_id}

Each run:
  1. pages through the search for every configured category (~100 requests for Jihomoravský kraj),
     storing one price snapshot per listing per day and first/last-seen dates,
  2. downloads the detail of listings that have none yet (area, condition, ownership, energy class,
     date listed, ...), at most `max_details_per_run` per run.

Sreality offers no history of removed listings, so the time series starts with the first run.
Seller/agency names and contacts (e-mails, phones) and free-text descriptions are not stored,
only their numeric ids (enough to spot the same flat offered by several agencies).
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .http import Blocked, GaveUp, PoliteClient
from .util import JsonlGzWriter, now_utc, today_local

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.sreality.cz/api/v1/estates/search"
DETAIL_URL = "https://www.sreality.cz/api/v1/estates/{hash_id}"
CATEGORY_MAIN = {1: "byty", 2: "domy"}
CATEGORY_TYPE = {1: "prodej", 2: "pronajem"}
BLOCKED_COOLDOWN = timedelta(hours=72)

# Bulky or personal parts of the API responses that we do not keep.
SEARCH_DROP = {"advert_images", "advert_images_all", "premise", "premise_logo"}
DETAIL_DROP = {
    "advert_images", "advert_images_all", "advert_description", "labels", "labels_extended",
    "panorama", "panorama_data", "videos", "matterport_url", "user", "premise", "rus", "rus_reply",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id         INTEGER PRIMARY KEY,
    snapshot_date  TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL,          -- running | ok | partial | details | blocked | failed | interrupted
    n_requests     INTEGER NOT NULL DEFAULT 0,
    n_seen         INTEGER NOT NULL DEFAULT 0,
    n_new          INTEGER NOT NULL DEFAULT 0,
    n_details      INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    scan_complete  INTEGER NOT NULL DEFAULT 0  -- 1 = this run saw every listing online (defines is_active)
);

-- One row per listing ever seen; locality is refreshed from the latest search result.
CREATE TABLE IF NOT EXISTS listings (
    hash_id            INTEGER PRIMARY KEY,
    category_main_cb   INTEGER NOT NULL,
    category_type_cb   INTEGER NOT NULL,
    category_sub_cb    INTEGER,
    category_sub       TEXT,               -- dispozice (2+kk, ...) or house type
    first_seen         TEXT NOT NULL,      -- date of the first run that saw it
    last_seen          TEXT NOT NULL,      -- date of the last run that saw it
    advert_name        TEXT,
    name_usable_area   INTEGER,            -- parsed from advert_name, fallback until details exist
    name_land_area     INTEGER,
    region             TEXT,
    district           TEXT,               -- okres
    city               TEXT,
    citypart           TEXT,
    quarter            TEXT,               -- e.g. Brno-Královo Pole
    street             TEXT,
    gps_lat            REAL,
    gps_lon            REAL,
    inaccuracy_type    TEXT,               -- how precise the GPS is (address/street/ward/...)
    sreality_region_id       INTEGER,
    sreality_district_id     INTEGER,
    sreality_municipality_id INTEGER,
    sreality_quarter_id      INTEGER,
    sreality_ward_id         INTEGER,
    premise_id         INTEGER,            -- real-estate agency
    user_id            INTEGER             -- broker account (pseudonymous id only)
);

-- One row per listing per day it was seen: the price history.
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_date      TEXT NOT NULL,
    hash_id            INTEGER NOT NULL,
    price_czk          INTEGER,            -- NULL = "cena na dotaz"
    price_summary_czk  INTEGER,
    price_czk_m2       INTEGER,
    price_unit         TEXT,               -- za nemovitost / za měsíc / ...
    discount_show      INTEGER,
    advert_name        TEXT,
    PRIMARY KEY (snapshot_date, hash_id)
);

-- Latest detail of each listing. raw_json keeps the (trimmed) API response for re-parsing later.
CREATE TABLE IF NOT EXISTS details (
    hash_id                INTEGER PRIMARY KEY,
    fetched_at             TEXT NOT NULL,
    http_status            INTEGER NOT NULL,
    since                  TEXT,           -- date the listing was published
    edited                 TEXT,
    usable_area            INTEGER,
    estate_area            INTEGER,        -- plocha pozemku
    floor_area             INTEGER,
    building_area          INTEGER,
    garden_area            INTEGER,
    building_condition     TEXT,           -- stav objektu
    building_type          TEXT,           -- panel / cihla / ...
    ownership              TEXT,           -- osobní / družstevní / ...
    energy_class           TEXT,           -- A..G
    energy_rating          TEXT,
    floor_number           INTEGER,
    floors                 INTEGER,
    object_type            TEXT,
    object_kind            TEXT,
    furnished              TEXT,
    elevator               TEXT,
    balcony                INTEGER,
    loggia                 INTEGER,
    terrace                INTEGER,
    cellar                 INTEGER,
    garage                 INTEGER,
    parking                INTEGER,
    low_energy             INTEGER,
    acceptance_year        INTEGER,
    reconstruction_year    INTEGER,
    price_summary_old_czk  INTEGER,        -- original price when a discount is shown
    price_note             TEXT,
    exclusively_at_rk      INTEGER,
    premise_id             INTEGER,
    raw_json               TEXT
);

-- Official municipality of each listing, from its GPS and the RÚIAN boundaries (see ruian.py).
CREATE TABLE IF NOT EXISTS listing_geo (
    hash_id      INTEGER PRIMARY KEY,
    kod_obec     INTEGER,                  -- ČSÚ/RÚIAN kód obce (Brno = 582786); NULL = no match
    kod_momc     INTEGER,                  -- Brno only: kód městské části (551317 = Brno-Chrlice)
    geo_match    TEXT,                     -- polygon | name | nearest; NULL = no match
    gps_lat      REAL,                     -- the GPS that was matched (re-matched when it changes)
    gps_lon      REAL,
    boundaries   TEXT,                     -- version of the boundary extract used
    geocoded_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snapshots_hash ON snapshots(hash_id);
CREATE INDEX IF NOT EXISTS ix_listings_last_seen ON listings(last_seen);
"""

VIEWS = """
DROP VIEW IF EXISTS v_listings;
CREATE VIEW v_listings AS
SELECT
    l.hash_id,
    CASE l.category_main_cb WHEN 1 THEN 'byt' WHEN 2 THEN 'dům' END AS property_type,
    CASE l.category_type_cb WHEN 1 THEN 'prodej' WHEN 2 THEN 'pronájem' END AS offer_type,
    l.category_sub AS disposition,
    l.advert_name,
    COALESCE(d.usable_area, l.name_usable_area) AS usable_area,
    COALESCE(d.estate_area, l.name_land_area) AS land_area,
    s.price_czk AS last_price_czk,
    s.price_unit AS last_price_unit,
    first_s.price_czk AS first_price_czk,
    l.region, l.district, l.city, l.citypart, l.quarter, l.street,
    l.gps_lat, l.gps_lon, l.inaccuracy_type,
    g.kod_obec, g.kod_momc, g.geo_match,
    d.building_condition, d.building_type, d.ownership, d.energy_class,
    d.floor_number, d.floors, d.object_type, d.furnished, d.elevator,
    d.balcony, d.loggia, d.terrace, d.cellar, d.garage, d.parking,
    d.acceptance_year, d.reconstruction_year, d.price_summary_old_czk,
    d.since AS listed_since, d.edited AS last_edited,
    l.first_seen, l.last_seen,
    -- still online = seen by the latest complete scan (or a later, possibly incomplete one)
    (l.last_seen >= (SELECT MAX(snapshot_date) FROM runs WHERE scan_complete = 1)) AS is_active,
    CAST(julianday(l.last_seen) - julianday(MIN(COALESCE(d.since, l.first_seen), l.first_seen)) AS INTEGER)
        AS days_on_market,
    l.premise_id, l.user_id,
    l.sreality_municipality_id, l.sreality_district_id, l.sreality_quarter_id, l.sreality_ward_id,
    d.fetched_at AS detail_fetched_at
FROM listings l
LEFT JOIN details d ON d.hash_id = l.hash_id AND d.raw_json IS NOT NULL
LEFT JOIN snapshots s ON s.hash_id = l.hash_id AND s.snapshot_date = l.last_seen
LEFT JOIN snapshots first_s ON first_s.hash_id = l.hash_id AND first_s.snapshot_date = l.first_seen
LEFT JOIN listing_geo g ON g.hash_id = l.hash_id;
"""

# "1 120 m²" (thousands separator), but not the "1" of a layout such as "3+1 75 m²"
_AREA_RE = re.compile(r"(?<![\d+])(\d{1,3}(?:[ \u00a0]\d{3})*)\s*m²")


def _cb(value) -> str | None:
    """Sreality code-book value {"name": ..., "value": n}; value 0 means "not filled in"."""
    if isinstance(value, dict) and value.get("value"):
        return value.get("name")
    return None


def _int(value) -> int | None:
    if value is None or value == "" or isinstance(value, (dict, list)):
        return None
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return None


def _price(value) -> int | None:
    p = _int(value)
    return p if p else None  # 0 = price on request


def _flag(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value > 0)
    return None


def parse_name_areas(name: str | None) -> tuple[int | None, int | None]:
    """'Prodej rodinného domu 120 m², pozemek 640 m²' -> (120, 640)"""
    if not name:
        return None, None
    areas = [int(re.sub(r"\D", "", m)) for m in _AREA_RE.findall(name)]
    usable = areas[0] if areas else None
    land = areas[1] if len(areas) > 1 and "pozem" in name.lower() else None
    return usable, land


def listing_row(item: dict, snapshot_date: str) -> dict:
    loc = item.get("locality") or {}
    usable, land = parse_name_areas(item.get("advert_name"))
    sub = item.get("category_sub_cb") or {}
    return {
        "hash_id": item["hash_id"],
        "category_main_cb": (item.get("category_main_cb") or {}).get("value"),
        "category_type_cb": (item.get("category_type_cb") or {}).get("value"),
        "category_sub_cb": sub.get("value"),
        "category_sub": sub.get("name"),
        "first_seen": snapshot_date,
        "last_seen": snapshot_date,
        "advert_name": item.get("advert_name"),
        "name_usable_area": usable,
        "name_land_area": land,
        "region": loc.get("region"),
        "district": loc.get("district"),
        "city": loc.get("city"),
        "citypart": loc.get("citypart"),
        "quarter": loc.get("quarter"),
        "street": loc.get("street"),
        "gps_lat": loc.get("gps_lat"),
        "gps_lon": loc.get("gps_lon"),
        "inaccuracy_type": loc.get("inaccuracy_type"),
        "sreality_region_id": loc.get("region_id"),
        "sreality_district_id": loc.get("district_id"),
        "sreality_municipality_id": loc.get("municipality_id"),
        "sreality_quarter_id": loc.get("quarter_id"),
        "sreality_ward_id": loc.get("ward_id"),
        "premise_id": item.get("premise_id"),
        "user_id": item.get("user_id"),
    }


def snapshot_row(item: dict, snapshot_date: str) -> dict:
    return {
        "snapshot_date": snapshot_date,
        "hash_id": item["hash_id"],
        "price_czk": _price(item.get("price_czk")),
        "price_summary_czk": _price(item.get("price_summary_czk")),
        "price_czk_m2": _price(item.get("price_czk_m2")),
        "price_unit": _cb(item.get("price_unit_cb")),
        "discount_show": _flag(item.get("discount_show")),
        "advert_name": item.get("advert_name"),
    }


def trim_detail(result: dict) -> dict:
    out = {k: v for k, v in result.items() if k not in DETAIL_DROP}
    if isinstance(out.get("locality"), dict):
        out["locality"] = {k: v for k, v in out["locality"].items() if k != "geometry"}
    user = result.get("user") or {}
    premise = result.get("premise") or {}
    out["user"] = {"user_id": user.get("user_id")}
    out["premise"] = {"id": premise.get("id")}
    return out


def detail_row(hash_id: int, result: dict, fetched_at: str) -> dict:
    energy = _cb(result.get("energy_efficiency_rating_cb"))
    premise = result.get("premise") or {}
    return {
        "hash_id": hash_id,
        "fetched_at": fetched_at,
        "http_status": 200,
        "since": result.get("since"),
        "edited": result.get("edited"),
        "usable_area": _int(result.get("usable_area")),
        "estate_area": _int(result.get("estate_area")),
        "floor_area": _int(result.get("floor_area")),
        "building_area": _int(result.get("building_area")),
        "garden_area": _int(result.get("garden_area")),
        "building_condition": _cb(result.get("building_condition")),
        "building_type": _cb(result.get("building_type")),
        "ownership": _cb(result.get("ownership")),
        "energy_class": energy[0] if energy and energy[0] in "ABCDEFG" else None,
        "energy_rating": energy,
        "floor_number": _int(result.get("floor_number")),
        "floors": _int(result.get("floors")),
        "object_type": _cb(result.get("object_type")),
        "object_kind": _cb(result.get("object_kind")),
        "furnished": _cb(result.get("furnished")),
        "elevator": _cb(result.get("elevator")),
        "balcony": _flag(result.get("balcony")),
        "loggia": _flag(result.get("loggia")),
        "terrace": _flag(result.get("terrace")),
        "cellar": _flag(result.get("cellar")),
        "garage": _flag(result.get("garage")),
        "parking": _flag(result.get("parking")),
        "low_energy": _flag(result.get("low_energy")),
        "acceptance_year": _int(result.get("acceptance_year")),
        "reconstruction_year": _int(result.get("reconstruction_year")),
        "price_summary_old_czk": _price(result.get("price_summary_old_czk")),
        "price_note": result.get("price_note") or None,
        "exclusively_at_rk": _flag(result.get("exclusively_at_rk")),
        "premise_id": premise.get("id"),
        "raw_json": json.dumps(trim_detail(result), ensure_ascii=False, separators=(",", ":")),
    }


def _upsert(db: sqlite3.Connection, table: str, row: dict, conflict: str, update: list[str]) -> None:
    cols = ", ".join(row)
    marks = ", ".join(f":{c}" for c in row)
    sets = ", ".join(f"{c}=excluded.{c}" for c in update)
    db.execute(
        f"INSERT INTO {table} ({cols}) VALUES ({marks}) ON CONFLICT({conflict}) DO UPDATE SET {sets}", row
    )


# Columns added after the first release: (table, column, definition). CREATE TABLE IF NOT EXISTS
# does not touch existing tables, so databases created earlier get them via ALTER TABLE.
MIGRATIONS = [
    ("runs", "scan_complete", "INTEGER NOT NULL DEFAULT 0"),
]


def _migrate(db: sqlite3.Connection) -> None:
    for table, column, definition in MIGRATIONS:
        existing = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.commit()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    _migrate(db)
    db.executescript(VIEWS)
    return db


class SrealityCollector:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.scfg = cfg.sreality
        self.db = open_db(cfg.data_dir / "sreality" / "sreality.sqlite")
        self.snapshot_date = today_local()
        self.raw_dir = cfg.data_dir / "raw" / "sreality" / self.snapshot_date
        self.stats = {"n_seen": 0, "n_new": 0, "n_details": 0}

    # ---- run bookkeeping -------------------------------------------------------------------
    def _check_cooldown(self) -> None:
        row = self.db.execute(
            "SELECT run_id, started_at, note FROM runs WHERE status = 'blocked' ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return
        blocked_at = datetime.fromisoformat(row["started_at"])
        later_ok = self.db.execute(
            "SELECT 1 FROM runs WHERE status IN ('ok', 'partial') AND run_id > ?", (row["run_id"],)
        ).fetchone()
        if not later_ok and datetime.now(timezone.utc) - blocked_at < BLOCKED_COOLDOWN:
            raise SystemExit(
                f"Sreality refused requests at {row['started_at']} ({row['note']}). Pausing for "
                f"{BLOCKED_COOLDOWN.total_seconds() / 3600:.0f} h; use --force to override."
            )

    def _start_run(self) -> int:
        cur = self.db.execute(
            "INSERT INTO runs (snapshot_date, started_at, status) VALUES (?, ?, 'running')",
            (self.snapshot_date, now_utc()),
        )
        self.db.commit()
        return cur.lastrowid

    def _finish_run(self, run_id: int, status: str, n_requests: int, note: str | None) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, n_requests=?, n_seen=?, n_new=?, n_details=?, note=? "
            "WHERE run_id=?",
            (now_utc(), status, n_requests, self.stats["n_seen"], self.stats["n_new"],
             self.stats["n_details"], note, run_id),
        )
        self.db.commit()

    # ---- main entry ------------------------------------------------------------------------
    def run(self, *, details_only: bool = False, max_details: int | None = None, force: bool = False) -> str:
        if not force:
            self._check_cooldown()
        run_id = self._start_run()
        status, notes = "failed", []
        client = PoliteClient(self.cfg.http, self.cfg.user_agent, accept="application/json")
        try:
            if details_only:
                status = "details"
            else:
                notes += self.scan_search(client)
                if not notes:
                    notes += self._check_plausibility()
                status = "partial" if notes else "ok"
                self.db.execute(
                    "UPDATE runs SET scan_complete=?, n_seen=?, n_new=? WHERE run_id=?",
                    (int(not notes), self.stats["n_seen"], self.stats["n_new"], run_id),
                )
                self.db.commit()
            limit = self.scfg.max_details_per_run if max_details is None else max_details
            if not self.fetch_details(client, limit):
                notes.append("detail downloads failing, stopped early")
        except Blocked as exc:
            status, notes = "blocked", notes + [str(exc)]
            log.error("Sreality refused our requests: %s. Stopping; next runs pause for %s.", exc, BLOCKED_COOLDOWN)
        except KeyboardInterrupt:
            status, notes = "interrupted", notes + ["stopped by user or signal"]
            raise
        except Exception as exc:
            status, notes = "failed", notes + [f"{type(exc).__name__}: {exc}"]
            log.exception("Sreality run failed")
        finally:
            self._finish_run(run_id, status, client.n_requests, "; ".join(notes) or None)
            client.close()
            log.info(
                "Sreality run %s: %s, %d requests, %d listings seen (%d new), %d details%s",
                run_id, status, client.n_requests, self.stats["n_seen"], self.stats["n_new"],
                self.stats["n_details"], f" ({'; '.join(notes)})" if notes else "",
            )
        return status

    def _check_plausibility(self) -> list[str]:
        """Guard against a silently changed API: a scan that is 'complete' by the API's own total
        but finds far fewer listings than the previous complete scan is not trusted."""
        prev = self.db.execute(
            "SELECT n_seen FROM runs WHERE scan_complete = 1 ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        if prev and self.stats["n_seen"] < 0.5 * prev["n_seen"]:
            return [f"suspicious drop: {self.stats['n_seen']} listings vs {prev['n_seen']} in the previous scan"]
        return []

    # ---- search ----------------------------------------------------------------------------
    def scan_search(self, client: PoliteClient) -> list[str]:
        """Page through all categories. Returns descriptions of categories that came back incomplete."""
        known = {r[0] for r in self.db.execute("SELECT hash_id FROM listings")}
        incomplete = []
        with JsonlGzWriter(self.raw_dir / "search.jsonl.gz") as raw:
            for main, typ in self.scfg.categories:
                label = f"{CATEGORY_MAIN.get(main, main)}/{CATEGORY_TYPE.get(typ, typ)}"
                try:
                    seen, total = self._scan_category(client, raw, main, typ, known)
                except (GaveUp, ValueError) as exc:  # ValueError = response was not JSON
                    log.error("%s: scan failed: %s", label, exc)
                    incomplete.append(f"{label}: failed ({exc})")
                    continue
                log.info("%s: %d of %d listings", label, len(seen), total)
                if total and len(seen) < self.scfg.min_completeness * total:
                    incomplete.append(f"{label}: {len(seen)}/{total}")
                elif not total:
                    incomplete.append(f"{label}: API reported 0 listings")
        return incomplete

    def _scan_category(self, client, raw, main: int, typ: int, known: set[int]) -> tuple[set[int], int]:
        seen: set[int] = set()
        offset, total = 0, 0
        while True:
            params = {
                "category_main_cb": main,
                "category_type_cb": typ,
                "locality_region_id": self.scfg.region_id,
                "limit": self.scfg.page_size,
                "offset": offset,
                "sort": "-date",
                "lang": "cs",
            }
            resp = client.get(SEARCH_URL, params=params)
            if resp.status_code != 200:
                raise GaveUp(f"search returned HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            total = (data.get("pagination") or {}).get("total") or 0
            results = data.get("results") or []
            raw.write({
                "fetched_at": now_utc(),
                "params": params,
                "pagination": data.get("pagination"),
                "results": [{k: v for k, v in r.items() if k not in SEARCH_DROP} for r in results],
            })
            for item in results:
                if "hash_id" not in item:
                    continue
                hid = item["hash_id"]
                if hid not in seen:
                    seen.add(hid)
                    self.stats["n_seen"] += 1
                if hid not in known:
                    known.add(hid)
                    self.stats["n_new"] += 1
                row = listing_row(item, self.snapshot_date)
                _upsert(self.db, "listings", row, "hash_id",
                        [c for c in row if c not in ("hash_id", "first_seen")])
                snap = snapshot_row(item, self.snapshot_date)
                _upsert(self.db, "snapshots", snap, "snapshot_date, hash_id",
                        [c for c in snap if c not in ("snapshot_date", "hash_id")])
            self.db.commit()
            offset += self.scfg.page_size
            if not results or offset >= total:
                return seen, total

    # ---- details ---------------------------------------------------------------------------
    def fetch_details(self, client: PoliteClient, limit: int) -> bool:
        """Download missing details. Returns False if it stopped early because requests kept failing."""
        if limit <= 0:
            return True
        refresh_before = None
        if self.scfg.detail_refresh_days > 0:
            refresh_before = (datetime.now(timezone.utc) - timedelta(days=self.scfg.detail_refresh_days)).isoformat()
        # Only listings still online (seen by the latest scan); brand-new ones first.
        todo = [r[0] for r in self.db.execute(
            """
            SELECT l.hash_id FROM listings l
            LEFT JOIN details d ON d.hash_id = l.hash_id
            WHERE l.last_seen = (SELECT MAX(last_seen) FROM listings)
              AND (d.hash_id IS NULL OR (? IS NOT NULL AND d.fetched_at < ?))
            ORDER BY d.hash_id IS NOT NULL, l.first_seen DESC, l.hash_id DESC
            LIMIT ?
            """,
            (refresh_before, refresh_before, limit),
        )]
        pending = self.db.execute(
            "SELECT COUNT(*) FROM listings l LEFT JOIN details d ON d.hash_id = l.hash_id "
            "WHERE d.hash_id IS NULL AND l.last_seen = (SELECT MAX(last_seen) FROM listings)"
        ).fetchone()[0]
        log.info("Fetching %d details (%d active listings without detail)", len(todo), pending)
        failures_in_a_row = 0
        with JsonlGzWriter(self.raw_dir / "details.jsonl.gz") as raw:
            for i, hash_id in enumerate(todo, 1):
                fetched_at = now_utc()
                try:
                    resp = client.get(DETAIL_URL.format(hash_id=hash_id), params={"lang": "cs"})
                    result = (resp.json() or {}).get("result") if resp.status_code == 200 else None
                except (GaveUp, ValueError) as exc:
                    resp, result = None, None
                    log.warning("detail %s: %s", hash_id, exc)
                if result:
                    row = detail_row(hash_id, result, fetched_at)
                    raw.write({"fetched_at": fetched_at, "hash_id": hash_id, "result": json.loads(row["raw_json"])})
                    failures_in_a_row = 0
                elif resp is not None and resp.status_code in (404, 410):
                    # Removed since the scan; remember it so we do not ask again.
                    row = {"hash_id": hash_id, "fetched_at": fetched_at, "http_status": resp.status_code}
                    failures_in_a_row = 0
                else:
                    # Unexpected answer: not stored, so the next run tries again.
                    if resp is not None:
                        log.warning("detail %s: HTTP %s %s", hash_id, resp.status_code, resp.text[:200])
                    failures_in_a_row += 1
                    if failures_in_a_row >= 10:
                        log.error("10 detail requests in a row failed; stopping details for this run")
                        return False
                    continue
                _upsert(self.db, "details", row, "hash_id", [c for c in row if c != "hash_id"])
                self.db.commit()
                self.stats["n_details"] += 1
                if i % 100 == 0:
                    log.info("details: %d/%d", i, len(todo))
        return True


def export_csv(cfg: Config) -> list[Path]:
    """Write analysis-ready CSVs (UTF-8, comma separated) for the ETL into data/export/sreality/."""
    db = open_db(cfg.data_dir / "sreality" / "sreality.sqlite")
    out_dir = cfg.data_dir / "export" / "sreality"
    out_dir.mkdir(parents=True, exist_ok=True)
    queries = {
        "listings.csv": "SELECT * FROM v_listings ORDER BY hash_id",
        "snapshots.csv": "SELECT * FROM snapshots ORDER BY snapshot_date, hash_id",
        "runs.csv": "SELECT * FROM runs ORDER BY run_id",
    }
    written = []
    for name, sql in queries.items():
        cur = db.execute(sql)
        path = out_dir / name
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow([c[0] for c in cur.description])
            writer.writerows(cur)
        tmp.replace(path)
        written.append(path)
    db.close()
    return written


def status(cfg: Config) -> str:
    path = cfg.data_dir / "sreality" / "sreality.sqlite"
    if not path.exists():
        return "sreality: no data yet"
    db = open_db(path)
    lines = ["sreality:"]
    for r in db.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 5"):
        lines.append(
            f"  run {r['run_id']} {r['snapshot_date']} {r['status']:<8} req={r['n_requests']} "
            f"seen={r['n_seen']} new={r['n_new']} details={r['n_details']} {r['note'] or ''}"
        )
    n_listings, n_active = db.execute(
        "SELECT COUNT(*), SUM(last_seen = (SELECT MAX(last_seen) FROM listings)) FROM listings"
    ).fetchone()
    n_details = db.execute("SELECT COUNT(*) FROM details WHERE raw_json IS NOT NULL").fetchone()[0]
    n_days = db.execute("SELECT COUNT(DISTINCT snapshot_date) FROM snapshots").fetchone()[0]
    lines.append(f"  listings: {n_listings} total, {n_active or 0} active, {n_details} with detail, {n_days} snapshot days")
    matches = db.execute(
        "SELECT COALESCE(g.geo_match, CASE WHEN g.hash_id IS NULL THEN 'not geocoded yet' ELSE 'unmatched' END), "
        "COUNT(*) "
        "FROM listings l LEFT JOIN listing_geo g ON g.hash_id = l.hash_id GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    n_geo = db.execute("SELECT COUNT(*) FROM listing_geo WHERE kod_obec IS NOT NULL").fetchone()[0]
    detail = ", ".join(f"{n} {k}" for k, n in matches)
    lines.append(f"  kod_obec: {n_geo} of {n_listings} listings ({detail})")
    db.close()
    return "\n".join(lines)
