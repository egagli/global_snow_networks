"""
Offline tests for the SNIH network — Argentine snow pillows (issue #41).

Metadata dicts have the shape ``SNIHClient.get_metadata`` returns
(easysnowdata 0.3.3; Toscas and Cerro Azufre as listed live on 2026-10-21).
"""

from datetime import date

import pandas as pd

import scripts.get_all_stations_data as gasd
from scripts.create_all_stations_geojson import (
    UNIVERSAL_FIELDS,
    snih_station_to_feature,
)
from scripts.get_all_stations_data import (
    RefreshStats,
    RevisionLog,
    StationWriter,
    read_station_csv,
    refresh_snih,
    snih_daily_from_raw,
    snih_series,
    verify_feature_from_csv,
)


def _dv(name, interval, begin=None, end=None, n=None, vtype="swe"):
    return {
        "name": name, "code": 73, "type": vtype, "interval": interval,
        "units": "cm", "description": "d", "notes": "n",
        "associated_since": "1989-05-01", "begin_date": begin,
        "end_date": end, "n_obs": n,
    }


TOSCAS = {
    "station_id": "19145", "name": "Toscas", "latitude": -35.3,
    "longitude": -69.9, "elevation_m": 2950.0, "network_id": 1,
    "network": "Red Hidrológica Nacional", "province_code": "M",
    "province": "Mendoza", "snow_codes": [70, 72, 73, 74],
    "measurement_method": "snow pillow; snow course", "status": "Active",
    "last_raw_obs": "2026-10-21T16:00:00-03:00",
    "station_url": "https://snih.hidricosargentina.gob.ar/",
    "data_variables": [
        _dv("SWE_PILLOW", "daily", "1989-05-01", "2023-09-11", 5301),
        _dv("SWE_PILLOW", "sub_daily", None, "2026-10-21"),
        _dv("SWE_COURSE", "periodic", "1951-08-01", "2008-09-01", 35),
    ],
}
CERRO_AZUFRE = {
    **TOSCAS, "station_id": "280719", "name": "Cerro Azufre",
    "elevation_m": None, "network": "Red Meteorológica RMET",
    "snow_codes": [73],
    "data_variables": [
        _dv("SWE_PILLOW", "daily", None, None, 0),
        _dv("SWE_PILLOW", "sub_daily", None, "2026-10-21"),
    ],
}
COURSE_ONLY = {
    **TOSCAS, "station_id": "19150", "name": "Course", "snow_codes": [70, 74],
    "status": "Inactive",
    "data_variables": [_dv("SWE_COURSE", "periodic", "1951-08-01", "2008-09-01", 3)],
}


def test_pillow_feature_schema_codes_and_boundary():
    p = snih_station_to_feature(TOSCAS)["properties"]
    assert set(UNIVERSAL_FIELDS) <= set(p)
    assert p["code"] == "SNIH-19145" and p["snih_station_id"] == "19145"
    assert p["client"] == "snih" and p["network_code"] == "RHN"
    assert p["operator"] is None                 # agency not stated
    assert p["state"] == "AR" and p["province"] == "Mendoza"
    assert p["snih_validated_through"] == "2023-09-11"
    assert p["has_daily_swe"] and p["daily_provenance"] == "resampled_sub_daily"
    swe = [dv for dv in p["data_variables"] if dv["name"] == "SWE_PILLOW"]
    assert {dv["interval"] for dv in swe} == {"daily", "sub_daily"}
    assert "SIPH" in p["notes"]


def test_raw_only_pillow_and_missing_elevation():
    p = snih_station_to_feature(CERRO_AZUFRE)["properties"]
    assert p["network_code"] == "RMET" and p["elevation_m"] is None
    assert "snih_validated_through" not in p      # null extras dropped
    assert p["daily_or_better"]


def test_snow_course_only_station_is_periodic():
    assert not snih_station_to_feature(COURSE_ONLY)["properties"]["daily_or_better"]


# ── Spike filter and series assembly ─────────────────────────────────────────

def _raw(day, hour, value):
    return {"station_id": "19131", "date": day,
            "datetime": f"{day}T{hour:02d}:00:00-03:00", "variable": "SWE_PILLOW",
            "type": "swe", "value": value, "units": "cm", "interval": "sub_daily",
            "flag": "raw"}


