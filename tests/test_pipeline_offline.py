# -*- coding: utf-8 -*-
"""
Offline unit tests for the pipeline scripts — no network access.

The pipeline produces every published artifact yet previously had zero
tests; the phantom-CDEC, isActive, and discarded-date-fields bugs all
shipped invisibly (DESIGN.md §9).
"""

import json

import pandas as pd
import pytest

from scripts.create_all_stations_geojson import (
    _awdb_is_active,
    _cdec_data_variables,
    _has_daily_type,
    annotate_possible_duplicates,
    borrow_operators_from_twins,
    carry_forward_record_dates,
    drop_invalid_coordinates,
    make_feature,
    normalize_operator,
    upgrade_legacy_feature,
)
from scripts.get_all_stations_data import (
    _station_records_to_df,
    check_daily_cadence,
    compute_record_dates,
    report_orphan_csvs,
    resample_records_to_daily,
    verify_feature_from_csv,
    write_csv_atomically,
)


# ── _has_daily_type / inclusion logic ────────────────────────────────────────

def test_has_daily_type_accepts_daily_or_better():
    dvars = [{"type": "swe", "interval": "hourly"}]
    assert _has_daily_type(dvars, "swe")
    dvars = [{"type": "swe", "interval": "periodic"}]
    assert not _has_daily_type(dvars, "swe")
    dvars = [{"type": "snwd", "interval": "daily"}]
    assert not _has_daily_type(dvars, "swe")
    assert _has_daily_type(dvars, "snwd")


# ── CDEC phantom-daily fix ───────────────────────────────────────────────────

def test_cdec_course_without_pillow_is_periodic():
    station = {
        "sensors": [3, 18, 82],
        "is_snow_course": True,
        "is_snow_pillow": False,
    }
    dvars = _cdec_data_variables(station)
    assert dvars, "sensors should still be documented"
    assert all(dv["interval"] == "periodic" for dv in dvars)
    assert not _has_daily_type(dvars, "swe")


def test_cdec_pillow_is_daily_candidate():
    station = {
        "sensors": [3, 18, 82],
        "is_snow_course": True,   # course co-located with a pillow
        "is_snow_pillow": True,
    }
    dvars = _cdec_data_variables(station)
    assert all(dv["interval"] == "daily" for dv in dvars)
    assert _has_daily_type(dvars, "swe")


def test_cdec_course_with_no_sensors_gets_periodic_swe():
    station = {"sensors": [], "is_snow_course": True}
    dvars = _cdec_data_variables(station)
    assert len(dvars) == 1
    assert dvars[0]["type"] == "swe"
    assert dvars[0]["interval"] == "periodic"


# ── AWDB isActive sentinel ───────────────────────────────────────────────────

def test_awdb_is_active_handles_2100_sentinel():
    assert _awdb_is_active("2100-01-01")
    assert _awdb_is_active("2100-01-01 00:00:00")
    assert _awdb_is_active(None)
    assert _awdb_is_active("")
    assert not _awdb_is_active("2014-09-30")


# ── Record-date carry-forward across rebuilds ────────────────────────────────

def test_carry_forward_record_dates(tmp_path):
    previous = {
        "type": "FeatureCollection",
        "features": [
            {"properties": {
                "client": "awdb", "code": "303_CO_SNTL",
                "earliest_record_date": "1979-10-01",
                "latest_record_date": "2026-07-27",
                "csv_refreshed_at_utc": "2026-07-27T08:00:00+00:00",
            }},
        ],
    }
    prev_path = tmp_path / "all.geojson"
    prev_path.write_text(json.dumps(previous))

    rebuilt = [
        make_feature(-105.0, 40.0, {"client": "awdb", "code": "303_CO_SNTL"}),
        make_feature(-120.0, 39.0, {"client": "cdec", "code": "QUA"}),
    ]
    applied = carry_forward_record_dates(prev_path, rebuilt)
    assert applied == 1
    props = rebuilt[0]["properties"]
    assert props["earliest_record_date"] == "1979-10-01"
    assert props["latest_record_date"] == "2026-07-27"
    # Universal schema: the key exists on every feature, null when unknown
    assert rebuilt[1]["properties"]["earliest_record_date"] is None


