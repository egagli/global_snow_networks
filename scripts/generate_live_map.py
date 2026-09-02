#!/usr/bin/env python3
"""
Generate the live interactive SWE / Snow Depth percent-of-normal map.

Input: station CSV directory + station geojson
Output: live_swe_map.html + charts/*.json

The frontend template and behavior are kept in parity with the prior
implementation so the map looks and feels the same.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

import numpy as np
import pandas as pd
from utils import day_of_water_year, water_year

REPO_ROOT = Path(__file__).resolve().parent.parent

GEOJSON_PATH = REPO_ROOT / "all_snow_stations.geojson"
CSV_DIR = REPO_ROOT / "data" / "stations"
OUTPUT_HTML = REPO_ROOT / "live_swe_map.html"
CHARTS_DIR = REPO_ROOT / "charts"
ASSET_CACHE_DIR = REPO_ROOT / ".cache" / "live_map_assets"

LEAFLET_CSS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
PLOTLY_JS_URL = "https://cdn.plot.ly/plotly-basic-2.30.0.min.js"

N_DOWY = 366
MIN_YEARS = 10

REF_PERIODS = {
    "por": (None, None),
    "n9120": (1991, 2020),
    "n8110": (1981, 2010),
    "n7100": (1971, 2000),
}

N_PAST_WYS = 0

# ── Context imagery (browser-side, best-effort — DESIGN.md §8) ───────────────
# Microsoft Planetary Computer serves a keyless, CORS-enabled STAC search plus
# a renderer that crops an arbitrary bbox out of a *single* scene at native
# resolution, so a station-scale chip needs no build-time raster work and no
# committed image assets.  Nothing here runs in this script: the browser talks
# to MPC only when a station panel opens, and every failure degrades to a
# message.  Station values, marker colors, and charts never depend on it.
#
# Sentinel-2 L2A only: 10 m, ~5-day revisit, global, free.  HLS needs an
# Earthdata login and Planet is licensed, so neither can be reached from a
# public static page.
#
# Renders are lists of (key, value) pairs — the frontend feeds them to
# URLSearchParams, which encodes them and preserves the repeated `assets` /
# `rescale` keys that titiler needs in band order.
IMAGERY_CONFIG = {
    "enabled": True,
    "collection": "sentinel-2-l2a",
    "collection_label": "Sentinel-2 L2A",
    "search_url": "https://planetarycomputer.microsoft.com/api/stac/v1/search",
    "render_url": (
        "https://planetarycomputer.microsoft.com/api/data/v1/item/bbox"
    ),
    "stats_url": (
        "https://planetarycomputer.microsoft.com/api/data/v1/item/statistics"
    ),
    "item_url": (
        "https://planetarycomputer.microsoft.com/api/stac/v1/collections"
        "/sentinel-2-l2a/items"
    ),
    "credit": "Copernicus Sentinel-2 L2A via Microsoft Planetary Computer",
    "credit_url": "https://planetarycomputer.microsoft.com/dataset/sentinel-2-l2a",
    # Search windows, in days back from the date the slider is on.
    "recent_window_days": 45,
    "clearest_window_days": 90,
    # Polar-night latitudes get no optical acquisitions for months; widen once
    # rather than reporting "no imagery" for every Norwegian station in January.
    "empty_window_days": 400,
    "search_limit": 40,
    "max_scenes": 6,
    "chip_max_size": 640,
    "chip_hires_max_size": 1400,
    "thumb_max_size": 160,
    "chip_aspect": 1.5,
    "extent_min_km": 1,
    "extent_max_km": 30,
    "extent_step_km": 1,
    "default_extent_km": 10,
    "default_render": "true_color",
    # Per-chip contrast. A fixed 0–10000 rescale spends almost the whole
    # output range on brightness the chip does not contain: a 1 km forest
    # chip spans roughly 1100–1800 reflectance and renders as near-black
    # mud. The statistics endpoint returns percentiles for the exact chip,
    # which become the stretch. `stats_min_span` keeps a uniform chip (solid
    # cloud, solid snow) from having its sensor noise stretched to full range.
    "stats_percentiles": [2, 98],
    "stats_max_size": 128,
    "stats_min_span": 500,
    # Thumbnails reuse the selected scene's stretch, loosened by this factor
    # so brighter or darker dates in the strip do not clip to flat white.
    "thumb_stretch_factor": 1.6,
    "renders": {
        # TCI (the `visual` asset) clips hard on snow, so these are raw bands.
        # Per-band percentiles white-balance the chip, which reads far more
        # naturally than one shared stretch (that leaves forest neon green).
        "true_color": {
            "label": "True color",
            "bands": ["B04", "B03", "B02"],
            "stretch": "per_band",
            "fallback_rescale": [[0, 10000], [0, 10000], [0, 10000]],
            "color_formula": "gamma RGB 1.1",
        },
        # Snow is dark in SWIR and bright in NIR; cloud is bright in both.
        # B11/B08/B04 therefore renders snow cyan and cloud white/grey — the
        # single most useful view for "is that snow or weather?". That reading
        # is a *ratio* between the bands, so this render stretches all three
        # together: stretching per band re-brightens SWIR, turns bare ground
        # red, and destroys the very distinction the render exists for.
        "swir": {
            "label": "SWIR false color (snow vs cloud)",
            "bands": ["B11", "B08", "B04"],
            "stretch": "common",
            "fallback_rescale": [[0, 9000], [0, 11000], [0, 11000]],
            "color_formula": "gamma RGB 1.15",
        },
    },
    # STAC fields extension: the full 40-item response is ~600 kB, trimmed
    # to ~20 kB.  `geometry` stays — the frontend uses the granule footprint
    # to skip scenes that only clip the corner of the chip.
    "search_fields": {
        "include": [
            "id", "geometry", "properties.datetime",
            "properties.eo:cloud_cover", "properties.platform",
        ],
        "exclude": ["assets", "links", "stac_extensions", "bbox", "collection"],
    },
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _clean_meta_text(raw) -> str:
    if raw is None:
        return ""
    if pd.isna(raw):
        return ""
    s = str(raw).strip()
    if s.lower() in ("nan", "none", ""):
        return ""
    return s


# Display order/labels for the shared interval enum (clients/_common.py);
# unknown values render last under their raw name rather than disappearing.
_INTERVAL_ORDER = (
    "daily", "sub_daily", "hourly", "sub_hourly", "instantaneous",
    "semi_monthly", "monthly", "annual", "periodic",
)
_INTERVAL_LABELS = {
    "daily": "Daily", "sub_daily": "Sub-daily", "hourly": "Hourly",
    "sub_hourly": "Sub-hourly", "instantaneous": "Instantaneous",
    "semi_monthly": "Semi-monthly", "monthly": "Monthly",
    "annual": "Annual", "periodic": "Periodic",
}


def _vars_by_interval(data_vars) -> list[list[str]]:
    """Group every data_variables entry by interval for the popups.

    Returns ``[[label, "NAME1, NAME2"], ...]`` covering ALL intervals a
    station serves — nothing is filtered out.
    """
    groups: dict[str, set] = {}
    for dv in data_vars or []:
        name = dv.get("name")
        if not name:
            continue
        groups.setdefault(str(dv.get("interval") or "unknown"), set()).add(
            str(name)
        )
    out = []
    for iv in _INTERVAL_ORDER:
        if iv in groups:
            out.append([_INTERVAL_LABELS[iv], ", ".join(sorted(groups.pop(iv)))])
    for iv in sorted(groups):
        out.append([iv, ", ".join(sorted(groups[iv]))])
    return out


def _build_chart_stats(pivot: pd.DataFrame) -> dict[str, list]:
    p10: list[float | None] = []
    p20: list[float | None] = []
    p30: list[float | None] = []
    p40: list[float | None] = []
    p50: list[float | None] = []
    p60: list[float | None] = []
    p70: list[float | None] = []
    p80: list[float | None] = []
    p90: list[float | None] = []
    mins: list[float | None] = []
    maxs: list[float | None] = []
    min_yrs: list[int | None] = []
    max_yrs: list[int | None] = []

    pr = pivot.reindex(range(1, N_DOWY + 1))
    for dowy in range(1, N_DOWY + 1):
        day = pr.loc[dowy].dropna()
        if day.empty:
            p10.append(None)
            p20.append(None)
            p30.append(None)
            p40.append(None)
            p50.append(None)
            p60.append(None)
            p70.append(None)
            p80.append(None)
            p90.append(None)
            mins.append(None)
            maxs.append(None)
            min_yrs.append(None)
            max_yrs.append(None)
            continue

        vals = day.to_numpy(dtype=float)
        years = day.index.to_numpy(dtype=int)

        q = np.quantile(
            vals,
            [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90],
        )
        mn = float(np.min(vals))
        mx = float(np.max(vals))
        mn_year = int(years[np.where(vals == mn)[0][0]])
        mx_year = int(years[np.where(vals == mx)[0][0]])

        p10.append(round(float(q[0]), 3))
        p20.append(round(float(q[1]), 3))
        p30.append(round(float(q[2]), 3))
        p40.append(round(float(q[3]), 3))
        p50.append(round(float(q[4]), 3))
        p60.append(round(float(q[5]), 3))
        p70.append(round(float(q[6]), 3))
        p80.append(round(float(q[7]), 3))
        p90.append(round(float(q[8]), 3))
        mins.append(round(mn, 3))
        maxs.append(round(mx, 3))
        min_yrs.append(mn_year)
        max_yrs.append(mx_year)

    return {
        "p10": p10,
        "p20": p20,
        "p30": p30,
        "p40": p40,
        "p50": p50,
        "p60": p60,
        "p70": p70,
        "p80": p80,
        "p90": p90,
        "mins": mins,
        "maxs": maxs,
        "minYrs": min_yrs,
        "maxYrs": max_yrs,
    }


def _escape_inline_script(js_text: str) -> str:
    return js_text.replace("</script>", "<\\/script>")


def _load_asset_text(url: str, cache_name: str) -> str | None:
    cache_path = ASSET_CACHE_DIR / cache_name
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")

    try:
        with request.urlopen(url, timeout=30) as resp:
            text = resp.read().decode("utf-8")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
        return text
    except Exception as exc:
        logger.warning(f"Could not download frontend asset {url}: {exc}")
        return None


def _build_frontend_asset_tags() -> dict[str, str]:
    leaf_css = _load_asset_text(LEAFLET_CSS_URL, "leaflet.css")
    leaf_js = _load_asset_text(LEAFLET_JS_URL, "leaflet.js")
    plotly_js = _load_asset_text(PLOTLY_JS_URL, "plotly-basic-2.30.0.min.js")

    css_tag = (
        f"<style>\n{leaf_css}\n</style>"
        if leaf_css
        else f'<link rel="stylesheet" href="{LEAFLET_CSS_URL}"/>'
    )
    leaflet_js_tag = (
        f"<script>\n{_escape_inline_script(leaf_js)}\n</script>"
        if leaf_js
        else f'<script src="{LEAFLET_JS_URL}"></script>'
    )
    plotly_js_tag = (
        f"<script>\n{_escape_inline_script(plotly_js)}\n</script>"
        if plotly_js
        else f'<script src="{PLOTLY_JS_URL}"></script>'
    )

    return {
        "leaflet_css": css_tag,
        "leaflet_js": leaflet_js_tag,
        "plotly_js": plotly_js_tag,
    }


def _load_station_csv(csv_path: Path) -> pd.DataFrame | None:
    if not csv_path.exists():
        return None
    try:
        df = pd.read_csv(csv_path)
    except Exception as exc:
        logger.warning(f"Could not read CSV {csv_path.name}: {exc}")
        return None

    if "date" not in df.columns:
        return None

    out = pd.DataFrame()
    out["time"] = pd.to_datetime(df["date"], errors="coerce")
    out["WTEQ"] = pd.to_numeric(df.get("wteq_cm"), errors="coerce")
    out["SNWD"] = pd.to_numeric(df.get("snwd_cm"), errors="coerce")
    out = out.dropna(subset=["time"]).sort_values("time")
    if out.empty:
        return None
    return out


def process_station_from_csv(
    code: str,
    csv_path: Path,
    meta: dict,
    today_dowy: int,
    current_wy: int,
    embed_wys: list[int],
) -> dict | None:
    df = _load_station_csv(csv_path)
    if df is None:
        return None

    idx = df["time"]
    months = idx.dt.month
    years = idx.dt.year
    wy_arr = np.where(months >= 10, years + 1, years)
    wy_start_ts = pd.to_datetime({"year": wy_arr - 1, "month": 10, "day": 1})
    dowy_arr = (idx - pd.DatetimeIndex(wy_start_ts)).dt.days + 1

    df["_wy"] = wy_arr
    df["_dowy"] = dowy_arr
    snow = df[(df["_dowy"] >= 1) & (df["_dowy"] <= N_DOWY)].copy()
    if snow.empty:
        return None

    cur: dict = {}
    for var in ("WTEQ", "SNWD"):
        if var in snow.columns:
            s = snow[var].dropna()
            if not s.empty:
                val = float(s.iloc[-1])
                if not np.isnan(val):
                    cur[var] = {
                        "val": round(val, 4),
                        "date": str(snow.loc[s.index[-1], "time"].date()),
                    }

    stat: dict = {}
    meds: dict = {}
    chart = {"wteq": None, "snwd": None}

    for var in ("WTEQ", "SNWD"):
        vk = var.lower()
        stat[vk] = {}

        if var not in snow.columns:
            for rk in REF_PERIODS:
                stat[vk][rk] = {"pct": None, "n": 0, "med": None}
                meds[f"pm_{rk}_{vk}"] = [0] * N_DOWY
                meds[f"pn_{rk}_{vk}"] = [0] * N_DOWY
            continue

        pivot = snow.pivot_table(
            index="_dowy",
            columns="_wy",
            values=var,
            aggfunc="first",
        )

        for rk, (y0, y1) in REF_PERIODS.items():
            if y0 is None:
                p = pivot
            else:
                cols = [c for c in pivot.columns if y0 <= c <= y1]
                p = pivot[cols] if cols else pd.DataFrame()

            if not p.empty and today_dowy in p.index:
                day = p.loc[today_dowy].dropna()
                n = len(day)
                med = float(day.median()) if n > 0 else None
            else:
                n = 0
                med = None

            cur_val = cur.get(var, {}).get("val")
            if cur_val is not None and med is not None and med > 1e-6:
                pct = round(cur_val / med * 100, 1)
            else:
                pct = None

            stat[vk][rk] = {
                "pct": pct,
                "n": n,
                "med": round(med, 2) if med is not None else None,
            }

            if not p.empty:
                pr = p.reindex(range(1, N_DOWY + 1))
                med_arr = (
                    (pr.median(axis=1) * 10)
                    .clip(0, 32767)
                    .fillna(0)
                    .round()
                    .astype(int)
                    .tolist()
                )
                n_arr = (
                    pr.count(axis=1)
                    .clip(0, 255)
                    .fillna(0)
                    .astype(int)
                    .tolist()
                )
            else:
                med_arr = [0] * N_DOWY
                n_arr = [0] * N_DOWY

            meds[f"pm_{rk}_{vk}"] = med_arr
            meds[f"pn_{rk}_{vk}"] = n_arr

        chart[vk] = _build_chart_stats(pivot)

    wy_data: dict = {}
    for wy in embed_wys:
        wy_df = snow[snow["_wy"] == wy].sort_values("_dowy")
        if wy_df.empty:
            continue
        wy_entry: dict = {}
        for var, vk in (("WTEQ", "wteq"), ("SNWD", "snwd")):
            if var not in wy_df.columns:
                continue
            sub = wy_df[["_dowy", var]].dropna()
            sub = sub[sub[var] >= 0]
            if not sub.empty:
                wy_entry[vk] = {
                    "d": sub["_dowy"].astype(int).tolist(),
                    "v": [round(float(x), 4) for x in sub[var]],
                }
        if wy_entry:
            wy_data[str(wy)] = wy_entry

    network = _clean_meta_text(meta.get("network_code")) or "?"
    state_code = _clean_meta_text(meta.get("state") or "")
    if not state_code and code.count("_") >= 2:
        state_code = code.split("_")[1]

    station_name = _clean_meta_text(meta.get("name") or code)
    vars_by_interval = _vars_by_interval(meta.get("data_variables"))

    obs_cols = [c for c in ("WTEQ", "SNWD") if c in df.columns]
    bdate = ""
    edate = ""
    if obs_cols:
        valid_mask = df[obs_cols].notna().any(axis=1)
        if valid_mask.any():
            valid_times = df.loc[valid_mask, "time"]
            bdate = str(valid_times.min().date())
            edate = str(valid_times.max().date())

    upd = (
        _clean_meta_text(meta.get("latest_record_date"))
        or _clean_meta_text(meta.get("csv_refreshed_at_utc"))
        or _clean_meta_text(meta.get("metadata_fetched_at"))
        or edate
    )

    return {
        "lat": round(meta["lat"], 5),
        "lon": round(meta["lon"], 5),
        "name": station_name,
        "url": _clean_meta_text(meta.get("station_url")),
        "img": _clean_meta_text(meta.get("station_image_url")),
        "net": network,
        "op": _clean_meta_text(meta.get("operator")),
        "cli": _clean_meta_text(meta.get("client")),
        "st": state_code,
        "elev_m": meta.get("elevation"),
        "bdate": bdate,
        "edate": edate,
        "vars": vars_by_interval,
        "upd": upd,
        "mtype": "automated",
        "dp": _clean_meta_text(meta.get("data_provider")),
        "cam": _clean_meta_text(meta.get("station_camera_url")),
        "prov": _clean_meta_text(meta.get("daily_provenance")),
        "dups": meta.get("possible_duplicates") or [],
        "wteq": cur.get("WTEQ", {}).get("val"),
        "snwd": cur.get("SNWD", {}).get("val"),
        "wteq_d": cur.get("WTEQ", {}).get("date"),
        "snwd_d": cur.get("SNWD", {}).get("date"),
        "stat": stat,
        **meds,
        "wy": wy_data,
        "_chart": chart,
    }


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Live SWE Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdn.plot.ly/plotly-basic-2.30.0.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
html,body{height:100%;overflow:hidden}
#app{display:flex;flex-direction:column;height:100%}
#topbar{background:#1a2a3a;color:#eee;padding:6px 12px;display:flex;
  flex-direction:column;align-items:stretch;gap:8px;z-index:1000;flex-shrink:0;position:relative}
#map-title-block{display:flex;flex-direction:column;align-items:center;gap:2px}
#map-title-main{font-weight:650;font-size:16px;line-height:1.2;color:#d0e8ff;text-align:center}
#map-title-sub{font-weight:500;font-size:12px;line-height:1.2;color:#c0d9f3;text-align:center}
#top-controls{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:8px}
.ctl-group{display:flex;align-items:center;gap:4px}
.ctl-label{font-size:11px;color:#aac;white-space:nowrap}
#clock-block{position:absolute;top:6px;right:12px;display:flex;flex-direction:column;align-items:flex-start;gap:2px;
  padding:6px 8px;border:1px solid #3f5165;border-radius:4px;background:rgba(18,34,52,0.72);
  width:fit-content;max-width:min(36vw, 330px)}
#clock-now{font-size:11px;color:#e8f3ff;font-weight:700;line-height:1.25;white-space:nowrap}
#clock-utc{font-size:11px;color:#e8f3ff;font-weight:600;line-height:1.25;
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:nowrap}
#clock-pt{font-size:11px;color:#e8f3ff;font-weight:600;line-height:1.25;
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  white-space:nowrap}
.clock-tz-line{display:flex;align-items:baseline;justify-content:flex-start;gap:0.3ch}
.clock-tz-label{display:inline-block;width:4ch;text-align:right}
#clock-note{font-size:10px;color:#b9d2ea;line-height:1.25;white-space:normal}
select{font-size:11px;padding:3px 5px;border-radius:3px;border:1px solid #446;
       background:#223;color:#eee;cursor:pointer}
select:focus{outline:none;border-color:#4af}
#main-area{display:flex;flex:1;overflow:hidden}
#map{flex:1;min-width:0}
#station-panel{width:560px;max-width:70vw;flex-shrink:0;display:none;flex-direction:column;
               overflow-y:auto;background:#f5f7fa;border-left:2px solid #ccd;
               padding:12px}
#station-panel.visible{display:flex}
#close-btn{align-self:flex-end;background:none;border:none;font-size:20px;
           cursor:pointer;color:#555;line-height:1;padding:0 4px}
#close-btn:hover{color:#000}
#station-info{font-size:13px;line-height:1.6;color:#222}
#station-info h2{font-size:16px;margin-bottom:6px;color:#1a2a3a}
#station-photo-wrap{margin:4px auto 8px;display:flex;flex-direction:column;align-items:flex-start;
                    width:fit-content;max-width:100%}
#station-photo{width:auto;max-width:100%;height:220px;object-fit:contain;border-radius:4px;
               border:1px solid #ccd;display:block}
#station-photo-credit{font-size:10px;color:#666;margin-top:2px}
#station-photo-no-img{font-size:12px;color:#888;font-style:italic;padding:8px 0}
#station-camera-link{margin-top:4px;font-size:12px;font-weight:600}
#station-camera-link a{color:#0b6bcb;text-decoration:none}
#station-camera-link a:hover{text-decoration:underline}
#station-info .info-row{display:flex;gap:4px}
#station-info .info-key{color:#555;min-width:120px;font-weight:500}
#station-info .swe-line{margin:6px 0;padding:6px 8px;border-radius:4px;background:#e8f0fe}
#station-info .snwd-line{margin:6px 0;padding:6px 8px;border-radius:4px;background:#e8fef0}
#station-info .na-line{color:#888;font-style:italic;font-size:12px}
#imagery-section{margin:12px 0 4px;padding:8px;border:1px solid #d3dae4;
                 border-radius:4px;background:#fff}
#imagery-section.imagery-empty{display:none}
.imagery-head{display:flex;align-items:baseline;justify-content:space-between;
              gap:8px;flex-wrap:wrap;margin-bottom:6px}
.imagery-title{font-size:13px;font-weight:650;color:#1a2a3a}
.imagery-controls{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.imagery-controls select{font-size:11px;background:#fff;color:#223;
                         border:1px solid #aab}
.imagery-extent-ctl{display:inline-flex;align-items:center;gap:5px;
  border:1px solid #aab;border-radius:3px;padding:1px 6px;background:#fff}
.imagery-extent-ctl input[type=range]{width:104px;height:14px;cursor:pointer;accent-color:#1a2a3a}
#imagery-extent-val{font-size:11px;color:#223;font-variant-numeric:tabular-nums;
  min-width:5ch;text-align:right}
.imagery-frame{position:relative;width:100%;background:#e9edf2;border-radius:3px;
               overflow:hidden;min-height:60px}
.imagery-frame img{display:block;width:100%;height:auto}
.imagery-frame.is-loading img{opacity:0.35}
/* Ring only, no cross hairs — the lines covered the ground they were
   pointing at, which is the part you actually want to look at. */
.imagery-marker{position:absolute;left:50%;top:50%;width:17px;height:17px;
  transform:translate(-50%,-50%);pointer-events:none;border-radius:50%;
  border:1.5px solid rgba(255,45,45,0.95);
  box-shadow:0 0 2px rgba(0,0,0,0.8), inset 0 0 2px rgba(0,0,0,0.5)}
.imagery-scalebar{position:absolute;left:8px;bottom:8px;height:5px;
  border:1.5px solid rgba(255,255,255,0.95);border-top:none;
  background:rgba(0,0,0,0.30);box-shadow:0 0 2px rgba(0,0,0,0.7)}
.imagery-scalebar span{position:absolute;left:0;bottom:6px;font-size:10px;
  font-weight:700;color:#fff;white-space:nowrap;text-shadow:0 0 3px rgba(0,0,0,0.95)}
.imagery-status{position:absolute;left:0;right:0;top:0;bottom:0;display:flex;
  align-items:center;justify-content:center;text-align:center;padding:10px;
  font-size:12px;color:#556;font-style:italic}
.imagery-caption{font-size:12px;color:#333;margin-top:5px;line-height:1.45}
.imagery-caption a{color:#0b6bcb}
.imagery-note{font-size:11px;color:#8a5a00;margin-top:3px;line-height:1.4}
.imagery-strip{display:flex;gap:5px;margin-top:6px;overflow-x:auto;padding-bottom:2px}
.imagery-thumb{flex:0 0 auto;width:84px;border:2px solid transparent;border-radius:3px;
  background:#eef1f5;padding:0;cursor:pointer;overflow:hidden;text-align:center}
.imagery-thumb img{display:block;width:100%;height:56px;object-fit:cover;background:#dde3ea}
.imagery-thumb.active{border-color:#1a2a3a}
.imagery-thumb .th-date{display:block;font-size:9.5px;line-height:1.25;color:#333;padding:1px 0 0}
.imagery-thumb .th-cloud{display:block;font-size:9px;line-height:1.25;color:#667;padding:0 0 2px}
.imagery-credit{font-size:10px;color:#777;margin-top:5px}
.imagery-credit a{color:#777}
#chart-controls{display:flex;gap:6px;margin:8px 0 4px;flex-wrap:wrap}
.chart-btn{padding:4px 10px;border:1px solid #889;border-radius:3px;background:#fff;
           font-size:12px;cursor:pointer;color:#444}
.chart-btn.active{background:#1a2a3a;color:#fff;border-color:#1a2a3a}
#chart-loading{color:#666;font-style:italic;font-size:12px;margin:8px 0}
#chart-div{min-height:380px;position:relative}
#chart-frozen-tip{position:absolute;top:8px;right:8px;z-index:20;background:rgba(255,255,255,0.98);
  border:1px solid rgba(0,0,0,0.45);padding:6px 8px;font-size:12px;line-height:1.35;
  color:#111;border-radius:4px;max-width:260px;pointer-events:none;display:none}
.js-plotly-plot .cursor-crosshair{cursor:default !important}
.js-plotly-plot .hoverlayer line{stroke:#000;stroke-width:2px !important;opacity:1 !important}
#legend-stack{position:absolute;bottom:28px;left:8px;z-index:900;display:flex;
        flex-direction:column;gap:8px;align-items:flex-start}
#legend{background:rgba(255,255,255,0.92);border:1px solid #bbb;border-radius:4px;
  padding:8px 10px;font-size:11px;min-width:140px}
#legend h3{font-size:12px;margin-bottom:5px;color:#333}
.legend-row{display:flex;align-items:center;gap:6px;margin:2px 0}
.legend-dot{width:12px;height:12px;border-radius:50%;flex-shrink:0;border:1px solid rgba(0,0,0,0.2)}
#network-legend{background:rgba(255,255,255,0.92);border:1px solid #bbb;
    border-radius:4px;padding:8px 10px;font-size:11px;min-width:120px}
#network-legend h3{font-size:12px;margin-bottom:5px;color:#333}
.nlrow{display:flex;align-items:center;gap:6px;margin:2px 0;cursor:pointer;
  padding:1px 3px;border-radius:3px;user-select:none;transition:background 0.12s}
.nlrow:hover{background:rgba(0,0,0,0.07)}
.nlrow.net-off{opacity:0.38}
.nlrow .net-label{font-size:11px;color:#333;white-space:nowrap;flex:1}
.nlrow .net-count{font-size:10px;color:#888;margin-left:2px}
.nshape{width:14px;height:14px;flex-shrink:0;display:flex;align-items:center;justify-content:center}
#date-slider-wrap{display:flex;flex-direction:column;align-items:stretch;
                  width:min(860px, calc(100vw - 40px));margin:7px auto 0;gap:2px}
#date-slider-title{font-size:10px;font-weight:600;color:#cfe5fb;text-align:center;line-height:1.2;margin-bottom:2px}
#date-slider-row{display:flex;align-items:center;gap:8px;width:100%}
#date-slider-track{position:relative;flex:1 1 auto;min-width:0;height:24px;--thumb-w:9px;--thumb-half:4.5px}
#snap-current-day{height:24px;padding:0 9px;border:1px solid rgba(220,236,252,0.65);
                  border-radius:3px;background:rgba(17,28,40,0.35);color:#eef7ff;
                  font-size:11px;font-weight:700;letter-spacing:0.1px;cursor:pointer;
                  white-space:nowrap;flex:0 0 17ch;width:17ch;text-align:center;
                  display:flex;align-items:center;justify-content:center;
                  box-sizing:border-box;overflow:hidden}
#snap-current-day:disabled{opacity:0.45;cursor:default}
#sel-date{position:absolute;inset:0;width:100%;height:24px;appearance:none;-webkit-appearance:none;
          background:#5d6773;background-repeat:no-repeat;background-position:center;
          background-size:100% 6px;border-radius:0;outline:none;z-index:2;
          margin:0;padding:0;box-sizing:border-box;cursor:pointer}
#sel-date::-webkit-slider-runnable-track{height:6px;background:transparent;border-radius:0}
#sel-date::-webkit-slider-thumb{-webkit-appearance:none;width:9px;height:24px;
                                border-radius:1px;background:transparent;
                                border:2px solid rgba(246,251,255,0.98);
                                box-sizing:border-box;
                                margin-top:-9px;box-shadow:0 0 0 1px rgba(0,0,0,0.75)}
#sel-date::-moz-range-track{height:6px;background:transparent;border-radius:0}
#sel-date::-moz-range-thumb{width:9px;height:24px;border-radius:1px;
                            background:transparent;
                            border:2px solid rgba(246,251,255,0.98);
                            box-sizing:border-box;
                            box-shadow:0 0 0 1px rgba(0,0,0,0.75)}
#day-ticks{position:absolute;left:0;right:0;top:9px;height:6px;pointer-events:none;z-index:3}
.day-tick{position:absolute;width:1px;height:6px;background:rgba(236,244,252,0.9);
          transform:translateX(-0.5px)}
.day-tick.month-tick{height:10px;transform:translate(-0.5px,-2px);background:rgba(244,250,255,0.98)}
#date-tick-labels{width:100%;position:relative;height:16px;margin-top:5px;
                  font-size:11px;font-weight:700;color:#edf6ff;line-height:1;pointer-events:none}
.date-tick-label{position:absolute;transform:translateX(-50%);white-space:nowrap}
@media (max-width: 980px){
  #clock-block{position:static;width:auto;max-width:none;margin:0 auto;align-items:center}
  #clock-note{text-align:center}
}
</style>
</head>
<body>
<div id="app">
  <div id="topbar">
    <div id="map-title-block">
      <div id="map-title-main">WY SWE % of Period of Record</div>
      <div id="map-title-sub">Loading date…</div>
      <div id="date-slider-wrap">
        <div id="date-slider-title">Select a date with the slider</div>
        <div id="date-slider-row">
          <div id="date-slider-track">
            <div id="day-ticks"></div>
            <input id="sel-date" type="range" min="1" max="366" step="1" value="1" list="date-ticks">
          </div>
          <button id="snap-current-day" type="button" title="Jump to current day">Current day</button>
        </div>
        <datalist id="date-ticks"></datalist>
        <div id="date-tick-labels"></div>
      </div>
    </div>
      <div id="clock-block">
        <div id="clock-now">It is currently...</div>
        <div id="clock-utc" class="clock-tz-line"><span class="clock-tz-label">UTC:</span><span>--:--:--</span></div>
        <div id="clock-pt" class="clock-tz-line"><span class="clock-tz-label">PT:</span><span>Loading Pacific time...</span></div>
        <div id="clock-note">Values reflect the first measurement of the day. Values are often adjusted and revised for quality within a week or two.</div>
      </div>
    <div id="top-controls">
      <div class="ctl-group">
        <span class="ctl-label">Basemap:</span>
        <select id="sel-basemap">
          <option value="esri_light">Esri Light Gray</option>
          <option value="esri_sat">Esri WorldImagery</option>
          <option value="esri_topo">Esri Topo</option>
        </select>
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Variable:</span>
        <select id="sel-var">
          <option value="WTEQ">SWE</option>
          <option value="SNWD">Snow Depth</option>
        </select>
      </div>
      <div class="ctl-group">
        <span class="ctl-label">Reference:</span>
        <select id="sel-ref">
          <option value="por">Period of Record</option>
          <option value="n9120">1991-2020 Normal</option>
          <option value="n8110">1981-2010 Normal</option>
          <option value="n7100">1971-2000 Normal</option>
        </select>
      </div>
    </div>
  </div>
  <div id="main-area">
    <div id="map">
      <div id="legend-stack">
        <div id="network-legend">
          <h3>Network</h3>
          <div id="network-legend-rows"></div>
        </div>
      <div id="legend">
        <h3>% of Normal</h3>
        <div class="legend-row"><div class="legend-dot" style="background:#8B0000"></div>&lt;50% (Extreme low)</div>
        <div class="legend-row"><div class="legend-dot" style="background:#FF6600"></div>50–70% (Much below)</div>
        <div class="legend-row"><div class="legend-dot" style="background:#CCAA00"></div>70–90% (Below normal)</div>
        <div class="legend-row"><div class="legend-dot" style="background:#009900"></div>90–110% (Near normal)</div>
        <div class="legend-row"><div class="legend-dot" style="background:#00AAFF"></div>110–130% (Above normal)</div>
        <div class="legend-row"><div class="legend-dot" style="background:#0000CC"></div>130–150% (Much above)</div>
        <div class="legend-row"><div class="legend-dot" style="background:#9900CC"></div>&gt;150% (Extreme high)</div>
        <div class="legend-row"><div class="legend-dot" style="background:#555555"></div>Normal is 0 cm</div>
        <div class="legend-row"><div class="legend-dot" style="background:#888888"></div>Insufficient data</div>
      </div>
      </div>
    </div>
    <div id="station-panel">
      <button id="close-btn" title="Close">&#x2715;</button>
      <div id="station-info"></div>
      <div id="chart-controls" style="display:none">
        <button class="chart-btn active" id="chart-btn-wteq">SWE</button>
        <button class="chart-btn" id="chart-btn-snwd">Snow Depth</button>
      </div>
      <div id="chart-loading"></div>
      <div id="chart-div"></div>
      <div id="chart-shading-legend" style="display:block;margin:4px 0 0;font-size:11px;color:#444;line-height:1.6">
        <b>Shading (Period of Record):</b><br>
        Decile bands from min-10th, 10th-20th, ..., 90th-max (red = low, blue = high)
      </div>
      <div id="imagery-section" class="imagery-empty"></div>
    </div>
  </div>
</div>

<!-- ═══════════════════ DATA ═══════════════════ -->
<script>
const MAP_META = __MAP_META__;
const SD = __STATION_DATA__;
const PD = __PERIODIC_DATA__;  // periodic / non-daily point observations
</script>

<!-- ═══════════════════ APP LOGIC ═══════════════════ -->
<script>
"use strict";

// ─── State ────────────────────────────────────────────────────────────────────
const st = {
  variable: "WTEQ",
  ref: "por",
  wy: MAP_META.current_wy,
  dowy: MAP_META.today_dowy,
  basemap: "esri_light",
  selectedCode: null,
  chartVar: "WTEQ",
  visibleNetworks: new Set(MAP_META.available_networks),
};

const sliderColorCache = {};
let sliderDragFrame = null;

// ─── Helpers ──────────────────────────────────────────────────────────────────
const MONTHS = ["January","February","March","April","May","June",
                "July","August","September","October","November","December"];

const STATE_NAMES = {
  AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",
  CO:"Colorado",CT:"Connecticut",DE:"Delaware",FL:"Florida",GA:"Georgia",
  HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",
  KY:"Kentucky",LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",
  MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",MT:"Montana",
  NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",
  NM:"New Mexico",NY:"New York",NC:"North Carolina",ND:"North Dakota",
  OH:"Ohio",OK:"Oklahoma",OR:"Oregon",PA:"Pennsylvania",RI:"Rhode Island",
  SC:"South Carolina",SD:"South Dakota",TN:"Tennessee",TX:"Texas",
  UT:"Utah",VT:"Vermont",VA:"Virginia",WA:"Washington",WV:"West Virginia",
  WI:"Wisconsin",WY:"Wyoming",
  // Canadian provinces and territories — BC and AB arrive via AWDB and
  // DataBC; YT via the Yukon AquaCache client. AWDB codes its Yukon
  // partner snow courses "YK" — kept verbatim per DESIGN.md §5.
  AB:"Alberta", BC:"British Columbia", NT:"Northwest Territories",
  NU:"Nunavut", YT:"Yukon", YK:"Yukon",
};

const NET_LABELS = {
  SNTL:"SNOTEL", SNTLT:"SNOTEL Lite", MSNT:"Manual SNOTEL",
  MPRC:"Manual", SNOW:"Manual",
  SCAN:"SCAN", COOP:"COOP",
  CCSS:"CCSS", BCSS:"BC Snow Survey",
  NVE:"NVE (Norway)",
  YSS:"Yukon Snow Survey", YKEC:"ECCC Yukon",
};

// SVG shape markup for each network code (12×12 viewBox)
const NET_SHAPES = {
  SNTL:'<circle cx="6" cy="6" r="5" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  SNTLT:'<polygon points="6,1 11,11 1,11" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  MSNT:'<polygon points="6,0.8 11.2,4.6 9.2,10.7 2.8,10.7 0.8,4.6" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  SCAN:'<polygon points="6,0.8 10.5,3.4 10.5,8.6 6,11.2 1.5,8.6 1.5,3.4" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  MPRC:'<rect x="1" y="1" width="10" height="10" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  SNOW:'<polygon points="6,0.5 11.5,6 6,11.5 0.5,6" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  COOP:'<rect x="4.2" y="1" width="3.6" height="10" fill="#666" stroke="#fff" stroke-width="0.5"/><rect x="1" y="4.2" width="10" height="3.6" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  CCSS:'<polygon points="1,1 11,1 6,11" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  BCSS:'<polygon points="6,0.5 10.9,3.6 10.9,8.4 6,11.5 1.1,8.4 1.1,3.6" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  NVE:'<polygon points="6,1 11,6 6,11 1,6" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  YSS:'<polygon points="6,1 9.54,2.46 11,6 9.54,9.54 6,11 2.46,9.54 1,6 2.46,2.46" fill="#666" stroke="#fff" stroke-width="0.5"/>',
  YKEC:'<polygon points="11,6 3.5,10.33 3.5,1.67" fill="#666" stroke="#fff" stroke-width="0.5"/>',
};

function pctColor(pct) {
  if (pct === null || pct === undefined) return "#888888";
  if (pct <  50) return "#8B0000";
  if (pct <  70) return "#FF6600";
  if (pct <  90) return "#CCAA00";
  if (pct <= 110) return "#009900";
  if (pct <= 130) return "#00AAFF";
  if (pct <= 150) return "#0000CC";
  return "#9900CC";
}

function dowyToDate(dowy, wy) {
  const start = new Date(wy - 1, 9, 1);  // Oct 1 of prior year
  start.setDate(start.getDate() + dowy - 1);
  return start;
}

function formatDate(d) {
  return `${d.getFullYear()} ${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

function ordinalDay(n) {
  const mod100 = n % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${n}th`;
  const mod10 = n % 10;
  if (mod10 === 1) return `${n}st`;
  if (mod10 === 2) return `${n}nd`;
  if (mod10 === 3) return `${n}rd`;
  return `${n}th`;
}

function updateClockPanel() {
  const nowUtc = new Date();
  const utcYear = nowUtc.getUTCFullYear();
  const utcMonth = MONTHS[nowUtc.getUTCMonth()];
  const utcDay = nowUtc.getUTCDate();
  const utcDate = new Date(Date.UTC(utcYear, nowUtc.getUTCMonth(), utcDay));
  const utcDowy = dateToDowyWy(utcDate).dowy;
  const utcTime = nowUtc.toLocaleTimeString("en-US", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: "UTC",
  });

  const nowPt = new Date(nowUtc.toLocaleString("en-US", {timeZone: "America/Los_Angeles"}));
  const ptYear = nowPt.getFullYear();
  const ptMonth = MONTHS[nowPt.getMonth()];
  const ptDay = nowPt.getDate();
  const ptDow = dateToDowyWy(new Date(ptYear, nowPt.getMonth(), ptDay)).dowy;
  const ptTime = nowPt.toLocaleTimeString("en-US", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true});
  const utcDay2 = String(utcDay).padStart(2, "0");
  const ptDay2 = String(ptDay).padStart(2, "0");

  const nowEl = document.getElementById("clock-now");
  const utcEl = document.getElementById("clock-utc");
  const ptEl = document.getElementById("clock-pt");
  if (nowEl) {
    nowEl.textContent = "It is currently...";
  }
  if (utcEl) {
    utcEl.innerHTML = `<span class="clock-tz-label">UTC:</span><span>${utcYear} ${utcMonth} ${utcDay2} (DOWY ${utcDowy}) ${utcTime}</span>`;
  }
  if (ptEl) {
    ptEl.innerHTML = `<span class="clock-tz-label">PT:</span><span>${ptYear} ${ptMonth} ${ptDay2} (DOWY ${ptDow}) ${ptTime}</span>`;
  }
}

function dateToDowyWy(dateObj) {
  const m = dateObj.getMonth() + 1;
  const y = dateObj.getFullYear();
  const wy = m >= 10 ? y + 1 : y;
  const wyStart = new Date(wy - 1, 9, 1);
  const dowy = Math.round((dateObj - wyStart) / 864e5) + 1;
  return {wy, dowy};
}

// ─── Get pct-normal for arbitrary station + dowy + wy ─────────────────────────
function getStationPct(code, dowy, wy, variable, ref) {
  const s = SD[code];
  if (!s) return {pct: null, n: 0, cur: null, curDowy: null, med_mm: 0};

  const vk = variable.toLowerCase();
  const dowyIdx = dowy - 1;

  // n_years at this DOWY
  const nKey = `pn_${ref}_${vk}`;
  const n = s[nKey] ? (s[nKey][dowyIdx] || 0) : 0;

  // Get current value for the selected WY + DOWY.
  // Fall back up to 3 days earlier to handle stations whose data arrives late
  // (e.g. Alaska stations are typically 1 day behind UTC at the update time).
  const wyStr = String(wy);
  let cur = null;
  let curDowy = null;  // actual DOWY the value came from
  if (s.wy && s.wy[wyStr]) {
    const wyEntry = s.wy[wyStr][vk];
    if (wyEntry) {
      for (let lag = 0; lag <= 3; lag++) {
        const di = wyEntry.d.indexOf(dowy - lag);
        if (di >= 0) { cur = wyEntry.v[di]; curDowy = dowy - lag; break; }
      }
    }
  }

  // Historical median (stored in mm)
  const mKey = `pm_${ref}_${vk}`;
  const med_mm = s[mKey] ? (s[mKey][dowyIdx] || 0) : 0;

  let pct = null;
  if (n >= MAP_META.min_years && cur !== null && med_mm > 0) {
    pct = Math.round((cur * 10 / med_mm) * 1000) / 10;  // one decimal place
  }
  return {pct, n, cur, curDowy, med_mm};
}

function formatObsSummary(code, variable) {
  const obs = getStationPct(code, st.dowy, st.wy, variable, st.ref);
  const varLabel = variable === "WTEQ" ? "SWE" : "Snow Depth";
  if (obs.cur === null) {
    return `${varLabel}: No recent data`;
  }
  const valStr = `${(obs.cur).toFixed(1)} cm`;
  if (obs.n < MAP_META.min_years) {
    return `${varLabel}: ${valStr}, insufficient history (${obs.n} years)`;
  }
  if (obs.med_mm <= 0) {
    return `${varLabel}: ${valStr}, normal is 0 cm`;
  }
  if (obs.med_mm <= 0 || obs.pct === null) {
    return `${varLabel}: ${valStr}, no normal available`;
  }
  return `${varLabel}: ${valStr}, ${obs.pct}% of normal`;
}

function markerColorForObs(obs) {
  if (obs.cur === null || obs.n < MAP_META.min_years) return "#888888";
  if (obs.med_mm <= 0) return "#555555";
  if (obs.pct === null) return "#888888";
  return pctColor(obs.pct);
}

function computeSliderAverages(variable, ref) {
  const key = `${variable}_${ref}_${st.wy}_${MAP_META.today_dowy}`;
  if (sliderColorCache[key]) return sliderColorCache[key];

  const avgPct = [];
  const validCount = [];
  const codes = Object.keys(SD);
  for (let dowy = 1; dowy <= MAP_META.today_dowy; dowy++) {
    let sum = 0;
    let n = 0;
    for (const code of codes) {
      const obs = getStationPct(code, dowy, st.wy, variable, ref);
      if (obs.cur !== null && obs.n >= MAP_META.min_years && obs.med_mm > 0 && obs.pct !== null) {
        sum += obs.pct;
        n += 1;
      }
    }
    validCount.push(n);
    avgPct.push(n >= 100 ? (sum / n) : null);
  }
  const out = {avgPct, validCount};
  sliderColorCache[key] = out;
  return out;
}

function updateSliderTrackColor() {
  const slider = document.getElementById("sel-date");
  const {avgPct} = computeSliderAverages(st.variable, st.ref);
  if (!avgPct.length) {
    slider.style.background = "#5d6773";
    return;
  }

  const stops = [];
  const len = avgPct.length;
  const step = Math.max(1, Math.floor(len / 80));
  for (let i = 0; i < len; i += step) {
    const p = len > 1 ? (i / (len - 1)) * 100 : 0;
    const color = avgPct[i] === null ? "#6a737e" : pctColor(avgPct[i]);
    stops.push(`${color} ${p.toFixed(2)}%`);
  }
  if ((len - 1) % step !== 0) {
    const color = avgPct[len - 1] === null ? "#6a737e" : pctColor(avgPct[len - 1]);
    stops.push(`${color} 100%`);
  }
  slider.style.background = `linear-gradient(90deg, ${stops.join(",")})`;
}

// ─── Map setup ────────────────────────────────────────────────────────────────
// The light basemap used to be CARTO Positron. CARTO now requires an API key
// for its basemap tiles and stamps "API KEY REQUIRED" across any tile fetched
// without one, so the light option is Esri's key-free canvas instead: a grey
// base with its labels as a separate layer, grouped so the switcher below can
// add and remove the pair as a single basemap.
//
// That canvas is only tiled to z16; past it Esri serves a grey "Map data not
// yet available" tile, so maxNativeZoom pins the request at 16 and lets
// Leaflet upscale the rest of the way.
const ESRI_CANVAS = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas";
// The canvas is drawn from OpenStreetMap among other sources, so it carries
// Esri's full copyright line rather than Esri alone.
const ESRI_CANVAS_ATTR =
  "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, " +
  "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors, " +
  "and the GIS user community";
// Both taken from the copyrightText each MapServer publishes for itself; topo
// is OpenStreetMap-derived too and had been crediting only Esri.
const ESRI_IMAGERY_ATTR =
  "Tiles &copy; Esri &mdash; Source: Esri, Vantor, Earthstar Geographics, " +
  "and the GIS User Community";
const ESRI_TOPO_ATTR =
  "Tiles &copy; Esri &mdash; Esri, HERE, Garmin, Intermap, USGS, NPS, NRCAN, " +
  "&copy; <a href='https://www.openstreetmap.org/copyright'>OpenStreetMap</a> contributors, " +
  "and the GIS User Community";
const BASEMAPS = {
  esri_light: L.layerGroup([
    L.tileLayer(
      `${ESRI_CANVAS}/World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}`,
      {attribution:ESRI_CANVAS_ATTR,maxZoom:19,maxNativeZoom:16}
    ),
    L.tileLayer(
      `${ESRI_CANVAS}/World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}`,
      {attribution:ESRI_CANVAS_ATTR,maxZoom:19,maxNativeZoom:16}
    ),
  ]),
  esri_sat: L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {attribution:ESRI_IMAGERY_ATTR,maxZoom:19}
  ),
  esri_topo: L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    {attribution:ESRI_TOPO_ATTR,maxZoom:19}
  ),
};

// zoomSnap is a quarter level rather than a whole one, and one mouse notch
// moves one level rather than the two Leaflet gives by default — the wheel had
// been jumping the map two zoom levels at a time. A quarter, not zero: these
// basemaps are raster, so any fractional zoom leaves the tiles scaled, and
// quarter steps keep that softness slight while still feeling continuous.
//
// The bounds are stated on the map, not left to be inferred from whichever
// layers happen to be attached: the switcher swaps basemaps at runtime, and a
// map with no explicit maxZoom reports Infinity whenever nothing attached
// declares one.
const map = L.map("map", {
  center: [43, -112], zoom: 5,
  layers: [BASEMAPS.esri_light],
  zoomControl: true,
  minZoom: 2,
  maxZoom: 19,
  zoomSnap: 0.25,
  zoomDelta: 0.5,
  wheelPxPerZoomLevel: 120,
});

const markerLayer = L.layerGroup().addTo(map);
const leafletMarkers = {};

// ─── Build SVG icon ───────────────────────────────────────────────────────────
function buildIcon(network, measurementType, color, isSelected) {
  const sz = isSelected ? 18 : 10;
  const sw = isSelected ? 2.2 : 0.8;
  const bc = isSelected ? "#000" : "rgba(0,0,0,0.35)";
  const ring = isSelected
    ? `<circle cx="${sz/2}" cy="${sz/2}" r="${sz/2 - 0.8}" fill="none" stroke="#fff" stroke-width="1.6"/>`
    : "";
  const cx = sz / 2;
  const cy = sz / 2;
  const r = sz / 2 - sw / 2;

  function regularPolygonPoints(sides, rotationDeg = -90) {
    const pts = [];
    for (let i = 0; i < sides; i++) {
      const a = ((rotationDeg + (360 * i) / sides) * Math.PI) / 180;
      pts.push(`${(cx + r * Math.cos(a)).toFixed(2)},${(cy + r * Math.sin(a)).toFixed(2)}`);
    }
    return pts.join(" ");
  }

  let inner;
  switch (network) {
    case "SNTLT":
      inner = `<polygon points="${regularPolygonPoints(3, -90)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "CCSS":
      inner = `<polygon points="${regularPolygonPoints(3, 90)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "BCSS":
      inner = `<polygon points="${regularPolygonPoints(6, -90)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "MPRC":
      inner = `<rect x="${sw/2}" y="${sw/2}" width="${sz-sw}" height="${sz-sw}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "SNOW":
      inner = `<polygon points="${regularPolygonPoints(4, -45)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "MSNT":
      inner = `<polygon points="${regularPolygonPoints(5, -90)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "SCAN":
      inner = `<polygon points="${regularPolygonPoints(6, -90)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "COOP": {
      const bar = Math.max(1.2, sz * 0.22);
      inner = [
        `<rect x="${(cx - bar / 2).toFixed(2)}" y="${(sw/2).toFixed(2)}" width="${bar.toFixed(2)}" height="${(sz - sw).toFixed(2)}" fill="${color}" stroke="${bc}" stroke-width="${(sw * 0.7).toFixed(2)}"/>`,
        `<rect x="${(sw/2).toFixed(2)}" y="${(cy - bar / 2).toFixed(2)}" width="${(sz - sw).toFixed(2)}" height="${bar.toFixed(2)}" fill="${color}" stroke="${bc}" stroke-width="${(sw * 0.7).toFixed(2)}"/>`
      ].join("");
      break;
    }
    case "SNTL":
      inner = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "NVE":
      inner = `<polygon points="${regularPolygonPoints(4, 0)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "YSS":
      inner = `<polygon points="${regularPolygonPoints(8, -90)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    case "YKEC":
      inner = `<polygon points="${regularPolygonPoints(3, 0)}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      break;
    default:
      if (measurementType === "manual") {
        inner = `<rect x="${sw/2}" y="${sw/2}" width="${sz-sw}" height="${sz-sw}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      } else {
        inner = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${color}" stroke="${bc}" stroke-width="${sw}"/>`;
      }
      break;
  }
  return L.divIcon({
    html: `<svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}">${ring}${inner}</svg>`,
    iconSize: [sz, sz],
    iconAnchor: [sz/2, sz/2],
    className: "",
  });
}

// ─── Periodic / non-daily point observations (toggle overlay) ────────────────
const periodicMarkers = {};  // "client|code" -> marker
const periodicLayer = L.layerGroup();

function periodicIcon(isSelected) {
  const sz = isSelected ? 16 : 11;
  const c = sz / 2;
  return L.divIcon({
    html: `<svg width="${sz}" height="${sz}" viewBox="0 0 ${sz} ${sz}">`
      + `<circle cx="${c}" cy="${c}" r="${c - 1.5}" fill="none" `
      + `stroke="#555" stroke-width="1.6" stroke-dasharray="2.5,1.8"/></svg>`,
    iconSize: [sz, sz],
    iconAnchor: [c, c],
    className: "",
  });
}

function periodicPopupHtml(s) {
  const rows = [];
  rows.push(`<b>${s.name}</b>`);
  rows.push(`Code: ${s.code} · ${NET_LABELS[s.net] || s.net}`);
  if (s.op) rows.push(`Operator: ${s.op}`);
  if (s.dp) rows.push(`Data provider: ${s.dp}`);
  for (const v of s.vars || []) {
    rows.push(`<span style="font-size:11px">${v[0]} variables: ${v[1]}</span>`);
  }
  rows.push("Periodic / non-daily point observations — no daily chart");
  if (s.url) rows.push(`<a href="${s.url}" target="_blank" rel="noopener noreferrer">Station page</a>`);
  if (s.cam) rows.push(`<a href="${s.cam}" target="_blank" rel="noopener noreferrer">🛰 Live satellite camera</a>`);
  const dupHtml = duplicatesHtml(s.dups);
  if (dupHtml) rows.push(dupHtml);
  return rows.join("<br>");
}

function initPeriodicMarkers() {
  for (const s of PD) {
    const m = L.marker([s.lat, s.lon], {
      icon: periodicIcon(false), zIndexOffset: 50,
    });
    m.bindPopup(periodicPopupHtml(s), {maxWidth: 300});
    m.bindTooltip(
      `<b>${s.name}</b><br>Code: ${s.code}<br>`
      + `${NET_LABELS[s.net] || s.net} — periodic`
      + (s.cam ? "<br>🛰 live satellite camera" : ""),
      {sticky: true, direction: "top"}
    );
    m.addTo(periodicLayer);
    periodicMarkers[`${s.cli}|${s.code}`] = m;
  }
  L.control.layers(null, {
    [`All point observations (${PD.length.toLocaleString()} periodic / non-daily sites)`]: periodicLayer,
  }, {position: "topright", collapsed: false}).addTo(map);
}

// ─── Potentially duplicated stations (DESIGN.md §5) ──────────────────────────
function duplicatesHtml(dups) {
  if (!dups || !dups.length) return "";
  const links = dups.map(d =>
    `<a href="#" onclick="panToStation('${d.client}','${d.code}');return false;">`
    + `${d.code} (${d.client}, ${d.distance_m} m)</a>`
  );
  return `<span style="color:#a60">⚠ Potentially duplicated station — `
    + `also reachable as:</span> ${links.join(", ")}`;
}

function panToStation(client, code) {
  // daily marker first (SD is keyed by code; verify the client matches)
  const s = SD[code];
  if (s && s.cli === client && leafletMarkers[code]) {
    map.setView([s.lat, s.lon], Math.max(map.getZoom(), 11));
    onMarkerClick(code);
    return;
  }
  const pm = periodicMarkers[`${client}|${code}`];
  if (pm) {
    if (!map.hasLayer(periodicLayer)) map.addLayer(periodicLayer);
    map.setView(pm.getLatLng(), Math.max(map.getZoom(), 11));
    pm.openPopup();
  }
}

// ─── Initialise markers ───────────────────────────────────────────────────────
function initMarkers() {
  markerLayer.clearLayers();
  for (const code of Object.keys(SD)) {
    const s = SD[code];
    if (!st.visibleNetworks.has(s.net)) continue;
    const obs = getStationPct(code, st.dowy, st.wy, st.variable, st.ref);
    const color = markerColorForObs(obs);
    const icon = buildIcon(s.net, s.mtype, color, false);
    const m = L.marker([s.lat, s.lon], {icon, zIndexOffset: 100})
      .addTo(markerLayer);
    m._stationCode = code;

    const varSummary = formatObsSummary(code, st.variable);
    m.bindTooltip(
      `<b>${s.name}</b><br>Code: ${code}<br>Network: ${NET_LABELS[s.net]||s.net}<br>${varSummary}${s.cam ? "<br>🛰 live satellite camera" : ""}`,
      {sticky: true, direction: "top"}
    );
    m.on("click", () => onMarkerClick(code));
    leafletMarkers[code] = m;
  }
}

// ─── Recolour all markers ─────────────────────────────────────────────────────
function recolorAll() {
  for (const [code, m] of Object.entries(leafletMarkers)) {
    const s = SD[code];
    // Show/hide based on network filter
    if (st.visibleNetworks.has(s.net)) {
      markerLayer.addLayer(m);
    } else {
      markerLayer.removeLayer(m);
      continue;
    }
    const obs = getStationPct(code, st.dowy, st.wy, st.variable, st.ref);
    const isSelected = code === st.selectedCode;
    const color = markerColorForObs(obs);
    m.setIcon(buildIcon(s.net, s.mtype, color, isSelected));

    const varSummary = formatObsSummary(code, st.variable);
    m.setTooltipContent(
      `<b>${s.name}</b><br>Code: ${code}<br>Network: ${NET_LABELS[s.net]||s.net}<br>${varSummary}${s.cam ? "<br>🛰 live satellite camera" : ""}`
    );

    if (isSelected) {
      m.setZIndexOffset(1000);
    } else {
      m.setZIndexOffset(100);
    }
  }
}

// ─── Title bar ────────────────────────────────────────────────────────────────
function updateTitle() {
  const varLabel = st.variable === "WTEQ" ? "SWE" : "Snow Depth";
  const refLabel = {
    por: "Period of Record", n9120: "1991-2020 Normal",
    n8110: "1981-2010 Normal", n7100: "1971-2000 Normal",
  }[st.ref];
  const mainEl = document.getElementById("map-title-main");
  const subEl = document.getElementById("map-title-sub");
  if (mainEl) mainEl.textContent = `WY${st.wy} ${varLabel} % of ${refLabel}`;
  if (subEl) {
    const dateObj = dowyToDate(st.dowy, st.wy);
    const curTag = st.dowy === MAP_META.today_dowy ? " (Current)" : "";
    subEl.textContent = `${formatDate(dateObj)}, DOWY ${st.dowy}${curTag}`;
  }
}

function updateDatePreviewLabel(dowy) {
  const dateObj = dowyToDate(dowy, st.wy);
  const curTag = dowy === MAP_META.today_dowy ? " (Current)" : "";
  const dateLabel = `${formatDate(dateObj)}, DOWY ${dowy}${curTag}`;
  const dateLabelEl = document.getElementById("map-title-sub");
  if (dateLabelEl) dateLabelEl.textContent = dateLabel;
  updateSnapToCurrentButton();
}

function updateSnapToCurrentButton() {
  const snapBtn = document.getElementById("snap-current-day");
  if (!snapBtn) return;
  const atCurrent = st.dowy === MAP_META.today_dowy;
  snapBtn.disabled = atCurrent;
  snapBtn.textContent = atCurrent ? "Current day" : "Go to current day";
}

// ─── Date slider (current WY only) ──────────────────────────────────────────
function initDateSlider() {
  const slider = document.getElementById("sel-date");
  const snapBtn = document.getElementById("snap-current-day");
  slider.min = 1;
  slider.max = MAP_META.today_dowy;
  slider.value = st.dowy;

  const ticks = document.getElementById("date-ticks");
  const dayTicks = document.getElementById("day-ticks");
  const tickLabels = document.getElementById("date-tick-labels");
  ticks.innerHTML = "";
  dayTicks.innerHTML = "";
  tickLabels.innerHTML = "";
  const uniqueTicks = new Set([1, MAP_META.today_dowy]);
  for (let v = 1; v <= MAP_META.today_dowy; v += 1) {
    uniqueTicks.add(v);
  }
  const tickLabelByDowy = new Map();
  for (let m = 10; m <= 12; m++) {
    const dt = new Date(st.wy - 1, m - 1, 1);
    const dwy = dateToDowyWy(dt);
    if (dwy.wy === st.wy && dwy.dowy >= 1 && dwy.dowy <= MAP_META.today_dowy) {
      tickLabelByDowy.set(dwy.dowy, `${MONTHS[dt.getMonth()].slice(0, 3)} 1`);
    }
  }
  for (let m = 1; m <= 9; m++) {
    const dt = new Date(st.wy, m - 1, 1);
    const dwy = dateToDowyWy(dt);
    if (dwy.wy === st.wy && dwy.dowy >= 1 && dwy.dowy <= MAP_META.today_dowy) {
      tickLabelByDowy.set(dwy.dowy, `${MONTHS[dt.getMonth()].slice(0, 3)} 1`);
    }
  }
  const orderedTicks = Array.from(uniqueTicks).sort((a, b) => a - b);
  const monthTickSet = new Set(tickLabelByDowy.keys());

  for (const v of orderedTicks) {
    const opt = document.createElement("option");
    opt.value = String(v);
    ticks.appendChild(opt);
  }

  function renderDateTicks() {
    dayTicks.innerHTML = "";
    tickLabels.innerHTML = "";

    const trackStyles = getComputedStyle(document.getElementById("date-slider-track"));
    const thumbW = parseFloat(trackStyles.getPropertyValue("--thumb-w")) || 9;
    const maxDowy = MAP_META.today_dowy;
    const sliderWidth = slider.clientWidth || 0;
    const usableWidth = Math.max(0, sliderWidth - thumbW);
    const xForDowy = v => {
      if (maxDowy <= 1) return thumbW / 2;
      return (thumbW / 2) + ((v - 1) / (maxDowy - 1)) * usableWidth;
    };

    for (const v of orderedTicks) {
      const tick = document.createElement("span");
      tick.className = "day-tick" + (monthTickSet.has(v) ? " month-tick" : "");
      tick.style.left = `${xForDowy(v)}px`;
      dayTicks.appendChild(tick);
    }

    for (const [v, label] of tickLabelByDowy.entries()) {
      const lbl = document.createElement("span");
      lbl.className = "date-tick-label";
      lbl.textContent = label;
      lbl.style.left = `${xForDowy(v)}px`;
      tickLabels.appendChild(lbl);
    }
  }

  renderDateTicks();
  window.addEventListener("resize", renderDateTicks);

  if (snapBtn) {
    snapBtn.onclick = () => {
      if (st.dowy === MAP_META.today_dowy) return;
      st.dowy = MAP_META.today_dowy;
      slider.value = String(st.dowy);
      updateDatePreviewLabel(st.dowy);
      updateTitle();
      recolorAll();
      if (st.selectedCode) onMarkerClick(st.selectedCode);
    };
  }

  updateSliderTrackColor();
  updateSnapToCurrentButton();
}

// ─── Station popup ───────────────────────────────────────────────────────────
function onMarkerClick(code) {
  const isNewStation = st.selectedCode !== code;
  st.selectedCode = code;
  recolorAll();  // highlight selected
  if (isNewStation) imgClear();

  const s = SD[code];
  const panel = document.getElementById("station-panel");
  panel.classList.add("visible");

  const stateName = STATE_NAMES[s.st] || s.st || "—";
  const elevStr = s.elev_m != null ? `${s.elev_m} m` : "—";
  const netLabel = NET_LABELS[s.net] || s.net || "—";
  const updStr = s.upd ? s.upd.replace("T", " ").replace("Z", " UTC") : "—";
  const stationUrl = s.url || "";

  const cameraLinkHtml = s.cam
    ? `<div id="station-camera-link"><a href="${s.cam}" target="_blank" rel="noopener noreferrer">🛰 View live satellite camera</a></div>`
    : "";
  let stationPhotoHtml = "";
  if (s.img) {
    const operator = s.op || "Station Operator";
    stationPhotoHtml = `<div id="station-photo-wrap">`
      + `<img id="station-photo" src="${s.img}" alt="${s.name} station photo" loading="lazy" referrerpolicy="no-referrer">`
      + `<div id="station-photo-credit">Photo credit: <a href="${s.img}" target="_blank" rel="noopener noreferrer">${operator}</a></div>`
      + cameraLinkHtml
      + `</div>`;
  } else {
    stationPhotoHtml = `<div id="station-photo-wrap"><div id="station-photo-no-img">No station image available</div>${cameraLinkHtml}</div>`;
  }

  // SWE + snow depth lines
  function buildVarLine(varName, cssClass) {
    const vk = varName.toLowerCase();
    const ref = st.ref;
    const refLabel = {
      por: "POR", n9120: "1991-2020",
      n8110: "1981-2010", n7100: "1971-2000",
    }[ref];
    const obs = getStationPct(code, st.dowy, st.wy, varName, ref);
    const label = varName === "WTEQ" ? "SWE" : "Snow Depth";
    if (obs.cur == null) {
      return `<div class="${cssClass} na-line">${label}: No recent data</div>`;
    }
    const valCm = (obs.cur).toFixed(1);
    const dataDate = obs.curDowy != null
      ? formatDate(dowyToDate(obs.curDowy, st.wy))
      : "—";
    if (obs.n >= MAP_META.min_years && obs.pct != null && obs.med_mm > 0) {
      const medCm = (obs.med_mm / 10).toFixed(2);
      return `<div class="${cssClass}"><b>${label}:</b> ${valCm} cm `
           + `(${obs.pct}% of ${refLabel} median ${medCm} cm)<br>`
           + `<span style="font-size:11px;color:#555">data date: ${dataDate}</span></div>`;
    }
    if (obs.n < MAP_META.min_years) {
      return `<div class="${cssClass}"><b>${label}:</b> ${valCm} cm`
           + `<br><span style="font-size:11px;color:#555">data date: ${dataDate} (insufficient history: ${obs.n} years)</span></div>`;
    }
    return `<div class="${cssClass}"><b>${label}:</b> ${valCm} cm`
         + `<br><span style="font-size:11px;color:#555">data date: ${dataDate} (no normal available)</span></div>`;
  }

  const operatorStr = s.op || "—";
  const clientStr = s.cli || "—";
  const info = document.getElementById("station-info");
  info.innerHTML = `
    <h2>${s.name}</h2>
    ${stationPhotoHtml}
    <div class="info-row"><span class="info-key">Code:</span><span>${code}</span></div>
    <div class="info-row"><span class="info-key">Network:</span><span>${netLabel}</span></div>
    <div class="info-row"><span class="info-key">Operator:</span><span>${operatorStr}</span></div>
    <div class="info-row"><span class="info-key">Data provider:</span><span>${s.dp || "—"} (client: ${clientStr})</span></div>
    <div class="info-row"><span class="info-key">State:</span><span>${stateName}</span></div>
    <div class="info-row"><span class="info-key">Elevation:</span><span>${elevStr}</span></div>
    ${(s.vars && s.vars.length)
      ? s.vars.map(v => `<div class="info-row"><span class="info-key">${v[0]} variables:</span><span style="font-size:11px">${v[1]}</span></div>`).join("")
      : `<div class="info-row"><span class="info-key">Variables:</span><span>—</span></div>`}
    <div class="info-row"><span class="info-key">Earliest record:</span><span>${s.bdate||"—"}</span></div>
    <div class="info-row"><span class="info-key">Latest record:</span><span>${s.edate||"—"}</span></div>
    <div class="info-row"><span class="info-key">Last updated:</span><span>${updStr}</span></div>
    <div class="info-row"><span class="info-key">Station page:</span><span>${stationUrl ? `<a href="${stationUrl}" target="_blank" rel="noopener noreferrer">${stationUrl}</a>` : "—"}</span></div>
    ${s.prov === "resampled_hourly" ? `<div class="info-row"><span class="info-key">Daily series:</span><span>resampled from sub-daily data</span></div>` : ""}
    ${duplicatesHtml(s.dups) ? `<div class="info-row" style="font-size:11px">${duplicatesHtml(s.dups)}</div>` : ""}
    ${buildVarLine("WTEQ", "swe-line")}
    ${buildVarLine("SNWD", "snwd-line")}
  `;

  // Chart controls
  document.getElementById("chart-controls").style.display = "flex";
  document.getElementById("chart-btn-wteq").className =
    "chart-btn" + (st.chartVar === "WTEQ" ? " active" : "");
  document.getElementById("chart-btn-snwd").className =
    "chart-btn" + (st.chartVar === "SNWD" ? " active" : "");

  // Keep explanatory legend visible for all stations when panel is open.
  document.getElementById("chart-shading-legend").style.display = "block";

  // Trigger chart load
  loadChart(code, st.chartVar);

  // Context imagery is fire-and-forget — the panel above is already complete.
  loadImagery(code);
}

// ─── Chart rendering (payload fetched lazily from charts/*.json) ─────────────
const chartCache = {};  // code -> chart payload

async function loadChart(code, variable) {
  document.getElementById("chart-loading").textContent =
    "Loading chart data…";
  document.getElementById("chart-div").innerHTML = "";

  let payload = chartCache[code];
  if (!payload) {
    try {
      const resp = await fetch(`./charts/${code}.json`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      payload = await resp.json();
      chartCache[code] = payload;
    } catch (e) {
      document.getElementById("chart-loading").textContent =
        `Could not load chart data: ${e.message}`;
      return;
    }
  }

  const key = variable === "WTEQ" ? "wteq" : "snwd";
  const stats = payload[key] || null;
  if (!stats) {
    document.getElementById("chart-loading").textContent =
      "No chart data available.";
    return;
  }

  document.getElementById("chart-loading").textContent = "";
  renderChart(code, variable, stats);
}

function renderChart(code, variable, stats) {
  if (!stats) {
    document.getElementById("chart-loading").textContent = "No chart data available.";
    return;
  }
  const s = SD[code];
  const scale = 1;  // values are already in cm
  const varLabel = variable === "WTEQ" ? "SWE (cm)" : "Snow Depth (cm)";
  const dowyArr = Array.from({length:366}, (_,i) => i+1);

  // Current WY data for dots — use st.wy (the selected WY, not always current)
  const vk = variable.toLowerCase();
  const dotWY = st.wy;
  const dotWYStr = String(dotWY);
  const curDots = {d:[], v:[]};
  if (s.wy && s.wy[dotWYStr] && s.wy[dotWYStr][vk]) {
    curDots.d = s.wy[dotWYStr][vk].d;
    curDots.v = s.wy[dotWYStr][vk].v.map(x => x * scale);
  }

  function isoDate(d) {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`;
  }

  const wyByDowy = new Map();
  for (let i = 0; i < curDots.d.length; i++) {
    wyByDowy.set(curDots.d[i], curDots.v[i]);
  }

  const crosshairHover = dowyArr.map((dowy, i) => {
    const dateObj = dowyToDate(dowy, dotWY);
    const minCm = stats.mins[i] !== null ? `${(stats.mins[i] * scale).toFixed(1)} cm` : "n/a";
    const maxCm = stats.maxs[i] !== null ? `${(stats.maxs[i] * scale).toFixed(1)} cm` : "n/a";
    const medCm = stats.p50[i] !== null ? `${(stats.p50[i] * scale).toFixed(1)} cm` : "n/a";
    const wyVal = wyByDowy.get(dowy);
    const wyCm = wyVal != null ? `${wyVal.toFixed(1)} cm` : "n/a";
    const minDate = stats.minYrs[i]
      ? isoDate(dowyToDate(dowy, stats.minYrs[i]))
      : "n/a";
    const maxDate = stats.maxYrs[i]
      ? isoDate(dowyToDate(dowy, stats.maxYrs[i]))
      : "n/a";
    return `<b style="color:#111">${formatDate(dateObj)}</b>`
      + `<br><span style="color:#A10000"><b>Min:</b> ${minCm} <b>@</b> ${minDate}</span>`
      + `<br><span style="color:#0A2F99"><b>Max:</b> ${maxCm} <b>@</b> ${maxDate}</span>`
      + `<br><span style="color:#0B6A0B"><b>Median:</b> ${medCm}</span>`
      + `<br><span style="color:#111111"><b>WY${dotWY}:</b> ${wyCm}</span>`;
  });

  const yVals = [];
  for (const arr of [stats.mins, stats.maxs, stats.p50]) {
    for (const v of arr) {
      if (v !== null) yVals.push(v * scale);
    }
  }
  for (const v of curDots.v) {
    if (Number.isFinite(v)) yVals.push(v);
  }

  let yAxisRange = null;
  if (yVals.length > 0) {
    const yMin = Math.min(...yVals);
    const yMax = Math.max(...yVals);
    const pad = Math.max(1, (yMax - yMin) * 0.08);
    yAxisRange = [Math.max(0, yMin - pad), yMax + pad];
  }
  const selectedDowyText = st.dowy === MAP_META.today_dowy
    ? "<b>selected DOWY (current)</b>"
    : "<b>selected DOWY</b>";

  function fillArr(a, b) {
    // Build polygon path: a going forward, b going backward
    const ax = [...dowyArr, ...dowyArr.slice().reverse()];
    const ay = [...a.map(v => v!==null ? v*scale : null),
                ...b.slice().reverse().map(v => v!==null ? v*scale : null)];
    return {x: ax, y: ay};
  }

  const decileBands = [
    {low: stats.mins, high: stats.p10, color: "rgba(125,0,0,0.46)", name: "Min-10th"},
    {low: stats.p10, high: stats.p20, color: "rgba(160,20,20,0.44)", name: "10th-20th"},
    {low: stats.p20, high: stats.p30, color: "rgba(190,45,45,0.42)", name: "20th-30th"},
    {low: stats.p30, high: stats.p40, color: "rgba(220,72,72,0.40)", name: "30th-40th"},
    {low: stats.p40, high: stats.p50, color: "rgba(240,105,105,0.38)", name: "40th-50th"},
    {low: stats.p50, high: stats.p60, color: "rgba(120,175,255,0.38)", name: "50th-60th"},
    {low: stats.p60, high: stats.p70, color: "rgba(90,150,245,0.40)", name: "60th-70th"},
    {low: stats.p70, high: stats.p80, color: "rgba(60,125,235,0.42)", name: "70th-80th"},
    {low: stats.p80, high: stats.p90, color: "rgba(30,100,225,0.44)", name: "80th-90th"},
    {low: stats.p90, high: stats.maxs, color: "rgba(0,75,205,0.46)", name: "90th-Max"},
  ];

  const bandSeparatorLines = [stats.p10, stats.p20, stats.p30, stats.p40, stats.p50,
                              stats.p60, stats.p70, stats.p80, stats.p90].map((arr) => ({
    x: dowyArr,
    y: arr.map(v => v !== null ? v * scale : null),
    mode: "lines",
    line: {color: "rgba(35,35,35,0.30)", width: 0.45},
    showlegend: false,
    hoverinfo: "skip",
    type: "scatter",
  }));

  const currentIdx = curDots.d.indexOf(st.dowy);
  const currentStarX = currentIdx >= 0 ? [curDots.d[currentIdx]] : [];
  const currentStarY = currentIdx >= 0 ? [curDots.v[currentIdx]] : [];
  const circleX = [];
  const circleY = [];
  for (let i = 0; i < curDots.d.length; i++) {
    if (curDots.d[i] !== st.dowy) {
      circleX.push(curDots.d[i]);
      circleY.push(curDots.v[i]);
    }
  }

  const baseTraces = decileBands.map((band) => Object.assign(fillArr(band.low, band.high), {
    fill:"toself", fillcolor:band.color, line:{width:0},
    name:band.name, showlegend:false, hoverinfo:"skip", type:"scatter"
  })).concat(bandSeparatorLines).concat([
    // Min line (red)
    {
      x: dowyArr, y: stats.mins.map(v => v!==null ? v*scale : null),
      mode:"lines", line:{color:"#CC0000",width:1.5}, name:"Min (POR)",
      hoverinfo:"skip", type:"scatter"
    },
    // Max line (blue)
    {
      x: dowyArr, y: stats.maxs.map(v => v!==null ? v*scale : null),
      mode:"lines", line:{color:"#0000CC",width:1.5}, name:"Max (POR)",
      hoverinfo:"skip", type:"scatter"
    },
    // Median line (green)
    {
      x: dowyArr, y: stats.p50.map(v => v!==null ? v*scale : null),
      mode:"lines", line:{color:"#009900",width:2}, name:"Median (POR)",
      hoverinfo:"skip", type:"scatter"
    },
    // Current WY dots (black)
    {
      x: circleX, y: circleY,
      mode:"markers", marker:{color:"black",size:7},
      name:"WY" + dotWY,
      hoverinfo:"skip", type:"scatter"
    },
    {
      x: currentStarX, y: currentStarY,
      mode:"markers", marker:{color:"black",size:12,symbol:"star"},
      showlegend:false,
      hoverinfo:"skip", type:"scatter"
    },
  ]);

  // Crosshair-following highlight markers.
  const minFocusIdx = baseTraces.length;
  const maxFocusIdx = minFocusIdx + 1;
  const medFocusIdx = minFocusIdx + 2;
  const wyFocusCircleIdx = minFocusIdx + 3;
  const wyFocusStarIdx = minFocusIdx + 4;

  const traces = baseTraces.concat([
    {
      x: [], y: [],
      mode:"markers",
      marker:{color:"#CC0000",size:9,line:{color:"#ffffff",width:1}},
      showlegend:false,
      hoverinfo:"skip",
      type:"scatter"
    },
    {
      x: [], y: [],
      mode:"markers",
      marker:{color:"#0000CC",size:9,line:{color:"#ffffff",width:1}},
      showlegend:false,
      hoverinfo:"skip",
      type:"scatter"
    },
    {
      x: [], y: [],
      mode:"markers",
      marker:{color:"#009900",size:9,line:{color:"#ffffff",width:1}},
      showlegend:false,
      hoverinfo:"skip",
      type:"scatter"
    },
    {
      x: [], y: [],
      mode:"markers",
      marker:{color:"#000000",size:9,line:{color:"#ffffff",width:1}},
      showlegend:false,
      hoverinfo:"skip",
      type:"scatter"
    },
    {
      x: [], y: [],
      mode:"markers",
      marker:{color:"#000000",size:13,symbol:"star",line:{color:"#ffffff",width:1}},
      showlegend:false,
      hoverinfo:"skip",
      type:"scatter"
    },
    // Invisible helper trace for single unified tooltip at each date.
    {
      x: dowyArr,
      y: stats.p50.map(v => v!==null ? v*scale : null),
      mode:"lines",
      line:{color:"rgba(0,0,0,0)", width:1},
      name:"",
      showlegend:false,
      text: crosshairHover,
      hovertemplate:"%{text}<extra></extra>",
      type:"scatter"
    },
  ]);

  const layout = {
    title: {
      text: `${s.name}<br>${varLabel}<br>Reference period: Period of Record`,
      font: {size: 13},
    },
    xaxis: {
      title: {text: "Day of Water Year (DOWY)", standoff: 28},
      range:[1,366],
      showspikes:true,
      spikemode:"across",
      spikesnap:"hovered data",
      spikethickness:2,
      spikecolor:"rgb(0,0,0)",
    },
    yaxis: {
      title: varLabel,
      showspikes:false,
      autorange: yAxisRange === null,
      range: yAxisRange,
    },
    legend: {orientation:"h", y:-0.34, font:{size:11}},
    shapes: [{
      type: "line",
      x0: st.dowy, x1: st.dowy,
      y0: 0, y1: 1,
      yref: "paper",
      line: {color: "rgba(0,0,0,0.60)", width: 1, dash: "dot"},
    }, {
      type: "line",
      x0: st.dowy, x1: st.dowy,
      y0: 0, y1: 1,
      yref: "paper",
      visible: false,
      line: {color: "rgb(0,0,0)", width: 1.2},
    }],
    annotations: [{
      x: st.dowy + 0.08,
      y: 0.985,
      yref: "paper",
      text: selectedDowyText,
      showarrow: false,
      textangle: -90,
      yanchor: "top",
      xanchor: "left",
      xshift: 0,
      font: {color: "#444", size: 13, family: "Segoe UI, Arial, sans-serif"},
      align: "left",
      opacity: 1,
    }],
    margin: {l:50, r:10, t:70, b:95},
    height: 430,
    hovermode: "x",
    hoverdistance: -1,
    spikedistance: -1,
    uirevision: "station-chart-fixed",
    hoverlabel: {
      font: {size: 12, color: "#111"},
      bgcolor: "rgba(255,255,255,1)",
      bordercolor: "rgba(0,0,0,0.45)",
      align: "left",
    },
    paper_bgcolor:"#f5f7fa",
    plot_bgcolor:"#fff",
  };

  const chartDiv = document.getElementById("chart-div");
  if (!document.getElementById("chart-frozen-tip")) {
    const tip = document.createElement("div");
    tip.id = "chart-frozen-tip";
    chartDiv.appendChild(tip);
  }
  const frozenTip = document.getElementById("chart-frozen-tip");
  let hoverFrozen = false;
  let frozenDowy = null;
  let freezeSetAtMs = 0;

  function _setFrozenTip(dowy, clickEvent) {
    const i = Math.max(0, Math.min(365, Math.round(dowy) - 1));
    frozenTip.innerHTML = crosshairHover[i];
    frozenTip.style.left = "8px";
    frozenTip.style.right = "auto";
    frozenTip.style.top = "8px";
    if (clickEvent && typeof clickEvent.clientX === "number" && typeof clickEvent.clientY === "number") {
      const rect = chartDiv.getBoundingClientRect();
      const maxLeft = Math.max(8, chartDiv.clientWidth - 268);
      const maxTop = Math.max(8, chartDiv.clientHeight - 130);
      const left = Math.max(8, Math.min(maxLeft, (clickEvent.clientX - rect.left) + 16));
      const top = Math.max(8, Math.min(maxTop, (clickEvent.clientY - rect.top) - 24));
      frozenTip.style.left = `${left}px`;
      frozenTip.style.top = `${top}px`;
    }
    frozenTip.style.display = "block";
  }

  function _clearFrozenTip() {
    frozenTip.style.display = "none";
    frozenTip.innerHTML = "";
  }

  function _setFreezeVisuals(dowy, clickEvent) {
    Plotly.Fx.unhover(chartDiv);
    Plotly.relayout(chartDiv, {
      "xaxis.showspikes": false,
      hovermode: false,
      "shapes[1].visible": true,
      "shapes[1].x0": dowy,
      "shapes[1].x1": dowy,
    });
    _setFocusMarkers(dowy);
    _setFrozenTip(dowy, clickEvent);
  }

  function _clearFreezeVisuals() {
    Plotly.relayout(chartDiv, {
      "xaxis.showspikes": true,
      hovermode: "x",
      "shapes[1].visible": false,
    });
    _clearFocusMarkers();
    _clearFrozenTip();
  }

  Plotly.newPlot(chartDiv, traces, layout, {responsive:true, displayModeBar:false});

  function _focusPoint(value, dowy) {
    return value != null ? {x:[dowy], y:[value]} : {x:[], y:[]};
  }

  function _clearFocusMarkers() {
    Plotly.restyle(
      chartDiv,
      {x:[[[]],[[]],[[]],[[]],[[]]], y:[[[]],[[]],[[]],[[]],[[]]]},
      [minFocusIdx, maxFocusIdx, medFocusIdx, wyFocusCircleIdx, wyFocusStarIdx]
    );
  }

  function _setFocusMarkers(dowy) {
    const i = Math.max(0, Math.min(365, Math.round(dowy) - 1));
    const minV = stats.mins[i] != null ? stats.mins[i] * scale : null;
    const maxV = stats.maxs[i] != null ? stats.maxs[i] * scale : null;
    const medV = stats.p50[i] != null ? stats.p50[i] * scale : null;
    const wyV = wyByDowy.has(i + 1) ? wyByDowy.get(i + 1) : null;

    const minPt = _focusPoint(minV, i + 1);
    const maxPt = _focusPoint(maxV, i + 1);
    const medPt = _focusPoint(medV, i + 1);
    const wyPt = _focusPoint(wyV, i + 1);
    const useStar = (i + 1) === st.dowy;
    const wyCirclePt = useStar ? {x:[], y:[]} : wyPt;
    const wyStarPt = useStar ? wyPt : {x:[], y:[]};

    Plotly.restyle(
      chartDiv,
      {
        x:[minPt.x, maxPt.x, medPt.x, wyCirclePt.x, wyStarPt.x],
        y:[minPt.y, maxPt.y, medPt.y, wyCirclePt.y, wyStarPt.y],
      },
      [minFocusIdx, maxFocusIdx, medFocusIdx, wyFocusCircleIdx, wyFocusStarIdx]
    );
  }

  chartDiv.on("plotly_hover", (ev) => {
    if (hoverFrozen) return;
    if (!ev || !ev.points || ev.points.length === 0) return;
    const p = ev.points[0];
    if (p && p.x != null) _setFocusMarkers(Number(p.x));
    
    // Offset tooltip text only (not the spike line)
    setTimeout(() => {
      const textElems = chartDiv.querySelectorAll(".hoverlayer text");
      textElems.forEach(el => {
        const rawX = el.getAttribute("data-orig-x") || el.getAttribute("x") || "0";
        const rawY = el.getAttribute("data-orig-y") || el.getAttribute("y") || "0";
        const x = parseFloat(rawX) || 0;
        const y = parseFloat(rawY) || 0;
        el.setAttribute("data-orig-x", String(x));
        el.setAttribute("data-orig-y", String(y));
        el.setAttribute("x", String(x + 16));
        el.setAttribute("y", String(y - 22));
      });
    }, 0);
  });

  chartDiv.on("plotly_unhover", () => {
    if (hoverFrozen) return;
    _clearFocusMarkers();
  });

  chartDiv.on("plotly_click", (ev) => {
    if (hoverFrozen) {
      hoverFrozen = false;
      frozenDowy = null;
      _clearFreezeVisuals();
      return;
    }
    if (!ev || !ev.points || ev.points.length === 0) return;
    const p = ev.points[0];
    if (!p || p.x == null) return;
    hoverFrozen = true;
    frozenDowy = Math.round(Number(p.x));
    freezeSetAtMs = Date.now();
    _setFreezeVisuals(frozenDowy, ev.event);
  });

  // Ensure a second click unfreezes even if Plotly does not emit plotly_click
  // (e.g., clicking non-point whitespace while frozen).
  chartDiv.addEventListener("click", () => {
    if (!hoverFrozen) return;
    if (Date.now() - freezeSetAtMs < 220) return;
    hoverFrozen = false;
    frozenDowy = null;
    _clearFreezeVisuals();
  });

  document.getElementById("chart-shading-legend").style.display = "block";
}

// ─── Context imagery (Sentinel-2 chips, fetched live from MPC) ───────────────
// Best-effort decoration only: the station's numbers, marker color, and chart
// come from the committed archive and never wait on this (DESIGN.md §8).
const IMG_CFG = MAP_META.imagery || {enabled: false};

const img = {
  code: null,
  mode: "recent",                              // "recent" | "clearest"
  render: IMG_CFG.default_render,
  extentKm: IMG_CFG.default_extent_km,
  scenes: [],
  selected: 0,
  token: 0,
};
const imgSearchCache = {};   // `${code}|${endDay}|${windowDays}` -> scene list

function imgIsoDay(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
       + `-${String(d.getDate()).padStart(2, "0")}`;
}

// Imagery follows the date slider, but never searches into the future.
function imgEndDate() {
  const sel = dowyToDate(st.dowy, st.wy);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return sel > today ? today : sel;
}

// Chip bbox: a ground rectangle `km` wide with the station at dead centre, so
// the ring overlay marks the station without any pixel maths.
function imgChipBbox(lat, lon, km, aspect) {
  const halfW = (km / 2) / (111.320 * Math.cos(lat * Math.PI / 180));
  const halfH = (km / 2 / aspect) / 110.574;
  return [lon - halfW, lat - halfH, lon + halfW, lat + halfH];
}

function imgRender() {
  return IMG_CFG.renders[img.render] || IMG_CFG.renders[IMG_CFG.default_render];
}

function imgBboxStr(bbox) {
  return bbox.map(v => v.toFixed(6)).join(",");
}

function imgChipUrl(itemId, bbox, maxSize, rescale) {
  const r = imgRender();
  const p = new URLSearchParams();
  p.append("collection", IMG_CFG.collection);
  p.append("item", itemId);
  for (const band of r.bands) p.append("assets", band);
  for (const [lo, hi] of rescale) p.append("rescale", `${Math.round(lo)},${Math.round(hi)}`);
  p.append("color_formula", r.color_formula);
  p.append("nodata", "0");
  // Without dst_crs the crop comes back in plate carrée and everything is
  // vertically squashed by 1/cos(lat) — badly so at Norwegian latitudes.
  p.append("dst_crs", "EPSG:3857");
  p.append("max_size", String(maxSize));
  return `${IMG_CFG.render_url}/${imgBboxStr(bbox)}.png?${p.toString()}`;
}

// ── Per-chip contrast stretch ───────────────────────────────────────────────
const imgStretchCache = {};   // `${item}|${bbox}|${render}` -> [[lo,hi] x3]

function imgWidenSpan(lo, hi) {
  const span = hi - lo;
  if (span >= IMG_CFG.stats_min_span) return [Math.max(0, lo), hi];
  const mid = (lo + hi) / 2;
  const half = IMG_CFG.stats_min_span / 2;
  return [Math.max(0, mid - half), mid + half];
}

// The strip shares the selected scene's stretch instead of firing six more
// stats calls. Widening it keeps the other dates — which may be far brighter
// or darker — from clipping to flat white or black in the thumbnails.
function imgLoosenStretch(rescale, factor) {
  return rescale.map(([lo, hi]) => {
    const mid = (lo + hi) / 2;
    const half = ((hi - lo) / 2) * factor;
    return [Math.max(0, mid - half), mid + half];
  });
}

async function imgStretchFor(itemId, bbox) {
  const r = imgRender();
  const key = `${itemId}|${imgBboxStr(bbox)}|${img.render}`;
  if (imgStretchCache[key]) return imgStretchCache[key];

  const [w, s, e, n] = bbox;
  const p = new URLSearchParams();
  p.append("collection", IMG_CFG.collection);
  p.append("item", itemId);
  for (const band of r.bands) p.append("assets", band);
  for (const pct of IMG_CFG.stats_percentiles) p.append("p", String(pct));
  p.append("max_size", String(IMG_CFG.stats_max_size));

  let rescale = r.fallback_rescale;
  try {
    const resp = await fetch(`${IMG_CFG.stats_url}?${p.toString()}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        type: "Feature", properties: {},
        geometry: {type: "Polygon",
                   coordinates: [[[w, s], [e, s], [e, n], [w, n], [w, s]]]},
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    const stats = (data.properties || data).statistics || {};
    const [pLo, pHi] = IMG_CFG.stats_percentiles;
    const bands = r.bands.map(b => {
      const st = stats[`${b}_b1`] || stats[b];
      if (!st) return null;
      const lo = st[`percentile_${pLo}`];
      const hi = st[`percentile_${pHi}`];
      return (lo == null || hi == null || hi <= lo) ? null : [lo, hi];
    });
    if (bands.every(Boolean)) {
      rescale = r.stretch === "common"
        // One stretch for all three bands: the SWIR render's snow-vs-cloud
        // reading lives in the ratio between them.
        ? (() => {
            const [lo, hi] = imgWidenSpan(Math.min(...bands.map(b => b[0])),
                                          Math.max(...bands.map(b => b[1])));
            return [[lo, hi], [lo, hi], [lo, hi]];
          })()
        : bands.map(([lo, hi]) => imgWidenSpan(lo, hi));
    }
  } catch (e) {
    // Contrast is an enhancement; a failed stats call just means the chip
    // renders with the render's fixed fallback stretch.
  }
  imgStretchCache[key] = rescale;
  return rescale;
}

// ── Granule footprint tests: skip scenes that only clip the chip ────────────
function imgPointInRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    if (((yi > y) !== (yj > y)) &&
        (x < ((xj - xi) * (y - yi)) / (yj - yi) + xi)) {
      inside = !inside;
    }
  }
  return inside;
}

function imgGeomContains(geom, x, y) {
  if (!geom) return false;
  const polys = geom.type === "Polygon" ? [geom.coordinates]
              : geom.type === "MultiPolygon" ? geom.coordinates
              : [];
  for (const poly of polys) {
    if (!poly.length || !imgPointInRing(x, y, poly[0])) continue;
    let inHole = false;
    for (let h = 1; h < poly.length; h++) {
      if (imgPointInRing(x, y, poly[h])) { inHole = true; break; }
    }
    if (!inHole) return true;
  }
  return false;
}

function imgCoversChip(geom, bbox) {
  const [w, s, e, n] = bbox;
  return [[w, s], [w, n], [e, s], [e, n]]
    .every(([x, y]) => imgGeomContains(geom, x, y));
}

async function imgSearchScenes(lat, lon, endDate, windowDays) {
  const start = new Date(endDate.getTime() - windowDays * 864e5);
  const body = {
    collections: [IMG_CFG.collection],
    intersects: {type: "Point", coordinates: [lon, lat]},
    datetime: `${imgIsoDay(start)}T00:00:00Z/${imgIsoDay(endDate)}T23:59:59Z`,
    limit: IMG_CFG.search_limit,
    sortby: [{field: "properties.datetime", direction: "desc"}],
    fields: IMG_CFG.search_fields,
  };
  const resp = await fetch(IMG_CFG.search_url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  return (data.features || []).map(f => ({
    id: f.id,
    date: String(f.properties.datetime || "").slice(0, 10),
    cloud: f.properties["eo:cloud_cover"],
    platform: f.properties.platform || "",
    geom: f.geometry,
  }));
}

// Scene-level cloud cover describes the whole 110 km granule, not this
// station, so it ranks the strip — it never hides a scene.
function imgPickScenes(all, bbox) {
  const full = all.filter(s => imgCoversChip(s.geom, bbox));
  const clipped = full.length === 0 && all.length > 0;
  const pool = full.length ? full : all;
  const ranked = img.mode === "clearest"
    ? pool.slice().sort((a, b) => (a.cloud == null ? 101 : a.cloud)
                               - (b.cloud == null ? 101 : b.cloud))
    : pool;   // the search already returns newest-first
  return {scenes: ranked.slice(0, IMG_CFG.max_scenes), clipped};
}

function imgScaleBarKm(extentKm) {
  const steps = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10];
  let best = steps[0];
  for (const v of steps) if (v <= extentKm / 3) best = v;
  return best;
}

function imgFormatDate(iso) {
  const parts = String(iso).split("-").map(Number);
  if (parts.length !== 3 || parts.some(Number.isNaN)) return iso || "—";
  return `${parts[0]} ${MONTHS[parts[1] - 1]} ${parts[2]}`;
}

function imgCloudLabel(cloud) {
  return cloud == null ? "cloud n/a" : `${Math.round(cloud)}% cloud`;
}

function imgControlsHtml() {
  const renderOpts = Object.entries(IMG_CFG.renders)
    .map(([k, v]) => `<option value="${k}"${k === img.render ? " selected" : ""}>`
                   + `${v.label}</option>`).join("");
  const extentCtl = `<span class="imagery-extent-ctl" `
    + `title="Chip width on the ground">`
    + `<input id="imagery-extent" type="range" min="${IMG_CFG.extent_min_km}" `
    + `max="${IMG_CFG.extent_max_km}" step="${IMG_CFG.extent_step_km}" `
    + `value="${img.extentKm}" aria-label="Chip width in km">`
    + `<span id="imagery-extent-val">${img.extentKm} km</span></span>`;
  const modeOpts = [
    ["recent", `${IMG_CFG.max_scenes} most recent`],
    ["clearest", `${IMG_CFG.max_scenes} least cloudy`],
  ].map(([k, label]) => `<option value="${k}"${k === img.mode ? " selected" : ""}>`
                      + `${label}</option>`).join("");
  return `<div class="imagery-controls">`
    + `<select id="imagery-mode" title="Which scenes to show">${modeOpts}</select>`
    + `<select id="imagery-render" title="Band combination">${renderOpts}</select>`
    + extentCtl
    + `</div>`;
}

function imgShellHtml(bodyHtml) {
  const label = IMG_CFG.collection_label || "Satellite";
  return `<div class="imagery-head">`
    + `<span class="imagery-title">Context imagery — ${label}</span>`
    + imgControlsHtml()
    + `</div>${bodyHtml}`
    + `<div class="imagery-credit">`
    + `<a href="${IMG_CFG.credit_url}" target="_blank" rel="noopener noreferrer">`
    + `${IMG_CFG.credit}</a></div>`;
}

function imgRenderStatus(message) {
  const sec = document.getElementById("imagery-section");
  sec.classList.remove("imagery-empty");
  sec.innerHTML = imgShellHtml(
    `<div class="imagery-frame" style="height:120px">`
    + `<div class="imagery-status">${message}</div></div>`
  );
  imgBindControls();
}

function imgRenderScenes(pick, note) {
  const sec = document.getElementById("imagery-section");
  const s = SD[img.code];
  if (!s) return;
  sec.classList.remove("imagery-empty");

  if (!pick.scenes.length) {
    sec.innerHTML = imgShellHtml(
      `<div class="imagery-frame" style="height:110px"><div class="imagery-status">`
      + `No ${IMG_CFG.collection_label} acquisitions found for this location `
      + `on or before ${imgFormatDate(imgIsoDay(imgEndDate()))}.`
      + `</div></div>`
    );
    imgBindControls();
    return;
  }

  const bbox = imgChipBbox(s.lat, s.lon, img.extentKm, IMG_CFG.chip_aspect);
  const idx = Math.min(img.selected, pick.scenes.length - 1);
  const scene = pick.scenes[idx];
  const barKm = imgScaleBarKm(img.extentKm);
  const barPct = (barKm / img.extentKm) * 100;
  const barLabel = barKm < 1 ? `${barKm * 1000} m` : `${barKm} km`;

  // In date order the newest scene is often the cloudiest; flag the clearest
  // one in the strip so a usable image is one click away.
  let clearestIdx = -1;
  pick.scenes.forEach((sc, i) => {
    if (sc.cloud == null) return;
    if (clearestIdx < 0 || sc.cloud < pick.scenes[clearestIdx].cloud) clearestIdx = i;
  });
  const flagClearest = img.mode === "recent" && clearestIdx >= 0
    && pick.scenes.length > 1
    && (pick.scenes[clearestIdx].cloud ?? 100) < (pick.scenes[0].cloud ?? 0);

  const strip = pick.scenes.map((sc, i) => {
    const mark = (flagClearest && i === clearestIdx) ? " ★" : "";
    return `<button class="imagery-thumb${i === idx ? " active" : ""}" `
      + `data-idx="${i}" title="${sc.date} · ${sc.platform} · ${imgCloudLabel(sc.cloud)}`
      + `${mark ? " · clearest of these scenes" : ""}">`
      + `<img data-item="${sc.id}" alt="${sc.date} scene" loading="lazy">`
      + `<span class="th-date">${sc.date.slice(5)}${mark}</span>`
      + `<span class="th-cloud">${imgCloudLabel(sc.cloud)}</span></button>`;
  }).join("");

  const daysBack = Math.round(
    (imgEndDate() - new Date(`${scene.date}T00:00:00`)) / 864e5
  );
  const ageStr = Number.isFinite(daysBack) && daysBack > 0
    ? ` (${daysBack} day${daysBack === 1 ? "" : "s"} before the selected date)`
    : "";

  sec.innerHTML = imgShellHtml(
    // aspect-ratio reserves the right space while the chip loads, so the
    // caption below it does not jump when the image lands.
    `<div class="imagery-frame is-loading" id="imagery-frame" `
    + `style="aspect-ratio:${IMG_CFG.chip_aspect}">`
    + `<img id="imagery-chip" `
    + `alt="${IMG_CFG.collection_label} chip for ${s.name} on ${scene.date}">`
    + `<div class="imagery-status" id="imagery-chip-status">Loading imagery…</div>`
    + `<div class="imagery-marker"></div>`
    + `<div class="imagery-scalebar" style="width:${barPct.toFixed(1)}%">`
    + `<span>${barLabel}</span></div>`
    + `</div>`
    + `<div class="imagery-caption"><b>${imgFormatDate(scene.date)}</b>${ageStr}`
    + ` · ${scene.platform || IMG_CFG.collection_label} · ${imgCloudLabel(scene.cloud)}`
    + ` · <a id="imagery-hires" href="#" `
    + `target="_blank" rel="noopener noreferrer">larger ↗</a>`
    + ` · <a href="${IMG_CFG.item_url}/${encodeURIComponent(scene.id)}" `
    + `target="_blank" rel="noopener noreferrer">scene metadata ↗</a>`
    + `<br><span style="font-size:11px;color:#555">Ring marks the station; `
    + `chip is ${img.extentKm} km across. Cloud % is for the whole scene, `
    + `not this chip.${flagClearest ? " ★ marks the clearest scene below." : ""}`
    + `</span></div>`
    + (note ? `<div class="imagery-note">${note}</div>` : "")
    + `<div class="imagery-strip">${strip}</div>`
  );

  const chip = document.getElementById("imagery-chip");
  const frame = document.getElementById("imagery-frame");
  const status = document.getElementById("imagery-chip-status");
  chip.addEventListener("load", () => {
    frame.classList.remove("is-loading");
    if (status) status.remove();
    frame.style.aspectRatio = "auto";   // let the real chip set the height
    // A 1 km chip is only ~100 px of real data. Smooth upscaling turns that
    // into mush; nearest-neighbour keeps the 10 m pixels legible.
    chip.style.imageRendering =
      chip.naturalWidth && chip.naturalWidth < chip.clientWidth ? "pixelated" : "auto";
  });
  chip.addEventListener("error", () => {
    frame.classList.remove("is-loading");
    chip.style.display = "none";
    if (status) status.textContent = "This scene could not be rendered.";
  });

  // Stretch first, then paint: the chip URL carries the rescale, so the
  // sources cannot be set until the chip's own percentiles come back. The
  // strip shares the selected scene's stretch rather than firing six more
  // stats calls.
  const token = img.token;
  imgStretchFor(scene.id, bbox).then(rescale => {
    if (token !== img.token || !document.body.contains(chip)) return;
    chip.src = imgChipUrl(scene.id, bbox, IMG_CFG.chip_max_size, rescale);
    const hires = document.getElementById("imagery-hires");
    if (hires) {
      hires.href = imgChipUrl(scene.id, bbox, IMG_CFG.chip_hires_max_size, rescale);
    }
    const stripRescale = imgLoosenStretch(rescale, IMG_CFG.thumb_stretch_factor);
    sec.querySelectorAll(".imagery-thumb img").forEach(el => {
      el.src = imgChipUrl(el.dataset.item, bbox, IMG_CFG.thumb_max_size,
                          el.dataset.item === scene.id ? rescale : stripRescale);
    });
  });

  sec.querySelectorAll(".imagery-thumb").forEach(btn => {
    btn.addEventListener("click", () => {
      img.selected = Number(btn.dataset.idx);
      imgRenderScenes(pick, note);
    });
  });
  imgBindControls();
}

function imgBindControls() {
  const modeSel = document.getElementById("imagery-mode");
  const renderSel = document.getElementById("imagery-render");
  const extentSel = document.getElementById("imagery-extent");
  if (modeSel) modeSel.addEventListener("change", e => {
    img.mode = e.target.value;
    img.selected = 0;
    loadImagery(img.code);
  });
  if (renderSel) renderSel.addEventListener("change", e => {
    img.render = e.target.value;
    loadImagery(img.code);
  });
  const extentVal = document.getElementById("imagery-extent-val");
  if (extentSel) {
    // Live label while dragging, but only re-render on release — otherwise
    // every pixel of slider travel would fire a stats call and seven renders.
    extentSel.addEventListener("input", e => {
      if (extentVal) extentVal.textContent = `${Number(e.target.value)} km`;
    });
    extentSel.addEventListener("change", e => {
      const km = Number(e.target.value);
      if (km === img.extentKm) return;
      img.extentKm = km;
      img.selected = 0;
      loadImagery(img.code);
    });
  }
}

function imgClear() {
  img.token += 1;         // orphan any search still in flight
  img.code = null;
  img.sig = null;
  img.selected = 0;
  const sec = document.getElementById("imagery-section");
  sec.classList.add("imagery-empty");
  sec.innerHTML = "";
}

async function loadImagery(code) {
  if (!IMG_CFG.enabled || !SD[code]) return;
  const s = SD[code];
  const endDate = imgEndDate();

  // onMarkerClick re-runs on every variable/reference change too; only redraw
  // when something imagery actually depends on moved.
  const sig = `${code}|${imgIsoDay(endDate)}|${img.mode}|${img.render}`
            + `|${img.extentKm}`;
  if (sig === img.sig && document.getElementById("imagery-chip")) return;
  img.sig = sig;

  const token = ++img.token;
  img.code = code;

  const windowDays = img.mode === "clearest"
    ? IMG_CFG.clearest_window_days
    : IMG_CFG.recent_window_days;
  const key = `${code}|${imgIsoDay(endDate)}|${windowDays}`;

  let entry = imgSearchCache[key];
  if (!entry) {
    imgRenderStatus(`Searching ${IMG_CFG.collection_label} scenes…`);
    try {
      let all = await imgSearchScenes(s.lat, s.lon, endDate, windowDays);
      let widenedDays = 0;
      if (!all.length) {
        widenedDays = IMG_CFG.empty_window_days;
        all = await imgSearchScenes(s.lat, s.lon, endDate, widenedDays);
      }
      entry = {all, windowDays, widenedDays};
      imgSearchCache[key] = entry;
    } catch (e) {
      if (token !== img.token) return;
      imgRenderStatus(
        `Context imagery is unavailable right now (${e.message}). `
        + `Everything else on this panel is unaffected.`
      );
      return;
    }
  }
  if (token !== img.token) return;

  const bbox = imgChipBbox(s.lat, s.lon, img.extentKm, IMG_CFG.chip_aspect);
  const pick = imgPickScenes(entry.all, bbox);
  const notes = [];
  if (entry.widenedDays) {
    notes.push(
      `No acquisitions in the ${entry.windowDays} days before the selected `
      + `date — searched back ${entry.widenedDays} days instead `
      + `(polar night and long cloudy spells both do this).`
    );
  }
  if (pick.clipped) {
    notes.push(
      `No single granule covers a ${img.extentKm} km chip here, so part of `
      + `the image may be blank — a narrower chip usually fills in.`
    );
  }
  imgRenderScenes(pick, notes.join(" "));
}

// ─── Control event handlers ────────────────────────────────────────────────────
document.getElementById("sel-basemap").addEventListener("change", e => {
  Object.values(BASEMAPS).forEach(l => map.removeLayer(l));
  BASEMAPS[e.target.value].addTo(map);
  st.basemap = e.target.value;
});

document.getElementById("sel-var").addEventListener("change", e => {
  st.variable = e.target.value;
  updateTitle();
  updateSliderTrackColor();
  recolorAll();
  // Also update popup stats if open
  if (st.selectedCode) onMarkerClick(st.selectedCode);
});

document.getElementById("sel-ref").addEventListener("change", e => {
  st.ref = e.target.value;
  updateTitle();
  updateSliderTrackColor();
  recolorAll();
  if (st.selectedCode) onMarkerClick(st.selectedCode);
});

document.getElementById("sel-date").addEventListener("change", e => {
  st.dowy = parseInt(e.target.value);
  updateTitle();
  updateSnapToCurrentButton();
  recolorAll();
  if (st.selectedCode) onMarkerClick(st.selectedCode);
});

document.getElementById("sel-date").addEventListener("input", e => {
  const val = Math.round(parseInt(e.target.value));
  e.target.value = String(val);
  updateDatePreviewLabel(val);
  st.dowy = val;
  updateSnapToCurrentButton();
  if (sliderDragFrame) cancelAnimationFrame(sliderDragFrame);
  sliderDragFrame = requestAnimationFrame(() => {
    updateTitle();
    recolorAll();
  });
});

document.getElementById("close-btn").addEventListener("click", () => {
  document.getElementById("station-panel").classList.remove("visible");
  st.selectedCode = null;
  imgClear();
  recolorAll();
});

document.getElementById("chart-btn-wteq").addEventListener("click", () => {
  st.chartVar = "WTEQ";
  document.getElementById("chart-btn-wteq").className = "chart-btn active";
  document.getElementById("chart-btn-snwd").className = "chart-btn";
  if (st.selectedCode) loadChart(st.selectedCode, "WTEQ");
});

document.getElementById("chart-btn-snwd").addEventListener("click", () => {
  st.chartVar = "SNWD";
  document.getElementById("chart-btn-wteq").className = "chart-btn";
  document.getElementById("chart-btn-snwd").className = "chart-btn active";
  if (st.selectedCode) loadChart(st.selectedCode, "SNWD");
});

// ─── Network legend (interactive filter) ─────────────────────────────────────
function initNetworkFilter() {
  const container = document.getElementById("network-legend-rows");
  const netOrder = [
    "SNTL","SNTLT","MSNT","MPRC","SNOW","SCAN","COOP","CCSS","BCSS",
    "NVE","YSS","YKEC"
  ];
  const available = MAP_META.available_networks;

  // Count stations per network from SD
  const netCounts = {};
  for (const s of Object.values(SD)) {
    netCounts[s.net] = (netCounts[s.net] || 0) + 1;
  }

  // Sort by netOrder first, then alphabetically
  const sorted = available.slice().sort((a, b) => {
    const ia = netOrder.indexOf(a), ib = netOrder.indexOf(b);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return a.localeCompare(b);
  });

  for (const net of sorted) {
    const shapeSvg = NET_SHAPES[net]
      || '<circle cx="6" cy="6" r="5" fill="#666" stroke="#fff" stroke-width="0.5"/>';
    const label = NET_LABELS[net] || net;
    const count = netCounts[net] || 0;

    const row = document.createElement("div");
    row.className = "nlrow";
    row.dataset.net = net;
    row.innerHTML = `<div class="nshape"><svg width="12" height="12">${shapeSvg}</svg></div>`
      + `<span class="net-label">${label}</span>`
      + `<span class="net-count">(${count})</span>`;
    row.addEventListener("click", () => {
      const on = st.visibleNetworks.has(net);
      if (on) {
        st.visibleNetworks.delete(net);
        row.classList.add("net-off");
      } else {
        st.visibleNetworks.add(net);
        row.classList.remove("net-off");
      }
      recolorAll();
    });
    container.appendChild(row);
  }
}

// ─── Initialise ───────────────────────────────────────────────────────────────
initDateSlider();
initNetworkFilter();
initMarkers();
initPeriodicMarkers();
updateClockPanel();
setInterval(updateClockPanel, 1000);
updateTitle();
</script>
</body>
</html>
"""


