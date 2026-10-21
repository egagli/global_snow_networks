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
    databc_station_to_feature,
    drop_invalid_coordinates,
    make_feature,
    normalize_operator,
    prune_non_daily_duplicates,
    retain_vanished_stations,
    upgrade_legacy_feature,
)
from scripts.get_all_stations_data import (
    REVISION_COLUMNS,
    RefreshStats,
    RevisionLog,
    StationWriter,
    _station_records_to_df,
    check_daily_cadence,
    compute_record_dates,
    diff_revisions,
    is_fetch_candidate,
    merge_pending_revisions,
    read_station_csv,
    report_orphan_csvs,
    resample_records_to_daily,
    shrink_reason,
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


def test_prune_non_daily_duplicates():
    feats = [
        make_feature(-120.0, 39.0, {
            "client": "awdb", "code": "A", "name": "Twin Lakes",
            "latitude": 39.0, "longitude": -120.0, "daily_or_better": True,
        }),
        make_feature(-120.0, 39.0005, {
            "client": "cdec", "code": "B", "name": "Twin Lakes",
            "latitude": 39.0005, "longitude": -120.0, "daily_or_better": True,
        }),
        make_feature(-120.0, 39.001, {
            "client": "databc", "code": "C", "name": "Twin Lakes",
            "latitude": 39.001, "longitude": -120.0, "daily_or_better": False,
        }),
    ]
    annotate_possible_duplicates(feats)
    assert all(f["properties"]["possible_duplicates"] for f in feats)

    pruned = prune_non_daily_duplicates(feats)
    assert pruned == 3
    a, b, c = (f["properties"] for f in feats)
    # daily <-> daily link survives; anything involving the periodic C is gone
    assert [d["code"] for d in a["possible_duplicates"]] == ["B"]
    assert [d["code"] for d in b["possible_duplicates"]] == ["A"]
    assert c["possible_duplicates"] is None


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


def test_databc_camera_never_fills_the_photo_slot():
    """A live-camera snapshot is not a site photo (1C38P had no AQRT photo)."""
    cam = "https://pvs.nupointsystems.com/latest.php?pass=abc"
    base = {"station_type": "ASWS", "latitude": 50.0, "longitude": -120.0,
            "status": "Active", "camera_url": cam}
    no_photo = databc_station_to_feature({**base, "location_id": "9Z99P"})
    p = no_photo["properties"]
    assert p["station_image_url"] is None
    assert p["station_camera_url"] == cam

    photo = "https://bcmoe-prod.aquaticinformatics.net/Data/GetFileById/1"
    with_photo = databc_station_to_feature(
        {**base, "location_id": "9Z99P", "station_image_url": photo})
    assert with_photo["properties"]["station_image_url"] == photo

    # The tabled photo-slider page wins over the bare WFS snapshot as the link.
    tabled = databc_station_to_feature({**base, "location_id": "1C38P"})
    assert "photo-slider" in tabled["properties"]["station_camera_url"]


def test_write_csv_never_emits_string_nan(tmp_path):
    """A literal "nan" (or None) that reaches the writer as text still
    becomes an empty cell (DESIGN.md §6.3)."""
    df = pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02"],
        "wteq_cm": ["nan", None],
        "snwd_cm": ["NaN", 3.0],
    }, dtype=object)
    out = tmp_path / "X.csv"
    write_csv_atomically(out, df)
    assert out.read_text().splitlines()[1:] == [
        "2024-01-01,,", "2024-01-02,,3.0",
    ]


def test_read_station_csv_treats_literal_nan_as_missing(tmp_path):
    path = tmp_path / "X.csv"
    path.write_text("date,wteq_cm,snwd_cm\n2024-01-01,nan,nan\n")
    df = read_station_csv(path)
    assert df["wteq_cm"].isna().all() and df["snwd_cm"].isna().all()
    assert read_station_csv(tmp_path / "absent.csv") is None


# ── Retention of stations that vanish upstream (DESIGN.md §6.4) ────────────

def _feat(client, code, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0.5, 0.5]},
        "properties": {
            "client": client, "code": code, "network_code": "X",
            "operator": None, "notes": None, **props,
        },
    }


def _write_prev(tmp_path, feats):
    prev = tmp_path / "prev.geojson"
    prev.write_text(json.dumps({"features": feats}))
    return prev


