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
import scripts.create_all_stations_geojson as casg
from scripts.create_all_stations_geojson import (
    DAY_CONVENTIONS,
    UNIVERSAL_FIELDS,
    day_convention,
    fmi_previous_served,
    fmi_sample_days,
    fmi_station_to_feature,
    run_fmi_workflow,
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
    assert "no daily snow depth" in p["notes"]
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


def test_sample_days_cover_the_record_cheaply():
    days = fmi_sample_days(date(2026, 11, 4))
    assert days[0] == date(1880, 1, 15)
    assert date(1958, 3, 15) not in days and date(1959, 3, 15) in days
    assert days[-1] == date(2026, 5, 15)  # 15 November 2026 is still ahead
    assert len(days) < 360


class _InventoryClient:
    sampled: list = []

    def get_all_stations(self):
        return [{k: v for k, v in s.items() if k != "data_variables"}
                for s in (SODANKYLA, KITEE)]

    def get_metadata(self, sid):
        return {"101932": SODANKYLA, "100694": KITEE}[sid]

    def reporting_stations(self, day):
        type(self).sampled.append(day)
        return {"101932": {"name": "Sodankylä Tähtelä", "latitude": 67.36663,
                           "longitude": 26.62901}}


def test_first_inventory_samples_the_record_later_ones_two_days(monkeypatch):
    monkeypatch.setattr(casg, "FMIClient", _InventoryClient)
    _InventoryClient.sampled = []
    feats, daily = run_fmi_workflow(previous={}, today=date(2026, 11, 4))
    assert len(_InventoryClient.sampled) == len(fmi_sample_days(date(2026, 11, 4)))
    served = {f["properties"]["code"]: f["properties"]["fmi_served"] for f in feats}
    assert served == {"FMI-101932": True, "FMI-100694": False}
    assert [f["properties"]["code"] for f in daily] == ["FMI-101932"]

    # Later: previous verdicts kept (Kitee was found before), two days sampled
    _InventoryClient.sampled = []
    feats, _ = run_fmi_workflow(
        previous={"101932": True, "100694": True}, today=date(2026, 11, 4)
    )
    assert _InventoryClient.sampled == [date(2026, 11, 2), date(2026, 11, 3)]
    assert all(f["properties"]["fmi_served"] for f in feats)


def test_table_only_station_takes_the_observed_position():
    table_only = {**KITEE, "latitude": 61.9, "longitude": 30.1,
                  "coordinate_precision_deg": 0.1, "begin_year": 1925,
                  "end_year": 1958, "begin_date": None, "end_date": None}
    p = fmi_station_to_feature(table_only, served=True)["properties"]
    assert p["fmi_position_source"] == "station_table" and "0.1°" in p["notes"]
    p = fmi_station_to_feature(
        table_only, served=True,
        position={"latitude": 61.915285, "longitude": 30.141083},
    )["properties"]
    assert p["fmi_position_source"] == "observations"
    assert (p["latitude"], p["longitude"]) == (61.915285, 30.141083)
    assert p["fmi_begin_year"] == 1925
    # a listed station keeps its own position
    p = fmi_station_to_feature(SODANKYLA, served=True, position={"latitude": 1.0,
                                                                 "longitude": 1.0})
    assert p["properties"]["latitude"] == 67.36663
