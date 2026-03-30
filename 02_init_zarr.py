# -*- coding: utf-8 -*-
"""
02_init_zarr.py
===============
Create the sparse Zarr v3 store for daily snow observations.

Chunking strategy
-----------------
  station dim : chunk size = 1  (one station per chunk file in that dimension)
  time dim    : chunk size = 366 (≈ one water year; max WY length)

The store is sparse by design: data chunks are written only for
(station, water-year) pairs that actually have observations.  Stations
that were not active in a given water year produce no chunk and consume
no disk space.

The primary station dimension index remains the string triplet.

WY / DOWY coordinates
---------------------
``water_year`` (int32) and ``dowy`` (int16) are attached as non-index
coordinates on the ``time`` dimension via ``utils.add_wy_coords``.

Usage
-----
  python 02_init_zarr.py     # run ONCE; overwrites any existing store
  # or:
  pixi run init-zarr
"""

import datetime
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr
import zarr
from zarr.codecs import BloscCodec

from utils import add_wy_coords

# ── Config ────────────────────────────────────────────────────────────────────
GEOJSON_PATH  = Path("snow_stations.geojson")
ZARR_OUT      = Path("snow_stations.zarr")
MANIFEST      = Path("stations_manifest.json")
CHECKPOINT    = Path("fetch_checkpoint.json")

# One station per chunk on the station axis.
# ~366 days per chunk on the time axis (≈ 1 water year).
CHUNK_STATION = 1
CHUNK_TIME    = 366


