# -*- coding: utf-8 -*-
"""
create_all_stations_geojson.py
==============================
Build station GeoJSON inventories from all configured clients
(DESIGN.md §6).

Two kinds of output per run:

1. **Per-client GeoJSONs** (one per client, written to the client
   folder) — ALL stations from that source, including periodic snow
   courses, with all available metadata.

2. **``all_snow_stations.geojson``** (repo root) — the combined
   inventory of every station from every client, on the universal
   schema (``UNIVERSAL_FIELDS``).  ``daily_or_better`` marks the
   stations the CSV archive and live map cover; it starts as an
   advertised candidate and the data fetch verifies it
   (``daily_verified``, DESIGN.md §4).

Duplicate stations (same physical site accessible via multiple clients)
are intentional and expected; ``possible_duplicates`` cross-links them
(DESIGN.md §5).  ``code`` is the native station identifier per client.

The ``operator`` field is recorded only when certain — AWDB partner
stations (MSNT/SNOW) get theirs borrowed from a uniquely matching native
twin, never guessed.  SNOTEL stations carry an NRCS air-temperature
bias-correction note fetched live at build time.
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
from clients.nve.nve_client import (
    VARIABLES as NVE_VARIABLES,
    _PARAM_TO_VAR as _NVE_PARAM_TO_VAR,
)
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
ALL_STATIONS_OUT = REPO_ROOT / "all_snow_stations.geojson"
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


# Intervals that qualify a variable as a daily-or-better candidate
_DAILY_INTERVALS = {"daily", "sub_daily", "hourly"}

# ── Universal feature schema (DESIGN.md §6.1) ────────────────────────────────

# Properties present on EVERY feature across all clients — null when the
# source has nothing, never omitted.  Client-specific extras ride along
# and are dropped when None.
UNIVERSAL_FIELDS: tuple[str, ...] = (
    "code",
    "name",
    "latitude",
    "longitude",
    "elevation_m",
    "state",
    "network_code",
    "operator",
    "client",
    "data_provider",
    "status",
    "is_active",
    "begin_date",
    "end_date",
    "earliest_record_date",
    "latest_record_date",
    "station_url",
    "station_image_url",
    "station_camera_url",
    "notes",
    "data_variables",
    "has_daily_swe",
    "has_daily_snwd",
    "daily_or_better",
    "daily_verified",
    "daily_provenance",
    "possible_duplicates",
    "metadata_fetched_at",
)

# Human-readable organization/portal behind each access path (DESIGN.md §5)
DATA_PROVIDERS: dict[str, str] = {
    "awdb": "USDA NRCS AWDB",
    "cdec": "CDEC (CA DWR)",
    "databc": "BC Data Catalogue / BC ENV",
    "nve": "NVE HydAPI",
    "yukon": "Yukon Water Data (AquaCache)",
}

# Canonical operator spellings.  Values of None mean "treat as unknown"
# (DESIGN.md §5: never guess an operator).
OPERATOR_NORMALIZATION: dict[str, str | None] = {
    ".None Specified": None,
    "Natural Resources Conservation Service": "USDA NRCS",
    "BC Ministry of Environment": "BC ENV",
}

# BC snow-station satellite cameras.  The camera pages use opaque
# per-station tokens, so they are tabled here; the authoritative index is
# https://www2.gov.bc.ca/gov/content/environment/air-land-water/water/
# water-science-data/water-data-tools/snow-survey-data/
# snow-station-satellite-cameras (checked 2026-07-28).
_BC_CAMERA_BASE = "https://pvs.nupointsystems.com/api/photo-slider-by-nsn"
BC_CAMERA_URLS: dict[str, str] = {
    "2A31P": _BC_CAMERA_BASE + "?pass=%F3%CB%0A-%F7%0FI6%F2%CDr%AD%F2sq%AD%F2uq%2C%F7u%F15%04%00#images-1",
    "1F04P": _BC_CAMERA_BASE + "?pass=%F3uq%2C%F7%85c%D7%2A0%9D%E5X%0E%00#images-1",
    "1C38P": _BC_CAMERA_BASE + "?pass=%F3%CDJ7%F6uq%AD%F4%CB%8A4%F0%0Bq%AD%F2uq%2C%F7u%094%04%00#images-1",
    "2F08P": _BC_CAMERA_BASE + "?pass=%F3uq%2C%F7%85c%D7%2A0%5D%15j%00%00#images-1",
    "3B26P": _BC_CAMERA_BASE + "?pass=%F3uq%2C%F7%85c%D7%2A0%9D%E5X%01%00#images-1",
    "4A02P": _BC_CAMERA_BASE + "?pass=%F3%CB%8A4%F4sq%AD%F4%0F%894%F0%0Bq%AD%F2uq%2C%F7u%094%00%00#images-1",
    "2D14P": _BC_CAMERA_BASE + "?pass=%F3uq%2C%F7%85c%D7%2A0%5D%95%5E%0E%00#images-1",
    "3A19P": _BC_CAMERA_BASE + "?pass=%F3uq%2C%F7%85c%D7%2A0%1D%12X%0E%00#images-1",
}


def normalize_operator(raw: str | None) -> str | None:
    """Canonicalize an operator string; unknown/junk becomes None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return OPERATOR_NORMALIZATION.get(text, text)


