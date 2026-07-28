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

The **normative contract** for everything below is
[DESIGN.md](https://github.com/egagli/global_snow_networks/blob/main/DESIGN.md)
— client API (§3), daily semantics (§4), identity fields (§5), artifact
schema (§6). This checklist sequences the work; it does not restate the
contract, and where the two disagree, DESIGN.md wins.

Work through the steps **in order**. Mark each task complete before moving to
the next. Run all test code against the **live API** — do not mock responses.
The network overview above is a rough approximation and could be wrong or
outdated: do your own research, and if you find a better way to access the
data, prefer it. For example, if daily SWE is available and we said it doesn't
exist, trust your own findings and implement it.

### 1. Explore the API / data format

- [ ] Identify the base URL(s) and authentication requirements (if any).
- [ ] Determine the station-list endpoint / file format (JSON, CSV, WFS, …).
- [ ] Fetch a sample station list and print 3–5 representative records.
- [ ] Search the source's webpages for per-station metadata pages, station
  photos, and live/satellite cameras (`station_url`, `station_image_url`,
  `station_camera_url`).
- [ ] Identify all available variables — these can differ across stations
  in the network (sensor names, units, element codes).
- [ ] Determine available temporal resolutions (daily, hourly, periodic, …).
- [ ] Identify any station-type distinctions (e.g. automated vs. manual),
  and note what the source's network labels actually mean — never take
  network codes at face value (DESIGN.md §5).
- [ ] Document the data endpoint(s) and any required parameters
  (date range, station ID format, pagination).
- [ ] Fetch sample time-series data for 2–3 stations (one per station type)
  and print the raw response.
- [ ] Collect authoritative references (API docs, portal pages, network
  design reports) for `docs/SOURCES.md` and the per-client GeoJSON
  `metadata.references`.

### 2. Create the client module (DESIGN.md §3)

- [ ] Create `clients/<network>/` with `__init__.py` and
  `<network>_client.py` defining `<Network>Client` and
  `<Network>Error(Exception)`; export both from `clients/__init__.py`.
- [ ] Define the module-level `VARIABLES` registry — one entry per native
  variable with `type` / `units` / `output_units` / `description` /
  `notes` / `source` (§3.2 has the type vocabulary) — and `DATA_FLAGS`
  (an empty dict with a comment if the source has no flags).
- [ ] Use the shared helpers in `clients/_common.py` (retry loop, interval
  enum, list/date coercion, missing-value sentinels) — never re-implement
  them per client.
- [ ] Implement `get_all_stations(active_only=False, bbox=None)` per §3.4.
- [ ] Implement `get_data(...)` per §3.4: flat records, fully metric output
  units (§3.5 table), `datetime` on sub-daily records, `variables=None`
  meaning snow variables only, and no silent fallbacks — unknown variable
  or interval raises `<Network>Error` (§3.6).
- [ ] Implement `get_metadata(station_id)` including the station's
  variable/series inventory.
- [ ] Verify against the live API: station count + a sample record from
  `get_all_stations()`; 1 year of daily SWE + snow depth for 3 stations
  from `get_data()`.

### 3. Wire into the inventory (`scripts/create_all_stations_geojson.py`)

- [ ] Add `_<network>_data_variables(station) -> list[dict]` — one entry per
  series: `{name, type, interval, units, description, notes, begin_date,
  end_date, n_obs}` (the last three `None` where the source doesn't say).
- [ ] Add `<network>_station_to_feature(station)` building its properties
  through `make_feature()` (which enforces the §6.1 universal schema).
  `network_code` verbatim from the source; `operator` only when certain,
  else `None`; daily candidate flags via `_daily_candidate_props(...)`.
- [ ] Add `run_<network>_workflow()` returning
  `(all_features, daily_features)` and wire it into `main()` with the
  same `try/except` + `--skip-<network>` pattern as existing clients.
- [ ] Add the per-client GeoJSON path to the staging step in
  `.github/workflows/daily_station_update.yml`.
- [ ] Run `pixi run fetch-stations` locally: the per-client GeoJSON is
  valid and the new stations appear in `all_snow_stations.geojson` with
  the full universal schema.

### 4. Wire into the data refresh (`scripts/get_all_stations_data.py`)

- [ ] Add a `refresh_<network>()` following the existing refreshers: fetch
  daily SWE/snow depth via the client, write `date,wteq_cm,snwd_cm` CSVs
  atomically, and let the probe verify `daily_or_better` (DESIGN.md §4).
- [ ] Resample to daily **only if the source has no native daily series**,
  with `daily_provenance` set (§4).
- [ ] Route the network in the fetch-candidate logic and run
  `pixi run fetch-data-<network>` locally for a small subset.

### 5. Map, docs, and tests

- [ ] Add `NET_LABELS` / `NET_SHAPES` / `buildIcon()` entries in
  `scripts/generate_live_map.py`; run `pixi run live-map` and confirm the
  new stations render with correct popups (the legend builds itself from
  the inventory).
- [ ] Document the client in `clients/README.md` (API surface, quirks,
  units) and add the network to the root `README.md` Networks section and
  comparison table.
- [ ] Add offline parsing tests plus live-marked tests
  (`pytest.mark.live`); run `pixi run -e dev test-unit`, including the
  inventory contract test.

---

## Acceptance criteria

- [ ] `pixi run fetch-stations` completes without error; every new feature
  carries the DESIGN.md §6.1 universal schema (`null` when unavailable,
  never omitted).
- [ ] `all_snow_stations.geojson` includes the new stations with correct
  advertised `has_daily_swe` / `has_daily_snwd` candidates.
- [ ] `pixi run fetch-data` writes correct CSVs (`wteq_cm` / `snwd_cm`,
  metric, empty cells for missing) for at least one station, and the
  probe marks it `daily_or_better`.
- [ ] `pixi run live-map` produces a valid HTML map showing the new
  stations.
- [ ] All test code was run against the **live API** (no mocked responses),
  and `pixi run -e dev test-unit` is green.