def test_carry_forward_missing_previous_is_noop(tmp_path):
    features = [make_feature(0.0, 1.0, {"client": "awdb", "code": "X"})]
    assert carry_forward_record_dates(tmp_path / "nope.geojson", features) == 0


# ── Coordinate filter ────────────────────────────────────────────────────────

def test_drop_invalid_coordinates():
    feats = [
        make_feature(-105.0, 40.0, {"client": "awdb", "code": "ok",
                                    "latitude": 40.0, "longitude": -105.0}),
        make_feature(0, 0, {"client": "cdec", "code": "TST",
                            "latitude": 0, "longitude": 0}),
        make_feature(None, None, {"client": "cdec", "code": "NONE"}),
    ]
    kept = drop_invalid_coordinates(feats)
    assert [f["properties"]["code"] for f in kept] == ["ok"]


# ── CSV writing: missing values are empty, never the string 'nan' ───────────

def test_write_csv_missing_values_are_empty(tmp_path):
    df = pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02"],
        "wteq_cm": [1.5, float("nan")],
        "snwd_cm": [float("nan"), 10.0],
    })
    out = tmp_path / "X.csv"
    write_csv_atomically(out, df)
    text = out.read_text()
    assert "nan" not in text
    assert text.splitlines()[1] == "2024-01-01,1.5,"
    assert text.splitlines()[2] == "2024-01-02,,10.0"


def test_station_records_to_df_and_record_dates():
    records = [
        {"date": "2024-01-02", "type": "swe", "value": 5.0},
        {"date": "2024-01-01", "type": "snwd", "value": 20.0},
        {"date": "2024-01-03", "type": "swe", "value": None},
    ]
    df = _station_records_to_df(records)
    assert list(df.columns) == ["date", "wteq_cm", "snwd_cm"]
    assert df["date"].tolist() == ["2024-01-01", "2024-01-02", "2024-01-03"]
    earliest, latest = compute_record_dates(df)
    assert earliest == "2024-01-01"
    assert latest == "2024-01-02"  # the 01-03 row is all-null


# ── Orphan CSVs are reported, not deleted ────────────────────────────────────

def test_report_orphan_csvs(tmp_path):
    (tmp_path / "KEEP.csv").write_text("date,wteq_cm,snwd_cm\n")
    (tmp_path / "ORPHAN.csv").write_text("date,wteq_cm,snwd_cm\n")
    features = [{"properties": {"code": "KEEP"}}]
    orphans = report_orphan_csvs(tmp_path, features)
    assert orphans == ["ORPHAN"]
    assert (tmp_path / "ORPHAN.csv").exists()  # retained (DESIGN.md §6.4)


# ── Cadence probe (DESIGN.md §4) ─────────────────────────────────────────────

def _df(dates_swe, dates_snwd=()):
    rows = [{"date": d, "wteq_cm": 1.0, "snwd_cm": None} for d in dates_swe]
    rows += [{"date": d, "wteq_cm": None, "snwd_cm": 5.0} for d in dates_snwd]
    return pd.DataFrame(rows or [{"date": [], "wteq_cm": [], "snwd_cm": []}])


def test_cadence_daily_series_passes():
    days = pd.date_range("2024-01-01", periods=120, freq="D")
    swe_ok, snwd_ok = check_daily_cadence(_df(days.strftime("%Y-%m-%d")))
    assert swe_ok and not snwd_ok


def test_cadence_sporadic_measurements_fail():
    # monthly-ish manual course readings over four years: plenty of
    # rows in total, never dense — must NOT count as daily-or-better
    days = pd.date_range("2020-01-01", periods=48, freq="MS")
    swe_ok, snwd_ok = check_daily_cadence(_df(days.strftime("%Y-%m-%d")))
    assert not swe_ok and not snwd_ok


def test_cadence_seasonal_station_passes():
    # winter-only pillow: daily Dec–Mar, silent the rest of the year
    days = pd.date_range("2023-12-01", "2024-03-31", freq="D")
    swe_ok, _ = check_daily_cadence(_df(days.strftime("%Y-%m-%d")))
    assert swe_ok


