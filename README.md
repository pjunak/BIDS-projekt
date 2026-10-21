# BIDS projekt – data collection

Extraction part (the "E" of ETL) for the BIDS project *Analýza trhu s nemovitostmi v Jihomoravském kraji*.

| # | Source | What we get | History | Refresh |
|---|---|---|---|---|
| 1 | Sreality.cz | flats/houses for sale/rent in JMK, daily price snapshots | from the first run only | daily |
| 2 | ČSÚ OBY02A | population of the 673 JMK municipalities | 2000–2026 | weekly check |
| 3 | MPSV | job applicants, unemployment share per municipality; vacancies | 03/2014–now | weekly check |
| 4 | Policie ČR | every recorded offence, with municipality code | 01/2013–now | weekly check |
| 5 | ČSÚ 110080 / MZDR | average + median wage by region | 2011–2024 / 2011–2025 | weekly check |
| 6 | ČÚZK RÚIAN | boundaries of the JMK municipalities and Brno districts, to give each listing its `kod_obec` | current only | monthly check |

## Setup

```bash
# needs uv (https://docs.astral.sh/uv/); it installs Python 3.11+ if missing
git clone <this repo> ~/BIDS-projekt && cd ~/BIDS-projekt
cp config.example.toml config.toml   # put your e-mail into `contact`
uv sync
```

## Commands

```bash
uv run bids-scrape sreality        # scan all listings, then fetch details of new ones
uv run bids-scrape opendata        # ČSÚ + MPSV + Policie + RÚIAN: backfill on the first run, then only changes
uv run bids-scrape opendata mpsv   # a single source (csu, mpsv, policie, ruian)
uv run bids-scrape geocode         # kod_obec / kod_momc for new listings (also done by sreality and export)
uv run bids-scrape export          # Sreality CSVs for the ETL -> data/export/sreality/
uv run bids-scrape status          # what has been collected so far
uv run pytest                      # offline test suite (fake servers, no real requests)
```

Exit code is 0 on success and 1 otherwise, so systemd/cron see failures. Sreality run statuses
(`runs` table, `status` command):

| status | meaning |
|---|---|
| `ok` | full scan, every category reached the total the API reported |
| `partial` | some category came back incomplete, or far fewer listings than last time (possible API change). Listings not seen still count as active. |
| `details` | a `--details-only` run |
| `blocked` | 401/403 from Sreality; following runs pause for 72 h |
| `failed` / `interrupted` | an error / stopped by Ctrl-C or `systemctl stop` |

Everything goes to `data/` (not in git). The first open-data run (measured: 313 requests, ~30 min)
leaves about 0.75 GB raw + 0.45 GB extract; Sreality grows by roughly 1–2 MB a day.

```
data/
  sreality/sreality.sqlite        Sreality database (tables + view v_listings)
  raw/sreality/<date>/*.jsonl.gz  trimmed raw API responses
  raw/csu/  raw/mpsv/  raw/policie/  raw/ruian/   open-data files exactly as downloaded, + manifest.json
  raw/<source>/_history/          previous versions of files that changed
  extract/policie/                derived: JMK offences with municipality code
  extract/ruian/                  derived: JMK municipality + Brno district polygons (GeoJSON)
  export/sreality/                derived: CSVs from `bids-scrape export`
  logs/scrape.log
```

`manifest.json` records URL, ETag/Last-Modified, SHA-256 and download time of every file, which
is what the documentation needs to describe the data lineage.

## 1. Sreality

Uses the JSON API the sreality.cz frontend calls. Each run:

1. Pages through `/api/v1/estates/search` (60 per page) for flats/houses × sale/rent in region 14 (JMK):
   ~5 900 listings = ~100 requests.
2. Upserts each listing into `listings` (`first_seen` / `last_seen`) and writes one row per listing
   per day into `snapshots` (price history).
3. Fetches `/api/v1/estates/{id}` for listings without detail: usable/land area, condition,
   construction, ownership, energy class, floor, publish date (`since`), etc. At most 1 500 per
   run, so the initial ~5 900 take 4 daily runs; afterwards only new listings (~100–200 a day).

| Zadání | Column in `v_listings` / `listings.csv` |
|---|---|
| ID inzerátu | `hash_id` |
| Typ nemovitosti / Typ nabídky | `property_type`, `offer_type` |
| Dispozice | `disposition` |
| Užitná plocha / Plocha pozemku | `usable_area`, `land_area` |
| Cena | `last_price_czk`, `first_price_czk`; full history in `snapshots.csv`; NULL = price on request |
| Lokalita | `district` (okres), `city`, `citypart`, `quarter`, `street` |
| GPS | `gps_lat`, `gps_lon`; `inaccuracy_type` says how precise |
| Kód obce / městská část | `kod_obec` (ČSÚ/RÚIAN, Brno = 582786), `kod_momc` (Brno only); `geo_match` says how (see below) |
| Stav objektu / Vlastnictví / Energetická třída | `building_condition`, `ownership`, `energy_class` |
| Datum stažení | `snapshots.snapshot_date`, `first_seen`, `last_seen` |
| Time on market | `listed_since`, `days_on_market`, `is_active` |

