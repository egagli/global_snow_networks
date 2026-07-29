# global_snow_networks

A Python toolkit for documenting and accessing snow point observations
(SWE and snow depth) from networks around the world — automated pillows,
manual snow courses, aerial markers, and mirrored climate stations.

The normative design contract lives in [DESIGN.md](DESIGN.md); the
storage strategy is CSV-first:

- a combined station inventory in GeoJSON (`all_snow_stations.geojson`)
  covering **every** known station, periodic sites included, with
  `daily_or_better` marking the probe-verified daily subset
- one CSV time-series file per daily-or-better station
  (`data/stations/*.csv`)
- a compressed bundle for bulk transfer (`data/all_station_csvs.tar.xz`)

This design favors transparency, interoperability, and easy downstream use in
Python, R, GIS tools, and command-line workflows.

---

## Table of Contents

1. [Project Structure](#1-project-structure)
2. [Installation](#2-installation)
3. [Pipeline Overview](#3-pipeline-overview)
4. [Live Map](#4-live-map)
5. [Data Model](#5-data-model)
6. [Networks](#6-networks)
7. [Data Access Methods and Design Philosophy](#7-data-access-methods-and-design-philosophy)
8. [Usage Examples](#8-usage-examples)
9. [Known Caveats](#9-known-caveats)
10. [Provenance and Lineage](#10-provenance-and-lineage)
11. [License and Citation](#11-license-and-citation)

---

## 1. Project Structure

```text
global_snow_networks/
├── DESIGN.md                              # Normative contract (schema, units, semantics)
├── README.md                              # This file
├── CITATION.cff                           # How to cite this archive
├── pixi.toml                              # Environment + task definitions
├── all_snow_stations.geojson              # Combined inventory of ALL stations
├── docs/
│   ├── SOURCES.md                         # Authoritative per-network references
│   └── UNIFICATION_PLAN.md                # July 2026 unification plan/status
├── scripts/
│   ├── create_all_stations_geojson.py     # Build station GeoJSONs from all clients
│   ├── get_all_stations_data.py           # Refresh CSVs + probe verification + archive
│   └── generate_live_map.py               # Build map HTML + chart JSON payloads
│
├── clients/                               # Pure data-access layer (DESIGN.md §2)
│   ├── README.md                          # Client API docs
│   ├── _common.py                         # Shared retry loop, interval enum, helpers
│   ├── awdb/                              #   USDA NRCS AWDB REST API
│   ├── cdec/                              #   CDEC (California)
│   ├── databc/                            #   BC Data Catalogue
│   ├── nve/                               #   NVE HydAPI (Norway)
│   └── yukon/                             #   Yukon AquaCache
│       └── *_stations.geojson             #   Per-client full inventories (generated)
│
├── data/
│   ├── stations/*.csv                     # One CSV per daily-or-better station
│   └── all_station_csvs.tar.xz            # Bulk archive of all station CSVs
│
├── tests/                                 # Offline unit + contract tests; live suites marked
├── utils/                                 # Water-year / day-of-water-year helpers
├── notebooks/                             # Exploration notebooks
└── .github/workflows/
     ├── daily_station_update.yml           # Nightly refresh pipeline
     ├── ci.yml                             # Tests (offline on push, live from pipeline)
     └── deploy-pages.yml                   # GitHub Pages map deployment
```

---

## 2. Installation

This project uses [pixi](https://prefix.dev/docs/pixi/overview) for reproducible
environments.

```bash
# install dependencies
pixi install

# optional interactive shell
pixi shell
```

### NVE API key

The NVE HydAPI (Norway) requires a free API key.  Register at
<https://hydapi.nve.no/> and set it before running the pipeline:

```bash
export NVE_API_KEY="your-key"
```

In GitHub Actions the key is provided via the `NVE_API_KEY` repository
secret.  Without a key the NVE client logs a warning and all NVE requests
fail with HTTP 401 (other clients are unaffected).

---

## 3. Pipeline Overview

The pipeline is split into explicit stages:

```bash
# Stage 1: Build station GeoJSON inventories for all clients
#   Writes per-client GeoJSONs (clients/*/..._stations.geojson)
#   and the combined all_snow_stations.geojson
pixi run fetch-stations

# Stage 2: Fetch/update station CSVs and update GeoJSON record dates
pixi run fetch-data

# Stage 3: Build the interactive live map and per-station chart payloads
pixi run live-map

# Convenience task for all stages
pixi run update-all
```

Individual clients can also be skipped during station fetching:

```bash
python -m scripts.create_all_stations_geojson --skip-cdec --skip-databc
```

### 3.1 Stage 1: Create Station GeoJSONs

Script: `scripts/create_all_stations_geojson.py`

What it does:

1. Fetches the NRCS air temperature bias correction table (live JSON endpoint).
2. Queries each configured client for station locations and metadata.
3. For each client, writes a per-client GeoJSON with **all stations and all
   available metadata** (including periodic snow course sites).
4. Writes `all_snow_stations.geojson` — the combined inventory of every
   station from every client on the universal schema (DESIGN.md §6.1),
   cross-linking `possible_duplicates`, carrying forward probe-verified
   fields, and marking daily-or-better candidates.

### 3.2 Stage 2: Refresh Per-Station CSV Data

Script: `scripts/get_all_stations_data.py`

What it does:

1. Reads daily-or-better candidates from `all_snow_stations.geojson`.
2. Routes each station to the appropriate client based on its `client` field.
3. Writes/replaces station CSVs atomically on successful fetch.
4. **Verifies** each station against the fetched data: a cadence check
   (≥ 30 observations and a dense 90-day window) sets `daily_or_better`,
   `daily_verified`, and the record-date fields — sporadic manual
   readings never masquerade as daily stations (DESIGN.md §4).
   Inactive stations with a regular historical record stay archived.
5. Writes `data/all_station_csvs.tar.xz`.

For stations with **no native daily series**, `--resample-probe` fetches
their sub-daily record and resamples to daily means over the
station-local day (currently wired for NVE; run explicitly, not
nightly).

---

## 4. Live Map

The project includes an interactive map experience driven by per-station CSV data.

Generator script: `scripts/generate_live_map.py`

Primary outputs:
- `live_swe_map.html`
- `charts/*.json` (per-station chart payloads loaded by the popup chart panel)

Features:
- Interactive station markers and popups (photo, operator, data
  provider, live satellite camera link where available)
- **Context imagery**: recent Sentinel-2 chips centred on the selected
  station (see below)
- Variable toggling (WTEQ/SNWD)
- Period-of-record and normal-period comparisons
- Date slider behavior for current water year
- A toggleable overlay of **all other point observations** (snow
  courses, aerial markers, non-daily sites) with metadata-only popups
- "Potentially duplicated station" links that pan to the same physical
  site's other access paths

### Context imagery

Selecting a station adds a **Context imagery** block to the side panel: a
Sentinel-2 chip centred on the station, with a crosshair marking the exact
station location, a scale bar, and a filmstrip of nearby acquisitions.

| Control | Options | Notes |
| --- | --- | --- |
| Scene set | `6 most recent` / `6 least cloudy` | Recent searches the 45 days before the selected date; least-cloudy searches 90 days and ranks by scene cloud cover. In recent order, ★ flags the clearest scene in the strip. |
| Render | `True colour` / `SWIR false colour` | True colour is B4/B3/B2 stretched 0–10000 so snow keeps texture instead of clipping white (the stock TCI asset blows out on snowpack). SWIR is B11/B8/B4: snow reads cyan, cloud reads white/grey — the view for telling a snowy station from a cloudy one. |
| Extent | `3 km` / `10 km` / `30 km` | Chip width on the ground. Wide chips near a granule edge may be partly blank, and the panel says so when that happens. |

Behavior worth knowing:

- **Imagery follows the date slider.** Move it to February and you get the
  scenes around that date, not today's — the imagery matches the SWE values
  beside it. Scene age relative to the selected date is spelled out in the
  caption.
- **Cloud percentages are whole-scene**, covering the full ~110 km granule,
  not the chip. Judge the chip by eye; the numbers only rank the strip.
- **Scenes that only clip the chip are skipped** using the granule footprint,
  falling back to partial coverage (with a note) when nothing better exists.
- **Polar night and long cloudy spells** widen the search to 400 days rather
  than reporting nothing, and say so.
- Clicking `larger ↗` opens a higher-resolution render of the same chip;
  `scene metadata ↗` opens the STAC item.

Source: Copernicus Sentinel-2 L2A served through the
[Microsoft Planetary Computer](https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a)
STAC API and its bbox renderer — keyless, CORS-enabled, and queried by the
browser only when a panel opens. Nothing is fetched at build time and no
imagery is committed. If the service is unreachable the block says so and the
rest of the panel is unaffected (DESIGN.md §8.1). Configuration lives in
`IMAGERY_CONFIG` in `scripts/generate_live_map.py`.

### View on GitHub Pages

Map URL: `https://egagli.github.io/global_snow_networks/`

---

## 5. Data Model

### 5.1 Station Inventory: `all_snow_stations.geojson`

`all_snow_stations.geojson` is the **combined** inventory: every station
from every client, including periodic snow courses and other manual
sites.  The daily archive and map cover exactly the features with
`daily_or_better: true`.

**Universal fields (present on every feature; `null` when unavailable —
DESIGN.md §6.1):**

| Field | Description |
| --- | --- |
| `code` | Native station identifier (e.g. `303_CO_SNTL`, `QUA`, `1A01P`) |
| `name` | Station name |
| `latitude`, `longitude` | WGS-84 coordinates |
| `elevation_m` | Elevation in metres |
| `state` | State / province / country code |
| `network_code` | Network label, verbatim from the source (SNTL, MSNT, CCSS, BCSS, NVE, YSS, …) |
| `operator` | Agency maintaining the physical site — only when certain, else `null` (DESIGN.md §5) |
| `client` | Access-path module: `"awdb"`, `"cdec"`, `"databc"`, `"nve"`, `"yukon"` |
| `data_provider` | Human-readable organization/portal behind that access path |
| `status`, `is_active` | Active status as reported/derived |
| `begin_date`, `end_date` | Period of record from source metadata |
| `earliest_record_date`, `latest_record_date` | Actual archived CSV record span |
| `station_url` | Station information page |
| `station_image_url` | Station photo (where published) |
| `station_camera_url` | Live/satellite camera (e.g. BC snow-station cameras) |
| `notes` | Caveats (bias-correction status, coordinate overrides, probe results, …) |
| `data_variables` | `{name, type, interval, units, description, notes, begin_date, end_date, n_obs}` per variable |
| `has_daily_swe`, `has_daily_snwd` | **Advertised** daily-or-better candidates from source metadata |
| `daily_or_better` | **Probe-verified** daily-or-better status (drives archive + map) |
| `daily_verified` | Whether the data probe has confirmed `daily_or_better` |
| `daily_provenance` | `native`, `resampled_hourly`, or `none` |
| `possible_duplicates` | `{code, client, distance_m}` links to the same physical site via other clients |
| `metadata_fetched_at` | Date the metadata was fetched |

Client-specific extras ride along when present (e.g. AWDB
`awdb_station_triplet`/`county`/`huc`, CDEC `is_snow_course`/`sensors`/
`april1_avg_swe_cm`, DataBC/Yukon `station_type`, NVE
`drainage_basin_key`).

#### Duplicate stations

The same physical station may appear once per access path — some BC and
California stations arrive via both AWDB (`MSNT`/`SNOW`) and their
native clients.  This is intentional (each entry is a distinct access
path with potentially different variables, QC, or metadata); the
`possible_duplicates` field cross-links them so consumers can
de-duplicate however they prefer.  Only pairs where **both** stations
are daily-or-better are published — a duplicate note on a periodic
snow course adds noise, not signal.  (The full matching still runs
internally to borrow operators from native twins.)

#### Per-client GeoJSONs

The per-client GeoJSONs in `clients/*/` carry all available source
metadata and serve as the complete reference for each data source,
including sites the combined schema flattens.

### 5.2 Station CSVs: `data/stations/*.csv`

| Column | Type | Description |
| --- | --- | --- |
| `date` | `YYYY-MM-DD` string | Observation date |
| `wteq_cm` | float or null | Snow water equivalent in cm |
| `snwd_cm` | float or null | Snow depth in cm |

Notes:
- All values are in centimetres (metric-first normalisation).
- Missing observations are represented as null/empty.
- CDEC: `wteq_cm` uses sensor 82 (SNO ADJ) preferentially; falls back to
  sensor 3 (raw SWE) if sensor 82 is not available.
- DataBC ASWS: `snwd_cm` sourced from SD.csv / SD_Archive.csv (16:00 UTC reading).
- Data flags are not stored in CSVs.  Use the respective client's
  `get_data(include_flags=True)` for flag information.

### 5.3 Bulk Archive: `data/all_station_csvs.tar.xz`

All station CSVs are bundled under `stations/` for single-file distribution.

---

## 6. Networks

### 6.1 SNOTEL (SNTL)

**Data source:** USDA NRCS National Water and Climate Center (NWCC)
**Client:** `awdb`
**Stations in archive:** ~865
**Coverage:** Western United States (AK, AZ, CA, CO, ID, MT, NV, NM, OR, UT,
WA, WY) and parts of western Canada (BC, AB).  AWDB carries no Yukon snow
stations — see [6.9](#69-yukon-snow-survey-yss--ykec)
**Period of record:** ~1978 – present
**Temporal resolution:** Daily and hourly
**Variables:** SWE (WTEQ), snow depth (SNWD), precipitation, air temperature,
soil moisture, and more
**Operator:** USDA NRCS

SNOTEL (SNOw TELemetry) is the primary automated snow monitoring network in
the western United States. Established in the mid-1970s, the network comprises
over 900 automated, solar-powered stations installed at remote, high-elevation
mountain watersheds. Data are transmitted via meteor-burst telemetry to a
central database (AWDB/WCIS) several times per day.

**Air temperature bias correction:** NRCS has identified a warm bias in SNOTEL
air temperature sensors at many sites. A correction programme is in progress.
The `notes` field in `all_snow_stations.geojson` indicates whether a correction
has been applied for each SNOTEL station. Status is fetched at runtime from:
https://www.wcc.nrcs.usda.gov/ftpref/support/air_temp_bias/nrcs_air_temp_unbias.html

**Links**
- Network home: https://www.nrcs.usda.gov/programs-initiatives/snotel-snow-telemetry
- Interactive map: https://nwcc-apps.sc.egov.usda.gov/imap/
- Report generator: https://wcc.sc.egov.usda.gov/reportGenerator/

#### Data Sources and Access Methods

| Tool / Source | Type | Description |
|---|---|---|
| AWDB REST API v1 | Primary API | Modern JSON REST API. Full metadata, all elements. **Used by this project.** |
| AWDB SOAP API | Legacy API | Older XML/SOAP service. Full feature parity. |
| metloom | Python | Unified interface to SNOTEL, CDEC, USGS, and others. |
| snotelr | R | R interface to SNOTEL via AWDB. |
| soilDB::fetchSCAN | R | Unified R interface to SCAN and SNOTEL. |

### 6.2 SNOLite (SNTLT)

**Data source:** USDA NRCS NWCC | **Client:** `awdb`
**Stations:** ~44 | **Coverage:** Western U.S. | **Period:** ~2011 – present
**Operator:** USDA NRCS

Lower-cost sensor packages extending coverage where full SNOTEL infrastructure
is not cost-effective. Accessible via all AWDB-based tools using network code
`SNTLT`.

### 6.3 Manual SNOTEL (MSNT)

**Data source:** USDA NRCS NWCC | **Client:** `awdb`
**Stations:** ~173 | **Coverage:** Western U.S. and Canada (BC, AB)
**Period:** Varies, some from the 1960s | **Operator:** USDA NRCS

Historical and transitional sites stored with daily temporal resolution in
AWDB. Includes some BC provincial snow survey stations (see [Known Caveats](#9-known-caveats)).

### 6.4 Soil Climate Analysis Network (SCAN)

**Data source:** USDA NRCS NWCC | **Client:** `awdb`
**Stations:** ~23 with daily SNWD/WTEQ | **Coverage:** Nationwide (CONUS)
**Operator:** USDA NRCS/ARS

National network focused on soil climate monitoring. Only a subset report
meaningful daily snowpack and are included in this archive.

### 6.5 NWS Cooperative Observer Network (COOP)

**Data source:** NOAA NWS (mirrored in AWDB) | **Client:** `awdb`
**Stations:** ~23 with daily SNWD/WTEQ | **Operator:** NOAA NWS

Subset of the nationwide COOP volunteer observer network that also reports
snow-relevant elements in AWDB.

### 6.6 California Cooperative Snow Surveys (CCSS)

**Data sources:** CDEC (California Data Exchange Center) and AWDB
**Clients:** `cdec` and `awdb` (MSNT network)
**Coverage:** California mountain ranges (Sierra Nevada, Cascades, etc.)
**Operator:** California Department of Water Resources (CA DWR)

The California Cooperative Snow Surveys programme, operated by CA DWR, is
California's primary snow monitoring system. It includes two types of sites:

#### Automated snow pillows (daily)

Automated snow pillow stations measure SWE continuously and report daily
values. These stations are `daily_or_better` in `all_snow_stations.geojson`.

**SWE variables (CDEC sensor numbers):**
- **Sensor 3 (SNOW WC):** Raw telemetered reading from the snow pillow load
  cell (SWE, inches).
- **Sensor 82 (SNO ADJ):** Quality-controlled, calibration-offset-corrected
  version of sensor 3. This is the **preferred SWE variable** and is stored
  as `wteq_cm` in per-station CSVs. Carries the `r` (revised) data flag.
- **Sensor 18 (SNOW DP):** Ultrasonic snow depth sensor (inches → `snwd_cm`).

Sensor 82 is a revised version of sensor 3 — both represent SWE from the same
snow pillow — with calibration offsets applied. When both are available,
sensor 82 is always used in preference.

#### Manual snow courses (periodic)

Snow course sites are visited manually by surveyors, typically monthly from
January through May. They record snow depth and SWE by weighing snow cores.
**These sites carry `daily_or_better: false`** (periodic data only) —
they are inventoried and shown on the map's periodic overlay but get no
CSV archive.  Their metadata includes `april1_avg_swe_cm` (April 1
climatological average, converted from the source's inches).

**Station URL format:** `https://cdec.water.ca.gov/dynamicapp/staMeta?station_id={ID}`

#### Comparison: CDEC vs. AWDB for CCSS

| Feature | CDEC (`cdec` client) | AWDB (`awdb` client, MSNT) |
|---|---|---|
| SWE variable | Sensor 82 (SNO ADJ) — adjusted | WTEQ — may be sensor 3 (raw) |
| Snow depth | Sensor 18 (SNOW DP) | SNWD |
| Snow courses | Yes (periodic, in per-client GeoJSON) | Some under MSNT |
| Data flags | Yes (sensor-level flags: r, o, e, …) | Yes (element-level) |
| Hourly data | Yes (sensor 3, 18) | Yes |
| API type | JSON data service + HTML scraping | JSON REST API |
| Station URLs | cdec.water.ca.gov/dynamicapp/staMeta | wcc.sc.egov.usda.gov/nwcc/site |

**Pros of CDEC:**
- Authoritative source for CA DWR data; sensor 82 (SNO ADJ) is the official
  adjusted product
- Includes full snow course inventory and April 1 normals
- Data flags available at the individual value level

**Cons of CDEC:**
- No bulk data API; HTML scraping required for station metadata
- No structured JSON for station list (staSearch is HTML only)
- Monthly aggregates not available for snow sensors (daily only)
- Station metadata requires per-station HTTP requests for full sensor inventory

**Pros of AWDB for CCSS stations:**
- Consistent REST API with batch queries
- Normalised metadata across all networks in one place
- Supports all durations (daily, hourly, monthly, semimonthly, annual)

**Cons of AWDB for CCSS stations:**
- CCSS stations labelled as MSNT, which is semantically misleading
- May serve raw (sensor 3) rather than adjusted (sensor 82) SWE values
- Not all CCSS snow courses are represented

### 6.7 BC Snow Survey

**Data sources:** BC Data Catalogue (DataBC) and AWDB
**Clients:** `databc` and `awdb` (MSNT network)
**Coverage:** British Columbia, Canada
**Operator:** BC Ministry of Environment (BC ENV)

The BC River Forecast Centre (RFC) operates BC's snow survey network,
comprising automated snow weather stations (ASWS) and manual snow course sites
(MSS).

#### Automated Snow Weather Stations — ASWS (daily)

ASWS stations are automated snow pillow and weather sites with location IDs
ending in `P` (e.g. `1A01P`, `1E08P`). They report hourly observations for
a full meteorological suite and are `daily_or_better` in
`all_snow_stations.geojson`.

**Variables (ASWS) — sourced from the public BC env.gov.bc.ca CSV directory:**

| Variable | Units | CSV file | Archive |
|---|---|---|---|
| `swe_mm` | mm | SWDaily.csv (daily) / SW.csv (hourly) | Yes |
| `snwd_cm` | cm | SD.csv | Yes |
| `air_temp_degc` | °C | TA.csv | Yes |
| `precip_cumul_mm` | mm | PC.csv | Yes |
| `baro_press_hpa` | hPa | PA.csv | No (current season only) |
| `wind_dir_deg` | ° | UD.csv | No |
| `wind_spd_kmh` | km/h | US.csv | No |
| `wind_spd_peak_kmh` | km/h | UP.csv | No |
| `wind_run_km` | km | UR.csv | No |
| `rh_pct` | % | XR.csv | No |

The **16:00 UTC reading** is used as the canonical daily value (~08:00 PST /
09:00 PDT) for all variables.  Only `swe_mm` (`wteq_cm` in CSVs) and
`snwd_cm` are stored in the per-station CSV archive; use the client directly
for other variables.

**Per-station CSVs:** `wteq_cm` = `swe_mm ÷ 10`.  `snwd_cm` is stored
directly.  All other ASWS variables are available via the client but not
stored in the daily CSV archive.

**Station URL format:**
`https://aqrt.nrs.gov.bc.ca/Data/Location/Summary/Location/{ID}/Interval/Latest`

**Station images:** Each ASWS station has a photo hosted on the BC Ministry
of Environment AQRT portal (`bcmoe-prod.aquaticinformatics.net`).  The
`station_image_url` GeoJSON field contains a direct `GetFileById` URL fetched
during `fetch-stations` via `DataBCClient.get_station_image_url()`.  These
images are displayed in the live map station popup.

#### Manual Snow Survey Sites — MSS (periodic)

Manual snow course sites have location IDs that do NOT end in `P`
(e.g. `1A06A`, `1A10`). Survey visits occur monthly during the snow season.
**MSS sites carry `daily_or_better: false`** — inventoried, on the
periodic map overlay, no CSV archive.

**Variables (MSS):**
- `swe_mm` (Water Equiv., mm) — snow water equivalent
- `snwd_cm` (Snow Depth, cm) — measured snow depth
- `density_pct` — density percentage
- `snow_line_m` — elevation of snow line

#### Comparison: DataBC vs. AWDB for BC Snow Survey

| Feature | DataBC (`databc` client) | AWDB (`awdb` client, MSNT) |
|---|---|---|
| ASWS daily SWE | Yes — SWDaily.csv (mm) | Yes — WTEQ element |
| ASWS snow depth | Yes — SD.csv (cm) | SNWD element |
| ASWS air temperature | Yes — TA.csv (°C, archived) | TOBS element |
| ASWS precipitation | Yes — PC.csv (mm, archived) | PREC element |
| ASWS wind / humidity / pressure | Yes — UD/US/UP/UR/XR.csv (current season) | Not available |
| MSS surveys (periodic) | Yes — allmss CSV files | Some under MSNT |
| Survey metadata | Depth, density, snow line | WTEQ only |
| Station IDs | Native BC IDs (e.g. `1A01P`) | AWDB triplet (e.g. `1A01P:BC:MSNT`) |
| Station photos | Yes — via AQRT BCMOE portal | No |
| Data flags | MSS survey code field | Yes (element-level) |
| API type | WFS GeoJSON + public CSV files | JSON REST API |
| Station page | AQRT portal | NRCS site page |

**Pros of DataBC:**
- Authoritative BC government data source
- Full meteorological suite from ASWS (SWE, depth, temperature, precip, wind, humidity, pressure)
- Includes full MSS survey data (depth, density, snow line) back to ~1950
- Both ASWS and MSS station locations available as WFS GeoJSON
- Station photos available via AQRT BCMOE portal
- Open Government Licence

**Cons of DataBC:**

- Wind/humidity/pressure (PA, UD, US, UP, UR, XR) have no archive — current season only
- ASWS data is wide-format CSV requiring reshaping
- Two readings per day (00:00 and 16:00 UTC); 16:00 UTC used as daily value
- No per-value data flags for ASWS data

**Pros of AWDB for BC stations:**
- Consistent REST API and triplet format
- SNWD (snow depth) available daily alongside WTEQ
- Supports hourly and other durations

**Cons of AWDB for BC stations:**
- BC snow survey stations labelled as MSNT (misleading)
- Not all BC stations are represented in AWDB

### 6.8 Norway Snow Pillow Network (NVE)

**Data source:** NVE HydAPI (hydapi.nve.no)
**Client:** `nve`
**Stations in archive:** ~31 with daily SWE and/or snow depth
(~1,880 NVE stations carry a snow parameter at some resolution)
**Coverage:** Norway
**Period of record:** ~1970 – present (most stations from the 2000s)
**Temporal resolution:** Instantaneous, hourly, and daily
**Operator:** Norwegian Water Resources and Energy Directorate (NVE)

NVE (Norges vassdrags- og energidirektorat) operates Norway's national
hydrological monitoring network, including automated snow pillow stations.
Data are served by the HydAPI REST service (JSON, API key required — see
[Installation](#2-installation)).

**Snow parameters (NVE parameter IDs):**

| Parameter | Name | Native units | Stored as |
|---|---|---|---|
| **2003** | Snøens vannekvivalent (SWE) | m | `wteq_cm` (× 100) |
| **2002** | Snødybde (snow depth) | cm | `snwd_cm` (as-is) |

> ⚠️ Parameter **2001** is *Markfuktighet* (soil water, %) — it is **not**
> a snow parameter despite its neighbouring ID.

**Quality codes** (`quality` field, via `get_data(include_flags=True)`):
0 = unknown, 1 = uncontrolled, 2 = primary controlled,
3 = secondary controlled.

**Data quality filtering:** the NVE archive contains occasional glitches
that pass NVE's own quality control (e.g. ~145 m SWE readings flagged
"secondary controlled").  The client normalises physically implausible
values (negative or > 15 m) to null.

**Rate limit:** 5 requests/second per API key.  The client spaces
requests and retries HTTP 429 honouring `Retry-After`.

**Station URL format:** `https://sildre.nve.no/station/{station_id}`
(station IDs are three dot-separated numbers, e.g. `12.142.0`).

**Links**
- HydAPI documentation: https://hydapi.nve.no/UserDocumentation/
- Sildre station portal: https://sildre.nve.no/
- xgeo.no (Norwegian snow map): https://www.xgeo.no/

#### Data Sources and Access Methods

| Tool / Source | Type | Description |
|---|---|---|
| HydAPI REST v1 | Primary API | JSON REST API, API key required. Series metadata via `/Series`, values via `/Observations`. **Used by this project.** |
| Sildre portal | Web | Interactive station pages with plots and metadata. |
| xgeo.no | Web | National snow/weather map built on NVE + MET data. |
| seNorge (thredds) | Gridded | Gridded snow products (not point observations). |

### 6.9 Yukon Snow Survey (YSS / YKEC)

**Data source:** Yukon Water Data (AquaCache) API v1
(service.yukon.ca/water-data)
**Client:** `yukon`
**Stations in archive:** 17 with a continuous SWE or snow-depth series
(109 total, including 92 manual snow courses)
**Coverage:** Yukon Territory, plus seven Yukon-operated sites in northern
BC and southeast Alaska
**Period of record:** 1963 – present
**Temporal resolution:** Hourly to 3-hourly (automated), daily (ECCC),
periodic (snow courses)
**Operator:** Yukon Government Department of Environment, Water Science and
Stewardship; ECCC for the `YKEC` subset

AquaCache is the open database behind the Government of Yukon
[Water Data Explorer](https://service.yukon.ca/water-data/shiny/).  The API
requires no authentication and self-describes at
[`/openapi.json`](https://service.yukon.ca/water-data/api/v1/openapi.json).
The Explorer front end sits behind a Cloudflare JS challenge, but the API
itself is unrestricted.

**Two network codes** distinguish who operates the site:

| `network_code` | Station types | Count | Description |
|---|---|---|---|
| `YSS` | `SC`, `AWS` | 101 | Yukon Snow Survey — 92 manual snow courses plus 9 automated snow-weather stations with snow-pillow SWE |
| `YKEC` | `ECCC` | 8 | ECCC climate stations with daily snow depth, mirrored into AquaCache |

**Snow courses (`SC`, 92 sites)** are 10-point manual surveys targeting
Feb 1, Mar 1, Apr 1, May 1 and May 15, with records from 1964.  Being
periodic, they carry `daily_or_better: false` — inventoried, on the
periodic map overlay, no per-station CSV — the same treatment as DataBC
MSS sites.  Reach their survey data with
`client.get_snow_survey_data()`.

**Automated snow-weather stations (`AWS`, 9 sites)** carry snow-pillow SWE
at hourly to 3-hourly resolution, the longest starting 1980-02-25 (Log
Cabin).  Several also report air temperature, precipitation, relative
humidity, wind and soil moisture.

**ECCC stations (`YKEC`, 8 sites)** extend coverage to the Arctic coast —
Herschel Island at 69.57°N, and Komakuk Beach with snow depth from
1963-12-26.  Snow depth is sparse at some of these sites (roughly monthly
in places), so short date windows can legitimately return nothing.

**Snow variables:**

| Parameter | Native units | Stored as |
|---|---|---|
| snow water equivalent | mm | `wteq_cm` (÷ 10) |
| snow depth | cm | `snwd_cm` (as-is) |

**Daily values are means over the local day.**
`/timeseries/measurementsDaily` returns the mean of the day given by the
series' `day_timezone` (UTC-07 for Yukon Snow Survey sites), not an
instantaneous reading.

**`state` is not uniformly `YT`.**  The Yukon Snow Survey operates courses
outside the territory — `09AA-SC04` ("Atlin (B.C.)"), `09EC-SC02`
("Boundary (Alaska)"), the Eaglecrest course near Juneau, and four others.
The client resolves each site's jurisdiction from its published name first,
then a Yukon bounding box, then an explicit override table.

**Bonus endpoints.**  `/snow-survey/stats` and `/snow-survey/trends` give
per-course record length, max/mean/median SWE and depth, and Mann-Kendall
trend tests with Sen's slopes — exposed as `get_snow_survey_stats()` and
`get_snow_survey_trends()`, and useful as an independent cross-check on
course metadata.

**Beyond snow.** The same API serves hydrometric (132 river/stream sites),
groundwater (81 wells) and water-quality data.  None of it is ingested here
— the archive's CSV schema is snow-only (see [§7.4](#74-csv-storage-scope))
— but `YukonClient.get_locations()` and `get_timeseries()` will list those
series if you want them.

**Links**
- Water Data Explorer: https://service.yukon.ca/water-data/shiny/
- OpenAPI spec: https://service.yukon.ca/water-data/api/v1/openapi.json
- Open Yukon — Snow Survey Network: https://open.yukon.ca/data/yukon-snow-survey-network
- Snow surveys and water supply forecasts: https://yukon.ca/en/science-and-natural-resources/water/snow-surveys-and-water-supply-forecasts

#### Data Sources and Access Methods

| Tool / Source | Type | Description |
|---|---|---|
| AquaCache API v1 | Primary API | Open CSV/JSON REST API, no key. Catalogue via `/locations` and `/timeseries`, values via `/timeseries/measurementsDaily`. **Used by this project.** |
| Water Data Explorer | Web | Interactive Shiny app for browsing and downloading (browser only — Cloudflare JS challenge). |
| Open Yukon | Web | Dataset landing pages and historical bulletin PDFs. |
| AquaCache (upstream) | Database | The open-source R/Postgres project the API is built on. |

---

## 7. Data Access Methods and Design Philosophy

### 7.1 Client architecture

Each data source has a dedicated client module under `clients/`:

```
clients/awdb/awdb_client.py      → AWDBClient
clients/cdec/cdec_client.py      → CDECClient
clients/databc/databc_client.py  → DataBCClient
clients/nve/nve_client.py        → NVEClient
clients/yukon/yukon_client.py    → YukonClient
```

**Invariants across all clients** (normative version in
[DESIGN.md](DESIGN.md) §3):
- `get_data()` returns flat record dicts; callers choose pandas/xarray.
  (DataBC additionally offers a pandas-DataFrame convenience layer for
  its bulk CSV files.)
- Metric-first: every value is metric — cm for SWE and snow depth, °C for
  temperature, mm for precipitation, km/h for wind.  No imperial
  passthrough.
- Missing values normalised to `None` / `NaN`.
- Errors raise `{Client}Error(Exception)` with descriptive messages;
  unknown variables and unsupported intervals raise too (no silent
  fallbacks).
- Sub-daily records carry a `datetime` key alongside `date`.
- `get_data(..., include_flags=True)` adds a `flag` key to each value record.

### 7.2 Variables and flags

Each client module exposes:
- **`SENSORS` / `VARIABLES`** — dict mapping variable codes to metadata
  (name, native units, output units, description).
- **`DATA_FLAGS`** — dict mapping flag codes to human-readable
  descriptions (empty for AWDB, which returns no per-value flags).
- CDEC additionally exposes **`DURATION_CODES`** (its native duration
  vocabulary).

These are importable for documentation and downstream use:

```python
from clients.cdec import CDECClient
from clients.cdec.cdec_client import SENSORS, DATA_FLAGS

print(SENSORS[82])
# {'name': 'Snow Water Content (Adjusted)', 'short_name': 'SNO ADJ', ...}
```

### 7.3 Snow course / periodic data

Clients can retrieve all available intervals including periodic snow course
measurements.  Example — CDEC monthly (note: monthly aggregation unavailable
for sensors 3/18/82; use daily with sparse records):

```python
client = CDECClient()
courses = client.get_snow_courses()  # CCSS course list
records = client.get_data(
    station_ids=["QUA", "BLC"],
    variables=["swe"],
    interval="daily",
    begin_date="1981-10-01",
)
```

For BC snow courses (periodic survey data):

```python
client = DataBCClient()
records = client.get_data(
    station_ids=["1A06A", "1A10"],
    variables=["swe", "snwd", "density", "snow_line"],
    interval="periodic",
    include_flags=True,   # includes survey_code quality flag
)
```

For Yukon snow courses (periodic 10-point surveys back to 1964):

```python
client = YukonClient()
rows = client.get_snow_survey_data(
    station_ids=["08AA-SC01"],   # Canyon Lake Snow Course
    include_flags=True,          # "Actual" vs "Estimated SWE"
)
apr1 = [r for r in rows if r["survey_period"] == "01-Apr"]
```

### 7.4 CSV storage scope

**Per-station CSVs (`data/stations/*.csv`) contain only:**
- Daily SWE (`wteq_cm`)
- Daily snow depth (`snwd_cm`)

Snow course/periodic data, hourly data, other variables (temperature,
precipitation, soil moisture), and data flags are NOT stored in CSVs.  Use
the client APIs directly for those.

---

## 8. Usage Examples

### 8.1 Rebuild the archive locally

```bash
pixi run fetch-stations
pixi run fetch-data
pixi run live-map
```

### 8.2 Inspect station inventory in Python

```python
import geopandas as gpd

gdf = gpd.read_file("all_snow_stations.geojson")
# Probe-verified daily stations from a single client
cdec = gdf[(gdf["client"] == "cdec") & gdf["daily_or_better"]]
print(cdec[["code", "name", "operator", "has_daily_swe", "has_daily_snwd"]].head())
```

### 8.3 Read one station CSV

```python
import pandas as pd

df = pd.read_csv("data/stations/303_CO_SNTL.csv", parse_dates=["date"])
print(df[["wteq_cm", "snwd_cm"]].describe())
```

### 8.4 Load bulk archive

```bash
tar -xJf data/all_station_csvs.tar.xz -C /tmp
ls /tmp/stations | head
```

### 8.5 Fetch CDEC station data with flags

```python
from clients.cdec import CDECClient
from clients.cdec.cdec_client import SENSORS, DATA_FLAGS

client = CDECClient()

# Get full sensor inventory for a station
meta = client.get_metadata("QUA")
print(meta["sensor_inventory"])

# Fetch data with quality flags
records = client.get_data(
    station_ids=["QUA"],
    variables=["swe", "snwd"],
    interval="daily",
    begin_date="2023-10-01",
    end_date="2024-09-30",
    include_flags=True,
)
# records[0] → {"station_id": "QUA", "date": "2023-10-01",
#               "variable": "SNO ADJ", "type": "swe",
#               "value": 5.08, "units": "cm", "interval": "daily", "flag": "r"}
```

### 8.6 Fetch BC snow survey data

```python
from clients.databc import DataBCClient

client = DataBCClient()

# List all automated stations
asws = client.get_asws_stations(active_only=True)
print(len(asws), "active ASWS stations")

# Daily SWE + snow depth for two ASWS stations (values in cm)
records = client.get_data(
    station_ids=["1A01P", "1E08P"],
    variables=["swe", "snwd"],
    interval="daily",
    begin_date="2022-10-01",
)
print(records[0])

# Periodic snow course surveys for BC MSS stations
surveys = client.get_data(
    station_ids=["1A06A", "1A10"],
    variables=["swe", "snwd", "density"],
    interval="periodic",
)
print(len(surveys), "survey records")
```

### 8.7 Fetch NVE (Norway) snow data

```python
from clients.nve import NVEClient
from clients.nve.nve_client import VARIABLES, DATA_FLAGS

client = NVEClient()  # reads NVE_API_KEY from the environment

# All NVE stations with snow parameters (daily_parameters shows which
# have daily series)
stations = client.get_all_stations(active_only=True)

# Daily SWE + snow depth with quality flags (values in cm)
records = client.get_data(
    station_ids=["12.142.0"],       # Bakko snow pillow
    variables=["swe", "snwd"],
    interval="daily",
    begin_date="2023-10-01",
    end_date="2024-06-30",
    include_flags=True,
)
# records[0] → {"station_id": "12.142.0", "date": "2023-10-01",
#               "variable": "swe_m", "type": "swe", "value": 0.0,
#               "units": "cm", "interval": "daily", "flag": "3"}
```

### 8.8 Fetch Yukon snow data

```python
from clients.yukon import YukonClient

client = YukonClient()   # no API key needed

# Every Yukon snow station: 92 courses + 9 automated + 8 ECCC
stations = client.get_all_stations()

# Daily SWE + snow depth from an automated snow-weather station (cm)
records = client.get_data(
    station_ids=["09AA-M1"],        # Tagish Meteorological, SWE since 1988
    variables=["swe", "snwd"],
    interval="daily",
    begin_date="2023-10-01",
    end_date="2024-06-30",
)
# records[0] → {"station_id": "09AA-M1", "date": "2023-10-01",
#               "variable": "swe_mm", "type": "swe", "value": 0.0,
#               "units": "cm", "interval": "daily",
#               "aggregation": "instantaneous", "timeseries_id": "20"}

# Hourly snow-pillow SWE with grade/approval/qualifier flags
hourly = client.get_data(
    station_ids=["09AA-M1"],
    variables=["swe"],
    interval="hourly",
    begin_date="2024-03-01 00:00",
    end_date="2024-03-02 00:00",
    include_flags=True,
)

# Per-course statistics and Mann-Kendall trends
stats = client.get_snow_survey_stats()
trends = client.get_snow_survey_trends()
```

---

## 9. Known Caveats

### 9.1 AWDB network label semantics

AWDB network codes can be semantically misleading:

- Some BC snow survey stations appear under `MSNT` ("Manual SNOTEL"), which
  does not reflect their actual operating programme.
- Some California CCSS stations also appear under `MSNT`.

This project preserves source-provided AWDB network codes exactly to avoid
introducing ambiguity.  The `client` and `data_provider` fields in
`all_snow_stations.geojson` distinguish access paths; `network_code`
reflects what AWDB reports, and `operator` is corrected only when a
uniquely matching native twin makes it certain (DESIGN.md §5).

### 9.2 Duplicate stations

The same physical station may appear multiple times in
`all_snow_stations.geojson` if accessible via more than one client.
This is intentional — each entry reflects a distinct data access path
with potentially different variables, QC levels, or metadata.  The
`possible_duplicates` field cross-links candidates (spatial +
name matching) so a single-entry view is one filter away.  Links are
published only between daily-or-better stations; pairs involving a
periodic site are matched internally (for operator borrowing) but not
annotated.

### 9.3 DataBC ASWS met variables with no archive

Daily SWE (SWDaily.csv), snow depth (SD.csv), air temperature (TA.csv), and
cumulative precipitation (PC.csv) have full historical archives.  Wind
direction, wind speed, wind gust, wind run, relative humidity, and barometric
pressure (UD, US, UP, UR, XR, PA) are available from the current season only —
no archive files exist for these variables.  For historical met analysis at BC
ASWS stations, contact BC Ministry of Environment or use the AQRT portal.

### 9.4 CDEC monthly data unavailability

CDEC's JSON data service does not return monthly aggregates for snow sensors
3, 18, or 82.  Use daily duration (`"D"`) for all CDEC snow data retrieval.

### 9.5 NVE parameter ID semantics and data glitches

NVE parameter IDs are easy to misread: **2001 is soil water**
(Markfuktighet), **2002 is snow depth** (Snødybde), and **2003 is SWE**
(Snøens vannekvivalent, native unit metres).  The NVE archive also
contains occasional extreme glitches that carry a "quality controlled"
flag (e.g. ~145 m SWE at station 123.93.0 in Jan 2018); the client
normalises values outside 0–15 m to null.  Only ~31 of NVE's ~1,880
snow-parameter stations have *daily* (1440-minute) series — the rest are
instantaneous/hourly-or-sporadic only and carry
`daily_or_better: false` (`--resample-probe` can promote genuinely
continuous ones — see §3.2).

### 9.6 Stations with invalid coordinates

- **Null-island placeholders** — e.g. CDEC's `TST` ("SNOW SURVEYS TEST
  STATION") sits at latitude/longitude (0, 0).  Features with missing or
  (0, 0) coordinates are excluded from `all_snow_stations.geojson`
  (but retained in the per-client GeoJSONs).
- **NVE Nepal cooperation stations** (drainage-basin group `1977.*`:
  Langtang and Mustang, SnowAMP project with ICIMOD/DHM) — HydAPI serves
  longitudes exactly 60° west of reality, which would render them in
  Africa.  The NVE client detects the known-wrong positions and falls
  back to hardcoded corrected coordinates (the Langtang positions are
  corroborated by the SnowAMP literature; the Mustang ones apply the
  same +60° correction but are unverified).  Corrected stations carry an
  explanatory `notes` entry.  If HydAPI ever starts reporting a position
  close to the correction, the upstream value is used.  Two river gauges
  in the group (`1977.1.5`, `1977.1.6`) have unfixable coordinates but
  no daily snow data, so they never reach the daily inventory.

### 9.7 Yukon AquaCache response quirks

- **Empty results arrive as a status envelope, not an empty CSV.**  A query
  matching no rows returns a `status,message` CSV with one informational
  row.  The client recognises and drops it, so it never surfaces as a bogus
  observation.
- **`/snow-survey/*` CSVs carry a quoted comment header** whose block ends
  with a line containing exactly `""`.  Filtering only on `line.strip()`
  treats that line as content and silently makes it the CSV header.
- **`/snow-survey/data` reports empty `units` for snow depth.**  The unit is
  cm, corroborated by the `/snow-survey/stats` field name `max_DEPTH_cm`.
  The client takes units from `VARIABLES`, never from the response.
- **`09DC-SC01` (Mayo Airport) is a composite placeholder** — present in
  `/locations` but absent from `/snow-survey/metadata`, with no survey rows
  of its own (its constituents `09DC-SC01A`/`B` hold the data).  Courses are
  therefore built from `/locations` so composites are not silently dropped.
- **Multiple series per parameter.**  A location can hold several series of
  the same parameter distinguished only by `aggregation_type` — ECCC daily
  air temperature exists as `minimum`, `maximum` and `(min+max)/2`.  Records
  carry `aggregation` and `timeseries_id` to disambiguate.  This does not
  affect the CSV archive, where each station has one SWE and one snow-depth
  series.
- **The Water Data Explorer is browser-only.**  It sits behind a Cloudflare
  JS challenge, so `station_url` points at the Explorer entry page rather
  than a per-station permalink, and `get_station_image_url()` always returns
  `None` — this source publishes no station imagery.

### 9.8 Yukon / ECCC provenance overlap

The eight `YKEC` stations are a Yukon-Government **mirror** of ECCC data, not
an independent observation network.  If a direct ECCC client is ever added,
it will duplicate these sites and should be de-duplicated by lat/lon plus
name — the same situation as BC and CCSS stations appearing under both
`awdb`/`MSNT` and their native clients (see [§9.2](#92-duplicate-stations)).

---

## 10. Provenance and Lineage

This repository is the fourth generation of an evolving effort:

1. **[snotel_ccss_stations](https://github.com/egagli/snotel_ccss_stations)**
   (2024, v1) — SNOTEL + CCSS daily CSVs readable straight off
   `raw.githubusercontent.com`, no API needed.  Frozen but citable
   (DOI [10.5281/zenodo.17246162](https://doi.org/10.5281/zenodo.17246162)).
2. **snotel_ccss_stations_v2** (early 2026) — rebuilt on the AWDB REST
   API with inline QC flags, per-station audit logs of NRCS retroactive
   corrections, and adaptive lookback.  Retired March 2026.  (Reviving
   its flags/audit machinery in this repo is tracked as future work.)
3. **global_snow_point_obs** (March 2026) — an architecture spike
   sketching a unified API over ~29 networks; its registry-first design
   and "preserve source metadata, duplicates are intentional" principles
   carried into this repo's `DESIGN.md`.
4. **global_snow_networks** (this repo) — five fully-implemented
   clients, the combined probe-verified inventory, the daily CSV
   archive, and the live map.

---

## 11. License and Citation

Data accessed from AWDB is public domain (U.S. Government).
BC snow survey data is published under the Open Government Licence — British
Columbia.
CDEC data is published by CA DWR.
NVE data is published under the Norwegian Licence for Open Government Data
(NLOD).
Yukon snow and water data is published under the Open Government Licence —
Yukon.

Suggested citations for source data:

> USDA Natural Resources Conservation Service (NRCS). Air and Water Database
> (AWDB) REST API v1. National Water and Climate Center, Portland, OR.
> <https://wcc.sc.egov.usda.gov/awdbRestApi/>

> California Department of Water Resources (CA DWR). California Data Exchange
> Center (CDEC). <https://cdec.water.ca.gov>

> BC Ministry of Environment. BC Snow Survey Network. BC Data Catalogue.
> <https://catalogue.data.gov.bc.ca>

> Norwegian Water Resources and Energy Directorate (NVE). HydAPI —
> hydrological API. <https://hydapi.nve.no/>

> Government of Yukon, Department of Environment, Water Resources. Yukon Water
> Data (AquaCache) API. <https://service.yukon.ca/water-data/>
