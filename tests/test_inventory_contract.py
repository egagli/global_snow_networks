# -*- coding: utf-8 -*-
"""
Contract test for the committed station inventory (DESIGN.md §9).

Validates invariants of ``all_snow_stations.geojson`` — the combined
inventory of ALL stations (periodic sites included) on the universal
schema — that must hold on every commit.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = REPO_ROOT / "all_snow_stations.geojson"
DATA_DIR = REPO_ROOT / "data" / "stations"

KNOWN_CLIENTS = {"awdb", "cdec", "databc", "nve", "yukon"}

# Interval vocabulary (DESIGN.md §3.3)
INTERVAL_ENUM = {
    "periodic", "monthly", "semi_monthly", "daily", "sub_daily",
    "hourly", "sub_hourly", "instantaneous", "annual",
}

# Universal properties present on every feature (DESIGN.md §6.1)
UNIVERSAL_FIELDS = {
    "code", "name", "latitude", "longitude", "elevation_m", "state",
    "network_code", "operator", "client", "data_provider", "status",
    "is_active", "begin_date", "end_date", "earliest_record_date",
    "latest_record_date", "station_url", "station_image_url",
    "station_camera_url", "notes", "data_variables", "has_daily_swe",
    "has_daily_snwd", "daily_or_better", "daily_verified",
    "daily_provenance", "possible_duplicates", "metadata_fetched_at",
}

DATA_VARIABLE_KEYS = {
    "name", "type", "interval", "units", "description", "notes",
    "begin_date", "end_date", "n_obs",
}

PROVENANCE_ENUM = {"native", "resampled_hourly", "resampled_sub_hourly",
                   "none"}


@pytest.fixture(scope="module")
def inventory():
    if not INVENTORY.exists():
        pytest.skip(
            "all_snow_stations.geojson not generated yet — produced by "
            "`pixi run fetch-stations` (nightly Stage 1)"
        )
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


def test_universal_schema_on_every_feature(features):
    for f in features:
        missing = UNIVERSAL_FIELDS - set(f["properties"])
        assert not missing, (
            f"{f['properties'].get('code')}: missing universal fields "
            f"{sorted(missing)}"
        )


def test_every_feature_has_code_and_known_client(features):
    for f in features:
        p = f["properties"]
        assert str(p.get("code") or ""), f"feature without code: {p}"
        assert p.get("client") in KNOWN_CLIENTS, (
            f"{p.get('code')}: unknown client {p.get('client')!r} — "
            "the refresh router cannot handle it"
        )
        assert p.get("data_provider"), p.get("code")


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


def test_daily_flags_are_booleans(features):
    for f in features:
        p = f["properties"]
        assert isinstance(p.get("has_daily_swe"), bool), p.get("code")
        assert isinstance(p.get("has_daily_snwd"), bool), p.get("code")
        assert isinstance(p.get("daily_or_better"), bool), p.get("code")
        assert p.get("daily_provenance") in PROVENANCE_ENUM, (
            f"{p.get('code')}: daily_provenance "
            f"{p.get('daily_provenance')!r}"
        )


def test_data_variables_schema(features):
    for f in features:
        p = f["properties"]
        dvars = p.get("data_variables")
        assert isinstance(dvars, list) and dvars, p.get("code")
        for dv in dvars:
            assert DATA_VARIABLE_KEYS <= set(dv), (
                f"{p.get('code')}: data_variables entry missing keys: "
                f"{sorted(DATA_VARIABLE_KEYS - set(dv))}"
            )


def test_interval_vocabulary(features):
    bad = set()
    for f in features:
        for dv in f["properties"].get("data_variables", []):
            iv = str(dv.get("interval", "")).lower()
            if iv not in INTERVAL_ENUM:
                bad.add(iv)
    assert not bad, f"intervals outside the enum (DESIGN.md §3.3): {sorted(bad)}"


def test_operator_never_junk(features):
    for f in features:
        op = f["properties"].get("operator")
        assert op is None or (isinstance(op, str) and op.strip()), (
            f"{f['properties'].get('code')}: junk operator {op!r}"
        )
        assert op != ".None Specified", f["properties"].get("code")


def test_possible_duplicates_shape(features):
    """Links are shaped right and only published between daily-or-better
    stations (DESIGN.md §5) — periodic pairs are matched internally for
    operator borrowing but never annotated."""
    daily = {
        (f["properties"].get("client"), f["properties"].get("code"))
        for f in features
        if f["properties"].get("daily_or_better")
    }
    for f in features:
        p = f["properties"]
        dups = p.get("possible_duplicates")
        if dups is None:
            continue
        assert p.get("daily_or_better"), (
            f"{p.get('code')}: periodic station carries duplicate links"
        )
        for d in dups:
            assert {"code", "client", "distance_m"} <= set(d), d
            assert d["client"] in KNOWN_CLIENTS, d
            assert (d["client"], d["code"]) in daily, (
                f"{p.get('code')}: links non-daily twin {d['code']}"
            )


def test_verified_daily_stations_have_csvs(features):
    """Every probe-verified daily station must have its CSV; candidates
    not yet verified are exempt (the probe hasn't run for them)."""
    missing = [
        f["properties"]["code"]
        for f in features
        if f["properties"].get("daily_or_better")
        and f["properties"].get("daily_verified")
        and not (DATA_DIR / f"{f['properties']['code']}.csv").exists()
    ]
    assert not missing, (
        f"{len(missing)} verified daily stations have no CSV, e.g. "
        f"{missing[:10]}"
    )


def test_awdb_is_not_uniformly_inactive(features):
    awdb = [f["properties"] for f in features
            if f["properties"].get("client") == "awdb"]
    if not awdb:
        pytest.skip("no AWDB features")
    active = sum(1 for p in awdb if p.get("is_active"))
    # SNOTEL is overwhelmingly an active network; all-False means the
    # endDate sentinel bug is back.
    assert active > len(awdb) / 3, (
        f"only {active}/{len(awdb)} AWDB stations active — "
        "endDate-sentinel regression?"
    )
