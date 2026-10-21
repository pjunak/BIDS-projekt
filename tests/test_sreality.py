"""Sreality collector against a fake API that mimics /api/v1/estates/search and /estates/{id}."""

import csv
import json

import pytest

from bids_scrapers import sreality
from bids_scrapers.sreality import (
    SrealityCollector,
    detail_row,
    export_csv,
    open_db,
    parse_name_areas,
    trim_detail,
)
from conftest import Response

SEARCH = "/api/v1/estates/search"


class FakeSreality:
    def __init__(self, server):
        self.listings: dict[int, dict] = {}  # hash_id -> {"main", "type", "price", "area"}
        self.search_override: Response | None = None
        self.serve_at_most: int | None = None  # simulate a scan that loses items
        self.detail_override: dict[int, Response] = {}
        server.routes[SEARCH] = self.search
        server.routes["/api/v1/estates/*"] = self.detail

    def add(self, hash_id, main=1, typ=1, price=5_000_000, area=58):
        self.listings[hash_id] = {"main": main, "type": typ, "price": price, "area": area}

    def item(self, hid):
        li = self.listings[hid]
        return {
            "hash_id": hid,
            "advert_name": f"Prodej bytu 2+kk {li['area']} m²",
            "advert_images": ["//img"],
            "category_main_cb": {"name": "Byty", "value": li["main"]},
            "category_type_cb": {"name": "Prodej", "value": li["type"]},
            "category_sub_cb": {"name": "2+kk", "value": 4},
            "locality": {"city": "Brno", "district": "Brno-město", "region": "Jihomoravský kraj",
                         "region_id": 14, "gps_lat": 49.2, "gps_lon": 16.6, "inaccuracy_type": "street"},
            "price_czk": li["price"],
            "price_unit_cb": {"name": "za nemovitost", "value": 1},
            "premise": {"seo_name": "agency"},
            "premise_id": 7,
            "user_id": 8,
        }

    def search(self, req):
        if self.search_override:
            return self.search_override
        q = {k: v[0] for k, v in req.query.items()}
        ids = sorted(
            (h for h, li in self.listings.items()
             if li["main"] == int(q["category_main_cb"]) and li["type"] == int(q["category_type_cb"])),
            reverse=True,
        )
        total = len(ids)
        if self.serve_at_most is not None:
            ids = ids[: self.serve_at_most]
        offset, limit = int(q["offset"]), int(q["limit"])
        page = [self.item(h) for h in ids[offset: offset + limit]]
        body = {"pagination": {"limit": limit, "offset": offset, "total": total}, "results": page}
        return Response(200, json.dumps(body), {"Content-Type": "application/json"})

    def detail(self, req):
        hid = int(req.path.rsplit("/", 1)[-1])
        if hid in self.detail_override:
            return self.detail_override[hid]
        if hid not in self.listings:
            return Response(404, json.dumps({"status_code": 404}))
        li = self.listings[hid]
        result = {
            "hash_id": hid,
            "since": "2026-10-01",
            "usable_area": li["area"],
            "building_condition": {"name": "Novostavba", "value": 6},
            "ownership": {"name": "Osobní", "value": 1},
            "energy_efficiency_rating_cb": {"name": "B - Velmi úsporná", "value": 2},
            "furnished": {"name": "- vyber vybavení", "value": 0},
            "advert_description": "Volejte 777 123 456",
            "user": {"user_id": 8, "user_name": "Jana Nováková", "user_email": "jana@example.cz",
                     "user_phones": [{"phone": "+420777123456"}]},
            "premise": {"id": 7, "name": "Reality Nováková", "company": {"company_ic": 123}},
            "advert_images": ["//img"],
        }
        return Response(200, json.dumps({"result": result}), {"Content-Type": "application/json"})


@pytest.fixture
def api(server, monkeypatch):
    monkeypatch.setattr(sreality, "SEARCH_URL", server.url + SEARCH)
    monkeypatch.setattr(sreality, "DETAIL_URL", server.url + "/api/v1/estates/{hash_id}")
    return FakeSreality(server)


@pytest.fixture
def scfg(cfg):
    cfg.sreality.categories = [[1, 1]]
    cfg.sreality.page_size = 60
    cfg.sreality.max_details_per_run = 1000
    return cfg


