# -*- coding: utf-8 -*-
"""
create_all_stations_geojson.py
==============================
Create a full station GeoJSON inventory using the configured client.

Current supported client: AWDB

This script intentionally does not fetch long time-series records. It only
builds station metadata and variable inventories. Daily record dates are added
or refreshed by get_all_stations_data.py after CSV updates succeed.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from clients import AWDBClient

REPO_ROOT = Path(__file__).resolve().parent.parent

SNOW_NETWORKS = ["SNTL", "SNTLT", "MSNT", "SCAN", "COOP"]
SNOW_ELEMENTS = ["WTEQ", "SNWD"]

DEFAULT_GEOJSON_OUT = REPO_ROOT / "snow_stations.geojson"
API_BATCH = 150
FULL_META_BATCH = 10


def get_client(client_name: str) -> Any:
    if client_name.lower() == "awdb":
        return AWDBClient()
    raise ValueError(f"Unsupported client: {client_name}")


def ft_to_m(feet: float | int | None) -> float | None:
    if feet is None:
        return None
    return round(float(feet) * 0.3048, 1)


def triplet_to_code(triplet: str | None) -> str:
    if not triplet:
        return ""
    return str(triplet).replace(":", "_")


def station_page_url(station: dict) -> str:
    network = str(station.get("networkCode") or "")
    if network not in {"SNTL", "SNTLT"}:
        return ""
    station_id = station.get("stationId")
    if not station_id:
        return ""
    return f"https://wcc.sc.egov.usda.gov/nwcc/site?sitenum={station_id}"


def station_image_url(station: dict) -> str:
    network = str(station.get("networkCode") or "")
    if network not in {"SNTL", "SNTLT"}:
        return ""
    station_id = str(station.get("stationId") or "").strip()
    if not station_id or not station_id.isdigit():
        return ""
    return f"https://www.wcc.nrcs.usda.gov/siteimages/{station_id}.jpg"


def station_to_feature(station: dict) -> dict:
    lon = station.get("longitude")
    lat = station.get("latitude")
    awdb_triplet = station.get("stationTriplet")
    code = triplet_to_code(awdb_triplet)

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

    props = {
        "stationTriplet": code,
        "awdb_station_triplet": awdb_triplet,
        "code": code,
        "stationId": station.get("stationId"),
        "networkCode": station.get("networkCode"),
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
        "snowElements": elements_summary,
        "elementCodes": all_vars,
        "variables_daily": ", ".join(daily_vars),
        "variables_hourly": ", ".join(hourly_vars),
        "station_url": station_page_url(station),
        "station_image_url": station_image_url(station),
        "metadata_fetched_at": date.today().isoformat(),
    }
    props = {k: v for k, v in props.items() if v is not None and v != ""}

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create full station GeoJSON metadata inventory"
    )
    ap.add_argument(
        "--client", default="awdb", help="Data client to use (default: awdb)"
    )
    ap.add_argument(
        "--output",
        default=str(DEFAULT_GEOJSON_OUT),
        help="Output geojson path",
    )
    args = ap.parse_args()

    client = get_client(args.client)
    geojson_out = Path(args.output)

    print("=" * 60)
    print("Step 1 - Fetching station list")
    print("=" * 60)
    all_stations = client.get_stations(
        networks=SNOW_NETWORKS, active_only=False
    )
    print(f"  Raw stations: {len(all_stations):,}")
    all_triplets = [s["stationTriplet"] for s in all_stations]

    print("\nStep 2 - Filtering stations with daily snow observations")
    snow_metadata: list[dict] = []
    batches = [
        all_triplets[i:i + API_BATCH]
        for i in range(0, len(all_triplets), API_BATCH)
    ]
    for i, batch in enumerate(batches, start=1):
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

    print(f"\n  Stations with daily WTEQ/SNWD: {len(snow_metadata):,}")
    print(
        "  By network:",
        dict(Counter(s["networkCode"] for s in snow_metadata)),
    )

    print("\nStep 3 - Fetching full station metadata for variable inventories")
    eligible_triplets = [s["stationTriplet"] for s in snow_metadata]
    full_metadata: list[dict] = []
    full_batches = [
        eligible_triplets[i:i + FULL_META_BATCH]
        for i in range(0, len(eligible_triplets), FULL_META_BATCH)
    ]
    for i, batch in enumerate(full_batches, start=1):
        print(
            (
                f"  Full meta batch {i}/{len(full_batches)} "
                f"({len(batch)} triplets)..."
            ),
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
        full_metadata.extend(results)
        print(f"fetched {len(results)}")

    print("\nStep 4 - Writing GeoJSON")
    features = [station_to_feature(s) for s in full_metadata]
    geojson = {
        "type": "FeatureCollection",
        "metadata": {
            "generated": date.today().isoformat(),
            "source": "USDA NRCS AWDB REST API v1",
            "client": args.client,
            "networks": SNOW_NETWORKS,
            "elements": SNOW_ELEMENTS,
            "durations": ["DAILY"],
            "total": len(features),
        },
        "features": features,
    }

    geojson_out.parent.mkdir(parents=True, exist_ok=True)
    with geojson_out.open("w", encoding="utf-8") as f:
        json.dump(geojson, f, indent=2)

    print(f"  Written: {geojson_out} ({len(features):,} features)")
    print("\nDone.")


if __name__ == "__main__":
    main()
