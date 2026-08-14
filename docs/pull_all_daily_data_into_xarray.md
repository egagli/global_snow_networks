Some example code to pull all daily data into xarray format......

```python
import requests, tarfile, numpy as np, pandas as pd, xarray as xr, geopandas as gpd
from pathlib import Path

d = Path("data/snow_pillows"); d.mkdir(parents=True, exist_ok=True)
base = "https://raw.githubusercontent.com/egagli/global_snow_networks/main"
for remote, fp in [("all_snow_stations.geojson", d / "all_snow_stations.geojson"),
                   ("data/all_station_csvs.tar.xz", d / "all_station_csvs.tar.xz")]:
    if not fp.exists():
        fp.write_bytes(requests.get(f"{base}/{remote}").content)
if not (d / "stations").exists():
    tarfile.open(d / "all_station_csvs.tar.xz").extractall(d)

inv = gpd.read_file(d / "all_snow_stations.geojson",
                    columns=['code', 'name', 'network_code', 'client', 'state',
                             'latitude', 'longitude', 'elevation_m']).set_index('code')

frames = {}
for f in sorted((d / "stations").glob("*.csv")):
    if f.stem not in inv.index:          # CSV newer than the inventory refresh
        continue
    df = pd.read_csv(f)
    df['date'] = pd.to_datetime(df['date'], format='%Y-%m-%d', errors='coerce')
    frames[f.stem] = df.dropna(subset=['date']).drop_duplicates('date').set_index('date')

tmin = min(df.index.min() for df in frames.values())
time = pd.date_range(tmin, max(df.index.max() for df in frames.values()), freq='D')
sids = np.array(list(frames))
swe = np.full((len(time), len(sids)), np.nan, dtype='float32')
snd = np.full_like(swe, np.nan)
for j, sid in enumerate(frames):
    pos = (frames[sid].index - tmin).days.values
    swe[pos, j] = frames[sid]['wteq_cm'].values * 10.0   # cm -> mm
    snd[pos, j] = frames[sid]['snwd_cm'].values          # stays cm

meta = inv.reindex(sids)
snow_pillows_ds = xr.Dataset(
    {'swe': (('time', 'station_id'), swe), 'snow_depth': (('time', 'station_id'), snd)},
    coords={'time': time, 'station_id': sids.astype(str),
            'station_name': ('station_id', meta['name'].to_numpy(dtype=str)),
            'network':      ('station_id', meta['network_code'].to_numpy(dtype=str)),
            'client':       ('station_id', meta['client'].to_numpy(dtype=str)),
            'state':        ('station_id', meta['state'].fillna('').to_numpy(dtype=str)),
            'latitude':     ('station_id', meta['latitude'].to_numpy('float64')),
            'longitude':    ('station_id', meta['longitude'].to_numpy('float64')),
            'elevation':    ('station_id', meta['elevation_m'].to_numpy('float32'))},

)

snow_pillows_ds

```
