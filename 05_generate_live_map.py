#!/usr/bin/env python3
"""
Generate the live interactive SWE / Snow Depth percent-of-normal map.

Input: zarr store (from 02_init_zarr.py) + geojson (stations with metadata)
Output: live_swe_map.html  (self-contained, all data embedded)

Features
--------
- Variable selector: SWE (WTEQ) or Snow Depth (SNWD)
- Reference period: Period of Record (default), 1991-2020, 1981-2010, 1971-2000
- Basemap: CartoDB Light (default), Esri WorldImagery, Esri Topo
- Date slider within current water year (with live date preview)
- 7-colour legend: <50% dark-red | 50-70% orange | 70-90% yellow | 90-110% green
                   110-130% light-blue | 130-150% dark-blue | >150% purple | gray
- 10-year minimum: fewer than 10 years in the chosen period → gray dot
- Hover tooltip: name, code, network, % normal
- Station popup: full metadata + interactive Plotly chart
- Chart: POR percentile envelope with decile bands (min-10th, ..., 90th-max)
         median (green), min (red), max (blue), current-WY dots (black)
- Network marker shapes: unique symbol per network code
"""

import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib import request

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr

sys.path.insert(0, str(Path(__file__).parent))
from utils import water_year, day_of_water_year

REPO_ROOT = Path(__file__).resolve().parent
ZARR_PATH = REPO_ROOT / "snow_stations.zarr"
GEOJSON_PATH = REPO_ROOT / "snow_stations.geojson"
OUTPUT_HTML = REPO_ROOT / "live_swe_map.html"
CHARTS_DIR = REPO_ROOT / "charts"
ASSET_CACHE_DIR = REPO_ROOT / ".cache" / "live_map_assets"

LEAFLET_CSS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS_URL = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
PLOTLY_JS_URL = "https://cdn.plot.ly/plotly-basic-2.30.0.min.js"

# ── Constants ──────────────────────────────────────────────────────────────────

N_DOWY = 366  # full water year: Oct 1 (DOWY 1) through Sep 30 (DOWY 365/366)
MIN_YEARS = 10  # minimum years of historical data to assign a color

REF_PERIODS = {
    "por":   (None,  None),
    "n9120": (1991, 2020),
    "n8110": (1981, 2010),
    "n7100": (1971, 2000),
}

# Embed only current water year in the map to keep payload smaller.
N_PAST_WYS = 0

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _elev_m(raw, network: str) -> float | None:
    """Return elevation in metres. GeoJSON already stores metres."""
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    return round(float(raw), 1)


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
            p10.append(None); p20.append(None); p30.append(None); p40.append(None); p50.append(None)
            p60.append(None); p70.append(None); p80.append(None); p90.append(None)
            mins.append(None); maxs.append(None); min_yrs.append(None); max_yrs.append(None)
            continue

        vals = day.to_numpy(dtype=float)
        years = day.index.to_numpy(dtype=int)

        q = np.quantile(vals, [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90])
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
    # Prevent accidental script-tag termination when inlining vendor JS.
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


