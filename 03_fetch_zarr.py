# -*- coding: utf-8 -*-
"""
03_fetch_zarr.py
================
Resumable AWDB data fetcher.  For each pending batch of stations, fetches
all historical daily WTEQ and SNWD data, then writes one water year at a
time directly into the Zarr store.

Sparse writing
--------------
Only water years that contain at least one non-NaN value are written.
This means the Zarr store is sparse: unoccupied (station, WY) chunk files
simply do not exist on disk.  With chunk shape ``(1 station × 366 days)``
this corresponds exactly to one chunk file per active station-water-year.

Resumability
------------
The checkpoint file (``fetch_checkpoint.json``) records completed batch
start-indices.  Re-running this script after an interruption skips all
completed batches and picks up from where it left off.

API value limit
---------------
The AWDB /data endpoint rejects requests where
  n_stations × n_elements × n_days > 500,000.
With ~47,000 days and 2 elements, the limit is ~5 stations per batch.
The AWDBClient computes this automatically, but we set BATCH_SIZE=5 here
to control checkpoint granularity independently.

Usage
-----
  python 03_fetch_zarr.py          # process up to MAX_BATCHES_PER_RUN batches
  pixi run fetch-data

  # Loop to completion (Unix/macOS):
  until python 03_fetch_zarr.py | grep -q "ALL BATCHES COMPLETE"; do
    sleep 5
  done
"""

import datetime
import json
import argparse
from pathlib import Path
import time

import numpy as np
import pandas as pd
import zarr

from clients import AWDBClient
from clients.awdb_client import AWDBError

# ── Config ────────────────────────────────────────────────────────────────────
ZARR_OUT   = Path("snow_stations.zarr")
MANIFEST   = Path("stations_manifest.json")
CHECKPOINT = Path("fetch_checkpoint.json")

BATCH_SIZE          = 5     # stations per fetch call (see API value limit note)
MAX_BATCHES_PER_RUN = 6     # batches to process before exiting (~4 min/run)


def code_to_awdb_triplet(code: str) -> str:
    return str(code).replace("_", ":")


def awdb_triplet_to_code(triplet: str) -> str:
    return str(triplet).replace(":", "_")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Fetch AWDB data into the sparse Zarr store. "
            "Use --until-complete to keep running passes until all batches finish."
        )
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=MAX_BATCHES_PER_RUN,
        help=(
            "Maximum batches to process per pass. "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--until-complete",
        action="store_true",
        help="Run repeated passes until all batches are complete.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=5,
        help="Sleep duration between passes when using --until-complete.",
    )
    args = parser.parse_args()

    pass_num = 0
    while True:
        pass_num += 1
        if args.until_complete:
            print(f"\n{'=' * 24} PASS {pass_num} {'=' * 24}")

        all_done = _run_pass(max_batches_per_run=args.max_batches)
        if all_done or not args.until_complete:
            break

        if args.sleep_seconds > 0:
            print(f"\nWaiting {args.sleep_seconds}s before next pass…")
            time.sleep(args.sleep_seconds)