def test_cadence_too_few_observations_fail():
    days = pd.date_range("2024-01-01", periods=10, freq="D")
    swe_ok, _ = check_daily_cadence(_df(days.strftime("%Y-%m-%d")))
    assert not swe_ok


def test_verify_feature_from_csv_marks_failures_as_periodic():
    feat = make_feature(-105.0, 40.0, {
        "client": "cdec", "code": "CBM",
        "has_daily_swe": True, "daily_provenance": "native",
    })
    days = pd.date_range("2020-01-01", periods=48, freq="MS")
    passed = verify_feature_from_csv(feat, _df(days.strftime("%Y-%m-%d")))
    p = feat["properties"]
    assert not passed
    assert p["daily_or_better"] is False
    assert p["daily_verified"] is True
    assert p["daily_provenance"] == "none"
    assert "cadence check failed" in p["notes"]


# ── Resampler (DESIGN.md §4 — only when no native daily exists) ─────────────

def test_resample_hourly_records_to_daily_mean():
    records = [
        {"station_id": "X", "date": "2024-01-01",
         "datetime": f"2024-01-01T{h:02d}:00:00Z", "variable": "swe_m",
         "type": "swe", "value": float(h), "units": "cm",
         "interval": "hourly"}
        for h in range(24)
    ]
    daily = resample_records_to_daily(records, tz="UTC")
    assert len(daily) == 1
    assert daily[0]["date"] == "2024-01-01"
    assert daily[0]["value"] == pytest.approx(11.5)
    assert daily[0]["interval"] == "daily"


# ── Duplicates + provider model (DESIGN.md §5) ───────────────────────────────

def test_annotate_duplicates_and_borrow_operator():
    feats = [
        make_feature(-121.0, 50.0, {
            "client": "awdb", "code": "4A30P_BC_MSNT",
            "name": "Aiken Lake", "latitude": 50.0, "longitude": -121.0,
            "operator": None,
        }),
        make_feature(-121.001, 50.001, {
            "client": "databc", "code": "4A30P",
            "name": "Aiken Lake", "latitude": 50.001,
            "longitude": -121.001, "operator": "BC ENV",
        }),
        make_feature(-105.0, 40.0, {
            "client": "awdb", "code": "303_CO_SNTL",
            "name": "Bear Lake", "latitude": 40.0, "longitude": -105.0,
            "operator": "USDA NRCS",
        }),
    ]
    linked = annotate_possible_duplicates(feats)
    assert linked == 2
    dups = feats[0]["properties"]["possible_duplicates"]
    assert dups == [
        {"code": "4A30P", "client": "databc",
         "distance_m": dups[0]["distance_m"]},
    ]
    assert feats[2]["properties"]["possible_duplicates"] is None

    borrowed = borrow_operators_from_twins(feats)
    assert borrowed == 1
    assert feats[0]["properties"]["operator"] == "BC ENV"
    assert "twin 4A30P" in feats[0]["properties"]["notes"]


def test_normalize_operator():
    assert normalize_operator(".None Specified") is None
    assert normalize_operator("") is None
    assert normalize_operator(None) is None
    assert (
        normalize_operator("Natural Resources Conservation Service")
        == "USDA NRCS"
    )
    assert normalize_operator("BC Hydro") == "BC Hydro"


def test_upgrade_legacy_feature():
    legacy = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-105.0, 40.0]},
        "properties": {
            "code": "303_CO_SNTL", "client": "awdb",
            "latitude": 40.0, "longitude": -105.0,
            "Operator": "Natural Resources Conservation Service",
            "networkCode": "SNTL", "isActive": True,
            "dailySWE": True, "dailySnowDepth": False,
            "beginDate": "1979-10-01", "endDate": "2100-01-01",
        },
    }
    upgraded = upgrade_legacy_feature(legacy)
    p = upgraded["properties"]
    assert p["network_code"] == "SNTL"
    assert p["operator"] == "USDA NRCS"
    assert p["has_daily_swe"] is True
    assert p["daily_or_better"] is True
    assert p["data_provider"] == "USDA NRCS AWDB"
    assert "Operator" not in p and "dailySWE" not in p
    # universal fields materialized
    assert "station_camera_url" in p
