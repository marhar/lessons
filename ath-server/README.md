# ath-server: a REST front end for VictoriaMetrics, via DuckDB

`server.py` takes a REST call, turns it into a DuckDB query over VictoriaMetrics'
`/api/v1/export` (the same pattern as `../duckmetrics-demo/v2big.sql`), and returns
the result as JSON, CSV, Parquet or Arrow.

```sh
pip install -r requirements.txt     # duckdb>=2.0, pyarrow (only for format=arrow)
python server.py                    # http://127.0.0.1:8000 -> http://localhost:8428
python server.py --port 9000 --vm http://vm:8428 --host 0.0.0.0
```

There is also a C++ port in [`cpp/`](cpp/README.md). Its responses are identical,
and it includes a test that checks this.

## API

The API follows VictoriaMetrics and Prometheus where it can, so their docs and
habits carry over: the same `/api/v1/` prefix, the same repeated `match[]`
series selectors, the same time formats, and the same
`{"status": ..., "data": ...}` JSON envelope.

| Endpoint          | VictoriaMetrics analogue                           | What it returns                                       |
|-------------------|----------------------------------------------------|-------------------------------------------------------|
| `/api/v1/metrics` | `/api/v1/label/__name__/values`                    | metric names                                          |
| `/api/v1/samples` | `/api/v1/query_range` with a plain selector         | every sample, or one per `step` bucket if `step` is set |
| `/api/v1/latest`  | `/api/v1/query` (instant query)                    | the newest sample per series (what `v2big.sql` does)  |

The data endpoints don't reuse the names `query` and `query_range`, because those
take a PromQL/MetricsQL `query=` expression. These take selectors only, and
DuckDB does the computation. Use VictoriaMetrics directly when you need
`rate()`, joins across metrics, and similar.

### Parameters

| Param     | Endpoints        | Default        | Notes |
|-----------|------------------|----------------|-------|
| `match[]` | all; required for samples and latest | | A series selector, repeatable: `demo_metric`, `{job="vmagent"}`, `{__name__=~".*_total"}` |
| `start`   | all              | `end` − 1h     | For `/metrics`, VictoriaMetrics picks the default |
| `end`     | all              | now            | |
| `step`    | samples          | none (raw)     | Bucket width: `30s`, `5m`, `1h30m`, or bare seconds |
| `agg`     | samples, with `step` | `last`     | `last` `first` `avg` `min` `max` `sum` `count` |
| `format`  | all              | `json`         | `json` `csv` `parquet` `arrow` |

### Time parameters

`start` and `end` accept the same forms as Prometheus and VictoriaMetrics:

| Form            | Example                      |
|-----------------|------------------------------|
| unix seconds    | `1793427000`, `1793427000.5` |
| RFC3339         | `2026-10-30T22:00:00Z`; a time with no offset is read as UTC |
| now             | `now`                        |
| relative        | `-1h`, `now-30m`, `-1d12h`   |

Units: `ms s m h d w y`.

The server resolves both times to epoch milliseconds once per request, so
`start=-1h` and `end=now` share the same "now". It then sends absolute times to
VictoriaMetrics and echoes them back in the JSON response, so you can see the
exact window the data came from.

Two defaults differ from raw VictoriaMetrics, and both are deliberate:

- `start` defaults to one hour before `end`. Plain `/export` would read all of
  history.
- `step` is capped at 11,000 points per series, the same cap Prometheus uses.

### Bucketing (`step`)

Buckets are aligned to `start`: `[start, start+step)`, `[start+step, start+2·step)`, …
Each bucket is stamped with its start time. The default `agg=last` matches what
`query_range` returns for a plain selector: the last raw value in each window.
That suits both gauges and counters. Use `avg`/`min`/`max` for gauges.

### Response formats

**JSON** (default) is shaped for programs and charts. In `/samples`, each series
carries parallel `timestamps` and `values` arrays, the same columnar layout
VictoriaMetrics' `/export` uses. This is much smaller than one object per point,
because the name and labels aren't repeated for every sample, and it maps
directly onto charting libraries.

- Timestamps are epoch **milliseconds**, which is what JavaScript's `Date` and
  `/export` use.
- Values are JSON numbers, not the strings Prometheus uses. NaN and Inf
  (VictoriaMetrics staleness markers) become `null`.
- `name` is pulled out of the labels, and `labels` holds the remaining labels.

```json
{"status": "success", "start": 1793426518214, "end": 1793427118214, "step": 60000,
 "data": [{"name": "demo_metric", "labels": {"env": "test"},
           "timestamps": [1793426518214, 1793426578214],
           "values": [32.06, 6.10]}]}
```

`/latest` returns one point per series:
`{"name", "labels", "timestamp", "value"}`.

On an error you get a 4xx/5xx with `{"status": "error", "error": "..."}`.
A selector that VictoriaMetrics rejects comes back as a 400 that carries
VictoriaMetrics' own message.

**CSV, Parquet, Arrow** are a long (tidy) table, one row per point:
`name, labels, ts, value`. This is the shape DuckDB, pandas and polars want.

- In Parquet and Arrow, `labels` is a real `MAP(VARCHAR, VARCHAR)` and `ts` is a
  UTC timestamp.
- In CSV, `labels` is a JSON object string and `ts` is RFC3339
  (`2026-10-31T06:11:49.679Z`).
- Arrow is sent as an IPC stream.

## Example calls

```sh
B=localhost:8000/api/v1
```

**Metric names**

```sh
curl -s "$B/metrics"
curl -s -G "$B/metrics" --data-urlencode 'match[]={job="vmagent"}' -d format=csv
```

**Latest value per series.** The second call is `v2big.sql` as a REST call.

