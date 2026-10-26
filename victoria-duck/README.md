# Querying VictoriaMetrics from DuckDB

DuckDB has no VictoriaMetrics connector, and it doesn't need one. VictoriaMetrics (VM) serves
everything over plain HTTP, and DuckDB's `httpfs` extension (autoloaded) can run `read_csv` /
`read_json` directly against a URL. So a VM API endpoint can be queried like a table.

Every example here was run against the local VM stack (v1.154.1 on `localhost:8428`, see
`../victoria-notes`) with **DuckDB 2.0.0** (CLI) and **duckdb 1.5.4** (Python).

## Files in this lesson

| File | Method | What you get |
|------|--------|--------------|
| `export_csv.sql` | `/api/v1/export/csv` → `read_csv` | raw samples, flat columns you choose |
| `export_json.sql` | `/api/v1/export` → `read_json` | raw samples, **all** labels (as a MAP) |
| `query_range.sql` | `/api/v1/query_range` → `read_json` | PromQL/MetricsQL evaluated by VM, on a grid |
| `query_range.py` | Python fetch → pandas → DuckDB | same, with full control over the HTTP call |
| `vm.sql` | table macros `vm_export()` / `vm_query_range()` | the above, reusable and safely URL-encoded |
| `snapshot.sql` | `COPY … TO parquet` | export once, then analyze locally |

```bash
duckdb -f export_csv.sql
duckdb -f export_json.sql
duckdb -f query_range.sql
duckdb -f snapshot.sql                    # writes demo_metric.parquet
duckdb -init vm.sql                       # interactive shell with the macros loaded

python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python query_range.py           # default http://localhost:8428
```

The examples use `demo_metric{env="test"}` (from `../victoria-generator/sine.py`: 4 samples/s,
a 5-minute sine between 0 and 100) on a fixed window, **2026-10-23 17:30–18:30 UTC**, so the
output is reproducible. They also use VM's own metrics over the last few minutes. VM retention
is 1 month, so the fixed window is available until about 2026-11-23.

> If you run the DuckDB CLI from Claude Code or another agent, it switches to "agent mode" and
> changes its output format. Pass `-no-agent` to get the normal tables.

---

## Which method?

```
want raw samples, know the labels you need ........ 1. export/csv
want raw samples, labels vary or unknown .......... 2. export (JSON lines)
want rate()/sum by/rollups computed by VM ......... 3. query_range
need auth headers, retries, or pandas anyway ...... 4. Python
querying the same range repeatedly ................ snapshot to Parquet
```

**Export** returns exactly what was stored, at irregular timestamps. **query_range** returns
values resampled onto `start, start+step, …`. As `../victoria-notes` explains, the step only
*samples* the data: it doesn't summarize it. With export, DuckDB does the aggregation. With
query_range, VM does.

## Settings that apply to every method

```sql
SET force_download = true;   -- required for live endpoints
SET TimeZone = 'UTC';        -- otherwise TIMESTAMPTZ prints in your local zone
```

Without `force_download`, DuckDB first sends a HEAD request to get the size, then a GET. A live
endpoint can grow between the two, and the read fails:

```
HTTP Error: The size reported by HEAD for '…/api/v1/query_range?…' was 315 bytes,
but the full GET downloaded 329 bytes. You can try to resolve this by enabling `SET force_download=true`
```

---

## 1. CSV export (`export_csv.sql`)

`format=` picks the columns: any label name, plus `__name__`, `__value__` and
`__timestamp__:<fmt>`, where `<fmt>` is `rfc3339`, `unix_s`, `unix_ms` or `unix_ns`.

```sql
SELECT *
FROM read_csv('http://localhost:8428/api/v1/export/csv'
              '?format=__name__,env,__timestamp__:rfc3339,__value__'
              '&match[]={__name__="demo_metric",env="test"}'
              '&start=2026-10-23T17:30:00Z&end=2026-10-23T17:30:01Z');
```
```
┌─────────────┬─────────┬────────────────────────────┬───────────┐
│  __name__   │   env   │   __timestamp__:rfc3339    │ __value__ │
│   varchar   │ varchar │  timestamp with time zone  │  double   │
├─────────────┼─────────┼────────────────────────────┼───────────┤
│ demo_metric │ test    │ 2026-10-23 17:30:00.181+00 │     50.19 │
│ demo_metric │ test    │ 2026-10-23 17:30:00.438+00 │    50.459 │
│ demo_metric │ test    │ 2026-10-23 17:30:00.696+00 │     50.73 │
│ demo_metric │ test    │ 2026-10-23 17:30:00.955+00 │      51.0 │
└─────────────┴─────────┴────────────────────────────┴───────────┘
```