def run_on(monkeypatch, cfg, day, **kwargs):
    monkeypatch.setattr(sreality, "today_local", lambda: day)
    return SrealityCollector(cfg).run(**kwargs)


def db_of(cfg):
    return open_db(cfg.data_dir / "sreality" / "sreality.sqlite")


# ---- parsing -------------------------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("Prodej bytu 2+kk 58 m²", (58, None)),
    ("Pronájem bytu 3+1 75 m²", (75, None)),
    ("Prodej bytu 2+1 120 m²", (120, None)),
    ("Prodej rodinného domu 1 120 m², pozemek 1 640 m²", (1120, 1640)),
    ("Prodej rodinného domu 6+1 1 200 m², pozemek 12 500 m²", (1200, 12500)),
    ("Prodej bytu 1+kk 25 m²", (25, None)),
    ("Prodej chaty", (None, None)),
    (None, (None, None)),
])
def test_parse_name_areas(name, expected):
    assert parse_name_areas(name) == expected


def test_detail_trimming_drops_personal_data():
    result = {
        "since": "2026-10-01",
        "usable_area": 58,
        "energy_efficiency_rating_cb": {"name": "C - Úsporná", "value": 3},
        "ownership": {"name": "- vyber", "value": 0},
        "advert_description": "Volejte 777 123 456",
        "user": {"user_id": 8, "user_name": "Jana", "user_email": "jana@example.cz", "user_phones": ["+420"]},
        "premise": {"id": 7, "name": "Reality Jana", "company": {"company_ic": 1}},
        "labels": [{"poi": 1}],
        "locality": {"city": "Brno", "geometry": {"big": True}},
    }
    row = detail_row(1, result, "2026-10-21T00:00:00+00:00")
    assert row["energy_class"] == "C"
    assert row["ownership"] is None  # value 0 = not filled in
    assert row["premise_id"] == 7
    raw = row["raw_json"]
    for secret in ("jana@example.cz", "Jana", "777 123 456", "Reality Jana", "geometry", "labels"):
        assert secret not in raw
    assert trim_detail(result)["user"] == {"user_id": 8}


# ---- runs ------------------------------------------------------------------------------------

def test_first_run_scans_all_pages_and_fetches_details(monkeypatch, scfg, api, server):
    for h in range(1, 131):
        api.add(h, price=0 if h == 5 else 5_000_000 + h)
    assert run_on(monkeypatch, scfg, "2026-10-20") == "ok"

    assert len(server.hits(SEARCH)) == 3  # 130 listings / 60 per page
    db = db_of(scfg)
    run = db.execute("SELECT * FROM runs").fetchone()
    assert (run["status"], run["scan_complete"], run["n_seen"], run["n_new"], run["n_details"]) == ("ok", 1, 130, 130, 130)
    assert db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 130
    assert db.execute("SELECT price_czk FROM snapshots WHERE hash_id = 5").fetchone()[0] is None  # na dotaz
    v = db.execute("SELECT * FROM v_listings WHERE hash_id = 7").fetchone()
    assert (v["usable_area"], v["building_condition"], v["energy_class"], v["listed_since"]) == (58, "Novostavba", "B", "2026-10-01")
    assert v["is_active"] == 1 and v["days_on_market"] == 19


def test_second_run_tracks_new_removed_and_price_changes(monkeypatch, scfg, api, server):
    for h in range(1, 11):
        api.add(h)
    run_on(monkeypatch, scfg, "2026-10-20")
    del api.listings[3]
    api.add(11)
    api.listings[4]["price"] = 4_500_000
    n_detail_requests = len([r for r in server.requests if r.path != SEARCH])

    assert run_on(monkeypatch, scfg, "2026-10-21") == "ok"
    assert len([r for r in server.requests if r.path != SEARCH]) == n_detail_requests + 1  # only the new one

    db = db_of(scfg)
    rows = {r["hash_id"]: r for r in db.execute("SELECT * FROM v_listings")}
    assert rows[3]["is_active"] == 0 and rows[3]["last_seen"] == "2026-10-20"
    assert rows[11]["first_seen"] == "2026-10-21" and rows[11]["is_active"] == 1
    assert (rows[4]["first_price_czk"], rows[4]["last_price_czk"]) == (5_000_000, 4_500_000)
    assert db.execute("SELECT n_new FROM runs ORDER BY run_id DESC").fetchone()[0] == 1

    paths = export_csv(scfg)
    with open(paths[0], newline="", encoding="utf-8") as fh:
        exported = list(csv.DictReader(fh))
    assert len(exported) == 11 and "jana@example.cz" not in paths[0].read_text()