def test_retain_vanished_station_is_marked_missing(tmp_path):
    prev = _write_prev(tmp_path, [
        _feat("awdb", "A", daily_or_better=True, daily_verified=True,
              latest_record_date="2026-07-19"),
        _feat("awdb", "B"),
    ])
    feats = [_feat("awdb", "B")]
    assert retain_vanished_stations(prev, feats, "2026-10-21") == 1
    by_code = {f["properties"]["code"]: f["properties"] for f in feats}
    a = by_code["A"]
    assert a["upstream_status"] == "missing"
    assert a["missing_upstream_since"] == "2026-10-21"
    assert "since 2026-10-21" in a["notes"]
    # probe verdict and record dates ride along, so it stays mapped
    assert a["daily_or_better"] is True
    assert a["latest_record_date"] == "2026-07-19"
    assert by_code["B"]["upstream_status"] == "listed"
    assert by_code["B"]["missing_upstream_since"] is None
    assert not is_fetch_candidate(a)


def test_retain_keeps_first_missing_date_and_note_once(tmp_path):
    prev = _write_prev(tmp_path, [
        _feat("awdb", "A", upstream_status="missing",
              missing_upstream_since="2026-07-23",
              notes="Not in the awdb station list since 2026-07-23; "
                    "retained with its archived record (DESIGN.md §6.4)."),
        _feat("awdb", "B"),
    ])
    feats = [_feat("awdb", "B")]
    retain_vanished_stations(prev, feats, "2026-10-21")
    a = next(f["properties"] for f in feats if f["properties"]["code"] == "A")
    assert a["missing_upstream_since"] == "2026-07-23"
    assert a["notes"].count("Not in the awdb station list") == 1


def test_reappearing_station_is_fresh_again(tmp_path):
    prev = _write_prev(tmp_path, [
        _feat("awdb", "A", upstream_status="missing",
              missing_upstream_since="2026-07-23", notes="gone"),
    ])
    feats = [_feat("awdb", "A")]
    assert retain_vanished_stations(prev, feats, "2026-10-21") == 0
    assert len(feats) == 1
    p = feats[0]["properties"]
    assert p["upstream_status"] == "listed"
    assert p["missing_upstream_since"] is None
    assert p["notes"] is None


def test_absent_client_is_an_outage_not_a_retirement(tmp_path):
    """A client with no features this run (skipped, or an outage with no
    fallback) keeps its stations unmarked — and still refreshed."""
    prev = _write_prev(tmp_path, [
        _feat("yukon", "Y1", upstream_status="listed", has_daily_swe=True),
        _feat("awdb", "B"),
    ])
    feats = [_feat("awdb", "B")]
    assert retain_vanished_stations(prev, feats, "2026-10-21") == 0
    y = next(f["properties"] for f in feats if f["properties"]["code"] == "Y1")
    assert y["upstream_status"] == "listed"
    assert is_fetch_candidate(y)


def test_retain_without_previous_inventory_is_noop(tmp_path):
    feats = [_feat("awdb", "B")]
    assert retain_vanished_stations(tmp_path / "nope.json", feats, "x") == 0
    assert feats[0]["properties"]["upstream_status"] == "listed"


# ── Shrink guard (DESIGN.md §6.3) ──────────────────────────────────────────

def _series(n, start="2000-01-01", value=1.0):
    dates = pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates, "wteq_cm": [value] * n, "snwd_cm": [None] * n,
    })


def test_shrink_guard_absolute_row_threshold():
    old = _series(100)
    assert shrink_reason(old, old.iloc[:70]) is None       # loses 30
    assert "loses 31" in shrink_reason(old, old.iloc[:69])  # loses 31


def test_shrink_guard_fractional_row_threshold():
    old = _series(10_000)
    assert shrink_reason(old, old.iloc[:9_500]) is None    # 5 % exactly
    assert shrink_reason(old, old.iloc[:9_499]) is not None


def test_shrink_guard_record_start_shift():
    old = _series(400)
    assert shrink_reason(old, old.iloc[30:].copy()) is None  # 30 days later
    reason = shrink_reason(old, pd.concat([old.iloc[31:], _series(31, "2001-02-05")]))
    assert reason is not None and "31 days later" in reason


def test_shrink_guard_ignores_blank_rows():
    old = _series(100)
    new = old.copy()
    new.loc[:49, "wteq_cm"] = None   # same rows, half of them blank now
    assert shrink_reason(old, new) is not None


def _writer(tmp_path, feats, allow=frozenset()):
    return StationWriter(
        features=feats, data_dir=tmp_path, refreshed_at_utc="2026-10-21T09:00:00+00:00",
        stats=RefreshStats(),
        revisions=RevisionLog(detected_utc="2026-10-21T09:00:00+00:00"),
        allow_shrink=allow,
    )


