# Migrating the clients into easysnowdata: what happened, what is left

Status as of 2026-09-17: **done, apart from the version pin.** The counterpart
section is
[easysnowdata's `REVAMP_PLAN.md` §9](https://github.com/egagli/easysnowdata/blob/main/REVAMP_PLAN.md),
which decided the move (§9.2, option B) and records which repository answers
which request (§9.3).

The short version: the five network clients and the water-year helpers live in
easysnowdata, this repo imports them, and there is now exactly one copy of
each. What stayed here is what this repo is for — the inventory, the daily CSV
archive, and the map.

---

## What moved

| Was here | Is now |
| --- | --- |
| `clients/<name>/<name>_client.py` | `easysnowdata.stations.clients.<name>` |
| `clients/_common.py` | `easysnowdata.stations.clients._common` |
| `clients/README.md` | [`easysnowdata/stations/clients/README.md`](https://github.com/egagli/easysnowdata/blob/main/easysnowdata/stations/clients/README.md) |
| `utils/` (water year, day of water year) | `easysnowdata.processing.wateryear` |
| `tests/test_*_client.py`, `tests/test_clients_offline.py` | easysnowdata's `tests/stations/` |

`clients/` came over with its history via `git subtree`, minus the five
`clients/*/<name>_stations.geojson` artefacts, which were filtered out of the
grafted history — 33 MB refreshed daily here, which would have added ~1.2 GB
of blob history there and shipped stale station lists inside the wheel.

**Fix a client in easysnowdata now.** DESIGN.md §3 is still its contract and
this repo is still where the contract is written, but the code and its tests
are there. `scripts/sync_clients.sh` has been deleted from that repo; there is
nothing left to sync.

## What stayed, and moved within this repo

The per-client inventories are `data/inventories/<name>_stations.geojson` now.
They are layer-2 artefacts (DESIGN.md §6.2) that happened to live under the
code directory; deleting that directory would have orphaned them. Their
content and schema are unchanged. If anything of yours reads them by raw URL,
those five URLs changed — `all_snow_stations.geojson` and `data/stations/`,
which are what easysnowdata reads, did not.

## One behaviour change

easysnowdata's NVE client resolves `NVE_API_KEY` through that package's `nve`
auth provider, which raises `CredentialError` with the signup URL before any
request goes out. It used to warn once and let HydAPI answer 401 to
everything. `refresh_nve` caught only `NVEError`, so an unset key would have
aborted the run; it resolves the credential up front now and reports a missing
key as one counted, skipped network.

## Left to do

**Move the dependency to conda-forge.** easysnowdata 0.2.0 was released on
2026-09-17, so `pixi.toml` pins a version rather than a commit now — but from
PyPI:

```toml
[pypi-dependencies]
easysnowdata = ">=0.2"
```

conda-forge is still at 0.0.24, and its recipe needed repairing rather than
bumping: `host:` still named setuptools long after the build backend became
hatchling, which fails the build outright under `--no-build-isolation`, and
the run list had drifted by eighteen packages.
[conda-forge/easysnowdata-feedstock#10](https://github.com/conda-forge/easysnowdata-feedstock/pull/10)
fixes it. When that lands, delete this table and put the same line under
`[dependencies]`; `pixi lock` then moves ~38 PyPI packages per platform to
conda-forge.

## Open questions, not blocking

- **The published inventory's `network` property means something else.** It is
  a Yukon-only display name ("Yukon Snow Survey Network"), null for the other
  four clients, while the obvious reading — and easysnowdata's — is the access
  path, which this repo calls `client`. easysnowdata renames the upstream
  value to `network_name` on read. Renaming it here would be a breaking change
  to a published artefact the live map consumes, so it needs a deliberate
  decision rather than a drive-by fix.
- **Yukon `precip_snow_cm`** is typed `snowfall` now rather than `precip`, so
  nothing rescales 5 cm of snow into 50 mm of water. `snowfall` is in the
  shared vocabulary and a test in easysnowdata holds the line.
- **Archive storage.** See [`STORAGE.md`](STORAGE.md): the daily `tar.xz` is a
  27 MB blob committed daily that duplicates the CSV tree, and `.git` is now
  3.7 GB against a ~510 MB working tree. A chunked store in the Pages artefact
  would be smaller and allow partial reads.
