"""Hello-world Zarr writer.

Builds `weather.zarr`: a small synthetic climate dataset with a realistic layout
(groups, coordinate arrays, a chunked + compressed 3-D variable, attributes).

    python write_zarr.py
"""

import numpy as np
import zarr
from zarr.codecs import BloscCodec

STORE = "weather.zarr"

# --- 1. Make some plausible data -------------------------------------------
n_time, n_lat, n_lon = 365, 90, 180  # one year of daily data on a 2-degree grid
time = np.arange(n_time, dtype="int32")  # days since 2025-01-01
lat = np.linspace(-89, 89, n_lat, dtype="float32")
lon = np.linspace(-179, 179, n_lon, dtype="float32")

t, la, lo = np.meshgrid(time, lat, lon, indexing="ij")
rng = np.random.default_rng(42)
temperature = (
    -15
    + 42 * np.cos(np.deg2rad(la))                                # warm equator, cold poles
    - 12 * np.cos(2 * np.pi * t / 365) * np.sin(np.deg2rad(la))  # seasons, flipped by hemisphere
    + 3 * np.sin(np.deg2rad(lo) * 2)                             # a little longitudinal texture
    + rng.normal(0, 1.5, size=t.shape)                           # weather noise
).round(1).astype("float32")                                     # 0.1 degC precision compresses better
wet = rng.random(t.shape) < 0.35                                 # it rains ~35% of days
precip = np.where(wet, rng.gamma(0.8, 8.0, t.shape), 0).round(1).astype("float32")

# --- 2. Create the hierarchy -----------------------------------------------
root = zarr.open_group(STORE, mode="w", zarr_format=3)
root.attrs.update(
    title="Hello Zarr: synthetic daily weather",
    source="write_zarr.py",
    conventions="loosely CF-like",
)

# Coordinates live at the root as small 1-D arrays (one chunk each).
for name, values, units in [
    ("time", time, "days since 2025-01-01"),
    ("lat", lat, "degrees_north"),
    ("lon", lon, "degrees_east"),
]:
    arr = root.create_array(name, shape=values.shape, dtype=values.dtype, dimension_names=[name])
    arr[:] = values
    arr.attrs["units"] = units

# Data variables go in a sub-group, just to show nesting.
surface = root.create_group("surface")
compressor = BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle")

tas = surface.create_array(
    "temperature",
    shape=temperature.shape,
    chunks=(30, 45, 90),          # ~1 month x quarter of the globe per chunk
    dtype="float32",
    compressors=compressor,
    fill_value=np.nan,
    dimension_names=["time", "lat", "lon"],
)
tas[:] = temperature
tas.attrs.update(units="degC", long_name="near-surface air temperature")

pr = surface.create_array(
    "precipitation",
    shape=precip.shape,
    chunks=(30, 45, 90),
    dtype="float32",
    compressors=compressor,
    fill_value=0.0,
    dimension_names=["time", "lat", "lon"],
)
pr[:] = precip
pr.attrs.update(units="mm/day", long_name="daily precipitation")

print(root.tree())
print(f"\nWrote {STORE}")
print(tas.info)
