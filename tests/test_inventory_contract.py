# -*- coding: utf-8 -*-
"""
Contract test for the committed station inventory (DESIGN.md §9).

Validates invariants of ``all_daily_snow_stations.geojson`` that must
hold on every commit.  Checks that depend on the nightly regeneration
picking up the July 2026 fixes are marked ``xfail(strict=False)`` — they
pass once the workflow has rebuilt the inventory, and will be promoted
to hard assertions in the Phase 3 schema migration.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = REPO_ROOT / "all_daily_snow_stations.geojson"
DATA_DIR = REPO_ROOT / "data" / "stations"

KNOWN_CLIENTS = {"awdb", "cdec", "databc", "nve", "yukon"}

# Interval vocabulary (DESIGN.md §3.3) plus values the pre-migration
# builders still emit; the extras are removed in Phase 2/3.
INTERVAL_ENUM = {
    "periodic", "monthly", "semi_monthly", "daily", "sub_daily",
    "hourly", "sub_hourly", "instantaneous", "annual",
}
LEGACY_INTERVALS = {"non-daily", "calendar_year"}

DATA_VARIABLE_KEYS = {
    "name", "type", "interval", "units", "description", "notes",
}


@pytest.fixture(scope="module")
def inventory():
    with INVENTORY.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def features(inventory):
    feats = inventory.get("features")
    assert feats, "inventory has no features"
    return feats


def test_feature_collection_shape(inventory):
    assert inventory["type"] == "FeatureCollection"
    assert isinstance(inventory.get("metadata"), dict)


def test_every_feature_has_code_and_known_client(features):
    for f in features:
        p = f["properties"]
        assert str(p.get("code") or ""), f"feature without code: {p}"
        assert p.get("client") in KNOWN_CLIENTS, (
            f"{p.get('code')}: unknown client {p.get('client')!r} — "
            "the refresh router cannot handle it"
        )


def test_no_duplicate_client_code_pairs(features):
    seen = set()
    for f in features:
        p = f["properties"]
        key = (p.get("client"), p.get("code"))
        assert key not in seen, f"duplicate feature {key}"
        seen.add(key)


def test_coordinates_valid(features):
    for f in features:
        p = f["properties"]
        lat, lon = p.get("latitude"), p.get("longitude")
        assert lat is not None and lon is not None, p.get("code")
        assert not (lat == 0 and lon == 0), (
            f"{p.get('code')}: null-island coordinates"
        )
        assert -90 <= lat <= 90 and -180 <= lon <= 180, p.get("code")


def test_daily_flags_present_and_at_least_one_true(features):
    for f in features:
        p = f["properties"]
        assert isinstance(p.get("dailySWE"), bool), p.get("code")
        assert isinstance(p.get("dailySnowDepth"), bool), p.get("code")
        assert p["dailySWE"] or p["dailySnowDepth"], (
            f"{p.get('code')}: in the daily inventory without a daily "
            "SWE or snow-depth flag"
        )


def test_data_variables_schema(features):
    for f in features:
        p = f["properties"]
        dvars = p.get("data_variables")
        assert isinstance(dvars, list) and dvars, p.get("code")
        for dv in dvars:
            assert DATA_VARIABLE_KEYS <= set(dv), (
                f"{p.get('code')}: data_variables entry missing keys: {dv}"
            )


def test_interval_vocabulary(features):
    allowed = INTERVAL_ENUM | LEGACY_INTERVALS
    bad = set()
    for f in features:
        for dv in f["properties"].get("data_variables", []):
            iv = str(dv.get("interval", "")).lower()
            if iv not in allowed:
                bad.add(iv)
    assert not bad, f"intervals outside the enum: {sorted(bad)}"


@pytest.mark.xfail(
    strict=False,
    reason="pre-unification inventory still carries metadata-only CDEC "
    "'daily' stations; becomes strict once the nightly rebuild has run "
    "with the phantom-daily fix (Phase 4 probe makes this exact)",
)
def test_every_daily_station_has_a_csv(features):
    missing = [
        f["properties"]["code"]
        for f in features
        if not (DATA_DIR / f"{f['properties']['code']}.csv").exists()
    ]
    assert not missing, (
        f"{len(missing)} inventory stations have no CSV, e.g. "
        f"{missing[:10]}"
    )


@pytest.mark.xfail(
    strict=False,
    reason="isActive fix lands in the inventory at the next nightly "
    "rebuild; the committed file predates it",
)
def test_awdb_is_not_uniformly_inactive(features):
    awdb = [f["properties"] for f in features
            if f["properties"].get("client") == "awdb"]
    active = sum(1 for p in awdb if p.get("isActive"))
    # SNOTEL is overwhelmingly an active network; all-False means the
    # endDate sentinel bug is back.
    assert active > len(awdb) / 2, (
        f"only {active}/{len(awdb)} AWDB stations active — "
        "endDate-sentinel regression?"
    )
