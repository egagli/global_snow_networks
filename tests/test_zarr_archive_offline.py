"""
Offline tests for ``scripts/build_zarr_archive.py`` (DESIGN.md §6.5).

Small synthetic CSVs and inventory in a temp dir; the store is written
to disk and read back.  The contract under test: a value in the store
equals the CSV value for that station and date, nothing else is there,
and the two layouts differ only in chunking.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from scripts import build_zarr_archive as bza


def _write_csv(path: Path, rows: list[tuple[str, str, str]]) -> None:
    lines = ["date,wteq_cm,snwd_cm"] + [",".join(r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _feature(code: str, **props) -> dict:
    base = {
        "code": code, "name": f"Station {code}", "network_code": "SNTL",
        "client": "awdb", "operator": "USDA NRCS", "state": "WA",
        "latitude": 47.0, "longitude": -121.0, "elevation_m": 1200.0,
        "daily_provenance": "native",
    }
    base.update(props)
    return {"type": "Feature", "geometry": None, "properties": base}


@pytest.fixture()
def tiny_archive(tmp_path, monkeypatch):
    stations = tmp_path / "stations"
    stations.mkdir()
    # A: SWE and depth, one missing depth, one bad date row, one duplicate date.
    _write_csv(stations / "A.csv", [
        ("2020-10-01", "1.5", "10"),
        ("2020-10-02", "2.0", ""),
        ("not-a-date", "9", "9"),
        ("2020-10-03", "2.5", "12"),
        ("2020-10-03", "2.6", "13"),   # duplicate date: last row wins
    ])
    # B: depth only, starts later, ends later.
    _write_csv(stations / "B.csv", [
        ("2020-10-03", "", "40"),
        ("2020-10-06", "", "44"),
    ])
    # Orphan on disk, not in the inventory: skipped, reported.
    _write_csv(stations / "ORPHAN.csv", [("2020-10-01", "1", "1")])
    # Inventory lists a station with no CSV at all (periodic site): absent.
    inventory = tmp_path / "all_snow_stations.geojson"
    inventory.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            _feature("A"),
            _feature("B", network_code="BCSS", client="databc", operator=None,
                     elevation_m=None, state=None),
            _feature("C"),
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(bza, "INVENTORY", inventory)
    monkeypatch.setattr(bza, "STATIONS_DIR", stations)
    return tmp_path


def test_dataset_matches_csvs(tiny_archive):
    inv = bza.load_inventory(bza.INVENTORY)
    frames, orphans = bza.collect_frames(inv, bza.STATIONS_DIR)
    assert sorted(frames) == ["A", "B"]
    assert orphans == ["ORPHAN"]

    ds = bza.build_dataset(frames, inv)
    assert list(ds["station"].values) == ["A", "B"]
    # Complete daily axis from the earliest to the latest observation.
    assert str(ds["time"].values[0])[:10] == "2020-10-01"
    assert str(ds["time"].values[-1])[:10] == "2020-10-06"
    assert ds.sizes["time"] == 6

    swe_a = ds["swe"].sel(station="A").values
    snd_a = ds["snow_depth"].sel(station="A").values
    np.testing.assert_array_equal(swe_a, np.array([1.5, 2.0, 2.6, np.nan, np.nan, np.nan], "float32"))
    np.testing.assert_array_equal(snd_a, np.array([10, np.nan, 13, np.nan, np.nan, np.nan], "float32"))
    # B has no SWE anywhere and depth on two days only.
    assert np.isnan(ds["swe"].sel(station="B").values).all()
    np.testing.assert_array_equal(
        ds["snow_depth"].sel(station="B").values,
        np.array([np.nan, np.nan, 40, np.nan, np.nan, 44], "float32"),
    )
    assert ds["swe"].dtype == np.float32
    assert ds["swe"].attrs["units"] == "cm"


def test_station_metadata_becomes_coordinates(tiny_archive):
    inv = bza.load_inventory(bza.INVENTORY)
    frames, _ = bza.collect_frames(inv, bza.STATIONS_DIR)
    ds = bza.build_dataset(frames, inv)
    assert list(ds["network_code"].values) == ["SNTL", "BCSS"]
    assert list(ds["client"].values) == ["awdb", "databc"]
    # Nulls: empty string for text, NaN for numbers — never the string "None".
    assert ds["operator"].sel(station="B").item() == ""
    assert np.isnan(ds["elevation_m"].sel(station="B").item())
    assert ds["elevation_m"].sel(station="A").item() == pytest.approx(1200.0)


def test_both_layouts_round_trip_and_differ_only_in_chunks(tiny_archive):
    out = tiny_archive / "archive"
    manifest = bza.build_archive(out, ["by_time", "by_station"])

    assert (out / "archive.json").exists()
    assert set(manifest["stores"]) == {"by_time.zarr", "by_station.zarr"}
    assert manifest["orphan_csvs_skipped"] == ["ORPHAN"]
    assert manifest["n_stations"] == 2

    a = xr.open_zarr(out / "by_time.zarr", consolidated=True)
    b = xr.open_zarr(out / "by_station.zarr", consolidated=True)
    xr.testing.assert_identical(
        a.drop_attrs(deep=False), b.drop_attrs(deep=False)
    )
    # On two stations × six days both layouts collapse to one chunk; the
    # spec itself is covered by test_chunk_sizes_follow_layout_spec.
    for ds, name in ((a, "by_time.zarr"), (b, "by_station.zarr")):
        chunks = manifest["stores"][name]["chunks"]
        assert chunks == {"station": 2, "time": 6}
        assert ds["swe"].encoding["chunks"] == (chunks["station"], chunks["time"])
    assert a.attrs["layout"] == "by_time"
    assert b.attrs["layout"] == "by_station"
    assert a.attrs["units"] == "cm"
    assert (out / "by_time.zarr" / "zarr.json").exists()   # format 3


def test_chunk_sizes_follow_layout_spec():
    ds = xr.Dataset(
        {"swe": (("station", "time"), np.zeros((200, 1000), "float32"))},
        coords={"station": np.arange(200).astype(str), "time": np.arange(1000)},
    )
    assert bza.resolve_chunks(ds, bza.LAYOUTS["by_time"]) == {"station": 200, "time": 366}
    assert bza.resolve_chunks(ds, bza.LAYOUTS["by_station"]) == {"station": 64, "time": 1000}
