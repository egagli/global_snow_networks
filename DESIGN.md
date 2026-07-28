# Design

This document is the **normative contract** for `global_snow_networks`. Every
other document — `README.md`, `clients/README.md`, the new-client issue
template — defers to it; where they disagree, this file wins and the others
are bugs. Migration status toward this contract is tracked in
[docs/UNIFICATION_PLAN.md](docs/UNIFICATION_PLAN.md).

## 1. Purpose and scope

Document and provide access to **any public record of point observations of
SWE or snow depth** — any interval, any network, observed (never modeled). A
station qualifies for the inventory if it has at least one SWE or snow-depth
series. Snow courses, snow pits, and other periodic point observations are in
scope for *documentation and access*; the *daily pre-download archive and map
charts* are restricted to stations with daily-or-better data (§4).

## 2. The three layers

1. **Clients** (`clients/`) — pure data access. A client can retrieve **any
   met variable the source serves, at any interval the source serves**
   (sub-hourly included where available). Clients know nothing about
   GeoJSONs, CSVs, or the map. This layer will eventually migrate into
   [easysnowdata](https://github.com/egagli/easysnowdata) as-is (dict-record
   API); until then, no new coupling to the rest of this repo.
2. **Archive pipeline** (`scripts/create_all_stations_geojson.py`,
   `scripts/get_all_stations_data.py`) — builds the station inventory and
   pre-downloads daily-or-better SWE/snow-depth into per-station CSVs.
   Resampling, probing, retention, and provenance live here, never in
   clients.
3. **Visualization** (`scripts/generate_live_map.py`) — renders the inventory
   and archive. Contains no data-access or unit-conversion logic of its own.

## 3. Client contract

### 3.1 Module layout

Each source gets `clients/<name>/` containing `__init__.py` and
`<name>_client.py` defining `<Name>Client` and `<Name>Error(Exception)`.
Shared helpers (retry loop, date/list coercion, bbox filtering, sentinel
policy, interval enum, unit conversions) live in `clients/_common.py` — never
re-implemented per client.

### 3.2 Variable registry

Every client module exposes a `VARIABLES` dict keyed by the **native**
variable/sensor name (string). Each entry:

```python
"NATIVE_NAME": {
    "type": "<standardized type>",   # vocabulary below
    "units": "<native units>",       # what the source serves
    "output_units": "<emitted units>",  # what get_data() returns (§3.5)
    "description": "Human-readable description.",
    "notes": "",                     # caveats
    "source": "<endpoint or file>",  # where this variable comes from
}
```

**Type vocabulary:** `swe`, `snwd`, `temp`, `temp_max`, `temp_min`,
`precip`, `rh`, `wind_spd`, `wind_gust`, `wind_dir`, `wind_run`, `solar`,
`baro`, `density`, `snow_line`, `soil_moisture`, `other`.

Every client also exposes `DATA_FLAGS` (flag code → description; empty dict
with a comment if the source has no flags).

### 3.3 Interval enum

One shared vocabulary, defined once in `clients/_common.py`:

`periodic`, `monthly`, `semi_monthly`, `daily`, `sub_daily`, `hourly`,
`sub_hourly`, `instantaneous`, `annual`.

Clients map native duration codes to this enum and back. Values outside the
enum (e.g. a raw source duration string) must never leak into records or
artifacts.

### 3.4 Method contract

Required on every client:

```python
def get_all_stations(self, active_only: bool = False,
                     bbox: tuple[float, float, float, float] | None = None) -> list[dict]
```

Station dicts contain at minimum `station_id`, `name`, `latitude`,
`longitude`, `elevation_m`, `status` — snake_case, normalized, regardless of
what the source calls them. Source-specific extras ride along.

```python
def get_data(self, station_ids=None, variables=None, bbox=None,
             begin_date=None, end_date=None, interval="daily",
             include_flags=False) -> list[dict]
```

- Requires `station_ids` or `bbox` (else `ValueError`).
- `variables` accepts native names and standardized types; `None` means all
  **snow** variables (`swe` + `snwd`), not everything.
