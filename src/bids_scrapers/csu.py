"""ČSÚ (Czech Statistical Office) open data.

  * ciselnik_obce_okresy.json  municipality -> district code list (DataStat dimension UZ35H)
  * obyvatele_obce_jmk_1-1.csv, obyvatele_obce_jmk_stredni.csv
                               population of the 673 JMK municipalities, 2000-now, at 1 January and
                               mid-year (DataStat dataset OBY02A, indicators 2406P and 9379, total).
                               Rows with an empty obec are district subtotals; values are "1462.0".
  * mzdy_kraje_110080.csv      average + median gross monthly wage by region (ISPV), 2011-2024
                               (dataset 110080; its URL changes with every release, found via NKOD)
  * mzdy_kraje_mzdr.csv        average gross monthly wage by region from the quarterly survey,
                               one year fresher than 110080 but a different methodology (DataStat MZDR)
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

from .http import PoliteClient
from .opendata import Store

log = logging.getLogger(__name__)

CODELIST_URL = "https://data.csu.gov.cz/api/katalog/v1/dimenze/UZ35H/polozky"
POPULATION_URL = "https://data.csu.gov.cz/api/dotaz/v1/data/sady/OBY02A/vlastni"
POPULATION_FILES = {
    "2406P": "obyvatele_obce_jmk_1-1.csv",  # k 1. 1. (počáteční stav)
    "9379": "obyvatele_obce_jmk_stredni.csv",  # k 1. 7. (střední stav), for per-1000 rates
}
MZDR_URL ="https://data.csu.gov.cz/opendata/sady/MZDR/distribuce/csv"
SPARQL_URL = "https://data.gov.cz/sparql"
WAGES_110080_FALLBACK = (
    "https://csu.gov.cz/docs/107508/9643ae62-f3a1-75c4-1ebd-23c9cdb5d81a/110080-25data052325.csv?version=1.0"
)
WAGES_110080_QUERY = """
PREFIX dcat: <http://www.w3.org/ns/dcat#>
PREFIX dct: <http://purl.org/dc/terms/>
SELECT ?title ?url WHERE {
  ?ds a dcat:Dataset ; dct:identifier ?id ; dcat:distribution ?d .
  FILTER(STR(?id) = "https://vdb.czso.cz/pll/eweb/lkod_ld.datova_sada?nazev=Prumerna_hruba_mesicni_mzda_a_median_mezd_v_krajich")
  ?d dcat:downloadURL ?url .
  OPTIONAL { ?d dct:title ?title }
}
"""

JMK_NUTS = "CZ064"


def jmk_municipalities(store: Store) -> dict[str, dict]:
    """{kod_obce: {"name": ..., "okres": "CZ06xx"}} for the 673 JMK municipalities."""
    items = json.loads(store.path("ciselnik_obce_okresy.json").read_text(encoding="utf-8"))
    return {
        i["kod"]: {"name": i["nazev"], "okres": i["nadrazenaPolozkaKod"]}
        for i in items
        if str(i.get("nadrazenaPolozkaKod", "")).startswith(JMK_NUTS)
    }


def _latest_110080_url(client: PoliteClient) -> str:
    try:
        resp = client.get(
            SPARQL_URL,
            params={"query": WAGES_110080_QUERY},
            headers={"Accept": "application/sparql-results+json"},
        )
        resp.raise_for_status()
        urls = [b["url"]["value"] for b in resp.json()["results"]["bindings"]]
        csvs = [u for u in urls if ".csv" in u.lower()]
        if csvs:
            # Release files are named 110080-<yy>data<mmddyy>.csv; the newest sorts last.
            return sorted(csvs)[-1]
    except Exception as exc:  # the catalogue is only used for discovery; fall back to the known URL
        log.warning("NKOD lookup for dataset 110080 failed (%s); using the last known URL", exc)
    return WAGES_110080_FALLBACK


def collect(client: PoliteClient, store: Store) -> dict[str, str]:
    results = {}
    results["ciselnik_obce_okresy.json"] = store.fetch(
        client, CODELIST_URL, "ciselnik_obce_okresy.json", params={"kodUrovne": "OBEC"}
    )
    codes = sorted(jmk_municipalities(store))
    log.info("ČSÚ: %d municipalities in Jihomoravský kraj", len(codes))

    # One request per indicator: both at once exceeds DataStat's synchronous size limit.
    for indicator, rel in POPULATION_FILES.items():
        results[rel] = store.fetch(
            client,
            POPULATION_URL,
            rel,
            # DataStat does not decode %2C, so the lists must keep literal commas.
            params=urlencode(
                {
                    "format": "CSV",
                    "IndicatorType": indicator,
                    "POHL2": "0",
                    "CasR": "*",
                    "UZ35H": ",".join(codes),
                    "formatHierarchie": "UROVNE_ZVLAST",
                    "kodZvlast": "true",
                },
                safe=",*",
            ),
        )

    url = _latest_110080_url(client)
    same_release = store.entries.get("mzdy_kraje_110080.csv", {}).get("source_url") == url
    results["mzdy_kraje_110080.csv"] = store.fetch(
        client, url, "mzdy_kraje_110080.csv", conditional=same_release, extra={"source_url": url}
    )
    results["mzdy_kraje_mzdr.csv"] = store.fetch(client, MZDR_URL, "mzdy_kraje_mzdr.csv")
    return results
