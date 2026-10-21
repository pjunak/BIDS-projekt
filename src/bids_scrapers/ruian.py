"""ČÚZK RÚIAN boundaries, and the official municipality code of every Sreality listing.

Sreality has no ČSÚ/RÚIAN code, only names (which repeat: there are two Březina in okres
Brno-venkov) and GPS. So the official boundaries are downloaded and every listing's GPS point is
matched to them.

Raw (data/raw/ruian/):
  * inspire_au.zip       INSPIRE Administrative Units of the whole of Czechia (GML 3.2, S-JTSK =
                         EPSG:5514): state, kraje, okresy and all obce with their original RÚIAN
                         boundaries, ~78 MB
  * obec_582786_shp.zip  RÚIAN data of Brno as shapefiles (S-JTSK); only the 29 městské části
                         (layer MOMC_P) are used, ~9 MB
  ČÚZK regenerates both every night, but boundaries change only a few times a year, so they are
  re-checked every REFRESH_DAYS and older versions are not kept.

Extract (data/extract/ruian/), coordinates in S-JTSK as in the source:
  * obce_jmk.geojson     the 673 JMK municipalities: kod_obec, nazev, kod_okres (LAU 1, CZ064x), okres
  * momc_brno.geojson    the 29 Brno districts: kod_momc, nazev, kod_obec

Geocoding (table listing_geo in the Sreality database): GPS (WGS84) -> S-JTSK with pyproj (EPSG
transformation "S-JTSK to WGS 84 (5)", about 1 m), then point-in-polygon. A listing whose point is
outside every JMK municipality is matched by Sreality's city + district names, then to the nearest
municipality of the same district within NEAREST_MAX_M. Brno districts come from the point too,
except when the point is only city-precise (inaccuracy_type = municipality): then from the
Sreality quarter name, if it names one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from pyproj import Transformer

from . import csu
from .config import Config
from .http import PoliteClient
from .opendata import CHANGED, NEW, Store
from .sreality import _upsert, open_db
from .util import now_utc

log = logging.getLogger(__name__)

BRNO = 582786
AU_URL = "https://services.cuzk.gov.cz/gml/inspire/au/epsg-5514/1.zip"
BRNO_SHP_URL = f"https://services.cuzk.gov.cz/shp/obec/epsg-5514/{BRNO}.zip"
AU_FILE = "inspire_au.zip"
BRNO_FILE = f"obec_{BRNO}_shp.zip"
OBCE_FILE = "obce_jmk.geojson"
MOMC_FILE = "momc_brno.geojson"
REFRESH_DAYS = 30
NEAREST_MAX_M = 1000.0

NS = {
    "gml": "http://www.opengis.net/gml/3.2",
    "au": "http://inspire.ec.europa.eu/schemas/au/4.0",
    "elf": "http://www.locationframework.eu/schemas/AdministrativeUnits/1.0",
    "base": "http://inspire.ec.europa.eu/schemas/base/3.3",
    "base2": "http://inspire.ec.europa.eu/schemas/base2/2.0",
    "gn": "http://inspire.ec.europa.eu/schemas/gn/4.0",
}
XLINK_TITLE = "{http://www.w3.org/1999/xlink}title"


class NoBoundaries(RuntimeError):
    """The boundary extract has not been built yet."""


# ---- reading the sources -----------------------------------------------------------------------
def _ring(pos_list: str) -> list[list[float]]:
    v = [float(t) for t in pos_list.split()]
    return [[v[i], v[i + 1]] for i in range(0, len(v), 2)]


def read_inspire_obce(path: Path, lau_prefix: str) -> list[dict]:
    """Municipalities (4th order units) whose LAU code starts with lau_prefix (CZ064 = JMK)."""
    obce = []
    member = f"{{{NS['base']}}}member"
    with zipfile.ZipFile(path) as zf, zf.open(next(n for n in zf.namelist() if n.endswith(".xml"))) as fh:
        for _, el in ET.iterparse(fh):
            if el.tag != member:
                continue
            unit = el.find("elf:AdministrativeUnit", NS)
            if unit is not None and unit.find("au:nationalLevel", NS).get(XLINK_TITLE) == "4thOrder":
                lau = unit.findtext("elf:SHNCode//base2:identifier", "", NS)  # CZ0642582786
                if lau.startswith(lau_prefix):
                    surface = unit.find("au:geometry/gml:MultiSurface", NS)
                    if not surface.get("srsName", "").endswith("5514"):
                        raise ValueError(f"unexpected coordinate system {surface.get('srsName')}")
                    obce.append({
                        "kod_obec": int(unit.findtext("au:nationalCode", namespaces=NS)),
                        "nazev": unit.findtext("au:name//gn:text", namespaces=NS),
                        "kod_okres": lau[:6],
                        "okres": unit.find("au:upperLevelUnit", NS).get(XLINK_TITLE),
                        "polygons": [
                            [_ring(p.findtext("gml:exterior//gml:posList", namespaces=NS))]
                            + [_ring(r.text) for r in p.iterfind("gml:interior//gml:posList", NS)]
                            for p in surface.iter(f"{{{NS['gml']}}}Polygon")
                        ],
                    })
            el.clear()
    return obce


def _read_dbf(data: bytes, encoding: str) -> list[dict]:
    n, header_len, record_len = struct.unpack("<IHH", data[4:12])
    fields, pos = [], 32
    while data[pos] != 0x0D:
        fields.append((data[pos:pos + 11].split(b"\0")[0].decode("ascii"), data[pos + 16]))
        pos += 32
    rows = []
    for i in range(n):
        pos = header_len + i * record_len + 1  # skip the deletion flag
        row = {}
        for name, length in fields:
            row[name] = data[pos:pos + length].decode(encoding).strip()
            pos += length
        rows.append(row)
    return rows


def _signed_area(ring: list[list[float]]) -> float:
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in pairwise(ring)) / 2


def _read_shp_polygons(data: bytes) -> list[list]:
    """Polygon records of a .shp file as GeoJSON MultiPolygon coordinates."""
    shapes, pos = [], 100
    while pos < len(data):
        length = struct.unpack(">i", data[pos + 4:pos + 8])[0] * 2
        rec = data[pos + 8:pos + 8 + length]
        pos += 8 + length
        shape_type = struct.unpack("<i", rec[:4])[0]
        if shape_type == 0:
            shapes.append([])
            continue
        if shape_type not in (5, 15, 25):
            raise ValueError(f"shape type {shape_type} is not a polygon")
        n_parts, n_points = struct.unpack("<ii", rec[36:44])
        parts = list(struct.unpack(f"<{n_parts}i", rec[44:44 + 4 * n_parts])) + [n_points]
        xy = struct.unpack(f"<{2 * n_points}d", rec[44 + 4 * n_parts:44 + 4 * n_parts + 16 * n_points])
        polygons: list[list] = []
        for a, b in pairwise(parts):
            ring = [[xy[2 * i], xy[2 * i + 1]] for i in range(a, b)]
            if _signed_area(ring) < 0 or not polygons:  # clockwise = outer ring, else a hole
                polygons.append([ring])
            else:
                polygons[-1].append(ring)
        shapes.append(polygons)
    return shapes


def read_shp(path: Path, layer: str) -> list[dict]:
    """Attributes + polygons of one layer of a zipped shapefile set."""
    with zipfile.ZipFile(path) as zf:
        names = {n.rsplit("/", 1)[-1]: n for n in zf.namelist()}
        codepage = zf.read(names[f"{layer}.cpg"]).decode("ascii").strip() if f"{layer}.cpg" in names else "utf-8"
        rows = _read_dbf(zf.read(names[f"{layer}.dbf"]), f"cp{codepage}" if codepage.isdigit() else codepage)
        shapes = _read_shp_polygons(zf.read(names[f"{layer}.shp"]))
    return [dict(row, polygons=polygons) for row, polygons in zip(rows, shapes, strict=True)]


# ---- download + extract ------------------------------------------------------------------------
def _due(store: Store, rel: str) -> bool:
    if not store.has(rel):
        return True
    checked = datetime.fromisoformat(store.entries[rel]["checked_at"])
    return datetime.now(timezone.utc) - checked >= timedelta(days=REFRESH_DAYS)


def _write_geojson(path: Path, features: list[dict]) -> None:
    collection = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::5514"}},
        "features": [
            {
                "type": "Feature",
                "properties": {k: v for k, v in f.items() if k != "polygons"},
                "geometry": {"type": "MultiPolygon", "coordinates": f["polygons"]},
            }
            for f in features
        ],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def build_extract(store: Store, out_dir: Path, csu_store: Store | None = None) -> None:
    obce = sorted(read_inspire_obce(store.path(AU_FILE), csu.JMK_NUTS), key=lambda o: o["kod_obec"])
    momc = sorted(
        (
            {"kod_momc": int(r["KOD"]), "nazev": r["NAZEV"], "kod_obec": int(r["OBEC_KOD"]), "polygons": r["polygons"]}
            for r in read_shp(store.path(BRNO_FILE), "MOMC_P")
        ),
        key=lambda m: m["kod_momc"],
    )
    if csu_store is not None and csu_store.has("ciselnik_obce_okresy.json"):
        expected = {int(k) for k in csu.jmk_municipalities(csu_store)}
        found = {o["kod_obec"] for o in obce}
        if expected != found:
            log.warning(
                "RÚIAN and the ČSÚ code list differ: missing in RÚIAN %s, not in ČSÚ %s",
                sorted(expected - found), sorted(found - expected),
            )
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_geojson(out_dir / OBCE_FILE, obce)
    _write_geojson(out_dir / MOMC_FILE, momc)
    log.info("RÚIAN extract: %d JMK municipalities, %d Brno districts -> %s", len(obce), len(momc), out_dir)


def collect(client: PoliteClient, store: Store, csu_store: Store, *, rebuild_extract: bool = False) -> dict[str, str]:
    results = {}
    for rel, url in ((AU_FILE, AU_URL), (BRNO_FILE, BRNO_SHP_URL)):
        if _due(store, rel):
            results[rel] = store.fetch(client, url, rel, keep_history=False)
    extract_dir = store.root.parent.parent / "extract" / "ruian"
    missing = not all((extract_dir / name).exists() for name in (OBCE_FILE, MOMC_FILE))
    if rebuild_extract or missing or any(r in (NEW, CHANGED) for r in results.values()):
        build_extract(store, extract_dir, csu_store)
    return results


# ---- geocoding ---------------------------------------------------------------------------------
def _inside(x: float, y: float, rings: list) -> bool:
    """Ray casting, even-odd rule over all rings, so holes and multipolygons need no special case."""
    inside = False
    for ring in rings:
        x1, y1 = ring[-1]
        for x2, y2 in ring:
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                inside = not inside
            x1, y1 = x2, y2
    return inside


def _distance(x: float, y: float, rings: list) -> float:
    best = math.inf
    for ring in rings:
        for (ax, ay), (bx, by) in pairwise(ring):
            dx, dy = bx - ax, by - ay
            seg = dx * dx + dy * dy
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / seg)) if seg else 0.0
            best = min(best, math.hypot(x - ax - t * dx, y - ay - t * dy))
    return best


def _norm(name: str | None) -> str:
    return " ".join((name or "").casefold().replace("okres ", "").split())


class Areas:
    """Polygons from an extract GeoJSON, with a bounding-box prefilter."""

    def __init__(self, path: Path):
        self.items = []
        for f in json.loads(path.read_text(encoding="utf-8"))["features"]:
            rings = [ring for polygon in f["geometry"]["coordinates"] for ring in polygon]
            xs = [p[0] for ring in rings for p in ring]
            ys = [p[1] for ring in rings for p in ring]
            self.items.append(((min(xs), min(ys), max(xs), max(ys)), rings, f["properties"]))

    def locate(self, x: float, y: float) -> dict | None:
        for (x0, y0, x1, y1), rings, props in self.items:
            if x0 <= x <= x1 and y0 <= y <= y1 and _inside(x, y, rings):
                return props
        return None

    def nearest(self, x: float, y: float, max_dist: float, accept=lambda props: True) -> dict | None:
        best, best_dist = None, max_dist
        for (x0, y0, x1, y1), rings, props in self.items:
            if x0 - best_dist <= x <= x1 + best_dist and y0 - best_dist <= y <= y1 + best_dist and accept(props):
                dist = _distance(x, y, rings)
                if dist <= best_dist:
                    best, best_dist = props, dist
        return best


class Geocoder:
    def __init__(self, extract_dir: Path):
        paths = [extract_dir / OBCE_FILE, extract_dir / MOMC_FILE]
        if not all(p.exists() for p in paths):
            raise NoBoundaries(f"No RÚIAN boundaries in {extract_dir}; run `bids-scrape opendata ruian` first")
        # Listings are re-matched when the boundaries change.
        self.version = hashlib.sha256(b"".join(p.read_bytes() for p in paths)).hexdigest()[:12]
        self.obce = Areas(paths[0])
        self.momc = Areas(paths[1])
        self.obce_by_code = {props["kod_obec"]: props for _, _, props in self.obce.items}
        self.obce_by_name: dict[tuple[str, str], list[dict]] = {}
        for props in self.obce_by_code.values():
            self.obce_by_name.setdefault((_norm(props["nazev"]), _norm(props["okres"])), []).append(props)
        self.momc_by_name = {_norm(props["nazev"]): props for _, _, props in self.momc.items}
        self.to_sjtsk = Transformer.from_crs("EPSG:4326", "EPSG:5514", always_xy=True)

    def _obec_by_name(self, city: str | None, district: str | None) -> dict | None:
        if district:
            found = self.obce_by_name.get((_norm(city), _norm(district)), [])
        else:
            found = [p for (name, _), ps in self.obce_by_name.items() if name == _norm(city) for p in ps]
        return found[0] if len(found) == 1 else None

    def _momc_by_name(self, quarter: str | None, citypart: str | None) -> dict | None:
        for name in (quarter, citypart, f"Brno-{citypart}" if citypart else None):
            if name and _norm(name) in self.momc_by_name:
                return self.momc_by_name[_norm(name)]
        return None

    def match(self, row) -> tuple[int | None, int | None, str | None]:
        """(kod_obec, kod_momc, method) for one listings row."""
        xy = None
        if row["gps_lat"] is not None and row["gps_lon"] is not None:
            xy = self.to_sjtsk.transform(row["gps_lon"], row["gps_lat"])
        obec, method = (self.obce.locate(*xy), "polygon") if xy else (None, None)
        if obec is None:
            obec, method = self._obec_by_name(row["city"], row["district"]), "name"
        if obec is None and xy and row["district"]:
            # Only within the district Sreality names, so a listing just over the kraj border stays unmatched.
            district = _norm(row["district"])
            obec = self.obce.nearest(*xy, NEAREST_MAX_M, lambda props: _norm(props["okres"]) == district)
            method = "nearest"
        if obec is None:
            return None, None, None
        momc = None
        if obec["kod_obec"] == BRNO:
            if method == "polygon" and row["inaccuracy_type"] != "municipality":
                momc = self.momc.locate(*xy)
            if momc is None:
                momc = self._momc_by_name(row["quarter"], row["citypart"])
        return obec["kod_obec"], momc and momc["kod_momc"], method


def geocode(cfg: Config, *, redo: bool = False) -> Counter:
    """Match listings that are new, whose GPS changed, or that were matched to older boundaries."""
    geocoder = Geocoder(cfg.data_dir / "extract" / "ruian")
    db = open_db(cfg.data_dir / "sreality" / "sreality.sqlite")
    todo = db.execute(
        """
        SELECT l.hash_id, l.gps_lat, l.gps_lon, l.inaccuracy_type, l.district, l.city, l.citypart, l.quarter
        FROM listings l LEFT JOIN listing_geo g ON g.hash_id = l.hash_id
        WHERE ? OR g.hash_id IS NULL OR g.boundaries IS NOT ?
           OR g.gps_lat IS NOT l.gps_lat OR g.gps_lon IS NOT l.gps_lon
        """,
        (redo, geocoder.version),
    ).fetchall()
    counts: Counter = Counter()
    other_name = 0
    now = now_utc()
    for r in todo:
        kod_obec, kod_momc, method = geocoder.match(r)
        counts[method or "unmatched"] += 1
        row = {
            "hash_id": r["hash_id"], "kod_obec": kod_obec, "kod_momc": kod_momc, "geo_match": method,
            "gps_lat": r["gps_lat"], "gps_lon": r["gps_lon"], "boundaries": geocoder.version, "geocoded_at": now,
        }
        _upsert(db, "listing_geo", row, "hash_id", [c for c in row if c != "hash_id"])
        if method == "polygon":
            other_name += _norm(r["city"]) != _norm(geocoder.obce_by_code[kod_obec]["nazev"])
    db.commit()
    db.close()
    if todo:
        summary = ", ".join(f"{n} {k}" for k, n in counts.most_common())
        log.info("Geocoded %d listings (boundaries %s): %s", len(todo), geocoder.version, summary)
        if counts["unmatched"]:
            log.warning("%d of %d listings got no municipality code", counts["unmatched"], len(todo))
        if other_name:
            log.info("%d point matches lie in a municipality other than the Sreality city name", other_name)
    return counts