VM 1.154 **writes a header row** that repeats the `format` fields, and DuckDB sniffs the types
correctly: `rfc3339` becomes `TIMESTAMPTZ` and `unix_ms` becomes `BIGINT`. To get nicer column
names, add `names = ['name', 'env', 'ts', 'value']`. Don't pass `header = false` with a
`columns =` override: the header line is then read as data, and the read fails on
`Could not convert string "__value__" to 'DOUBLE'`.

With the raw samples in DuckDB, you can do any SQL. Here are per-minute stats:

```sql
SELECT time_bucket(INTERVAL 1 minute, ts) AS minute, count(*) AS n,
       round(min(value), 2) AS min, round(avg(value), 2) AS avg, round(max(value), 2) AS max
FROM read_csv('…&start=2026-10-23T17:30:00Z&end=2026-10-23T17:40:00Z', names = ['name','env','ts','value'])
GROUP BY ALL ORDER BY minute;
```
```
│ 2026-10-23 17:33:00+00   │   234 │    0.0 │   5.54 │   20.6 │
│ 2026-10-23 17:34:00+00   │   132 │   2.53 │  11.72 │  24.14 │   <- sine.py stopped …
│ 2026-10-23 17:35:00+00   │    46 │  92.39 │  95.18 │  97.52 │   <- … and restarted
│ 2026-10-23 17:36:00+00   │   233 │  79.48 │  94.48 │  100.0 │
```

A query_range with `step=1m` would show one value per minute here and **hide the gap**. The
`count` column exposes it.

## 2. JSON-lines export (`export_json.sql`)

Each line is one series: `{"metric":{…},"values":[…],"timestamps":[…ms]}`. Two `unnest`s in
the same SELECT are zipped together element by element:

```sql
SELECT metric['__name__']                          AS name,
       metric                                      AS labels,
       make_timestamptz(unnest(timestamps) * 1000) AS ts,      -- ms -> µs
       unnest("values")                            AS value
FROM read_json('http://localhost:8428/api/v1/export?match[]=demo_metric'
               '&start=2026-10-23T17:30:00Z&end=2026-10-23T17:30:01Z',
               format  = 'newline_delimited',
               columns = {metric: 'MAP(VARCHAR, VARCHAR)', "values": 'DOUBLE[]', timestamps: 'BIGINT[]'});
```
```
│ demo_metric │ {__name__=demo_metric, env=test} │ 2026-10-23 17:30:00.181+00 │  50.19 │
```

**Declare `columns`.** If you let DuckDB infer them, you get:

```
metric     struct(__name__ varchar, job varchar, instance varchar)
values     bigint[]
```

`metric` becomes a STRUCT of whichever labels the sample happened to contain, which breaks once
series have different label sets. `values` becomes `BIGINT[]` if the first values look like
integers. A `MAP` handles any label set: `metric['job']` returns NULL when the label is missing.

```sql
-- several series, different labels
… '?match[]={__name__="process_resident_memory_bytes",job=~"v.%2A"}&start=-5m' …
```
```
┌─────────────────┬─────────┬─────────────┐
│       job       │ samples │ max_rss_mib │
├─────────────────┼─────────┼─────────────┤
│ victoriametrics │      30 │       421.8 │
│ vmagent         │      29 │        72.3 │
│ vmalert         │      29 │        43.4 │
└─────────────────┴─────────┴─────────────┘
```

> **Gotcha: `*` in the URL.** DuckDB treats it as a file glob and refuses:
> `Globs (*) for generic HTTP file is are not supported`. Write it as `%2A`, or
> `SET allow_asterisks_in_http_paths = true`, or use the macros, which encode everything.
> Other characters such as `[ ] { } "` and spaces worked unencoded.

## 3. PromQL via `query_range` (`query_range.sql`)

The response is nested (`data.result[].values[[seconds, "string"]]`), so you unnest twice and
cast:

```sql
WITH series AS (
  SELECT unnest(data.result) AS s
  FROM read_json('http://localhost:8428/api/v1/query_range?query='
                 || url_encode('sum(rate(vm_http_requests_total[5m])) by (job)')
                 || '&start=-10m&step=5m')
), points AS (
  SELECT s.metric AS metric, unnest(s."values") AS p FROM series
)
SELECT metric.job AS job, to_timestamp(p[1]::DOUBLE) AS ts, round(p[2]::DOUBLE, 3) AS req_per_s
FROM points ORDER BY job, ts;
```
```
┌─────────────────┬────────────────────────────┬───────────┐
│       job       │             ts             │ req_per_s │
├─────────────────┼────────────────────────────┼───────────┤
│ victoriametrics │ 2026-10-26 05:00:58.386+00 │     2.136 │
│ victoriametrics │ 2026-10-26 05:05:58.386+00 │     4.673 │
│ vmagent         │ 2026-10-26 05:00:58.386+00 │       0.1 │
…
```

- **Use `url_encode()` on the query.** In a query string, `+` means a space, so `a + b` would
  reach VM as `a   b`. `*`, `&` and `#` break things too.
