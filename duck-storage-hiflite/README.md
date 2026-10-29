# Some interesting live DuckDB queries and data destinations

DuckDB's `read_json()` can read straight from an HTTP endpoint, so a metrics server's query API
works like any other table. This lesson pulls the last hour of `demo_metric` from Prometheus and
VictoriaMetrics, combines them, and then sends the result to each of these places:

- querying a live prometheus
- querying a live victoriametrics
- querying both and joining the results
- same, and caching to a local store
- same, and storing in a cloud parquet
- same, and storing in a data lake (local and on S3)
- same, and storing in local process (pandas DataFrame)
- same, and storing in local process as Arrow, ready for GPU processing

Everything targets **DuckDB 2.0+** and **DuckLake 1.1+**.

## Files

| File | Source | Destination |
|------|--------|-------------|
| `prometheus.sql` | Prometheus `query_range` API | printed |
| `victoriametrics.sql` | VictoriaMetrics `export` API | printed |
| `both.sql` | both, unioned | printed |
| `both_local.sql` | both | table `local_data` in the DuckDB database |
| `both_parquet.sql` | both | `s3://dataproducts-marhar-dev/demo.parquet` |
| `both_lake.sql` | both | DuckLake `lake1.ducklake`, Parquet in `lake1_data/` |
| `both_cloudlake.sql` | both | DuckLake `cloudlake1.ducklake`, Parquet in `s3://dataproducts-ducklake-dev/scratch1_data/` |
| `both_df.py` | runs `both.sql` | pandas DataFrame in Python |
| `both_arrow.py` | runs `both.sql` | Arrow table in Python |

## Prerequisites

- A VictoriaMetrics on `localhost:8428` with a `demo_metric` series that has an `env` label.
  `python ../victoria-generator/sine.py` writes one (`demo_metric{env="test"}`).
- Prometheus is optional. VictoriaMetrics serves the Prometheus query API too, so `prom_url`
  points at `:8428` by default. Change it to `http://localhost:9090` to query a real Prometheus.
- For the S3 examples: an AWS SSO profile named `data_products`. Log in with
  `aws sso login --profile data_products` first.
- For the Python examples: `pip install duckdb pandas pyarrow`.

## Running

```bash
duckdb -f prometheus.sql
duckdb -f victoriametrics.sql
duckdb -f both.sql

duckdb local.duckdb -f both_local.sql   # pass a file, or the table disappears on exit
duckdb -f both_parquet.sql
duckdb -f both_lake.sql
duckdb -f both_cloudlake.sql

python both_df.py
python both_arrow.py
```

## Querying Prometheus

`prometheus.sql` calls `/api/v1/query_range`, which takes a PromQL query, a time window and a
step:

```sql
FROM read_json(
    getvariable('prom_url') || '/api/v1/query_range?query=demo_metric'
    || '&start=' || (epoch(now()) - 3600)::BIGINT
    || '&end='   || epoch(now())::BIGINT
    || '&step=15s'
)
```

The response is one JSON document: `data.result` is a list of series, and each series has a
`metric` struct of labels and a `values` list of `[timestamp, "value"]` pairs. Two `unnest`s
flatten it into one row per sample. The value comes back as a string, so it is cast with
`::DOUBLE`. Because of `step=15s`, the samples are evaluated on a 15s grid rather than being
the raw points.

## Querying VictoriaMetrics

`victoriametrics.sql` uses VictoriaMetrics' own `/api/v1/export`, which returns **raw** samples
as newline-delimited JSON, one line per series:

```json
{"metric":{"__name__":"demo_metric","env":"test"},"values":[...],"timestamps":[...]}
```

`read_json` reads NDJSON without any options. `values` and `timestamps` are parallel arrays, so
unnesting both in the same `SELECT` zips them together. The timestamps are in milliseconds, so
they go through `make_timestamp_ms()` (or `to_timestamp(ms / 1000)`, as in `both.sql`).

## Combining both

`both.sql` puts each query in a CTE, gives each the same columns (`source, name, env, ts,
value`), and combines them with `UNION ALL BY NAME`. `BY NAME` matches the columns by name
instead of by position, so the two CTEs don't have to list them in the same order. The `source`
column records where each row came from.

Every `both_*` file below runs this same query and only changes where the result goes.

## Destinations

### Local DuckDB table — `both_local.sql`

`CREATE OR REPLACE TABLE local_data AS (...)` stores the result as a table, and each run
refreshes it. Run it with a database file (`duckdb local.duckdb -f both_local.sql`) so the table
outlives the process, and later queries can read the cache instead of calling the servers again.

### Parquet on S3 — `both_parquet.sql`

```sql
CREATE OR REPLACE SECRET aws_s3 (
    TYPE s3, PROVIDER credential_chain, CHAIN 'sso',
    PROFILE 'data_products', REGION 'us-west-2'
);
COPY (...) TO 's3://dataproducts-marhar-dev/demo.parquet';
```

The secret gets credentials from the AWS SSO login, so no keys appear in the file. `COPY ... TO`
infers Parquet from the `.parquet` extension. The script then reads the file back from S3 to
confirm it was written.

### DuckLake — `both_lake.sql`, `both_cloudlake.sql`

```sql
ATTACH 'ducklake:lake1.ducklake' AS lake1 (DATA_PATH 'lake1_data/');
USE lake1;
CREATE OR REPLACE TABLE lake_data AS (...);
```

DuckLake splits a table in two: the catalog (schemas, snapshots, file lists) lives in a DuckDB
file, and the data is written as Parquet files under `DATA_PATH`. `both_lake.sql` keeps both on
local disk. `both_cloudlake.sql` keeps the catalog local but sets `DATA_PATH` to an S3 prefix,
using the same SSO secret as the Parquet example. Each `CREATE OR REPLACE` adds a new snapshot
instead of overwriting the files in place.

### pandas — `both_df.py`

```python
tbl = duckdb.connect().sql(open("both.sql").read()).to_df()
```

The Python client runs the same `both.sql` and returns the result as a pandas DataFrame in the
same process. Nothing is written to disk.

### Arrow — `both_arrow.py`

Same as above, with `.to_arrow_table()` instead. DuckDB's results are already columnar, so the
conversion is cheap, and the resulting `pyarrow.Table` can go straight to Arrow-native tools such
as Polars or cuDF on the GPU.
