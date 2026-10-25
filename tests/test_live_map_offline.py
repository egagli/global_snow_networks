# -*- coding: utf-8 -*-
"""
Offline unit tests for the map generator — no network access.

The generator's frontend config is a Python dict that the browser reads back
out of the embedded ``MAP_META`` payload, so a renamed key breaks silently in
the browser and nowhere else (DESIGN.md §9).  These tests pin that seam.
"""

import json
import re

import numpy as np
import pandas as pd
import pytest

from scripts.generate_live_map import (
    IMAGERY_CONFIG,
    _HTML_TEMPLATE,
    _camera_snapshot_url,
    _vars_by_interval,
    build_html,
    process_station_from_csv,
    station_hemisphere,
)


# ── Context imagery config (DESIGN.md §8) ────────────────────────────────────

def test_imagery_config_shape():
    cfg = IMAGERY_CONFIG
    required = {
        "enabled", "collection", "collection_label", "search_url",
        "render_url", "stats_url", "item_url", "credit", "credit_url",
        "recent_window_days", "clearest_window_days", "empty_window_days",
        "search_limit", "max_scenes", "chip_max_size", "chip_hires_max_size",
        "thumb_max_size", "chip_aspect", "extent_min_km", "extent_max_km",
        "extent_step_km", "default_extent_km", "default_render", "renders",
        "search_fields", "stats_percentiles", "stats_max_size",
        "stats_min_span",
    }
    assert required <= set(cfg)
    assert cfg["default_render"] in cfg["renders"]
    assert cfg["extent_min_km"] >= 1
    assert cfg["extent_min_km"] <= cfg["default_extent_km"] <= cfg["extent_max_km"]
    # The default must land on a step, else the slider snaps somewhere else
    # the moment it is touched.
    span = cfg["default_extent_km"] - cfg["extent_min_km"]
    assert span % cfg["extent_step_km"] == 0
    # Two percentiles, low then high — the frontend unpacks them positionally.
    assert len(cfg["stats_percentiles"]) == 2
    assert cfg["stats_percentiles"][0] < cfg["stats_percentiles"][1]
    # The widened fallback must actually widen, else polar-night stations
    # would re-run the identical search.
    assert cfg["empty_window_days"] > cfg["recent_window_days"]
    assert cfg["search_limit"] >= cfg["max_scenes"]
    assert cfg["chip_hires_max_size"] > cfg["chip_max_size"] > cfg["thumb_max_size"]
    assert cfg["item_url"].endswith(f"/{cfg['collection']}/items")


def test_imagery_renders_declare_three_bands_and_a_fallback():
    """Renders are RGB triples; titiler pairs bands and rescales positionally,
    so a fallback of the wrong length silently mis-stretches a chip."""
    for key, render in IMAGERY_CONFIG["renders"].items():
        assert render["label"], key
        assert "colour" not in render["label"].lower(), key
        assert len(render["bands"]) == 3, key
        assert all(isinstance(b, str) for b in render["bands"]), key
        assert render["stretch"] in ("per_band", "common"), key
        fallback = render["fallback_rescale"]
        assert len(fallback) == 3, key
        for lo, hi in fallback:
            assert hi > lo >= 0, (key, lo, hi)
        assert render["color_formula"], key


def test_swir_stretches_bands_together():
    """Snow-vs-cloud in the SWIR render is a ratio between bands. A per-band
    stretch re-brightens SWIR and destroys the distinction, so this pairing is
    load-bearing, not stylistic."""
    swir = IMAGERY_CONFIG["renders"]["swir"]
    assert swir["bands"] == ["B11", "B08", "B04"]
    assert swir["stretch"] == "common"


def test_imagery_search_fields_keep_geometry():
    """The frontend rejects granules that only clip the chip, so it needs
    the footprint and the datetime/cloud properties it displays."""
    include = IMAGERY_CONFIG["search_fields"]["include"]
    assert "geometry" in include
    assert "id" in include
    for prop in ("datetime", "eo:cloud_cover", "platform"):
        assert f"properties.{prop}" in include
    assert "assets" in IMAGERY_CONFIG["search_fields"]["exclude"]