- Values are strings (`"2.136"`), so `NaN` and `+Inf` survive JSON. Cast them with `::DOUBLE`.
- Timestamps are **seconds** (fractional) here, but **milliseconds** in export.
- An **empty result** (no matching series) breaks this simple version:
  `UNNEST() can only be applied to lists, structs and NULL, not JSON`, because there was nothing
  to infer the type from. The `vm_query_range` macro fixes this with `json_transform`.

## 4. From Python (`query_range.py`)

Fetch the data with any HTTP client, build a DataFrame, and DuckDB queries it **by variable
name**:

```python
rps = query_range("sum(rate(vm_http_requests_total[5m])) by (job)", START, END, step="1m")
mem = query_range("max_over_time(process_resident_memory_bytes[5m])", START, END, step="5m")
duckdb.sql("SELECT m.job, m.ts, m.value / 2^20 AS rss_mib, r.value AS req_per_s "
           "FROM mem m JOIN rps r USING (job, ts)")
```
```
┌─────────────────┬──────────────────────────┬─────────┬───────────┐
│       job       │            ts            │ rss_mib │ req_per_s │
├─────────────────┼──────────────────────────┼─────────┼───────────┤
│ victoriametrics │ 2026-10-25 22:10:00-07   │   421.8 │      6.05 │
│ victoriametrics │ 2026-10-25 22:05:00-07   │   426.0 │      3.92 │
…
```

> **Gotcha: joining two query_range results.** The first version used `start="-30m"` in both
> calls and the join returned **0 rows**. Each request resolves "now" separately, so the two
> grids were a few milliseconds apart. Use one fixed, aligned `START`/`END` (the script rounds
> to the minute) and steps that divide evenly into each other.

Use this route when you need auth headers, retries, or to page through long ranges (VM limits
points per series per query). It's also the natural choice when the data ends up in pandas
anyway.

## 5. Reusable macros (`vm.sql`)

```sql
.read vm.sql          -- or: duckdb -init vm.sql

FROM vm_export('{__name__="up",job=~"v.*"}', '-5m', 'now');            -- name, labels, ts, value
FROM vm_query_range('sum(rate(x[5m])) by (job) * 60 + 0', '-10m', 'now', '5m');   -- labels, ts, value
```

Both macros `url_encode()` every argument, so `*`, `+` and quotes are safe. Both return
`labels` as `MAP(VARCHAR, VARCHAR)`. Both return 0 rows, not an error, when nothing matches.
The base URL lives in `SET VARIABLE vm = 'http://localhost:8428'`.

## 6. Snapshot to Parquet (`snapshot.sql`)

Every `read_json`/`read_csv` above goes back to VM over HTTP. If you query the same range more
than once, materialize it first:

```sql
COPY (FROM vm_export('demo_metric', '2026-10-23T17:30:00Z', '2026-10-23T18:30:00Z'))
TO 'demo_metric.parquet' (FORMAT parquet);           -- 12,583 samples -> 136 KiB

-- find gaps in the raw data (expected spacing ~0.25 s)
SELECT ts - gap AS gap_start, ts AS gap_end, gap
FROM (SELECT ts, ts - lag(ts) OVER (ORDER BY ts) AS gap FROM 'demo_metric.parquet')
WHERE gap > INTERVAL 2 seconds ORDER BY gap DESC;
```
```
│ 2026-10-23 18:16:31.063+00 │ 2026-10-23 18:21:15.816+00 │ 00:04:44.753 │
│ 2026-10-23 17:34:34.049+00 │ 2026-10-23 17:35:48.31+00  │ 00:01:14.261 │
```

This kind of question is awkward in PromQL and easy in SQL. It's the main reason to bring VM
data into DuckDB.

---

## Gotchas recap

1. **`SET force_download = true`.** Live endpoints change size between HEAD and GET.
2. **`*` is a glob to DuckDB.** Use `%2A`, `allow_asterisks_in_http_paths`, or `url_encode()`.
3. **`+` is a space in query strings.** Always `url_encode()` PromQL that contains arithmetic.
4. **Declare the JSON export `columns`.** Use a MAP for labels and `DOUBLE[]` for values.
5. **CSV export has a header row** in current VM. Let DuckDB sniff it, or rename with `names =`.
6. **Units differ:** export uses ms (`make_timestamptz(ms * 1000)`), and query uses seconds
   (`to_timestamp(s)`).
7. **query_range samples onto a grid** and hides gaps. Export plus SQL shows them.
8. **Empty query_range results** have no type to infer. Use `json_transform` (see `vm.sql`).
9. **Data gaps on this box are real.** While writing this lesson, the self-scrape metrics only
   went back about 12 minutes in a 30-minute window. VM was missing those samples (even `up`
   was absent), so DuckDB wasn't the cause.
