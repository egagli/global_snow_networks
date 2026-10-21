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
  Hydro-Québec: GMON SWE in mm ÷ 10, last valid value of the
  America/Toronto local day (published hourly, changes every 6 h).
  Alberta: pillow SW in mm ÷ 10, AEPA's own daily mean (A.DayMean) over
  the MST day.
- ``snwd_cm``: Snow depth in centimetres.
  AWDB: SNWD element (inches × 2.54, converted by AWDBClient).
  CDEC: sensor 18 (Snow Depth), inches × 2.54.
  DataBC ASWS: SD.csv / SD_Archive.csv value in cm (16:00 UTC reading).
  NVE: parameter 2002 in cm.
  Yukon: /timeseries/measurementsDaily value in cm (daily mean over the
  station's local day).
  Hydro-Québec: hourly depth in cm, last valid value of the local day.
  Alberta: SD in cm, AEPA's A.DayMean.
  SLF IMIS: HS in cm, median over the 24 h ending at the date's
  stamp; SLF study plots: manual morning HS reading in cm.
  SNIH: pillow SWE in mm ÷ 10 — the validated daily record, then (after
  ``snih_validated_through``) the median of the day's raw telemetry after
  the spike filter (:func:`snih_daily_from_raw`).

Hydro-Québec's feed keeps only ~10 days, so its CSVs are *merged* into
rather than replaced (``StationWriter.store(merge=True)``): the archive
is the only place its history exists.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from easysnowdata import auth
from easysnowdata.auth import CredentialError
from easysnowdata.stations.clients.alberta import AlbertaClient, AlbertaError
from easysnowdata.stations.clients.awdb import AWDBClient, AWDBError
from easysnowdata.stations.clients.cdec import CDECClient, CDECError
from easysnowdata.stations.clients.databc import DataBCClient, DataBCError
from easysnowdata.stations.clients.hydroquebec import (
    HydroQuebecClient,
    HydroQuebecError,
)
from easysnowdata.stations.clients.nve import NVEClient, NVEError
from easysnowdata.stations.clients.slf import SLFClient, SLFError
from easysnowdata.stations.clients.snih import SNIHClient, SNIHError
from easysnowdata.stations.clients.yukon import YukonClient, YukonError

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
    refused_shrink: int = 0
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
        # Coerce to numbers first: a stray string "nan" (or any other
        # non-number) must become an empty cell too, never be passed
        # through as text (DESIGN.md §6.3).
        swe = pd.to_numeric(df["wteq_cm"], errors="coerce")
        snwd = pd.to_numeric(df["snwd_cm"], errors="coerce")
        for d, w, sd in zip(df["date"], swe, snwd):
            # Missing observations are empty cells, never the string "nan"
            writer.writerow([
                d,
                "" if pd.isna(w) else w,
                "" if pd.isna(sd) else sd,
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
    # densest N-day window, vectorized: for each observation, count how
    # many observations fall within the preceding window
    day_numbers = np.sort(
        days.values.astype("datetime64[D]").astype("int64")
    )
    window_starts = day_numbers - _CADENCE_WINDOW_DAYS
    counts = (
        np.arange(1, len(day_numbers) + 1)
        - np.searchsorted(day_numbers, window_starts, side="left")
    )
    return int(counts.max()) >= _CADENCE_MIN_WINDOW_OBS


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


# Clients whose archived daily values are always aggregated from a
# sub-daily series (no native daily product), so a station that passes the
# probe is never "native" (DESIGN.md §4).
PASSED_PROVENANCE: dict[str, str] = {"hydroquebec": "resampled_hourly"}


def passed_provenance(props: dict) -> str:
    """``daily_provenance`` for a station that passes the probe.

    Native unless its client has no daily product (:data:`PASSED_PROVENANCE`)
    or — SNIH — the station's CSV continues past its validated daily
    record with resampled raw telemetry: its latest record date (stamped
    from the CSV just before the probe) is after
    ``snih_validated_through``, or it never had a validated record.
    """
    client = str(props.get("client") or "")
    if client == "snih":
        through = props.get("snih_validated_through")
        latest = props.get("latest_record_date")
        raw = not through or (latest is not None and str(latest) > str(through))
        return "resampled_sub_daily" if raw else "native"
    return PASSED_PROVENANCE.get(client, "native")


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
    elif props.get("client") == "snih" or not props.get(
        "daily_provenance"
    ) or props.get("daily_provenance") == "none":
        # SNIH is re-derived every time: a station's CSV moves from
        # validated-only to validated + raw as telemetry arrives.
        props["daily_provenance"] = passed_provenance(props)
    return passed


def is_fetch_candidate(props: dict) -> bool:
    """Stations worth fetching: advertised daily-or-better candidates
    plus anything previously verified (keeps inactive-but-historical
    stations refreshed).

    Stations retained after vanishing from their source's station list
    (``upstream_status == "missing"``, DESIGN.md §6.4) are not fetched:
    the source no longer serves them, and their archived CSV is kept as
    it is.
    """
    if props.get("upstream_status") == "missing":
        return False
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


# ── Shrink guard and revision log (DESIGN.md §6.3, §6.6) ─────────────────────

# A replacement CSV may lose this many populated rows (either column
# non-null) before it is refused: max(absolute, fraction of the old count).
SHRINK_MAX_LOST_ROWS = 30
SHRINK_MAX_LOST_FRACTION = 0.05
# ...and its earliest populated date may move this much later.
SHRINK_MAX_START_SHIFT_DAYS = 30

# Two values closer than this are the same observation (float noise from
# a unit conversion is not a revision).
REVISION_TOLERANCE = 1e-6

DEFAULT_REVISIONS_DIR = REPO_ROOT / "data" / "revisions"
REVISION_COLUMNS = [
    "detected_utc", "code", "client", "date", "variable",
    "old_value", "new_value", "kind",
]
_VALUE_COLUMNS = ("wteq_cm", "snwd_cm")


def read_station_csv(csv_path: Path) -> pd.DataFrame | None:
    """Read a committed station CSV as ``date`` strings + float columns.

    Returns None when there is no file (a new station).  Non-numeric
    cells (e.g. a legacy literal ``nan``) read as missing.
    """
    if not csv_path.exists():
        return None
    df = pd.read_csv(
        csv_path, dtype={"date": str}, float_precision="round_trip"
    )
    for col in _VALUE_COLUMNS:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["date", *_VALUE_COLUMNS]]


def _as_numeric(df: pd.DataFrame) -> pd.DataFrame:
    """``date`` as strings, value columns as floats (None → NaN)."""
    out = df[["date", *_VALUE_COLUMNS]].copy()
    out["date"] = out["date"].astype(str)
    for col in _VALUE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _populated(df: pd.DataFrame) -> pd.DataFrame:
    return df.dropna(subset=list(_VALUE_COLUMNS), how="all")


def shrink_reason(old: pd.DataFrame, new: pd.DataFrame) -> str | None:
    """Why replacing *old* with *new* would lose history, or None.

    The nightly refresh re-downloads every station's full record and
    replaces its CSV, which is how upstream revisions reach the archive
    (DESIGN.md §6.6).  Only an *empty* payload used to be refused, so a
    truncated-but-non-empty response would silently delete history.  A
    replacement is refused when it loses more than
    max(SHRINK_MAX_LOST_ROWS, SHRINK_MAX_LOST_FRACTION × old) populated
    rows, or when its earliest populated date moves more than
    SHRINK_MAX_START_SHIFT_DAYS later.
    """
    old_pop = _populated(_as_numeric(old))
    new_pop = _populated(_as_numeric(new))
    n_old, n_new = len(old_pop), len(new_pop)
    lost = n_old - n_new
    allowed = max(SHRINK_MAX_LOST_ROWS, SHRINK_MAX_LOST_FRACTION * n_old)
    if lost > allowed:
        return (
            f"populated rows {n_old:,} → {n_new:,} "
            f"(loses {lost:,}, more than {allowed:,.0f})"
        )
    if n_old and n_new:
        old_start = pd.Timestamp(min(old_pop["date"]))
        new_start = pd.Timestamp(min(new_pop["date"]))
        shift = (new_start - old_start).days
        if shift > SHRINK_MAX_START_SHIFT_DAYS:
            return (
                f"record start {old_start.date()} → {new_start.date()} "
                f"({shift} days later)"
            )
    return None


def _fmt_value(v: float) -> str:
    return "" if pd.isna(v) else str(v)


def merge_with_archive(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """*new* laid over *old*, for sources that serve only a recent window.

    A rolling-window feed (Hydro-Québec keeps ~10 days) cannot replace an
    archived CSV: everything older than the window would be lost.  Dates
    only in *old* are kept; for dates in both, a value in *new* wins and a
    blank in *new* keeps the archived value (a sensor gap in today's
    response is not evidence that yesterday's reading was wrong).
    """
    old, new = _as_numeric(old), _as_numeric(new)
    merged = old.merge(new, on="date", how="outer", suffixes=("_old", ""))
    for col in _VALUE_COLUMNS:
        merged[col] = merged[col].combine_first(merged[f"{col}_old"])
    return (
        merged[["date", *_VALUE_COLUMNS]]
        .sort_values("date", ignore_index=True)
    )


def diff_revisions(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """Cells of *old*'s period that *new* changes (DESIGN.md §6.6).

    Compares every date up to the old file's last date; dates *new*
    appends beyond it are new observations, not revisions.  Returns
    columns ``date, variable, old_value, new_value, kind`` with kind
    ``revised`` (value → different value), ``filled`` (blank → value)
    or ``removed`` (value → blank, or the date is gone).  Values are
    strings as written in the CSV; blank is ``""``.
    """
    out_cols = ["date", "variable", "old_value", "new_value", "kind"]
    old, new = _as_numeric(old), _as_numeric(new)
    if old.empty:
        return pd.DataFrame(columns=out_cols)
    old_last = max(old["date"])
    merged = old.merge(
        new[new["date"] <= old_last],
        on="date", how="outer", suffixes=("_old", "_new"),
    )
    parts = []
    for col in _VALUE_COLUMNS:
        o, n = merged[f"{col}_old"], merged[f"{col}_new"]
        o_na, n_na = o.isna(), n.isna()
        changed = (o - n).abs() > REVISION_TOLERANCE
        kind = pd.Series(pd.NA, index=merged.index, dtype="object")
        kind[o_na & ~n_na] = "filled"
        kind[~o_na & n_na] = "removed"
        kind[~o_na & ~n_na & changed] = "revised"
        hit = kind.notna()
        if not hit.any():
            continue
        parts.append(pd.DataFrame({
            "date": merged.loc[hit, "date"],
            "variable": col,
            "old_value": o[hit].map(_fmt_value),
            "new_value": n[hit].map(_fmt_value),
            "kind": kind[hit],
        }))
    if not parts:
        return pd.DataFrame(columns=out_cols)
    return (
        pd.concat(parts, ignore_index=True)
        .sort_values(["date", "variable"], ignore_index=True)
    )


@dataclass
class RevisionLog:
    """Rows for ``data/revisions/`` collected during one refresh run.

    A full run appends them to ``YYYY-MM.csv`` (month of detection).  A
    per-network job (``--network``) writes ``pending/<network>.csv``
    instead, because parallel jobs cannot safely append to one file;
    ``--finalize-only`` merges the pending files into the month files.
    """

    detected_utc: str
    frames: list[pd.DataFrame] = field(default_factory=list)

    def add(self, code: str, client: str, rows: pd.DataFrame) -> None:
        if rows.empty:
            return
        rows = rows.copy()
        rows.insert(0, "client", client)
        rows.insert(0, "code", code)
        rows.insert(0, "detected_utc", self.detected_utc)
        self.frames.append(rows[REVISION_COLUMNS])

    def add_refusal(
        self, code: str, client: str, old: pd.DataFrame,
        new: pd.DataFrame,
    ) -> None:
        """One ``refused_shrink`` row: ``variable`` is ``rows`` and the
        values are populated-row counts, ``date`` the old record start."""
        old_pop = _populated(old)
        self.add(code, client, pd.DataFrame([{
            "date": min(old_pop["date"]) if len(old_pop) else "",
            "variable": "rows",
            "old_value": str(len(old_pop)),
            "new_value": str(len(_populated(new))),
            "kind": "refused_shrink",
        }]))

    def table(self) -> pd.DataFrame:
        if not self.frames:
            return pd.DataFrame(columns=REVISION_COLUMNS)
        return pd.concat(self.frames, ignore_index=True)

    def summary(self) -> list[str]:
        """Nightly summary lines: rows per client and kind, oldest date."""
        df = self.table()
        if df.empty:
            return ["Revisions                : none"]
        lines = [f"Revisions logged         : {len(df):,}"]
        for client, grp in df.groupby("client"):
            kinds = ", ".join(
                f"{k} {n:,}" for k, n in grp["kind"].value_counts().items()
            )
            dated = grp.loc[grp["kind"] != "refused_shrink", "date"]
            oldest = f", oldest {min(dated)}" if len(dated) else ""
            lines.append(
                f"  {client:<22} : {grp['code'].nunique():,} stations; "
                f"{kinds}{oldest}"
            )
        return lines

    def write(self, revisions_dir: Path, network: str | None = None) -> int:
        df = self.table()
        if df.empty:
            return 0
        if network:
            path = revisions_dir / "pending" / f"{network}.csv"
            _write_revision_file(path, df)
        else:
            append_revisions(revisions_dir, df)
        return len(df)


def _write_revision_file(path: Path, df: pd.DataFrame) -> None:
    """Atomic write of a whole revision file (temp file + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    df[REVISION_COLUMNS].to_csv(tmp_path, index=False)
    tmp_path.replace(path)


def append_revisions(revisions_dir: Path, df: pd.DataFrame) -> None:
    """Append revision rows to ``<revisions_dir>/YYYY-MM.csv`` by the
    month of ``detected_utc``.  Each month file is rewritten whole and
    renamed into place, so an interrupted run never leaves half a row."""
    if df.empty:
        return
    months = df["detected_utc"].str[:7]
    for month, rows in df.groupby(months):
        path = revisions_dir / f"{month}.csv"
        if path.exists():
            existing = pd.read_csv(path, dtype=str, keep_default_na=False)
            rows = pd.concat([existing, rows], ignore_index=True)
        _write_revision_file(path, rows)


def merge_pending_revisions(revisions_dir: Path) -> int:
    """Fold per-network ``pending/*.csv`` files into the month files and
    delete them (``--finalize-only``).  Returns rows merged."""
    pending = sorted((revisions_dir / "pending").glob("*.csv"))
    total = 0
    for path in pending:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
        append_revisions(revisions_dir, df)
        total += len(df)
        path.unlink()
    return total


@dataclass
class StationWriter:
    """Writes refreshed station CSVs for every client (one code path).

    Before an existing CSV is replaced, the replacement is checked by
    :func:`shrink_reason` and diffed into the revision log.  A refused
    replacement keeps the old CSV and leaves the feature's dates and
    probe verdict untouched.

    ``merge=True`` is for rolling-window sources: the response is laid
    over the archived CSV (:func:`merge_with_archive`) instead of
    replacing it, so a short response never reads as lost history, and
    changed values inside the window still reach the revision log.
    """

    features: list[dict]
    data_dir: Path
    refreshed_at_utc: str
    stats: RefreshStats
    revisions: RevisionLog
    # Codes allowed to shrink this run; "all" allows every station.
    allow_shrink: frozenset[str] = frozenset()

    def store(
        self, feat_idx: int, code: str, client: str, df: pd.DataFrame,
        merge: bool = False,
    ) -> bool:
        if df.empty:
            self.stats.skipped_empty += 1
            return False
        df = _as_numeric(df)
        csv_path = station_csv_path(self.data_dir, code)
        old = read_station_csv(csv_path)
        if old is not None and merge:
            df = merge_with_archive(old, df)
        if old is not None:
            reason = shrink_reason(old, df)
            if reason and not (
                "all" in self.allow_shrink or code in self.allow_shrink
            ):
                self.stats.refused_shrink += 1
                logging.warning(
                    "[%s] %s: refusing to replace CSV — %s. Kept the "
                    "archived file; rerun with --allow-shrink %s if the "
                    "upstream change is real.",
                    client, code, reason, code,
                )
                self.revisions.add_refusal(code, client, old, df)
                return False
            self.revisions.add(code, client, diff_revisions(old, df))
        write_csv_atomically(csv_path, df)
        earliest, latest = compute_record_dates(df)
        feature = self.features[feat_idx]
        update_geojson_dates(
            feature, earliest, latest, self.refreshed_at_utc
        )
        verify_feature_from_csv(feature, df)
        self.stats.updated_csvs += 1
        self.stats.by_client[client] = self.stats.by_client.get(client, 0) + 1
        return True


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
    writer: StationWriter,
) -> None:
    """Refresh AWDB station CSVs.

    ``stations`` is a list of (feature_index, code, triplet) tuples.
    """
    stats = writer.stats
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
            if writer.store(feat_idx, code, "awdb", df):
                updated += 1

        print(f"updated {updated}")


# ── CDEC refresh ──────────────────────────────────────────────────────────────

def refresh_cdec(
    stations: list[tuple[int, str]],
    writer: StationWriter,
) -> None:
    """Refresh CDEC station CSVs.

    ``stations`` is a list of (feature_index, station_id) tuples.
    """
    stats = writer.stats
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
            if writer.store(feat_idx, sid, "cdec", df):
                updated += 1

        print(f"updated {updated}")


# ── DataBC refresh ────────────────────────────────────────────────────────────

def refresh_databc(
    stations: list[tuple[int, str]],
    writer: StationWriter,
) -> None:
    """Refresh DataBC ASWS station CSVs.

    ``stations`` is a list of (feature_index, location_id) tuples.
    SWE is fetched from SWDaily.csv (mm → cm).
    Snow depth is fetched from SD.csv / SD_Archive.csv (cm).
    """
    stats = writer.stats
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
        if writer.store(feat_idx, lid_str, "databc", df):
            updated += 1

    print(f"  [DataBC] updated {updated} station CSVs")


# ── NVE refresh ───────────────────────────────────────────────────────────────

def refresh_nve(
    stations: list[tuple[int, str]],
    writer: StationWriter,
    resample_probe: bool = False,
) -> None:
    """Refresh NVE station CSVs.

    ``stations`` is a list of (feature_index, station_id) tuples.
    NVE fetches per station+parameter; all IDs are passed in one call.
    """
    stats = writer.stats
    features = writer.features
    if not stations:
        return

    # The NVE client reads its key through easysnowdata's `nve` auth provider
    # now, which raises CredentialError rather than letting HydAPI answer 401
    # to every request. Resolve it once here: a missing key is one counted,
    # skipped network, not a traceback per batch.
    try:
        auth.ensure("nve")
    except CredentialError as exc:
        stats.failed_batches += 1
        print(f"  [NVE] SKIPPED {len(stations)} stations — {exc}")
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
        if writer.store(feat_idx, sid, "nve", df):
            updated += 1

    print(f"  [NVE] updated {updated} station CSVs")


# ── Yukon refresh ─────────────────────────────────────────────────────────────

def refresh_yukon(
    stations: list[tuple[int, str]],
    writer: StationWriter,
) -> None:
    """Refresh Yukon (AquaCache) station CSVs.

    ``stations`` is a list of (feature_index, location_code) tuples.
    SWE is fetched from /timeseries/measurementsDaily (mm → cm) and snow
    depth from the same endpoint (already cm).  Daily values are means over
    the station's local day.

    Only stations with a continuous series reach this function — the 92
    manual snow courses are periodic and are excluded from
    all_snow_stations.geojson upstream.
    """
    stats = writer.stats
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
        if writer.store(feat_idx, sid, "yukon", df):
            updated += 1

    print(f"  [Yukon] updated {updated} station CSVs")


# ── Hydro-Québec refresh ──────────────────────────────────────────────────────

# The feed's declared timezone; the client aggregates hourly values over
# this local day.
HYDROQUEBEC_TZ = "America/Toronto"
# Ask for more than the ~10-day window so nothing it still holds is missed.
HYDROQUEBEC_LOOKBACK_DAYS = 15
HYDROQUEBEC_BATCH = 50


def refresh_hydroquebec(
    stations: list[tuple[int, str]],
    writer: StationWriter,
    today: date | None = None,
) -> None:
    """Refresh Hydro-Québec station CSVs by merging the rolling window in.

    ``stations`` is a list of (feature_index, identifiant) tuples.  The
    feed holds only ~10 days of hourly data, so each night's daily values
    are merged into the archived CSV (``merge=True``) — older history
    exists nowhere else.  Daily values are the client's aggregation: the
    last valid SWE / depth of the America/Toronto local day.  The current
    local day is left out because it is incomplete; storing it would log a
    "revision" for every station every night.

    A run that returns no records at all is reported as a GitHub Actions
    error annotation, since every missed night is lost for good.
    """
    stats = writer.stats
    if not stations:
        return

    local_today = today or datetime.now(ZoneInfo(HYDROQUEBEC_TZ)).date()
    end = local_today - timedelta(days=1)
    begin = end - timedelta(days=HYDROQUEBEC_LOOKBACK_DAYS)

    client = HydroQuebecClient()
    station_ids = [sid for _, sid in stations]
    idx_by_id = {sid: idx for idx, sid in stations}

    n = len(station_ids)
    print(
        f"  [Hydro-Québec] Fetching daily SWE + snow depth for {n} stations "
        f"({begin} – {end})..."
    )
    records: list[dict] = []
    for start in range(0, n, HYDROQUEBEC_BATCH):
        batch = station_ids[start:start + HYDROQUEBEC_BATCH]
        try:
            records.extend(client.get_data(
                station_ids=batch,
                variables=["swe", "snwd"],
                interval="daily",
                begin_date=begin.isoformat(),
                end_date=end.isoformat(),
            ))
        except HydroQuebecError as exc:
            stats.failed_batches += 1
            print(
                f"  [Hydro-Québec] batch "
                f"{start // HYDROQUEBEC_BATCH + 1} FAILED ({exc})"
            )
    print(f"  [Hydro-Québec] ok ({len(records)} records)")
    if not records:
        msg = (
            f"Hydro-Québec returned no records for {n} stations — the feed "
            "keeps only ~10 days, so a missed night is lost for good"
        )
        logging.error(msg)
        print(f"::error title=Hydro-Québec refresh::{msg}")

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
        if writer.store(feat_idx, sid, "hydroquebec", df, merge=True):
            updated += 1

    print(f"  [Hydro-Québec] updated {updated} station CSVs")


# ── Alberta refresh ───────────────────────────────────────────────────────────

def refresh_alberta(
    stations: list[tuple[int, str]],
    writer: StationWriter,
) -> None:
    """Refresh Alberta River Basins station CSVs (full record, replaced).

    ``stations`` is a list of (feature_index, station_number) tuples —
    only AEPA's and the other non-mirror operators' automated stations;
    BC Snow Survey and SNOTEL mirrors never reach here.  Daily values are
    the source's own ``A.DayMean`` aggregate over the MST day (the client
    computes a mean of the native samples only where that aggregate is
    missing).  The client downloads one series per station × parameter
    and logs and skips a failed download, so a station can come back
    partial; the shrink guard keeps its archived record in that case.
    """
    stats = writer.stats
    if not stations:
        return

    client = AlbertaClient()
    n = len(stations)
    print(f"  [Alberta] Fetching daily SWE + snow depth for {n} stations...")
    updated = 0
    n_records = 0
    for feat_idx, sid in stations:
        # One call per station: a station is two downloads at most.
        try:
            records = client.get_data(
                station_ids=[sid],
                variables=["swe", "snwd"],
                interval="daily",
                end_date=date.today().isoformat(),
            )
        except AlbertaError as exc:   # the catalog query failed
            stats.failed_batches += 1
            print(f"  [Alberta] {sid} FAILED ({exc})")
            continue
        n_records += len(records)
        if records:
            stats.fetched += 1
        df = _station_records_to_df(records)
        if writer.store(feat_idx, sid, "alberta", df):
            updated += 1
    print(f"  [Alberta] ok ({n_records} records)")
    print(f"  [Alberta] updated {updated} station CSVs")


# ── SLF refresh ───────────────────────────────────────────────────────────────

def refresh_slf(
    stations: list[tuple[int, str]],
    writer: StationWriter,
) -> None:
    """Refresh SLF IMIS and study-plot CSVs (full record, replaced).

    ``stations`` is a list of (feature_index, code) tuples; each feature's
    ``slf_dataset`` and ``slf_station_id`` say which SLF file to read (the
    code carries a dataset prefix that SLF does not know).  SLF's file
    service holds one CSV per station with its whole record, rebuilt
    daily, so this is one download per station.  Depth only: neither
    network measures SWE, and its new-snow series are not archived.
    """
    stats = writer.stats
    features = writer.features
    if not stations:
        return

    by_dataset: dict[str, list[tuple[int, str, str]]] = {}
    for feat_idx, code in stations:
        p = features[feat_idx]["properties"]
        by_dataset.setdefault(str(p.get("slf_dataset")), []).append(
            (feat_idx, code, str(p.get("slf_station_id") or ""))
        )

    for dataset, members in by_dataset.items():
        client = SLFClient(dataset=dataset)
        print(
            f"  [SLF] Fetching daily snow depth for {len(members)} "
            f"{dataset} stations..."
        )
        updated = 0
        for feat_idx, code, sid in members:
            try:
                records = client.get_data(
                    station_ids=[sid], variables=["snwd"], interval="daily",
                )
            except SLFError as exc:
                stats.failed_batches += 1
                print(f"  [SLF] {code} FAILED ({exc})")
                continue
            if records:
                stats.fetched += 1
            df = _station_records_to_df(records)
            if writer.store(feat_idx, code, "slf", df):
                updated += 1
        print(f"  [SLF] {dataset}: updated {updated} station CSVs")


# ── SNIH refresh ──────────────────────────────────────────────────────────────

# Spike filter for SNIH raw pillow telemetry (pipeline layer: the client
# passes raw values through, clamped only to 0–15 m).  Raw values show
# isolated spikes (Laguna Diamante reached 14,370 mm, Las Leñas 4,007 mm)
# next to plausible 10 mm steps and real storms.  A spike is a value that
# jumps away from BOTH neighbours by more than the bound, in the same
# direction, while staying a single reading (raw, every 4–6 h) or a single
# day (after daily aggregation).  A level shift that persists is not a
# spike and is kept.  50 mm between consecutive 4–6-hourly readings and
# 150 mm between consecutive days are above what snowfall or melt does
# at these pillows.
SNIH_RAW_SPIKE_CM = 5.0
SNIH_DAILY_SPIKE_CM = 15.0
# Telemetry starts in 2009; nothing raw exists before.
SNIH_RAW_START = date(2009, 1, 1)


def _drop_isolated_spikes(values: list[float | None], bound: float) -> list[float | None]:
    """None out values that leave both neighbours by > *bound*, same way."""
    out = list(values)
    for i in range(1, len(values) - 1):
        v, a, b = values[i], values[i - 1], values[i + 1]
        if v is None or a is None or b is None:
            continue
        up = v - a > bound and v - b > bound
        down = a - v > bound and b - v > bound
        if up or down:
            out[i] = None
    return out


def snih_daily_from_raw(records: list[dict]) -> dict[str, float]:
    """Daily SWE (cm) from SNIH raw sub-daily pillow records.

    Records are the client's ``interval="sub_daily"`` output for one
    station (``date`` is the Argentina local day).  Isolated raw spikes are
    dropped (:data:`SNIH_RAW_SPIKE_CM`), each day becomes the **median** of
    its remaining readings (robust to a spike the first pass missed), and
    isolated daily spikes are dropped (:data:`SNIH_DAILY_SPIKE_CM`).
    Returns ``{date: value}``; dropped days are absent.
    """
    rows = sorted(
        (r for r in records if r.get("value") is not None),
        key=lambda r: str(r.get("datetime") or r.get("date")),
    )
    kept = _drop_isolated_spikes([float(r["value"]) for r in rows], SNIH_RAW_SPIKE_CM)
    by_day: dict[str, list[float]] = {}
    for r, v in zip(rows, kept):
        if v is not None:
            by_day.setdefault(str(r["date"])[:10], []).append(v)
    days = sorted(by_day)
    medians = [float(np.median(by_day[d])) for d in days]
    daily = _drop_isolated_spikes(medians, SNIH_DAILY_SPIKE_CM)
    return {d: round(v, 3) for d, v in zip(days, daily) if v is not None}


def snih_series(
    validated: list[dict],
    raw: list[dict],
    validated_through: str | None,
) -> pd.DataFrame:
    """One station's daily CSV frame: validated record, then raw-derived.

    Validated values are used for every date up to *validated_through*;
    raw telemetry only fills dates after it, so the two are never mixed
    within the validated period (DESIGN.md §4).
    """
    rows = [r for r in validated if r.get("type") == "swe"]
    cut = validated_through or ""
    for d, v in snih_daily_from_raw(raw).items():
        if d > cut:
            rows.append({"date": d, "type": "swe", "value": v})
    return _station_records_to_df(rows)


def refresh_snih(
    stations: list[tuple[int, str]],
    writer: StationWriter,
    today: date | None = None,
) -> None:
    """Refresh SNIH snow-pillow CSVs (full record, replaced).

    ``stations`` is a list of (feature_index, code) tuples; each feature's
    ``snih_station_id`` and ``snih_validated_through`` come from stage 1.
    Per station: the validated daily SWE record, then raw telemetry from
    the day after it (or the pillow's association date) to yesterday in
    Argentina time — today is incomplete.  See :func:`snih_series`.
    """
    stats = writer.stats
    features = writer.features
    if not stations:
        return

    end = (today or datetime.now(timezone(timedelta(hours=-3))).date()) - timedelta(days=1)
    client = SNIHClient()
    print(f"  [SNIH] Fetching pillow SWE for {len(stations)} stations...")
    updated = 0
    for feat_idx, code in stations:
        p = features[feat_idx]["properties"]
        sid = str(p.get("snih_station_id") or code.removeprefix("SNIH-"))
        through = p.get("snih_validated_through")
        try:
            validated = client.get_data(
                station_ids=[sid], variables=["SWE_PILLOW"], interval="daily",
                end_date=end.isoformat(),
            )
            got = [r["date"] for r in validated if r.get("value") is not None]
            # The record itself is the authority on where it ends.
            through = max(got) if got else through
            start = SNIH_RAW_START
            if through:
                start = max(start, date.fromisoformat(through) + timedelta(days=1))
            raw = []
            if start <= end:
                raw = client.get_data(
                    station_ids=[sid], variables=["SWE_PILLOW"],
                    interval="sub_daily", begin_date=start.isoformat(),
                    end_date=end.isoformat(),
                )
        except SNIHError as exc:
            stats.failed_batches += 1
            print(f"  [SNIH] {code} FAILED ({exc})")
            continue
        if validated or raw:
            stats.fetched += 1
        df = snih_series(validated, raw, through)
        if writer.store(feat_idx, code, "snih", df):
            updated += 1
    print(f"  [SNIH] updated {updated} station CSVs")


# ── Finalize from existing CSVs ───────────────────────────────────────────────

def finalize_from_csvs(
    geojson_path: Path,
    data_dir: Path,
    archive_path: Path,
    revisions_dir: Path = DEFAULT_REVISIONS_DIR,
) -> None:
    """Update GeoJSON date metadata from existing CSVs, then build archive.

    Used by the ``--finalize-only`` mode after parallel per-network jobs have
    written their CSVs as GitHub Actions artifacts and those artifacts have
    been downloaded into ``data_dir``.  Their revision-log rows arrive
    as ``revisions_dir/pending/*.csv`` and are merged into the month files
    here (DESIGN.md §6.6).
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

    merged = merge_pending_revisions(revisions_dir)
    print(f"Revision log: {merged:,} rows merged into {revisions_dir}")

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
        choices=[
            "awdb", "cdec", "databc", "nve", "yukon", "hydroquebec",
            "alberta", "slf", "snih",
        ],
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
    ap.add_argument(
        "--revisions-dir",
        default=str(DEFAULT_REVISIONS_DIR),
        help="Revision log directory (DESIGN.md §6.6)",
    )
    ap.add_argument(
        "--allow-shrink",
        action="append",
        default=[],
        metavar="CODES",
        help=(
            "Station codes (comma-separated, repeatable) whose CSV may be "
            "replaced even though the new record is much shorter, or "
            "`all`. Without it such replacements are refused and logged "
            "(shrink guard, DESIGN.md §6.3)."
        ),
    )
    ap.add_argument(
        "--archive-only",
        action="store_true",
        help=(
            "Only bundle the existing CSVs into --archive. Used by the "
            "snapshot release workflow; the bundle is not committed."
        ),
    )
    args = ap.parse_args()

    geojson_path = Path(args.geojson)
    data_dir = Path(args.data_dir)
    archive_path = Path(args.archive)
    revisions_dir = Path(args.revisions_dir)
    allow_shrink = frozenset(
        c.strip() for arg in args.allow_shrink for c in arg.split(",")
        if c.strip()
    )

    # ── Archive-only mode ─────────────────────────────────────────────────────
    if args.archive_only:
        n = build_archive(data_dir, archive_path)
        print(f"Bundled {n} station CSVs into {archive_path}")
        return

    # ── Finalize-only mode ────────────────────────────────────────────────────
    if args.finalize_only:
        finalize_from_csvs(
            geojson_path, data_dir, archive_path, revisions_dir
        )
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
    hydroquebec_stations: list[tuple[int, str]] = []
    alberta_stations: list[tuple[int, str]] = []
    slf_stations: list[tuple[int, str]] = []
    snih_stations: list[tuple[int, str]] = []

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
        elif client_name == "hydroquebec":
            hydroquebec_stations.append((idx, code))
        elif client_name == "alberta":
            alberta_stations.append((idx, code))
        elif client_name == "slf":
            slf_stations.append((idx, code))
        elif client_name == "snih":
            snih_stations.append((idx, code))
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
    run_hydroquebec = (
        (network in (None, "hydroquebec")) and bool(hydroquebec_stations)
    )
    run_alberta = (network in (None, "alberta")) and bool(alberta_stations)
    run_slf = (network in (None, "slf")) and bool(slf_stations)
    run_snih = (network in (None, "snih")) and bool(snih_stations)

    total = sum([
        len(awdb_stations) if run_awdb else 0,
        len(cdec_stations) if run_cdec else 0,
        len(databc_stations) if run_databc else 0,
        len(nve_stations) if run_nve else 0,
        len(yukon_stations) if run_yukon else 0,
        len(hydroquebec_stations) if run_hydroquebec else 0,
        len(alberta_stations) if run_alberta else 0,
        len(slf_stations) if run_slf else 0,
        len(snih_stations) if run_snih else 0,
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
        f"Hydro-Québec: "
        f"{len(hydroquebec_stations) if run_hydroquebec else 'skip'}  "
        f"Alberta: {len(alberta_stations) if run_alberta else 'skip'}  "
        f"SLF: {len(slf_stations) if run_slf else 'skip'}  "
        f"SNIH: {len(snih_stations) if run_snih else 'skip'}  "
        f"(total: {total:,})"
    )
    print("=" * 70)

    refreshed_at_utc = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    writer = StationWriter(
        features=features,
        data_dir=data_dir,
        refreshed_at_utc=refreshed_at_utc,
        stats=stats,
        revisions=RevisionLog(detected_utc=refreshed_at_utc),
        allow_shrink=allow_shrink,
    )

    if run_awdb:
        refresh_awdb(awdb_stations, writer)
    if run_cdec:
        refresh_cdec(cdec_stations, writer)
    if run_databc:
        refresh_databc(databc_stations, writer)
    if run_nve:
        refresh_nve(
            nve_stations, writer, resample_probe=args.resample_probe,
        )
    if run_yukon:
        refresh_yukon(yukon_stations, writer)
    if run_hydroquebec:
        refresh_hydroquebec(hydroquebec_stations, writer)
    if run_alberta:
        refresh_alberta(alberta_stations, writer)
    if run_slf:
        refresh_slf(slf_stations, writer)
    if run_snih:
        refresh_snih(snih_stations, writer)
    if network == "hydroquebec" and not run_hydroquebec:
        # The inventory has no Hydro-Québec stations to refresh — e.g.
        # stage 1 could not list them.  Say so where the run summary shows it.
        msg = "no Hydro-Québec stations in the inventory to refresh"
        logging.error(msg)
        print(f"::error title=Hydro-Québec refresh::{msg}")

    # In per-network mode: CSVs written, skip GeoJSON update and archive.
    # The downstream finalize job handles those via --finalize-only.
    logged = writer.revisions.write(revisions_dir, network=network)
    if network:
        print(f"\n[{network.upper()}] CSVs written to {data_dir}")
        print(f"  updated: {stats.updated_csvs:,}  "
              f"failed: {stats.failed_batches:,}  "
              f"refused (shrink): {stats.refused_shrink:,}  "
              f"revision rows: {logged:,}")
        for line in writer.revisions.summary():
            print(line)
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
    print(f"Refused (shrink guard)   : {stats.refused_shrink:,}")
    for line in writer.revisions.summary():
        print(line)
    print(f"Archive members          : {archived_count:,}")
    print(f"Archive written          : {archive_path}")
    print(f"GeoJSON updated          : {geojson_path}")


if __name__ == "__main__":
    main()
