# -*- coding: utf-8 -*-
"""
create_all_stations_geojson.py
==============================
Build station GeoJSON inventories from all configured clients.

Three things are written per run:

1. **Per-client GeoJSONs** (one per client, written to the client folder).
   These contain ALL stations available from that client with ALL available
   metadata.  They are NOT filtered to daily-only snow stations.
     - ``clients/awdb/awdb_stations.geojson``
     - ``clients/cdec/cdec_stations.geojson``
     - ``clients/databc/databc_stations.geojson``

2. **``all_daily_snow_stations.geojson``** (repo root) — the merged, daily-only
   inventory used by the data pipeline and live map.  Includes stations
   from all clients that have at least one **daily** SWE or snow depth
   observation.

Duplicate stations (same physical site accessible via multiple clients) are
intentional and expected.  Each entry carries a ``client`` field so
consumers can filter or de-duplicate by source.  The ``code`` field is the
native station identifier for each client — it does not embed the client
name.

Notes field
-----------
SNOTEL (SNTL/SNTLT) stations in AWDB receive a ``notes`` value describing
the status of the NRCS air temperature bias correction programme.  This is
fetched from the live NRCS JSON endpoint at runtime.

Operator field
--------------
Each feature includes an ``Operator`` field populated from the best
available source metadata.  For AWDB stations, the operator is inferred
from the network code.  For CDEC, it comes from the staSearch HTML.  For
DataBC ASWS stations, it comes from the WFS ``OPERATOR`` field.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import requests

from clients.awdb import AWDBClient
from clients.awdb.awdb_client import (
    VARIABLES as AWDB_VARIABLES,
    _AWDB_DURATION_TO_INTERVAL,
)
from clients.cdec import CDECClient
from clients.cdec.cdec_client import SENSORS as CDEC_SENSORS
from clients.databc import DataBCClient
from clients.databc.databc_client import VARIABLES as DATABC_VARIABLES
from clients.nve import NVEClient
from clients.yukon import YukonClient
from clients.yukon.yukon_client import VARIABLES as YUKON_VARIABLES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Output paths
ALL_STATIONS_OUT = REPO_ROOT / "all_daily_snow_stations.geojson"
AWDB_GEOJSON_OUT = REPO_ROOT / "clients" / "awdb" / "awdb_stations.geojson"
CDEC_GEOJSON_OUT = REPO_ROOT / "clients" / "cdec" / "cdec_stations.geojson"
DATABC_GEOJSON_OUT = (
    REPO_ROOT / "clients" / "databc" / "databc_stations.geojson"
)
NVE_GEOJSON_OUT = REPO_ROOT / "clients" / "nve" / "nve_stations.geojson"
YUKON_GEOJSON_OUT = (
    REPO_ROOT / "clients" / "yukon" / "yukon_stations.geojson"
)

# AWDB networks queried for the all-stations GeoJSON.  SNOW (manual snow
# courses, ~2,700) and MPRC (aerial markers, ~260) carry WTEQ/SNWD at
# semi-monthly/monthly cadence — periodic sites that belong in the
# per-client inventory even though they never qualify as daily.
AWDB_NETWORKS = ["SNTL", "SNTLT", "MSNT", "SCAN", "COOP", "SNOW", "MPRC"]
SNOW_ELEMENTS = ["WTEQ", "SNWD"]

# Batching parameters for AWDB
API_BATCH = 150

# NRCS air temp bias correction JSON
BIAS_CORRECTION_URL = (
    "https://www.wcc.nrcs.usda.gov/ftpref/support/"
    "air_temp_bias/nrcs_air_temp_unbias.json"
)

# Networks that receive bias correction notes
BIAS_NETWORKS = {"SNTL", "SNTLT"}

# Operator lookup by AWDB network code.  SNOW and MPRC are deliberately
# absent: those networks mix NRCS-run and partner-run sites, and the
# operator is only recorded when certain (DESIGN.md §5) — unknown
# operators stay null rather than guessed.
AWDB_NETWORK_OPERATOR: dict[str, str] = {
    "SNTL": "USDA NRCS",
    "SNTLT": "USDA NRCS",
    "MSNT": "USDA NRCS",
    "SCAN": "USDA NRCS/ARS",
    "COOP": "NOAA NWS",
}


# Intervals that qualify a variable for inclusion in all_daily_snow_stations
_DAILY_INTERVALS = {"daily", "sub_daily", "hourly"}


def _has_daily_type(data_variables: list[dict], type_str: str) -> bool:
    """Return True if any entry in data_variables has the given type and a
    qualifying daily interval."""
    return any(
        dv.get("type") == type_str
        and dv.get("interval", "").lower() in _DAILY_INTERVALS
        for dv in data_variables
    )


def _awdb_data_variables(station: dict) -> list[dict]:
    """Build the data_variables list for an AWDB station from stationElements."""
    seen: set[tuple] = set()
    dvars: list[dict] = []
    for el in station.get("stationElements", []):
        code = str(el.get("elementCode") or "").strip()
        if not code:
            continue
        dur_name = str(el.get("durationName") or "DAILY").upper()
        interval = _AWDB_DURATION_TO_INTERVAL.get(dur_name)
        if interval is None:
            # Unknown durations must not leak source vocabulary into the
            # inventory (DESIGN.md §3.3) — 5,772 'calendar_year' entries
            # once did exactly that.
            logger.warning(
                "AWDB station %s element %s has unmapped duration %r — "
                "skipping data_variables entry",
                station.get("stationTriplet"), code, dur_name,
            )
            continue
        key = (code, interval)
        if key in seen:
            continue
        seen.add(key)
        var_info = AWDB_VARIABLES.get(code, {})
        units = (
            var_info.get("output_units")
            or el.get("originalUnitCode", "")
        )
        dvars.append({
            "name": code,
            "type": var_info.get("type", "other"),
            "interval": interval,
            "units": units,
            "description": var_info.get("description", ""),
            "notes": var_info.get("notes", ""),
        })
    return dvars


def _cdec_data_variables(station: dict) -> list[dict]:
    """Build the data_variables list for a CDEC station from its sensor list.

    CDEC's station reports advertise snow sensors (3/18/82) on manual snow
    courses even though those sites have no continuous record, so sensor
    presence alone must not imply a daily interval.  Snow pillows (and
    other non-course stations) are daily *candidates*; manual courses
    without a pillow get ``periodic``.  The data fetch is the final
    authority on what is actually daily (DESIGN.md §4).
    """
    course_only = bool(
        station.get("is_snow_course") and not station.get("is_snow_pillow")
    )
    interval = "periodic" if course_only else "daily"
    dvars: list[dict] = []
    for sensor in station.get("sensors", []):
        if isinstance(sensor, dict):
            sensor = sensor.get("sensor_num")
        try:
            snum = int(sensor)
        except (TypeError, ValueError):
            continue
        sinfo = CDEC_SENSORS.get(snum, {})
        if not sinfo:
            continue
        dvars.append({
            "name": sinfo.get("short_name", str(snum)),
            "type": sinfo.get("type", "other"),
            "interval": interval,
            "units": "cm",
            "description": sinfo.get("description", ""),
            "notes": sinfo.get("notes", ""),
        })
    # Snow courses with no sensors listed still have periodic manual SWE
    if not dvars and station.get("is_snow_course"):
        dvars.append({
            "name": "SWE (manual)",
            "type": "swe",
            "interval": "periodic",
            "units": "cm",
            "description": "Manually measured snow water equivalent.",
            "notes": "Snow course — periodic survey only.",
        })
    return dvars


def _databc_data_variables(station: dict) -> list[dict]:
    """Build the data_variables list for a DataBC station."""
    station_type = station.get("station_type", "ASWS")
    dvars: list[dict] = []
    for key, vinfo in DATABC_VARIABLES.items():
        source = vinfo.get("source", "")
        # Assign interval based on source and station type
        if "ASWS" in source and station_type == "ASWS":
            if "daily" in source.lower() or "SWDaily" in source:
                interval = "daily"
            elif "hourly" in source.lower() or (
                "SW.csv" in source and "SWDaily" not in source
            ):
                interval = "hourly"
            else:
                interval = "daily"
            # Variables with no archive (current season only) — still daily
        elif "MSS" in source and station_type == "MSS":
            interval = "periodic"
        else:
            continue
        # Convert swe_mm units note: returned as cm by get_data()
        units = "cm" if key == "swe_mm" else vinfo.get("units", "")
        dvars.append({
            "name": key,
            "type": vinfo.get("type", "other"),
            "interval": interval,
            "units": units,
            "description": vinfo.get("description", ""),
            "notes": vinfo.get("notes", ""),
        })
    return dvars


# ── Air temperature bias correction ──────────────────────────────────────────

def fetch_bias_table() -> dict[str, dict]:
    """
    Fetch the NRCS SNOTEL air temperature bias correction table.

    Returns
    -------
    dict
        Keyed by station triplet (e.g. ``"303:CO:SNTL"``).  Each value is
        the full record with ``status`` (``"Complete"`` or ``"Biased"``),
        ``beginDate``, and ``endDate``.
    """
    try:
        resp = requests.get(BIAS_CORRECTION_URL, timeout=30)
        resp.raise_for_status()
        records = resp.json()
        return {r["stationTriplet"]: r for r in records}
    except Exception as exc:
        logger.warning(
            "Could not fetch air temp bias table: %s — notes will be empty",
            exc,
        )
        return {}


def bias_note(triplet: str | None, bias_table: dict) -> str:
    """Return a human-readable bias correction note for a SNOTEL triplet."""
    if not triplet or not bias_table:
        return ""
    entry = bias_table.get(str(triplet))
    if not entry:
        return ""
    status = entry.get("status", "")
    if status == "Complete":
        begin = (entry.get("beginDate") or "")[:10]
        end_raw = (entry.get("endDate") or "")[:10]
        end_str = "ongoing" if end_raw.startswith("2100") else end_raw
        return (
            f"NRCS air temperature bias correction applied: "
            f"{begin} to {end_str}"
        )
    if status == "Biased":
        return "NRCS air temperature bias correction not yet applied"
    return ""


# ── Unit conversions ──────────────────────────────────────────────────────────

def ft_to_m(feet: float | int | None) -> float | None:
    if feet is None:
        return None
    return round(float(feet) * 0.3048, 1)


def triplet_to_code(triplet: str | None) -> str:
    if not triplet:
        return ""
    return str(triplet).replace(":", "_")


def _awdb_is_active(end_date: str | None) -> bool:
    """AWDB marks active stations with a far-future endDate sentinel
    (2100-01-01) rather than a null endDate, so ``not endDate`` is wrong
    for every AWDB station.  A station is active while its period of
    record extends to today or beyond."""
    if not end_date:
        return True
    return str(end_date)[:10] >= date.today().isoformat()


# ── GeoJSON helpers ───────────────────────────────────────────────────────────

def make_feature(
    lon: float | None,
    lat: float | None,
    props: dict[str, Any],
) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {k: v for k, v in props.items() if v is not None},
    }


def drop_invalid_coordinates(features: list[dict]) -> list[dict]:
    """Drop merged-inventory features with unusable coordinates.

    Catches null-island placeholders such as CDEC's ``TST``
    ("SNOW SURVEYS TEST STATION" at 0, 0) and features with missing
    coordinates.  Per-client GeoJSONs keep every station; this filter
    applies only to the merged daily inventory that drives the map and
    data pipeline.
    """
    kept: list[dict] = []
    for f in features:
        p = f.get("properties", {})
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None or (lat == 0 and lon == 0):
            logger.warning(
                "Dropping %s station %s (%s) from merged inventory: "
                "invalid coordinates lat=%r lon=%r",
                p.get("client"), p.get("code"), p.get("name"), lat, lon,
            )
            continue
        kept.append(f)
    return kept


def keep_previous_if_empty(
    client_name: str,
    geojson_path: Path,
    all_feats: list[dict],
    daily_feats: list[dict],
) -> tuple[list[dict], list[dict], bool]:
    """Fall back to the last saved inventory when a client fetch is empty.

    A source outage (e.g. BC OpenMaps WFS timing out from GitHub runners,
    2026-07-03) must never overwrite a good station inventory with an
    empty one, nor silently drop the client from the merged daily file.
    Reuses the previously committed per-client GeoJSON and derives the
    daily subset from its dailySWE/dailySnowDepth properties.

    Returns ``(all_features, daily_features, fresh)``.  ``fresh`` is False
    when the previous inventory was reused — the caller should then skip
    rewriting the per-client file so it keeps its original metadata.
    """
    if all_feats:
        return all_feats, daily_feats, True
    try:
        with geojson_path.open(encoding="utf-8") as fp:
            previous = json.load(fp).get("features") or []
    except (OSError, json.JSONDecodeError) as exc:
        previous = []
        logger.error("[%s] Could not read previous inventory %s: %s",
                     client_name, geojson_path, exc)
    if not previous:
        logger.error(
            "[%s] Fetch returned 0 stations and there is no previous "
            "inventory to fall back on — client will be missing from "
            "the merged GeoJSON", client_name,
        )
        return all_feats, daily_feats, False
    daily = [
        f for f in previous
        if f.get("properties", {}).get("dailySWE")
        or f.get("properties", {}).get("dailySnowDepth")
    ]
    logger.error(
        "[%s] Fetch returned 0 stations — KEEPING PREVIOUS inventory "
        "(%d stations, %d daily) from %s",
        client_name, len(previous), len(daily), geojson_path.name,
    )
    return previous, daily, False


def write_geojson(
    path: Path,
    features: list[dict],
    metadata: dict,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fc = {
        "type": "FeatureCollection",
        "metadata": metadata,
        "features": features,
    }
    # Atomic: these are multi-MB tracked files — an interrupt mid-write
    # must not leave a truncated inventory behind.
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2)
    tmp_path.replace(path)
    logger.info("  Written %s (%d features)", path, len(features))


_RECORD_DATE_FIELDS = (
    "earliest_record_date",
    "latest_record_date",
    "csv_refreshed_at_utc",
)


def carry_forward_record_dates(
    previous_path: Path, features: list[dict]
) -> int:
    """Preserve CSV-derived record dates across inventory rebuilds.

    Stage 2 (get_all_stations_data) stamps ``earliest_record_date`` /
    ``latest_record_date`` / ``csv_refreshed_at_utc`` onto the merged
    inventory from actual CSV content.  This script rebuilds the inventory
    from source metadata every run and would otherwise wipe those fields
    each morning.  Fields are carried forward per (client, code); the
    next data refresh overwrites them with fresher values.
    """
    try:
        with previous_path.open(encoding="utf-8") as fp:
            previous = json.load(fp).get("features") or []
    except (OSError, json.JSONDecodeError):
        return 0
    prev_by_key: dict[tuple, dict] = {}
    for f in previous:
        p = f.get("properties", {})
        vals = {k: p[k] for k in _RECORD_DATE_FIELDS if p.get(k)}
        if vals:
            prev_by_key[(p.get("client"), p.get("code"))] = vals
    applied = 0
    for f in features:
        p = f.get("properties", {})
        vals = prev_by_key.get((p.get("client"), p.get("code")))
        if vals:
            for k, v in vals.items():
                p.setdefault(k, v)
            applied += 1
    return applied


# ── AWDB workflow ─────────────────────────────────────────────────────────────

def awdb_station_url(station: dict) -> str:
    network = str(station.get("networkCode") or "")
    if network not in {"SNTL", "SNTLT"}:
        return ""
    sid = station.get("stationId")
    if not sid:
        return ""
    return f"https://wcc.sc.egov.usda.gov/nwcc/site?sitenum={sid}"


def awdb_image_url(station: dict) -> str:
    network = str(station.get("networkCode") or "")
    if network not in {"SNTL", "SNTLT"}:
        return ""
    sid = str(station.get("stationId") or "").strip()
    if not sid or not sid.isdigit():
        return ""
    return f"https://www.wcc.nrcs.usda.gov/siteimages/{sid}.jpg"


def awdb_station_to_feature(
    station: dict,
    bias_table: dict,
) -> dict:
    """Convert an AWDB station dict to a GeoJSON feature."""
    lon = station.get("longitude")
    lat = station.get("latitude")
    triplet = station.get("stationTriplet")
    code = triplet_to_code(triplet)
    network = str(station.get("networkCode") or "")

    elements_summary = [
        {
            "elementCode": el.get("elementCode"),
            "elementName": el.get("elementName", ""),
            "durationName": el.get("durationName", ""),
            "unitCode": (
                "cm"
                if el.get("elementCode") in {"WTEQ", "SNWD"}
                else el.get("originalUnitCode", "")
            ),
            "beginDate": (el.get("beginDate") or "")[:10],
            "endDate": (el.get("endDate") or "")[:10],
        }
        for el in station.get("stationElements", [])
    ]

    daily_vars = sorted(
        {
            str(el.get("elementCode") or "").strip()
            for el in station.get("stationElements", [])
            if str(el.get("durationName") or "").upper() == "DAILY"
            and str(el.get("elementCode") or "").strip()
        }
    )
    hourly_vars = sorted(
        {
            str(el.get("elementCode") or "").strip()
            for el in station.get("stationElements", [])
            if str(el.get("durationName") or "").upper() == "HOURLY"
            and str(el.get("elementCode") or "").strip()
        }
    )
    all_vars = sorted(
        {
            str(el.get("elementCode") or "").strip()
            for el in station.get("stationElements", [])
            if str(el.get("elementCode") or "").strip()
        }
    )

    # Notes: bias correction for SNOTEL networks
    notes = ""
    if network in BIAS_NETWORKS:
        notes = bias_note(triplet, bias_table)

    props: dict[str, Any] = {
        "code": code,
        "awdb_station_triplet": triplet,
        "stationId": station.get("stationId"),
        "networkCode": network,
        "name": station.get("name"),
        "state": station.get("stateCode"),
        "county": station.get("countyName"),
        "huc": station.get("huc"),
        "latitude": lat,
        "longitude": lon,
        "elevation_m": ft_to_m(station.get("elevation")),
        "beginDate": (station.get("beginDate") or "")[:10],
        "endDate": (station.get("endDate") or "")[:10],
        "isActive": _awdb_is_active(station.get("endDate")),
        "Operator": AWDB_NETWORK_OPERATOR.get(network),
        "client": "awdb",
        "notes": notes,
        "station_url": awdb_station_url(station),
        "station_image_url": awdb_image_url(station),
        "metadata_fetched_at": date.today().isoformat(),
    }
    props["snowElements"] = elements_summary
    props["elementCodes"] = all_vars
    props["variables_daily"] = ", ".join(daily_vars)
    props["variables_hourly"] = ", ".join(hourly_vars)

    data_vars = _awdb_data_variables(station)
    props["data_variables"] = data_vars
    props["dailySWE"] = _has_daily_type(data_vars, "swe")
    props["dailySnowDepth"] = _has_daily_type(data_vars, "snwd")

    return make_feature(lon, lat, props)


def run_awdb_workflow(
    bias_table: dict,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch AWDB stations and return (all_features, daily_features).

    ``all_features``   — ALL AWDB stations with any WTEQ/SNWD element at
                         ANY duration — including periodic snow courses
                         (SNOW) and aerial markers (MPRC) — for
                         clients/awdb/awdb_stations.geojson.
    ``daily_features`` — the subset with daily-or-better WTEQ/SNWD
                         (for all_daily_snow_stations.geojson).
    """
    client = AWDBClient()

    print("=" * 60)
    print("[AWDB] Fetching station list")
    all_stations = client.get_stations(
        networks=AWDB_NETWORKS, active_only=False
    )
    print(f"  Raw stations: {len(all_stations):,}")
    all_triplets = [s["stationTriplet"] for s in all_stations]

    print("[AWDB] Filtering to stations with WTEQ/SNWD at any duration")
    snow_metadata: list[dict] = []
    batches = [
        all_triplets[i: i + API_BATCH]
        for i in range(0, len(all_triplets), API_BATCH)
    ]
    for i, batch in enumerate(batches, 1):
        print(
            f"  Batch {i}/{len(batches)} ({len(batch)} triplets)...",
            end=" ",
            flush=True,
        )
        results = client.get_metadata(
            triplets=batch,
            elements=SNOW_ELEMENTS,
            durations="*",
            active_only=False,
        )
        kept = [s for s in results if s.get("stationElements")]
        snow_metadata.extend(kept)
        print(f"kept {len(kept)}")

    print(
        f"  Stations with WTEQ/SNWD: {len(snow_metadata):,}  "
        f"({dict(Counter(s['networkCode'] for s in snow_metadata))})"
    )

    print("[AWDB] Fetching full metadata for variable inventories")
    eligible = [s["stationTriplet"] for s in snow_metadata]
    print(
        f"  Requesting {len(eligible):,} triplets with adaptive fallback batching..."
    )
    full_meta = client.get_metadata(
        triplets=eligible,
        elements="*",
        durations="*",
        include_forecast_point=True,
        include_reservoir=True,
        active_only=False,
    )
    print(f"  Fetched {len(full_meta):,} full-metadata station records")

    # Per-client GeoJSON: all stations with full metadata
    all_features = [
        awdb_station_to_feature(s, bias_table) for s in full_meta
    ]

    # Merged daily inventory: only daily-or-better WTEQ/SNWD
    daily_features = [
        f for f in all_features
        if f["properties"].get("dailySWE")
        or f["properties"].get("dailySnowDepth")
    ]
    print(
        f"  Daily-or-better stations: {len(daily_features):,} of "
        f"{len(all_features):,}"
    )

    return all_features, daily_features