def process_station_from_zarr(
    ds: xr.Dataset,
    code: str,
    meta: dict,
    today_ts: pd.Timestamp,
    today_dowy: int,
    current_wy: int,
    embed_wys: list[int],
) -> dict | None:
    """
    Extract data for one station from Zarr. Returns a compact dict for embedding in HTML.
    Returns None if the station has no usable data.
    """
    try:
        station_ds = ds.sel(station=code)
    except KeyError:
        logger.warning(f"{code}: not found in zarr store")
        return None

    if station_ds.sizes.get("time", 0) == 0:
        return None

    # Convert to DataFrame for easier manipulation
    df = station_ds.to_dataframe().reset_index()
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time")

    if df.empty:
        return None

    # Add water-year and DOWY columns (fully vectorised)
    idx = df["time"]
    months = idx.dt.month
    years = idx.dt.year
    wy_arr = np.where(months >= 10, years + 1, years)
    wy_start_ts = pd.to_datetime({"year": wy_arr - 1, "month": 10, "day": 1})
    dowy_arr = (idx - pd.DatetimeIndex(wy_start_ts)).dt.days + 1

    df["_wy"] = wy_arr
    df["_dowy"] = dowy_arr

    snow = df[(df["_dowy"] >= 1) & (df["_dowy"] <= N_DOWY)].copy()

    # ── Current values ────────────────────────────────────────────────────────
    cur = {}
    for var in ("WTEQ", "SNWD"):
        if var in snow.columns:
            s = snow[var].dropna()
            if not s.empty:
                val = float(s.iloc[-1])
                if not np.isnan(val):
                    cur[var] = {
                        "val": round(val, 4),
                        "date": str(snow["time"].iloc[-1].date()),
                    }

    # ── Today's pct-normal for each ref × variable ────────────────────────────
    stat = {}  # {var_lower: {ref_key: {pct, n, med_cm}}}

    for var in ("WTEQ", "SNWD"):
        vk = var.lower()
        stat[vk] = {}
        if var not in snow.columns:
            for rk in REF_PERIODS:
                stat[vk][rk] = {"pct": None, "n": 0, "med": None}
            continue

        pivot = snow.pivot_table(
            index="_dowy", columns="_wy", values=var, aggfunc="first"
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

    # ── Historical medians for ALL DOWYs (for date slider) ───────────────────
    # Stored as flat integer arrays (mm precision) for compactness.
    meds = {}

    for var in ("WTEQ", "SNWD"):
        vk = var.lower()
        if var not in snow.columns:
            for rk in REF_PERIODS:
                meds[f"pm_{rk}_{vk}"] = [0] * N_DOWY
                meds[f"pn_{rk}_{vk}"] = [0] * N_DOWY
            continue

        pivot = snow.pivot_table(
            index="_dowy", columns="_wy", values=var, aggfunc="first"
        )

        for rk, (y0, y1) in REF_PERIODS.items():
            if y0 is None:
                p = pivot
            else:
                cols = [c for c in pivot.columns if y0 <= c <= y1]
                p = pivot[cols] if cols else pd.DataFrame()

            if not p.empty:
                pr = p.reindex(range(1, N_DOWY + 1))
                med_arr = (
                    (pr.median(axis=1) * 10)
                    .clip(0, 32767).fillna(0).round().astype(int).tolist()
                )
                n_arr = (
                    pr.count(axis=1).clip(0, 255).fillna(0).astype(int).tolist()
                )
            else:
                med_arr = [0] * N_DOWY
                n_arr = [0] * N_DOWY

            meds[f"pm_{rk}_{vk}"] = med_arr
            meds[f"pn_{rk}_{vk}"] = n_arr

    # ── Water-year data for the last N_PAST_WYS + current WY ─────────────────
    wy_data = {}
    chart = {"wteq": None, "snwd": None}

    for wy in embed_wys:
        wy_df = snow[snow["_wy"] == wy].sort_values("_dowy")
        if wy_df.empty:
            continue
        wy_entry = {}
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

    for var, vk in (("WTEQ", "wteq"), ("SNWD", "snwd")):
        if var not in snow.columns:
            continue
        pivot = snow.pivot_table(index="_dowy", columns="_wy", values=var, aggfunc="first")
        chart[vk] = _build_chart_stats(pivot)

    # ── Network / metadata ────────────────────────────────────────────────────
    network = str(meta.get("network") or "SNTL")
    state_code = _clean_meta_text(meta.get("state") or "")
    if not state_code and code.count("_") >= 2:
        state_code = code.split("_")[1]

    daily_vars = _parse_var_list(meta.get("variables_daily"))
    hourly_vars = _parse_var_list(meta.get("variables_hourly"))
    station_name = _clean_meta_text(meta.get("name") or code)

    elev = _elev_m(meta.get("elevation"), network)
    mtype = str(meta.get("measurement_type") or "automated").lower()

    obs_cols = [c for c in ("WTEQ", "SNWD") if c in df.columns]
    if obs_cols:
        valid_mask = df[obs_cols].notna().any(axis=1)
        if valid_mask.any():
            valid_times = df.loc[valid_mask, "time"]
            bdate = str(valid_times.min().date())
            edate = str(valid_times.max().date())
        else:
            bdate = ""
            edate = ""
    else:
        bdate = ""
        edate = ""

    upd = _clean_meta_text(meta.get("metadata_fetched_at") or "")
    if not upd and edate:
        upd = edate

    station_dict = {
        "lat": round(meta["lat"], 5),
        "lon": round(meta["lon"], 5),
        "name": station_name,
        "url": _clean_meta_text(meta.get("station_url")),
        "img": _clean_meta_text(meta.get("station_image_url")),
        "net": network,
        "st": state_code,
        "elev_m": elev,
        "bdate": bdate,
        "edate": edate,
        "vars_d": ", ".join(daily_vars),
        "vars_h": ", ".join(hourly_vars),
        "upd": upd,
        "mtype": mtype,
        # Current values
        "wteq": cur.get("WTEQ", {}).get("val"),
        "snwd": cur.get("SNWD", {}).get("val"),
        "wteq_d": cur.get("WTEQ", {}).get("date"),
        "snwd_d": cur.get("SNWD", {}).get("date"),
        # Today's pct-normals
        "stat": stat,
        # Historical medians (flat int arrays, mm)
        **meds,
        # Water-year time series
        "wy": wy_data,
        # Chart stats are written to per-station JSON files to keep HTML smaller.
        "_chart": chart,
    }
    return station_dict


def _load_reference_template() -> str:
        """Load the full UI template from the reference generator script.

        This keeps the global map frontend in feature parity (slider + charts)
        with the richer implementation used in the station workflow.
        """
        ref_path = (
                Path("/home/eric/repos/snotel_ccss_stations_v2/")
                / "snotel_ccss_stations/generate_swe_map.py"
        )
        text = ref_path.read_text(encoding="utf-8")

        start_token = '_HTML_TEMPLATE = """\\\n'
        end_token = '\n"""\n\n\n# ── Main'
        start_idx = text.find(start_token)
        if start_idx < 0:
                raise RuntimeError("Could not locate reference _HTML_TEMPLATE start")
        start_idx += len(start_token)
        end_idx = text.find(end_token, start_idx)
        if end_idx < 0:
                raise RuntimeError("Could not locate reference _HTML_TEMPLATE end")

        return text[start_idx:end_idx]


_HTML_TEMPLATE = _load_reference_template()


def build_html(map_meta: dict, station_data: dict, generated_at: str) -> str:
    asset_tags = _build_frontend_asset_tags()
    meta_js = json.dumps(map_meta, separators=(",", ":"))
    stations_js = json.dumps(station_data, separators=(",", ":"))
    html = _HTML_TEMPLATE.replace("__MAP_META__", meta_js)
    html = html.replace("__STATION_DATA__", stations_js)
    # Replace CDN tags with inlined assets when available.
    html = html.replace(
        '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>',
        asset_tags["leaflet_css"],
    )
    html = html.replace(
        '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>',
        asset_tags["leaflet_js"],
    )
    html = html.replace(
        '<script src="https://cdn.plot.ly/plotly-basic-2.30.0.min.js"></script>',
        asset_tags["plotly_js"],
    )
    # Use GeoJSON-provided station image URLs only.
    html = html.replace(
        "if ((s.net === \"SNTL\" || s.net === \"SNTLT\") && /^\\\\d+_/.test(code)) {",
        "if (s.img) {",
    )
    html = html.replace("const siteNum = code.split(\"_\")[0];", "")
    html = html.replace(
        "const photoUrl = `https://www.wcc.nrcs.usda.gov/siteimages/${siteNum}.jpg`;",
        "const photoUrl = s.img;",
    )
    # Units are already in centimeters upstream; frontend must not rescale.
    html = html.replace(
        "const scale = 100;  // m → cm",
        "const scale = 1;  // values already in cm",
    )
    html = html.replace("(obs.cur * 100).toFixed(1)", "(obs.cur).toFixed(1)")
    html = html.replace("(cur * 1000 / med_mm)", "(cur * 10 / med_mm)")

    chart_start = html.find("const chartCache = {};")
    chart_end = html.find("function renderChart(code, variable, stats) {", chart_start)
    if chart_start < 0 or chart_end < 0:
        raise RuntimeError("Could not locate chart loader block in template")
    chart_loader = '''const chartCache = {};  // code -> chart payload\n\nasync function loadChart(code, variable) {\n  document.getElementById("chart-loading").textContent = "Loading chart data…";\n  document.getElementById("chart-div").innerHTML = "";\n\n  let payload = chartCache[code];\n  if (!payload) {\n    try {\n      const resp = await fetch(`./charts/${code}.json`);\n      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);\n      payload = await resp.json();\n      chartCache[code] = payload;\n    } catch (e) {\n      document.getElementById("chart-loading").textContent = `Could not load chart data: ${e.message}`;\n      return;\n    }\n  }\n\n  const key = variable === "WTEQ" ? "wteq" : "snwd";\n  const stats = payload[key] || null;\n  if (!stats) {\n    document.getElementById("chart-loading").textContent = "No chart data available.";\n    return;\n  }\n\n  document.getElementById("chart-loading").textContent = "";\n  renderChart(code, variable, stats);\n}\n\n'''
    html = html[:chart_start] + chart_loader + html[chart_end:]
    html = html.replace("__LEAFLET_CSS_TAG__", asset_tags["leaflet_css"])
    html = html.replace("__LEAFLET_JS_TAG__", asset_tags["leaflet_js"])
    html = html.replace("__PLOTLY_JS_TAG__", asset_tags["plotly_js"])
    html = html.replace("__GENERATED_AT__", generated_at)
    return html


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Generate live SWE map from Zarr store")
    ap.add_argument("--geojson", default=str(GEOJSON_PATH))
    ap.add_argument("--zarr", default=str(ZARR_PATH))
    ap.add_argument("--charts-dir", default=str(CHARTS_DIR))
    ap.add_argument("--output", default=str(OUTPUT_HTML))
    args = ap.parse_args()

    geojson_path = Path(args.geojson)
    zarr_path = Path(args.zarr)
    charts_dir = Path(args.charts_dir)
    output_path = Path(args.output)

    # ── Load store and metadata ────────────────────────────────────────────────
    if not zarr_path.exists():
        logger.error(f"Zarr store not found: {zarr_path}")
        sys.exit(1)

    if not geojson_path.exists():
        logger.error(f"GeoJSON not found: {geojson_path}")
        sys.exit(1)

    charts_dir.mkdir(parents=True, exist_ok=True)
    for p in charts_dir.glob("*.json"):
        p.unlink()

    logger.info(f"Loading zarr from {zarr_path}")
    ds = xr.open_zarr(zarr_path)
    logger.info(f"Loaded zarr with {ds.sizes.get('station', 0)} stations")

    gdf = gpd.read_file(geojson_path)
    logger.info(f"Loaded {len(gdf)} stations from GeoJSON")

    # Build metadata lookup
    meta_by_code: dict = {}
    for _, row in gdf.iterrows():
        code = str(row.get("stationTriplet") or row.get("code") or "")
        if not code:
            continue
        network = _clean_meta_text(row.get("networkCode") or row.get("network")) or "SNTL"
        
        # Extract available variables from elementCodes list
        element_codes = row.get("elementCodes", [])
        if isinstance(element_codes, str):
            element_codes = element_codes.strip("[]").replace("'", "").split(", ")
        element_codes = [str(e).strip() for e in element_codes if e]
        
        meta_by_code[code] = {
            "lat": float(row.geometry.y),
            "lon": float(row.geometry.x),
            "name": _clean_meta_text(row.get("name")) or code,
            "network": network,
            "state": _clean_meta_text(row.get("state")),
            "elevation": row.get("elevation_m") or row.get("elevation"),
            "variables_daily": _clean_meta_text(row.get("variables_daily")),
            "variables_hourly": _clean_meta_text(row.get("variables_hourly")),
            "station_url": _clean_meta_text(row.get("station_url")),
            "station_image_url": _clean_meta_text(row.get("station_image_url")),
            "last_updated": _clean_meta_text(
                row.get("metadata_fetched_at")
            ),
            "measurement_type": _clean_meta_text(row.get("measurement_type")) or "automated",
        }

    # ── Timestamps ────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    today_ts = pd.Timestamp(now.date())
    today_dowy = day_of_water_year(today_ts)
    current_wy = int(water_year(today_ts))
    embed_wys = list(range(current_wy - N_PAST_WYS, current_wy + 1))

    logger.info(f"Today: {today_ts.date()}, DOWY {today_dowy}, WY{current_wy}")
    logger.info(f"Embedding WYs: {embed_wys}")

    # ── Process all stations (sequentially for stability) ─────────────────────────
    station_codes = sorted(meta_by_code.keys())
    logger.info(f"Processing {len(station_codes)} stations...")

    station_data: dict = {}
    processed = 0
    failed = 0

    for i, code in enumerate(station_codes, 1):
        meta = meta_by_code[code]
        result = process_station_from_zarr(
            ds,
            code,
            meta,
            today_ts,
            today_dowy,
            current_wy,
            embed_wys,
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
            logger.info(f"  {i}/{len(station_codes)} processed "
                        f"({processed} ok, {failed} failed)")

    logger.info(f"Done: {processed} stations, {failed} failed/empty")

    # ── Build map metadata ────────────────────────────────────────────────────
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

    # ── Write HTML ────────────────────────────────────────────────────────────
    logger.info(f"Building HTML for {len(station_data)} stations…")
    html = build_html(map_meta, station_data, now.strftime("%Y-%m-%d %H:%M:%S UTC"))
    output_path.write_text(html, encoding="utf-8")
    size_mb = output_path.stat().st_size / 1e6
    logger.info(f"Written: {output_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