def test_frontend_only_reads_config_keys_that_exist():
    """Every ``IMG_CFG.<key>`` in the template must exist in the dict."""
    referenced = set(re.findall(r"IMG_CFG\.([A-Za-z_][A-Za-z0-9_]*)", _HTML_TEMPLATE))
    assert referenced, "imagery frontend disappeared from the template"
    missing = referenced - set(IMAGERY_CONFIG)
    assert not missing, f"template reads unknown imagery config keys: {missing}"


def test_imagery_is_optional_at_runtime():
    """A build with imagery disabled must still leave the frontend inert
    rather than throwing on a missing config."""
    assert "IMG_CFG.enabled" in _HTML_TEMPLATE
    assert "MAP_META.imagery || {enabled: false}" in _HTML_TEMPLATE


# ── build_html ───────────────────────────────────────────────────────────────

@pytest.fixture
def offline_assets(monkeypatch):
    """Stub the CDN fetch so build_html never touches the network."""
    monkeypatch.setattr(
        "scripts.generate_live_map._build_frontend_asset_tags",
        lambda: {"leaflet_css": "<style></style>", "leaflet_js": "<script></script>",
                 "plotly_js": "<script></script>"},
    )


MAP_META_STUB = {
    "generated": "2026-01-01T00:00:00+00:00",
    "today_date": "2026-01-01",
    "today_dowy": 93,
    "current_wy": 2026,
    "min_years": 10,
    "n_stations": 1,
    "available_networks": ["SNTL"],
    "imagery": IMAGERY_CONFIG,
}

STATION_STUB = {
    "679_WA_SNTL": {
        "lat": 46.78266, "lon": -121.74767, "name": "Paradise", "net": "SNTL",
        "mtype": "automated", "wy": {}, "stat": {},
    }
}


def test_build_html_embeds_imagery_config(offline_assets):
    html = build_html(MAP_META_STUB, STATION_STUB, [])

    # No placeholder survives substitution.
    for token in ("__MAP_META__", "__STATION_DATA__", "__PERIODIC_DATA__"):
        assert token not in html

    # The panel container and the config both made it through.
    assert 'id="imagery-section"' in html
    assert IMAGERY_CONFIG["search_url"] in html
    assert IMAGERY_CONFIG["render_url"] in html
    assert IMAGERY_CONFIG["credit"] in html

    # MAP_META must round-trip as JSON the browser can parse.
    payload = re.search(r"const MAP_META = (\{.*?\});\n", html, re.DOTALL).group(1)
    parsed = json.loads(payload)
    assert parsed["imagery"]["collection"] == IMAGERY_CONFIG["collection"]
    assert parsed["imagery"]["renders"][IMAGERY_CONFIG["default_render"]]["bands"]


def test_build_html_survives_missing_imagery_config(offline_assets):
    """Imagery is a decoration; a map built without it must still render."""
    meta = {k: v for k, v in MAP_META_STUB.items() if k != "imagery"}
    html = build_html(meta, STATION_STUB, [])
    assert 'id="imagery-section"' in html
    assert '"imagery"' not in html.split("const SD =")[0].split("const MAP_META = ")[1]


# ── live cameras ─────────────────────────────────────────────────────────────

def test_camera_snapshot_url_from_slider_page():
    slider = ("https://pvs.nupointsystems.com/api/photo-slider-by-nsn"
              "?pass=%F3uq%2C%F7#images-1")
    assert _camera_snapshot_url(slider) == (
        "https://pvs.nupointsystems.com/latest.php?pass=%F3uq%2C%F7")
    latest = "https://pvs.nupointsystems.com/latest.php?pass=x"
    assert _camera_snapshot_url(latest) == latest
    assert _camera_snapshot_url("https://example.com/cam?pass=x") == ""
    assert _camera_snapshot_url("") == ""


# ── variable inventory grouping ──────────────────────────────────────────────

def test_vars_by_interval_orders_and_keeps_everything():
    groups = _vars_by_interval([
        {"name": "WTEQ", "interval": "daily"},
        {"name": "SNWD", "interval": "daily"},
        {"name": "TOBS", "interval": "hourly"},
        {"name": "SNOW_LINE", "interval": "made_up_interval"},
        {"name": "PREC", "interval": None},
    ])
    labels = [g[0] for g in groups]
    assert labels[:2] == ["Daily", "Hourly"]
    assert dict(groups)["Daily"] == "SNWD, WTEQ"
    # Unknown intervals render last under their raw name instead of vanishing.
    assert "made_up_interval" in labels
    assert "unknown" in labels


