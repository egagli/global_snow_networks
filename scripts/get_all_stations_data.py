# -*- coding: utf-8 -*-
"""
get_all_stations_data.py
========================
Refresh per-station CSV files from all configured clients and update station
date and verification fields in all_snow_stations.geojson.

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
  NVE: parameter 2003 in metres × 100.
  Yukon: /timeseries/measurementsDaily value in mm ÷ 10 (daily mean over
  the station's local day).
- ``snwd_cm``: Snow depth in centimetres.
  AWDB: SNWD element (inches × 2.54, converted by AWDBClient).
  CDEC: sensor 18 (Snow Depth), inches × 2.54.
  DataBC ASWS: SD.csv / SD_Archive.csv value in cm (16:00 UTC reading).
  NVE: parameter 2002 in cm.
  Yukon: /timeseries/measurementsDaily value in cm (daily mean over the
  station's local day).

Data flags are not stored in CSV files.  Use the respective client's
``get_data(include_flags=True)`` method if flag information is needed.
"""

from __future__ import annotations

import argparse
import csv
import json
import tarfile
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import pandas as pd

from clients.awdb import AWDBClient, AWDBError
from clients.cdec import CDECClient, CDECError
from clients.databc import DataBCClient, DataBCError
from clients.nve import NVEClient, NVEError
from clients.yukon import YukonClient, YukonError

# INFO so client-level diagnostics (e.g. NVE series-index coverage) are
# visible in CI logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_GEOJSON = REPO_ROOT / "all_snow_stations.geojson"
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
    unroutable: int = 0
    by_client: dict[str, int] = field(default_factory=dict)


def station_csv_path(data_dir: Path, code: str) -> Path:
    return data_dir / f"{code}.csv"


def compute_record_dates(
    df: pd.DataFrame,
) -> tuple[str | None, str | None]:
    if df.empty:
        return None, None
    obs = df.dropna(subset=["wteq_cm", "snwd_cm"], how="all")
    if obs.empty:
        return None, None
    earliest = str(obs["date"].iloc[0])
    latest = str(obs["date"].iloc[-1])
    return earliest, latest


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
            # Missing observations are empty cells, never the string "nan"
            writer.writerow([
                row["date"],
                "" if pd.isna(row["wteq_cm"]) else row["wteq_cm"],
                "" if pd.isna(row["snwd_cm"]) else row["snwd_cm"],
            ])
    tmp_path.replace(csv_path)


