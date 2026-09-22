"""
Build the chunked (Zarr) form of the daily archive from the committed CSVs.

Reads ``all_snow_stations.geojson`` and ``data/stations/*.csv`` — the two
artefacts the daily pipeline commits — and writes the same observations as
Zarr stores under an output directory, for the GitHub Pages artefact.
Nothing here touches the clients, the inventory schema or the CSVs
(docs/STORAGE.md §2): this is a second *published form* of the archive,
not a second source of truth.

Two stores are written, differing only in chunk layout, because the two
dominant queries want opposite layouts and both stores are small
(docs/STORAGE.md §4):

- ``by_time.zarr``      — chunks ``(station=all, time=366)``: "one water
  year, every station" costs one or two chunks per variable.
- ``by_station.zarr``   — chunks ``(station=64, time=all)``: "this
  station's whole record" costs one chunk per variable.

Both hold ``swe`` and ``snow_depth`` in **centimetres** as float32
(DESIGN.md §3.5), on a complete daily ``time`` axis from the earliest to
the latest observation, with the inventory's station metadata as
coordinates along ``station``.  Missing observations are NaN, and chunks
that are entirely missing are not written.

Zarr format 3 with consolidated metadata.  The store is served by a static
host that cannot list a directory, so a reader has to learn the hierarchy
from one document — that is what consolidated metadata is for, and why
zarr-python's "not part of the v3 spec" warning is accepted here rather
than avoided.  ``archive.json`` beside the stores records what was built
from what.

Usage (from the repo root)::

    python -m scripts.build_zarr_archive --output _site/archive
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import xarray as xr

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = REPO_ROOT / "all_snow_stations.geojson"
STATIONS_DIR = REPO_ROOT / "data" / "stations"

# Metadata carried as coordinates along `station`, and their dtypes.
# All come straight from the inventory feature properties.
STATION_COORDS: dict[str, str] = {
    "name": "str",
    "network_code": "str",
    "client": "str",
    "operator": "str",
    "state": "str",
    "latitude": "float64",
    "longitude": "float64",
    "elevation_m": "float32",
    "daily_provenance": "str",
}

VARIABLES = {
    # CSV column -> (array name, attrs)
    "wteq_cm": (
        "swe",
        {
            "long_name": "snow water equivalent",
            "units": "cm",
            "standard_name": "lwe_thickness_of_surface_snow_amount",
            "source_column": "wteq_cm",
        },
    ),
    "snwd_cm": (
        "snow_depth",
        {
            "long_name": "snow depth",
            "units": "cm",
            "standard_name": "surface_snow_thickness",
            "source_column": "snwd_cm",
        },
    ),
}

LAYOUTS: dict[str, dict[str, int]] = {
    # name -> chunk sizes; -1 means "the whole axis"
    "by_time": {"station": -1, "time": 366},
    "by_station": {"station": 64, "time": -1},
}

logger = logging.getLogger("build_zarr_archive")


# ── Inputs ────────────────────────────────────────────────────────────────────

def load_inventory(path: Path = INVENTORY) -> pd.DataFrame:
    """Inventory properties as a frame indexed by station ``code``."""
    with path.open(encoding="utf-8") as f:
        fc = json.load(f)
    rows = [feat["properties"] for feat in fc["features"]]
    df = pd.DataFrame(rows).set_index("code")
    df.index = df.index.astype(str)
    return df


def read_station_csv(path: Path) -> pd.DataFrame:
    """One station CSV as a frame indexed by date, one row per date.

    Dates that do not parse are dropped; a repeated date keeps its last
    row, which is what the CSV writer would have produced had the source
    not repeated it.
    """
    df = pd.read_csv(
        path,
        usecols=["date", "wteq_cm", "snwd_cm"],
        dtype={"wteq_cm": "float32", "snwd_cm": "float32"},
    )
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
    df = df.dropna(subset=["date"])
    df = df[~df["date"].duplicated(keep="last")]
    return df.set_index("date").sort_index()


def collect_frames(
    inventory: pd.DataFrame,
    stations_dir: Path = STATIONS_DIR,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Read every station CSV that the inventory knows about.

    Returns the frames keyed by code, in inventory order, plus the codes
    of CSVs on disk that the inventory does not list (they are skipped:
    DESIGN.md §6.4 keeps such files, but a store row with no metadata is
    not useful).
    """
    frames: dict[str, pd.DataFrame] = {}
    orphans: list[str] = []
    for path in sorted(stations_dir.glob("*.csv")):
        code = path.stem
        if code not in inventory.index:
            orphans.append(code)
            continue
        df = read_station_csv(path)
        if df.empty or df[["wteq_cm", "snwd_cm"]].isna().all().all():
            continue
        frames[code] = df
    return frames, orphans


# ── Dataset ───────────────────────────────────────────────────────────────────