ETL notes:

- **No history before the first run**: Sreality does not expose removed listings. `listed_since`
  gives the publish date of listings that are online on the first run.
- **Municipality code**: Sreality has none, only names and GPS, and 21 names repeat within JMK
  (two Březina even in the same okres Brno-venkov). So `geocode` puts each listing's GPS into the RÚIAN boundaries (section 6)
  and stores the result in table `listing_geo`, shown in `v_listings` / `listings.csv` as
  `kod_obec`, `kod_momc` and `geo_match`:

  | `geo_match` | meaning |
  |---|---|
  | `polygon` | the GPS point lies in that municipality (nearly all listings) |
  | `name` | the point is outside every JMK municipality (or missing); Sreality's `city` + `district` name exactly one |
  | `nearest` | still nothing: the nearest municipality of the same district, at most 1 km away (points just across the state border) |
  | NULL | no match, e.g. a listing that is really in another kraj. Count them with `bids-scrape status`. |

  Even street- or ward-precise points (`inaccuracy_type`) lie inside the right municipality. Brno's
  `kod_momc` comes from the point too, except for city-precise points (`inaccuracy_type` =
  `municipality`, which sit at Brno's centre): those take it from `quarter`, or stay NULL.
  `kod_obec` joins directly to ČSÚ population, MPSV unemployment and the Policie extract;
  `kod_momc` to the Policie `kod_momc`. Listings are re-matched when their GPS or the boundaries change.
- **Duplicates** (same flat via several agencies): compare GPS, area, floor, layout, price;
  `premise_id` distinguishes agencies.
- Seller/agency names, e-mails, phones and descriptions are deliberately not stored (GDPR), only ids.

## 2. ČSÚ – population (`raw/csu/`)

- `obyvatele_obce_jmk_1-1.csv`: population at 1 January, 2000–2026, DataStat dataset OBY02A.
- `obyvatele_obce_jmk_stredni.csv`: mid-year population, 2000–2025. Use this for "per 1 000 inhabitants" rates.
- `ciselnik_obce_okresy.json`: municipality → district code list (`kod`, `nazev`, `nadrazenaPolozkaKod` = CZ06xx).

CSV columns: `CasR` (year), `UZ35H.OKRES`, `UZ35H.OBEC` (kód obce, Brno = 582786), `Hodnota`.
Rows with empty `UZ35H.OBEC` are district subtotals; values look like `"1462.0"`.

## 3. MPSV – unemployment (`raw/mpsv/`)

The municipality series changed format in 2025:

| Period | Files | Notes |
|---|---|---|
| 03/2014–12/2024 | `nezamestnanost_obce_2014-2024/*.json.gz` | `{"polozky": [...]}`; `obec` = "Obec/582786"; `podilNezamestnanychOsob` in %; includes `volnaPracovnaMista` |
| 01/2025– | `nezamestnanost_obce/YYYY-MM.csv` | `;` separated, UTF-8 BOM, decimal comma; **two rows per municipality (M/Ž) – sum them**; `pno` is a fraction; use the `*_trvala` columns to continue the old series; drop `(neuvedeno)` rows |
| 01/2025– | `volna_mista_okresy/YYYY-MM.csv` | vacancies **only per district** since 2025 (`kod_okres_pobocky_up`, sum `volna_mista_rozhodne_datum`); MPSV warns of a break after its new system in autumn 2026 |

Filter to JMK: `kraj_kod = 116` (new files) or `okres` 3701–3713 (old files). Code lists are in `ciselniky/`.

## 4. Policie ČR – offences (`raw/policie/`, `extract/policie/`)

- `raw/policie/mesice/YYYYMM.zip`: the whole of Czechia, one row per (offence, type code).
  Count **distinct `id`**; ~92 % are přestupky (type 97).
- `raw/policie/voronoi.gpkg.zip`: the cells offences are snapped to (for anonymity).
- `extract/policie/voronoi_body.csv`: cell centroid → `kod_obec`, `kod_momc` (Brno districts).
- `extract/policie/jmk/YYYYMM.csv`: **use this one**. JMK rows only, with `kod_obec`/`kod_momc` added
  (exact centroid match, 100 % of rows), about 30k rows (10k offences) a month.
- `extract/policie/jmk/types.csv`, `states.csv`, `relevance.csv`: code lists. `state` 1 = offender identified ("objasněno").

The police regenerate files nightly as cases get solved. Months from the last 12 months are
re-downloaded when they change, and older ones are kept as first downloaded. Credit the source
(Policie ČR) in the documentation.

## 5. ČSÚ – wages (`raw/csu/`)

- `mzdy_kraje_110080.csv`: average **and median** gross monthly wage by region, 2011–2024 (ISPV).
  Average = empty `SPKVANTIL_kod`, median = `Q5`; total = empty `POHLAVI_kod`; JMK = `uzemi_cis` 100, `uzemi_kod` 3115.
  The release URL changes every year, so the newest one is looked up in the national catalogue (NKOD).
- `mzdy_kraje_mzdr.csv`: average wage from the quarterly survey, which already has 2025, but uses a
  different methodology (JMK 2024: 44 759 vs 47 923 in 110080). If you mix the two, say so in the documentation.

## 6. ČÚZK RÚIAN – boundaries (`raw/ruian/`, `extract/ruian/`)

- `raw/ruian/inspire_au.zip`: INSPIRE Administrative Units of the whole of Czechia from ČÚZK
  (GML, S-JTSK = EPSG:5514): kraje, okresy and every obec with its original RÚIAN boundary, ~78 MB.
- `raw/ruian/obec_582786_shp.zip`: RÚIAN data of Brno as shapefiles; only the 29 městské části are used.
- `extract/ruian/obce_jmk.geojson`: the 673 JMK municipalities (`kod_obec`, `nazev`, `kod_okres` =
  LAU 1 CZ064x, `okres`), checked against the ČSÚ code list.
- `extract/ruian/momc_brno.geojson`: Brno's 29 districts (`kod_momc`, `nazev`).

The GeoJSON keeps S-JTSK coordinates (QGIS reads them; Power BI maps need WGS84). For matching,
the listings' WGS84 GPS is transformed to S-JTSK with pyproj (EPSG "S-JTSK to WGS 84 (5)", about
1 m). ČÚZK regenerates both files nightly, but boundaries change only a few times a year, so they
are re-checked every 30 days (2 requests, ~86 MB) and older versions are not kept. Credit the
source (ČÚZK, open data under CC BY 4.0) in the documentation.

