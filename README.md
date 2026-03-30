# global_snow_networks

A Python toolkit for archiving and accessing daily snow point observations from
the USDA NRCS Air and Water Database (AWDB) and additional networks over time.

The current storage strategy is CSV-first:

- a station inventory in GeoJSON (`snow_stations.geojson`)
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
7. [Usage Examples](#7-usage-examples)
8. [Known Metadata Caveat](#8-known-metadata-caveat)
9. [License and Citation](#9-license-and-citation)

---

## 1. Project Structure

```text
global_snow_networks/
├── pixi.toml                              # Environment + task definitions
├── README.md                              # This file
├── snow_stations.geojson                  # Station inventory + metadata
├── scripts/
│   ├── create_all_stations_geojson.py     # Build station GeoJSON from client
│   ├── get_all_stations_data.py           # Refresh CSVs + archive + date fields
│   └── generate_live_map.py               # Build map HTML + chart JSON payloads
│
├── clients/
│   ├── __init__.py
│   ├── awdb_client.py                     # AWDB REST API client
│   └── README.md                          # Client API docs + extension guidance
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

The pipeline is intentionally split into two explicit stages:

```bash
# Stage 1: Create fresh station inventory metadata
pixi run fetch-stations

# Stage 2: Fetch/update station CSVs, update GeoJSON record dates,
# and build tar.xz bundle
pixi run fetch-data

# Stage 3: Build the interactive live map and per-station chart payloads
pixi run live-map

# Convenience task for all stages
pixi run update-all
```

### 3.1 Stage 1: Create Station GeoJSON

Script: `scripts/create_all_stations_geojson.py`

What it does:

1. Queries supported network groups from the selected client (currently AWDB).
2. Filters to stations with daily snow observations (WTEQ and/or SNWD).
3. Pulls full station metadata for variable inventories and descriptors.
4. Writes `snow_stations.geojson` with per-station properties such as:
    - station identifiers (`code`, triplet mapping)
    - location/elevation/state/network metadata
    - daily/hourly variable inventories
    - station page/image URLs for SNOTEL/SNOLite when available

### 3.2 Stage 2: Refresh Per-Station CSV Data

Script: `scripts/get_all_stations_data.py`

What it does:

1. Reads stations from `snow_stations.geojson`.
2. Pulls fresh daily observations via the selected client (currently AWDB).
3. Writes/replaces station CSVs atomically only on successful station fetch.
4. Updates date fields in the GeoJSON from the refreshed CSV content:
    - `earliest_record_date`
    - `latest_record_date`
    - `updated_date`
5. Writes `data/all_station_csvs.tar.xz` containing all station CSVs.

### 3.3 Daily Automation

Workflow: `.github/workflows/daily_station_update.yml`

Daily job sequence:

1. install environment
2. run `scripts/create_all_stations_geojson.py`
3. run `scripts/get_all_stations_data.py`
4. run `scripts/generate_live_map.py`
5. commit/push changed artifacts to `main` when there are updates

---

## 4. Live Map

The project includes the same interactive map experience as the prior workflow,
now driven by per-station CSV data instead of Zarr.

Generator script:

- `scripts/generate_live_map.py`

Primary outputs:

- `live_swe_map.html`
- `charts/*.json` (per-station chart payloads loaded by the popup chart panel)

Feature parity goals:

- interactive station markers and popups
- variable toggling (WTEQ/SNWD)
- period-of-record and normal-period comparisons
- date slider behavior for current water year
- same basemap and chart style from prior implementation template

### View on GitHub Pages

This repository is configured to publish static map outputs on GitHub Pages.

Required repo setting:

1. GitHub repository Settings
2. Pages
3. Build and deployment source: `Deploy from a branch`
4. Branch: `main` and folder: `/ (root)`

Then the map is available at:

- `https://<owner>.github.io/global_snow_networks/live_swe_map.html`

The `.nojekyll` file is included so Pages serves static files directly, and
relative chart fetches from `charts/*.json` work correctly.

---

## 5. Data Model

### 5.1 Station Inventory: `snow_stations.geojson`

`snow_stations.geojson` is the metadata index for the archive and includes:

- geometry (point lat/lon)
- station identity and source keys
- network/state/county/huc/elevation
- available variables (`variables_daily`, `variables_hourly`, `elementCodes`)
- freshness and linkage fields (`csv_path`, refresh timestamp)
- record-date summary fields updated from CSV content

Identifier conventions:

- `code`: underscore format (`303_CO_SNTL`)
- `awdb_station_triplet`: colon format (`303:CO:SNTL`)

### 5.2 Station CSVs: `data/stations/*.csv`

Each station CSV currently follows this schema:

| Column | Type | Description |
| --- | --- | --- |
| `date` | `YYYY-MM-DD` string | Observation date |
| `wteq_cm` | float or null | Snow water equivalent in cm |
| `snwd_cm` | float or null | Snow depth in cm |

Notes:

- Units are centimeters (metric-first normalization in `AWDBClient`).
- Missing values are represented as empty/null values.
- A station may have one variable populated more consistently than the other.

### 5.3 Bulk Archive: `data/all_station_csvs.tar.xz`

The tarball contains all station CSVs under `stations/` to support efficient
single-file distribution for mirrors, cloud transfer, and reproducible snapshots.

---

## 6. Networks

All networks in this section are accessible via the
[USDA NRCS AWDB REST API](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html)
and provide daily SNWD and/or WTEQ observations.

Networks with only periodic/manual records (for example, CCSS snow courses and
NRCS manual snow courses that do not report daily values) are not included in
the daily archive and are outside the current scope.

The architecture is client-oriented rather than AWDB-only. Pipeline scripts are
designed to remain stable as new clients are added under `clients/`.

### 6.1 SNOTEL (SNTL)

**Data source:** USDA NRCS National Water and Climate Center (NWCC)  
**Stations in archive:** ~865  
**Coverage:** Western United States (AK, AZ, CA, CO, ID, MT, NV, NM, OR, UT,
WA, WY) and some Canadian provinces (BC, AB, YK)  
**Period of record:** ~1978 - present  
**Temporal resolution:** Daily (and sub-daily / hourly)  
**Variables:** SWE (WTEQ), snow depth (SNWD), precipitation, air temperature,
soil moisture, and more

SNOTEL (SNOw TELemetry) is the primary automated snow monitoring network in the
western United States, operated by the NRCS NWCC. Established in the mid-1970s,
the network now comprises over 900 automated, solar-powered stations installed
at remote, high-elevation mountain watersheds. Data are transmitted via
meteor-burst telemetry to a central database (AWDB / WCIS) several times per
day. SNOTEL is the backbone of operational snowpack monitoring and water supply
forecasting across the western U.S. and is widely used for climate research.

**Links**
- Network home: https://www.nrcs.usda.gov/programs-initiatives/snotel-snow-telemetry
- Interactive map: https://nwcc-apps.sc.egov.usda.gov/imap/
- Report generator: https://wcc.sc.egov.usda.gov/reportGenerator/
- Temperature bias correction (air temp only): https://www.wcc.nrcs.usda.gov/ftpref/support/air_temp_bias/nrcs_air_temp_unbias.html

#### Data Sources And Access Methods

| Tool / Source | Type | Language | Description | Link |
|---|---|---|---|---|
| AWDB REST API v1 | Primary API | Any | Modern JSON REST API. Full metadata, all elements, all durations. Recommended for new projects. | [Swagger docs](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html) - [Demo notebooks](https://github.com/nrcs-nwcc/iow_awdb_rest_api_demo) |
| AWDB SOAP API | Legacy API | Any | Older XML/SOAP web service. Full feature parity with REST API but more verbose. Still active and used by many existing tools. | [Reference](https://www.nrcs.usda.gov/wps/portal/wcc/home/dataAccessHelp/webService/webServiceReference/) - [User guide (PDF)](https://www.nrcs.usda.gov/sites/default/files/2023-03/AWDB%20Web%20Service%20User%20Guide.pdf) |
| CUAHSI WaterOneFlow | Standards API | Any | WaterML-based service exposing SNOTEL via CUAHSI HydroPortal. Useful for interoperability with CUAHSI ecosystem. | [WSDL endpoint](https://hydroportal.cuahsi.org/Snotel/cuahsi_1_1.asmx?WSDL) |
| metloom | Python package | Python | Unified interface to SNOTEL, CDEC, USGS, and others. Returns GeoDataFrames indexed on datetime + station. | [GitHub](https://github.com/M3Works/metloom) - [Docs](https://metloom.readthedocs.io/) |
| ulmo | Python package | Python | Hydrology/climate data access library including CUAHSI WOF (for SNOTEL). Older but still functional. | [GitHub](https://github.com/ulmo-dev/ulmo) - [Docs](https://ulmo.readthedocs.io/) |
| snotelr | R package | R | R interface to SNOTEL via AWDB. Includes snow phenology extraction. | [CRAN](https://cran.r-project.org/package=snotelr) - [GitHub](https://github.com/bluegreen-labs/snotelr) |
| soilDB::fetchSCAN | R package | R | Unified R interface to SCAN and SNOTEL via AWDB. Covers soil moisture sensors in addition to snow. | [Docs](https://ncss-tech.github.io/AQP/soilDB/fetchSCAN-demo.html) - [CRAN](https://cran.r-project.org/package=soilDB) |
| climata | Python package | Python | Lightweight Python AWDB/SNOTEL access. Low maintenance (last release 2017). | [GitHub](https://github.com/heigeo/climata) - [PyPI](https://pypi.org/project/climata/) |

**Pros of AWDB REST API (used in this project)**
- Modern JSON, no XML parsing
- Batch queries (up to 500k values per call)
- Supports all elements, durations, networks, and normals
- Active development by NRCS

**Cons of AWDB REST API**
- 500,000 value limit per request (requires batching for long time series)
- No public SLA; occasional downtime
- Rate limiting is not explicitly documented but can be observed under heavy load

### 6.2 SNOLite (SNTLT)

**Data source:** USDA NRCS NWCC  
**Stations in archive:** ~44  
**Coverage:** Western United States  
**Period of record:** ~2011 - present  
**Temporal resolution:** Daily  
**Variables:** SWE (WTEQ), snow depth (SNWD), precipitation, temperature

SNOLite (or SnowLite) stations use a simplified, lower-cost sensor package
compared to full SNOTEL sites. They are intended to extend coverage into areas
where full SNOTEL infrastructure is not cost-effective. SNOLite stations are
stored in AWDB under the `SNTLT` network code and are accessible via the same
APIs as SNOTEL. Data quality and sensor redundancy are generally lower than
full SNTL stations.

**Access methods:** Same as SNOTEL. All AWDB-based tools (REST API, SOAP,
metloom, soilDB) support `SNTLT` stations transparently.

### 6.3 Manual SNOTEL (MSNT)

**Data source:** USDA NRCS NWCC  
**Stations in archive:** ~173  
**Coverage:** Western United States and Canada (BC, AB)  
**Period of record:** Varies widely; some from the 1960s  
**Temporal resolution:** Daily (telemetered observations)  
**Variables:** SWE (WTEQ), snow depth (SNWD)

Manual SNOTEL (`MSNT`) stations represent older or transitional sites that feed
into the AWDB database but may use legacy telemetry or data entry methods.
Despite the manual label, they are stored with daily temporal resolution in
AWDB. Many are historical records from retired sites or predecessor networks
that predate the current automated SNOTEL system. Data density and quality can
vary significantly by station.

**Access methods:** Same as SNOTEL. All AWDB tooling supports `MSNT` stations.

### 6.4 Soil Climate Analysis Network (SCAN)

**Data source:** USDA NRCS NWCC  
**Stations in archive:** ~23 with daily SNWD/WTEQ  
**Coverage:** Nationwide (contiguous U.S.)  
**Period of record:** ~2000 - present  
**Temporal resolution:** Daily (and hourly)  
**Variables:** Soil moisture (multiple depths), soil temperature, air
temperature, precipitation, SWE (WTEQ), snow depth (SNWD), wind

SCAN is a national network of automated stations focused primarily on soil
climate monitoring (soil moisture and temperature at multiple depths), though
many sites also measure snowpack where snow is present. SCAN is operated by
NRCS in cooperation with USDA ARS and university partners. Unlike SNOTEL, which
is concentrated at high-elevation western sites, SCAN spans the full
continental U.S. including the Southeast, Midwest, and East. Not all SCAN
stations are in snowy climates; only a subset report meaningful daily
SNWD/WTEQ and are included in this archive.

**Links**
- Network home: https://www.wcc.nrcs.usda.gov/scan/
- Station map: https://www.wcc.nrcs.usda.gov/scan/app/station-map

#### Data Sources And Access Methods

| Tool / Source | Type | Language | Description | Link |
|---|---|---|---|---|
| AWDB REST API v1 | Primary API | Any | Same API as SNOTEL. SCAN stations use network code `SCAN`. Supports all elements including soil sensors. | [Swagger docs](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html) |
| soilDB::fetchSCAN | R package | R | Purpose-built R interface to SCAN (and SNOTEL). Returns named lists by sensor type and handles multi-depth soil sensor disambiguation. | [Tutorial](https://ncss-tech.github.io/AQP/soilDB/fetchSCAN-demo.html) - [CRAN](https://cran.r-project.org/package=soilDB) |
| metloom | Python package | Python | Supports SCAN stations via AWDB. Returns GeoDataFrames. | [GitHub](https://github.com/M3Works/metloom) |

**Note on SCAN vs. SNOTEL via soilDB:** `fetchSCAN()` in soilDB supports both
`SCAN` and `SNTL`/`SNTLT` network codes. `SCAN_site_metadata()` can return a
unified metadata table for both networks, which is useful for cross-network
analysis in R workflows.

### 6.5 NWS Cooperative Observer Network (COOP)

**Data source:** NOAA National Weather Service / USDA NRCS (mirrored in AWDB)  
**Stations in archive:** ~23 with daily SNWD/WTEQ in AWDB  
**Coverage:** Western United States  
**Period of record:** Varies; some records extend back to the early 1900s  
**Temporal resolution:** Daily  
**Variables:** Snow depth (SNWD), SWE (WTEQ at some sites), temperature,
precipitation

The NWS Cooperative Observer Program (COOP) is a nationwide volunteer weather
observation network with over 8,500 active stations, some with records dating
to the 1890s. A subset of COOP stations in AWDB also report snow-relevant
elements (SNWD, WTEQ), generally in mountainous western states. Those are the
stations included here. The much larger COOP network outside AWDB is managed by
NOAA and accessible via GHCND.

**Links**
- NOAA COOP: https://www.weather.gov/coop/
- GHCND (full COOP + global): https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily

#### Data Sources And Access Methods

| Tool / Source | Type | Language | Description | Link |
|---|---|---|---|---|
| AWDB REST API v1 | API | Any | For the AWDB-mirrored COOP subset. Uses network code `COOP`. | [Swagger docs](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html) |
| NOAA GHCND / CDO | API | Any | Full COOP network (and global stations). Authoritative source for long COOP records outside AWDB. | [CDO API](https://www.ncei.noaa.gov/cdo-web/webservices/v2) |
| meteostat | Python package | Python | Wraps GHCND and other sources for global daily station data access. | [GitHub](https://github.com/meteostat/meteostat-python) |

---

## 7. Usage Examples

### 7.1 Rebuild the Archive Locally

```bash
pixi run fetch-stations
pixi run fetch-data
pixi run live-map
```

### 7.2 Inspect Station Inventory in Python

```python
import geopandas as gpd

gdf = gpd.read_file("snow_stations.geojson")
print(gdf[["code", "networkCode", "state", "earliest_record_date", "latest_record_date"]].head())
```

### 7.3 Read One Station CSV in Python

```python
import pandas as pd

df = pd.read_csv("data/stations/303_CO_SNTL.csv", parse_dates=["date"])
print(df.tail())
print(df[["wteq_cm", "snwd_cm"]].describe())
```

### 7.4 Load Bulk Archive

```bash
tar -xJf data/all_station_csvs.tar.xz -C /tmp
ls /tmp/stations | head
```

---

## 8. Known Metadata Caveat

AWDB network labels can be semantically misleading for some station groups.

Examples:

- Some British Columbia Snow Survey stations appear under `MSNT`
  (often described as "Manual SNOTEL"), which does not reflect their actual
  operating program context.
- Some CCSS-related stations also appear with `MSNT` labels.

Current behavior in this repository:

- preserve source-provided AWDB network codes exactly
- document the caveat so downstream users do not over-interpret label semantics

---

## 9. License and Citation

Data accessed from AWDB is public domain (U.S. Government).

Suggested citation for source data:

> USDA Natural Resources Conservation Service (NRCS). Air and Water Database
> (AWDB) REST API v1. National Water and Climate Center, Portland, OR.
> <https://wcc.sc.egov.usda.gov/awdbRestApi/>