def build_dataset(
    frames: dict[str, pd.DataFrame],
    inventory: pd.DataFrame,
) -> xr.Dataset:
    """Dense ``(station, time)`` dataset from the per-station frames."""
    if not frames:
        raise ValueError("no station frames to build a dataset from")

    tmin = min(df.index.min() for df in frames.values())
    tmax = max(df.index.max() for df in frames.values())
    time = pd.date_range(tmin, tmax, freq="D")
    codes = np.array(list(frames), dtype=str)

    arrays = {
        col: np.full((len(codes), len(time)), np.nan, dtype="float32")
        for col in VARIABLES
    }
    for i, (code, df) in enumerate(frames.items()):
        pos = (df.index - tmin).days.to_numpy()
        for col in VARIABLES:
            arrays[col][i, pos] = df[col].to_numpy(dtype="float32")

    meta = inventory.reindex(codes)
    coords: dict[str, tuple[str, np.ndarray]] = {
        "station": ("station", codes),
    }
    for field, dtype in STATION_COORDS.items():
        if field not in meta.columns:
            continue
        series = meta[field]
        if dtype == "str":
            values = series.fillna("").astype(str).to_numpy()
        else:
            values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=dtype)
        coords[field] = ("station", values)

    data_vars = {
        name: (("station", "time"), arrays[col], attrs)
        for col, (name, attrs) in VARIABLES.items()
    }
    ds = xr.Dataset(data_vars, coords={**coords, "time": time})
    ds["time"].attrs["long_name"] = "observation date"
    ds["station"].attrs["long_name"] = "station code (inventory `code`)"
    return ds


def _git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def dataset_attrs(ds: xr.Dataset, layout: str, chunks: dict[str, int]) -> dict:
    finite = int(np.isfinite(ds["swe"].values).sum()
                 + np.isfinite(ds["snow_depth"].values).sum())
    total = 2 * ds["swe"].size
    return {
        "title": "global_snow_networks daily archive",
        "summary": (
            "Daily snow water equivalent and snow depth for every probe-verified "
            "daily-or-better station in all_snow_stations.geojson, built from "
            "the per-station CSVs in data/stations/. Same observations as the "
            "CSVs; this is the chunked form for partial reads over HTTP."
        ),
        "source": "https://github.com/egagli/global_snow_networks",
        "license": "See README.md §11 for the upstream data licences",
        "units": "cm",
        "layout": layout,
        "chunks": json.dumps(chunks),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_from_commit": _git_head() or "",
        "n_stations": int(ds.sizes["station"]),
        "n_days": int(ds.sizes["time"]),
        "time_min": str(ds["time"].values[0])[:10],
        "time_max": str(ds["time"].values[-1])[:10],
        "fraction_finite": round(finite / total, 4) if total else 0.0,
    }


# ── Output ────────────────────────────────────────────────────────────────────

def resolve_chunks(ds: xr.Dataset, spec: dict[str, int]) -> dict[str, int]:
    """Chunk sizes for *ds*: -1 means the whole axis, and nothing exceeds it."""
    return {
        dim: (ds.sizes[dim] if n == -1 else min(n, ds.sizes[dim]))
        for dim, n in spec.items()
    }


def write_store(ds: xr.Dataset, path: Path, layout: str) -> dict[str, int]:
    chunks = resolve_chunks(ds, LAYOUTS[layout])
    out = ds.copy()
    out.attrs = dataset_attrs(ds, layout, chunks)
    encoding = {
        name: {"chunks": (chunks["station"], chunks["time"])}
        for name in out.data_vars
    }
    # Consolidated metadata on a v3 store is deliberate (module docstring);
    # zarr-python warns that it is not in the v3 spec.  Everything else is
    # a real warning and stays visible.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=".*[Cc]onsolidated metadata.*", category=UserWarning
        )
        out.to_zarr(
            path, mode="w", zarr_format=3, consolidated=True,
            encoding=encoding, write_empty_chunks=False,
        )
    return chunks


def store_size_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def build_archive(
    output: Path,
    layouts: list[str],
    inventory_path: Path | None = None,
    stations_dir: Path | None = None,
) -> dict:
    inventory_path = inventory_path or INVENTORY
    stations_dir = stations_dir or STATIONS_DIR
    inventory = load_inventory(inventory_path)
    frames, orphans = collect_frames(inventory, stations_dir)
    logger.info("%d station CSVs read, %d orphans skipped", len(frames), len(orphans))
    ds = build_dataset(frames, inventory)
    logger.info(
        "dataset: %d stations × %d days (%s → %s)",
        ds.sizes["station"], ds.sizes["time"],
        str(ds["time"].values[0])[:10], str(ds["time"].values[-1])[:10],
    )

    output.mkdir(parents=True, exist_ok=True)
    manifest: dict = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "built_from_commit": _git_head(),
        "inventory": inventory_path.name,
        "n_stations": int(ds.sizes["station"]),
        "n_days": int(ds.sizes["time"]),
        "time_min": str(ds["time"].values[0])[:10],
        "time_max": str(ds["time"].values[-1])[:10],
        "variables": {name: attrs["units"] for _, (name, attrs) in VARIABLES.items()},
        "orphan_csvs_skipped": orphans,
        "stores": {},
    }
    for layout in layouts:
        path = output / f"{layout}.zarr"
        chunks = write_store(ds, path, layout)
        size = store_size_bytes(path)
        logger.info("%s: chunks %s, %.1f MB", path.name, chunks, size / 1e6)
        manifest["stores"][path.name] = {
            "layout": layout,
            "chunks": chunks,
            "zarr_format": 3,
            "consolidated": True,
            "size_bytes": size,
        }
    (output / "archive.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--output", type=Path, default=REPO_ROOT / "_site" / "archive",
        help="directory to write the stores and archive.json into",
    )
    parser.add_argument(
        "--layout", action="append", choices=sorted(LAYOUTS),
        help="write only this layout (repeatable; default: all)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    manifest = build_archive(args.output, args.layout or sorted(LAYOUTS))
    print(json.dumps(manifest["stores"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