# ── CDEC workflow ─────────────────────────────────────────────────────────────

def cdec_station_to_feature(station: dict) -> dict:
    """Convert a CDEC station dict to a GeoJSON feature."""
    sid = str(station.get("station_id") or "").strip()
    lat = station.get("latitude")
    lon = station.get("longitude")
    elev_ft = station.get("elevation_ft")

    props: dict[str, Any] = {
        "code": sid,
        "name": station.get("name", ""),
        "latitude": lat,
        "longitude": lon,
        "elevation_m": ft_to_m(elev_ft),
        "elevation_ft": elev_ft,
        "state": "CA",
        "river_basin": station.get("river_basin", ""),
        "county": station.get("county", ""),
        "Operator": station.get("operator", "")
        or station.get("measuring_agency", ""),
        "client": "cdec",
        "networkCode": "CCSS",
        "notes": "",
        "is_snow_course": station.get("is_snow_course", False),
        "is_snow_pillow": station.get("is_snow_pillow", False),
        "has_daily_swe": station.get("has_daily_swe", False),
        "has_daily_snwd": station.get("has_daily_snwd", False),
        "sensors": station.get("sensors", []),
        "station_url": station.get(
            "station_url",
            f"https://cdec.water.ca.gov/dynamicapp/staMeta"
            f"?station_id={sid}",
        ),
        "april1_avg_swe_in": station.get("april1_avg_swe_in"),
        "course_number": station.get("course_number"),
        "measuring_agency": station.get("measuring_agency"),
        "metadata_fetched_at": date.today().isoformat(),
    }

    data_vars = _cdec_data_variables(station)
    props["data_variables"] = data_vars
    props["dailySWE"] = _has_daily_type(data_vars, "swe")
    props["dailySnowDepth"] = _has_daily_type(data_vars, "snwd")
    daily_names = [
        dv["name"] for dv in data_vars
        if dv.get("interval", "").lower() in _DAILY_INTERVALS
    ]
    if daily_names:
        props["variables_daily"] = ", ".join(daily_names)

    return make_feature(lon, lat, props)


