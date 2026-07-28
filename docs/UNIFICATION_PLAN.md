# Unification Plan

**Status:** approved 2026-07-28 (with clarifications below) — **implemented** (merged to `main` via PR #26, 2026-07-28); §7 items remain open as future issues
**Purpose:** unify the repository around a single, formalized design philosophy; fix all bugs found in the July 2026 audit; standardize the clients, artifacts, pipeline, map, docs, and tests so they all say and do the same thing.

This plan is organized as: the consolidated design philosophy (§0, to be promoted to `DESIGN.md`), six implementation phases (§1–§6), future work to capture as GitHub issues (§7), and proposed-unless-vetoed decisions (§8).

---

## 0. Consolidated design philosophy → `DESIGN.md`

The first deliverable is a `DESIGN.md` at the repo root containing the text below (sharpened as needed). Every other document — `README.md`, `clients/README.md`, `.github/ISSUE_TEMPLATE/new_client.md` — must defer to it rather than restate contracts in their own words. CI should eventually lint the overlap (see §6).

### 0.1 Scope

Document and provide access to **any public record of point observations of SWE or snow depth** — any interval, any network, observed (never modeled). A station qualifies if it has at least one SWE or snow-depth series. Snow courses, snow pits, and other periodic point observations are in scope for *documentation and access*; they are excluded from the *daily pre-download archive and map charts*.

### 0.2 The three layers (separation of concerns)

1. **Clients** (`clients/`) — pure data access. A client can retrieve **any met variable the source serves, at any interval the source serves** (sub-hourly included where available). Clients know nothing about GeoJSONs, CSVs, or the map. This layer will eventually migrate into `easysnowdata` as-is (dict-record API; no easysnowdata idiom adoption) — no migration work now, but no new coupling either.
2. **Archive pipeline** (`scripts/create_all_stations_geojson.py`, `scripts/get_all_stations_data.py`) — builds the station inventory and pre-downloads **daily-or-better** SWE/snow-depth into per-station CSVs. Where only hourly/sub-hourly exists, the pipeline resamples to daily itself and records that provenance. Native daily is always preferred over resampled.
3. **Visualization** (`scripts/generate_live_map.py`) — renders the inventory + archive. Contains no data-access or unit-conversion logic of its own.

### 0.3 Daily semantics (decided 2026-07)

- "Daily" in station metadata means **truly daily native data**.
- Inclusion in the pre-download archive and on the map means **daily-or-better**: if the source serves native daily, use it — always, even when hourly also exists. We resample (hourly/sub-hourly → daily) **only when no native daily series exists**.
- Inclusion is **probe-verified**: a station is only marked daily-capable after the pipeline has actually retrieved daily(-or-better) SWE or snow-depth values from it. Advertised sensor metadata and client-side capability flags are hints, never the criterion.
- **Regular cadence required**: one-off or sporadic point measurements are *not* "daily or better", regardless of the resolution the source labels them with. The probe requires a genuinely regular daily series (minimum observation count and density — see §8.6). Irregular records classify as `periodic`.
- **Inactive stations with a regular daily record stay in**: a station that is no longer active but did produce regular daily data remains pre-downloaded, archived, and on the interactive map. Active status affects display metadata, never inclusion.
- Resampled stations carry explicit provenance (`daily_provenance: "resampled_hourly"` etc.).

### 0.4 Network vs. operator vs. data provider

Three distinct concepts, three distinct fields:

- `network_code` — the monitoring program, as the source states it (SNTL, MSNT, CCSS, BCSS, …). Preserved verbatim; never taken at face value semantically (MSNT ≠ operated by NRCS).
- `operator` — the agency that maintains the physical site. Corrected from source-provided values **only when we are certain** (e.g. BC MSNT partner stations → BC ENV); otherwise left null. Never guessed.
- `client` — which module in this repo retrieves the data (the access path). `data_provider` — the human-readable organization/portal serving that access path (e.g. "USDA NRCS AWDB", "CDEC").

The same physical station may appear once per access path. De-duplication is punted to consumers, but duplicates are **annotated**: a `possible_duplicates` field cross-links candidates, and the map surfaces them.

### 0.5 Units (decided 2026-07: fully metric, explicit, obvious)

Every value a client emits is metric, converted at the source. `units` is present on every record. Canonical emitted units by type:

| type | unit |
| --- | --- |
| `swe` | cm |
| `snwd` | cm |
| `temp*` | °C |
| `precip*` | mm |
| `rh` | % |
| `density` | % |
| `wind_spd` / `wind_gust` | km/h |
| `wind_dir` | degrees |
| `wind_run` | km |
| `baro` | hPa |
| `snow_line` | m |
| `solar` | W/m² |

Variable registries carry both `units` (native) and `output_units` (emitted) — the Yukon pattern, adopted everywhere.

### 0.6 Data integrity

- **Correction over exclusion**: detect bad upstream data and correct (with a `notes` explanation) rather than drop. Stations that vanish upstream are retained in the inventory with a status note; their CSVs are kept.
- Implausibility clamps are scoped per variable type (never blanket filters like "null all negatives").
- Silent fallbacks are forbidden: unknown variable names, unknown intervals, and unroutable stations raise errors.

### 0.7 Metadata richness

Wherever the source offers it, capture: station page URL, station photo URL, station camera URL (e.g. BC snow-station satellite cameras), per-variable period of record and observation counts, and links to authoritative documentation (API docs, network design reports).

---

## Phase 1 — Correctness bugs

Fix these regardless of any redesign; each is independently shippable.

- [ ] **1.1 CDEC phantom daily stations.** Stop deriving `interval: "daily"` from mere sensor presence (`scripts/create_all_stations_geojson.py:166-168`; the dict branch at :169-176 is dead). Interim fix: use the client's duration-aware sensor data; real fix arrives with probe-verification (Phase 4). Expected effect: ~215 manual courses leave the daily inventory (~212 have no data at all).
- [ ] **1.2 `isActive` false for all 1,209 AWDB stations.** `not station.get("endDate")` at `create_all_stations_geojson.py:494` ignores AWDB's `2100-01-01` active sentinel. Treat `endDate` ≥ today as active.
- [ ] **1.3 Workflow discards GeoJSON updates.** `finalize` commits with `git add data/` only (`.github/workflows/daily_station_update.yml:188`), so CSV-derived date fields have never shipped and the map's "Last updated" shows the metadata-fetch date. Add the root GeoJSON to the commit.
- [ ] **1.4 Artifact clobber.** Each fetch job uploads *all* of `data/stations/` (fresh files + day-old copies of other networks); `merge-multiple: true` is last-writer-wins. Have each job upload only its own network's CSVs (per-network staging dir or path filter).
- [ ] **1.5 CI secrets.** `run-tests` calls `ci.yml` without `secrets: inherit` — NVE live tests run unauthenticated (401) in the daily workflow.
- [ ] **1.6 DataBC negative-value filter destroys sub-zero air temperature** (`clients/databc/databc_client.py:1637-1638` and :1276-1277). Scope the sentinel filter to non-negative variable types (swe, snwd, precip, wind_run, rh).
- [ ] **1.7 CDEC hourly collapse.** Timestamps are truncated at ingest (`cdec_client.py:1153-1164`), so hourly SWE keeps one value/day and hourly depth returns 24 timeless duplicates. Preserve timestamps; emit a `datetime` key on sub-daily records (Yukon convention).
- [ ] **1.8 NVE hourly records lack `datetime`** (`nve_client.py:886`). Same fix as 1.7.
- [ ] **1.9 DataBC `get_data(interval="hourly")` silently returns `[]`** (`databc_client.py:527`). Wire hourly/sub_daily to the hourly fetchers (`get_asws_sw_hourly_data`, `daily_only=False` paths); stop hardcoding `"interval": "daily"` (:628).
- [ ] **1.10 AWDB emits imperial** for non-snow variables (°F/in/mph passthrough, `awdb_client.py:793-797`). Convert everything per §0.5; fix the wrong °C/cm claims in `clients/README.md:70-77` and the imperial entries in `VARIABLES`.
- [ ] **1.11 Silent fallbacks → errors.** Unknown variable → raise (currently resolves to *all* variables, `awdb_client.py:150`, `cdec_client.py:193`, `yukon_client.py:1994`, `nve_client.py:1068`); unknown interval → raise (currently falls back to daily, or to instantaneous in Yukon); client-less GeoJSON feature → error, not silent AWDB routing (`get_all_stations_data.py:700`).
- [ ] **1.12 `clients/README.md` §5 (NVE) rewrite.** Wrong parameter IDs (says 2001/2002; truth: 2003=SWE, 2002=depth), claims no auth, wrong flag vocabulary, wrong variable key (`swe_mm` vs `swe_m`), missing `/Series`. Root README already has correct text. Fix the four stale docstrings inside `nve_client.py` (:427-428, :494-496, :561, :667) too.
- [ ] **1.13 Small cleanups:** CDEC dead SWE-priority branches (`cdec_client.py:786-801`); dead `_CDEC_DATA_SOURCE` with unexpanded `{BASE_URL}` (:77); unused `urlencode` import (`awdb_client.py:54`); inert `include_flags` on `get_asws_daily_data` (`databc_client.py:714`); `sensors` → `sensor_inventory` docstring (`cdec_client.py:476`); dead `awdb_station_to_feature(full_metadata=False)` branch; orphan CSVs (`962_AK_SNTL`, `2213_AK_SCAN`) — retain per §0.6 but flag in inventory rather than silently tarring.
- [ ] **1.14 `pixi.toml` identity:** still `name = "global-snow-point-obs"`, placeholder author, stale description, `playwright` declared twice and used nowhere.
- [ ] **1.15 CSV missing values:** write empty strings, not the literal string `nan` (`get_all_stations_data.py:121`), matching README §5.2.
- [ ] **1.16 Atomic writes** for the merged GeoJSON and tarball (CSV writes are already atomic; GeoJSON is a plain `open("w")` on a 31 MB tracked file).

## Phase 2 — Client contract standardization

Goal: the five clients present one API era. Prerequisite for the easysnowdata extraction later.

- [ ] **2.1 `clients/_common.py`:** shared `_date_str`, `_coerce_list`, `_to_float`, `_filter_by_bbox`, chunking, one retry/backoff loop (with 429 handling for all), one missing-value sentinel policy, the interval enum, and the §0.5 unit-conversion table.
- [ ] **2.2 Interval enum**, single source of truth: `periodic, monthly, semi_monthly, daily, sub_daily, hourly, sub_hourly, instantaneous, annual`. Kill `non-daily` (NVE sentinel invented in the geojson builder) and the leaking `calendar_year` (5,772 entries in the current merged file). Map NVE `resTime=0` to `instantaneous` so it becomes reachable.
- [ ] **2.3 Registry convention:** `VARIABLES` everywhere, keyed by string (CDEC keeps an int-sensor lookup internally); every entry gains `output_units`; add `DATA_FLAGS` to AWDB (or document flags as unavailable and drop the README §7.2 claim); drop the `DURATION_CODES` contract (exists only in CDEC) in favor of the shared enum.
- [ ] **2.4 Full met suites:** extend CDEC `SENSORS`→`VARIABLES` beyond sensors 3/18/82 (precip 2/45, temp 4/30/31/32, wind 9/10, RH 12, …— CDEC's `get_metadata` already scrapes the inventory it can't fetch); extend NVE beyond 2003/2002 (air temp, precip, soil water 2001 — correctly labeled). AWDB: add soil moisture elements (SMS etc.); investigate sub-hourly durations.
- [ ] **2.5 AWDB completeness:** query the `SNOW` and `MPRC` networks (NRCS's actual snow courses — currently absent from the whole repo, while the map template has dead legend entries for them); stop pre-filtering the per-client GeoJSON to daily (docstring and README both promise "ALL stations").
- [ ] **2.6 DataBC API shape:** make `get_data` the canonical record-returning interface with full interval support; move the 14 DataFrame methods behind it (private or explicitly documented as a convenience layer — see §8.4). Add `get_metadata` (only client without one).
- [ ] **2.7 Naming:** public API speaks `station_ids` everywhere (internal native vocab can stay); station dicts normalize `station_id`, `name`, `latitude`, `longitude`, `elevation_m`, `status` in AWDB too (currently raw `stationTriplet` camelCase passthrough); CDEC `get_metadata` returns `elevation_m` alongside `elevation_ft`.
- [ ] **2.8 Sub-daily records carry `datetime`** (Yukon pattern) in every client.
- [ ] **2.9 Update `.github/ISSUE_TEMPLATE/new_client.md`** to match: `get_all_stations` (not `get_stations`), the shared enum/units imports, `datetime` key, probe expectations, `output_units`.

## Phase 3 — Inventory & artifact schema redesign

One clean break (no downstream consumers confirmed 2026-07). Everything in one commit series so the map never sees a half-migrated schema.

- [ ] **3.1 One combined inventory: `all_snow_stations.geojson`** replaces `all_daily_snow_stations.geojson`. Contains **all** stations from all clients (~4,300 features incl. courses), with:
  - `has_daily_swe` / `has_daily_snwd` (probe-verified, per §0.3)
  - `daily_or_better` (bool — drives archive + map inclusion)
  - `daily_provenance`: `native` | `resampled_hourly` | `resampled_sub_hourly` | `none`
  - `earliest_record_date` / `latest_record_date` (from CSV content, finally persisted)
  Per-client GeoJSONs remain the full-metadata reference. Slim the fat first (e.g. AWDB's 57 always-empty `elementName` strings ×1,209 features) to keep size manageable; if the combined file still bloats, fall back to a slim-properties combined + rich per-client split.
- [ ] **3.2 snake_case property migration:** `Operator`→`operator`, `networkCode`→`network_code`, `isActive`→`is_active`, `dailySWE/dailySnowDepth`→`has_daily_swe/has_daily_snwd`, `beginDate/endDate`→`begin_date/end_date`. Remove redundant twins (CDEC `has_daily_*` vs `dailySWE`; `status` vs `isActive` — keep both `status` string and `is_active` bool, derived consistently).
- [ ] **3.3 Provider model (§0.4):** add `data_provider`; fix `operator` where certain — BC MSNT → "BC ENV", CA MSNT → CA DWR/CCSS operator when matched to the native-client twin; leave uncertain ones null. Normalize agency spellings ("USDA NRCS" vs "Natural Resources Conservation Service"; "BC ENV" vs "BC Ministry of Environment"); null out CDEC's `".None Specified"`. **Investigate AK (14) and AB (16) MSNT stations** — Alaska MSNT may genuinely be NRCS-operated manual sites (in which case `operator: USDA NRCS` is *correct* there); Alberta ones are likely Alberta Environment partners. Document findings in the network notes.
- [ ] **3.4 Duplicate annotation (not de-duplication):** at merge time, spatial+name matching (the audit found 168 cross-client pairs within 2 km) populates `possible_duplicates: [{code, client, distance_m}]` on each side. Consumers still de-duplicate themselves; we just make the links explicit.
- [ ] **3.5 `data_variables` enrichment:** add structured `begin_date`, `end_date`, `n_obs` per variable where the source catalog provides them (AWDB `stationElements` already returns per-element dates — currently stranded in the parallel `snowElements` array; Yukon has them as free text in `notes`; CDEC/DataBC/NVE need probing — populate lazily/approximately rather than not at all). Make DataBC entries per-station rather than per-station-type where the source allows. Harmonize the interval vocabulary to the §2.2 enum.
- [ ] **3.6 Context metadata on every station (§0.7):** `station_url`, `station_image_url`, and `station_camera_url` are **universal schema fields present on all features across all clients** — null where a station or network genuinely has none, never omitted from the schema. Populate: `station_url` for the 229 AWDB rows missing it; investigate photos for CDEC/NVE (Yukon publishes none); `station_camera_url` from the BC snow-station satellite cameras index (<https://www2.gov.bc.ca/gov/content/environment/air-land-water/water/water-science-data/water-data-tools/snow-survey-data/snow-station-satellite-cameras>), keyed by location ID.
- [ ] **3.7 Authoritative references:** a `references` block in each per-client GeoJSON `metadata` + a consolidated `docs/SOURCES.md` (API docs, portals, design reports — e.g. USBR *Emerging Snow Monitoring Technologies* report appendix, AWDB REST docs, HydAPI docs, AquaCache OpenAPI, CDEC pages, BC RFC pages).

## Phase 4 — Pipeline restructure

- [ ] **4.1 Probe-verified inclusion loop:** Stage 1 builds the combined inventory with *advertised* capabilities; Stage 2's actual fetch results set `has_daily_*`/`daily_or_better`/record dates; finalize commits the updated GeoJSON (with 1.3 fixed, this closes the loop). The probe enforces the §0.3 cadence rule (§8.6 threshold), so sporadic records classify as `periodic` even if the source labels them daily. Inactive stations with a regular daily record pass the probe on their historical data and **stay archived and on the map**. Stations failing the probe stay in the inventory as `daily_or_better: false` — filtered off the map, never deleted (§0.6). Bootstrap run probes all candidates once.
- [ ] **4.2 Resample-to-daily** for stations with **no native daily series** (native daily always wins when it exists): new shared resampler in the pipeline layer (not in clients); provenance recorded per §0.3. Follow the source's own daily convention where one exists; otherwise mean over the station-local day, documented per client. **Scale caveat (2026-07):** NVE's ~1,850 non-daily "snow-parameter stations" are largely *not* continuous hourly stations — many are one-off/irregular point observations, which per §0.3 must classify as `periodic`, not resample candidates. The probe (4.1) determines the true count of resample-eligible stations; expect it to be far smaller. Roll out per-network behind a flag (NVE last, respecting the 5 req/s key limit).
- [ ] **4.3 Upstream-disappearance retention:** extend `keep_previous_if_empty` to per-station retention with `status`/`notes` annotation instead of silent vanishing; stop silently sweeping orphan CSVs into the tarball — inventory them.
- [ ] **4.4 Per-client fetch granularity:** DataBC/NVE/Yukon currently make one `get_data` call for all stations — one exception zeroes the whole network for the day. Batch per station-group with per-batch error containment (AWDB/CDEC pattern); fix `refresh_databc` stats accounting.
- [ ] **4.5 CLI + logging consistency:** same flag vocabulary across all three scripts (`--skip-<client>` and `--only <client>`, one `--output` semantic); pick `logging` over `print` everywhere; make per-client GeoJSON output paths configurable for scratch runs.
- [ ] **4.6 De-duplicate constants:** the `wteq_cm/snwd_cm ↔ WTEQ/SNWD` map (defined in both pipeline scripts), NVE's registry (currently hardcoded in the geojson builder instead of importing `nve_client.VARIABLES`), DOWY/water-year logic (use `utils/` as the single Python implementation).

## Phase 5 — Live map

- [ ] **5.1 Template modernization:** rewrite `_HTML_TEMPLATE` to be cm-native and CSV-schema-current, deleting the three string-surgery unit patches (`generate_live_map.py:1938-1943`) and the excised dead chart loader (:1945-1988); generate `NET_LABELS`/`NET_SHAPES` from the inventory instead of hand-maintained JS literals; update `netOrder` (stops at BCSS today); derive `mtype` from station type instead of hardcoding `"automated"`.
- [ ] **5.2 Duplicate UX (decided 2026-07):** popup shows "⚠ potentially duplicated station" with links per `possible_duplicates`; clicking snaps/pans the map to that station and opens its popup.
- [ ] **5.3 Layer toggle:** default layer = daily-or-better stations (charts enabled); optional toggle layer for periodic/other point observations (distinct hollow/manual markers, metadata-only popups, no charts). Terminology on the control: "Daily stations" / "All point observations".
- [ ] **5.4 Popup context:** station photo where available (already BC/AWDB), camera link (`station_camera_url`), corrected "Last updated" (real data dates after 1.3/4.1), variables listed from `data_variables` (the current `variables_hourly` row renders "—" for all 757 non-AWDB stations), operator/data-provider shown per §0.4.
- [ ] **5.5 Repo hygiene:** untrack `live_swe_map.html` (29 MB, stale since April — Pages rebuilds it), `charts/` (1,486 files), and `.cache/` (add to `.gitignore`); verify `deploy-pages.yml` needs none of them tracked.

## Phase 6 — Tests, docs, hygiene

- [ ] **6.1 Offline unit tests for `scripts/`** (currently zero): the `_*_data_variables` builders, `_has_daily_type`, feature builders, `compute_record_dates`, chart stats, the new resampler and duplicate-matcher.
- [ ] **6.2 Artifact contract test:** validate the committed GeoJSON on every run — schema keys, every `daily_or_better` station has a CSV or an annotated reason, `is_active` sanity, interval vocabulary ∈ enum, no obs-count regressions. This class of test would have caught the phantom-CDEC, `isActive`, and discarded-date-fields bugs.
- [ ] **6.3 Test tiering:** `pytest` markers to split offline unit tests from live-API integration tests; `pixi run test-unit` / `test-live`; stop running the full live suite on every push to every branch.
- [ ] **6.4 Documentation reconciliation:** rewrite `README.md` and `clients/README.md` against `DESIGN.md` (fix all §6.9-style false claims — Yukon courses do *not* appear on the map today; per-client GeoJSONs "ALL stations" claim; §5.1 common-fields table); document `utils/`, `tests/`, `notebooks/`, all three workflows in the project tree; delete or modernize `notebooks/inspect_geojson.ipynb` (references `snow_stations.geojson`, `snow_daily.zarr`, colon-form IDs — all dead).
- [ ] **6.5 Provenance & citation:** add `CITATION.cff`; add a lineage section (v1 `snotel_ccss_stations` with its Zenodo DOI, v2 `snotel_ccss_stations_v2`, the `global_snow_point_obs` spike) and deprecation pointers in those repos toward this one; explicitly decide (and document) the fate of v2's dropped features — flags-in-CSV, retroactive-correction audit logs, adaptive lookback (see §8.5).
- [ ] **6.6 Fix the stale usage docstring** `from global_snow_point_obs.clients import AWDBClient` (`awdb_client.py:23`).

## 7. Future work (draft as GitHub issue text for Eric to file — never file directly)

Checked against existing issues 2026-07-28: **context satellite imagery is already issue #20**; **new networks are already issues #2–#18** (ECCC, SMHI, Hydro-Québec, Frost, NIVO, DWD, AEMET, SLF IMIS, ECA&D, FMI, NIWA, JMA, BoM, DGA Chile, SAIL) plus **#22 (NorSWE/CANSWE)**; **#19 (observation times)** overlaps the §8.1 daily-convention work — Phase 4 should document per-network observation times and reference #19. Genuinely new issues to draft:

1. **easysnowdata migration** — move `clients/` as-is into easysnowdata; this repo then imports it for pre-download + map. Blocked on Phases 1–2. Note easysnowdata currently pulls from the frozen v1 repo; repointing it is a separate migration step.
2. **v2 feature revival** — inline QC flags in the CSV archive and/or retroactive-change audit logs (both existed in `snotel_ccss_stations_v2`, dropped in the rewrite), if wanted in the new schema.
3. **Sub-hourly support audit** — which sources actually serve sub-hourly (AWDB? CDEC event data?) and wiring it through the interval enum.
4. **DataBC wide-CSV memory** — melting the full hourly archives (`SW_Archive.csv` etc.) in `_load_asws_wide_csv` can exhaust memory (observed OOM-kill on a small machine, 2026-07). Chunked parsing or per-station column selection would fix it; affects `get_data(interval="hourly")` over long periods.

## 8. Proposed decisions (acting on these unless vetoed)

1. **Daily resample convention (clarified 2026-07):** resampling happens **only when the source has no native daily series** — if daily exists alongside hourly, use the daily. When we do resample: follow the source's own daily convention where one exists (e.g. DataBC 16:00 UTC reading, Yukon `measurementsDaily` local-day mean); otherwise mean over the station-local day. Recorded per client in `DESIGN.md`.
2. **Pressure standardized to hPa** (DataBC native; Yukon kPa ×10). Precip to mm; SWE/depth stay cm (§0.5 table).
3. **Combined-file strategy:** single `all_snow_stations.geojson` with `daily_or_better` flag replaces `all_daily_snow_stations.geojson` entirely (per your suggestion), with per-client files as the full-metadata reference; fall back to a slim-combined + rich-per-client split only if file size becomes a problem after de-bloating.
4. **DataBC DataFrame methods become private** (`_get_asws_*`); `get_data` records are the public contract. (Alternative: keep them public but documented as a convenience layer — say the word.)
5. **v2's flags/audit machinery stays dropped for now** (revival captured as issue #4 above, not in this plan's scope).
6. **Cadence threshold for `daily_or_better`:** a station's daily(-or-resampled) SWE/depth series must contain **≥ 30 observations** and average **≥ 1 observation per 3 days over its densest 90-day window** — enough to reject one-off and sporadic point measurements while admitting seasonal stations (snow pillows that only report in winter) and short-lived but genuine daily stations. Tune the numbers during Phase 4 bootstrap against known-good and known-sporadic examples.

## 9. Sequencing

Phase 1 is independent and immediately shippable (bug-fix PRs). Phase 2 before Phase 3 (the artifact schema depends on the client contract). Phase 3 + 4 land together (probe/provenance fields are part of the new schema; workflow must commit the GeoJSON for any of it to matter). Phase 5 follows 3/4 (map reads the new schema). Phase 6 runs alongside everything, with 6.2 (contract test) landing as early as possible — ideally with Phase 1 so regressions get caught during the migration itself.