# ── Water years by hemisphere ────────────────────────────────────────────────

def _season_csv(tmp_path, name, start, end, peak_month):
    """Daily SWE for 2010–2026: a tent-shaped season peaking in *peak_month*."""
    days = pd.date_range(start, end)
    swe = np.where(
        days.month.isin(_season_months(peak_month)),
        50.0 - 5.0 * np.abs(days.month - peak_month),
        0.0,
    )
    path = tmp_path / f"{name}.csv"
    pd.DataFrame({
        "date": days.strftime("%Y-%m-%d"), "wteq_cm": swe, "snwd_cm": None,
    }).to_csv(path, index=False)
    return path


def _season_months(peak):
    return [(peak - 1 + k) % 12 + 1 for k in range(-4, 5)]


def _meta(lat):
    return {"lat": lat, "lon": -70.0, "name": "x", "network_code": "T"}


def test_station_hemisphere_from_latitude():
    assert station_hemisphere(-33.0) == "southern"
    assert station_hemisphere(46.8) == "northern"
    assert station_hemisphere(None) == "northern"


def test_southern_station_uses_an_april_water_year(tmp_path):
    """A southern season (May–Dec here) lands in ONE water year, DOWY 1 = 1 April."""
    path = _season_csv(tmp_path, "S", "2010-04-01", "2026-10-20", peak_month=8)
    out = process_station_from_csv(
        "S", path, _meta(-33.0), today=pd.Timestamp("2026-10-21"),
    )
    assert out["hem"] == "S"
    # WY2026 = 2026-04-01 … 2027-03-31; today is DOWY 204 of it.
    assert list(out["wy"]) == ["2026"]
    d = out["wy"]["2026"]["wteq"]["d"]
    assert d[0] == 1                                   # 1 April 2026
    assert d[-1] == 203                                # 20 October 2026
    # The whole 2026 season is one continuous run of the water year.
    assert d == list(range(1, 204))
    # The season (April–December, peak in August) sits inside one water
    # year: DOWY 1 is 1 April (30 cm), the peak is mid-August, and the
    # snow-free months come at the END of the year (February).
    med = out["pm_por_wteq"]                           # cm ×10
    assert med[0] == 300
    aug15 = (pd.Timestamp("2026-08-15") - pd.Timestamp("2026-04-01")).days
    feb15 = (pd.Timestamp("2027-02-15") - pd.Timestamp("2026-04-01")).days
    assert med[aug15] == 500 and med[feb15] == 0


def test_northern_station_keeps_the_october_water_year(tmp_path):
    path = _season_csv(tmp_path, "N", "2010-10-01", "2026-10-20", peak_month=3)
    out = process_station_from_csv(
        "N", path, _meta(46.8), today=pd.Timestamp("2026-10-21"),
    )
    assert out["hem"] == "N"
    assert list(out["wy"]) == ["2027"]                 # 2026-10-01 … today
    assert out["wy"]["2027"]["wteq"]["d"][0] == 1
    mar15 = (pd.Timestamp("2026-03-15") - pd.Timestamp("2025-10-01")).days
    assert out["pm_por_wteq"][mar15] == 500


def test_southern_station_embeds_both_years_the_date_control_reaches(tmp_path):
    """In May the date control spans the northern WY (from 1 October), which
    for a southern station crosses 1 April: both water years are embedded."""
    path = _season_csv(tmp_path, "S", "2010-04-01", "2027-05-01", peak_month=8)
    out = process_station_from_csv(
        "S", path, _meta(-33.0), today=pd.Timestamp("2027-05-02"),
    )
    assert sorted(out["wy"]) == ["2026", "2027"]


def test_frontend_converts_dates_per_hemisphere():
    """The page keeps one calendar date and converts it per station."""
    for needle in (
        "const WY_START_MONTH = {N: 10, S: 4};",
        "function stationWyDowy(code, dowy, wy)",
        "const loc = stationWyDowy(code, dowy, wy);",
        'dowyToDate(obs.curDowy, obs.curWy, obs.hem)',
    ):
        assert needle in _HTML_TEMPLATE
