# -*- coding: utf-8 -*-
"""
get_all_stations_data.py
========================
Refresh per-station CSV files from all configured clients and update station
date fields in all_daily_snow_stations.geojson.

Workflow
--------
1. Read station list from GeoJSON created by create_all_stations_geojson.py.
2. Route each station to the appropriate client based on its ``client`` field.
3. Pull fresh data in batches from each client.
4. If fetch succeeds for a station, atomically replace that station CSV.
5. Update geojson properties (earliest/latest/updated dates) from new CSV.
6. Build a tar.xz archive containing all station CSV files.

CSV schema (all clients)
------------------------
    date,wteq_cm,snwd_cm

- ``wteq_cm``: Snow water equivalent in centimetres.
  AWDB: WTEQ element (inches × 2.54, converted by AWDBClient).
  CDEC: sensor 82 (SNO ADJ, preferred) or sensor 3 (raw SWE), inches × 2.54.
  DataBC ASWS: SWDaily.csv value in mm ÷ 10.
- ``snwd_cm``: Snow depth in centimetres.
  AWDB: SNWD element (inches × 2.54, converted by AWDBClient).
  CDEC: sensor 18 (Snow Depth), inches × 2.54.
  DataBC ASWS: SD.csv / SD_Archive.csv value in cm (16:00 UTC reading).

Data flags are not stored in CSV files.  Use the respective client's
``get_data(include_flags=True)`` method if flag information is needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import tarfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from clients.awdb import AWDBClient, AWDBError
from clients.cdec import CDECClient, CDECError
from clients.databc import DataBCClient, DataBCError

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GEOJSON = REPO_ROOT / "all_daily_snow_stations.geojson"
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "stations"
DEFAULT_ARCHIVE = REPO_ROOT / "data" / "all_station_csvs.tar.xz"

# AWDB batching
AWDB_BATCH = 5

# CDEC batching
CDEC_BATCH = 20


@dataclass
class RefreshStats:
    fetched: int = 0
    failed_batches: int = 0
    updated_csvs: int = 0
    skipped_empty: int = 0
    by_client: dict[str, int] = field(default_factory=dict)


def station_csv_path(data_dir: Path, code: str) -> Path:
    return data_dir / f"{code}.csv"


def compute_record_dates(
    df: pd.DataFrame,
) -> tuple[str | None, str | None, str | None]:
    if df.empty:
        return None, None, None
    obs = df.dropna(subset=["wteq_cm", "snwd_cm"], how="all")
    if obs.empty:
        return None, None, None
    earliest = str(obs["date"].iloc[0])
    latest = str(obs["date"].iloc[-1])
    return earliest, latest, latest


def write_csv_atomically(csv_path: Path, df: pd.DataFrame) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        "w",
        delete=False,
        dir=str(csv_path.parent),
        suffix=".tmp",
        newline="",
    ) as tmp:
        tmp_path = Path(tmp.name)
        writer = csv.writer(tmp)
        writer.writerow(["date", "wteq_cm", "snwd_cm"])
        for _, row in df.iterrows():
            writer.writerow([row["date"], row["wteq_cm"], row["snwd_cm"]])
    tmp_path.replace(csv_path)


def update_geojson_dates(
    feature: dict,
    earliest: str | None,
    latest: str | None,
    updated: str | None,
    csv_rel_path: str,
    refreshed_at_utc: str,
) -> None:
    props = feature.setdefault("properties", {})
    if earliest:
        props["earliest_record_date"] = earliest
    if latest:
        props["latest_record_date"] = latest
    if updated:
        props["updated_date"] = updated
    props["csv_path"] = csv_rel_path
    props["csv_refreshed_at_utc"] = refreshed_at_utc


def build_archive(data_dir: Path, archive_path: Path) -> int:
    csv_files = sorted(data_dir.glob("*.csv"))
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="w:xz") as tar:
        for csv_file in csv_files:
            tar.add(csv_file, arcname=f"stations/{csv_file.name}")
    return len(csv_files)


# ── Shared helpers ────────────────────────────────────────────────────────────

def _station_records_to_df(station_records: list[dict]) -> pd.DataFrame:
    """Convert flat standardized records for one station to {date, wteq_cm, snwd_cm}."""
    by_date: dict[str, dict[str, Any]] = {}
    for r in station_records:
        d = str(r.get("date") or "")[:10]
        if not d:
            continue
        t = r.get("type", "")
        v = r.get("value")
        if d not in by_date:
            by_date[d] = {"date": d, "wteq_cm": None, "snwd_cm": None}
        if t == "swe":
            by_date[d]["wteq_cm"] = v
        elif t == "snwd":
            by_date[d]["snwd_cm"] = v
    if not by_date:
        return pd.DataFrame(columns=["date", "wteq_cm", "snwd_cm"])
    df = pd.DataFrame(by_date.values()).sort_values("date")
    return df[["date", "wteq_cm", "snwd_cm"]]


# ── AWDB refresh ──────────────────────────────────────────────────────────────


def refresh_awdb(
    stations: list[tuple[int, str, str]],
    features: list[dict],
    data_dir: Path,
    refreshed_at_utc: str,
    stats: RefreshStats,
) -> None:
    """Refresh AWDB station CSVs.

    ``stations`` is a list of (feature_index, code, triplet) tuples.
    """
    client = AWDBClient()
    total_batches = (len(stations) + AWDB_BATCH - 1) // AWDB_BATCH
    for start in range(0, len(stations), AWDB_BATCH):
        batch = stations[start: start + AWDB_BATCH]
        triplets = [t for _, _, t in batch]
        batch_no = start // AWDB_BATCH + 1
        print(
            f"  [AWDB] Batch {batch_no}/{total_batches} "
            f"({len(batch)} stations)...",
            end=" ",
            flush=True,
        )
        try:
            records = client.get_data(
                station_ids=triplets,
                variables=["swe", "snwd"],
                interval="daily",
                begin_date="1800-01-01",
                end_date=date.today().isoformat(),
            )
        except AWDBError as exc:
            stats.failed_batches += 1
            print(f"FAILED ({exc})")
            continue

        # Group flat records by station_id for efficient per-station lookup
        by_triplet: dict[str, list[dict]] = {}
        for r in records:
            sid = str(r.get("station_id") or "")
            if sid:
                by_triplet.setdefault(sid, []).append(r)
        stats.fetched += len(by_triplet)

        updated = 0
        for feat_idx, code, triplet in batch:
            station_recs = by_triplet.get(triplet)
            if not station_recs:
                continue
            df = _station_records_to_df(station_recs)
            if df.empty:
                stats.skipped_empty += 1
                continue
            csv_path = station_csv_path(data_dir, code)
            write_csv_atomically(csv_path, df)
            earliest, latest, upd = compute_record_dates(df)
            update_geojson_dates(
                features[feat_idx],
                earliest,
                latest,
                upd,
                f"stations/{code}.csv",
                refreshed_at_utc,
            )
            stats.updated_csvs += 1
            stats.by_client["awdb"] = stats.by_client.get("awdb", 0) + 1
            updated += 1

        print(f"updated {updated}")


# ── CDEC refresh ──────────────────────────────────────────────────────────────

def refresh_cdec(
    stations: list[tuple[int, str]],
    features: list[dict],
    data_dir: Path,
    refreshed_at_utc: str,
    stats: RefreshStats,
) -> None:
    """Refresh CDEC station CSVs.

    ``stations`` is a list of (feature_index, station_id) tuples.
    """
    client = CDECClient()
    station_ids = [sid for _, sid in stations]
    idx_by_id = {sid: idx for idx, sid in stations}
    total_batches = (len(station_ids) + CDEC_BATCH - 1) // CDEC_BATCH

    for start in range(0, len(station_ids), CDEC_BATCH):
        batch = station_ids[start: start + CDEC_BATCH]
        batch_no = start // CDEC_BATCH + 1
        print(
            f"  [CDEC] Batch {batch_no}/{total_batches} "
            f"({len(batch)} stations)...",
            end=" ",
            flush=True,
        )
        try:
            records = client.get_data(
                station_ids=batch,
                variables=["swe", "snwd"],
                interval="daily",
                begin_date="1900-01-01",
                end_date=date.today().isoformat(),
            )
        except CDECError as exc:
            stats.failed_batches += 1
            print(f"FAILED ({exc})")
            continue

        # Group flat records by station_id
        by_station: dict[str, list[dict]] = {}
        for r in records:
            sid = str(r.get("station_id") or "").strip().upper()
            if sid:
                by_station.setdefault(sid, []).append(r)
        stats.fetched += len(by_station)

        updated = 0
        for sid in batch:
            feat_idx = idx_by_id.get(sid)
            if feat_idx is None:
                continue
            station_recs = by_station.get(sid, [])
            df = _station_records_to_df(station_recs)
            if df.empty:
                stats.skipped_empty += 1
                continue
            csv_path = station_csv_path(data_dir, sid)
            write_csv_atomically(csv_path, df)
            earliest, latest, upd = compute_record_dates(df)
            update_geojson_dates(
                features[feat_idx],
                earliest,
                latest,
                upd,
                f"stations/{sid}.csv",
                refreshed_at_utc,
            )
            stats.updated_csvs += 1
            stats.by_client["cdec"] = stats.by_client.get("cdec", 0) + 1
            updated += 1

        print(f"updated {updated}")


# ── DataBC refresh ────────────────────────────────────────────────────────────

def refresh_databc(
    stations: list[tuple[int, str]],
    features: list[dict],
    data_dir: Path,
    refreshed_at_utc: str,
    stats: RefreshStats,
) -> None:
    """Refresh DataBC ASWS station CSVs.

    ``stations`` is a list of (feature_index, location_id) tuples.
    SWE is fetched from SWDaily.csv (mm → cm).
    Snow depth is fetched from SD.csv / SD_Archive.csv (cm).
    """
    if not stations:
        return

    client = DataBCClient()
    location_ids = [lid for _, lid in stations]
    idx_by_id = {lid: idx for idx, lid in stations}

    n = len(location_ids)
    print(
        f"  [DataBC] Loading daily SWE + snow depth for {n} ASWS stations...",
        end=" ",
        flush=True,
    )
    try:
        records = client.get_data(
            station_ids=location_ids,
            variables=["swe", "snwd"],
            interval="daily",
        )
        print(f"ok ({len(records)} records)")
    except DataBCError as exc:
        stats.failed_batches += 1
        print(f"FAILED ({exc})")
        records = []

    # Group flat records by station_id
    by_station: dict[str, list[dict]] = {}
    for r in records:
        sid = str(r.get("station_id") or "")
        if sid:
            by_station.setdefault(sid, []).append(r)
    stats.fetched += len(location_ids)

    updated = 0
    for lid_str in location_ids:
        feat_idx = idx_by_id.get(lid_str)
        if feat_idx is None:
            continue

        df = _station_records_to_df(by_station.get(lid_str, []))
        if df.empty:
            stats.skipped_empty += 1
            continue

        csv_path = station_csv_path(data_dir, lid_str)
        write_csv_atomically(csv_path, df)
        earliest, latest, upd = compute_record_dates(df)
        update_geojson_dates(
            features[feat_idx],
            earliest,
            latest,
            upd,
            f"stations/{lid_str}.csv",
            refreshed_at_utc,
        )
        stats.updated_csvs += 1
        stats.by_client["databc"] = stats.by_client.get("databc", 0) + 1
        updated += 1

    print(f"  [DataBC] updated {updated} station CSVs")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Refresh per-station CSVs and update GeoJSON date fields"
    )
    ap.add_argument(
        "--geojson",
        default=str(DEFAULT_GEOJSON),
        help="Input/output station GeoJSON path",
    )
    ap.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Station CSV directory",
    )
    ap.add_argument(
        "--archive",
        default=str(DEFAULT_ARCHIVE),
        help="Output tar.xz archive path",
    )
    args = ap.parse_args()

    geojson_path = Path(args.geojson)
    data_dir = Path(args.data_dir)
    archive_path = Path(args.archive)

    if not geojson_path.exists():
        raise FileNotFoundError(f"GeoJSON not found: {geojson_path}")

    with geojson_path.open("r", encoding="utf-8") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    # Partition stations by client
    awdb_stations: list[tuple[int, str, str]] = []
    cdec_stations: list[tuple[int, str]] = []
    databc_stations: list[tuple[int, str]] = []

    for idx, feat in enumerate(features):
        props = feat.get("properties", {})
        code = str(props.get("code") or "")
        client_name = str(props.get("client") or "awdb").lower()

        if not code:
            continue

        if client_name == "awdb":
            triplet = props.get("awdb_station_triplet") or code.replace(
                "_", ":"
            )
            awdb_stations.append((idx, code, str(triplet)))
        elif client_name == "cdec":
            cdec_stations.append((idx, code))
        elif client_name == "databc":
            databc_stations.append((idx, code))

    total = len(awdb_stations) + len(cdec_stations) + len(databc_stations)
    print("=" * 70)
    print("Refreshing station CSVs — multi-client")
    print(
        f"  AWDB: {len(awdb_stations):,}  "
        f"CDEC: {len(cdec_stations):,}  "
        f"DataBC: {len(databc_stations):,}  "
        f"(total: {total:,})"
    )
    print("=" * 70)

    refreshed_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = RefreshStats()

    if awdb_stations:
        refresh_awdb(
            awdb_stations, features, data_dir, refreshed_at_utc, stats
        )
    if cdec_stations:
        refresh_cdec(
            cdec_stations, features, data_dir, refreshed_at_utc, stats
        )
    if databc_stations:
        refresh_databc(
            databc_stations, features, data_dir, refreshed_at_utc, stats
        )

    # Update GeoJSON metadata
    geojson.setdefault("metadata", {})
    geojson["metadata"]["csv_refreshed_at_utc"] = refreshed_at_utc
    geojson["metadata"]["csv_elements"] = ["wteq_cm", "snwd_cm"]
    geojson["metadata"]["csv_units"] = {"wteq_cm": "cm", "snwd_cm": "cm"}

    with geojson_path.open("w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2)

    archived_count = build_archive(data_dir=data_dir, archive_path=archive_path)

    print("\n" + "=" * 70)
    print("Refresh summary")
    print("=" * 70)
    print(f"Fetched station payloads : {stats.fetched:,}")
    print(f"CSV files updated        : {stats.updated_csvs:,}")
    print(f"  by client              : {stats.by_client}")
    print(f"Empty station payloads   : {stats.skipped_empty:,}")
    print(f"Failed batches           : {stats.failed_batches:,}")
    print(f"Archive members          : {archived_count:,}")
    print(f"Archive written          : {archive_path}")
    print(f"GeoJSON updated          : {geojson_path}")


if __name__ == "__main__":
    main()