def test_missing_and_failing_details(monkeypatch, scfg, api):
    for h in (1, 2, 3):
        api.add(h)
    api.detail_override[2] = Response(404, "{}")
    api.detail_override[3] = Response(500, "oops")
    assert run_on(monkeypatch, scfg, "2026-10-20") == "ok"
    db = db_of(scfg)
    stored = {r["hash_id"]: r["http_status"] for r in db.execute("SELECT * FROM details")}
    assert stored == {1: 200, 2: 404}  # the 500 is not stored, so it is retried next run

    del api.detail_override[3]
    run_on(monkeypatch, scfg, "2026-10-21")
    assert db_of(scfg).execute("SELECT raw_json IS NOT NULL FROM details WHERE hash_id = 3").fetchone()[0] == 1


def test_stops_details_after_repeated_failures(monkeypatch, scfg, api):
    for h in range(1, 30):
        api.add(h)
        api.detail_override[h] = Response(400, "bad")
    assert run_on(monkeypatch, scfg, "2026-10-20") == "ok"
    run = db_of(scfg).execute("SELECT * FROM runs").fetchone()
    assert "stopped early" in run["note"]


def test_incomplete_scan_is_partial(monkeypatch, scfg, api):
    for h in range(1, 101):
        api.add(h)
    api.serve_at_most = 60  # API says 100, but only 60 come back
    assert run_on(monkeypatch, scfg, "2026-10-20") == "partial"
    run = db_of(scfg).execute("SELECT * FROM runs").fetchone()
    assert run["scan_complete"] == 0 and "60/100" in run["note"]


def test_suspicious_drop_is_not_trusted(monkeypatch, scfg, api):
    for h in range(1, 101):
        api.add(h)
    run_on(monkeypatch, scfg, "2026-10-20")
    for h in range(1, 71):
        del api.listings[h]  # API "changed": suddenly 30 listings, but consistent with its own total
    assert run_on(monkeypatch, scfg, "2026-10-21") == "partial"
    db = db_of(scfg)
    assert "suspicious drop" in db.execute("SELECT note FROM runs ORDER BY run_id DESC").fetchone()[0]
    # The last trusted scan is still 2026-10-20, so listings not seen on the 21st stay active.
    assert db.execute("SELECT COUNT(*) FROM v_listings WHERE is_active = 1").fetchone()[0] == 100


def test_blocked_run_pauses_next_runs(monkeypatch, scfg, api):
    api.add(1)
    api.search_override = Response(403, "forbidden")
    assert run_on(monkeypatch, scfg, "2026-10-20") == "blocked"
    with pytest.raises(SystemExit):
        run_on(monkeypatch, scfg, "2026-10-21")
    api.search_override = None
    assert run_on(monkeypatch, scfg, "2026-10-21", force=True) == "ok"
    assert run_on(monkeypatch, scfg, "2026-10-22") == "ok"  # a good run lifts the pause


def test_interrupted_run_is_recorded(monkeypatch, scfg, api):
    for h in (1, 2):
        api.add(h)

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(SrealityCollector, "fetch_details", boom)
    with pytest.raises(KeyboardInterrupt):
        run_on(monkeypatch, scfg, "2026-10-20")
    run = db_of(scfg).execute("SELECT * FROM runs").fetchone()
    assert run["status"] == "interrupted" and run["scan_complete"] == 1


def test_migration_adds_columns_to_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE runs (run_id INTEGER PRIMARY KEY, snapshot_date TEXT NOT NULL, started_at TEXT NOT NULL, "
                "finished_at TEXT, status TEXT NOT NULL, n_requests INTEGER NOT NULL DEFAULT 0, n_seen INTEGER NOT NULL "
                "DEFAULT 0, n_new INTEGER NOT NULL DEFAULT 0, n_details INTEGER NOT NULL DEFAULT 0, note TEXT)")
    old.execute("INSERT INTO runs (snapshot_date, started_at, status) VALUES ('2026-10-01', 'x', 'ok')")
    old.commit()
    old.close()
    db = open_db(path)
    assert db.execute("SELECT scan_complete FROM runs").fetchone()[0] == 0
