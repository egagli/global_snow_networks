"""
Offline tests for the Hydro-Québec network (issue #39) — no network access.

The station dicts below have the shape ``HydroQuebecClient.get_all_stations``
returns (easysnowdata 0.3.3, built from the portal's ``group_by`` exports);
the daily records have the shape of its ``get_data(interval="daily")``.
"""

from datetime import date

import pandas as pd

import scripts.get_all_stations_data as gasd
from scripts.create_all_stations_geojson import (
    UNIVERSAL_FIELDS,
    hydroquebec_station_to_feature,
)
from scripts.get_all_stations_data import (
    RefreshStats,
    RevisionLog,
    StationWriter,
    is_fetch_candidate,
    merge_with_archive,
    read_station_csv,
    refresh_hydroquebec,
    verify_feature_from_csv,
    write_csv_atomically,
)

SWE = "Équivalent en eau de la neige"
SD = "Épaisseur de neige"
TX = "Température Maximum"

GMON_STATION = {
    "station_id": "1-10613",
    "name": "Cooper Ouvert (CM3V)",
    "latitude": 48.8492,
    "longitude": -71.3,
    "elevation_m": 506.0,
    "status": "Active",
    "region": "Saguenay--Lac-Saint-Jean",
    "region_code": "02",
    "hq_code": "CM3V",
    "operator": "Hydro-Québec",
    "network": "Hydro-Québec hydrometeorological network",
    "measurement_method": "GMON passive gamma (SWE, 6-hourly)",
    "commissioned": "2016-09-01",
    "decommissioned": None,
    "variables": {
        SWE: {"n_obs": 240, "first": "2026-10-04T08:00:00+00:00",
              "last": "2026-10-14T11:00:00+00:00"},
        SD: {"n_obs": 240, "first": "2026-10-04T08:00:00+00:00",
             "last": "2026-10-14T11:00:00+00:00"},
        TX: {"n_obs": 240, "first": "2026-10-04T08:00:00+00:00",
             "last": "2026-10-14T11:00:00+00:00"},
    },
    "has_swe": True,
    "has_snwd": True,
    "begin_date": "2026-10-04",
    "end_date": "2026-10-14",
    "latest_observation": "2026-10-14T11:00:00+00:00",
    "license": "CC BY-NC 4.0",
    "station_url": "https://donnees.hydroquebec.com/explore/dataset/donnees-hydrometeorologiques/",
}

DEPTH_ONLY_STATION = {
    **GMON_STATION,
    "station_id": "1-15884",
    "name": "Bernard (ULaval)",
    "elevation_m": None,
    "hq_code": None,
    "measurement_method": "automatic snow depth (sensor unspecified)",
    "variables": {SD: {"n_obs": 0, "first": None, "last": None}},
    "has_swe": False,
}


# ── Inventory ────────────────────────────────────────────────────────────────

def test_gmon_station_feature_schema_and_flags():
    f = hydroquebec_station_to_feature(GMON_STATION)
    p = f["properties"]
    assert set(UNIVERSAL_FIELDS) <= set(p)
    assert f["geometry"]["coordinates"] == [-71.3, 48.8492]
    assert p["code"] == "1-10613" and p["client"] == "hydroquebec"
    assert p["network_code"] == "HQ" and p["state"] == "QC"
    assert p["operator"] == "Hydro-Québec"
    assert "CC BY-NC" in p["data_provider"]
    assert "no commercial use" in p["notes"]
    assert p["license"] == "CC BY-NC 4.0"
    assert p["has_daily_swe"] and p["has_daily_snwd"]
    assert p["daily_or_better"] and not p["daily_verified"]
    # No native daily product: never "native" (DESIGN.md §4).
    assert p["daily_provenance"] == "resampled_hourly"
    assert p["begin_date"] == "2016-09-01"   # commissioning, not the window
    by_type = {dv["type"]: dv for dv in p["data_variables"]}
    assert set(by_type) == {"swe", "snwd", "temp_max"}
    assert all(dv["interval"] == "hourly" for dv in p["data_variables"])
    assert by_type["swe"]["units"] == "cm"
    assert by_type["swe"]["begin_date"] == "2026-10-04"
    assert is_fetch_candidate(p)


def test_depth_only_station_and_missing_elevation():
    p = hydroquebec_station_to_feature(DEPTH_ONLY_STATION)["properties"]
    assert not p["has_daily_swe"] and p["has_daily_snwd"]
    assert p["elevation_m"] is None          # null, never 0
    assert "hq_code" not in p                 # null extras are dropped


# ── Merge mode (rolling-window source) ───────────────────────────────────────

def _days(start, n, swe=1.0):
    dates = pd.date_range(start, periods=n, freq="D").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates, "wteq_cm": [swe] * n, "snwd_cm": [None] * n,
    })


def test_merge_keeps_history_new_wins_blank_keeps_archived():
    old = _days("2026-09-01", 40)                 # to 2026-10-10
    new = _days("2026-10-06", 10, swe=2.0)       # 10-06 … 10-15
    new.loc[new["date"] == "2026-10-07", "wteq_cm"] = None
    out = merge_with_archive(old, new)
    assert len(out) == 45 and out["date"].is_monotonic_increasing
    swe = out.set_index("date")["wteq_cm"]
    assert swe["2026-09-01"] == 1.0              # outside the window: kept
    assert swe["2026-10-06"] == 2.0              # in both: new wins
    assert swe["2026-10-07"] == 1.0              # blank in new: kept
    assert swe["2026-10-15"] == 2.0              # appended


