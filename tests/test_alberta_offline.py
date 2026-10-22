"""
Offline tests for the Alberta River Basins network (issue #40).

Station dicts have the shape ``AlbertaClient.get_all_stations`` returns
(easysnowdata 0.3.3; values from the live catalog on 2026-10-21).
"""

import pandas as pd

import scripts.get_all_stations_data as gasd
from scripts.create_all_stations_geojson import (
    UNIVERSAL_FIELDS,
    alberta_mirror_client,
    alberta_station_to_feature,
    annotate_possible_duplicates,
    link_alberta_mirrors,
    make_feature,
    prune_non_daily_duplicates,
)
from scripts.get_all_stations_data import (
    RefreshStats,
    RevisionLog,
    StationWriter,
    is_fetch_candidate,
    read_station_csv,
    refresh_alberta,
)

AEPA_PILLOW = {
    "station_id": "05BB803", "name": "Sunshine Village",
    "latitude": 51.0775, "longitude": -115.7825, "elevation_m": None,
    "status": "Active", "source_status": "Active", "state": "AB",
    "operator": "Alberta Environment and Protected Areas",
    "operator_code": "EPA",
    "measurement_method": (
        "automated SWE (snow pillow); automated snow depth; manual snow survey"
    ),
    "has_swe": True, "has_snwd": True, "has_pillow_swe": True,
    "has_survey": True, "begin_date": "1970-02-25", "end_date": "2026-10-21",
    "parameters": ["PC", "SD", "SD-SS", "SW", "SW-SS", "TA"],
    "station_url": "https://rivers.alberta.ca/?StationNumber=05BB803",
}
MSC_DEPTH = {
    **AEPA_PILLOW, "station_id": "3031875", "name": "Banff CS",
    "operator": "MSC", "operator_code": "MSC", "status": "Inactive",
    "has_swe": False, "has_pillow_swe": False, "has_survey": False,
    "parameters": ["SD", "TA"],
}
BC_MIRROR = {
    **AEPA_PILLOW, "station_id": "2C09Q", "name": "Morrissey Ridge",
    "latitude": 49.4483, "longitude": -114.8, "state": "BC",
    "operator": "British Columbia Snow Survey Network", "operator_code": "BC",
    "parameters": ["SD", "SW"],
}
UNNAMED_BC_PILLOW = {
    **BC_MIRROR, "station_id": "4A30P", "name": "4A30P", "operator": None,
    "operator_code": None,
}
SNOTEL_MIRROR = {
    **AEPA_PILLOW, "station_id": "13A19S", "name": "Flattop Mountain",
    "state": "MT", "operator": "USDA NRCS SNOTEL", "operator_code": "Snotel",
    "parameters": ["SD", "SW", "TA"],
}
AEPA_COURSE = {
    **AEPA_PILLOW, "station_id": "05BB801", "name": "Sunshine Village (course)",
    "has_pillow_swe": False, "parameters": ["SD-SS", "SW-SS"],
}


def test_mirror_detection_by_suffix_and_by_state():
    assert alberta_mirror_client(AEPA_PILLOW) is None
    assert alberta_mirror_client(MSC_DEPTH) is None
    assert alberta_mirror_client(BC_MIRROR) == "databc"
    assert alberta_mirror_client(UNNAMED_BC_PILLOW) == "databc"
    assert alberta_mirror_client(SNOTEL_MIRROR) == "awdb"


def test_aepa_pillow_is_an_archived_daily_station():
    p = alberta_station_to_feature(AEPA_PILLOW)["properties"]
    assert set(UNIVERSAL_FIELDS) <= set(p)
    assert p["client"] == "alberta" and p["network_code"] == "ARB"
    assert p["operator"] == "Alberta Environment and Protected Areas"
    assert p["has_daily_swe"] and p["has_daily_snwd"]
    assert p["daily_provenance"] == "native"      # AEPA's own A.DayMean
    assert "no licence" in p["notes"]
    intervals = {dv["name"]: dv["interval"] for dv in p["data_variables"]}
    assert intervals == {
        "PC": "daily", "SD": "daily", "SD-SS": "periodic", "SW": "daily",
        "SW-SS": "periodic", "TA": "sub_hourly",
    }
    assert is_fetch_candidate(p)


