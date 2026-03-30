# -*- coding: utf-8 -*-
"""
get_all_stations_data.py
========================
Refresh per-station CSV files from AWDB and update station date fields in
snow_stations.geojson based on the refreshed CSV content.

Workflow
--------
1. Read station list from GeoJSON created by create_all_stations_geojson.py
2. Pull fresh data in batches from the data client
3. If fetch succeeds for a station, atomically replace that station CSV
4. Update geojson properties (earliest/latest/updated dates) from new CSV data
5. Build a tar.xz archive containing all station CSV files
"""

from __future__ import annotations

import argparse
import csv
import json
import tarfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd
from clients import AWDBClient
from clients.awdb_client import AWDBError

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GEOJSON = REPO_ROOT / "snow_stations.geojson"
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "stations"
DEFAULT_ARCHIVE = REPO_ROOT / "data" / "all_station_csvs.tar.xz"

SNOW_ELEMENTS = ["WTEQ", "SNWD"]
BATCH_SIZE = 5


@dataclass
class RefreshStats:
    fetched: int = 0
    failed_batches: int = 0
    updated_csvs: int = 0
    skipped_empty: int = 0


def get_client(client_name: str) -> Any:
    if client_name.lower() == "awdb":
        return AWDBClient()
    raise ValueError(f"Unsupported client: {client_name}")


def code_to_triplet(code: str) -> str:
    return str(code).replace("_", ":")


def station_csv_path(data_dir: Path, code: str) -> Path:
    return data_dir / f"{code}.csv"


def parse_station_response_to_df(station_data: dict) -> pd.DataFrame:
    rows: dict[str, dict[str, float | None]] = {}
    for block in station_data.get("data", []):
        element = block.get("stationElement", {}).get("elementCode")
        if element not in SNOW_ELEMENTS:
            continue
        column = f"{element.lower()}_cm"
        for rec in block.get("values", []):
            d = str(rec.get("date") or "")[:10]
            if not d:
                continue
            if d not in rows:
                rows[d] = {"date": d, "wteq_cm": None, "snwd_cm": None}
            rows[d][column] = rec.get("value")

    if not rows:
        return pd.DataFrame(columns=["date", "wteq_cm", "snwd_cm"])

    df = pd.DataFrame(rows.values()).sort_values("date")
    return df[["date", "wteq_cm", "snwd_cm"]]


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
    updated = latest
    return earliest, latest, updated


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


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Refresh per-station CSVs and update GeoJSON date fields"
    )
    ap.add_argument(
        "--client", default="awdb", help="Data client to use (default: awdb)"
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

    client = get_client(args.client)
    geojson_path = Path(args.geojson)
    data_dir = Path(args.data_dir)
    archive_path = Path(args.archive)

    if not geojson_path.exists():
        raise FileNotFoundError(f"GeoJSON not found: {geojson_path}")

    with geojson_path.open("r", encoding="utf-8") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    if not isinstance(features, list):
        raise ValueError("GeoJSON malformed: 'features' is not a list")

    stations: list[tuple[int, str, str]] = []
    for idx, feat in enumerate(features):
        props = feat.get("properties", {})
        code = props.get("code") or props.get("stationTriplet")
        triplet = props.get("awdb_station_triplet") or code_to_triplet(code)
        if code and triplet:
            stations.append((idx, str(code), str(triplet)))

    print("=" * 70)
    print(f"Refreshing station CSVs from {args.client} client")
    print(f"Stations discovered: {len(stations):,}")
    print("=" * 70)

    refreshed_at_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stats = RefreshStats()

    for start in range(0, len(stations), BATCH_SIZE):
        batch = stations[start:start + BATCH_SIZE]
        triplets = [t for _, _, t in batch]
        batch_no = (start // BATCH_SIZE) + 1
        batch_total = (len(stations) + BATCH_SIZE - 1) // BATCH_SIZE
        print(
            f"Batch {batch_no}/{batch_total} ({len(batch)} stations)...",
            end=" ",
            flush=True,
        )

        try:
            response = client.get_data(
                triplets=triplets,
                elements=SNOW_ELEMENTS,
                duration="DAILY",
                begin_date="1800-01-01",
                end_date=date.today().isoformat(),
            )
        except AWDBError as exc:
            stats.failed_batches += 1
            print(f"FAILED ({exc})")
            continue

        stats.fetched += len(response)
        by_triplet = {
            r.get("stationTriplet"): r
            for r in response
            if r.get("stationTriplet")
        }

        updated_this_batch = 0
        for feat_idx, code, triplet in batch:
            station_payload = by_triplet.get(triplet)
            if not station_payload:
                continue
            df = parse_station_response_to_df(station_payload)
            if df.empty:
                stats.skipped_empty += 1
                continue

            csv_path = station_csv_path(data_dir, code)
            write_csv_atomically(csv_path, df)
            earliest, latest, updated = compute_record_dates(df)
            update_geojson_dates(
                feature=features[feat_idx],
                earliest=earliest,
                latest=latest,
                updated=updated,
                csv_rel_path=f"stations/{code}.csv",
                refreshed_at_utc=refreshed_at_utc,
            )
            stats.updated_csvs += 1
            updated_this_batch += 1

        print(f"updated {updated_this_batch}")

    geojson.setdefault("metadata", {})
    geojson["metadata"]["csv_refreshed_at_utc"] = refreshed_at_utc
    geojson["metadata"]["csv_elements"] = SNOW_ELEMENTS
    geojson["metadata"]["csv_units"] = {"WTEQ": "cm", "SNWD": "cm"}

    with geojson_path.open("w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2)

    archived_count = build_archive(
        data_dir=data_dir,
        archive_path=archive_path,
    )

    print("\n" + "=" * 70)
    print("Refresh summary")
    print("=" * 70)
    print(f"Fetched station payloads : {stats.fetched:,}")
    print(f"CSV files updated        : {stats.updated_csvs:,}")
    print(f"Empty station payloads   : {stats.skipped_empty:,}")
    print(f"Failed batches           : {stats.failed_batches:,}")
    print(f"Archive members          : {archived_count:,}")
    print(f"Archive written          : {archive_path}")
    print(f"GeoJSON updated          : {geojson_path}")


if __name__ == "__main__":
    main()
