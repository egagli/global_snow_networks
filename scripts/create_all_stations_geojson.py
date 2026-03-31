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

2. **``snow_stations.geojson``** (repo root) — the merged, daily-only
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
from clients.cdec import CDECClient
from clients.databc import DataBCClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Output paths
ALL_STATIONS_OUT = REPO_ROOT / "snow_stations.geojson"
AWDB_GEOJSON_OUT = REPO_ROOT / "clients" / "awdb" / "awdb_stations.geojson"
CDEC_GEOJSON_OUT = REPO_ROOT / "clients" / "cdec" / "cdec_stations.geojson"
DATABC_GEOJSON_OUT = (
    REPO_ROOT / "clients" / "databc" / "databc_stations.geojson"
)

# AWDB networks queried for the all-stations GeoJSON
AWDB_NETWORKS = ["SNTL", "SNTLT", "MSNT", "SCAN", "COOP"]
SNOW_ELEMENTS = ["WTEQ", "SNWD"]

# Batching parameters for AWDB
API_BATCH = 150
FULL_META_BATCH = 10

# NRCS air temp bias correction JSON
BIAS_CORRECTION_URL = (
    "https://www.wcc.nrcs.usda.gov/ftpref/support/"
    "air_temp_bias/nrcs_air_temp_unbias.json"
)

# Networks that receive bias correction notes
BIAS_NETWORKS = {"SNTL", "SNTLT"}

# Operator lookup by AWDB network code
AWDB_NETWORK_OPERATOR: dict[str, str] = {
    "SNTL": "USDA NRCS",
    "SNTLT": "USDA NRCS",
    "MSNT": "USDA NRCS",
    "SCAN": "USDA NRCS/ARS",
    "COOP": "NOAA NWS",
}


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
    with path.open("w", encoding="utf-8") as f:
        json.dump(fc, f, indent=2)
    logger.info("  Written %s (%d features)", path, len(features))


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
    full_metadata: bool = False,
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
        "isActive": not station.get("endDate"),
        "Operator": AWDB_NETWORK_OPERATOR.get(network, "USDA NRCS"),
        "client": "awdb",
        "notes": notes,
        "station_url": awdb_station_url(station),
        "station_image_url": awdb_image_url(station),
        "metadata_fetched_at": date.today().isoformat(),
    }
    if full_metadata:
        props["snowElements"] = elements_summary
        props["elementCodes"] = all_vars
        props["variables_daily"] = ", ".join(daily_vars)
        props["variables_hourly"] = ", ".join(hourly_vars)

    return make_feature(lon, lat, props)


def run_awdb_workflow(
    bias_table: dict,
) -> tuple[list[dict], list[dict]]:
    """
    Fetch AWDB stations and return (all_features, daily_features).

    ``all_features``   — all AWDB stations with any snow element (for
                         clients/awdb/awdb_stations.geojson).
    ``daily_features`` — filtered to daily WTEQ/SNWD (for snow_stations.geojson).
    """
    client = AWDBClient()

    print("=" * 60)
    print("[AWDB] Fetching station list")
    all_stations = client.get_stations(
        networks=AWDB_NETWORKS, active_only=False
    )
    print(f"  Raw stations: {len(all_stations):,}")
    all_triplets = [s["stationTriplet"] for s in all_stations]

    print("[AWDB] Filtering to stations with daily snow obs")
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
            durations=["DAILY"],
            active_only=False,
        )
        kept = [s for s in results if s.get("stationElements")]
        snow_metadata.extend(kept)
        print(f"kept {len(kept)}")

    print(
        f"  Stations with daily WTEQ/SNWD: {len(snow_metadata):,}  "
        f"({dict(Counter(s['networkCode'] for s in snow_metadata))})"
    )

    print("[AWDB] Fetching full metadata for variable inventories")
    eligible = [s["stationTriplet"] for s in snow_metadata]
    full_meta: list[dict] = []
    full_batches = [
        eligible[i: i + FULL_META_BATCH]
        for i in range(0, len(eligible), FULL_META_BATCH)
    ]
    for i, batch in enumerate(full_batches, 1):
        print(
            f"  Full meta batch {i}/{len(full_batches)} "
            f"({len(batch)} triplets)...",
            end=" ",
            flush=True,
        )
        params = {
            "stationTriplets": ",".join(batch),
            "returnStationElements": "true",
            "returnForecastPointMetadata": "true",
            "returnReservoirMetadata": "true",
            "activeOnly": "false",
        }
        results = client._get("stations", params)
        full_meta.extend(results)
        print(f"fetched {len(results)}")

    # Per-client GeoJSON: all stations with full metadata
    all_features = [
        awdb_station_to_feature(s, bias_table, full_metadata=True)
        for s in full_meta
    ]

    # All-stations GeoJSON: same set (already filtered to daily)
    daily_features = all_features

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
        "networkCode": "CDEC",
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
        cdec_station_to_feature(s)
        for s in stations
        if s.get("has_daily_swe") or s.get("has_daily_snwd")
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
        "networkCode": stype,
        "notes": "",
        "station_type": stype,
        "status": station.get("status", ""),
        "isActive": str(station.get("status", "")).lower() == "active",
        "station_url": station.get("station_url", ""),
        "metadata_fetched_at": date.today().isoformat(),
    }

    if stype == "ASWS":
        props["has_daily_swe"] = True
        props["variables_daily"] = "swe_mm"
        camera = station.get("camera_url")
        if camera:
            props["station_image_url"] = camera
    else:
        props["has_daily_swe"] = False
        props["variables_daily"] = ""

    return make_feature(lon, lat, props)