def test_alberta_codes_are_namespaced():
    # Alberta's numbers are other agencies' codes ("2C09Q" is DataBC's);
    # the archive code must not collide with them.
    p = alberta_station_to_feature(BC_MIRROR)["properties"]
    assert p["code"] == "ARB-" + p["alberta_station_id"]
    assert p["code"] != BC_MIRROR["station_id"]


def test_other_operators_depth_station_is_archived_with_unexpanded_operator():
    p = alberta_station_to_feature(MSC_DEPTH)["properties"]
    assert p["operator"] is None and p["operator_code"] == "MSC"
    assert p["has_daily_snwd"] and not p["has_daily_swe"]


def test_mirrors_are_inventory_only():
    for station in (BC_MIRROR, UNNAMED_BC_PILLOW, SNOTEL_MIRROR):
        p = alberta_station_to_feature(station)["properties"]
        assert not p["daily_or_better"] and not p["has_daily_swe"]
        assert p["daily_provenance"] == "none"
        assert "not archived twice" in p["notes"]
        assert not is_fetch_candidate(p)


def test_snow_course_is_periodic():
    p = alberta_station_to_feature(AEPA_COURSE)["properties"]
    assert not p["daily_or_better"]


def _twin(client, code, lat, lon, name, daily=True):
    return make_feature(lon, lat, {
        "code": code, "client": client, "name": name, "latitude": lat,
        "longitude": lon, "daily_or_better": daily,
    })


def test_mirror_of_survives_the_duplicate_pruning():
    feats = [
        alberta_station_to_feature(BC_MIRROR),
        _twin("databc", "2C09Q", 49.4483, -114.8001, "Morrissey Ridge"),
        alberta_station_to_feature(SNOTEL_MIRROR),
        _twin("awdb", "482_MT_SNTL", 51.0776, -115.7824, "Flattop Mtn"),
        alberta_station_to_feature(AEPA_PILLOW),
    ]
    # The SNOTEL mirror shares the AEPA pillow's coordinates in this fixture
    # (it copies AEPA_PILLOW); only its AWDB twin may become mirror_of.
    annotate_possible_duplicates(feats)
    assert link_alberta_mirrors(feats) == 2
    prune_non_daily_duplicates(feats)
    bc, snotel = feats[0]["properties"], feats[2]["properties"]
    assert [d["code"] for d in bc["mirror_of"]] == ["2C09Q"]
    assert [d["client"] for d in snotel["mirror_of"]] == ["awdb"]
    assert not bc["possible_duplicates"]       # pruned: mirror is periodic
    assert "mirror_of" not in feats[4]["properties"]


def test_databc_twin_matched_by_code_without_coordinates_match():
    feats = [
        alberta_station_to_feature(BC_MIRROR),
        _twin("databc", "2C09Q", 55.0, -120.0, "Morrissey Ridge"),  # far away
    ]
    annotate_possible_duplicates(feats)
    link_alberta_mirrors(feats)
    assert feats[0]["properties"]["mirror_of"] == [
        {"code": "2C09Q", "client": "databc", "distance_m": None}
    ]


class _FakeClient:
    """Stands in for AlbertaClient: one station's daily records."""

    def get_data(self, station_ids, **kwargs):
        assert kwargs["interval"] == "daily"
        days = pd.date_range("1984-03-12", periods=60, freq="D")
        return [
            {"station_id": station_ids[0], "date": d.strftime("%Y-%m-%d"),
             "variable": "SW", "type": "swe", "value": 1.0 + i / 10,
             "units": "cm", "interval": "daily"}
            for i, d in enumerate(days)
        ]


def test_refresh_alberta_writes_full_record(tmp_path, monkeypatch):
    monkeypatch.setattr(gasd, "AlbertaClient", _FakeClient)
    feats = [alberta_station_to_feature(AEPA_PILLOW)]
    w = StationWriter(
        features=feats, data_dir=tmp_path, refreshed_at_utc="t",
        stats=RefreshStats(), revisions=RevisionLog(detected_utc="t"),
    )
    refresh_alberta([(0, "ARB-05BB803", "05BB803")], w)
    csv = read_station_csv(tmp_path / "ARB-05BB803.csv")
    assert len(csv) == 60 and csv["date"].iloc[0] == "1984-03-12"
    p = feats[0]["properties"]
    assert p["daily_verified"] and p["daily_or_better"]
    assert p["daily_provenance"] == "native"
