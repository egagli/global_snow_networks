# -*- coding: utf-8 -*-
"""
Shared fixtures and configuration for live-API client tests.

All tests in this suite hit real network endpoints.  Run with::

    pixi run test
"""

import pytest


# ── Well-known test stations ──────────────────────────────────────────────────

# AWDB triplets
AWDB_SNOTEL_CO = ["303:CO:SNTL", "457:CO:SNTL"]   # Bear Lake, Grand Mesa
AWDB_SNOTEL_WA = ["335:WA:SNTL"]                    # Fish Lake

# CDEC station IDs
CDEC_PILLOWS = ["QUA", "BLC"]     # Quartz Basin, Blue Canyon
CDEC_COURSE = ["HNT"]             # Huntington Lake snow course

# DataBC location IDs
DATABC_ASWS = ["1A01P", "1E08P"]  # Tupper, Yellowhead
DATABC_MSS = ["1A06A", "1A10"]    # Tupper Creek MSS sites

# NVE station IDs — snow pillows with daily SWE (param 2003) and snow
# depth (param 2002).  NB: 2.11.0 / 12.228.0 are river gauges, not snow.
NVE_SWE_STATIONS = ["12.142.0", "121.2.0"]  # Bakko, Maurhaugen-Oppdal
NVE_SNWD_STATION = ["12.142.0"]             # Bakko — has snow depth too

# Date window for data tests (known-good winter period)
TEST_BEGIN = "2024-01-01"
TEST_END = "2024-01-15"

# Bounding boxes
BBOX_COLORADO = (-109.1, 36.9, -102.0, 41.1)
BBOX_NORTHERN_CA = (-122.5, 38.5, -119.5, 41.5)
BBOX_BC_INTERIOR = (-120.5, 49.5, -119.0, 51.0)
BBOX_NORWAY = (4.5, 57.5, 31.5, 71.5)

# Required record keys for standardized get_data() output
RECORD_KEYS = {"station_id", "date", "variable", "type", "value", "units", "interval"}
