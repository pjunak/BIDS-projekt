import io
import struct
import zipfile

from pyproj import Transformer

from bids_scrapers import ruian
from bids_scrapers.http import PoliteClient
from bids_scrapers.opendata import Store
from bids_scrapers.sreality import open_db
from conftest import Response

TO_SJTSK = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)
TO_WGS = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
BX, BY = TO_SJTSK.transform(16.6068, 49.1951)  # Brno
KX, KY = TO_SJTSK.transform(16.5317, 49.2983)  # Kuřim
PX, PY = TO_SJTSK.transform(14.4208, 50.0878)  # Praha


def square(x, y, half, hole=0.0):
    """Clockwise rings (outer, then an optional hole) around x, y."""
    def ring(h):
        return [[x - h, y - h], [x - h, y + h], [x + h, y + h], [x + h, y - h], [x - h, y - h]]
    return [ring(half)] + ([ring(hole)] if hole else [])


def au_member(level, code, name, lau, okres, rings):
    pos = lambda r: " ".join(f"{x:.2f} {y:.2f}" for x, y in r)
    interiors = "".join(
        f"<gml:interior><gml:LinearRing><gml:posList>{pos(r)}</gml:posList></gml:LinearRing></gml:interior>"
        for r in rings[1:]
    )
    return f"""<base:member><elf-au:AdministrativeUnit gml:id="AU.{level[0]}.{code}">
      <au:geometry><gml:MultiSurface srsName="http://www.opengis.net/def/crs/EPSG/0/5514"><gml:surfaceMember>
        <gml:Polygon><gml:exterior><gml:LinearRing><gml:posList>{pos(rings[0])}</gml:posList></gml:LinearRing>
        </gml:exterior>{interiors}</gml:Polygon></gml:surfaceMember></gml:MultiSurface></au:geometry>
      <au:nationalCode>{code}</au:nationalCode>
      <au:nationalLevel xlink:title="{level}" />
      <au:name><gn:GeographicalName><gn:spelling><gn:SpellingOfName><gn:text>{name}</gn:text>
      </gn:SpellingOfName></gn:spelling></gn:GeographicalName></au:name>
      <au:upperLevelUnit xlink:title="{okres}" />
      <elf-au:SHNCode><ThematicIdentifier xmlns="http://inspire.ec.europa.eu/schemas/base2/2.0">
        <identifier>{lau}</identifier></ThematicIdentifier></elf-au:SHNCode>
    </elf-au:AdministrativeUnit></base:member>"""


def au_zip() -> bytes:
    members = [
        au_member("3rdOrder", "3702", "Brno-město", "CZ0642", "Jihomoravský kraj", square(BX, BY, 5000)),
        au_member("4thOrder", "582786", "Brno", "CZ0642582786", "Brno-město", square(BX, BY, 2000)),
        au_member("4thOrder", "583251", "Kuřim", "CZ0643583251", "Brno-venkov", square(KX, KY, 1500, hole=300)),
        au_member("4thOrder", "554782", "Praha", "CZ0100554782", "Hlavní město Praha", square(PX, PY, 2000)),
        "<base:member><elf-au:AdministrativeBoundary gml:id='AB.1' /></base:member>",
    ]
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<base:SpatialDataSet xmlns:base="http://inspire.ec.europa.eu/schemas/base/3.3"
  xmlns:elf-au="http://www.locationframework.eu/schemas/AdministrativeUnits/1.0"
  xmlns:au="http://inspire.ec.europa.eu/schemas/au/4.0" xmlns:gn="http://inspire.ec.europa.eu/schemas/gn/4.0"
  xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink">
{"".join(members)}
</base:SpatialDataSet>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("1.xml", xml)
    return buf.getvalue()


