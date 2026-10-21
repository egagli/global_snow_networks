"""
Offline tests for the SLF network — IMIS and study plots (issue #44).

Station dicts have the shape ``SLFClient(dataset=...).get_all_stations``
returns (easysnowdata 0.3.3; values from the live listing on 2026-10-21).
"""

import pandas as pd

import scripts.get_all_stations_data as gasd
from scripts.create_all_stations_geojson import (
    UNIVERSAL_FIELDS,
    annotate_possible_duplicates,
    make_feature,
    prune_non_daily_duplicates,
    slf_code,
    slf_station_to_feature,
)
from scripts.get_all_stations_data import (
    RefreshStats,
    RevisionLog,
    StationWriter,
    is_fetch_candidate,
    read_station_csv,
    refresh_slf,
)

_SLF = "WSL Institute for Snow and Avalanche Research SLF"
WFJ2 = {
    "station_id": "WFJ2", "name": "Weissfluhjoch", "latitude": 46.829638,
    "longitude": 9.809289, "elevation_m": 2536.0, "status": "Active",
    "operator": _SLF, "dataset": "imis", "doi": "10.16904/envidat.406",
    "license": "CC-BY-4.0", "network": "IMIS", "station_type": "SNOW_FLAT",
    "measurement_method": (
        "automatic (ultrasonic snow depth); HN_1D modelled by SNOWPACK"
    ),
    "period_of_record": None, "has_swe": False, "has_snwd": True,
    "station_url": "https://measurement-data.slf.ch/imis/",
}
STUDY_5WJ0 = {
    **WFJ2, "station_id": "5WJ0", "latitude": 46.82963, "longitude": 9.809236,
    "dataset": "study_plot", "doi": "10.16904/envidat.408", "network": "BEOB",
    "station_type": None, "measurement_method": "manual daily morning observation",
    "station_url": "https://measurement-data.slf.ch/study-plot/",
}
GCOS_5WJ = {
    **STUDY_5WJ0, "station_id": "5WJ", "dataset": "gcos", "doi": "10.16904/15",
    "license": "ODbL-1.0 (database) + DbCL-1.0 (contents)", "network": "GCOS",
    "has_swe": True,
}


def test_codes_carry_a_dataset_prefix():
    assert slf_code("imis", "WFJ2") == "SLF-IMIS-WFJ2"
    assert slf_code("study_plot", "5WJ0") == "SLF-SP-5WJ0"
    assert slf_code("gcos", "5WJ") == "SLF-GCOS-5WJ"


def test_imis_station_is_a_daily_depth_station():
    p = slf_station_to_feature(WFJ2)["properties"]
    assert set(UNIVERSAL_FIELDS) <= set(p)
    assert p["code"] == "SLF-IMIS-WFJ2" and p["slf_station_id"] == "WFJ2"
    assert p["client"] == "slf" and p["slf_dataset"] == "imis"
    assert p["network_code"] == "IMIS" and p["state"] == "CH"
    assert p["has_daily_snwd"] and not p["has_daily_swe"]
    assert p["daily_provenance"] == "native"
    assert "CC-BY-4.0" in p["notes"] and "www.slf.ch" in p["notes"]
    types = {dv["name"]: (dv["type"], dv["interval"]) for dv in p["data_variables"]}
    assert types["HS"] == ("snwd", "daily")
    assert types["HN_1D"] == ("snowfall", "daily")
    assert is_fetch_candidate(p)


def test_study_plot_is_daily_and_gcos_is_periodic_only():
    sp = slf_station_to_feature(STUDY_5WJ0)["properties"]
    assert sp["network_code"] == "BEOB" and sp["has_daily_snwd"]
    gc = slf_station_to_feature(GCOS_5WJ)["properties"]
    assert not gc["daily_or_better"] and not is_fetch_candidate(gc)
    assert {dv["interval"] for dv in gc["data_variables"]} == {"periodic"}


def test_co_located_imis_and_study_plot_are_cross_linked():
    feats = [
        slf_station_to_feature(WFJ2),
        slf_station_to_feature(STUDY_5WJ0),
        slf_station_to_feature(GCOS_5WJ),
        # Same client, same dataset, 30 m away: not a cross-path twin.
        slf_station_to_feature({**WFJ2, "station_id": "WFJ1",
                                "latitude": 46.8299}),
    ]
    annotate_possible_duplicates(feats)
    prune_non_daily_duplicates(feats)
    imis, sp = feats[0]["properties"], feats[1]["properties"]
    assert [d["code"] for d in imis["possible_duplicates"]] == ["SLF-SP-5WJ0"]
    assert [d["code"] for d in sp["possible_duplicates"]] == [
        "SLF-IMIS-WFJ2", "SLF-IMIS-WFJ1",
    ]
    assert not feats[2]["properties"]["possible_duplicates"]  # GCOS: periodic


def test_other_clients_still_link_only_across_clients():
    a = make_feature(9.8, 46.8, {"code": "A", "client": "x", "name": "n",
                                 "latitude": 46.8, "longitude": 9.8})
    b = make_feature(9.8, 46.8, {"code": "B", "client": "x", "name": "n",
                                 "latitude": 46.8, "longitude": 9.8})
    assert annotate_possible_duplicates([a, b]) == 0


class _FakeClient:
    """Stands in for SLFClient; records which dataset and ids were asked."""

    asked: list[tuple[str, str]]

    def __init__(self, dataset):
        self.dataset = dataset

    def get_data(self, station_ids, variables, interval):
        assert variables == ["snwd"] and interval == "daily"
        _FakeClient.asked.append((self.dataset, station_ids[0]))
        days = pd.date_range("1936-09-26", periods=40, freq="D")
        return [
            {"station_id": station_ids[0], "date": d.strftime("%Y-%m-%d"),
             "variable": "HS", "type": "snwd", "value": float(i),
             "units": "cm", "interval": "daily"}
            for i, d in enumerate(days)
        ]


def test_refresh_reads_slf_ids_and_writes_prefixed_codes(tmp_path, monkeypatch):
    monkeypatch.setattr(gasd, "SLFClient", _FakeClient)
    _FakeClient.asked = []
    feats = [slf_station_to_feature(WFJ2), slf_station_to_feature(STUDY_5WJ0)]
    w = StationWriter(
        features=feats, data_dir=tmp_path, refreshed_at_utc="t",
        stats=RefreshStats(), revisions=RevisionLog(detected_utc="t"),
    )
    refresh_slf([(0, "SLF-IMIS-WFJ2"), (1, "SLF-SP-5WJ0")], w)
    assert sorted(_FakeClient.asked) == [("imis", "WFJ2"), ("study_plot", "5WJ0")]
    for code in ("SLF-IMIS-WFJ2", "SLF-SP-5WJ0"):
        csv = read_station_csv(tmp_path / f"{code}.csv")
        assert len(csv) == 40 and csv["wteq_cm"].isna().all()
    assert all(f["properties"]["daily_verified"] for f in feats)
