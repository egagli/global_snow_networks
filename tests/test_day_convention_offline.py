"""
Offline tests for ``day_convention`` (DESIGN.md §4.1): what one dated value
in a station's CSV is, per network.
"""

from scripts.create_all_stations_geojson import (
    DAY_CONVENTIONS,
    UNIVERSAL_FIELDS,
    day_convention,
    make_feature,
)


def _props(client, network_code=None, state=None):
    return {"client": client, "network_code": network_code, "state": state}


def test_snotel_is_the_pst_midnight_reading_and_alaska_is_yst():
    assert day_convention(_props("awdb", "SNTL", "WA")) == "instant 00:00 UTC-08:00"
    assert day_convention(_props("awdb", "SCAN", "AK")) == "instant 00:00 UTC-09:00"


def test_manual_awdb_networks_are_not_guessed():
    assert day_convention(_props("awdb", "COOP", "WA")) == "unknown"
    assert day_convention(_props("awdb", "MSNT", "AB")) == "unknown"


def test_the_networks_differ_by_clock_time():
    assert day_convention(_props("cdec", "CCSS")) == (
        "instant 05:00 America/Los_Angeles"
    )
    assert day_convention(_props("databc", "BCSS")) == "instant 16:00 UTC"
    assert day_convention(_props("hydroquebec", "HQ")) == (
        "last of day America/Toronto"
    )


def test_slf_depends_on_the_dataset():
    assert day_convention(_props("slf", "IMIS")).startswith("median of 24 h")
    assert day_convention(_props("slf", "BEOB")) == "manual morning reading"


def test_unknown_client_is_unknown():
    assert day_convention(_props("ghcnd")) == "unknown"


def test_make_feature_always_carries_it():
    f = make_feature(-121.7, 46.8, {"code": "X", "client": "yukon"})
    p = f["properties"]
    assert "day_convention" in UNIVERSAL_FIELDS
    assert p["day_convention"] == "mean of day UTC-07:00"
    # A builder that knows better can set it explicitly
    f = make_feature(0, 0, {"code": "Y", "client": "nve", "day_convention": "x"})
    assert f["properties"]["day_convention"] == "x"


def test_every_archived_client_has_a_convention():
    archived = {"awdb", "cdec", "databc", "nve", "yukon", "hydroquebec",
                "alberta", "slf", "snih", "ceaza"}
    named = {key.split(":")[0] for key in DAY_CONVENTIONS}
    assert archived <= named
