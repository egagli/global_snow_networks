"""
Offline tests for the CEAZA-Met network (issue #42).

Station and sensor dicts have the shape ``CEAZAClient.get_all_stations``
and ``get_sensors`` return (easysnowdata 0.3.3).  The sensor codes and
periods are the ones the client documents for El Tapado and Llano de Las
Liebres; no request to CEAZA-Met was made for these tests.
"""

from datetime import date, timedelta

import pandas as pd

import scripts.get_all_stations_data as gasd
from scripts.create_all_stations_geojson import (
    UNIVERSAL_FIELDS,
    ceaza_station_to_feature,
)
from scripts.get_all_stations_data import (
    RefreshStats,
    RevisionLog,
    StationWriter,
    ceaza_despike,
    ceaza_request_windows,
    ceaza_sensor_plan,
    read_station_csv,
    refresh_ceaza,
    write_csv_atomically,
)

EL_TAPADO = {
    "station_id": "TPF", "name": "El Tapado", "latitude": -30.15,
    "longitude": -69.93, "elevation_m": 4306.0, "network": "CEAZA-Met",
    "owner": "ceaza",
    "operator": "Centro de Estudios Avanzados en Zonas Áridas (CEAZA)",
    "begin_date": "2011-05-01", "end_date": "2026-10-20", "status": "Active",
    "measurement_method": "automatic snow depth; automatic SWE sensor",
    "station_url": "https://www.ceazamet.cl/index.php?pag=mod_estacion&e_cod=TPF&p_cod=ceazamet",
}


def _sen(sensor, variable, begin, end, height=2.0):
    return {"station_id": "TPF", "sensor": sensor, "variable": variable,
            "name": "n", "units": "u", "height_m": height,
            "begin_date": begin, "end_date": end}


TAPADO_SENSORS = [
    _sen("TPFSNOWD", "ani_cm", "2011-05-01", "2023-10-25"),
    _sen("TPFANSHM31", "ani_cm", "2018-03-01", "2026-10-20"),
    _sen("TPFSWE_K", "ae_mm", "2014-04-01", "2025-11-03"),
    _sen("TPFSWE_TL", "ae_mm", "2014-04-01", "2025-10-30"),
    _sen("TPFTA", "ta_c", "2011-05-01", "2026-10-20"),
]


def test_plan_uses_newest_sensor_and_older_only_before_it():
    plan = ceaza_sensor_plan(TAPADO_SENSORS)
    assert plan["snwd"] == [
        {"sensor": "TPFANSHM31", "variable": "ani_cm",
         "from": "2018-03-01", "to": "2026-10-20"},
        {"sensor": "TPFSNOWD", "variable": "ani_cm",
         "from": "2011-05-01", "to": "2018-02-28"},
    ]
    # Paired SWE sensors over the same period: one is used, never both.
    assert [s["sensor"] for s in plan["swe"]] == ["TPFSWE_K"]
    assert set(plan) == {"snwd", "swe"}          # air temperature not archived


def test_snow_weight_counts_as_swe():
    plan = ceaza_sensor_plan([
        _sen("LLLSW1Wins1h", "pnkg", "2019-01-01", "2026-10-20"),
        _sen("LLLSW2Wins1h", "pnkg", "2019-01-01", "2026-10-19"),
    ])
    assert [s["sensor"] for s in plan["swe"]] == ["LLLSW1Wins1h"]


def test_feature_schema_attribution_and_plan():
    p = ceaza_station_to_feature(EL_TAPADO, TAPADO_SENSORS)["properties"]
    assert set(UNIVERSAL_FIELDS) <= set(p)
    assert p["code"] == "CEAZA-TPF" and p["ceaza_station_id"] == "TPF"
    assert p["client"] == "ceaza" and p["state"] == "CL"
    assert "Datos provistos por el laboratorio de meteorología CEAZA" in p["notes"]
    assert "www.ceazamet.cl" in p["notes"]
    assert p["has_daily_swe"] and p["has_daily_snwd"]
    assert p["daily_provenance"] == "native"
    assert p["ceaza_sensor_plan"]["swe"][0]["sensor"] == "TPFSWE_K"
    names = {dv["name"] for dv in p["data_variables"]}
    assert "ani_cm:TPFSNOWD" in names and "ta_c:TPFTA" in names


class _FakeClient:
    """Stands in for CEAZAClient; records every series request."""

    calls: list[dict]
    users: list[str | None]

    def __init__(self, user=None):
        _FakeClient.users.append(user)

    def get_sensors(self, station_id):
        return TAPADO_SENSORS

    def get_data(self, station_ids, variables, sensors, interval, begin_date, end_date):
        _FakeClient.calls.append({"sensor": sensors[0], "from": begin_date,
                                  "to": end_date})
        days = pd.date_range(begin_date, end_date).strftime("%Y-%m-%d")
        vtype = "snwd" if variables[0] == "ani_cm" else "swe"
        return [{"station_id": "TPF", "date": d, "type": vtype, "value": 10.0,
                 "sensor": sensors[0]} for d in days[-5:]]


def _writer(tmp_path, feats):
    return StationWriter(
        features=feats, data_dir=tmp_path, refreshed_at_utc="t",
        stats=RefreshStats(), revisions=RevisionLog(detected_utc="t"),
    )


