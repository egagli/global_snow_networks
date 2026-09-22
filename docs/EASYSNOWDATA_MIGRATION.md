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

## Done as of 2026-09-18

Nothing is outstanding. easysnowdata 0.2.0 is released on PyPI and
conda-forge, and `pixi.toml` depends on it the ordinary way:

```toml
[dependencies]
easysnowdata = ">=0.2"
```

Two things about that last step, because both cost time and neither is
obvious:

- **`pixi lock` wrote a lock with `easysnowdata` simply absent, and exited 0.**
  Twice — first while conda-forge's channel index was still serving 0.0.24
  (`api.anaconda.org` reports a new version as soon as the artifact uploads,
  tens of minutes before `repodata.json` serves it), and again from stale
  cached repodata. Only `pixi install` complained. Check the lock's contents
  rather than the exit code.
- **Incrementally updating the old lock produced a broken environment**:
  `libgdal.so.39: undefined symbol: sqlite3_error_offset`, because the solver
  kept old pins and landed on an incompatible libgdal/libsqlite pair. The same
  dependency set solved cleanly in a fresh project, so the fix was to delete
  `pixi.lock` and re-solve from scratch. That is why the lock changed
  wholesale in the same commit rather than gaining one package.

## Open questions, not blocking

- ~~**The published inventory's `network` property means something else.**~~
  **Resolved 2026-09-22: renamed to `network_name`.** The "breaking change"
  worry turned out to be unfounded: the live map reads `network_code`, not
  `network`; no script or test in this repo read it; and its only known
  consumer, easysnowdata's archive reader, guards its rename with an
  existence check and then sets `network` from `client`, so it works
  unchanged before and after. The property was a Yukon-only display name on
  109 features (`network_code` YSS/YKEC already identify the programs). The
  inventory contract test now asserts no feature carries a bare `network`.
- **Yukon `precip_snow_cm`** is typed `snowfall` now rather than `precip`, so
  nothing rescales 5 cm of snow into 50 mm of water. `snowfall` is in the
  shared vocabulary and a test in easysnowdata holds the line.
- **Archive storage.** See [`STORAGE.md`](STORAGE.md): the daily `tar.xz` is a
  27 MB blob committed daily that duplicates the CSV tree, and `.git` is now
  3.7 GB against a ~510 MB working tree. A chunked store in the Pages artefact
  would be smaller and allow partial reads.