def build_html(
    map_meta: dict,
    station_data: dict,
    periodic_data: list,
) -> str:
    """Substitute the data payloads and inlined assets into the template.

    The template is authoritative — no post-hoc string surgery on its
    JS (the old metres-era unit patches silently broke when the target
    strings drifted).
    """
    asset_tags = _build_frontend_asset_tags()
    html = _HTML_TEMPLATE.replace(
        "__MAP_META__", json.dumps(map_meta, separators=(",", ":"))
    )
    html = html.replace(
        "__STATION_DATA__",
        json.dumps(station_data, separators=(",", ":")),
    )
    html = html.replace(
        "__PERIODIC_DATA__",
        json.dumps(periodic_data, separators=(",", ":")),
    )
    html = html.replace(
        (
            '<link rel="stylesheet" '
            'href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>'
        ),
        asset_tags["leaflet_css"],
    )
    html = html.replace(
        (
            '<script '
            'src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
        ),
        asset_tags["leaflet_js"],
    )
    html = html.replace(
        (
            '<script '
            'src="https://cdn.plot.ly/plotly-basic-2.30.0.min.js"></script>'
        ),
        asset_tags["plotly_js"],
    )
    return html


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate live SWE map from station CSVs"
    )
    ap.add_argument("--geojson", default=str(GEOJSON_PATH))
    ap.add_argument("--csv-dir", default=str(CSV_DIR))
    ap.add_argument("--charts-dir", default=str(CHARTS_DIR))
    ap.add_argument("--output", default=str(OUTPUT_HTML))
    args = ap.parse_args()

    geojson_path = Path(args.geojson)
    csv_dir = Path(args.csv_dir)
    charts_dir = Path(args.charts_dir)
    output_path = Path(args.output)

    if not geojson_path.exists():
        logger.error(f"GeoJSON not found: {geojson_path}")
        sys.exit(1)
    if not csv_dir.exists():
        logger.error(f"CSV dir not found: {csv_dir}")
        sys.exit(1)

    charts_dir.mkdir(parents=True, exist_ok=True)
    for p in charts_dir.glob("*.json"):
        p.unlink()

    with geojson_path.open(encoding="utf-8") as f:
        inventory = json.load(f)
    features = inventory.get("features", [])
    logger.info(f"Loaded {len(features)} stations from GeoJSON")

    # The map charts exactly the probe-verified daily-or-better stations;
    # every other point observation goes on the periodic toggle layer
    # (DESIGN.md §8).
    meta_by_code: dict = {}
    periodic_data: list = []
    for feat in features:
        props = feat.get("properties", {})
        code = str(props.get("code") or "")
        lat, lon = props.get("latitude"), props.get("longitude")
        if not code or lat is None or lon is None:
            continue
        if props.get("daily_or_better"):
            meta_by_code[code] = {
                "lat": float(lat),
                "lon": float(lon),
                "name": _clean_meta_text(props.get("name")) or code,
                "network_code": _clean_meta_text(props.get("network_code")),
                "state": _clean_meta_text(props.get("state")),
                "elevation": props.get("elevation_m"),
                "operator": _clean_meta_text(props.get("operator")),
                "client": _clean_meta_text(props.get("client")),
                "data_provider": _clean_meta_text(
                    props.get("data_provider")
                ),
                "data_variables": props.get("data_variables") or [],
                "station_url": _clean_meta_text(props.get("station_url")),
                "station_image_url": _clean_meta_text(
                    props.get("station_image_url")
                ),
                "station_camera_url": _clean_meta_text(
                    props.get("station_camera_url")
                ),
                "daily_provenance": _clean_meta_text(
                    props.get("daily_provenance")
                ),
                "possible_duplicates": props.get("possible_duplicates"),
                "latest_record_date": _clean_meta_text(
                    props.get("latest_record_date")
                ),
                "csv_refreshed_at_utc": _clean_meta_text(
                    props.get("csv_refreshed_at_utc")
                ),
                "metadata_fetched_at": _clean_meta_text(
                    props.get("metadata_fetched_at")
                ),
            }
        else:
            periodic_data.append({
                "code": code,
                "lat": round(float(lat), 5),
                "lon": round(float(lon), 5),
                "name": _clean_meta_text(props.get("name")) or code,
                "net": _clean_meta_text(props.get("network_code")),
                "cli": _clean_meta_text(props.get("client")),
                "op": _clean_meta_text(props.get("operator")),
                "dp": _clean_meta_text(props.get("data_provider")),
                "url": _clean_meta_text(props.get("station_url")),
                "cam": _clean_meta_text(props.get("station_camera_url")),
                "vars": _vars_by_interval(props.get("data_variables")),
                "dups": props.get("possible_duplicates") or [],
            })
    logger.info(
        f"{len(meta_by_code)} daily-or-better stations, "
        f"{len(periodic_data)} periodic/non-daily sites"
    )

    now = datetime.now(timezone.utc)
    today_ts = pd.Timestamp(now.date())
    today_dowy = day_of_water_year(today_ts)
    current_wy = int(water_year(today_ts))
    embed_wys = list(range(current_wy - N_PAST_WYS, current_wy + 1))

    logger.info(f"Today: {today_ts.date()}, DOWY {today_dowy}, WY{current_wy}")
    logger.info(f"Embedding WYs: {embed_wys}")

    station_codes = sorted(meta_by_code.keys())
    logger.info(f"Processing {len(station_codes)} stations...")

    station_data: dict = {}
    processed = 0
    failed = 0

    for i, code in enumerate(station_codes, 1):
        meta = meta_by_code[code]
        csv_path = csv_dir / f"{code}.csv"
        result = process_station_from_csv(
            code=code,
            csv_path=csv_path,
            meta=meta,
            today_dowy=today_dowy,
            current_wy=current_wy,
            embed_wys=embed_wys,
        )
        if result is not None:
            chart_payload = result.pop("_chart", None)
            if chart_payload is not None:
                (charts_dir / f"{code}.json").write_text(
                    json.dumps(chart_payload, separators=(",", ":")),
                    encoding="utf-8",
                )
            station_data[code] = result
            processed += 1
        else:
            failed += 1

        if i % 50 == 0:
            logger.info(
                (
                    f"  {i}/{len(station_codes)} processed "
                    f"({processed} ok, {failed} failed)"
                )
            )

    logger.info(f"Done: {processed} stations, {failed} failed/empty")
    if processed == 0 and station_codes:
        raise RuntimeError(
            "No station CSVs were usable. Run fetch-data before live-map."
        )

    # Keep only duplicate links whose twin is actually charted — a
    # daily-or-better candidate without a usable CSV is on neither map
    # layer, so its pan-to link would go nowhere.  The inventory keeps
    # the full daily<->daily links; this filter is display-only.
    charted = {(s.get("cli"), c) for c, s in station_data.items()}
    dropped_links = 0
    for s in station_data.values():
        if s.get("dups"):
            kept = [
                d for d in s["dups"]
                if (d.get("client"), d.get("code")) in charted
            ]
            dropped_links += len(s["dups"]) - len(kept)
            s["dups"] = kept
    if dropped_links:
        logger.info(
            f"Dropped {dropped_links} duplicate links to unchartable "
            f"stations from popups"
        )

    available_networks = sorted({v["net"] for v in station_data.values()})
    map_meta = {
        "generated": now.isoformat(),
        "today_date": str(today_ts.date()),
        "today_dowy": today_dowy,
        "current_wy": current_wy,
        "min_years": MIN_YEARS,
        "n_stations": len(station_data),
        "available_networks": available_networks,
        "imagery": IMAGERY_CONFIG,
    }

    logger.info(
        f"Building HTML for {len(station_data)} daily stations "
        f"+ {len(periodic_data)} periodic sites"
    )
    html = build_html(map_meta, station_data, periodic_data)
    output_path.write_text(html, encoding="utf-8")
    size_mb = output_path.stat().st_size / 1e6
    logger.info(f"Written: {output_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
