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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request

import geopandas as gpd
import numpy as np
import pandas as pd
from utils import day_of_water_year, water_year

REPO_ROOT = Path(__file__).resolve().parent.parent

GEOJSON_PATH = REPO_ROOT / "all_daily_snow_stations.geojson"
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


def _parse_var_list(raw) -> list[str]:
    txt = _clean_meta_text(raw)
    if not txt:
        return []
    parts = re.split(r"[,;|]", txt)
    return sorted({v.strip() for v in parts if v.strip()})


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

    network = str(meta.get("network") or "SNTL")
    state_code = _clean_meta_text(meta.get("state") or "")
    if not state_code and code.count("_") >= 2:
        state_code = code.split("_")[1]

    station_name = _clean_meta_text(meta.get("name") or code)
    daily_vars = _parse_var_list(meta.get("variables_daily"))
    hourly_vars = _parse_var_list(meta.get("variables_hourly"))

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
        _clean_meta_text(meta.get("updated_date"))
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
        "vars_d": ", ".join(daily_vars),
        "vars_h": ", ".join(hourly_vars),
        "upd": upd,
        "mtype": "automated",
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
#station-info .info-row{display:flex;gap:4px}
#station-info .info-key{color:#555;min-width:120px;font-weight:500}
#station-info .swe-line{margin:6px 0;padding:6px 8px;border-radius:4px;background:#e8f0fe}
#station-info .snwd-line{margin:6px 0;padding:6px 8px;border-radius:4px;background:#e8fef0}
#station-info .na-line{color:#888;font-style:italic;font-size:12px}
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
          <option value="cartodb">CartoDB Light</option>
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
    </div>
  </div>
</div>

<!-- ═══════════════════ DATA ═══════════════════ -->
<script>
const MAP_META = __MAP_META__;
const SD = __STATION_DATA__;
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
  basemap: "cartodb",
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
};

const NET_LABELS = {
  SNTL:"SNOTEL", SNTLT:"SNOTEL Lite", MSNT:"Manual SNOTEL",
  MPRC:"Manual", SNOW:"Manual",
  SCAN:"SCAN", COOP:"COOP",
  CCSS:"CCSS", BCSS:"BC Snow Survey",
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
    pct = Math.round((cur * 1000 / med_mm) * 1000) / 10;  // one decimal place
  }
  return {pct, n, cur, curDowy, med_mm};
}

function formatObsSummary(code, variable) {
  const obs = getStationPct(code, st.dowy, st.wy, variable, st.ref);
  const varLabel = variable === "WTEQ" ? "SWE" : "Snow Depth";
  if (obs.cur === null) {
    return `${varLabel}: No recent data`;
  }
  const valStr = `${(obs.cur * 100).toFixed(1)} cm`;
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
const BASEMAPS = {
  cartodb: L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    {attribution:"&copy; OpenStreetMap contributors &copy; CARTO",maxZoom:19,subdomains:"abcd"}
  ),
  esri_sat: L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    {attribution:"&copy; Esri",maxZoom:19}
  ),
  esri_topo: L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    {attribution:"&copy; Esri",maxZoom:19}
  ),
};