def _has_daily_type(data_variables: list[dict], type_str: str) -> bool:
    """Return True if any entry in data_variables has the given type and a
    qualifying daily interval."""
    return any(
        dv.get("type") == type_str
        and dv.get("interval", "").lower() in _DAILY_INTERVALS
        for dv in data_variables
    )


def _awdb_data_variables(station: dict) -> list[dict]:
    """Build the data_variables list for an AWDB station from stationElements.

    AWDB reports a period of record per element, so ``begin_date`` /
    ``end_date`` are populated here; ``n_obs`` is unknown from metadata
    alone and stays null (DESIGN.md §6.1).
    """
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
        end_raw = (el.get("endDate") or "")[:10]
        dvars.append({
            "name": code,
            "type": var_info.get("type", "other"),
            "interval": interval,
            "units": units,
            "description": var_info.get("description", ""),
            "notes": var_info.get("notes", ""),
            "begin_date": (el.get("beginDate") or "")[:10] or None,
            # 2100-01-01 is AWDB's "still recording" sentinel
            "end_date": (
                None if end_raw.startswith("2100") else end_raw or None
            ),
            "n_obs": None,
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
            "units": sinfo.get("output_units", ""),
            "description": sinfo.get("description", ""),
            "notes": sinfo.get("notes", ""),
            "begin_date": None,
            "end_date": None,
            "n_obs": None,
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
            "begin_date": None,
            "end_date": None,
            "n_obs": None,
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
            "begin_date": None,
            "end_date": None,
            "n_obs": None,
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
    """Build a GeoJSON feature enforcing the universal schema.

    Universal fields (DESIGN.md §6.1) are always present — null when the
    source has nothing.  Client-specific extras are kept only when
    non-null.
    """
    properties: dict[str, Any] = {
        k: props.get(k) for k in UNIVERSAL_FIELDS
    }
    for k, v in props.items():
        if k not in UNIVERSAL_FIELDS and v is not None:
            properties[k] = v
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": properties,
    }


# Old-schema → new-schema property renames, for upgrading features read
# from a previously committed inventory (fallback paths).
_LEGACY_RENAMES: dict[str, str] = {
    "Operator": "operator",
    "networkCode": "network_code",
    "isActive": "is_active",
    "beginDate": "begin_date",
    "endDate": "end_date",
    "dailySWE": "has_daily_swe",
    "dailySnowDepth": "has_daily_snwd",
}


def upgrade_legacy_feature(feature: dict) -> dict:
    """Upgrade a pre-2026-07 feature to the universal schema in place.

    Used when a source outage forces a fallback to a previously
    committed inventory that may predate the schema migration — the
    merged file must never mix vocabularies.
    """
    props = feature.get("properties", {})
    if "network_code" in props and "operator" in props:
        return feature  # already new-schema
    for old, new in _LEGACY_RENAMES.items():
        if old in props and new not in props:
            props[new] = props.pop(old)
        else:
            props.pop(old, None)
    props["operator"] = normalize_operator(props.get("operator"))
    props["data_provider"] = DATA_PROVIDERS.get(props.get("client"))
    if props.get("daily_or_better") is None:
        props["daily_or_better"] = bool(
            props.get("has_daily_swe") or props.get("has_daily_snwd")
        )
    if props.get("daily_provenance") is None:
        props["daily_provenance"] = (
            "native" if props["daily_or_better"] else "none"
        )
    # Old data_variables entries used a pre-enum vocabulary and lacked
    # the period-of-record keys
    legacy_intervals = {"non-daily": "instantaneous",
                        "calendar_year": "annual"}
    for dv in props.get("data_variables") or []:
        iv = str(dv.get("interval", "")).lower()
        dv["interval"] = legacy_intervals.get(iv, iv)
        dv.setdefault("begin_date", None)
        dv.setdefault("end_date", None)
        dv.setdefault("n_obs", None)
    # Pre-migration bloat fields with no place in the new schema
    for stale in ("snowElements", "elementCodes", "variables_daily",
                  "variables_hourly", "elevation_ft", "stationId",
                  "april1_avg_swe_in", "csv_path", "updated_date"):
        props.pop(stale, None)
    lon, lat = props.get("longitude"), props.get("latitude")
    return make_feature(lon, lat, props)


# ── Duplicate annotation (DESIGN.md §5) ──────────────────────────────────────

def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def annotate_possible_duplicates(
    features: list[dict],
    near_m: float = 1000.0,
    name_match_m: float = 5000.0,
) -> int:
    """Cross-link likely duplicate stations across clients.

    The same physical site appearing once per access path is intentional
    (DESIGN.md §5); this makes the links explicit instead of leaving
    consumers to rediscover them.  Two features from *different* clients
    are candidates when they are within ``near_m`` metres, or within
    ``name_match_m`` metres with the same case-folded name.  Each side
    gets a ``possible_duplicates`` list of ``{code, client, distance_m}``.
    """
    # Grid-bucket at ~0.1° so only nearby pairs are compared
    cell = 0.1
    grid: dict[tuple[int, int], list[int]] = {}
    for idx, f in enumerate(features):
        p = f["properties"]
        lat, lon = p.get("latitude"), p.get("longitude")
        if lat is None or lon is None:
            continue
        grid.setdefault((int(lat // cell), int(lon // cell)), []).append(idx)

    links: dict[int, list[dict]] = {}
    seen_pairs: set[tuple[int, int]] = set()
    for (gy, gx), members in grid.items():
        neighborhood: list[int] = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                neighborhood.extend(grid.get((gy + dy, gx + dx), []))
        for i in members:
            pi = features[i]["properties"]
            for j in neighborhood:
                if j <= i:
                    continue
                pj = features[j]["properties"]
                if pi.get("client") == pj.get("client"):
                    continue
                pair = (i, j)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                d = _haversine_m(
                    pi["latitude"], pi["longitude"],
                    pj["latitude"], pj["longitude"],
                )
                same_name = (
                    str(pi.get("name") or "").casefold().strip()
                    == str(pj.get("name") or "").casefold().strip()
                    and str(pi.get("name") or "").strip() != ""
                )
                if d <= near_m or (same_name and d <= name_match_m):
                    links.setdefault(i, []).append({
                        "code": pj.get("code"),
                        "client": pj.get("client"),
                        "distance_m": round(d),
                    })
                    links.setdefault(j, []).append({
                        "code": pi.get("code"),
                        "client": pi.get("client"),
                        "distance_m": round(d),
                    })

    for idx, dups in links.items():
        features[idx]["properties"]["possible_duplicates"] = sorted(
            dups, key=lambda x: x["distance_m"]
        )
    return len(links)


def borrow_operators_from_twins(features: list[dict]) -> int:
    """Fill unknown operators from a uniquely matching native twin.

    AWDB labels partner stations (MSNT/SNOW) with no trustworthy
    operator.  When such a feature has exactly one possible duplicate
    from another client and that twin declares an operator, the twin's
    value is authoritative for the same physical site (DESIGN.md §5 —
    correction only when certain).
    """
    by_key = {
        (f["properties"].get("client"), f["properties"].get("code")): f
        for f in features
    }
    borrowed = 0
    for f in features:
        p = f["properties"]
        if p.get("operator") or p.get("client") != "awdb":
            continue
        dups = p.get("possible_duplicates") or []
        if len(dups) != 1:
            continue
        twin = by_key.get((dups[0]["client"], dups[0]["code"]))
        if twin is None:
            continue
        twin_op = twin["properties"].get("operator")
        if not twin_op:
            continue
        p["operator"] = twin_op
        note = (
            f"Operator taken from {dups[0]['client']} twin "
            f"{dups[0]['code']} ({dups[0]['distance_m']} m away)."
        )
        p["notes"] = f"{p.get('notes') or ''} {note}".strip()
        borrowed += 1
    return borrowed


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
    # The previous file may predate the schema migration — the merged
    # inventory must never mix vocabularies.
    previous = [upgrade_legacy_feature(f) for f in previous]
    daily = [
        f for f in previous
        if f.get("properties", {}).get("has_daily_swe")
        or f.get("properties", {}).get("has_daily_snwd")
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

# Probe-verified fields also carried forward across rebuilds — the data
# fetch is the authority on these (DESIGN.md §4), so a metadata rebuild
# must not reset them to advertised candidates.  ``has_daily_swe`` /
# ``has_daily_snwd`` deliberately stay *advertised* (rebuilt from source
# metadata every run) so a station that newly advertises daily data gets
# probed again even after a failed verification.
_VERIFIED_FIELDS = (
    "daily_or_better",
    "daily_verified",
    "daily_provenance",
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
        prev_by_key[(p.get("client"), p.get("code"))] = p
    applied = 0
    for f in features:
        p = f.get("properties", {})
        prev = prev_by_key.get((p.get("client"), p.get("code")))
        if prev is None:
            continue
        touched = False
        for k in _RECORD_DATE_FIELDS:
            if p.get(k) is None and prev.get(k) is not None:
                p[k] = prev[k]
                touched = True
        # The probe's verdict is authoritative until the next probe
        if prev.get("daily_verified"):
            for k in _VERIFIED_FIELDS:
                if prev.get(k) is not None:
                    p[k] = prev[k]
            touched = True
        if touched:
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


def _daily_candidate_props(data_vars: list[dict]) -> dict[str, Any]:
    """Advertised daily-or-better flags shared by every client's builder.

    These are *candidates* from source metadata; the data fetch verifies
    them (DESIGN.md §4) and stamps ``daily_verified`` accordingly.
    """
    has_swe = _has_daily_type(data_vars, "swe")
    has_snwd = _has_daily_type(data_vars, "snwd")
    return {
        "data_variables": data_vars,
        "has_daily_swe": has_swe,
        "has_daily_snwd": has_snwd,
        "daily_or_better": has_swe or has_snwd,
        "daily_verified": False,
        "daily_provenance": "native" if (has_swe or has_snwd) else "none",
    }


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

    # Notes: bias correction for SNOTEL networks
    notes = ""
    if network in BIAS_NETWORKS:
        notes = bias_note(triplet, bias_table)

    props: dict[str, Any] = {
        "code": code,
        "awdb_station_triplet": triplet,
        "awdb_station_id": station.get("stationId"),
        "network_code": network,
        "name": station.get("name"),
        "state": station.get("stateCode"),
        "county": station.get("countyName"),
        "huc": station.get("huc"),
        "latitude": lat,
        "longitude": lon,
        "elevation_m": ft_to_m(station.get("elevation")),
        "begin_date": (station.get("beginDate") or "")[:10] or None,
        "end_date": (station.get("endDate") or "")[:10] or None,
        "is_active": _awdb_is_active(station.get("endDate")),
        "status": (
            "Active" if _awdb_is_active(station.get("endDate"))
            else "Inactive"
        ),
        "operator": AWDB_NETWORK_OPERATOR.get(network),
        "client": "awdb",
        "data_provider": DATA_PROVIDERS["awdb"],
        "notes": notes or None,
        "station_url": awdb_station_url(station) or None,
        "station_image_url": awdb_image_url(station) or None,
        "station_camera_url": None,
        "metadata_fetched_at": date.today().isoformat(),
    }
    props.update(_daily_candidate_props(_awdb_data_variables(station)))

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

    april1_in = station.get("april1_avg_swe_in")
    props: dict[str, Any] = {
        "code": sid,
        "name": station.get("name", ""),
        "latitude": lat,
        "longitude": lon,
        "elevation_m": ft_to_m(elev_ft),
        "state": "CA",
        "river_basin": station.get("river_basin") or None,
        "county": station.get("county") or None,
        "operator": normalize_operator(
            station.get("operator")
            or station.get("measuring_agency")
        ),
        "client": "cdec",
        "data_provider": DATA_PROVIDERS["cdec"],
        "network_code": "CCSS",
        "notes": None,
        "status": None,
        "is_active": None,
        "begin_date": None,
        "end_date": None,
        "is_snow_course": station.get("is_snow_course", False),
        "is_snow_pillow": station.get("is_snow_pillow", False),
        "sensors": station.get("sensors", []),
        "station_url": station.get(
            "station_url",
            f"https://cdec.water.ca.gov/dynamicapp/staMeta"
            f"?station_id={sid}",
        ),
        "station_image_url": None,
        "station_camera_url": None,
        "april1_avg_swe_cm": (
            round(april1_in * 2.54, 1) if april1_in is not None else None
        ),
        "course_number": station.get("course_number"),
        "measuring_agency": station.get("measuring_agency"),
        "metadata_fetched_at": date.today().isoformat(),
    }
    props.update(_daily_candidate_props(_cdec_data_variables(station)))

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
        "operator": normalize_operator(
            station.get("operator") or "BC ENV"
        ),
        "client": "databc",
        "data_provider": DATA_PROVIDERS["databc"],
        "network_code": "BCSS",
        "notes": None,
        "begin_date": None,
        "end_date": None,
        "station_type": stype,  # "ASWS" or "MSS" — automated vs manual
        "status": station.get("status") or None,
        "is_active": str(station.get("status", "")).lower() == "active",
        "station_url": station.get("station_url") or None,
        "station_image_url": (
            station.get("station_image_url") or station.get("camera_url")
            if stype == "ASWS" else None
        ),
        "station_camera_url": BC_CAMERA_URLS.get(loc_id),
        "metadata_fetched_at": date.today().isoformat(),
    }
    props.update(_daily_candidate_props(_databc_data_variables(station)))

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

    Metadata comes from the NVE client's ``VARIABLES`` registry — the one
    source of truth — rather than literals duplicated here.  ``interval``
    is "daily" only when the station's series actually has a daily
    (1440-minute) resolution per HydAPI /Stations seriesList; otherwise
    the parameter exists only at instantaneous/hourly resolution, which
    is recorded as "instantaneous" (a non-daily interval that keeps the
    station out of the daily inventory until the pipeline can resample).
    """
    param_ids = station.get("parameters", [])
    daily_ids = station.get("daily_parameters", [])
    dvars: list[dict] = []
    for param_id, var_key in _NVE_PARAM_TO_VAR.items():
        if param_id not in param_ids:
            continue
        vinfo = NVE_VARIABLES[var_key]
        dvars.append({
            "name": var_key,
            "type": vinfo["type"],
            "interval": (
                "daily" if param_id in daily_ids else "instantaneous"
            ),
            "units": vinfo["output_units"],
            "description": vinfo["description"],
            "notes": vinfo["notes"],
            "begin_date": None,
            "end_date": None,
            "n_obs": None,
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
        "state": "NO",
        "operator": "NVE",
        "client": "nve",
        "data_provider": DATA_PROVIDERS["nve"],
        "network_code": "NVE",
        "notes": notes or None,
        "begin_date": None,
        "end_date": None,
        "status": station.get("status") or None,
        "is_active": station.get("status") == "Active",
        "station_url": station.get("station_url") or None,
        "station_image_url": None,
        "station_camera_url": None,
        "drainage_basin_key": station.get("drainage_basin_key") or None,
        "metadata_fetched_at": date.today().isoformat(),
    }
    props.update(_daily_candidate_props(_nve_data_variables(station)))

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
                "begin_date": station.get("first_survey") or None,
                "end_date": station.get("last_survey") or None,
                "n_obs": None,
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
        notes = (
            f"{notes} AquaCache timeseries {series['timeseries_id']}, "
            f"aggregation '{series['aggregation']}', recording rate "
            f"'{series['recording_rate']}'."
        )
        dvars.append({
            "name": key,
            "type": vinfo["type"],
            "interval": series["interval"],
            "units": vinfo["output_units"],
            "description": vinfo["description"],
            "notes": notes,
            "begin_date": (series.get("start_datetime") or "")[:10] or None,
            "end_date": (series.get("end_datetime") or "")[:10] or None,
            "n_obs": None,
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
        "state": station.get("state") or None,
        "operator": normalize_operator(station.get("operator")),
        "client": "yukon",
        "data_provider": DATA_PROVIDERS["yukon"],
        "network_code": station.get("network_code", "YSS"),
        "notes": notes or None,
        "begin_date": station.get("first_survey") or None,
        "end_date": None,
        # "SC" = manual snow course, "AWS" = automated snow-weather station
        # (snow-pillow SWE), "ECCC" = mirrored ECCC climate station.
        "station_type": stype,
        "network": station.get("network") or None,
        "status": station.get("status") or None,
        "is_active": station.get("status") == "Active",
        "station_url": station.get("station_url") or None,
        "station_image_url": None,
        "station_camera_url": None,
        "dataset_url": station.get("dataset_url") or None,
        "sub_basin": station.get("sub_basin"),
        "first_survey": station.get("first_survey"),
        "last_survey": station.get("last_survey"),
        "metadata_fetched_at": date.today().isoformat(),
    }
    props.update(_daily_candidate_props(_yukon_data_variables(station)))

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
        help="Path for the combined all-stations GeoJSON (default: all_snow_stations.geojson)",
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
    all_features_merged: list[dict] = []
    daily_count = 0

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
        all_features_merged.extend(awdb_all)
        daily_count += len(awdb_daily)
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
        all_features_merged.extend(cdec_all)
        daily_count += len(cdec_daily)
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
        all_features_merged.extend(databc_all)
        daily_count += len(databc_daily)
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
        all_features_merged.extend(nve_all)
        daily_count += len(nve_daily)
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
        all_features_merged.extend(yukon_all)
        daily_count += len(yukon_daily)
        print(
            f"[Yukon] {len(yukon_daily):,} daily stations added to merged GeoJSON"
        )

    # ── Write merged all_snow_stations.geojson ──────────────────────────────
    all_features_merged = drop_invalid_coordinates(all_features_merged)
    carried = carry_forward_record_dates(
        Path(args.output), all_features_merged
    )
    if carried:
        print(f"Carried forward record/verified fields for {carried:,} stations")

    linked = annotate_possible_duplicates(all_features_merged)
    print(f"Cross-linked possible duplicates on {linked:,} features")
    borrowed = borrow_operators_from_twins(all_features_merged)
    if borrowed:
        print(f"Borrowed operators from native twins for {borrowed:,} stations")

    print("=" * 60)
    print(
        f"Writing merged all_snow_stations.geojson "
        f"({len(all_features_merged):,} features, "
        f"{daily_count:,} daily-or-better candidates)"
    )
    clients_used = sorted(
        {
            f.get("properties", {}).get("client", "")
            for f in all_features_merged
        }
    )
    by_client = Counter(
        f.get("properties", {}).get("client", "")
        for f in all_features_merged
    )
    print(f"  By client: {dict(by_client)}")

    write_geojson(
        Path(args.output),
        all_features_merged,
        {
            "generated": today,
            "clients": clients_used,
            "description": (
                "Combined inventory of ALL known snow point-observation "
                "stations (SWE and/or snow depth) from AWDB (US + partner "
                "networks), CDEC (California), DataBC (BC, Canada), NVE "
                "(Norway), and Yukon AquaCache — including periodic snow "
                "courses and other manual sites. daily_or_better marks "
                "stations with a (probe-verified, see daily_verified) "
                "daily-or-better record; the CSV archive and live map "
                "cover exactly those. The same physical site may appear "
                "once per access path — see possible_duplicates. "
                "Schema: DESIGN.md §6.1."
            ),
            "references": [
                "https://github.com/egagli/global_snow_networks/blob/main/DESIGN.md",
                "https://github.com/egagli/global_snow_networks/blob/main/docs/SOURCES.md",
            ],
            "total": len(all_features_merged),
            "by_client": dict(by_client),
        },
    )
    print("\nDone.")


if __name__ == "__main__":
    main()
