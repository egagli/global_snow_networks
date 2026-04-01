# global_snow_networks

A Python toolkit for archiving and accessing daily snow point observations from
multiple networks and data sources.

The current storage strategy is CSV-first:

- a station inventory in GeoJSON (`all_daily_snow_stations.geojson`)
- one CSV time-series file per station (`data/stations/*.csv`)
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
10. [License and Citation](#10-license-and-citation)

---

## 1. Project Structure

```text
global_snow_networks/
├── pixi.toml                              # Environment + task definitions
├── README.md                              # This file
├── all_daily_snow_stations.geojson                  # Merged daily-only station inventory
├── scripts/
│   ├── create_all_stations_geojson.py     # Build station GeoJSONs from all clients
│   ├── get_all_stations_data.py           # Refresh CSVs + archive + date fields
│   └── generate_live_map.py               # Build map HTML + chart JSON payloads
│
├── clients/
│   ├── __init__.py
│   ├── awdb_client.py                     # Compatibility shim (→ clients/awdb/)
│   ├── README.md                          # Client API docs
│   ├── awdb/
│   │   ├── awdb_client.py                 # AWDB REST API client
│   │   └── awdb_stations.geojson          # All AWDB snow stations (generated)
│   ├── cdec/
│   │   ├── cdec_client.py                 # CDEC (California) client
│   │   └── cdec_stations.geojson          # All CDEC snow stations (generated)
│   └── databc/
│       ├── databc_client.py               # BC Data Catalogue client
│       └── databc_stations.geojson        # All BC snow stations (generated)
│
├── data/
│   ├── stations/
│   │   └── *.csv                          # One CSV per station
│   └── all_station_csvs.tar.xz            # Bulk archive of all station CSVs
│
└── .github/workflows/
     └── daily_station_update.yml           # Daily automated refresh
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

---

## 3. Pipeline Overview

The pipeline is split into explicit stages:

```bash
# Stage 1: Build station GeoJSON inventories for all clients
#   Writes per-client GeoJSONs (clients/*/..._stations.geojson)
#   and the merged daily-only all_daily_snow_stations.geojson
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
4. Writes `all_daily_snow_stations.geojson` — a merged inventory of only those stations
   with at least one **daily** SWE or snow depth observation.

### 3.2 Stage 2: Refresh Per-Station CSV Data

Script: `scripts/get_all_stations_data.py`

What it does:

1. Reads stations from `all_daily_snow_stations.geojson`.
2. Routes each station to the appropriate client based on its `client` field.
3. Writes/replaces station CSVs atomically on successful fetch.
4. Updates date fields in the GeoJSON from the refreshed CSV content.
5. Writes `data/all_station_csvs.tar.xz`.

---

## 4. Live Map

The project includes an interactive map experience driven by per-station CSV data.

Generator script: `scripts/generate_live_map.py`

Primary outputs:
- `live_swe_map.html`
- `charts/*.json` (per-station chart payloads loaded by the popup chart panel)

Features:
- Interactive station markers and popups
- Variable toggling (WTEQ/SNWD)
- Period-of-record and normal-period comparisons
- Date slider behavior for current water year

### View on GitHub Pages

Configure GitHub repository Settings → Pages → Deploy from branch `main` / root.

Map URL: `https://<owner>.github.io/global_snow_networks/live_swe_map.html`

---

## 5. Data Model

### 5.1 Station Inventory: `all_daily_snow_stations.geojson`

`all_daily_snow_stations.geojson` is the **daily-only** merged metadata index.

**Common fields across all clients:**

| Field | Description |
| --- | --- |
| `code` | Native station identifier (e.g. `303_CO_SNTL`, `QUA`, `1A01P`) |
| `name` | Station name |
| `latitude`, `longitude` | WGS-84 coordinates |
| `elevation_m` | Elevation in metres |
| `Operator` | Operating agency |
| `client` | Source client: `"awdb"`, `"cdec"`, `"databc"` |
| `networkCode` | Network label (SNTL, SNTLT, CDEC, ASWS, …) |
| `state` | State or province code |
| `isActive` | Boolean active status |
| `beginDate`, `endDate` | Period of record from source metadata |
| `notes` | Any notable caveats (e.g. SNOTEL air temp bias status) |
| `station_url` | URL to station information page |
| `station_image_url` | Station photo URL (where available) |
| `metadata_fetched_at` | Date the metadata was fetched |
| `data_variables` | List of variable dicts `{name, type, interval, units, description, notes}` |
| `dailySWE` | `true` if station has daily SWE observations |
| `dailySnowDepth` | `true` if station has daily snow depth observations |
| `variables_daily` | Comma-separated list of daily variable names (derived from `data_variables`) |

**AWDB-specific additional fields:** `awdb_station_triplet`, `stationId`,
`county`, `huc`, `snowElements`, `elementCodes`, `variables_hourly`.

**CDEC-specific additional fields:** `is_snow_course`, `is_snow_pillow`,
`sensors`, `river_basin`, `april1_avg_swe_in`, `course_number`.

**DataBC-specific additional fields:** `station_type` (ASWS or MSS),
`status`.

#### Duplicate stations

The same physical station may appear in `all_daily_snow_stations.geojson` more than once
if it is accessible via multiple clients.  For example, some BC snow survey
stations and California CCSS stations appear in both AWDB (`MSNT` network) and
their respective native clients (DataBC, CDEC).  Each entry has a distinct
`client` field.  Downstream consumers can de-duplicate by matching on
`latitude`/`longitude`/`name`, or filter to a preferred client.

#### Per-client GeoJSONs

The per-client GeoJSONs in `clients/*/` contain **all** stations from each
source — including manual snow course sites that have only periodic
measurements and are excluded from `all_daily_snow_stations.geojson`.  These files
carry all available source metadata and serve as a complete reference for each
data source.

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
WA, WY) and some Canadian provinces (BC, AB, YK)
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
The `notes` field in `all_daily_snow_stations.geojson` indicates whether a correction
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
values. These stations are included in `all_daily_snow_stations.geojson`.

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
**These sites are NOT included in `all_daily_snow_stations.geojson`** (no daily data)
but appear in `clients/cdec/cdec_stations.geojson` with full metadata
including the `april1_avg_swe_in` (April 1 climatological average).

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
a full meteorological suite and are included in `all_daily_snow_stations.geojson`.

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
**MSS sites are NOT in `all_daily_snow_stations.geojson`** but appear in
`clients/databc/databc_stations.geojson`.

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

