# global_snow_point_obs

A Python toolkit for archiving and accessing daily snow point observations — snow water equivalent (SWE) and snow depth — from the USDA NRCS Air and Water Database (AWDB) and other sources. Data is stored as a sparse, compressed [Zarr v3](https://zarr.dev/) store backed by [xarray](https://docs.xarray.dev/).

---

## Table of Contents

1. [Project Structure](#1-project-structure)
2. [Installation](#2-installation)
3. [Pipeline Overview](#3-pipeline-overview)
4. [Dataset Design](#4-dataset-design)
5. [Networks](#5-networks)
   - 5.1 [SNOTEL (SNTL)](#51-snotel-sntl)
   - 5.2 [SNOLite (SNTLT)](#52-snowlite-sntlt)
   - 5.3 [Manual SNOTEL (MSNT)](#53-manual-snotel-msnt)
   - 5.4 [Soil Climate Analysis Network (SCAN)](#54-soil-climate-analysis-network-scan)
   - 5.5 [NWS Cooperative Observer Network (COOP)](#55-nws-cooperative-observer-network-coop)
6. [Usage Examples](#6-usage-examples)
7. [License & Citation](#7-license--citation)

---

## 1. Project Structure

```
global_snow_point_obs/
├── pixi.toml                    # Pixi environment & task definitions
├── README.md                    # This file
│
├── clients/
│   ├── README.md                # Full client API documentation
│   ├── __init__.py
│   └── awdb_client.py           # AWDB REST API client
│
├── utils/
│   ├── __init__.py
│   └── utils.py                 # Water year / DOWY helpers
│
├── 01_get_stations_geojson.py   # Fetch station list + metadata → GeoJSON
├── 02_init_zarr.py              # Create empty sparse Zarr store
├── 03_fetch_zarr.py             # Populate Zarr store (resumable)
└── 04_inspect_zarr.py           # Validate + summarise completed store
```

---

## 2. Installation

This project uses [pixi](https://prefix.dev/docs/pixi/overview) for reproducible environments.

```bash
# Install pixi (if not already installed)
curl -fsSL https://pixi.sh/install.sh | bash

# Clone and set up the environment
git clone https://github.com/your-org/global_snow_point_obs.git
cd global_snow_point_obs
pixi install

# Activate a shell with the environment
pixi shell
```

---

## 3. Pipeline Overview

Run the four scripts in order. Steps 1 and 2 are one-time setup; step 3 is repeated until complete.

```bash
# Step 1 — Fetch all station metadata and create snow_stations.geojson (~10 min)
pixi run fetch-stations

# Step 2 — Create the empty sparse Zarr store (seconds)
pixi run init-zarr

# Step 3 — Download all historical data into the store (repeat until done)
#           Each run processes ~6 batches × 5 stations ≈ 4 min.
#           ~226 batches total → ~38 runs.
pixi run fetch-data

# Single-command continuous mode (runs passes until complete)
pixi run fetch-data-all

# Optional: tune per-pass batch count and sleep between passes
pixi run fetch-data-all --max-batches 6 --sleep-seconds 5

# Step 4 — Validate the completed store (optional)
pixi run inspect

# Step 5 — Generate live interactive map (after data complete)
pixi run live-map

# Or combine steps 4 & 5
pixi run map
```

### Live SWE Map

Once your Zarr store has been populated (or even while data is still downloading), you can generate an interactive **self-contained HTML map** showing:

- **Real-time SWE percentage-of-normal**: Each station color-coded by how close current SWE is to the historical median for the given day
- **Variable selector**: Switch between SWE and Snow Depth
- **Reference period selector**: Compare to Period of Record, 1991-2020, 1981-2010, or 1971-2000 normals
- **Basemap switching**: Explore with CartoDB Light, Esri Imagery, or Esri Topographic
- **Date slider**: Travel through the current water year and see how normals evolved day-by-day
- **Interactive station popups**: Click any station to see metadata and a Plotly chart overlaying historical percentiles
- **Network symbols**: Different marker shapes for different networks (SNTL, SNTLT, MSNT, SCAN, COOP, CCSS, MPRC, SNOW)
- **Offline-ready**: All data is embedded in a single HTML file (~1–3 MB for 1,000+ stations). No server required—works completely offline

```bash
# Generate the map (requires zarr store and snow_stations.geojson)
pixi run live-map

# Output: live_swe_map.html
# Open in any web browser; explore the snowpack in real time
```

The map updates dynamically as new data is fetched. Run `pixi run live-map` again after each major fetch pass to refresh the visualization.

---

## 4. Dataset Design

### Dimensions and Shape

| Dimension | Index type | Size |
|---|---|---|
| `station` | string triplet e.g. `303:CO:SNTL` | ≈ 1,128 |
| `time` | `pandas.DatetimeIndex` (daily, from 1896-10-01) | ≈ 47,600 |

### Variables

| Variable | Dims | dtype | Description |
|---|---|---|---|
| `WTEQ` | (station, time) | float32 | Snow Water Equivalent (cm) |
| `SNWD` | (station, time) | float32 | Snow Depth (cm) |

### Coordinates on `station`

| Coordinate | dtype | Description |
|---|---|---|
| `station` | str | AWDB station triplet (index) |
| `name` | str | Human-readable station name |
| `network` | str | Network code (SNTL, SNTLT, …) |
| `state` | str | Two-letter state/province code |
| `latitude` | float64 | Decimal degrees north |
| `longitude` | float64 | Decimal degrees east |
| `elevation_ft` | float32 | Elevation above MSL (feet) |
| `huc` | str | Hydrologic Unit Code |
| `wteq_begin` / `wteq_end` | str | WTEQ period of record (YYYY-MM-DD) |
| `snwd_begin` / `snwd_end` | str | SNWD period of record (YYYY-MM-DD) |

### Coordinates on `time`

| Coordinate | dtype | Description |
|---|---|---|
| `time` | datetime64[ns] | Calendar date (index) |
| `water_year` | int32 | Water year integer (e.g., 2024 for Oct 2023 – Sep 2024) |
| `dowy` | int16 | Day of water year (1 = Oct 1, 366 = Sep 30 in leap years) |

### Chunking and Sparsity

Chunks are `(1 station × 366 days)`, approximately one station-water-year. The store is **sparse**: only chunks that contain at least one non-NaN observation are written to disk. Stations that were not active in a given water year produce no chunk and consume no disk space. This results in very compact storage (expected ~40–100 MB on disk vs. ~430 MB dense).

### Spatial Subsetting

The dataset stores `latitude` and `longitude` coordinates for each station. You can subset geographic regions directly with boolean masks:

```python
co_bbox = ds.where(
    (ds["longitude"] >= -109.05) & (ds["longitude"] <= -102.05)
    & (ds["latitude"] >= 37.0) & (ds["latitude"] <= 41.0),
    drop=True,
)
```

---

## 5. Networks

All networks in this section are accessible via the [USDA NRCS AWDB REST API](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html) and provide **daily** SNWD and/or WTEQ observations. Networks with only periodic/manual records (e.g., CCSS snow courses, NRCS manual snow courses) are not included in the daily archive and are outside the current scope.

---

### 5.1 SNOTEL (SNTL)

**Data source:** USDA NRCS National Water and Climate Center (NWCC)  
**Stations in archive:** ~865  
**Coverage:** Western United States (AK, AZ, CA, CO, ID, MT, NV, NM, OR, UT, WA, WY) and some Canadian provinces (BC, AB, YK)  
**Period of record:** ~1978 – present  
**Temporal resolution:** Daily (and sub-daily / hourly)  
**Variables:** SWE (WTEQ), snow depth (SNWD), precipitation, air temperature, soil moisture, and more

SNOTEL (SNOw TELemetry) is the primary automated snow monitoring network in the western United States, operated by the NRCS NWCC. Established in the mid-1970s, the network now comprises over 900 automated, solar-powered stations installed at remote, high-elevation mountain watersheds. Data are transmitted via meteor-burst telemetry to a central database (AWDB / WCIS) several times per day. SNOTEL is the backbone of operational snowpack monitoring and water supply forecasting across the western U.S. and is widely used for climate research.

**Links**
- Network home: https://www.nrcs.usda.gov/programs-initiatives/snotel-snow-telemetry
- Interactive map: https://nwcc-apps.sc.egov.usda.gov/imap/
- Report generator: https://wcc.sc.egov.usda.gov/reportGenerator/
- Temperature bias correction (air temp only): https://www.wcc.nrcs.usda.gov/ftpref/support/air_temp_bias/nrcs_air_temp_unbias.html

#### Data Sources & Access Methods

| Tool / Source | Type | Language | Description | Link |
|---|---|---|---|---|
| AWDB REST API v1 | Primary API | Any | Modern JSON REST API. Full metadata, all elements, all durations. **Recommended for new projects.** | [Swagger docs](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html) · [Demo notebooks](https://github.com/nrcs-nwcc/iow_awdb_rest_api_demo) |
| AWDB SOAP API | Legacy API | Any | Older XML/SOAP web service. Full feature parity with REST API but more verbose. Still active and used by many existing tools. | [Reference](https://www.nrcs.usda.gov/wps/portal/wcc/home/dataAccessHelp/webService/webServiceReference/) · [User guide (PDF)](https://www.nrcs.usda.gov/sites/default/files/2023-03/AWDB%20Web%20Service%20User%20Guide.pdf) |
| CUAHSI WaterOneFlow | Standards API | Any | WaterML-based service exposing SNOTEL via CUAHSI HydroPortal. Useful for interoperability with CUAHSI ecosystem. | [WSDL endpoint](https://hydroportal.cuahsi.org/Snotel/cuahsi_1_1.asmx?WSDL) |
| metloom | Python package | Python | Unified interface to SNOTEL, CDEC, USGS, and others. Returns GeoDataFrames indexed on datetime + station. Actively maintained by M3Works. | [GitHub](https://github.com/M3Works/metloom) · [Docs](https://metloom.readthedocs.io/) |
| ulmo | Python package | Python | Hydrology/climate data access library including CUAHSI WOF (for SNOTEL). Older but still functional. | [GitHub](https://github.com/ulmo-dev/ulmo) · [Docs](https://ulmo.readthedocs.io/) |
| snotelr | R package | R | R interface to SNOTEL via AWDB. Includes snow phenology extraction. | [CRAN](https://cran.r-project.org/package=snotelr) · [GitHub](https://github.com/bluegreen-labs/snotelr) |
| soilDB::fetchSCAN | R package | R | Unified R interface to SCAN and SNOTEL via AWDB. Covers soil moisture sensors in addition to snow. | [Docs](https://ncss-tech.github.io/AQP/soilDB/fetchSCAN-demo.html) · [CRAN](https://cran.r-project.org/package=soilDB) |
| climata | Python package | Python | Lightweight Python AWDB/SNOTEL access. Low maintenance (last release 2017). | [GitHub](https://github.com/heigeo/climata) · [PyPI](https://pypi.org/project/climata/) |

**Pros of AWDB REST API (used in this project)**
- Modern JSON, no XML parsing
- Batch queries (up to 500k values per call)
- Supports all elements, durations, networks, and normals
- Active development by NRCS

**Cons of AWDB REST API**
- 500,000 value limit per request (requires batching for long time series)
- No public SLA; occasional downtime
- Rate limiting not documented but observed under heavy load

---

### 5.2 SNOLite (SNTLT)

**Data source:** USDA NRCS NWCC  
**Stations in archive:** ~44  
**Coverage:** Western United States  
**Period of record:** ~2011 – present  
**Temporal resolution:** Daily  
**Variables:** SWE (WTEQ), snow depth (SNWD), precipitation, temperature

SNOLite (or "SnowLite") stations use a simplified, lower-cost sensor package compared to full SNOTEL sites. They are intended to extend coverage into areas where full SNOTEL infrastructure is not cost-effective. SNOLite stations are stored in AWDB under the `SNTLT` network code and are accessible via the same APIs as SNOTEL. Data quality and sensor redundancy are somewhat lower than SNTL stations.

**Access methods:** Same as SNOTEL — all AWDB-based tools (REST API, SOAP, metloom, soilDB) support `SNTLT` stations transparently.

---

### 5.3 Manual SNOTEL (MSNT)

**Data source:** USDA NRCS NWCC  
**Stations in archive:** ~173  
**Coverage:** Western United States and Canada (BC, AB)  
**Period of record:** Varies widely; some from the 1960s  
**Temporal resolution:** Daily (telemetered observations)  
**Variables:** SWE (WTEQ), snow depth (SNWD)

Manual SNOTEL (`MSNT`) stations represent older or transitional sites that feed into the AWDB database but may use legacy telemetry or data entry methods. Despite the "manual" designation, they are stored with daily temporal resolution in AWDB. Many are historical records from retired sites or predecessor networks that predate the current automated SNOTEL system. Data density and quality vary significantly by station.

**Access methods:** Same as SNOTEL — all AWDB tools support `MSNT` stations.

---

### 5.4 Soil Climate Analysis Network (SCAN)

**Data source:** USDA NRCS NWCC  
**Stations in archive:** ~23 with daily SNWD/WTEQ  
**Coverage:** Nationwide (contiguous U.S.)  
**Period of record:** ~2000 – present  
**Temporal resolution:** Daily (and hourly)  
**Variables:** Soil moisture (multiple depths), soil temperature, air temperature, precipitation, SWE (WTEQ), snow depth (SNWD), wind

SCAN is a national network of automated stations focused primarily on **soil climate** monitoring — soil moisture and temperature at multiple depths — though many sites also measure snowpack where snow is present. SCAN is operated by NRCS in cooperation with USDA ARS and university partners. Unlike SNOTEL which is concentrated at high-elevation western sites, SCAN spans the full continental U.S. including the Southeast, Midwest, and East. Not all SCAN stations are in snowy climates; only ~23 report meaningful daily SNWD/WTEQ and are included in this archive.

**Links**
- Network home: https://www.wcc.nrcs.usda.gov/scan/
- Station map: https://www.wcc.nrcs.usda.gov/scan/app/station-map

#### Data Sources & Access Methods

| Tool / Source | Type | Language | Description | Link |
|---|---|---|---|---|
| AWDB REST API v1 | Primary API | Any | Same API as SNOTEL. SCAN stations use network code `SCAN`. Supports all elements including soil sensors. | [Swagger docs](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html) |
| soilDB::fetchSCAN | R package | R | Purpose-built R interface to SCAN (and SNOTEL). Returns named list of data frames by sensor type. Handles multi-depth soil sensor disambiguation. Most complete R interface for SCAN. | [Tutorial](https://ncss-tech.github.io/AQP/soilDB/fetchSCAN-demo.html) · [CRAN](https://cran.r-project.org/package=soilDB) |
| metloom | Python package | Python | Supports SCAN stations via AWDB. Returns GeoDataFrames. | [GitHub](https://github.com/M3Works/metloom) |

**Note on SCAN vs. SNOTEL via soilDB:** The `fetchSCAN()` function in soilDB supports both `SCAN` and `SNTL`/`SNTLT` network codes. The `SCAN_site_metadata()` function returns a unified metadata table for both networks, making it useful for cross-network analysis in R.

---

### 5.5 NWS Cooperative Observer Network (COOP)

**Data source:** NOAA National Weather Service / USDA NRCS (mirrored in AWDB)  
**Stations in archive:** ~23 with daily SNWD/WTEQ in AWDB  
**Coverage:** Western United States  
**Period of record:** Varies; some records extend back to the early 1900s  
**Temporal resolution:** Daily  
**Variables:** Snow depth (SNWD), SWE (WTEQ at some sites), temperature, precipitation

The NWS Cooperative Observer Program (COOP) is a nationwide volunteer weather observation network with over 8,500 active stations, some with records dating to the 1890s. A subset of COOP stations in AWDB also report snow-relevant elements (SNWD, WTEQ), generally in mountainous western states. These are the stations included here. Note that the much larger COOP network (outside AWDB) is managed by NOAA and accessible via GHCND — see below.

**Links**
- NOAA COOP: https://www.weather.gov/coop/
- GHCND (full COOP + global): https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily

#### Data Sources & Access Methods

| Tool / Source | Type | Language | Description | Link |
|---|---|---|---|---|
| AWDB REST API v1 | API | Any | For the ~23 COOP stations mirrored in AWDB. Network code `COOP`. | [Swagger docs](https://wcc.sc.egov.usda.gov/awdbRestApi/swagger-ui/index.html) |
| NOAA GHCND / CDO | API | Any | Full COOP network (and global stations). The authoritative source for long COOP records. Not via AWDB. | [CDO API](https://www.ncei.noaa.gov/cdo-web/webservices/v2) |
| meteostat | Python package | Python | Wraps GHCND and other sources. Easy access to daily station data globally. | [GitHub](https://github.com/meteostat/meteostat-python) |

---

## 6. Usage Examples

```python
import xarray as xr

ds = xr.open_zarr("snow_daily.zarr")

# ── Select a single station ───────────────────────────────────────────────────
s = ds.sel(station="303:CO:SNTL")
print(s["WTEQ"].dropna("time").tail(10))

# ── Select by network ─────────────────────────────────────────────────────────
sntl = ds.where(ds["network"] == "SNTL", drop=True)

# ── Select by water year ──────────────────────────────────────────────────────
wy2024 = ds.sel(time=ds["water_year"] == 2024)
median_swe = wy2024["WTEQ"].median(dim="station")

# ── Spatial subset via lat/lon bounding box ──────────────────────────────────
ds_in_co_bbox = ds.where(
    (ds["longitude"] >= -109.05) & (ds["longitude"] <= -102.05)
    & (ds["latitude"] >= 37.0) & (ds["latitude"] <= 41.0),
    drop=True,
)

# ── Load a subset into memory for computation ─────────────────────────────────
co_sntl = ds.sel(
    station=ds.where(
        (ds["state"] == "CO") & (ds["network"] == "SNTL"), drop=True
    ).station
).load()
```

---

## 7. License & Citation

Data accessed from AWDB is public domain (U.S. Government). Please cite:

> USDA Natural Resources Conservation Service (NRCS). Air and Water Database (AWDB) REST API v1. National Water and Climate Center, Portland, OR. https://wcc.sc.egov.usda.gov/awdbRestApi/

This toolkit is released under the MIT License.
