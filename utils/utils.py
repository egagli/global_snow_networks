# -*- coding: utf-8 -*-
"""
utils/utils.py
==============
Water year and day-of-water-year (DOWY) utilities.

A **water year** runs from October 1 of the *previous* calendar year through
September 30 of the named year.  For example:

    WY 2024 = 2023-10-01 → 2024-09-30

The **day of water year (DOWY)** counts from 1 on October 1.  In a non-leap
water year, DOWY 366 does not exist.  In a leap water year (when February of
the named year is in a leap year), DOWY runs from 1 to 366.

All scalar functions also accept numpy arrays, pandas Series / DatetimeIndex,
and xarray DataArrays of datetime64 dtype, making them suitable for
vectorised coordinate assignment.

Examples
--------
    from utils import water_year, day_of_water_year, wy_start, wy_end

    import pandas as pd
    dates = pd.date_range("2023-09-28", "2023-10-05")

    water_year(dates)
    # Int64Index([2023, 2023, 2023, 2024, 2024, 2024, 2024, 2024], dtype='int64')

    day_of_water_year(dates)
    # Int64Index([363, 364, 365, 1, 2, 3, 4, 5], dtype='int64')
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Union

import numpy as np
import pandas as pd

# Type alias for anything date-like that pandas can interpret
DateLike = Union[
    str,
    date,
    pd.Timestamp,
    pd.DatetimeIndex,
    pd.Series,
    np.ndarray,
]


# ── Scalar / vectorised helpers ───────────────────────────────────────────────

def water_year(dates: DateLike) -> int | np.ndarray | pd.Series:
    """
    Return the water year(s) for the given date(s).

    The water year is the calendar year in which a water year ends.
    Dates from October through December belong to the *following* calendar
    year's water year; dates from January through September belong to the
    *current* calendar year's water year.

    Parameters
    ----------
    dates : date-like scalar or array-like
        Any value pandas can interpret as a datetime.

    Returns
    -------
    int or array of int
        Water year integer(s).

    Examples
    --------
    >>> water_year("2023-10-01")
    2024
    >>> water_year("2024-09-30")
    2024
    >>> water_year("2024-10-01")
    2025
    """
    dt = _to_datetime(dates)
    month = _get_attr(dt, "month")
    year  = _get_attr(dt, "year")
    return np.where(month >= 10, year + 1, year)


def day_of_water_year(dates: DateLike) -> int | np.ndarray | pd.Series:
    """
    Return the day of water year (DOWY) for the given date(s).

    DOWY = 1 on October 1, regardless of whether it is a leap year.
    In a non-leap water year, the maximum DOWY is 365.
    In a leap water year (i.e., the named year is a leap year or Feb 29
    falls within the WY), the maximum DOWY is 366.

    Parameters
    ----------
    dates : date-like scalar or array-like

    Returns
    -------
    int or array of int
        Day of water year (1-based).

    Examples
    --------
    >>> day_of_water_year("2023-10-01")
    1
    >>> day_of_water_year("2024-09-30")
    366
    >>> day_of_water_year("2024-01-01")
    93
    """
    dt = _to_datetime(dates)

    # Oct 1 through Dec 31: DOWY is (day_of_year - wy_start_doy + 1)
    # where wy_start_doy = Oct 1 day-of-year in the given year.
    # Jan 1 through Sep 30: DOWY = days since Oct 1 of previous year + 1

    # Simpler to compute via the WY start date
    wy_starts = _wy_start_for_dates(dt)

    if isinstance(dt, (pd.DatetimeIndex, pd.Series)):
        delta = (dt - wy_starts).days + 1
        return delta.astype("int32")
    elif isinstance(dt, np.ndarray):
        wy_starts_arr = _wy_start_for_dates(dt)
        # numpy datetime64 subtraction → timedelta64
        delta = (dt.astype("datetime64[D]") - wy_starts_arr.astype("datetime64[D]")).astype(int) + 1
        return delta.astype(np.int32)
    else:
        # scalar
        return (dt - wy_starts).days + 1


def wy_start(year: int) -> pd.Timestamp:
    """
    Return the first day (Oct 1) of water year ``year``.

    Parameters
    ----------
    year : int
        The water year (e.g., 2024 for WY2024 = 2023-10-01 → 2024-09-30).

    Returns
    -------
    pd.Timestamp

    Examples
    --------
    >>> wy_start(2024)
    Timestamp('2023-10-01 00:00:00')
    """
    return pd.Timestamp(year - 1, 10, 1)


def wy_end(year: int) -> pd.Timestamp:
    """
    Return the last day (Sep 30) of water year ``year``.

    Parameters
    ----------
    year : int

    Returns
    -------
    pd.Timestamp

    Examples
    --------
    >>> wy_end(2024)
    Timestamp('2024-09-30 00:00:00')
    """
    return pd.Timestamp(year, 9, 30)


def wy_date_range(year: int) -> pd.DatetimeIndex:
    """
    Return a daily DatetimeIndex spanning water year ``year``.

    Parameters
    ----------
    year : int

    Returns
    -------
    pd.DatetimeIndex
        365 or 366 daily timestamps from Oct 1 (WY-1) through Sep 30 (WY).

    Examples
    --------
    >>> dr = wy_date_range(2024)
    >>> dr[0]
    Timestamp('2023-10-01 00:00:00')
    >>> dr[-1]
    Timestamp('2024-09-30 00:00:00')
    >>> len(dr)
    366
    """
    return pd.date_range(wy_start(year), wy_end(year), freq="D")


def wy_length(year: int) -> int:
    """
    Return the number of days in water year ``year`` (365 or 366).

    A water year is 366 days when February of ``year`` falls in a leap year
    (i.e., ``calendar.isleap(year)``).

    Parameters
    ----------
    year : int

    Returns
    -------
    int

    Examples
    --------
    >>> wy_length(2024)   # 2024 is a leap year
    366
    >>> wy_length(2023)
    365
    """
    return 366 if calendar.isleap(year) else 365


def add_wy_coords(ds):
    """
    Add ``water_year`` (int32) and ``dowy`` (int16) as coordinates to an
    xarray Dataset or DataArray that has a ``time`` dimension.

    Parameters
    ----------
    ds : xr.Dataset or xr.DataArray
        Must have a ``time`` dimension with datetime64 values.

    Returns
    -------
    xr.Dataset or xr.DataArray
        The input object with ``water_year`` and ``dowy`` coordinates assigned
        on the ``time`` dimension.

    Examples
    --------
    >>> import xarray as xr, pandas as pd
    >>> ds = xr.Dataset(coords={"time": pd.date_range("2023-10-01", periods=5)})
    >>> ds = add_wy_coords(ds)
    >>> ds["water_year"].values
    array([2024, 2024, 2024, 2024, 2024], dtype=int32)
    >>> ds["dowy"].values
    array([1, 2, 3, 4, 5], dtype=int16)
    """
    import xarray as xr

    times = ds["time"].values  # numpy datetime64 array

    # Convert to pandas for vectorised WY/DOWY computation
    pd_times = pd.DatetimeIndex(times)
    wy   = water_year(pd_times).astype(np.int32)
    dowy = day_of_water_year(pd_times).astype(np.int16)

    return ds.assign_coords(
        water_year=("time", wy,   {"long_name": "Water year (Oct–Sep)", "units": "year"}),
        dowy      =("time", dowy, {"long_name": "Day of water year (1 = Oct 1)"}),
    )


# ── Private helpers ───────────────────────────────────────────────────────────

def _to_datetime(dates: DateLike):
    """Coerce input to a pandas or numpy datetime type."""
    if isinstance(dates, (pd.DatetimeIndex, pd.Series)):
        if not pd.api.types.is_datetime64_any_dtype(dates):
            return pd.to_datetime(dates)
        return dates
    if isinstance(dates, np.ndarray):
        return dates.astype("datetime64[D]")
    if isinstance(dates, str):
        return pd.Timestamp(dates)
    if isinstance(dates, date):
        return pd.Timestamp(dates)
    return pd.Timestamp(dates)


def _get_attr(dt, attr: str):
    """Get month/year from scalar or array datetime."""
    if isinstance(dt, (pd.DatetimeIndex,)):
        return getattr(dt, attr)
    if isinstance(dt, pd.Series):
        return getattr(dt.dt, attr)
    if isinstance(dt, np.ndarray):
        # Convert to pandas for attribute access
        return getattr(pd.DatetimeIndex(dt), attr)
    # scalar
    return getattr(dt, attr)


def _wy_start_for_dates(dt):
    """
    Return the Oct 1 start date of the water year for each date in ``dt``.
    Works for pd.Timestamp (scalar), pd.DatetimeIndex, and np.ndarray.
    """
    if isinstance(dt, pd.Timestamp):
        wy = int(water_year(dt))
        return pd.Timestamp(wy - 1, 10, 1)

    if isinstance(dt, (pd.DatetimeIndex, pd.Series)):
        wy = water_year(dt)
        # Oct 1 of (wy - 1)
        return pd.DatetimeIndex(
            [pd.Timestamp(int(y) - 1, 10, 1) for y in wy]
        )

    # numpy array
    pd_dt = pd.DatetimeIndex(dt)
    wy = water_year(pd_dt)
    return np.array(
        [np.datetime64(f"{int(y)-1}-10-01", "D") for y in wy],
        dtype="datetime64[D]",
    )
