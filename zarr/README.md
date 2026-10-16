# Zarr in 10 minutes

Zarr is a format for storing **large N-dimensional arrays** as a tree of small, independently
compressed **chunks** plus a little **JSON metadata**. Think "NumPy arrays that live on disk or in
S3, and that you can read a slice of without touching the rest."

It's popular in climate/earth science (xarray), bio-imaging (OME-Zarr), genomics, and ML — anywhere
the data is too big for memory and you want parallel or cloud-native access.

## Files in this lesson

| File | What it is |
|------|------------|
| `write_zarr.py` | Hello-world writer: builds `weather.zarr` |
| `read_zarr.py` | Hello-world reader: explores and slices it |
| `weather.zarr/` | The example store: one year of synthetic daily global weather |

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python write_zarr.py     # (re)creates weather.zarr
.venv/bin/python read_zarr.py
```

Uses **zarr-python 3.x** and the **Zarr v3** format.

---

## 1. The core ideas

**Array** — has a shape, a dtype, a chunk shape, a codec pipeline (compression), a fill value and
optional attributes.

**Chunk** — the array is cut into a regular grid of chunks. Each chunk is one object/file, compressed
on its own. Reading `arr[196]` only fetches and decompresses the chunks that slice overlaps.

**Group** — a folder-like container of arrays and other groups. Gives you a hierarchy, like HDF5.

**Attributes** — free-form JSON on any array or group (units, descriptions, provenance…).

**Store** — where the bytes live: a local directory, a zip file, S3/GCS/Azure (via fsspec), or memory.
The format is just keys → bytes, so any key/value store works.

## 2. What it looks like on disk

```
weather.zarr/
├── zarr.json                      ← root group metadata + attributes
├── lat/
│   ├── zarr.json                  ← array metadata
│   └── c/0                        ← the single chunk
├── lon/ …
├── time/ …
└── surface/
    ├── zarr.json                  ← sub-group
    ├── temperature/
    │   ├── zarr.json
    │   └── c/<t>/<lat>/<lon>      ← chunk (0,1,0) is c/0/1/0
    └── precipitation/ …
```

The array metadata is human-readable (`cat weather.zarr/surface/temperature/zarr.json`):

```json
{
  "zarr_format": 3,
  "node_type": "array",
  "shape": [365, 90, 180],
  "data_type": "float32",
  "chunk_grid": {"name": "regular", "configuration": {"chunk_shape": [30, 45, 90]}},
  "chunk_key_encoding": {"name": "default", "configuration": {"separator": "/"}},
  "fill_value": "NaN",
  "codecs": [
    {"name": "bytes", "configuration": {"endian": "little"}},
    {"name": "blosc", "configuration": {"cname": "zstd", "clevel": 5, "shuffle": "bitshuffle", ...}}
  ],
  "dimension_names": ["time", "lat", "lon"],
  "attributes": {"units": "degC", "long_name": "near-surface air temperature"}
}
```

That's the whole trick: a JSON header plus a grid of compressed blobs. No central index file, so
many writers can write different chunks in parallel with no coordination.

## 3. Writing (see `write_zarr.py`)

```python
import zarr
from zarr.codecs import BloscCodec

root = zarr.open_group("weather.zarr", mode="w", zarr_format=3)
root.attrs["title"] = "Hello Zarr"

surface = root.create_group("surface")
tas = surface.create_array(
    "temperature",
    shape=(365, 90, 180),
    chunks=(30, 45, 90),
    dtype="float32",
    compressors=BloscCodec(cname="zstd", clevel=5, shuffle="bitshuffle"),
    fill_value=float("nan"),
    dimension_names=["time", "lat", "lon"],
)
tas[:] = data                 # or write piece by piece: tas[0:30] = january
tas.attrs["units"] = "degC"
```

## 4. Reading (see `read_zarr.py`)

```python
root = zarr.open_group("weather.zarr", mode="r")
print(root.tree())

tas = root["surface/temperature"]   # metadata only — nothing loaded yet
day = tas[196]                      # -> numpy array (90, 180); reads 4 chunks
ts  = tas[:, 70, 90]                # -> full time series at one point; reads 13 chunks
```

Indexing returns plain NumPy arrays. Untouched chunks are never read.

## 5. Choosing chunks (the one decision that matters)

- **Match your access pattern.** Chunks of `(30, 45, 90)` balance "map of one day" and "time series at
  one point". If you only ever read time series, chunk long in time and small in space, e.g. `(365, 10, 10)`.
- **Aim for ~1–100 MB uncompressed per chunk** for cloud storage (fewer, bigger requests). Locally,
  smaller is fine. Here each chunk is 30×45×90×4 B ≈ 0.5 MB — small, to keep the example readable.
- **Too many tiny chunks** = millions of files. Zarr v3 **sharding** packs many chunks into one
  object: `create_array(..., chunks=(30,45,90), shards=(360,90,180))`.
- **Missing chunks are free.** A chunk that was never written reads back as `fill_value` — great for
  sparse data.

## 6. Compression tips

- `shuffle`/`bitshuffle` reorders bytes so similar ones sit together — big win for numeric data.
- Round to the precision you actually have. The writer rounds temperature to 0.1 °C; noise in the
  low bits is incompressible. (`weather.zarr` is ~16 MB on disk vs ~47 MB raw.)
- `zstd` is a good default; `lz4` is faster, `gzip` is the most portable.

## 7. v2 vs v3

| | Zarr v2 | Zarr v3 |
|---|---|---|
| Metadata | `.zarray`, `.zgroup`, `.zattrs` | one `zarr.json` per node |
| Chunk keys | `0.1.0` | `c/0/1/0` |
| Compression | single `compressor` + `filters` | ordered `codecs` pipeline |
| Extras | — | sharding, `dimension_names`, extension points |

zarr-python 3 reads both; pass `zarr_format=2` to write v2 for older tools.

## 8. Where to go next

- **xarray**: `xr.open_zarr("weather.zarr", group="surface")` gives labelled dims and lazy Dask arrays.
- **Cloud**: `zarr.open_group("s3://bucket/weather.zarr", mode="r", storage_options={"anon": True})`
  (needs `fsspec` + `s3fs`).
- **Parallel writes**: each worker writes its own non-overlapping chunk-aligned region; no locks needed.
- Spec: https://zarr-specs.readthedocs.io · Docs: https://zarr.readthedocs.io
