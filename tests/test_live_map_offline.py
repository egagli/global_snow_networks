# -*- coding: utf-8 -*-
"""
Offline unit tests for the map generator — no network access.

The generator's frontend config is a Python dict that the browser reads back
out of the embedded ``MAP_META`` payload, so a renamed key breaks silently in
the browser and nowhere else (DESIGN.md §9).  These tests pin that seam.
"""

import json
import re

import pytest

from scripts.generate_live_map import (
    IMAGERY_CONFIG,
    _HTML_TEMPLATE,
    _vars_by_interval,
    build_html,
)


# ── Context imagery config (DESIGN.md §8) ────────────────────────────────────

def test_imagery_config_shape():
    cfg = IMAGERY_CONFIG
    required = {
        "enabled", "collection", "collection_label", "search_url",
        "render_url", "item_url", "credit", "credit_url",
        "recent_window_days", "clearest_window_days", "empty_window_days",
        "search_limit", "max_scenes", "chip_max_size", "chip_hires_max_size",
        "thumb_max_size", "chip_aspect", "extents_km", "default_extent_km",
        "default_render", "renders", "search_fields",
    }
    assert required <= set(cfg)
    assert cfg["default_render"] in cfg["renders"]
    assert cfg["default_extent_km"] in cfg["extents_km"]
    assert cfg["extents_km"] == sorted(cfg["extents_km"])
    # The widened fallback must actually widen, else polar-night stations
    # would re-run the identical search.
    assert cfg["empty_window_days"] > cfg["recent_window_days"]
    assert cfg["search_limit"] >= cfg["max_scenes"]
    assert cfg["chip_hires_max_size"] > cfg["chip_max_size"] > cfg["thumb_max_size"]
    assert cfg["item_url"].endswith(f"/{cfg['collection']}/items")


def test_imagery_renders_are_urlsearchparams_pairs():
    """The frontend feeds these straight to URLSearchParams."""
    for key, render in IMAGERY_CONFIG["renders"].items():
        assert render["label"]
        pairs = render["params"]
        assert isinstance(pairs, list) and pairs
        for pair in pairs:
            assert isinstance(pair, list) and len(pair) == 2, (key, pair)
            assert all(isinstance(v, str) for v in pair), (key, pair)
        keys = [k for k, _ in pairs]
        # Three bands, and one rescale per band in the same order — titiler
        # pairs them positionally, so a missing rescale silently mis-stretches.
        assert keys.count("assets") == 3, key
        assert keys.count("rescale") == 3, key


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
    assert parsed["imagery"]["renders"][IMAGERY_CONFIG["default_render"]]["params"]


def test_build_html_survives_missing_imagery_config(offline_assets):
    """Imagery is a decoration; a map built without it must still render."""
    meta = {k: v for k, v in MAP_META_STUB.items() if k != "imagery"}
    html = build_html(meta, STATION_STUB, [])
    assert 'id="imagery-section"' in html
    assert '"imagery"' not in html.split("const SD =")[0].split("const MAP_META = ")[1]


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
