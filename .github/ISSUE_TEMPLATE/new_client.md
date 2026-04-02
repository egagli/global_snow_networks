---
name: New data source client
about: Add support for a new snow monitoring network
title: "feat: add [NETWORK NAME] client"
labels: new-client
assignees: ''
---

## Network overview

<!--
Fill in what you know. The agent implementing this should discover
the rest by exploring the API / data portal directly.
-->

| Field | Value |
|-------|-------|
| Network name | <!-- e.g. BCWS Snow Survey --> |
| Operator / agency | <!-- e.g. BC Wildfire Service --> |
| Geographic coverage | <!-- e.g. British Columbia, Canada --> |
| Station count (approx.) | |
| Primary data URL / portal | <!-- public-facing URL --> |
| Data license | |
| Variables available | <!-- e.g. SWE, snow depth, air temp --> |
| Temporal resolution | <!-- e.g. daily, hourly, periodic --> |
| Historical depth | <!-- e.g. records from 1970 --> |

---

## Implementation checklist

Please implement the following network and client following the implementation checklist. Work through these steps **in order**.  Mark each task complete before
moving to the next.  Run all test code against the **live API** — do
not mock responses.  The network overview provided is just a very rough approximation of what to expect, it could be completely wrong our outdated. Please do your own research, and if you find a better way to utilize or access the data, please do so. For example, if daily SWE is available and we said it doesn't exist, prefer your own research and implement it. Or if the API example we gave you doesn't work as well as another, you can prefer your own research and testing and go the other way.

### 1. Explore the API / data format

- [ ] Identify the base URL(s) and authentication requirements (if any).
- [ ] Determine the station-list endpoint / file format (JSON, CSV, WFS, …).
- [ ] Fetch a sample station list and print 3–5 representative records in
  a notebook cell.
- [ ] Search for webpages for individual station in the network to find extra metadata, station urls, and station image urls.
- [ ] Identify all available variables, these could be different across different stations in the network (sensor names, units, element codes).
- [ ] Determine available temporal resolutions (daily, hourly, periodic).
- [ ] Identify any station-type distinctions (e.g. automated vs. manual).
- [ ] Document the data endpoint(s) and any required parameters
  (date range, station ID format, pagination).
- [ ] Fetch sample time-series data for 2–3 stations (one per station type)
  and print the raw response.

### 2. Create the client module

- [ ] Create `clients/<network>/` directory with `__init__.py`.
- [ ] Create `clients/<network>/<network>_client.py`.
- [ ] Define a module-level `VARIABLES` dict (or `SENSORS` for sensor-based
  networks). Each entry must include:
  ```python
  "ELEMENT_CODE": {
      "type": "<standardized_type>",   # see vocabulary below
      "units": "<native_units>",
      "description": "Human-readable description.",
      "notes": "",                     # or relevant caveats
      "source": "<endpoint_or_file>",  # where this variable comes from
  }
  ```
  **Standardized type vocabulary:**
  `"swe"`, `"snwd"`, `"temp"`, `"temp_max"`, `"temp_min"`,
  `"precip"`, `"rh"`, `"wind_spd"`, `"wind_gust"`, `"wind_dir"`,
  `"wind_run"`, `"solar"`, `"baro"`, `"density"`, `"snow_line"`,
  `"other"`

- [ ] Define `_TYPE_TO_<NETWORK>_VARS` dict mapping standardized type →
  list of native variable/sensor names.
- [ ] Define interval conversion dicts (native duration code ↔ standardized
  interval string: `"daily"`, `"hourly"`, `"sub_daily"`, `"periodic"`,
  `"monthly"`).

### 3. Implement `get_all_stations()`

Signature:
```python
def get_all_stations(
    self,
    active_only: bool = False,
    bbox: tuple[float, float, float, float] | None = None,
) -> list[dict]:
```

- [ ] Returns a list of station dicts.  Each dict **must** contain at
  minimum: `station_id` (or `location_id`), `name`, `latitude`,
  `longitude`, `elevation_m` (or derivable from feet), `status`.
- [ ] `active_only=True` filters to currently active stations.
- [ ] `bbox=(min_lon, min_lat, max_lon, max_lat)` spatially filters results.
- [ ] Write and run a notebook cell that calls `get_all_stations()` and
  prints the total count and a sample record.

### 4. Implement `get_data()`

Signature:
```python
def get_data(
    self,
    station_ids: list[str] | str | None = None,
    variables: list[str] | str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    begin_date: str | None = None,
    end_date: str | None = None,
    interval: str = "daily",
    include_flags: bool = False,
) -> list[dict]:
```

- [ ] Accepts **either** `station_ids` or `bbox` (raise `ValueError` if
  neither is provided).
- [ ] `variables` accepts **both** native names (e.g. `"swe_mm"`) and
  standardized types (e.g. `"swe"`); `None` returns all variables.