def run_databc_workflow() -> tuple[list[dict], list[dict]]:
    """
    Fetch DataBC stations.

    Returns (all_features, daily_features).
    ``all_features``   — all DataBC stations (ASWS + MSS).
    ``daily_features`` — only ASWS stations (have daily SWE).
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

    all_stations = asws + mss
    all_features = [databc_station_to_feature(s) for s in all_stations]

    # Only ASWS stations have daily SWE for the all-stations GeoJSON
    daily_features = [
        databc_station_to_feature(s)
        for s in asws
        if str(s.get("status", "")).lower() in ("active", "")
        or s.get("status") is None
    ]

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
        help="Path for the merged all-stations GeoJSON (default: snow_stations.geojson)",
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
        try:
            awdb_all, awdb_daily = run_awdb_workflow(bias_table)
            write_geojson(
                AWDB_GEOJSON_OUT,
                awdb_all,
                {
                    "generated": today,
                    "source": "USDA NRCS AWDB REST API v1",
                    "client": "awdb",
                    "networks": AWDB_NETWORKS,
                    "description": (
                        "All AWDB stations with daily WTEQ and/or SNWD. "
                        "Includes full element inventory."
                    ),
                    "total": len(awdb_all),
                },
            )
            all_daily_features.extend(awdb_daily)
            print(
                f"[AWDB] {len(awdb_daily):,} daily stations added to merged GeoJSON"
            )
        except Exception as exc:
            logging.warning("[AWDB] Workflow failed, skipping: %s", exc)

    # ── CDEC ──────────────────────────────────────────────────────────────────
    if not args.skip_cdec:
        try:
            cdec_all, cdec_daily = run_cdec_workflow()
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
                        "snow_stations.geojson."
                    ),
                    "total": len(cdec_all),
                },
            )
            all_daily_features.extend(cdec_daily)
            print(
                f"[CDEC] {len(cdec_daily):,} daily stations added to merged GeoJSON"
            )
        except Exception as exc:
            logging.warning("[CDEC] Workflow failed, skipping: %s", exc)

    # ── DataBC ────────────────────────────────────────────────────────────────
    if not args.skip_databc:
        try:
            databc_all, databc_daily = run_databc_workflow()
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
                        "Only ASWS stations appear in snow_stations.geojson."
                    ),
                    "total": len(databc_all),
                },
            )
            all_daily_features.extend(databc_daily)
            print(
                f"[DataBC] {len(databc_daily):,} daily stations added to merged GeoJSON"
            )
        except Exception as exc:
            logging.warning("[DataBC] Workflow failed, skipping: %s", exc)

    # ── Write merged snow_stations.geojson ────────────────────────────────────
    print("=" * 60)
    print(
        f"Writing merged snow_stations.geojson "
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
                "snow depth. Stations from multiple clients may represent "
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