def test_isolated_raw_spike_is_dropped_but_a_storm_is_kept():
    recs = [
        _raw("2026-07-01", 0, 30.0), _raw("2026-07-01", 6, 30.2),
        _raw("2026-07-01", 12, 1437.0),            # the 14,370 mm spike
        _raw("2026-07-01", 18, 30.4),
        _raw("2026-07-02", 0, 33.0), _raw("2026-07-02", 6, 36.0),  # storm
        _raw("2026-07-02", 12, 39.0), _raw("2026-07-02", 18, 41.0),
    ]
    daily = snih_daily_from_raw(recs)
    assert daily["2026-07-01"] == 30.2               # median without the spike
    assert daily["2026-07-02"] == 37.5


def test_isolated_daily_spike_is_dropped_and_level_shift_kept():
    days = [f"2026-08-{d:02d}" for d in range(1, 8)]
    values = [20.0, 20.5, 60.0, 21.0, 21.5, 45.0, 45.5]  # spike, then a shift
    recs = [_raw(d, 12, v) for d, v in zip(days, values)]
    daily = snih_daily_from_raw(recs)
    assert "2026-08-03" not in daily
    assert daily["2026-08-06"] == 45.0 and daily["2026-08-07"] == 45.5


def test_series_uses_validated_up_to_the_boundary_and_raw_after():
    validated = [
        {"station_id": "19145", "date": "2023-09-10", "type": "swe", "value": 50.0},
        {"station_id": "19145", "date": "2023-09-11", "type": "swe", "value": 49.0},
    ]
    raw = [_raw("2023-09-11", 12, 99.0), _raw("2023-09-12", 12, 48.0)]
    df = snih_series(validated, raw, "2023-09-11").set_index("date")
    assert df.loc["2023-09-11", "wteq_cm"] == 49.0   # validated wins
    assert df.loc["2023-09-12", "wteq_cm"] == 48.0   # raw only after it


def test_provenance_follows_the_csv_not_the_status():
    inactive = {**TOSCAS, "status": "Inactive"}
    feat = snih_station_to_feature(inactive)
    validated_only = pd.DataFrame({
        "date": pd.date_range("2023-06-01", periods=60).strftime("%Y-%m-%d"),
        "wteq_cm": 10.0, "snwd_cm": None,
    })
    feat["properties"]["latest_record_date"] = "2023-07-30"
    assert verify_feature_from_csv(feat, validated_only)
    assert feat["properties"]["daily_provenance"] == "native"
    feat["properties"]["latest_record_date"] = "2026-09-01"   # raw days too
    assert verify_feature_from_csv(feat, validated_only)
    assert feat["properties"]["daily_provenance"] == "resampled_sub_daily"


class _FakeClient:
    """Stands in for SNIHClient: validated to 2023-09-11, raw afterwards."""

    calls: list[dict]

    def get_data(self, station_ids, variables, interval, **kwargs):
        _FakeClient.calls.append({"interval": interval, **kwargs})
        if interval == "daily":
            days = pd.date_range("2023-08-01", "2023-09-11").strftime("%Y-%m-%d")
            return [{"station_id": "19145", "date": d, "type": "swe",
                     "value": 40.0} for d in days]
        days = pd.date_range("2026-09-01", "2026-10-20").strftime("%Y-%m-%d")
        return [_raw(d, h, 12.0) for d in days for h in (0, 6, 12, 18)]


def test_refresh_snih_raw_starts_after_the_validated_record(tmp_path, monkeypatch):
    monkeypatch.setattr(gasd, "SNIHClient", _FakeClient)
    _FakeClient.calls = []
    feats = [snih_station_to_feature(TOSCAS)]
    w = StationWriter(
        features=feats, data_dir=tmp_path, refreshed_at_utc="t",
        stats=RefreshStats(), revisions=RevisionLog(detected_utc="t"),
    )
    refresh_snih([(0, "SNIH-19145")], w, today=date(2026, 10, 21))
    daily_call, raw_call = _FakeClient.calls
    assert daily_call["end_date"] == "2026-10-20"
    assert raw_call["begin_date"] == "2023-09-12"
    assert raw_call["end_date"] == "2026-10-20"
    csv = read_station_csv(tmp_path / "SNIH-19145.csv")
    assert csv["date"].iloc[0] == "2023-08-01" and csv["date"].iloc[-1] == "2026-10-20"
    assert feats[0]["properties"]["daily_provenance"] == "resampled_sub_daily"