def test_refresh_is_skipped_without_a_registered_user(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CEAZA_USER", raising=False)
    monkeypatch.setattr(gasd, "CEAZAClient", _FakeClient)
    _FakeClient.users, _FakeClient.calls = [], []
    feats = [ceaza_station_to_feature(EL_TAPADO, TAPADO_SENSORS)]
    refresh_ceaza([(0, "CEAZA-TPF")], _writer(tmp_path, feats))
    assert "CEAZA_USER is not set" in capsys.readouterr().out
    assert _FakeClient.users == [] and not list(tmp_path.iterdir())


def test_first_refresh_is_full_then_a_90_day_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("CEAZA_USER", "someone@example.org")
    monkeypatch.setattr(gasd, "CEAZAClient", _FakeClient)
    _FakeClient.users, _FakeClient.calls = [], []
    feats = [ceaza_station_to_feature(EL_TAPADO, TAPADO_SENSORS)]
    refresh_ceaza([(0, "CEAZA-TPF")], _writer(tmp_path, feats),
                  today=date(2026, 10, 21))
    assert _FakeClient.users == ["someone@example.org"]
    first = {}
    for c in _FakeClient.calls:
        first[c["sensor"]] = min(first.get(c["sensor"], c["from"]), c["from"])
    assert first == {
        "TPFANSHM31": "2018-03-01", "TPFSNOWD": "2011-05-01",
        "TPFSWE_K": "2014-04-01",
    }
    assert max(c["to"] for c in _FakeClient.calls) == "2026-10-20"
    # CEAZA-Met allows at most one year per request.
    for c in _FakeClient.calls:
        span = date.fromisoformat(c["to"]) - date.fromisoformat(c["from"])
        assert span < timedelta(days=365)

    # Second night: an archived station asks only for the last 90 days,
    # and the older history in the CSV is kept (merge).
    write_csv_atomically(tmp_path / "CEAZA-TPF.csv", pd.DataFrame({
        "date": pd.date_range("2011-05-01", "2026-10-15").strftime("%Y-%m-%d"),
        "wteq_cm": None, "snwd_cm": 5.0,
    }))
    _FakeClient.calls = []
    refresh_ceaza([(0, "CEAZA-TPF")], _writer(tmp_path, feats),
                  today=date(2026, 10, 21))
    assert {c["sensor"] for c in _FakeClient.calls} == {"TPFANSHM31"}
    assert _FakeClient.calls[0]["from"] == "2026-07-22"
    csv = read_station_csv(tmp_path / "CEAZA-TPF.csv")
    assert csv["date"].iloc[0] == "2011-05-01" and csv["date"].iloc[-1] == "2026-10-20"


def test_request_windows_cover_the_range_a_year_at_a_time():
    wins = ceaza_request_windows("2011-05-01", "2026-10-20")
    assert wins[0][0] == "2011-05-01" and wins[-1][1] == "2026-10-20"
    for (b, e), (nb, _) in zip(wins, wins[1:]):
        assert date.fromisoformat(nb) == date.fromisoformat(e) + timedelta(days=1)
    assert all(
        (date.fromisoformat(e) - date.fromisoformat(b)).days < 365 for b, e in wins
    )
    assert ceaza_request_windows("2026-10-20", "2026-10-20") == [
        ("2026-10-20", "2026-10-20")
    ]


def _frame(depths, swe=None, start="2026-07-01"):
    days = pd.date_range(start, periods=len(depths)).strftime("%Y-%m-%d")
    return pd.DataFrame({"date": days,
                         "wteq_cm": swe if swe is not None else [None] * len(depths),
                         "snwd_cm": depths})


def test_despike_drops_isolated_days_and_keeps_level_shifts():
    # El Jote-like 13 m spike, and a 60 cm storm that stays.
    out = ceaza_despike(_frame([40, 41, 1300, 42, 43, 103, 101, 100]))
    assert out["snwd_cm"].isna().tolist() == [
        False, False, True, False, False, False, False, False
    ]
    # Downward dropout (sensor reads ground) is a spike too.
    out = ceaza_despike(_frame([80, 81, 2, 82, 83]))
    assert pd.isna(out["snwd_cm"].iloc[2])
    # SWE uses its own, tighter bound.
    out = ceaza_despike(_frame([10] * 5, swe=[20, 21, 45, 22, 23]))
    assert pd.isna(out["wteq_cm"].iloc[2]) and out["snwd_cm"].notna().all()


def test_despike_does_not_judge_across_a_date_gap():
    df = _frame([40, 41, 1300, 42])
    df.loc[3, "date"] = "2026-07-10"          # neighbour is 7 days later
    assert ceaza_despike(df)["snwd_cm"].iloc[2] == 1300


def test_merge_night_despikes_a_spike_left_on_the_previous_edge(tmp_path, monkeypatch):
    """Night 1 ended on a spike (no right neighbour yet); night 2 drops it."""
    monkeypatch.setenv("CEAZA_USER", "someone@example.org")

    class Client(_FakeClient):
        def get_data(self, station_ids, variables, sensors, interval,
                     begin_date, end_date):
            if variables[0] != "ani_cm":
                return []
            return [{"station_id": "TPF", "date": "2026-10-20", "type": "snwd",
                     "value": 50.0, "sensor": sensors[0]}]

    monkeypatch.setattr(gasd, "CEAZAClient", Client)
    Client.users, Client.calls = [], []
    write_csv_atomically(tmp_path / "CEAZA-TPF.csv", _frame(
        [48, 49, 900], start="2026-10-17"))
    feats = [ceaza_station_to_feature(EL_TAPADO, TAPADO_SENSORS)]
    refresh_ceaza([(0, "CEAZA-TPF")], _writer(tmp_path, feats),
                  today=date(2026, 10, 21))
    csv = read_station_csv(tmp_path / "CEAZA-TPF.csv").set_index("date")
    assert pd.isna(csv.loc["2026-10-19", "snwd_cm"])
    assert csv.loc["2026-10-17", "snwd_cm"] == 48
    assert csv.loc["2026-10-20", "snwd_cm"] == 50