const map = L.map("map", {
  center: [43, -112], zoom: 5,
  layers: [BASEMAPS.cartodb],
  zoomControl: true,
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
      `<b>${s.name}</b><br>Code: ${code}<br>Network: ${NET_LABELS[s.net]||s.net}<br>${varSummary}`,
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
      `<b>${s.name}</b><br>Code: ${code}<br>Network: ${NET_LABELS[s.net]||s.net}<br>${varSummary}`
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
  st.selectedCode = code;
  recolorAll();  // highlight selected

  const s = SD[code];
  const panel = document.getElementById("station-panel");
  panel.classList.add("visible");

  const stateName = STATE_NAMES[s.st] || s.st || "—";
  const elevStr = s.elev_m != null ? `${s.elev_m} m` : "—";
  const netLabel = NET_LABELS[s.net] || s.net || "—";
  const updStr = s.upd ? s.upd.replace("T", " ").replace("Z", " UTC") : "—";
  const stationUrl = s.url || "";

  let stationPhotoHtml = "";
  if (s.img) {
    const operator = s.op || "Station Operator";
    stationPhotoHtml = `<div id="station-photo-wrap">`
      + `<img id="station-photo" src="${s.img}" alt="${s.name} station photo" loading="lazy" referrerpolicy="no-referrer">`
      + `<div id="station-photo-credit">Photo credit: <a href="${s.img}" target="_blank" rel="noopener noreferrer">${operator}</a></div>`
      + `</div>`;
  } else {
    stationPhotoHtml = `<div id="station-photo-wrap"><div id="station-photo-no-img">No station image available</div></div>`;
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
    const valCm = (obs.cur * 100).toFixed(1);
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
    <div class="info-row"><span class="info-key">Client:</span><span>${clientStr}</span></div>
    <div class="info-row"><span class="info-key">State:</span><span>${stateName}</span></div>
    <div class="info-row"><span class="info-key">Elevation:</span><span>${elevStr}</span></div>
    <div class="info-row"><span class="info-key">Daily variables:</span><span style="font-size:11px">${s.vars_d||"—"}</span></div>
    <div class="info-row"><span class="info-key">Hourly variables:</span><span style="font-size:11px">${s.vars_h||"—"}</span></div>
    <div class="info-row"><span class="info-key">Earliest record:</span><span>${s.bdate||"—"}</span></div>
    <div class="info-row"><span class="info-key">Latest record:</span><span>${s.edate||"—"}</span></div>
    <div class="info-row"><span class="info-key">Last updated:</span><span>${updStr}</span></div>
    <div class="info-row"><span class="info-key">Station page:</span><span>${stationUrl ? `<a href="${stationUrl}" target="_blank" rel="noopener noreferrer">${stationUrl}</a>` : "—"}</span></div>
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
}

// ─── Chart rendering (CSV fetched lazily) ─────────────────────────────────────
const chartCache = {};  // code+var → parsed CSV stats

async function loadChart(code, variable) {
  const cacheKey = code + "_" + variable;
  document.getElementById("chart-loading").textContent = "Loading chart data…";
  document.getElementById("chart-div").innerHTML = "";

  let stats;
  if (chartCache[cacheKey]) {
    stats = chartCache[cacheKey];
  } else {
    try {
      const resp = await fetch(`./data/${code}.csv`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const text = await resp.text();
      stats = parseCSVForChart(text, variable);
      chartCache[cacheKey] = stats;
    } catch (e) {
      document.getElementById("chart-loading").textContent = `Could not load chart data: ${e.message}`;
      return;
    }
  }

  document.getElementById("chart-loading").textContent = "";
  renderChart(code, variable, stats);
}

function parseCSVForChart(csvText, variable) {
  const lines = csvText.trim().split("\\n");
  const headers = lines[0].split(",");
  const varIdx = headers.indexOf(variable);
  if (varIdx < 0) return null;

  // Build: {wy: {dowy: value}}
  const wyData = {};
  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i].split(",");
    const val = parseFloat(cols[varIdx]);
    if (isNaN(val) || val < -1e-9 || !cols[0]) continue;  // include 0 (bare ground)
    const d = new Date(cols[0]);
    if (isNaN(d.getTime())) continue;
    const m = d.getMonth() + 1;
    const y = d.getFullYear();
    const wy = m >= 10 ? y + 1 : y;
    const wyStart = new Date(wy - 1, 9, 1);
    const dowy = Math.round((d - wyStart) / 864e5) + 1;
    if (dowy < 1 || dowy > 366) continue;
    if (!wyData[wy]) wyData[wy] = {};
    wyData[wy][dowy] = val;
  }

  // For each DOWY, collect all values across years
  const p10=[],p20=[],p30=[],p40=[],p50=[],p60=[],p70=[],p80=[],p90=[];
  const mins=[],maxs=[],minYrs=[],maxYrs=[];
  for (let dowy = 1; dowy <= 366; dowy++) {
    const vals = [];
    const wyVals = {};
    for (const [wy, dowyMap] of Object.entries(wyData)) {
      if (dowyMap[dowy] !== undefined) {
        vals.push(dowyMap[dowy]);
        wyVals[parseInt(wy)] = dowyMap[dowy];
      }
    }
    if (vals.length === 0) {
      p10.push(null);p20.push(null);p30.push(null);p40.push(null);p50.push(null);
      p60.push(null);p70.push(null);p80.push(null);p90.push(null);
      mins.push(null);maxs.push(null);minYrs.push(null);maxYrs.push(null);
      continue;
    }
    vals.sort((a,b) => a - b);
    const q = p => {
      const i = p * (vals.length - 1);
      const lo = Math.floor(i), hi = Math.ceil(i);
      return vals[lo] + (vals[hi] - vals[lo]) * (i - lo);
    };
    p10.push(q(0.10)); p20.push(q(0.20)); p30.push(q(0.30)); p40.push(q(0.40));
    p50.push(q(0.50)); p60.push(q(0.60)); p70.push(q(0.70)); p80.push(q(0.80));
    p90.push(q(0.90));
    mins.push(vals[0]); maxs.push(vals[vals.length-1]);
    // Year of min/max
    let minV = vals[0], maxV = vals[vals.length-1], miny=null, maxy=null;
    for (const [wy, v] of Object.entries(wyVals)) {
      if (v === minV && miny === null) miny = parseInt(wy);
      if (v === maxV && maxy === null) maxy = parseInt(wy);
    }
    minYrs.push(miny); maxYrs.push(maxy);
  }
  return {p10,p20,p30,p40,p50,p60,p70,p80,p90,mins,maxs,minYrs,maxYrs};
}

function renderChart(code, variable, stats) {
  if (!stats) {
    document.getElementById("chart-loading").textContent = "No chart data available.";
    return;
  }
  const s = SD[code];
  const scale = 100;  // m → cm
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
    "SNTL","SNTLT","MSNT","MPRC","SNOW","SCAN","COOP","CCSS","BCSS"
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
updateClockPanel();
setInterval(updateClockPanel, 1000);
updateTitle();
</script>
</body>
</html>
"""


def build_html(map_meta: dict, station_data: dict, generated_at: str) -> str:
    asset_tags = _build_frontend_asset_tags()
    meta_js = json.dumps(map_meta, separators=(",", ":"))
    stations_js = json.dumps(station_data, separators=(",", ":"))
    html = _HTML_TEMPLATE.replace("__MAP_META__", meta_js)
    html = html.replace("__STATION_DATA__", stations_js)
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
    html = html.replace(
        "const scale = 100;  // m → cm",
        "const scale = 1;  // values already in cm",
    )
    html = html.replace("(obs.cur * 100).toFixed(1)", "(obs.cur).toFixed(1)")
    html = html.replace("(cur * 1000 / med_mm)", "(cur * 10 / med_mm)")

    chart_start = html.find("const chartCache = {};")
    chart_end = html.find(
        "function renderChart(code, variable, stats) {",
        chart_start,
    )
    if chart_start < 0 or chart_end < 0:
        raise RuntimeError("Could not locate chart loader block in template")

    chart_loader = """const chartCache = {};  // code -> chart payload

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

"""

    html = html[:chart_start] + chart_loader + html[chart_end:]
    html = html.replace("__LEAFLET_CSS_TAG__", asset_tags["leaflet_css"])
    html = html.replace("__LEAFLET_JS_TAG__", asset_tags["leaflet_js"])
    html = html.replace("__PLOTLY_JS_TAG__", asset_tags["plotly_js"])
    html = html.replace("__GENERATED_AT__", generated_at)
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

    gdf = gpd.read_file(geojson_path)
    logger.info(f"Loaded {len(gdf)} stations from GeoJSON")

    meta_by_code: dict = {}
    for _, row in gdf.iterrows():
        code = str(row.get("stationTriplet") or row.get("code") or "")
        if not code:
            continue
        network = (
            _clean_meta_text(row.get("networkCode") or row.get("network"))
            or "SNTL"
        )
        meta_by_code[code] = {
            "lat": float(row.geometry.y),
            "lon": float(row.geometry.x),
            "name": _clean_meta_text(row.get("name")) or code,
            "network": network,
            "state": _clean_meta_text(row.get("state")),
            "elevation": row.get("elevation_m") or row.get("elevation"),
            "operator": _clean_meta_text(
                row.get("Operator") or row.get("operator")
            ),
            "client": _clean_meta_text(row.get("client")),
            "variables_daily": _clean_meta_text(row.get("variables_daily")),
            "variables_hourly": _clean_meta_text(row.get("variables_hourly")),
            "station_url": _clean_meta_text(row.get("station_url")),
            "station_image_url": _clean_meta_text(
                row.get("station_image_url")
            ),
            "updated_date": _clean_meta_text(row.get("updated_date")),
            "csv_refreshed_at_utc": _clean_meta_text(
                row.get("csv_refreshed_at_utc")
            ),
            "metadata_fetched_at": _clean_meta_text(
                row.get("metadata_fetched_at")
            ),
        }

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

    available_networks = sorted({v["net"] for v in station_data.values()})
    map_meta = {
        "generated": now.isoformat(),
        "today_date": str(today_ts.date()),
        "today_dowy": today_dowy,
        "current_wy": current_wy,
        "min_years": MIN_YEARS,
        "n_stations": len(station_data),
        "available_networks": available_networks,
    }

    logger.info(f"Building HTML for {len(station_data)} stations")
    html = build_html(
        map_meta,
        station_data,
        now.strftime("%Y-%m-%d %H:%M:%S UTC"),
    )
    output_path.write_text(html, encoding="utf-8")
    size_mb = output_path.stat().st_size / 1e6
    logger.info(f"Written: {output_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