def main():
    # ── Parse GeoJSON ─────────────────────────────────────────────────────────
    print("Loading GeoJSON…")
    with open(GEOJSON_PATH) as f:
        gj = json.load(f)

    stations = []
    for feat in gj["features"]:
        p    = feat["properties"]
        dels = [e for e in p.get("snowElements", []) if e.get("durationName") == "DAILY"]
        if not dels:
            continue

        el_meta = {}
        for e in dels:
            code = e["elementCode"]
            el_meta[code] = {
                "begin": e.get("beginDate", "") or None,
                "end":   e.get("endDate",   "") or None,
            }

        stations.append({
            "triplet":      p["stationTriplet"],
            "network":      p.get("networkCode", ""),
            "name":         p.get("name", ""),
            "state":        p.get("state", ""),
            "latitude":     float(p["latitude"]),
            "longitude":    float(p["longitude"]),
            "elevation_m": float(p["elevation_m"])
                            if p.get("elevation_m") is not None else float("nan"),
            "huc":          p.get("huc", ""),
            "el_meta":      el_meta,
        })

    print(f"  Daily stations: {len(stations):,}")

    # ── Global time axis ──────────────────────────────────────────────────────
    # Start on Oct 1 of the earliest WY so chunk boundaries roughly
    # align with water year transitions.
    today = datetime.date.today()
    min_date = datetime.date(2100, 1, 1)
    for s in stations:
        for meta in s["el_meta"].values():
            bd = meta["begin"]
            if bd:
                try:
                    d = datetime.date.fromisoformat(bd[:10])
                    if d < min_date:
                        min_date = d
                except ValueError:
                    pass

    # Snap to Oct 1 of the WY that contains min_date
    first_wy_start_year = (min_date.year - 1) if min_date.month < 10 else min_date.year
    global_start = datetime.date(first_wy_start_year, 10, 1)
    time_index   = pd.date_range(global_start, today, freq="D")
    n_time       = len(time_index)
    n_stations   = len(stations)

    print(f"  Time axis  : {global_start} → {today}  ({n_time:,} days)")
    print(f"  Dense size : {n_stations * n_time * 2 * 4 / 1e6:.0f} MB uncompressed  "
          f"(but store is sparse)")

    # ── Build xarray Dataset (metadata only — no data arrays loaded) ──────────
    print("\nBuilding Dataset structure…")

    station_triplets = np.array([s["triplet"]      for s in stations], dtype=object)
    # NaN fill array — we write this once to define the structure, then
    # individual chunks will be overwritten (or left as implicit NaN) by
    # the fetch script.
    nan_arr = np.full((n_stations, n_time), np.nan, dtype=np.float32)

    ds = xr.Dataset(
        data_vars={
            "WTEQ": xr.Variable(
                dims=("station", "time"),
                data=nan_arr.copy(),
                attrs={
                    "long_name":  "Snow Water Equivalent",
                    "units":      "cm",
                    "source":     "USDA NRCS AWDB",
                    "_FillValue": "NaN",
                },
            ),
            "SNWD": xr.Variable(
                dims=("station", "time"),
                data=nan_arr.copy(),
                attrs={
                    "long_name":  "Snow Depth",
                    "units":      "cm",
                    "source":     "USDA NRCS AWDB",
                    "_FillValue": "NaN",
                },
            ),
        },
        coords={
            # ── Index dimensions ──────────────────────────────────────────────
            "station":  ("station", station_triplets),
            "time":     ("time",    time_index),

            # ── Per-station coordinates ───────────────────────────────────────
            "name": (
                "station",
                np.array([s["name"]    for s in stations], dtype=object),
            ),
            "network": (
                "station",
                np.array([s["network"] for s in stations], dtype=object),
            ),
            "state": (
                "station",
                np.array([s["state"]   for s in stations], dtype=object),
            ),
            "latitude": (
                "station",
                np.array([s["latitude"]    for s in stations], dtype=np.float64),
                {"units": "degrees_north"},
            ),
            "longitude": (
                "station",
                np.array([s["longitude"]   for s in stations], dtype=np.float64),
                {"units": "degrees_east"},
            ),
            "elevation_m": (
                "station",
                np.array([s["elevation_m"] for s in stations], dtype=np.float32),
                {"units": "meters"},
            ),
            "huc": (
                "station",
                np.array([s["huc"] for s in stations], dtype=object),
            ),
            "wteq_begin": (
                "station",
                np.array(
                    [s["el_meta"].get("WTEQ", {}).get("begin") or ""
                     for s in stations],
                    dtype=object,
                ),
            ),
            "wteq_end": (
                "station",
                np.array(
                    [s["el_meta"].get("WTEQ", {}).get("end") or ""
                     for s in stations],
                    dtype=object,
                ),
            ),
            "snwd_begin": (
                "station",
                np.array(
                    [s["el_meta"].get("SNWD", {}).get("begin") or ""
                     for s in stations],
                    dtype=object,
                ),
            ),
            "snwd_end": (
                "station",
                np.array(
                    [s["el_meta"].get("SNWD", {}).get("end") or ""
                     for s in stations],
                    dtype=object,
                ),
            ),
        },
        attrs={
            "title":        "USDA NRCS AWDB Daily Snow Observations",
            "source":       "AWDB REST API v1 — https://wcc.sc.egov.usda.gov/awdbRestApi",
            "elements":     "WTEQ (Snow Water Equivalent, cm), SNWD (Snow Depth, cm)",
            "networks":     "SNTL, SNTLT, MSNT, COOP, SCAN",
            "created":      datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "global_start": str(global_start),
            "conventions":  "CF-1.8",
        },
    )

    # Add WY and DOWY coordinates on the time dimension
    ds = add_wy_coords(ds)

    # ── Write Zarr store ──────────────────────────────────────────────────────
    print("\nWriting Zarr store…")
    if ZARR_OUT.exists():
        print(f"  Removing existing store at {ZARR_OUT}")
        shutil.rmtree(ZARR_OUT)

    _blosc = BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")
    encoding = {
        "WTEQ": {
            "chunks":      (CHUNK_STATION, CHUNK_TIME),
            "compressors": _blosc,
            "dtype":       "float32",
        },
        "SNWD": {
            "chunks":      (CHUNK_STATION, CHUNK_TIME),
            "compressors": _blosc,
            "dtype":       "float32",
        },
        "time": {
            "chunks": (CHUNK_TIME,),
            "dtype":  "int64",
        },
    }

    ds.to_zarr(str(ZARR_OUT), encoding=encoding, mode="w")
    print(f"  Store written → {ZARR_OUT}")

    # ── Save manifest + checkpoint ─────────────────────────────────────────────
    manifest = {
        "stations":     stations,
        "global_start": str(global_start),
        "today":        str(today),
        "n_time":       n_time,
        "n_stations":   n_stations,
    }
    with open(MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)

    with open(CHECKPOINT, "w") as f:
        json.dump({"completed_batches": []}, f)

    n_batches = (n_stations + 4) // 5
    print(f"  Manifest   → {MANIFEST}")
    print(f"  Checkpoint → {CHECKPOINT}")
    print(f"\n✓  Init complete. Run 03_fetch_zarr.py (~{n_batches} batches total).")


if __name__ == "__main__":
    main()
