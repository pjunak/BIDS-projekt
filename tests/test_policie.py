import csv
import io
import json
import sqlite3
import zipfile

from bids_scrapers import policie
from bids_scrapers.http import PoliteClient
from bids_scrapers.opendata import Store
from conftest import Response

# Two Voronoi cells in JMK (Brno district, Kuřim) and one outside (Praha).
CELLS = [
    (1, "582786", "551082", 16.606836, 49.195061),
    (2, "583251", "", 16.531667, 49.298333),
    (3, "554782", "500054", 14.42076, 50.087811),
]


def month_zip(rows) -> bytes:
    out = io.StringIO()
    w = csv.writer(out, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(["id", "x", "y", "mp", "date", "state", "relevance", "types"])
    w.writerows(rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("202609.csv", out.getvalue())
        zf.writestr("types.csv", 'id,label\r\n97,"Přestupky"\r\n')
        zf.writestr("states.csv", "id,label\r\n1,objasněno\r\n")
    return buf.getvalue()


def voronoi_zip(tmp_path) -> bytes:
    gpkg = tmp_path / "voronoi.gpkg"
    db = sqlite3.connect(gpkg)
    db.execute("CREATE TABLE gpkg_contents (table_name TEXT, data_type TEXT)")
    db.execute("INSERT INTO gpkg_contents VALUES ('voronoi', 'features')")
    db.execute("CREATE TABLE voronoi (fid INTEGER PRIMARY KEY, geom BLOB, id INTEGER, kod_zuj TEXT, "
               "kod_obec TEXT, kod_momc TEXT, def_bod_x REAL, def_bod_y REAL)")
    for cid, obec, momc, x, y in CELLS:
        db.execute("INSERT INTO voronoi (id, kod_zuj, kod_obec, kod_momc, def_bod_x, def_bod_y) VALUES (?,?,?,?,?,?)",
                   (cid, momc or obec, obec, momc, x, y))
    db.commit()
    db.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.write(gpkg, "voronoi.gpkg")
    return buf.getvalue()


def csu_codelist(cfg):
    store = Store(cfg.data_dir, "csu")
    items = [
        {"kod": "582786", "nazev": "Brno", "nadrazenaPolozkaKod": "CZ0642"},
        {"kod": "583251", "nazev": "Kuřim", "nadrazenaPolozkaKod": "CZ0643"},
        {"kod": "554782", "nazev": "Praha", "nadrazenaPolozkaKod": "CZ0100"},
    ]
    store.path("ciselnik_obce_okresy.json").write_text(json.dumps(items), encoding="utf-8")
    store.entries["ciselnik_obce_okresy.json"] = {"sha256": "x"}
    return store


ROWS = [
    (11, "16.606836", "49.195061", "false", "2026-09-01T10:00:00.0000+02:00", 1, 4, 97),  # Brno
    (11, "16.606836", "49.195061", "false", "2026-09-01T10:00:00.0000+02:00", 1, 4, 74),  # same offence, 2nd type
    (12, "16.531667", "49.298333", "true", "2026-09-02T10:00:00.0000+02:00", 2, 4, 97),   # Kuřim
    (13, "14.420760", "50.087811", "false", "2026-09-03T10:00:00.0000+02:00", 1, 4, 97),  # Praha
    (14, "15.000000", "49.000000", "false", "2026-09-04T10:00:00.0000+02:00", 1, 4, 97),  # no cell
]


def test_collect_downloads_and_extracts_jmk(cfg, server, monkeypatch, tmp_path):
    monkeypatch.setattr(policie, "INDEX_URL", server.url + "/api/v2/downloads")
    monkeypatch.setattr(policie, "MONTH_URL", server.url + "/api/v2/downloads/{name}.zip")
    monkeypatch.setattr(policie, "VORONOI_URL", server.url + "/voronoi.gpkg.zip")
    index = {"data": [
        {"name": "201201", "updated": "2026-10-21T00:30:00"},  # before FIRST_MONTH: skipped
        {"name": "202609", "updated": "2026-10-21T00:30:00"},
    ]}
    server.routes["/api/v2/downloads"] = lambda req: Response(200, json.dumps(index), {"ETag": '"i1"'})
    server.routes["/api/v2/downloads/202609.zip"] = Response(200, month_zip(ROWS), {"ETag": '"m1"'})
    server.routes["/voronoi.gpkg.zip"] = Response(200, voronoi_zip(tmp_path), {"Last-Modified": "Wed, 19 Oct 2022 10:46:15 GMT"})

    store = Store(cfg.data_dir, "policie")
    client = PoliteClient(cfg.http, cfg.user_agent)
    res = policie.collect(client, store, csu_codelist(cfg))
    assert res["mesice/202609.zip"] == "new" and "mesice/201201.zip" not in res

    out = cfg.data_dir / "extract" / "policie" / "jmk" / "202609.csv"
    with open(out, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [(r["id"], r["kod_obec"], r["kod_momc"]) for r in rows] == [
        ("11", "582786", "551082"), ("11", "582786", "551082"), ("12", "583251", "")]
    assert (out.parent / "types.csv").exists()

    # Second run: nothing changed -> no month download, no re-extract.
    n = len(server.requests)
    res = policie.collect(client, store, csu_codelist(cfg))
    month_hits = [r for r in server.requests[n:] if r.path.endswith(".zip") and "voronoi" not in r.path]
    assert month_hits == []

    # The police regenerated the month: re-checked with its ETag.
    index["data"][1]["updated"] = "2026-10-22T00:30:00"
    policie.collect(client, store, csu_codelist(cfg))
    assert server.hits("/api/v2/downloads/202609.zip")[-1].headers.get("If-None-Match") == '"m1"'


def test_month_cutoff(monkeypatch):
    import datetime as dt

    class FakeDate(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 21)

    monkeypatch.setattr(policie, "date", FakeDate)
    assert policie._month_cutoff(12) == "202510"
    assert policie._month_cutoff(10) == "202512"
    assert policie._month_cutoff(0) == "202610"
