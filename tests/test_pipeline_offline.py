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
    carry_forward_record_dates,
    drop_invalid_coordinates,
    make_feature,
)
from scripts.get_all_stations_data import (
    _station_records_to_df,
    compute_record_dates,
    report_orphan_csvs,
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
    assert "earliest_record_date" not in rebuilt[1]["properties"]


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