def _run_pass(max_batches_per_run: int) -> bool:
    # ── Load state ────────────────────────────────────────────────────────────
    with open(MANIFEST)   as f: manifest   = json.load(f)
    with open(CHECKPOINT) as f: checkpoint = json.load(f)

    stations     = manifest["stations"]
    global_start = manifest["global_start"]
    today_str    = manifest["today"]
    n_stations   = manifest["n_stations"]
    n_time       = manifest["n_time"]
    completed    = set(checkpoint["completed_batches"])

    # Time index — must exactly match 02_init_zarr.py's construction
    time_index  = pd.date_range(global_start, today_str, freq="D")
    assert len(time_index) == n_time

    # Fast lookup: date → position in the flat time axis
    date_to_idx = {d.date(): i for i, d in enumerate(time_index)}

    # Water-year → (start_idx, end_idx) slice map (inclusive on both ends)
    first_wy_start = time_index[0].date()
    last_date      = time_index[-1].date()
    wy_slices      = _build_wy_slices(first_wy_start, last_date, date_to_idx)

    all_batches = list(range(0, n_stations, BATCH_SIZE))
    pending     = [b for b in all_batches if b not in completed]

    print("=" * 60)
    print(f"  Total batches  : {len(all_batches)}")
    print(f"  Completed      : {len(completed)}")
    print(f"  Pending        : {len(pending)}")
    print(f"  This run       : up to {max_batches_per_run} batches")
    print("=" * 60)

    if not pending:
        print("\n✓  ALL BATCHES COMPLETE")
        print(f"   Zarr store ready at: {ZARR_OUT.resolve()}")
        return True

    # ── Open Zarr store for direct writes ─────────────────────────────────────
    store    = zarr.open(str(ZARR_OUT), mode="r+")
    wteq_arr = store["WTEQ"]   # shape (n_stations, n_time)
    snwd_arr = store["SNWD"]
    station_ids = list(store["station"][:])
    code_to_sidx = {str(code): i for i, code in enumerate(station_ids)}

    client = AWDBClient()

    run_wteq = run_snwd = run_chunks = 0
    processed = skipped = 0

    # ── Main fetch loop ───────────────────────────────────────────────────────
    import time as _time
    for batch_start in pending[:max_batches_per_run]:
        batch_codes = [
            s["triplet"]
            for s in stations[batch_start: batch_start + BATCH_SIZE]
        ]
        batch_triplets = [code_to_awdb_triplet(c) for c in batch_codes]
        batch_num = all_batches.index(batch_start) + 1
        end_idx   = batch_start + len(batch_triplets) - 1

        print(f"\nBatch {batch_num:3d}/{len(all_batches)}"
              f"  (stations {batch_start}–{end_idx})…", end=" ", flush=True)
        t0 = _time.time()

        try:
            api_results = client.get_data(
                triplets   = batch_triplets,
                elements   = ["WTEQ", "SNWD"],
                duration   = "DAILY",
                begin_date = global_start,
                end_date   = today_str,
            )
        except AWDBError as exc:
            print(f"ERROR ({exc}) — skipping")
            skipped += 1
            continue

        w, s, c = _write_sparse(
            api_results  = api_results,
            wteq_arr     = wteq_arr,
            snwd_arr     = snwd_arr,
            code_to_sidx = code_to_sidx,
            date_to_idx  = date_to_idx,
            wy_slices    = wy_slices,
        )
        elapsed = _time.time() - t0
        print(f"done  WTEQ={w:,}  SNWD={s:,}  chunks={c}  [{elapsed:.0f}s]")

        run_wteq   += w
        run_snwd   += s
        run_chunks += c
        processed  += 1

        # Checkpoint immediately after each successful batch
        completed.add(batch_start)
        with open(CHECKPOINT, "w") as f:
            json.dump({"completed_batches": sorted(completed)}, f)

    # ── Summary ───────────────────────────────────────────────────────────────
    remaining = len(pending) - processed - skipped
    print()
    print("=" * 60)
    print(f"  Batches processed  : {processed}")
    print(f"  Batches skipped    : {skipped}")
    print(f"  Batches remaining  : {remaining}")
    print(f"  WTEQ pts written   : {run_wteq:,}")
    print(f"  SNWD pts written   : {run_snwd:,}")
    print(f"  Chunks written     : {run_chunks:,}")
    print(f"  Completed total    : {len(completed)}/{len(all_batches)}")
    print("=" * 60)

    if remaining + skipped > 0:
        print(f"\n  ⟳  Re-run to continue.")
        return False
    else:
        print(f"\n  ✓  ALL BATCHES COMPLETE")
        print(f"     Zarr store ready at: {ZARR_OUT.resolve()}")
        return True


