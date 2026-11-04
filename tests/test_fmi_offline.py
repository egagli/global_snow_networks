"""
Offline tests for the FMI network (egagli/easysnowdata#37).

Station dicts have the shape ``FMIClient.get_metadata`` returns (the
station plus ``data_variables``).  Sodankylä Tähtelä's fmisid, position,
elevation and networks are the ones FMI's WFS and station table gave on
2026-11-04; no request to FMI is made here.
"""

import json
from datetime import date

import scripts.get_all_stations_data as gasd
from scripts.create_all_stations_geojson import (
    DAY_CONVENTIONS,
    UNIVERSAL_FIELDS,
    day_convention,
    fmi_previous_served,
    fmi_station_to_feature,
)
from scripts.get_all_stations_data import (
    FMI_BATCH,
    FMI_LOOKBACK_DAYS,
    RefreshStats,
    RevisionLog,
    StationWriter,
    fmi_batches,
    read_station_csv,
    refresh_fmi,
    write_csv_atomically,
)

DAILY_SNOW = {
    "name": "snow", "type": "snwd", "interval": "daily", "units": "cm",
    "description": "Daily snow depth (instant value, P1D).", "notes": "",
    "begin_date": "1891-01-01", "end_date": None, "n_obs": None,
}
HOURLY_SNOW = {**DAILY_SNOW, "name": "snow_aws", "interval": "hourly"}

SODANKYLA = {
    "station_id": "101932", "name": "Sodankylä Tähtelä",
    "latitude": 67.36663, "longitude": 26.62901, "elevation_m": 179.0,
    "status": "Active", "begin_date": "1891-01-01", "end_date": None,
    "wmo_id": "02836", "region": "Sodankylä", "network_code": "AWS",
    "network_ids": [121, 122, 123, 124],
    "networks": ["Automaattinen sääasema", "Sääasema", "Ilmastoasema", "Sadeasema"],
    "station_groups": "weather, sounding, air quality (FMI)",
    "operator": "Finnish Meteorological Institute", "station_url": None,
    "data_variables": [DAILY_SNOW, HOURLY_SNOW],
}
KITEE = {
    **SODANKYLA, "station_id": "100694", "name": "Kitee Närsäkkälä",
    "latitude": 61.915285, "longitude": 30.141083, "elevation_m": 105.0,
    "status": "Inactive", "begin_date": "1925-05-01", "end_date": "1958-12-31",
    "wmo_id": None, "network_code": "PRCP", "network_ids": [124],
    "networks": ["Sadeasema"], "data_variables": [DAILY_SNOW],
}


def test_feature_is_on_the_universal_schema():
    f = fmi_station_to_feature(SODANKYLA, served=True)
    p = f["properties"]
    assert set(UNIVERSAL_FIELDS) <= set(p)
    assert p["code"] == "FMI-101932"
    assert p["client"] == "fmi" and p["state"] == "FI"
    assert p["network_code"] == "FMI_AWS"
    assert p["operator"] == "Finnish Meteorological Institute"
    assert p["has_daily_snwd"] is True and p["has_daily_swe"] is False
    assert p["day_convention"] == "instant 06:00 UTC"
    assert p["fmi_served"] is True
    assert f["geometry"]["coordinates"][:2] == [26.62901, 67.36663]


def test_unserved_station_is_inventoried_but_never_a_candidate():
    p = fmi_station_to_feature(KITEE, served=False)["properties"]
    assert p["data_variables"] == []
    assert p["has_daily_snwd"] is False and p["daily_or_better"] is False
    assert "unknown to its observation service" in p["notes"]
    assert gasd.is_fetch_candidate(p) is False


def test_manual_stations_have_no_stated_day_convention():
    p = fmi_station_to_feature(KITEE, served=True)["properties"]
    assert p["network_code"] == "FMI_PRCP"
    assert p["day_convention"] == "unknown"
    assert day_convention({"client": "fmi", "network_code": "FMI_AWS"}) == (
        DAY_CONVENTIONS["fmi:FMI_AWS"]
    )