---

## 7. Data Access Methods and Design Philosophy

### 7.1 Client architecture

Each data source has a dedicated client module under `clients/`:

```
clients/awdb/awdb_client.py    → AWDBClient
clients/cdec/cdec_client.py    → CDECClient
clients/databc/databc_client.py → DataBCClient
```

**Invariants across all clients:**
- Return plain Python objects (dicts / lists); callers choose pandas/xarray.
- Metric-first: all values in SI units (centimetres for SWE and snow depth).
- Missing values normalised to `None` / `NaN`.
- Errors raise `{Client}Error(Exception)` with descriptive messages.
- `get_data(..., include_flags=True)` adds a `flag` key to each value record.

### 7.2 Variables and flags

Each client module exposes:
- **`SENSORS` / `VARIABLES`** — dict mapping variable codes to metadata
  (name, units, description).
- **`DATA_FLAGS`** — dict mapping flag codes to human-readable descriptions.
- **`DURATION_CODES`** — dict mapping duration codes to names.

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
df = client.get_mss_survey_data(
    location_ids=["1A06A", "1A10"],
    archive=True,
    include_flags=True,   # includes survey_code quality flag
)
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

gdf = gpd.read_file("all_daily_snow_stations.geojson")
# Filter to a single client
cdec = gdf[gdf["client"] == "cdec"]
print(cdec[["code", "name", "Operator", "dailySWE", "dailySnowDepth"]].head())
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

# Get daily SWE for current + archive season
df = client.get_asws_daily_data(
    location_ids=["1A01P", "1E08P"],
    archive=True,
)
print(df.head())

# Get snow course survey data for BC MSS stations
df_surveys = client.get_mss_survey_data(archive=True)
print(df_surveys.columns.tolist())
```

---

## 9. Known Caveats

### 9.1 AWDB network label semantics

AWDB network codes can be semantically misleading:

- Some BC snow survey stations appear under `MSNT` ("Manual SNOTEL"), which
  does not reflect their actual operating programme.
- Some California CCSS stations also appear under `MSNT`.

This project preserves source-provided AWDB network codes exactly to avoid
introducing ambiguity.  The `client` field in `all_daily_snow_stations.geojson`
distinguishes data sources; `networkCode` reflects what AWDB reports.

### 9.2 Duplicate stations

The same physical station may appear multiple times in `all_daily_snow_stations.geojson`
if accessible via more than one client.  This is intentional — each entry
reflects a distinct data access path with potentially different variables,
QC levels, or metadata.  De-duplicate by spatial proximity + name matching
if a single-entry view is needed.

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

---

## 10. License and Citation

Data accessed from AWDB is public domain (U.S. Government).
BC snow survey data is published under the Open Government Licence — British
Columbia.
CDEC data is published by CA DWR.

Suggested citations for source data:

> USDA Natural Resources Conservation Service (NRCS). Air and Water Database
> (AWDB) REST API v1. National Water and Climate Center, Portland, OR.
> <https://wcc.sc.egov.usda.gov/awdbRestApi/>

> California Department of Water Resources (CA DWR). California Data Exchange
> Center (CDEC). <https://cdec.water.ca.gov>

> BC Ministry of Environment. BC Snow Survey Network. BC Data Catalogue.
> <https://catalogue.data.gov.bc.ca>