- Returns a flat list of records:

```python
{
    "station_id": str,
    "date":       str,          # "YYYY-MM-DD"
    "datetime":   str,          # ISO 8601 — REQUIRED for sub-daily intervals
    "variable":   str,          # native name
    "type":       str,          # standardized type
    "value":      float | None,
    "units":      str,          # emitted units (§3.5)
    "interval":   str,          # enum value (§3.3)
    "flag":       str | None,   # only when include_flags=True
}
```

```python
def get_metadata(self, station_id: str) -> dict
```

Includes the station's variable/series inventory.

### 3.5 Units: fully metric, explicit, obvious

Every value `get_data()` emits is metric, converted at the source. Canonical
emitted units by type:

| type | unit |
| --- | --- |
| `swe` | cm |
| `snwd` | cm |
| `temp*` | °C |
| `precip*` | mm |
| `rh`, `density`, `soil_moisture` | % |
| `wind_spd`, `wind_gust` | km/h |
| `wind_dir` | degrees |
| `wind_run` | km |
| `baro` | hPa |
| `snow_line` | m |
| `solar` | W/m² |

No imperial passthrough, ever. `units` is present on every record and states
the emitted unit.

### 3.6 No silent fallbacks

- Unknown variable name → raise `{Client}Error` (never "fetch everything").
- Unknown interval → raise `{Client}Error` (never "assume daily").
- Physically implausible values are nulled with a scoped, per-type clamp
  (e.g. SWE outside 0–15 m), never a blanket rule like "negative = invalid"
  (air temperature is legitimately negative).
- Missing values are `None`. Sentinels (−9999 family) are handled in
  `_common`.

### 3.7 Errors and retries

All source/API failures raise `{Client}Error` with a descriptive message.
Argument-validation failures raise `ValueError`/`TypeError`. The shared retry
loop handles transient HTTP errors and honours `Retry-After` on 429.

## 4. Daily semantics

- "Daily" in metadata means **truly daily native data**.
- Archive/map inclusion means **daily-or-better**: if the source serves
  native daily, use it — always, even when hourly also exists. The pipeline
  resamples (hourly/sub-hourly → daily) **only when no native daily series
  exists**, following the source's own daily convention where one exists,
  otherwise the mean over the station-local day. The convention used is
  documented per client.
- Inclusion is **probe-verified**: a station is marked daily-capable only
  after the pipeline has actually retrieved daily(-or-better) SWE or
  snow-depth values. Advertised sensor metadata and capability flags are
  hints, never the criterion.
- **Regular cadence required**: one-off or sporadic measurements are not
  "daily or better" regardless of how the source labels them. The probe
  requires a genuinely regular series (≥ 30 observations and ≥ 1 obs per
  3 days over the densest 90-day window; thresholds tunable). Irregular
  records classify as `periodic`.
- **Inactive stations with a regular daily record stay in** — pre-downloaded,
  archived, and on the map. Active status affects display metadata only,
  never inclusion.
- Resampled stations carry provenance (`daily_provenance`).

## 5. Station identity: network, operator, data provider

Three distinct concepts, three distinct fields:

- `network_code` — the monitoring program, **as the source states it**
  (SNTL, MSNT, CCSS, BCSS, …). Preserved verbatim, never semantically
  trusted (AWDB's "Manual SNOTEL" label on BC/CA partner stations does not
  mean NRCS operates them).
- `operator` — the agency maintaining the physical site. Corrected from
  source values **only when certain**; otherwise `null`. Never guessed.
- `client` — the module in this repo that retrieves the data (the access
  path). `data_provider` — the human-readable organization/portal behind
  that access path (e.g. "USDA NRCS AWDB", "CDEC").

The same physical station may appear once per access path — intentionally.
De-duplication is the consumer's job, but duplicates are annotated: a
`possible_duplicates` list (`{code, client, distance_m}`) cross-links
candidates found by spatial + name matching, and the map surfaces them.

## 6. Artifact contract

### 6.1 `all_snow_stations.geojson` (combined inventory)