def run_cdec_workflow() -> tuple[list[dict], list[dict]]:
    """
    Fetch CDEC snow stations.

    Returns (all_features, daily_features).
    ``all_features``   — all CDEC snow stations (courses + pillows).
    ``daily_features`` — only stations with daily SWE or snow depth.
    """
    client = CDECClient()

    print("=" * 60)
    print("[CDEC] Fetching snow station list (sensors 3, 18, 82)")
    try:
        stations = client.get_stations(sensors=(3, 18, 82))
        print(f"  Total CDEC snow stations: {len(stations):,}")
    except Exception as exc:
        logger.error("CDEC station fetch failed: %s", exc)
        return [], []

    courses = sum(1 for s in stations if s.get("is_snow_course"))
    pillows = sum(1 for s in stations if s.get("is_snow_pillow"))
    daily = sum(
        1
        for s in stations
        if s.get("has_daily_swe") or s.get("has_daily_snwd")
    )
    print(
        f"  Snow courses: {courses}  |  Snow pillows: {pillows}  "
        f"|  With daily data: {daily}"
    )

    all_features = [cdec_station_to_feature(s) for s in stations]
    daily_features = [
        f for f in all_features
        if f["properties"].get("dailySWE") or f["properties"].get("dailySnowDepth")
    ]

    return all_features, daily_features