```sh
curl -s -G "$B/latest" -d 'match[]=demo_metric' -d 'match[]=go_goroutines'

curl -s -G "$B/latest" -d format=csv \
  -d 'match[]=demo_metric' -d 'match[]=process_resident_memory_bytes' \
  -d 'match[]=go_goroutines' --data-urlencode 'match[]={__name__=~".*_total"}'
```

Use `--data-urlencode` for any selector that contains `{ } " * ~`.

**Raw samples**

```sh
curl -s -G "$B/samples" -d 'match[]=go_goroutines' -d start=-5m
curl -s -G "$B/samples" --data-urlencode 'match[]=go_goroutines{job="vmagent"}' \
  -d start=2026-10-30T22:00:00Z -d end=2026-10-30T23:00:00Z
```

**Downsampled**

```sh
curl -s -G "$B/samples" -d 'match[]=demo_metric' -d start=-1h -d step=1m -d agg=avg
curl -s -G "$B/samples" -d 'match[]=demo_metric' -d start=-1d -d step=1h -d agg=max -d format=csv
```

**Straight into DuckDB, pandas or polars**

```sh
curl -s -G "$B/samples" -d 'match[]=demo_metric' -d start=-1h -d format=parquet -o demo.parquet
duckdb -c "SELECT labels['env'] AS env, avg(value) FROM 'demo.parquet' GROUP BY env"
```

```python
import pandas as pd, pyarrow as pa, urllib.request
url = 'http://localhost:8000/api/v1/samples?match[]=go_goroutines&start=-1h&step=1m&format=arrow'
df = pa.ipc.open_stream(urllib.request.urlopen(url)).read_all().to_pandas()

df = pd.read_csv('http://localhost:8000/api/v1/latest?match[]=go_goroutines&format=csv',
                 parse_dates=['ts'])
```

DuckDB can also read the CSV endpoint directly:
`SELECT * FROM read_csv('http://localhost:8000/api/v1/latest?match%5B%5D=go_goroutines&format=csv')`.

**Errors**

```sh
curl -s "$B/samples"                                           # 400: need match[]
curl -s -G "$B/samples" --data-urlencode 'match[]={bad'         # 400: VictoriaMetrics' parse error
curl -s -G "$B/samples" -d 'match[]=x' -d start=-30d -d step=1s # 400: too many points
```

## How it works

Each request runs one SQL statement:

1. `read_json(<export url>)` reads `/api/v1/export` with explicit column types.
   `metric` is read as a `MAP`, not an inferred `STRUCT`, because different
   metrics carry different labels.
2. A CTE unnests `timestamps` and `values` into one row per sample.
3. Then, depending on the endpoint:
   - raw samples are passed through;
   - with `step`, rows are grouped by `(name, labels, bucket)` with the chosen
     aggregate;
   - for `/latest`, `QUALIFY row_number() … = 1` keeps the newest row per series,
     as in `v2big.sql`.
4. The output is either `list(... ORDER BY ts)` per series (JSON), or the long
   table written out with DuckDB (CSV, Parquet) or pyarrow (Arrow).

Safety:

- The VictoriaMetrics URL goes into the query as a bound parameter (`$url`), so
  nothing from the request is spliced into the SQL text.
- `agg` is looked up in a whitelist.
- The DuckDB external file cache is turned off, because VictoriaMetrics answers
  are live data.
- The server binds to `127.0.0.1` by default.

DuckDB HTTP settings:

- `force_download = true` makes each read a single `GET`. DuckDB would otherwise
  send a `HEAD` first, and when VictoriaMetrics returns a 5xx to that `HEAD`, its
  error message is lost.
- `http_retries = 0` because DuckDB backs off for 30–70 s on a 503 or 429. It's
  better to answer 502 at once and let the client decide whether to retry.

## Tests

```sh
pip install -r requirements-dev.txt
make test         # builds the C++ server, then runs every test against both servers
make test-py      # just server.py: pytest --impl=py
make test-live    # both servers against a real VictoriaMetrics; they must agree
```

Most tests are black-box HTTP tests in `tests/`, run once per implementation.
The servers point at `tests/fake_vm.py`, a small stand-in for VictoriaMetrics.

**The fake VictoriaMetrics** serves the fixed data in `tests/data.py`: five minutes
of samples whose values are chosen so every expected answer can be worked out by
hand. It copies real VictoriaMetrics behaviour where the servers depend on it:

- whole numbers are written without `.0`;
- staleness markers are `null`;
- a series can be split across several lines;
- a bad selector gets a `text/plain` 400.

It also records each request it receives, so tests can check exactly what was
sent to VictoriaMetrics. Setting `vm.fail = (status, body)` makes every request
fail.

**The test files:**

| File | Covers |
|---|---|
| `test_times.py` | Every time and duration form, through the resolved `start`/`end`/`step` that responses echo; every `bad time` and `bad duration` message |
| `test_samples.py` | Raw samples, every `agg`, bucket alignment, staleness markers, series ordering |
| `test_latest.py` | Newest sample per series, the `v2big.sql` selectors |
| `test_metrics.py` | Metric names, and the exact URLs sent to VictoriaMetrics |
| `test_formats.py` | Content types, exact CSV text, Python-style JSON, Parquet and Arrow schemas and rows |
| `test_errors.py` | Every validation error, VM 400 passed through, VM 5xx giving 502, VM unreachable giving 500 |
| `test_live.py` | `-m live` only: 26 requests sent to both servers against a real VictoriaMetrics. JSON and CSV must match byte for byte; Parquet and Arrow must decode to equal tables. Use `--live-vm URL` to point elsewhere. |

If `cpp/build/ath-server` hasn't been built, the C++ tests are skipped, not failed.