One file, **all** stations from all clients, including periodic sites.
All property keys are `snake_case`. Universal properties (present on every
feature; `null` when unavailable, never omitted):

`code`, `name`, `latitude`, `longitude`, `elevation_m`, `state`,
`network_code`, `operator`, `client`, `data_provider`, `status`,
`is_active`, `begin_date`, `end_date`, `earliest_record_date`,
`latest_record_date`, `station_url`, `station_image_url`,
`station_camera_url`, `notes`, `data_variables`, `has_daily_swe`,
`has_daily_snwd`, `daily_or_better`, `daily_verified`,
`daily_provenance`, `possible_duplicates`, `metadata_fetched_at`.

`has_daily_swe` / `has_daily_snwd` are **advertised** candidates,
rebuilt from source metadata on every inventory rebuild.
`daily_or_better` is the **probe's verdict** (`daily_verified` says
whether the probe has run); the probe's verdict is carried forward
across rebuilds and only the next probe changes it.  The fetch tries
every station where either signal is positive, so a station that newly
advertises daily data is probed even after a failed verification.

`data_variables` entries: `{name, type, interval, units, description, notes,
begin_date, end_date, n_obs}` — the last three `null` where the source
doesn't say. `interval` uses the §3.3 enum.

Client-specific extras are allowed but namespaced by convention
(documented per client in `README.md`).

### 6.2 Per-client GeoJSONs (`clients/*/<name>_stations.geojson`)

The complete, unfiltered station list from each source — including periodic
sites — with all available source metadata. **Never pre-filtered to daily**
(that is the combined file's `daily_or_better` flag's job). Each file's
`metadata` block carries a `references` list of authoritative links (API
docs, portals, design reports).

### 6.3 Station CSVs (`data/stations/<code>.csv`)

Header `date,wteq_cm,snwd_cm`; missing values are **empty**, never the
string `nan`. Daily-or-better stations only. Flags, other variables, and
sub-daily data are not stored — use the clients. Writes are atomic
(temp file + rename); the same applies to GeoJSON and archive writes.

### 6.4 Retention (correction over exclusion)

Detect bad upstream data and correct it (with a `notes` explanation) rather
than dropping stations. Stations that vanish upstream are retained in the
inventory with a status note; their CSVs are kept and inventoried, never
silently orphaned or silently deleted.

## 7. Context metadata and references

Wherever the source offers it, capture per station: `station_url` (station
page), `station_image_url` (photo), `station_camera_url` (live/satellite
camera, e.g. BC snow-station satellite cameras). These fields exist on every
feature across all clients — `null` where a network has none.

Authoritative documentation (API references, network design reports such as
the USBR *Emerging Snow Monitoring Technologies* appendix) is indexed in
`docs/SOURCES.md` and per-client GeoJSON `metadata.references`.

## 8. Visualization principles

- The map reads the combined inventory + CSV archive only; no live API calls
  and no unit conversions in the viz layer.
- Default layer: `daily_or_better` stations with charts. Optional toggle
  layer: all other point observations (distinct markers, metadata-only
  popups).
- Popups show operator/data-provider per §5, real data-record dates,
  station photo and camera links, and "potentially duplicated station"
  links that pan to the twin marker.
- Network label/shape vocabularies are generated from the inventory, not
  hand-maintained in the template.

## 9. Testing policy

- Offline unit tests cover pipeline logic (feature builders, probe,
  resampler, duplicate matcher, chart stats) and client parsing helpers.
- A **contract test** validates the committed inventory: schema keys,
  interval vocabulary ∈ enum, every `daily_or_better` station has a CSV or
  an annotated reason, `is_active` sanity.
- Live-API tests are marked (`pytest.mark.live`) and run on schedule, not on
  every push.

## 10. Documentation policy

`DESIGN.md` (this file) owns contracts. `README.md` documents usage, the
data model as shipped, and per-network detail. `clients/README.md` documents
each client's API surface and quirks. The new-client issue template is a
checklist that references this file rather than restating it. When code and
docs disagree, fix whichever is wrong *and* add the missing test or lint
that would have caught it.