# ── DataBC workflow ───────────────────────────────────────────────────────────

def databc_station_to_feature(station: dict) -> dict:
    """Convert a DataBC station dict to a GeoJSON feature."""
    loc_id = str(station.get("location_id") or "").strip()
    lat = station.get("latitude")
    lon = station.get("longitude")
    stype = station.get("station_type", "")

    props: dict[str, Any] = {
        "code": loc_id,
        "name": station.get("name", ""),
        "latitude": lat,
        "longitude": lon,
        "elevation_m": station.get("elevation_m"),
        "state": "BC",
        "Operator": station.get("operator", "BC Ministry of Environment"),
        "client": "databc",
        "networkCode": "BCSS",
        "notes": "",
        "station_type": stype,  # "ASWS" or "MSS" — distinguishes automated vs manual
        "status": station.get("status", ""),
        "isActive": str(station.get("status", "")).lower() == "active",
        "station_url": station.get("station_url", ""),
        "metadata_fetched_at": date.today().isoformat(),
    }

    if stype == "ASWS":
        # Prefer AQRT-fetched station image over WFS camera URL
        img = station.get("station_image_url") or station.get("camera_url")
        if img:
            props["station_image_url"] = img

    data_vars = _databc_data_variables(station)
    props["data_variables"] = data_vars
    props["dailySWE"] = _has_daily_type(data_vars, "swe")
    props["dailySnowDepth"] = _has_daily_type(data_vars, "snwd")
    daily_names = [
        dv["name"] for dv in data_vars
        if dv.get("interval", "").lower() in _DAILY_INTERVALS
    ]
    if daily_names:
        props["variables_daily"] = ", ".join(daily_names)

    return make_feature(lon, lat, props)