- [ ] Returns a **flat list** of observation records.  Every record must
  have exactly these keys (plus optional `"flag"` when
  `include_flags=True`):
  ```python
  {
      "station_id": str,   # native station identifier
      "date":       str,   # "YYYY-MM-DD"
      "variable":   str,   # native variable/sensor name
      "type":       str,   # standardized type (e.g. "swe")
      "value":      float | None,
      "units":      str,   # standardized output units (cm for swe/snwd)
      "interval":   str,   # "daily", "hourly", etc.
  }
  ```
- [ ] SWE (`type="swe"`) must be returned in **cm**.  Convert at the
  source if the API returns other units (e.g. mm ÷ 10, inches × 2.54).
- [ ] Snow depth (`type="snwd"`) must also be in **cm**.
- [ ] Rename the internal/native fetch function to
  `_get_data_<network>(...)` (private) and have the public `get_data()`
  call it after resolving variables and intervals.
- [ ] Write and run a notebook cell that fetches 1 year of daily SWE +
  snow depth for 3 stations and prints the first 10 records and the
  record count.

### 5. Add per-client GeoJSON support

In `scripts/create_all_stations_geojson.py`:

- [ ] Import any new constants needed
  (`VARIABLES`, interval-conversion dicts, etc.) from your client.
- [ ] Add `_<network>_data_variables(station: dict) -> list[dict]`
  helper.  Each entry must be:
  ```python
  {
      "name":        str,  # native variable/sensor name
      "type":        str,  # standardized type
      "interval":    str,  # "daily", "hourly", "sub_daily", "periodic"
      "units":       str,
      "description": str,
      "notes":       str,
  }
  ```
- [ ] Add `<network>_station_to_feature(station: dict) -> dict` function
  that builds a GeoJSON feature.  The `properties` dict **must** include:

  | Key | Description |
  |-----|-------------|
  | `code` | Native station ID |
  | `name` | Station name |
  | `latitude` / `longitude` | Decimal degrees |
  | `elevation_m` | Elevation in metres |
  | `state` | State / province / country code |
  | `Operator` | Operator string (used in map photo credit) |
  | `client` | Client name string (e.g. `"mynetwork"`) |
  | `networkCode` | Short network code shown on map |
  | `notes` | Free-text notes |
  | `station_url` | Link to station metadata page |
  | `station_image_url` | Station photo URL, usually available on each station's metadata page (if available; omit if not) |
  | `data_variables` | Output of `_<network>_data_variables(station)` |
  | `dailySWE` | `_has_daily_type(data_vars, "swe")` |
  | `dailySnowDepth` | `_has_daily_type(data_vars, "snwd")` |
  | `metadata_fetched_at` | `date.today().isoformat()` |

- [ ] Add `run_<network>_workflow()` that returns
  `(all_features, daily_features)` where `daily_features` is filtered
  to `f["properties"].get("dailySWE") or f["properties"].get("dailySnowDepth")`.
- [ ] Add `--skip-<network>` CLI flag in `main()`.
- [ ] Wire the workflow into `main()` with a `try/except` guard (same
  pattern as existing clients).
- [ ] Add the per-client GeoJSON output path to the `git add` loop in
  `.github/workflows/daily_station_update.yml`.
- [ ] Write and run `pixi run fetch-stations` locally and verify the
  per-client GeoJSON is written and valid.

### 6. Add data-refresh support

In `scripts/get_all_stations_data.py`:

- [ ] Add a `refresh_<network>()` function with the same signature as
  `refresh_awdb()` / `refresh_cdec()` / `refresh_databc()`.
- [ ] Call `client.get_data(station_ids=..., variables=["swe","snwd"], interval="daily", ...)`.
- [ ] Use `_station_records_to_df(station_records)` to convert flat
  records to a `{date, wteq_cm, snwd_cm}` DataFrame.
- [ ] Wire `refresh_<network>()` into `main()`.
- [ ] Write and run `pixi run fetch-data` locally for a small subset
  of stations (e.g. `--skip-awdb --skip-cdec`).

### 7. Interactive map

In `scripts/generate_live_map.py` (or `_HTML_TEMPLATE` within it):

- [ ] Verify the photo credit block handles the new network's
  `Operator` field correctly (it should work automatically via `s.op`).
- [ ] Run `pixi run live-map` and open the HTML to confirm the new
  stations appear on the map with correct popups.

### 8. Documentation

- [ ] Add the new network to the **Networks** table in `README.md`.
- [ ] Add the per-client GeoJSON path to the README file listing.
- [ ] Update the CSV schema docstring in `get_all_stations_data.py` if
  any new units/conversions are applied.

---

## Acceptance criteria

- [ ] `pixi run fetch-stations` completes without error and writes a
  valid per-client GeoJSON with `data_variables`, `dailySWE`, and
  `dailySnowDepth` on every feature.
- [ ] `all_daily_snow_stations.geojson` includes the new stations.
- [ ] `pixi run fetch-data` writes correct CSVs with `wteq_cm` and/or
  `snwd_cm` columns for at least one station.
- [ ] `pixi run live-map` produces a valid HTML map that shows the new
  stations.
- [ ] All test code was run against the **live API** (no mocked
  responses).
