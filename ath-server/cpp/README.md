# ath-server in C++

This is a C++ port of `../server.py`. It has the same endpoints, parameters, SQL,
JSON and error messages. See [../README.md](../README.md) for the API and example
calls; they work unchanged against this server.

```sh
cmake -B build && cmake --build build
build/ath-server                       # http://127.0.0.1:8000 -> http://localhost:8428
build/ath-server --port 9000 --vm http://vm:8428 --host 0.0.0.0
```

The first time you run `cmake -B build`, it downloads two dependencies:

- the DuckDB 2.0.0 prebuilt library (`libduckdb` and `duckdb.h`), for macOS or Linux,
  on arm64 or amd64;
- [cpp-httplib](https://github.com/yhirose/cpp-httplib) v0.64.0, a single-header
  HTTP server.

You need a C++20 compiler and CMake 3.24 or later.

## How it maps to the Python version

| server.py                     | server.cpp |
|-------------------------------|------------|
| `duckdb` Python package       | DuckDB C API (`duckdb.h`), the stable interface; C++ headers aren't shipped |
| `con.cursor()` per request    | `duckdb_connect()` per request, on one shared database |
| `cur.execute(sql, {...})`     | `duckdb_prepare`, then `duckdb_bind_parameter_index` with the same `$url`/`$start`/`$step` names |
| `rel.write_csv` / `write_parquet` | `COPY (...) TO ... (FORMAT csv / parquet)` with the same options |
| pyarrow for `format=arrow`    | DuckDB's `nanoarrow` community extension: `COPY ... (FORMAT arrows)`. The server installs and loads it at startup. The Arrow schema is the same. |
| `json.dumps`                  | a small writer that copies its output: `", "` and `": "` separators, non-ASCII as `\uXXXX`, floats as Python `repr` (`154.0`, `1e-05`) |
| `parse_qs`, `urlencode`, `float()`, `repr()` | small C++ copies of each, so parameters parse the same way and error messages match exactly |
| `ThreadingHTTPServer`         | cpp-httplib's thread pool, logging in the Python server's format |

## Checking that the two servers match

The test suite in `../tests` runs every test against both servers. See
[Tests](../README.md#tests) in the main README. Run `make test` from `ath-server/`.

## Known differences

- **VictoriaMetrics error text.** Python reads VM's response body from
  `duckdb.HTTPException`, but the C API doesn't expose it. When a VM request fails,
  the C++ server makes the same request again with cpp-httplib to fetch VM's
  message. If the VM URL is `https://` (httplib is built without TLS), it can't
  re-fetch, so it returns DuckDB's own error message instead. Data requests go
  through DuckDB, so `https://` works for them either way.
- **ISO times.** The C++ server handles these forms of `datetime.fromisoformat`:
  - the date as `YYYY-MM-DD` or `YYYYMMDD`;
  - then optionally any one separator and `HH[:MM[:SS[.ffffff]]]`;
  - then optionally `Z` or `±HH[:MM]`.

  It doesn't handle Python's rarer forms: week dates (`2026-W44-5`), ordinal dates,
  or offsets with seconds.
- **Huge times.** A time or duration beyond about ±292 million years doesn't fit in
  an `int64`, so the C++ server rejects it with `bad time` or `bad duration`.
  Python's integers have no limit, so `server.py` accepts it, and it then fails
  later or not at all.
- **HTTP protocol.** The Python server answers in HTTP/1.0 and closes each
  connection. httplib uses HTTP/1.1 with keep-alive, and it also answers `HEAD`
  requests. The `Server` and `Date` headers differ.