def test_writer_refuses_shrinking_replacement(tmp_path):
    old = _series(100)
    write_csv_atomically(tmp_path / "S.csv", old)
    feats = [_feat("awdb", "S", latest_record_date="2000-04-09")]
    w = _writer(tmp_path, feats)
    assert not w.store(0, "S", "awdb", old.iloc[:10])
    assert len(read_station_csv(tmp_path / "S.csv")) == 100   # kept
    assert feats[0]["properties"]["latest_record_date"] == "2000-04-09"
    assert "daily_verified" not in feats[0]["properties"]
    assert w.stats.refused_shrink == 1 and w.stats.updated_csvs == 0
    rows = w.revisions.table()
    assert list(rows["kind"]) == ["refused_shrink"]
    assert rows.iloc[0][["variable", "old_value", "new_value"]].tolist() == [
        "rows", "100", "10",
    ]


@pytest.mark.parametrize("allow", [frozenset({"S"}), frozenset({"all"})])
def test_writer_allow_shrink_override(tmp_path, allow):
    old = _series(100)
    write_csv_atomically(tmp_path / "S.csv", old)
    feats = [_feat("awdb", "S")]
    w = _writer(tmp_path, feats, allow)
    assert w.store(0, "S", "awdb", old.iloc[:10])
    assert len(read_station_csv(tmp_path / "S.csv")) == 10
    assert w.stats.refused_shrink == 0
    # the deleted history is still logged, as removals
    assert set(w.revisions.table()["kind"]) == {"removed"}


def test_writer_new_station_has_no_revisions(tmp_path):
    feats = [_feat("awdb", "S")]
    w = _writer(tmp_path, feats)
    assert w.store(0, "S", "awdb", _series(40))
    assert w.revisions.table().empty
    assert feats[0]["properties"]["daily_verified"] is True


# ── Revision log (DESIGN.md §6.6, issue #24) ───────────────────────────────

def test_diff_revisions_kinds_and_appends_ignored():
    old = pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        "wteq_cm": [1.0, None, 3.0, 4.0],
        "snwd_cm": [10.0, 20.0, 30.0, None],
    })
    new = pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-05"],
        "wteq_cm": [1.0 + 1e-9, 2.0, 3.5, 9.0],  # noise, filled, revised
        "snwd_cm": [10.0, None, 30.0, 50.0],      # 01-02 removed
    })  # 2024-01-04 vanished; 2024-01-05 is an append, not a revision
    d = diff_revisions(old, new)
    got = set(map(tuple, d[["date", "variable", "kind"]].values))
    assert got == {
        ("2024-01-02", "wteq_cm", "filled"),
        ("2024-01-02", "snwd_cm", "removed"),
        ("2024-01-03", "wteq_cm", "revised"),
        ("2024-01-04", "wteq_cm", "removed"),
    }
    rev = d[d["kind"] == "revised"].iloc[0]
    assert (rev["old_value"], rev["new_value"]) == ("3.0", "3.5")
    filled = d[d["kind"] == "filled"].iloc[0]
    assert filled["old_value"] == ""


def test_diff_revisions_gap_filled_inside_old_period():
    old = pd.DataFrame({"date": ["2024-01-01", "2024-01-03"],
                        "wteq_cm": [1.0, 3.0], "snwd_cm": [None, None]})
    new = pd.DataFrame({"date": ["2024-01-01", "2024-01-02", "2024-01-03"],
                        "wteq_cm": [1.0, 2.0, 3.0], "snwd_cm": [None] * 3})
    d = diff_revisions(old, new)
    assert d[["date", "kind"]].values.tolist() == [["2024-01-02", "filled"]]


def test_diff_revisions_handles_all_none_object_columns():
    old = _series(5)
    new = old.copy()
    new["snwd_cm"] = pd.Series([None] * 5, dtype=object)
    assert diff_revisions(old, new).empty


def test_revision_log_month_files_and_pending_merge(tmp_path):
    log = RevisionLog(detected_utc="2026-10-21T09:00:00+00:00")
    rows = diff_revisions(_series(3), _series(3, value=2.0))
    log.add("S", "nve", rows)
    assert log.write(tmp_path, network="nve") == 3
    assert (tmp_path / "pending" / "nve.csv").exists()
    assert not (tmp_path / "2026-10.csv").exists()
    assert merge_pending_revisions(tmp_path) == 3
    assert not (tmp_path / "pending" / "nve.csv").exists()
    # a second night appends to the same month file
    log2 = RevisionLog(detected_utc="2026-10-22T09:00:00+00:00")
    log2.add("T", "awdb", rows)
    log2.write(tmp_path)
    month = pd.read_csv(tmp_path / "2026-10.csv", dtype=str,
                        keep_default_na=False)
    assert list(month.columns) == REVISION_COLUMNS
    assert month["code"].tolist() == ["S"] * 3 + ["T"] * 3
    assert month["kind"].eq("revised").all()
    assert any("oldest 2000-01-01" in line for line in log2.summary())