def shp_zip(layer, records) -> bytes:
    """A minimal polygon shapefile (one ring per record) with text attributes in cp1250."""
    body = b""
    for i, (_, ring) in enumerate(records, 1):
        xs, ys = [p[0] for p in ring], [p[1] for p in ring]
        content = struct.pack("<i4d3i", 5, min(xs), min(ys), max(xs), max(ys), 1, len(ring), 0)
        content += b"".join(struct.pack("<2d", x, y) for x, y in ring)
        body += struct.pack(">2i", i, len(content) // 2) + content
    shp = struct.pack(">7i", 9994, 0, 0, 0, 0, 0, (100 + len(body)) // 2) + struct.pack("<2i8d", 1000, 5, *[0.0] * 8)
    fields = [("KOD", 10), ("NAZEV", 40), ("OBEC_KOD", 10)]
    dbf = struct.pack("<4BIHH20x", 3, 126, 10, 21, len(records), 33 + 32 * len(fields), 1 + sum(n for _, n in fields))
    dbf += b"".join(name.encode().ljust(11, b"\0") + b"C" + bytes(4) + bytes([n, 0]) + bytes(14) for name, n in fields)
    dbf += b"\r" + b"".join(
        b" " + b"".join(str(attrs[name]).encode("cp1250").ljust(n) for name, n in fields) for attrs, _ in records
    ) + b"\x1a"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"582786/{layer}.shp", shp + body)
        zf.writestr(f"582786/{layer}.dbf", dbf)
        zf.writestr(f"582786/{layer}.cpg", "1250\n")
    return buf.getvalue()


def brno_zip() -> bytes:
    west = [[BX - 2000, BY - 2000], [BX - 2000, BY + 2000], [BX, BY + 2000], [BX, BY - 2000], [BX - 2000, BY - 2000]]
    east = [[x + 2000, y] for x, y in west]
    return shp_zip("MOMC_P", [
        ({"KOD": 550973, "NAZEV": "Brno-střed", "OBEC_KOD": 582786}, west),
        ({"KOD": 551287, "NAZEV": "Brno-Líšeň", "OBEC_KOD": 582786}, east),
    ])


def collect(cfg, server, monkeypatch):
    monkeypatch.setattr(ruian, "AU_URL", server.url + "/au.zip")
    monkeypatch.setattr(ruian, "BRNO_SHP_URL", server.url + "/582786.zip")
    server.routes["/au.zip"] = Response(200, au_zip(), {"ETag": '"a1"'})
    server.routes["/582786.zip"] = Response(200, brno_zip(), {"ETag": '"b1"'})
    with PoliteClient(cfg.http, "test") as client:
        return ruian.collect(client, Store(cfg.data_dir, "ruian"), Store(cfg.data_dir, "csu"))


def add_listing(db, hash_id, x, y, *, inaccuracy="street", district=None, city=None, quarter=None):
    lon, lat = TO_WGS.transform(x, y)
    db.execute(
        "INSERT INTO listings (hash_id, category_main_cb, category_type_cb, first_seen, last_seen, gps_lat, gps_lon, "
        "inaccuracy_type, district, city, quarter) VALUES (?, 1, 1, '2026-10-21', '2026-10-21', ?, ?, ?, ?, ?, ?)",
        (hash_id, lat, lon, inaccuracy, district, city, quarter),
    )


def test_collect_builds_jmk_extract_and_respects_refresh_window(cfg, server, monkeypatch):
    assert collect(cfg, server, monkeypatch) == {ruian.AU_FILE: "new", ruian.BRNO_FILE: "new"}
    extract = cfg.data_dir / "extract" / "ruian"
    obce = ruian.Areas(extract / ruian.OBCE_FILE)
    assert sorted((p["kod_obec"], p["nazev"], p["kod_okres"], p["okres"]) for _, _, p in obce.items) == [
        (582786, "Brno", "CZ0642", "Brno-město"),
        (583251, "Kuřim", "CZ0643", "Brno-venkov"),
    ]
    momc = ruian.Areas(extract / ruian.MOMC_FILE)
    assert sorted(p["nazev"] for _, _, p in momc.items) == ["Brno-Líšeň", "Brno-střed"]

    n = len(server.requests)
    assert collect(cfg, server, monkeypatch) == {}  # checked recently: no request at all
    assert len(server.requests) == n


def test_geocode_methods_and_incremental_runs(cfg, server, monkeypatch):
    collect(cfg, server, monkeypatch)
    db = open_db(cfg.data_dir / "sreality" / "sreality.sqlite")
    add_listing(db, 1, BX + 500, BY)                                            # Brno, east district
    add_listing(db, 2, BX - 500, BY, inaccuracy="municipality", quarter="Brno-Líšeň")  # city-precise point
    add_listing(db, 3, BX - 500, BY, inaccuracy="municipality")                 # ... and no quarter name
    add_listing(db, 4, KX, KY, city="Kuřim", district="Brno-venkov")            # in the hole: by name
    add_listing(db, 5, KX + 1800, KY, city="Jinde", district="Brno-venkov")     # 300 m outside Kuřim
    add_listing(db, 6, KX + 1800, KY, city="Jinde", district="Žďár nad Sázavou")  # ... in another district
    add_listing(db, 7, PX, PY, city="Praha", district="Praha")
    db.commit()

    counts = ruian.geocode(cfg)
    assert counts == {"polygon": 3, "name": 1, "nearest": 1, "unmatched": 2}
    rows = {r[0]: tuple(r[1:]) for r in db.execute("SELECT hash_id, kod_obec, kod_momc, geo_match FROM v_listings")}
    assert rows == {
        1: (582786, 551287, "polygon"),
        2: (582786, 551287, "polygon"),
        3: (582786, None, "polygon"),
        4: (583251, None, "name"),
        5: (583251, None, "nearest"),
        6: (None, None, None),
        7: (None, None, None),
    }

    assert ruian.geocode(cfg) == {}  # nothing new
    db.execute("UPDATE listings SET gps_lat = gps_lat + 0.0001 WHERE hash_id = 1")
    db.commit()
    assert ruian.geocode(cfg) == {"polygon": 1}
    assert sum(ruian.geocode(cfg, redo=True).values()) == 7
    db.close()


def test_geocode_without_boundaries_raises(cfg):
    import pytest

    with pytest.raises(ruian.NoBoundaries):
        ruian.geocode(cfg)
