# -*- coding: utf-8 -*-
"""
04_inspect_zarr.py
==================
Validate and summarise the completed Zarr store.

Usage
-----
  python 04_inspect_zarr.py
  pixi run inspect
"""

from pathlib import Path
import numpy as np
import pandas as pd
import xarray as xr

ZARR_OUT = Path("snow_stations.zarr")


def main():
    print(f"Opening {ZARR_OUT}…\n")
    ds = xr.open_zarr(str(ZARR_OUT))

    print("Dataset")
    print("───────")
    print(ds)

    # ── Coordinates ──────────────────────────────────────────────────────────
    print("\nSample station coordinates:")
    s = ds.isel(station=0)
    for coord in ["station", "name", "network", "state", "elevation_m",
                  "latitude", "longitude", "huc", "wteq_begin", "wteq_end"]:
        if coord in ds.coords:
            print(f"  {coord:15s} = {s[coord].values}")

    # ── WY / DOWY coords ──────────────────────────────────────────────────────
    print("\nTime coordinate sample (first 5 days):")
    for i in range(5):
        t    = pd.Timestamp(ds["time"].values[i])
        wy   = int(ds["water_year"].values[i])
        dowy = int(ds["dowy"].values[i])
        print(f"  {t.date()}  WY={wy}  DOWY={dowy}")

    # ── Coverage ─────────────────────────────────────────────────────────────
    print("\nData coverage")
    print("─────────────")
    n_total = ds.sizes["station"] * ds.sizes["time"]
    for var in ("WTEQ", "SNWD"):
        n_valid = int(ds[var].count())
        pct     = 100 * n_valid / n_total
        print(f"  {var}  non-null : {n_valid:>12,}  /  {n_total:,}  ({pct:.1f}%)")

    # ── Network breakdown ─────────────────────────────────────────────────────
    print("\nStation count by network:")
    networks, counts = np.unique(ds["network"].values, return_counts=True)
    for net, cnt in sorted(zip(networks, counts), key=lambda x: -x[1]):
        print(f"  {net:8s}: {cnt:,}")

    # ── Disk size ─────────────────────────────────────────────────────────────
    zarr_bytes  = sum(f.stat().st_size for f in ZARR_OUT.rglob("*") if f.is_file())
    dense_bytes = n_total * 2 * 4
    ratio       = dense_bytes / max(zarr_bytes, 1)
    n_chunks    = sum(1 for f in ZARR_OUT.rglob("*") if f.is_file() and not f.name.startswith("."))
    print(f"\nStorage")
    print(f"───────")
    print(f"  Store path        : {ZARR_OUT.resolve()}")
    print(f"  Chunk files       : {n_chunks:,}")
    print(f"  Size on disk      : {zarr_bytes/1e6:.1f} MB")
    print(f"  Dense equivalent  : {dense_bytes/1e6:.0f} MB")
    print(f"  Compression ratio : {ratio:.1f}×")

    # ── Simple geographic subset demo (Colorado bounding box) ────────────────
    print("\nGeographic subset example")
    print("─────────────────────────")
    lon_min, lon_max = -109.05, -102.05
    lat_min, lat_max = 37.0, 41.0
    ds_co = ds.where(
        (ds["longitude"] >= lon_min) & (ds["longitude"] <= lon_max)
        & (ds["latitude"] >= lat_min) & (ds["latitude"] <= lat_max),
        drop=True,
    )
    print(f"  Stations in Colorado bbox: {ds_co.sizes['station']}")

    # ── Sample station time series ────────────────────────────────────────────
    sample = "303_CO_SNTL"
    if sample in ds.station.values:
        s   = ds.sel(station=sample)
        wy  = ds["water_year"] == 2024
        swe = s["WTEQ"].sel(time=wy).dropna("time")
        print(f"\nSample: {sample} ({s['name'].values})")
        print(f"  WY2024 WTEQ observations: {len(swe):,}")
        if len(swe):
            print(f"  Peak SWE (WY2024): {float(swe.max()):.1f} cm "
                  f"on {pd.Timestamp(swe.idxmax('time').values).date()}")

    print("\n✓  Validation complete.")


if __name__ == "__main__":
    main()