def _writer(tmp_path, feats):
    return StationWriter(
        features=feats, data_dir=tmp_path,
        refreshed_at_utc="2026-10-21T09:00:00+00:00",
        stats=RefreshStats(),
        revisions=RevisionLog(detected_utc="2026-10-21T09:00:00+00:00"),
    )


def test_writer_merge_grows_csv_without_shrink_refusal(tmp_path):
    old = _days("2026-08-01", 80)                # to 2026-10-19
    write_csv_atomically(tmp_path / "1-10613.csv", old)
    feats = [hydroquebec_station_to_feature(GMON_STATION)]
    w = _writer(tmp_path, feats)
    window = _days("2026-10-11", 10)             # 10-11 … 10-20
    window.loc[window["date"] == "2026-10-12", "wteq_cm"] = 1.5
    assert w.store(0, "1-10613", "hydroquebec", window, merge=True)
    assert w.stats.refused_shrink == 0
    csv = read_station_csv(tmp_path / "1-10613.csv")
    assert len(csv) == 81 and csv["date"].iloc[0] == "2026-08-01"
    rev = w.revisions.table()
    assert rev[["date", "kind"]].values.tolist() == [["2026-10-12", "revised"]]
    p = feats[0]["properties"]
    assert p["earliest_record_date"] == "2026-08-01"
    assert p["latest_record_date"] == "2026-10-20"


def test_without_merge_a_window_would_be_refused(tmp_path):
    # The reason merge mode exists: a plain replace is a 70-row loss.
    write_csv_atomically(tmp_path / "S.csv", _days("2026-08-01", 80))
    feats = [hydroquebec_station_to_feature(GMON_STATION)]
    w = _writer(tmp_path, feats)
    assert not w.store(0, "S", "hydroquebec", _days("2026-10-11", 10))
    assert w.stats.refused_shrink == 1


def test_provenance_stays_resampled_after_a_failed_probe():
    feat = hydroquebec_station_to_feature(GMON_STATION)
    assert not verify_feature_from_csv(feat, _days("2026-10-11", 10))
    assert feat["properties"]["daily_provenance"] == "none"
    assert verify_feature_from_csv(feat, _days("2026-08-01", 80))
    assert feat["properties"]["daily_provenance"] == "resampled_hourly"


# ── refresh_hydroquebec ──────────────────────────────────────────────────────

class _FakeClient:
    """Stands in for HydroQuebecClient; set ``calls``/``records`` per test."""

    calls: list[dict]
    records: list[dict]

    def get_data(self, **kwargs):
        _FakeClient.calls.append(kwargs)
        ids = set(kwargs["station_ids"])
        return [r for r in _FakeClient.records if r["station_id"] in ids]


def _rec(sid, day, vtype, value):
    return {
        "station_id": sid, "date": day,
        "variable": SWE if vtype == "swe" else SD, "type": vtype,
        "value": value, "units": "cm", "interval": "daily",
    }


def test_refresh_merges_window_and_skips_incomplete_today(
    tmp_path, monkeypatch,
):
    monkeypatch.setattr(gasd, "HydroQuebecClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.records = [
        _rec("1-10613", "2026-10-19", "swe", 3.1),
        _rec("1-10613", "2026-10-19", "snwd", 12.0),
        _rec("1-10613", "2026-10-20", "swe", 3.4),
    ]
    write_csv_atomically(tmp_path / "1-10613.csv", _days("2026-08-01", 70))
    feats = [hydroquebec_station_to_feature(GMON_STATION)]
    w = _writer(tmp_path, feats)
    refresh_hydroquebec([(0, "1-10613")], w, today=date(2026, 10, 21))
    call = _FakeClient.calls[0]
    assert call["interval"] == "daily"
    assert call["end_date"] == "2026-10-20"     # today (local) is incomplete
    assert call["begin_date"] == "2026-10-05"
    csv = read_station_csv(tmp_path / "1-10613.csv")
    assert len(csv) == 72 and csv["date"].iloc[0] == "2026-08-01"
    last = csv.set_index("date").loc["2026-10-19"]
    assert last["wteq_cm"] == 3.1 and last["snwd_cm"] == 12.0
    assert w.stats.updated_csvs == 1


def test_refresh_with_no_records_raises_an_actions_error(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(gasd, "HydroQuebecClient", _FakeClient)
    _FakeClient.calls = []
    _FakeClient.records = []
    write_csv_atomically(tmp_path / "1-10613.csv", _days("2026-08-01", 70))
    feats = [hydroquebec_station_to_feature(GMON_STATION)]
    w = _writer(tmp_path, feats)
    refresh_hydroquebec([(0, "1-10613")], w, today=date(2026, 10, 21))
    assert "::error title=Hydro-Québec refresh::" in capsys.readouterr().out
    assert len(read_station_csv(tmp_path / "1-10613.csv")) == 70   # untouched
