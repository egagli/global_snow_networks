# -*- coding: utf-8 -*-
"""
01_get_stations_geojson.py
==========================
Fetch all AWDB stations that have DAILY WTEQ or SNWD observations,
collect full metadata, and write a GeoJSON FeatureCollection.

Networks queried: SNTL, SNTLT, MSNT, SCAN, COOP

Outputs
-------
  snow_stations.geojson

Usage
-----
  python 01_get_stations_geojson.py
  # or via pixi:
  pixi run fetch-stations
"""

import json
from collections import Counter
from datetime import date
from pathlib import Path

from clients import AWDBClient

SNOW_NETWORKS = ["SNTL", "SNTLT", "MSNT", "SCAN", "COOP"]
SNOW_ELEMENTS = ["WTEQ", "SNWD"]
OUTPUT_DIR    = Path(".")
GEOJSON_OUT   = OUTPUT_DIR / "snow_stations.geojson"
API_BATCH     = 150  # triplets per metadata request


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
    code = triplet_to_code(station.get("stationTriplet"))
    page_url = station_page_url(station)
    image_url = station_image_url(station)

    elements_summary = [
        {
            "elementCode":  el.get("elementCode"),
            "elementName":  el.get("elementName", ""),
            "durationName": el.get("durationName", ""),
            "unitCode":     "cm" if el.get("elementCode") in {"WTEQ", "SNWD"} else el.get("originalUnitCode", ""),
            "beginDate":    (el.get("beginDate") or "")[:10],
            "endDate":      (el.get("endDate")   or "")[:10],
        }
        for el in station.get("stationElements", [])
    ]

    daily_vars = sorted({
        str(el.get("elementCode") or "").strip()
        for el in station.get("stationElements", [])
        if str(el.get("durationName") or "").upper() == "DAILY"
        and str(el.get("elementCode") or "").strip()
    })
    hourly_vars = sorted({
        str(el.get("elementCode") or "").strip()
        for el in station.get("stationElements", [])
        if str(el.get("durationName") or "").upper() == "HOURLY"
        and str(el.get("elementCode") or "").strip()
    })
    all_vars = sorted({
        str(el.get("elementCode") or "").strip()
        for el in station.get("stationElements", [])
        if str(el.get("elementCode") or "").strip()
    })

    props = {
        "stationTriplet": code,
        "code":           code,
        "stationId":      station.get("stationId"),
        "networkCode":    station.get("networkCode"),
        "name":           station.get("name"),
        "state":          station.get("stateCode"),
        "county":         station.get("countyName"),
        "huc":            station.get("huc"),
        "latitude":       lat,
        "longitude":      lon,
        "elevation_m":    ft_to_m(station.get("elevation")),
        "beginDate":      (station.get("beginDate") or "")[:10],
        "endDate":        (station.get("endDate")   or "")[:10],
        "isActive":       not station.get("endDate"),
        "snowElements":   elements_summary,
        "elementCodes":   all_vars,
        "variables_daily": ", ".join(daily_vars),
        "variables_hourly": ", ".join(hourly_vars),
        "station_url":    page_url,
        "station_image_url": image_url,
        "metadata_fetched_at": date.today().isoformat(),
    }
    props = {k: v for k, v in props.items() if v is not None and v != ""}

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


def main():
    client = AWDBClient()

    print("=" * 60)
    print("Step 1 — Fetching station lists")
    print("=" * 60)
    all_stations = client.get_stations(networks=SNOW_NETWORKS, active_only=False)
    print(f"  Raw stations: {len(all_stations):,}")
    all_triplets = [s["stationTriplet"] for s in all_stations]

    print("\nStep 2 — Fetching full metadata (element filter: WTEQ, SNWD)")

    # Batch manually so we can show progress
    snow_metadata = []
    batch_size = 150
    batches = [all_triplets[i:i+batch_size] for i in range(0, len(all_triplets), batch_size)]
    for i, batch in enumerate(batches):
        print(f"  Batch {i+1}/{len(batches)} ({len(batch)} triplets)…", end=" ", flush=True)
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
    print("  By network:", dict(Counter(s["networkCode"] for s in snow_metadata)))

    print("\nStep 3 — Fetching full station metadata for variable inventory")
    eligible_triplets = [s["stationTriplet"] for s in snow_metadata]
    full_metadata = []
    full_batch_size = 10
    full_batches = [
        eligible_triplets[i:i+full_batch_size]
        for i in range(0, len(eligible_triplets), full_batch_size)
    ]
    for i, batch in enumerate(full_batches):
        print(f"  Full meta batch {i+1}/{len(full_batches)} ({len(batch)} triplets)…", end=" ", flush=True)
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

    print("\nStep 4 — Building GeoJSON")
    features = [station_to_feature(s) for s in full_metadata]

    geojson = {
        "type": "FeatureCollection",
        "metadata": {
            "generated": date.today().isoformat(),
            "source":    "USDA NRCS AWDB REST API v1",
            "networks":  SNOW_NETWORKS,
            "elements":  SNOW_ELEMENTS,
            "durations": ["DAILY"],
            "total":     len(features),
        },
        "features": features,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GEOJSON_OUT, "w") as f:
        json.dump(geojson, f, indent=2)

    print(f"  Written: {GEOJSON_OUT}  ({len(features):,} features)")
    print("\n✓  Done. Run 02_init_zarr.py next.")


if __name__ == "__main__":
    main()
