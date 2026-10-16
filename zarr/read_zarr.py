"""Hello-world Zarr reader.

    python read_zarr.py [path]      # default: weather.zarr
"""

import sys

import numpy as np
import zarr

path = sys.argv[1] if len(sys.argv) > 1 else "weather.zarr"
root = zarr.open_group(path, mode="r")

# --- 1. Discover what's inside (only metadata is read here) -----------------
print("Root attributes:", dict(root.attrs))
print(root.tree())

def walk(group, prefix=""):
    for name, node in group.members():
        if isinstance(node, zarr.Group):
            walk(node, f"{prefix}{name}/")
        else:
            print(f"  {prefix}{name:<14} shape={node.shape} chunks={node.chunks} "
                  f"dtype={node.dtype} dims={node.metadata.dimension_names} attrs={dict(node.attrs)}")

print("Arrays:")
walk(root)

# --- 2. Read data lazily: slicing only fetches the chunks it touches --------
lat, lon = root["lat"][:], root["lon"][:]
tas = root["surface/temperature"]           # no data loaded yet

day = 196                                   # mid-July
snapshot = tas[day]                         # one (lat, lon) slice -> touches 4 of 52 chunks
print(f"\nGlobal mean temperature on day {day}: {snapshot.mean():.2f} °C")

# Nearest grid point to London (51.5N, 0.1W) -> full time series from 1 column of chunks
i, j = np.abs(lat - 51.5).argmin(), np.abs(lon - -0.1).argmin()
series = tas[:, i, j]
print(f"London-ish ({lat[i]:.0f}N, {lon[j]:.0f}E): "
      f"min {series.min():.1f}, max {series.max():.1f}, mean {series.mean():.1f} °C")

# Total rainfall over the year for the tropics band
pr = root["surface/precipitation"]
tropics = (lat > -23.5) & (lat < 23.5)
print(f"Mean annual tropical rainfall: {pr[:, tropics, :].sum(axis=0).mean():.0f} mm")
