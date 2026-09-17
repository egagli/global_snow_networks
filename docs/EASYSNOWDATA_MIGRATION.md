# Migrating the clients into easysnowdata: what is done, what is left

Status as of 2026-09-17. The counterpart section is
[easysnowdata's `REVAMP_PLAN.md` §9](https://github.com/egagli/easysnowdata/blob/main/REVAMP_PLAN.md),
which decided this move (§9.2, option B) and records the runtime split (§9.3).

The short version: **the code has moved and both repos work, but this repo has
not yet switched over to consuming it.** Nothing here is urgent — the pipeline
runs fine against its local `clients/` — but the two copies will drift the
longer it waits.

---

## Done

- `clients/` is vendored into easysnowdata at `easysnowdata/stations/clients/`
  with its history, via `git subtree`. The five
  `clients/*/<name>_stations.geojson` pipeline artefacts are filtered out of
  the grafted history (33 MB refreshed daily, which would have added ~1.2 GB
  of blob history there and shipped stale station lists inside the wheel).
- easysnowdata wraps them in `easysnowdata.stations` — `inventory()`,
  `load()`, `metadata()` — and reads this repo's published archive through
  `easysnowdata.stations.archive`.
- Client fixes found during the migration landed **here first** and were then
  synced across: AWDB `TAVG`, AWDB `get_metadata` returning a dict for one
  station, Yukon barometric pressure in hPa, Yukon snowfall as its own type,
  the shared type vocabulary in `clients/_common.py`, and two stale tests.

## Left to do, in order

### 1. Wait for easysnowdata 0.2

Everything below depends on a released easysnowdata that contains
`easysnowdata.stations`. As of 2026-09-17 the newest tag is `v0.0.26` and the
work sits on the unmerged branch `revamp/phase3-stations`. **Do not start step
2 before the release**, or this repo's CI cannot resolve the pin.

### 2. Switch the imports (one PR)

- `scripts/create_all_stations_geojson.py` — 10 import lines
- `scripts/get_all_stations_data.py` — 5 import lines
- `scripts/generate_live_map.py` — 1 line:
  `from utils import day_of_water_year, water_year` becomes
  `from easysnowdata.processing.wateryear import day_of_water_year, water_year`
  (same values; verified against this repo's `utils/utils.py`)

Each `from clients.X import Y` becomes
`from easysnowdata.stations.clients.X import Y`. The dict-record contract is
unchanged, so nothing else in the pipeline moves.

### 3. Delete what has moved

- `clients/*.py` and `clients/*/` — but **not** the five
  `clients/*/<name>_stations.geojson`. Those are layer-2 pipeline artefacts
  (DESIGN.md §6.2) that happen to live under the code directory, and deleting
  the directory orphans them. Move them somewhere like `data/inventories/`
  first and **update DESIGN.md §6.2 to match**, since it names their path.
- `utils/` — `water_year` and `day_of_water_year` are in
  `easysnowdata.processing.wateryear`, along with `water_year_bounds`,
  `water_year_range` and `water_year_length` for the rest of what was there.
- `tests/test_awdb_client.py`, `test_cdec_client.py`, `test_databc_client.py`,
  `test_nve_client.py`, `test_yukon_client.py`, `test_clients_offline.py` —
  these live in easysnowdata's `tests/stations/` now.

Keep `tests/test_inventory_contract.py`, `test_pipeline_offline.py` and
`test_live_map_offline.py`: they test *this* repo's layers, not the clients.

### 4. Pin it

`pixi.toml` gains `easysnowdata = ">=0.2"` under `[dependencies]` (conda-forge)
or `[pypi-dependencies]`, whichever lands first for that release.

---

## Until step 2 happens: keeping the two copies in step

The vendored copy is byte-identical to `clients/` here, minus the filtered
artefacts. **Fix a client here first**, then in easysnowdata run:

```bash
./scripts/sync_clients.sh [path-to-this-repo] [branch]
```

which re-splits, re-filters and subtree-merges. Two things to know:

- **Do not rebase commits that touch `clients/`.** The sync depends on
  `git subtree split` plus the filter being deterministic, which holds only
  while those commits keep their SHAs. Verified: re-running the split
  reproduces the same SHAs exactly. Rebasing would silently produce a
  divergent split and a messy merge.
- **The client tests do not sync.** They sit outside the subtree prefix in
  both repos, so a test change here has to be carried over by hand. This has
  already bitten once — easysnowdata's copies were left asserting a type
  vocabulary that had moved on. Both sides now import the shared `TYPES`
  constant rather than restating it, which removes the commonest cause.

## Open questions, not blocking

- **The published inventory's `network` property means something else.** It is
  a Yukon-only display name ("Yukon Snow Survey Network"), null for the other
  four clients, while the obvious reading — and easysnowdata's — is the access
  path, which this repo calls `client`. easysnowdata renames the upstream
  value to `network_name` on read. Renaming it here would be a breaking change
  to a published artefact the live map consumes, so it needs a deliberate
  decision rather than a drive-by fix.
- **Archive storage.** See [`STORAGE.md`](STORAGE.md): the daily `tar.xz` is a
  27 MB blob committed daily that duplicates the CSV tree, and `.git` is now
  3.7 GB against a ~510 MB working tree. A chunked store in the Pages artefact
  would be smaller and allow partial reads.
