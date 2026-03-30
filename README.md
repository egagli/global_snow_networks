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

This project currently fetches snow observations from AWDB network codes:

- `SNTL` (SNOTEL)
- `SNTLT` (SNOLite)
- `MSNT`
- `SCAN`
- `COOP`

The architecture is client-oriented rather than AWDB-only. The two pipeline
scripts accept a client selection argument and are designed to remain stable as
new clients are added under `clients/`.

### AWDB Data Access Notes

- API: `https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html`
- Daily retrieval uses batched requests to respect AWDB service limits.
- Snow variables `WTEQ` and `SNWD` are normalized to cm within the client.

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