def test_previous_screening_verdicts_are_carried_forward(tmp_path):
    path = tmp_path / "fmi_stations.geojson"
    feats = [
        fmi_station_to_feature(SODANKYLA, served=True),
        fmi_station_to_feature(KITEE, served=False),
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))
    assert fmi_previous_served(path) == {"101932": True, "100694": False}
    assert fmi_previous_served(tmp_path / "missing.geojson") == {}


def _writer(tmp_path, feats):
    return StationWriter(
        features=feats, data_dir=tmp_path, refreshed_at_utc="t",
        stats=RefreshStats(), revisions=RevisionLog(detected_utc="t"),
    )


def test_batches_split_new_and_archived_and_respect_the_id_limit(tmp_path):
    feats = [
        fmi_station_to_feature({**KITEE, "station_id": str(100000 + i),
                                "begin_date": f"{1900 + i % 100:04d}-01-01"},
                               served=True)
        for i in range(FMI_BATCH + 3)
    ]
    stations = [(i, f["properties"]["code"]) for i, f in enumerate(feats)]
    write_csv_atomically(tmp_path / "FMI-100000.csv", gasd.pd.DataFrame(
        {"date": ["2024-01-01"], "wteq_cm": [None], "snwd_cm": [5.0]}))
    new, archived = fmi_batches(stations, feats, tmp_path)
    assert [len(b) for b in new] == [FMI_BATCH, 2]
    assert archived == [[(0, "FMI-100000", "100000")]]
    # new stations are ordered by begin date, so a batch's span stays tight
    begins = [feats[i]["properties"]["begin_date"] for b in new for i, _, _ in b]
    assert begins == sorted(begins)


class _FakeClient:
    calls: list[dict] = []

    def get_data(self, station_ids, variables, interval, begin_date=None, end_date=None):
        type(self).calls.append({"ids": list(station_ids), "variables": variables,
                                 "begin": begin_date, "end": end_date})
        day = begin_date or "1958-12-01"
        return [
            {"station_id": sid, "date": day, "variable": "snow", "type": "snwd",
             "value": 10.0, "units": "cm", "interval": "daily"}
            for sid in station_ids
        ]


def test_first_refresh_is_full_then_a_lookback_merge(tmp_path, monkeypatch):
    monkeypatch.setattr(gasd, "FMIClient", _FakeClient)
    _FakeClient.calls = []
    feats = [fmi_station_to_feature(KITEE, served=True)]
    refresh_fmi([(0, "FMI-100694")], _writer(tmp_path, feats), today=date(2026, 11, 4))
    # No CSV yet: no dates, so the client asks for the station's own period
    assert _FakeClient.calls == [
        {"ids": ["100694"], "variables": ["snwd"], "begin": None, "end": None}
    ]
    csv = read_station_csv(tmp_path / "FMI-100694.csv")
    assert csv["snwd_cm"].tolist() == [10.0] and csv["wteq_cm"].isna().all()

    _FakeClient.calls = []
    refresh_fmi([(0, "FMI-100694")], _writer(tmp_path, feats), today=date(2026, 11, 4))
    assert _FakeClient.calls[0]["end"] == "2026-11-04"
    assert _FakeClient.calls[0]["begin"] == date.fromordinal(
        date(2026, 11, 4).toordinal() - FMI_LOOKBACK_DAYS
    ).isoformat()
    # merged, not replaced: the archived 1958 value is kept
    csv = read_station_csv(tmp_path / "FMI-100694.csv").set_index("date")
    assert set(csv.index) == {"1958-12-01", _FakeClient.calls[0]["begin"]}


def test_aboa_is_in_antarctica():
    aboa = {**SODANKYLA, "station_id": "112275", "name": "Antarktis Aboa",
            "latitude": -73.05, "longitude": -13.38333, "region": "AQ"}
    assert fmi_station_to_feature(aboa, served=True)["properties"]["state"] == "AQ"
    assert fmi_station_to_feature(SODANKYLA, served=True)["properties"]["state"] == "FI"