## Being a good citizen (and not getting blocked)

All collectors behave like a single slow visitor:

- one request at a time, a random 2.5–6 s pause between requests, a 30–90 s break every 150,
- conditional requests (ETag / Last-Modified) and local state, so nothing already downloaded is
  fetched again: a normal weekly open-data run is ~15–30 requests, a daily Sreality run ~100 + new details,
- runs at night at a random time (systemd `RandomizedDelaySec`),
- an honest `User-Agent` with your contact (`contact` in config),
- backs off on 429/5xx (honours `Retry-After` and `X-RateLimit-*`). On 401/403 it **stops**, and
  Sreality then pauses for 72 h. Investigate before using `--force`.

There is deliberately no proxy rotation, browser-fingerprint spoofing or similar. At this volume
it isn't needed, and if a site does block you, the right move is to ask the operator.

## Deploying to a server

Data collected on another machine can be carried over, so the server does not download it again:

```bash
# on the server: install uv, then
git clone <this repo> ~/BIDS-projekt && cd ~/BIDS-projekt && uv sync
# from the machine that already has data (stop its timers first, so nothing writes meanwhile):
rsync -a --info=progress2 data config.toml server:~/BIDS-projekt/   # no trailing slash on data
# back on the server
uv run pytest -q && uv run bids-scrape status
```

Only one machine should collect Sreality: two of them would just double the requests. Disable
the timers on the old one.

## Monitoring

Create two free checks on https://healthchecks.io (period 1 day for Sreality, 7 days for open
data, grace a few hours) and put their ping URLs into `[ping]` in `config.toml`. Each run pings
on success and `<url>/fail` on failure. A run that never happens (server down, timer broken)
also triggers an e-mail, which cron/systemd alone would not.

## Running on a schedule

systemd user units are in `deploy/` (edit `WorkingDirectory` if the repo is not in `~/BIDS-projekt`):

```bash
mkdir -p ~/.config/systemd/user
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bids-sreality.timer bids-opendata.timer
loginctl enable-linger "$USER"      # on a server: keep user timers running when logged out
systemctl --user list-timers
journalctl --user -u bids-sreality   # logs (also data/logs/scrape.log)
```

`Persistent=true` runs a missed job right after boot, so a personal machine works too; Sreality
just gets a gap for the days it was off. For the time-on-market KPI, a server that is always on
is better.

Cron alternative:

```cron
17 3 * * *  cd ~/BIDS-projekt && ~/.local/bin/uv run bids-scrape sreality
40 4 * * 1  cd ~/BIDS-projekt && ~/.local/bin/uv run bids-scrape opendata
```
