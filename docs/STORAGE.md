# Archive storage: how it works today, and a proposal to change it

Status: **step 1 of 4 implemented 2026-09-22** — the Zarr stores are built
and published in the Pages artefact (§3 below has the sequence and what is
still open). Written 2026-09-17, while folding `clients/` into
[easysnowdata](https://github.com/egagli/easysnowdata).

This describes how the daily archive is built and published today, what that
costs, and a proposal to publish it as a chunked store instead. Nothing here
changes the client layer or the inventory schema — only how the *observations*
are stored and served.

---

## 1. How it works today

### 1.1 The per-station CSVs

`scripts/get_all_stations_data.py` runs daily in CI:

1. reads the station list from `all_snow_stations.geojson`;
2. routes each station to its client by the `client` property;
3. pulls fresh daily records in batches, one client at a time;
4. flattens the records to one row per date — `date,wteq_cm,snwd_cm`, missing
   values empty, never the string `nan` (DESIGN.md §6.3) — and **atomically
   replaces** `data/stations/<code>.csv` (temp file, then rename), so a failed
   fetch leaves the previous file intact;
5. writes the earliest/latest/verified dates back onto the GeoJSON.

Each CSV is the station's **whole record**, rewritten in full every run, not
appended to. That matters: it is why these files never go stale the way the
old `snotel_ccss_stations` archive did, where the updater only ever rewrote
the last ten days and so kept pre-bias-correction SNOTEL temperatures for
2004-2024 forever.

There are ~1,550 such files, one per probe-verified daily-or-better station.

### 1.2 The bundle

`build_archive()` then walks `data/stations/*.csv` and writes them all into
`data/all_station_csvs.tar.xz` — `tarfile.open(mode="w:xz")`, members named
`stations/<code>.csv`, again written to a temp file and renamed over the old
one. About 27 MB.

So the bundle is a **full duplicate** of the CSV tree, rebuilt from scratch
each run, and both are committed to `main`.

### 1.3 What that costs

| | |
| --- | --- |
| `.git` | **3.7 GB**, against a ~510 MB working tree |
| `data/` in the working tree | 399 MB |
| Rebuild cadence | daily, via CI commit |
| Bundle | 27.4 MB, incompressible by git (already xz), rewritten whole each run |

The tarball is the main driver: a new 27 MB binary blob per refresh, kept
forever, on top of the ~1,550 CSVs it duplicates.

### 1.4 How consumers read it

`easysnowdata.stations.archive` fetches `all_snow_stations.geojson` with
geopandas and `data/all_station_csvs.tar.xz` with `pooch` (cached), then
extracts the wanted members. Reading **one water year for all stations costs
the full 27.4 MB download**, because tar has no usable random access and xz is
a single solid stream. The whole archive materializes as 1,556 × 47,406, 21.8%
finite.

---

## 2. Proposal: publish a chunked store

Write the same observations as a chunked, compressed array — Zarr — alongside
(not instead of) the CSVs, and point library consumers at it.

Measured on the real archive:

| Layout | Total size | One water year, all stations |
| --- | --- | --- |
| `all_station_csvs.tar.xz` (today) | 27.4 MB | 27.4 MB — the whole file |
| Zarr v3, float32, `station=all, time=366` | **17.0 MB** | **~340 KB** |
| Zarr v3, float32, `station=64, time=all` | 16.7 MB | ~554 KB per station, whole record |

Smaller *and* roughly 80× less traffic for the commonest query. It compresses
well because the grid is only 21.8% finite.

### 2.1 Where to put it

GitHub Pages, in the artefact this repo already deploys. Two properties make
this nearly free:

- the Pages workflow uses `actions/upload-pages-artifact` + `actions/deploy-pages`,
  which publishes a **build artefact and commits nothing to a branch** — so the
  store costs zero git history, which is the whole problem with the tarball;
- Pages serves HTTP range requests (verified: `HTTP 206`, `accept-ranges: bytes`),
  which is what makes per-chunk reads work at all.

The daily workflow already calls the Pages deploy, so this is one more step in
a job that runs anyway. Pages allows 1 GB per site; 17 MB is not a concern.

### 2.2 Keep the CSVs

They should stay. "Open one station in a spreadsheet" is a real use, they are
the human-readable form of the archive, and they are what makes the data
legible without a Python stack. The proposal is to stop committing **the
tarball** — the store replaces it as the bulk-transfer route — which alone
stops `.git` growing.

### 2.3 Citable snapshots: Zenodo

Zenodo suits a periodic snapshot, not the daily refresh, for two documented
reasons:

- **100 files maximum per record** (50 GB, 200 GB on request). A chunked store
  is ~160 files, so it would have to be **zipped** — and zipping gives up the
  partial reads that motivated the change, since the whole zip must come down.
- Published files are effectively **immutable**: minor corrections within 30
  days, otherwise a new version with a new DOI. Daily versioning is explicitly
  not what it is for.

So: Pages for the rolling store, Zenodo a few times a year for a zipped,
DOI'd, citable snapshot. The concept DOI always resolves to the newest version.
That also gives the archive something to cite, which it currently lacks.

### 2.4 Icechunk as the alternative

[Icechunk](https://icechunk.io/) is worth considering instead of plain Zarr,
because this archive has exactly the shape it is built for: **a store rewritten
on a schedule, whose history matters.**

What it would add:

- **Transactions.** Today a failed run can leave the tarball and the CSVs
  disagreeing; the atomic-rename trick protects each file but not the set.
  Icechunk commits all-or-nothing, so the published archive is never half
  refreshed.
- **Version history without git.** Every daily refresh is a commit you can
  check out. "What did the archive say on 2026-03-01" becomes answerable —
  which is precisely the question the frozen `snotel_ccss_stations` archive
  could not answer, and which would have made the SNOTEL bias-correction
  discrepancy obvious years earlier.
- **Cheap appends.** Only changed chunks are written per commit, rather than
  a 27 MB blob.

What it would cost:

- It wants **object storage** (S3/GCS/Azure) rather than static hosting. Pages
  is not an Icechunk target, so this route means a bucket and its billing,
  where plain Zarr on Pages is free.
- One more dependency in the read path, and a newer one than Zarr.

A reasonable staging: plain Zarr on Pages first, since it is free, solves the
size and partial-read problems immediately, and requires no new infrastructure.
Move to Icechunk if and when the version history is worth a bucket — and note
that a Zarr store is not wasted work, since Icechunk stores Zarr.

---

## 3. What has to change, in order

1. ✅ **Publish the store on Pages (done 2026-09-22).** `scripts/build_zarr_archive.py`
   reads the committed inventory and CSVs and writes both layouts of §4
   (`by_time.zarr`, `by_station.zarr`) plus an `archive.json` manifest into
   `_site/archive/`; `deploy-pages.yml` runs it before staging the artefact.
   It is a separate script rather than a step in `get_all_stations_data.py`
   because the Pages build checks out `main` after the refresh has committed,
   so the CSVs are already on disk and nothing needs to be handed between
   jobs. Nothing is committed. Measured on the 2026-09-21 archive against a
   static server: one water year for all stations 0.7 MB, one station's whole
   record 0.5 MB, store open 0.14 MB, each store ~17 MB on disk. Format 3
   with consolidated metadata, accepted with its "not in the v3 spec" warning
   because a static host cannot list a directory.
2. ⬜ **`easysnowdata.stations.archive`: add the Pages store as a source**, and
   make it the default for `load()`, keeping `github-tarball` and
   `github-csv` so old pins keep working. Until this is released, the
   tarball must keep being committed: current easysnowdata pins fetch it
   from the `main` branch.
3. ⬜ **Stop committing `data/all_station_csvs.tar.xz`** once step 2 is on
   PyPI and conda-forge — build it into the Pages artefact instead if a
   single-file download is still wanted. A history rewrite to reclaim the
   blobs is disruptive and a separate, deliberate decision.
4. ✅ **Snapshots on a tag (workflow added 2026-09-22; Zenodo side `[needs Eric]`).**
   `release-snapshot.yml` runs on a `v*` tag: it builds the stores, zips them
   and attaches them, the inventory and the tarball to a GitHub Release. With
   the repository's Zenodo–GitHub integration switched on, Zenodo archives the
   tagged source tree — inventory and every CSV — and mints a version DOI
   under the concept DOI. That is the citable object; the Zarr zips are a
   convenience rebuildable from it. The integration has to be enabled once at
   zenodo.org for this repository, and `CITATION.cff` should then carry the
   concept DOI. The daily refresh is deliberately not a release.

Also decided along the way, from the best-practices notes: Icechunk cannot be
served from Pages (its backends are local disk, S3-compatible, GCS and Azure;
HTTP is only a virtual-chunk target), so §2.4 stays a bucket decision; and
nothing today mints a DOI per Icechunk tag, so Zenodo stays the citation
route whichever store serves the reads.

## 4. Open questions

- ~~Chunking depends on the dominant query.~~ **Both layouts are published**
  (`by_time`: `station=all, time=366`; `by_station`: `station=64, time=all`),
  each ~17 MB, so the reader picks by query instead of the writer guessing.
- Whether to carry more than `wteq_cm` and `snwd_cm`. The clients serve
  temperature, precipitation, wind and humidity, and a chunked store makes
  extra variables much cheaper to carry than extra CSV columns would.
- Whether the inventory should become GeoParquet at the same time. It is read
  in full every time today, and bbox-pushdown would make AOI queries cheaper.