def run_databc_workflow(
    fetch_images: bool = True,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch DataBC stations.

    Returns (all_features, daily_features).
    ``all_features``   — all DataBC stations (ASWS + MSS).
    ``daily_features`` — only ASWS stations (have daily SWE).

    Parameters
    ----------
    fetch_images : bool
        If True (default), fetch station photo URLs from the AQRT BCMOE
        portal for each ASWS station.  This adds ~2 HTTP requests per
        station (~300 requests total) and may take 2–5 minutes.
        Pass ``--skip-station-images`` on the command line to disable.
    """
    client = DataBCClient()

    print("=" * 60)
    print("[DataBC] Fetching ASWS station locations from WFS")
    try:
        asws = client.get_asws_stations()
        print(f"  ASWS stations: {len(asws):,}")
    except Exception as exc:
        logger.error("DataBC ASWS fetch failed: %s", exc)
        asws = []

    print("[DataBC] Fetching MSS (manual snow survey) locations from WFS")
    try:
        mss = client.get_mss_stations()
        print(f"  MSS sites: {len(mss):,}")
    except Exception as exc:
        logger.error("DataBC MSS fetch failed: %s", exc)
        mss = []

    if fetch_images and asws:
        print(
            f"[DataBC] Fetching station image URLs for {len(asws):,} "
            f"ASWS stations (AQRT BCMOE portal)..."
        )
        found = 0
        for sta in asws:
            lid = sta["location_id"]
            try:
                img_url = client.get_station_image_url(lid)
                if img_url:
                    sta["station_image_url"] = img_url
                    found += 1
            except Exception as exc:
                logger.debug(
                    "Image URL fetch failed for %s: %s", lid, exc
                )
        print(f"  Found images for {found}/{len(asws)} ASWS stations")
    elif not fetch_images:
        print("[DataBC] Skipping station image URL fetch (--skip-station-images)")

    all_stations = asws + mss
    all_features = [databc_station_to_feature(s) for s in all_stations]

    daily_features = [
        f for f in all_features
        if f["properties"].get("dailySWE") or f["properties"].get("dailySnowDepth")
    ]

    return all_features, daily_features


# ── NVE workflow ──────────────────────────────────────────────────────────────

def _nve_data_variables(station: dict) -> list[dict]:
    """Build data_variables for an NVE station from its parameter list.

    ``interval`` is "daily" only when the station's series actually has a
    daily (1440-minute) resolution per HydAPI /Stations seriesList;
    otherwise the parameter exists only at instantaneous/hourly resolution,
    which the NVE client does not aggregate to daily ("non-daily" — must
    NOT match _DAILY_INTERVALS).
    """
    param_ids = station.get("parameters", [])
    daily_ids = station.get("daily_parameters", [])
    dvars: list[dict] = []
    if 2003 in param_ids:  # SWE, "Snøens vannekvivalent" (m, returned as cm)
        dvars.append({
            "name": "swe_m",
            "type": "swe",
            "interval": "daily" if 2003 in daily_ids else "non-daily",
            "units": "cm",
            "description": (
                "Snow water equivalent from automated snow pillow. "
                "Native API unit is metres; returned here in cm (× 100)."
            ),
            "notes": "Parameter ID 2003 (Snøens vannekvivalent). Native units: m.",
        })
    if 2002 in param_ids:  # Snow depth, "Snødybde" (cm)
        dvars.append({
            "name": "snwd_cm",
            "type": "snwd",
            "interval": "daily" if 2002 in daily_ids else "non-daily",
            "units": "cm",
            "description": "Snow depth from automated sensor. Native unit cm.",
            "notes": "Parameter ID 2002 (Snødybde). Native units: cm.",
        })
    return dvars


def nve_station_to_feature(station: dict) -> dict:
    """Convert an NVE station dict to a GeoJSON feature."""
    sid = str(station.get("station_id") or "").strip()
    lat = station.get("latitude")
    lon = station.get("longitude")

    notes = ""
    if station.get("coordinates_overridden"):
        notes = (
            "Coordinates corrected by client: HydAPI reports a position "
            "~60° of longitude west of the station's actual location "
            "(Nepal cooperation station)."
        )

    props: dict[str, Any] = {
        "code": sid,
        "name": station.get("name", ""),
        "latitude": lat,
        "longitude": lon,
        "elevation_m": station.get("elevation_m"),
        "Operator": "NVE",
        "client": "nve",
        "networkCode": "NVE",
        "notes": notes,
        "status": station.get("status", ""),
        "isActive": station.get("status") == "Active",
        "station_url": station.get("station_url", ""),
        "drainage_basin_key": station.get("drainage_basin_key", ""),
        "metadata_fetched_at": date.today().isoformat(),
    }

    data_vars = _nve_data_variables(station)
    props["data_variables"] = data_vars
    props["dailySWE"] = _has_daily_type(data_vars, "swe")
    props["dailySnowDepth"] = _has_daily_type(data_vars, "snwd")
    daily_names = [
        dv["name"] for dv in data_vars
        if dv.get("interval", "").lower() in _DAILY_INTERVALS
    ]
    if daily_names:
        props["variables_daily"] = ", ".join(daily_names)

    return make_feature(lon, lat, props)


def run_nve_workflow() -> tuple[list[dict], list[dict]]:
    """
    Fetch NVE snow stations and return (all_features, daily_features).

    ``all_features``   — all NVE stations with snow parameters (SWE and/or
                         snow depth) for clients/nve/nve_stations.geojson.
    ``daily_features`` — filtered to stations with daily SWE or depth.
    """
    client = NVEClient()

    print("=" * 60)
    print("[NVE] Fetching snow station list (parameters 2003 SWE, 2002 snow depth)")
    try:
        stations = client.get_all_stations()
        print(f"  Total NVE snow stations: {len(stations):,}")
    except Exception as exc:
        logger.error("NVE station fetch failed: %s", exc)
        return [], []

    active = sum(1 for s in stations if s.get("status") == "Active")
    swe_count = sum(1 for s in stations if 2003 in s.get("parameters", []))
    snwd_count = sum(1 for s in stations if 2002 in s.get("parameters", []))
    print(
        f"  Active: {active}  |  With SWE: {swe_count}  "
        f"|  With snow depth: {snwd_count}"
    )

    all_features = [nve_station_to_feature(s) for s in stations]
    daily_features = [
        f for f in all_features
        if f["properties"].get("dailySWE") or f["properties"].get("dailySnowDepth")
    ]
    print(f"  Daily stations: {len(daily_features):,}")

    return all_features, daily_features


# ── Yukon workflow ────────────────────────────────────────────────────────────

def _yukon_data_variables(station: dict) -> list[dict]:
    """
    Build the data_variables list for a Yukon AquaCache station.

    Unlike the other clients this is derived from the live ``/timeseries``
    catalogue rather than a hardcoded per-station table, so the interval
    and units always reflect what the source currently serves.  Snow
    courses have no continuous series — they report SWE and snow depth
    periodically, so those two entries are synthesised.
    """
    dvars: list[dict] = []

    if station.get("station_type") == "SC":
        for key in ("swe_mm", "snwd_cm"):
            vinfo = YUKON_VARIABLES[key]
            dvars.append({
                "name": key,
                "type": vinfo["type"],
                "interval": "periodic",
                "units": vinfo["output_units"],
                "description": vinfo["description"],
                "notes": vinfo["notes"],
            })
        return dvars

    # One entry per continuous series.  A location can hold several series
    # of the same parameter (ECCC daily air temperature exists as minimum,
    # maximum and mean), so entries are keyed on the resolved variable name
    # and de-duplicated by (name, interval).
    seen: set[tuple[str, str]] = set()
    for series in station.get("series") or []:
        key = series["variable"]
        vinfo = YUKON_VARIABLES[key]
        ident = (key, series["interval"])
        if ident in seen:
            continue
        seen.add(ident)
        notes = vinfo["notes"]
        period = f"{series['start_datetime'][:10]} to {series['end_datetime'][:10]}"
        notes = (
            f"{notes} AquaCache timeseries {series['timeseries_id']}, "
            f"aggregation '{series['aggregation']}', recording rate "
            f"'{series['recording_rate']}', period of record {period}."
        )
        dvars.append({
            "name": key,
            "type": vinfo["type"],
            "interval": series["interval"],
            "units": vinfo["output_units"],
            "description": vinfo["description"],
            "notes": notes,
        })
    return dvars


def yukon_station_to_feature(station: dict) -> dict:
    """Convert a Yukon AquaCache station dict to a GeoJSON feature."""
    code = station["station_id"]
    lat = station.get("latitude")
    lon = station.get("longitude")
    stype = station.get("station_type", "")

    notes = station.get("note", "")
    if stype == "SC" and not station.get("has_survey_metadata"):
        extra = (
            "Listed in /locations but absent from /snow-survey/metadata; "
            "no survey rows are published under this code."
        )
        notes = f"{notes} {extra}".strip()

    props: dict[str, Any] = {
        "code": code,
        "name": station.get("name", ""),
        "latitude": lat,
        "longitude": lon,
        "elevation_m": station.get("elevation_m"),
        # Not blanket "YT": the Yukon Snow Survey also runs courses in BC
        # and Alaska (e.g. "Atlin (B.C.)", "Boundary (Alaska)").
        "state": station.get("state", ""),
        "Operator": station.get("operator", ""),
        "client": "yukon",
        "networkCode": station.get("network_code", "YSS"),
        "notes": notes,
        # "SC" = manual snow course, "AWS" = automated snow-weather station
        # (snow-pillow SWE), "ECCC" = mirrored ECCC climate station.
        "station_type": stype,
        "network": station.get("network", ""),
        "status": station.get("status", ""),
        "isActive": station.get("status") == "Active",
        "station_url": station.get("station_url", ""),
        "dataset_url": station.get("dataset_url", ""),
        "metadata_fetched_at": date.today().isoformat(),
    }

    if station.get("sub_basin"):
        props["sub_basin"] = station["sub_basin"]
    if station.get("first_survey"):
        props["first_survey"] = station["first_survey"]
    if station.get("last_survey"):
        props["last_survey"] = station["last_survey"]

    data_vars = _yukon_data_variables(station)
    props["data_variables"] = data_vars
    props["dailySWE"] = _has_daily_type(data_vars, "swe")
    props["dailySnowDepth"] = _has_daily_type(data_vars, "snwd")
    # A variable can appear at two daily-class intervals (ECCC serves air
    # temperature and precipitation both hourly and as a daily aggregate),
    # so de-duplicate while preserving first-seen order.
    daily_names = list(dict.fromkeys(
        dv["name"] for dv in data_vars
        if dv.get("interval", "").lower() in _DAILY_INTERVALS
    ))
    if daily_names:
        props["variables_daily"] = ", ".join(daily_names)

    return make_feature(lon, lat, props)


def run_yukon_workflow() -> tuple[list[dict], list[dict]]:
    """
    Fetch Yukon AquaCache stations and return (all_features, daily_features).

    ``all_features``   — every snow station: manual snow courses, automated
                         snow-weather stations, and the ECCC climate
                         stations mirrored into AquaCache.
    ``daily_features`` — only stations with a continuous SWE or snow-depth
                         series.  Snow courses are periodic (Feb 1 / Mar 1 /
                         Apr 1 / May 1 / May 15 targets) and are therefore
                         excluded, matching how DataBC MSS sites are handled.
    """
    client = YukonClient()

    print("=" * 60)
    print("[Yukon] Fetching snow courses and continuous snow series")
    try:
        stations = client.get_all_stations()
        print(f"  Total Yukon snow stations: {len(stations):,}")
    except Exception as exc:
        logger.error("Yukon station fetch failed: %s", exc)
        return [], []

    counts = Counter(s.get("station_type", "") for s in stations)
    active = sum(1 for s in stations if s.get("status") == "Active")
    print(
        f"  Snow courses: {counts.get('SC', 0)}  |  "
        f"Automated snow-weather: {counts.get('AWS', 0)}  |  "
        f"ECCC climate: {counts.get('ECCC', 0)}  |  Active: {active}"
    )

    all_features = [yukon_station_to_feature(s) for s in stations]
    daily_features = [
        f for f in all_features
        if f["properties"].get("dailySWE") or f["properties"].get("dailySnowDepth")
    ]
    print(f"  Daily stations: {len(daily_features):,}")

    return all_features, daily_features


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Create station GeoJSON inventories from all configured clients."
        )
    )
    ap.add_argument(
        "--output",
        default=str(ALL_STATIONS_OUT),
        help="Path for the merged all-stations GeoJSON (default: all_daily_snow_stations.geojson)",
    )
    ap.add_argument(
        "--skip-awdb",
        action="store_true",
        help="Skip AWDB client (useful for testing CDEC/DataBC only)",
    )
    ap.add_argument(
        "--skip-cdec",
        action="store_true",
        help="Skip CDEC client",
    )
    ap.add_argument(
        "--skip-databc",
        action="store_true",
        help="Skip DataBC client",
    )
    ap.add_argument(
        "--skip-station-images",
        action="store_true",
        help=(
            "Skip fetching ASWS station photo URLs from the AQRT BCMOE portal. "
            "Saves ~2-5 minutes but omits station_image_url for BC Snow Survey stations."
        ),
    )
    ap.add_argument(
        "--skip-nve",
        action="store_true",
        help="Skip NVE client",
    )
    ap.add_argument(
        "--skip-yukon",
        action="store_true",
        help="Skip Yukon (AquaCache) client",
    )
    args = ap.parse_args()

    today = date.today().isoformat()
    all_daily_features: list[dict] = []

    # ── Fetch bias correction table (used by AWDB) ────────────────────────────
    print("Fetching NRCS air temp bias correction table...")
    bias_table = fetch_bias_table()
    print(
        f"  {len(bias_table):,} stations in bias table "
        f"({sum(1 for v in bias_table.values() if v.get('status') == 'Complete')} Complete, "
        f"{sum(1 for v in bias_table.values() if v.get('status') == 'Biased')} Biased)"
    )

    # ── AWDB ──────────────────────────────────────────────────────────────────
    if not args.skip_awdb:
        awdb_all: list[dict] = []
        awdb_daily: list[dict] = []
        try:
            awdb_all, awdb_daily = run_awdb_workflow(bias_table)
        except Exception as exc:
            logger.warning("[AWDB] Workflow failed: %s", exc)
        awdb_all, awdb_daily, fresh = keep_previous_if_empty(
            "AWDB", AWDB_GEOJSON_OUT, awdb_all, awdb_daily
        )
        if fresh:
            write_geojson(
                AWDB_GEOJSON_OUT,
                awdb_all,
                {
                    "generated": today,
                    "source": "USDA NRCS AWDB REST API v1",
                    "client": "awdb",
                    "networks": AWDB_NETWORKS,
                    "description": (
                        "All AWDB stations with WTEQ and/or SNWD at any "
                        "duration — including periodic snow courses "
                        "(SNOW) and aerial markers (MPRC). Includes full "
                        "element inventory. Only stations with daily-or-"
                        "better snow data appear in "
                        "all_daily_snow_stations.geojson."
                    ),
                    "total": len(awdb_all),
                },
            )
        all_daily_features.extend(awdb_daily)
        print(
            f"[AWDB] {len(awdb_daily):,} daily stations added to merged GeoJSON"
        )

    # ── CDEC ──────────────────────────────────────────────────────────────────
    if not args.skip_cdec:
        cdec_all: list[dict] = []
        cdec_daily: list[dict] = []
        try:
            cdec_all, cdec_daily = run_cdec_workflow()
        except Exception as exc:
            logger.warning("[CDEC] Workflow failed: %s", exc)
        cdec_all, cdec_daily, fresh = keep_previous_if_empty(
            "CDEC", CDEC_GEOJSON_OUT, cdec_all, cdec_daily
        )
        if fresh:
            write_geojson(
                CDEC_GEOJSON_OUT,
                cdec_all,
                {
                    "generated": today,
                    "source": "CDEC — California Data Exchange Center (CA DWR)",
                    "client": "cdec",
                    "description": (
                        "All CDEC stations with snow sensors (3, 18, 82), "
                        "including manual snow courses (periodic) and "
                        "automated snow pillows (daily). "
                        "Only stations with daily SWE or depth appear in "
                        "all_daily_snow_stations.geojson."
                    ),
                    "total": len(cdec_all),
                },
            )
        all_daily_features.extend(cdec_daily)
        print(
            f"[CDEC] {len(cdec_daily):,} daily stations added to merged GeoJSON"
        )

    # ── DataBC ────────────────────────────────────────────────────────────────
    if not args.skip_databc:
        databc_all: list[dict] = []
        databc_daily: list[dict] = []
        try:
            databc_all, databc_daily = run_databc_workflow(
                fetch_images=not args.skip_station_images,
            )
        except Exception as exc:
            logger.warning("[DataBC] Workflow failed: %s", exc)
        databc_all, databc_daily, fresh = keep_previous_if_empty(
            "DataBC", DATABC_GEOJSON_OUT, databc_all, databc_daily
        )
        if fresh:
            write_geojson(
                DATABC_GEOJSON_OUT,
                databc_all,
                {
                    "generated": today,
                    "source": (
                        "BC Data Catalogue — BC Ministry of Environment "
                        "(BC OpenMaps WFS)"
                    ),
                    "client": "databc",
                    "description": (
                        "All BC snow survey stations: ASWS (automated, daily SWE) "
                        "and MSS (manual snow courses, periodic). "
                        "Only ASWS stations appear in all_daily_snow_stations.geojson."
                    ),
                    "total": len(databc_all),
                },
            )
        all_daily_features.extend(databc_daily)
        print(
            f"[DataBC] {len(databc_daily):,} daily stations added to merged GeoJSON"
        )

    # ── NVE ───────────────────────────────────────────────────────────────────
    if not args.skip_nve:
        nve_all: list[dict] = []
        nve_daily: list[dict] = []
        try:
            nve_all, nve_daily = run_nve_workflow()
        except Exception as exc:
            logger.warning("[NVE] Workflow failed: %s", exc)
        nve_all, nve_daily, fresh = keep_previous_if_empty(
            "NVE", NVE_GEOJSON_OUT, nve_all, nve_daily
        )
        if fresh:
            write_geojson(
                NVE_GEOJSON_OUT,
                nve_all,
                {
                    "generated": today,
                    "source": "NVE HydAPI v1 — https://hydapi.nve.no/api/v1",
                    "client": "nve",
                    "description": (
                        "All NVE (Norwegian Water Resources and Energy Directorate) "
                        "snow monitoring stations with SWE (parameter 2003) and/or "
                        "snow depth (parameter 2002). Daily automated measurements."
                    ),
                    "total": len(nve_all),
                },
            )
        all_daily_features.extend(nve_daily)
        print(
            f"[NVE] {len(nve_daily):,} daily stations added to merged GeoJSON"
        )

    # ── Yukon ─────────────────────────────────────────────────────────────────
    if not args.skip_yukon:
        yukon_all: list[dict] = []
        yukon_daily: list[dict] = []
        try:
            yukon_all, yukon_daily = run_yukon_workflow()
        except Exception as exc:
            logger.warning("[Yukon] Workflow failed: %s", exc)
        yukon_all, yukon_daily, fresh = keep_previous_if_empty(
            "Yukon", YUKON_GEOJSON_OUT, yukon_all, yukon_daily
        )
        if fresh:
            write_geojson(
                YUKON_GEOJSON_OUT,
                yukon_all,
                {
                    "generated": today,
                    "source": (
                        "Yukon Water Data (AquaCache) API v1 — "
                        "https://service.yukon.ca/water-data/api/v1"
                    ),
                    "client": "yukon",
                    "description": (
                        "All Yukon snow monitoring stations: manual snow "
                        "courses (YSS, periodic Feb/Mar/Apr/May surveys), "
                        "automated snow-weather stations with snow-pillow "
                        "SWE (YSS), and ECCC climate stations with daily "
                        "snow depth mirrored into AquaCache (YKEC). "
                        "Only stations with a continuous series appear in "
                        "all_daily_snow_stations.geojson."
                    ),
                    "total": len(yukon_all),
                },
            )
        all_daily_features.extend(yukon_daily)
        print(
            f"[Yukon] {len(yukon_daily):,} daily stations added to merged GeoJSON"
        )

    # ── Write merged all_daily_snow_stations.geojson ────────────────────────────────────
    all_daily_features = drop_invalid_coordinates(all_daily_features)
    carried = carry_forward_record_dates(
        Path(args.output), all_daily_features
    )
    if carried:
        print(f"Carried forward CSV record dates for {carried:,} stations")
    print("=" * 60)
    print(
        f"Writing merged all_daily_snow_stations.geojson "
        f"({len(all_daily_features):,} features)"
    )
    clients_used = sorted(
        {
            f.get("properties", {}).get("client", "")
            for f in all_daily_features
        }
    )
    by_client = Counter(
        f.get("properties", {}).get("client", "")
        for f in all_daily_features
    )
    print(f"  By client: {dict(by_client)}")

    write_geojson(
        Path(args.output),
        all_daily_features,
        {
            "generated": today,
            "clients": clients_used,
            "elements": ["WTEQ/swe", "SNWD/snwd"],
            "durations": ["DAILY"],
            "description": (
                "Merged inventory of all snow stations with daily SWE or "
                "snow depth from AWDB (US), CDEC (California), DataBC (BC, Canada), "
                "NVE (Norway), and Yukon AquaCache (Yukon, Canada). "
                "Stations from multiple clients may represent "
                "the same physical site — use the 'client' field to "
                "distinguish data sources. See per-client GeoJSONs in "
                "clients/*/  for complete metadata including periodic "
                "snow course sites."
            ),
            "total": len(all_daily_features),
            "by_client": dict(by_client),
        },
    )
    print("\nDone.")


if __name__ == "__main__":
    main()