# ── Sparse write helper ───────────────────────────────────────────────────────

def _write_sparse(
    api_results,
    wteq_arr,
    snwd_arr,
    code_to_sidx,
    date_to_idx,
    wy_slices,
) -> tuple[int, int, int]:
    """
    Parse API results and write data into the Zarr store one water year at a
    time.  Only writes a chunk if it contains at least one non-NaN value.

    Returns (n_wteq_pts, n_snwd_pts, n_chunks_written).
    """
    wteq_pts = snwd_pts = chunks = 0

    for station_data in api_results:
        triplet = station_data.get("stationTriplet")
        code = awdb_triplet_to_code(triplet)
        sidx = code_to_sidx.get(code)
        if sidx is None:
            continue

        # Parse all values into per-element dicts of {date_idx: value}
        el_data: dict[str, dict[int, float]] = {}
        for el_block in station_data.get("data", []):
            el_code = el_block.get("stationElement", {}).get("elementCode")
            if el_code not in ("WTEQ", "SNWD"):
                continue

            values_map: dict[int, float] = {}
            for rec in el_block.get("values", []):
                raw_date = rec.get("date")
                val      = rec.get("value")
                if raw_date is None or val is None:
                    continue
                try:
                    d   = datetime.date.fromisoformat(str(raw_date)[:10])
                    idx = date_to_idx.get(d)
                    if idx is not None:
                        values_map[idx] = float(val)
                except (ValueError, TypeError):
                    pass

            if values_map:
                el_data[el_code] = values_map

        if not el_data:
            continue

        # Write one water year at a time (sparse chunk writes)
        for wy, (start_i, end_i) in wy_slices.items():
            wy_range = range(start_i, end_i + 1)

            for el_code, values_map in el_data.items():
                # Collect points that fall in this WY
                wy_dates  = [i for i in wy_range if i in values_map]
                if not wy_dates:
                    continue

                wy_vals = np.array([values_map[i] for i in wy_dates], dtype=np.float32)

                # Read current row slice, apply updates, write back
                target = wteq_arr if el_code == "WTEQ" else snwd_arr
                row    = target[sidx, start_i: end_i + 1]
                for local_i, val in zip(
                    [i - start_i for i in wy_dates], wy_vals
                ):
                    row[local_i] = val
                target[sidx, start_i: end_i + 1] = row

                if el_code == "WTEQ":
                    wteq_pts += len(wy_dates)
                else:
                    snwd_pts += len(wy_dates)
                chunks += 1   # approximately: one per el × WY × station

    return wteq_pts, snwd_pts, chunks


# ── Water-year slice builder ──────────────────────────────────────────────────

def _build_wy_slices(
    first_wy_start: datetime.date,
    last_date: datetime.date,
    date_to_idx: dict,
) -> dict[int, tuple[int, int]]:
    """
    Build a dict mapping ``water_year`` (int) → ``(start_idx, end_idx)``
    into the flat time array for every WY that overlaps the time axis.
    """
    import calendar

    # First WY that starts on or after global_start
    # WY for a given Oct 1 of year Y → WY = Y+1
    wy_start_year = first_wy_start.year  # Oct 1 of this year starts WY = year+1
    first_wy = wy_start_year + 1

    slices = {}
    current_date = first_wy_start
    wy = first_wy

    while current_date <= last_date:
        # WY runs current_date (Oct 1, wy-1) → Sep 30, wy
        wy_end = datetime.date(wy, 9, 30)
        if wy_end > last_date:
            wy_end = last_date

        start_idx = date_to_idx.get(current_date)
        end_idx   = date_to_idx.get(wy_end)

        if start_idx is not None and end_idx is not None:
            slices[wy] = (start_idx, end_idx)

        # Next WY starts Oct 1
        wy          += 1
        current_date = datetime.date(wy - 1, 10, 1)

    return slices


if __name__ == "__main__":
    main()