def write_json_atomically(path: Path, obj: Any) -> None:
    """Atomic JSON write — the merged GeoJSON is a multi-MB tracked file
    and must never be left truncated by an interrupt."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    tmp_path.replace(path)


# ── Daily-or-better verification (DESIGN.md §4) ─────────────────────────────

# A station's series counts as genuinely regular daily data only when it
# has at least this many observations in total…
_CADENCE_MIN_OBS = 30
# …and its densest 90-day window averages at least one observation every
# three days.  One-off and sporadic point measurements fail this even if
# the source labels them "daily"; seasonal pillows (winter-only) pass.
_CADENCE_WINDOW_DAYS = 90
_CADENCE_MIN_WINDOW_OBS = 30


def _column_cadence_ok(dates: pd.Series) -> bool:
    """True when a series of observation dates is regular daily data."""
    if len(dates) < _CADENCE_MIN_OBS:
        return False
    days = pd.to_datetime(dates, errors="coerce").dropna()
    if len(days) < _CADENCE_MIN_OBS:
        return False
    days = days.sort_values().reset_index(drop=True)
    # densest N-day window via two pointers
    lo = 0
    best = 0
    for hi in range(len(days)):
        while (days[hi] - days[lo]).days > _CADENCE_WINDOW_DAYS:
            lo += 1
        best = max(best, hi - lo + 1)
    return best >= _CADENCE_MIN_WINDOW_OBS


def check_daily_cadence(df: pd.DataFrame) -> tuple[bool, bool]:
    """Per-column cadence verdict (swe_ok, snwd_ok) for a station CSV."""
    if df.empty:
        return False, False
    swe = df.dropna(subset=["wteq_cm"])
    snwd = df.dropna(subset=["snwd_cm"])
    return (
        _column_cadence_ok(swe["date"]),
        _column_cadence_ok(snwd["date"]),
    )


def verify_feature_from_csv(feature: dict, df: pd.DataFrame) -> bool:
    """Stamp probe-verified daily fields onto a feature from CSV content.

    Returns True when the station passes the cadence check.  A station
    that fails stays in the inventory (never deleted — DESIGN.md §6.4)
    but is excluded from the map by ``daily_or_better = False``.
    Inactive stations with a regular historical record pass on that
    record and stay archived and mapped.
    """
    props = feature.setdefault("properties", {})
    swe_ok, snwd_ok = check_daily_cadence(df)
    passed = swe_ok or snwd_ok
    props["daily_or_better"] = passed
    props["daily_verified"] = True
    if not passed:
        props["daily_provenance"] = "none"
        note = (
            "Probe: fetched data is too sparse for a daily-or-better "
            "record (cadence check failed) — treated as periodic."
        )
        existing = props.get("notes") or ""
        if "cadence check failed" not in existing:
            props["notes"] = f"{existing} {note}".strip()
    elif not props.get("daily_provenance") or (
        props.get("daily_provenance") == "none"
    ):
        props["daily_provenance"] = "native"
    return passed


def is_fetch_candidate(props: dict) -> bool:
    """Stations worth fetching: advertised daily-or-better candidates
    plus anything previously verified (keeps inactive-but-historical
    stations refreshed)."""
    return bool(
        props.get("has_daily_swe")
        or props.get("has_daily_snwd")
        or props.get("daily_or_better")
    )


def resample_records_to_daily(
    records: list[dict], tz: str = "UTC"
) -> list[dict]:
    """Resample sub-daily records to daily means over the station-local day.

    Used ONLY for stations with no native daily series (DESIGN.md §4 —
    native daily always wins).  Produces daily-shaped records compatible
    with ``_station_records_to_df``.
    """
    if not records:
        return []
    df = pd.DataFrame(records)
    ts_col = "datetime" if "datetime" in df.columns else "date"
    stamps = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
    try:
        local = stamps.dt.tz_convert(tz)
    except Exception:
        local = stamps
    df["_local_date"] = local.dt.strftime("%Y-%m-%d")
    out: list[dict] = []
    grouped = df.groupby(["station_id", "type", "_local_date"])
    for (sid, vtype, day), grp in grouped:
        vals = pd.to_numeric(grp["value"], errors="coerce").dropna()
        out.append({
            "station_id": sid,
            "date": day,
            "variable": str(grp["variable"].iloc[0]),
            "type": vtype,
            "value": round(float(vals.mean()), 3) if len(vals) else None,
            "units": str(grp["units"].iloc[0]),
            "interval": "daily",
        })
    out.sort(key=lambda r: (r["station_id"], r["type"], r["date"]))
    return out


def update_geojson_dates(
    feature: dict,
    earliest: str | None,
    latest: str | None,
    refreshed_at_utc: str,
) -> None:
    props = feature.setdefault("properties", {})
    if earliest:
        props["earliest_record_date"] = earliest
    if latest:
        props["latest_record_date"] = latest
    props["csv_refreshed_at_utc"] = refreshed_at_utc


def report_orphan_csvs(data_dir: Path, features: list[dict]) -> list[str]:
    """CSVs with no matching inventory feature (station vanished upstream).

    Retained per DESIGN.md §6.4 — historical data is never silently
    deleted — but reported loudly so they are a decision, not an accident.
    """
    codes = {
        str(f.get("properties", {}).get("code") or "") for f in features
    }
    orphans = sorted(
        p.stem for p in data_dir.glob("*.csv") if p.stem not in codes
    )
    if orphans:
        logging.warning(
            "%d orphan station CSV(s) have no inventory feature "
            "(kept in data/ and the archive): %s",
            len(orphans),
            ", ".join(orphans[:20]) + ("…" if len(orphans) > 20 else ""),
        )
    return orphans


def build_archive(data_dir: Path, archive_path: Path) -> int:
    csv_files = sorted(data_dir.glob("*.csv"))
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: write to a temp file, then rename over the old archive.
    tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    with tarfile.open(tmp_path, mode="w:xz") as tar:
        for csv_file in csv_files:
            tar.add(csv_file, arcname=f"stations/{csv_file.name}")
    tmp_path.replace(archive_path)
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
            earliest, latest = compute_record_dates(df)
            update_geojson_dates(
                features[feat_idx], earliest, latest, refreshed_at_utc
            )
            verify_feature_from_csv(features[feat_idx], df)
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
            earliest, latest = compute_record_dates(df)
            update_geojson_dates(
                features[feat_idx], earliest, latest, refreshed_at_utc
            )
            verify_feature_from_csv(features[feat_idx], df)
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
    stats.fetched += len(by_station)

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
        earliest, latest = compute_record_dates(df)
        update_geojson_dates(
            features[feat_idx], earliest, latest, refreshed_at_utc
        )
        verify_feature_from_csv(features[feat_idx], df)
        stats.updated_csvs += 1
        stats.by_client["databc"] = stats.by_client.get("databc", 0) + 1
        updated += 1

    print(f"  [DataBC] updated {updated} station CSVs")


# ── NVE refresh ───────────────────────────────────────────────────────────────

def refresh_nve(
    stations: list[tuple[int, str]],
    features: list[dict],
    data_dir: Path,
    refreshed_at_utc: str,
    stats: RefreshStats,
    resample_probe: bool = False,
) -> None:
    """Refresh NVE station CSVs.

    ``stations`` is a list of (feature_index, station_id) tuples.
    NVE fetches per station+parameter; all IDs are passed in one call.
    """
    if not stations:
        return

    client = NVEClient()
    station_ids = [sid for _, sid in stations]
    idx_by_id = {sid: idx for idx, sid in stations}

    n = len(station_ids)
    print(f"  [NVE] Fetching daily SWE + snow depth for {n} stations...")
    # Batched so one bad batch doesn't zero the whole network for the
    # day (DESIGN.md §6.4).
    records: list[dict] = []
    batch_size = 25
    for start in range(0, n, batch_size):
        batch = station_ids[start:start + batch_size]
        try:
            records.extend(client.get_data(
                station_ids=batch,
                variables=["swe", "snwd"],
                interval="daily",
                begin_date="1950-01-01",
                end_date=date.today().isoformat(),
            ))
        except NVEError as exc:
            stats.failed_batches += 1
            print(f"  [NVE] batch {start // batch_size + 1} FAILED ({exc})")
    print(f"  [NVE] ok ({len(records)} records)")

    if resample_probe:
        # Stations with no native daily series: probe their sub-daily
        # record and resample to daily means over the station-local day
        # (DESIGN.md §4 — only when no native daily exists).  Expensive;
        # run explicitly via --resample-probe, not nightly.
        have_daily = {str(r.get("station_id")) for r in records}
        missing = [sid for sid in station_ids if sid not in have_daily]
        print(
            f"  [NVE] resample probe: {len(missing)} stations without "
            f"native daily data"
        )
        for sid in missing:
            try:
                hourly = client.get_data(
                    station_ids=[sid],
                    variables=["swe", "snwd"],
                    interval="hourly",
                    begin_date="1950-01-01",
                    end_date=date.today().isoformat(),
                )
            except NVEError as exc:
                print(f"  [NVE] resample probe {sid} FAILED ({exc})")
                continue
            resampled = resample_records_to_daily(hourly, tz="Europe/Oslo")
            if resampled:
                records.extend(resampled)
                feat_idx = idx_by_id.get(sid)
                if feat_idx is not None:
                    features[feat_idx]["properties"][
                        "daily_provenance"
                    ] = "resampled_hourly"

    # Group flat records by station_id
    by_station: dict[str, list[dict]] = {}
    for r in records:
        sid = str(r.get("station_id") or "")
        if sid:
            by_station.setdefault(sid, []).append(r)
    stats.fetched += len(by_station)

    updated = 0
    for sid in station_ids:
        feat_idx = idx_by_id.get(sid)
        if feat_idx is None:
            continue
        df = _station_records_to_df(by_station.get(sid, []))
        if df.empty:
            stats.skipped_empty += 1
            continue
        csv_path = station_csv_path(data_dir, sid)
        write_csv_atomically(csv_path, df)
        earliest, latest = compute_record_dates(df)
        update_geojson_dates(
            features[feat_idx], earliest, latest, refreshed_at_utc
        )
        verify_feature_from_csv(features[feat_idx], df)
        stats.updated_csvs += 1
        stats.by_client["nve"] = stats.by_client.get("nve", 0) + 1
        updated += 1

    print(f"  [NVE] updated {updated} station CSVs")


# ── Yukon refresh ─────────────────────────────────────────────────────────────

def refresh_yukon(
    stations: list[tuple[int, str]],
    features: list[dict],
    data_dir: Path,
    refreshed_at_utc: str,
    stats: RefreshStats,
) -> None:
    """Refresh Yukon (AquaCache) station CSVs.

    ``stations`` is a list of (feature_index, location_code) tuples.
    SWE is fetched from /timeseries/measurementsDaily (mm → cm) and snow
    depth from the same endpoint (already cm).  Daily values are means over
    the station's local day.

    Only stations with a continuous series reach this function — the 92
    manual snow courses are periodic and are excluded from
    all_daily_snow_stations.geojson upstream.
    """
    if not stations:
        return

    client = YukonClient()
    station_ids = [code for _, code in stations]
    idx_by_id = {code: idx for idx, code in stations}

    n = len(station_ids)
    print(f"  [Yukon] Fetching daily SWE + snow depth for {n} stations...")
    records: list[dict] = []
    batch_size = 50
    for start in range(0, n, batch_size):
        batch = station_ids[start:start + batch_size]
        try:
            records.extend(client.get_data(
                station_ids=batch,
                variables=["swe", "snwd"],
                interval="daily",
                begin_date="1950-01-01",
                end_date=date.today().isoformat(),
            ))
        except YukonError as exc:
            stats.failed_batches += 1
            print(
                f"  [Yukon] batch {start // batch_size + 1} FAILED ({exc})"
            )
    print(f"  [Yukon] ok ({len(records)} records)")

    # Group flat records by station_id
    by_station: dict[str, list[dict]] = {}
    for r in records:
        sid = str(r.get("station_id") or "")
        if sid:
            by_station.setdefault(sid, []).append(r)
    stats.fetched += len(by_station)

    updated = 0
    for sid in station_ids:
        feat_idx = idx_by_id.get(sid)
        if feat_idx is None:
            continue
        df = _station_records_to_df(by_station.get(sid, []))
        if df.empty:
            stats.skipped_empty += 1
            continue
        csv_path = station_csv_path(data_dir, sid)
        write_csv_atomically(csv_path, df)
        earliest, latest = compute_record_dates(df)
        update_geojson_dates(
            features[feat_idx], earliest, latest, refreshed_at_utc
        )
        verify_feature_from_csv(features[feat_idx], df)
        stats.updated_csvs += 1
        stats.by_client["yukon"] = stats.by_client.get("yukon", 0) + 1
        updated += 1

    print(f"  [Yukon] updated {updated} station CSVs")


# ── Finalize from existing CSVs ───────────────────────────────────────────────

def finalize_from_csvs(
    geojson_path: Path,
    data_dir: Path,
    archive_path: Path,
) -> None:
    """Update GeoJSON date metadata from existing CSVs, then build archive.

    Used by the ``--finalize-only`` mode after parallel per-network jobs have
    written their CSVs as GitHub Actions artifacts and those artifacts have
    been downloaded into ``data_dir``.
    """
    if not geojson_path.exists():
        raise FileNotFoundError(f"GeoJSON not found: {geojson_path}")

    with geojson_path.open("r", encoding="utf-8") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])
    refreshed_at_utc = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    updated = 0
    verified_ok = 0
    verified_failed = 0

    for feat in features:
        props = feat.get("properties", {})
        code = str(props.get("code") or "")
        if not code:
            continue
        csv_path = station_csv_path(data_dir, code)
        if not csv_path.exists():
            continue
        try:
            df = pd.read_csv(csv_path)
        except Exception as exc:
            print(f"  Warning: could not read CSV for {code}: {exc}")
            continue
        if df.empty:
            continue
        earliest, latest = compute_record_dates(df)
        update_geojson_dates(feat, earliest, latest, refreshed_at_utc)
        if verify_feature_from_csv(feat, df):
            verified_ok += 1
        else:
            verified_failed += 1
        updated += 1

    print(
        f"Updated GeoJSON dates for {updated} stations "
        f"(cadence passed: {verified_ok}, failed: {verified_failed})"
    )

    geojson.setdefault("metadata", {})
    geojson["metadata"]["csv_refreshed_at_utc"] = refreshed_at_utc
    geojson["metadata"]["csv_elements"] = ["wteq_cm", "snwd_cm"]
    geojson["metadata"]["csv_units"] = {
        "wteq_cm": "cm", "snwd_cm": "cm"
    }

    write_json_atomically(geojson_path, geojson)
    print(f"GeoJSON updated: {geojson_path}")

    report_orphan_csvs(data_dir, features)
    archived_count = build_archive(
        data_dir=data_dir, archive_path=archive_path
    )
    print(f"Archive: {archived_count} files → {archive_path}")


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
    ap.add_argument(
        "--network",
        choices=["awdb", "cdec", "databc", "nve", "yukon"],
        default=None,
        help=(
            "Only refresh this network's stations. "
            "Skips archive building and GeoJSON date update — "
            "intended for parallel per-network GitHub Actions jobs."
        ),
    )
    ap.add_argument(
        "--resample-probe",
        action="store_true",
        help=(
            "For stations with no native daily series, fetch their "
            "sub-daily record and resample to daily (station-local-day "
            "mean). Expensive — run explicitly (bootstrap), not nightly. "
            "Currently wired for NVE."
        ),
    )
    ap.add_argument(
        "--finalize-only",
        action="store_true",
        help=(
            "Skip all network fetching. Update GeoJSON dates from "
            "existing CSVs and build archive. Used after parallel "
            "per-network jobs have uploaded their CSVs as artifacts."
        ),
    )
    args = ap.parse_args()

    geojson_path = Path(args.geojson)
    data_dir = Path(args.data_dir)
    archive_path = Path(args.archive)

    # ── Finalize-only mode ────────────────────────────────────────────────────
    if args.finalize_only:
        finalize_from_csvs(geojson_path, data_dir, archive_path)
        return

    if not geojson_path.exists():
        raise FileNotFoundError(f"GeoJSON not found: {geojson_path}")

    with geojson_path.open("r", encoding="utf-8") as f:
        geojson = json.load(f)

    features = geojson.get("features", [])

    # Partition stations by client
    awdb_stations: list[tuple[int, str, str]] = []
    cdec_stations: list[tuple[int, str]] = []
    databc_stations: list[tuple[int, str]] = []
    nve_stations: list[tuple[int, str]] = []
    yukon_stations: list[tuple[int, str]] = []

    stats = RefreshStats()
    for idx, feat in enumerate(features):
        props = feat.get("properties", {})
        code = str(props.get("code") or "")
        client_name = str(props.get("client") or "").lower()

        if not code:
            continue

        if not is_fetch_candidate(props):
            # Periodic-only sites (snow courses etc.) are inventoried but
            # not archived (DESIGN.md §4).
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
        elif client_name == "nve":
            nve_stations.append((idx, code))
        elif client_name == "yukon":
            yukon_stations.append((idx, code))
        else:
            # No silent fallback (DESIGN.md §3.6): a feature without a
            # known client cannot be refreshed.  Loud, counted, skipped —
            # the contract test keeps this from ever firing.
            stats.unroutable += 1
            logging.error(
                "Station %r has unknown client %r — cannot refresh",
                code, props.get("client"),
            )

    # Restrict to the requested network when --network is given
    network = args.network
    run_awdb = (network in (None, "awdb")) and bool(awdb_stations)
    run_cdec = (network in (None, "cdec")) and bool(cdec_stations)
    run_databc = (network in (None, "databc")) and bool(databc_stations)
    run_nve = (network in (None, "nve")) and bool(nve_stations)
    run_yukon = (network in (None, "yukon")) and bool(yukon_stations)

    total = sum([
        len(awdb_stations) if run_awdb else 0,
        len(cdec_stations) if run_cdec else 0,
        len(databc_stations) if run_databc else 0,
        len(nve_stations) if run_nve else 0,
        len(yukon_stations) if run_yukon else 0,
    ])

    print("=" * 70)
    if network:
        print(f"Refreshing station CSVs — {network.upper()} only")
    else:
        print("Refreshing station CSVs — multi-client")
    print(
        f"  AWDB: {len(awdb_stations) if run_awdb else 'skip'}  "
        f"CDEC: {len(cdec_stations) if run_cdec else 'skip'}  "
        f"DataBC: {len(databc_stations) if run_databc else 'skip'}  "
        f"NVE: {len(nve_stations) if run_nve else 'skip'}  "
        f"Yukon: {len(yukon_stations) if run_yukon else 'skip'}  "
        f"(total: {total:,})"
    )
    print("=" * 70)

    refreshed_at_utc = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    if run_awdb:
        refresh_awdb(
            awdb_stations, features, data_dir, refreshed_at_utc, stats
        )
    if run_cdec:
        refresh_cdec(
            cdec_stations, features, data_dir, refreshed_at_utc, stats
        )
    if run_databc:
        refresh_databc(
            databc_stations, features, data_dir, refreshed_at_utc, stats
        )
    if run_nve:
        refresh_nve(
            nve_stations, features, data_dir, refreshed_at_utc, stats,
            resample_probe=args.resample_probe,
        )
    if run_yukon:
        refresh_yukon(
            yukon_stations, features, data_dir, refreshed_at_utc, stats
        )

    # In per-network mode: CSVs written, skip GeoJSON update and archive.
    # The downstream finalize job handles those via --finalize-only.
    if network:
        print(f"\n[{network.upper()}] CSVs written to {data_dir}")
        print(f"  updated: {stats.updated_csvs:,}  "
              f"failed: {stats.failed_batches:,}")
        return

    # Update GeoJSON metadata (full run only)
    geojson.setdefault("metadata", {})
    geojson["metadata"]["csv_refreshed_at_utc"] = refreshed_at_utc
    geojson["metadata"]["csv_elements"] = ["wteq_cm", "snwd_cm"]
    geojson["metadata"]["csv_units"] = {"wteq_cm": "cm", "snwd_cm": "cm"}

    write_json_atomically(geojson_path, geojson)

    report_orphan_csvs(data_dir, features)
    archived_count = build_archive(
        data_dir=data_dir, archive_path=archive_path
    )

    print("\n" + "=" * 70)
    print("Refresh summary")
    print("=" * 70)
    print(f"Fetched station payloads : {stats.fetched:,}")
    print(f"CSV files updated        : {stats.updated_csvs:,}")
    print(f"  by client              : {stats.by_client}")
    print(f"Empty station payloads   : {stats.skipped_empty:,}")
    print(f"Failed batches           : {stats.failed_batches:,}")
    print(f"Unroutable stations      : {stats.unroutable:,}")
    print(f"Archive members          : {archived_count:,}")
    print(f"Archive written          : {archive_path}")
    print(f"GeoJSON updated          : {geojson_path}")


if __name__ == "__main__":
    main()
