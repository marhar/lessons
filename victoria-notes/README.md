# Reading VictoriaMetrics data in 10 minutes

VictoriaMetrics (VM) is a **time-series database** that speaks the Prometheus APIs. You read it
with plain HTTP GETs, using **PromQL**, or VM's superset of it, **MetricsQL**. There's no client
library to learn: anything that can do `curl` can query it.

Every example below was run against the VM stack on this machine, so you can paste them and get
the same kind of answers back.

## Files in this lesson

| File | What it is |
|------|------------|
| `read_vm.py` | Hello-world reader (stdlib only): discovery, instant query, range query, export |
| `../victoria-generator/sine.py` | Writes `demo_metric`, a sine wave (period 5 min, range 0..100) |

```bash
python3 read_vm.py                         # default http://localhost:8428
python3 ../victoria-generator/sine.py      # (optional) push fresh demo data; Ctrl-C to stop
```

---

## 0. What's running on this box

The stack is the upstream docker-compose from `~/g/VictoriaMetrics/deployment/docker`:

| Container | Port | Role |
|-----------|------|------|
| `victoriametrics` v1.154.1 | **8428** | the database: storage + query API + built-in UI at `/vmui` |
| `vmagent` | 8429 | scrapes targets every 10s, remote-writes to VM |
| `vmalert` | 8880 | evaluates alert rules by querying VM |
| `alertmanager` | 9093 | routes alerts |
| `grafana` | 3000 | dashboards (VM is a Prometheus datasource) |

vmagent scrapes the three VM components themselves (`job` = `victoriametrics`, `vmagent`,
`vmalert`), so most of the data here is VM monitoring itself: ~3,200 series and ~320 rows/s.
On top of that there's `demo_metric{env="test"}` from `sine.py`. Retention is the default
**1 month** (`-retentionPeriod=1M`).

```bash
$ curl -s localhost:8428/health
OK
```

## 1. The data model

A **series** is a metric name plus a set of labels. Under the hood the name is just one more label, `__name__`:

```
demo_metric{env="test"}
process_resident_memory_bytes{job="vmagent", instance="vmagent:8429"}
```

Each series holds **samples**: an int64 timestamp in **milliseconds** plus a float64 value. That's
the whole model. There are no tables, no schema, and no types beyond float64. "Counter",
"gauge" and "histogram" are naming conventions (`_total`, `_bucket`), not storage types.

## 2. Discovery: what's in there?

These endpoints only read the index, never the samples, so they're cheap:

```bash
B=localhost:8428

curl -s $B/api/v1/labels                       # all label names
curl -s $B/api/v1/label/__name__/values        # all metric names (~450 here)
curl -s $B/api/v1/label/job/values
# {"status":"success","data":["victoriametrics","vmagent","vmalert"]}

curl -s $B/api/v1/series -d 'match[]=demo_metric' -d start=-30d
# {"status":"success","data":[{"__name__":"demo_metric","env":"test"}]}

curl -s "$B/api/v1/status/tsdb?topN=5"          # cardinality: series count per metric/label
# "seriesCountByMetricName": [{"name":"flag","value":717}, {"name":"vm_http_requests_total","value":103}, ...]
```

> **Gotcha: discovery defaults to the last day.** If `sine.py` hasn't run in the last 24h,
> `curl $B/api/v1/label/env/values` returns `[]`, even though the data is still stored. Add
> `-d start=-30d` and you get `["test"]`.

> **Gotcha: curl and `[ ]`/`{ }`.** curl treats brackets and braces as glob patterns, so
> `curl "$B/api/v1/query?query=rate(x[5m])"` silently sends the wrong request. Always use
> `--data-urlencode 'query=...'` (or `curl -g`). The examples below do this.

Times can be given as unix seconds (`1792776600`), RFC3339 (`2026-10-23T17:30:00Z`), or relative
to now (`-1h`, `-30d`).

## 3. Instant queries: `/api/v1/query`

This asks: "what is the value of this expression **at time T**?" (the default is now). You get
one sample per series, a **vector**:

```bash
q() { curl -s $B/api/v1/query --data-urlencode "query=$1" "${@:2}"; echo; }

q process_resident_memory_bytes
# result: [
#   {"metric":{"__name__":"process_resident_memory_bytes","instance":"victoriametrics:8428","job":"victoriametrics"},
#    "value":[1792990965,"428089344"]},
#   {"metric":{... "job":"vmagent"},  "value":[1792990965,"76443648"]},
#   {"metric":{... "job":"vmalert"},  "value":[1792990965,"43917312"]} ]
```

`value` is `[unix_seconds, "string"]`. The value is a string so that `NaN` and `+Inf` survive
JSON.

To get an answer, VM takes the **most recent sample at or before T**, looking back only a limited
distance. That has two consequences:

```bash
q demo_metric                                  # sine.py running: e.g. "value":[1792991071,"2.774"]
                                               # sine.py stopped a while ago:
# "result":[]                                  # sample too old, so the series is "stale"

q demo_metric -d time=2026-10-23T17:30:00Z
# "value":[1792776600,"49.92"]
```

The raw data has `50.19` at `17:30:00.181`. The query returned `49.92`, which is the sample just
**before** 17:30:00. Queries never look forward in time.

## 4. Range queries: `/api/v1/query_range`

Range queries evaluate the expression at every `start, start+step, …, end` and return one list of
points per series, a **matrix**. This is what Grafana calls for every panel.

```bash
curl -s $B/api/v1/query_range --data-urlencode 'query=demo_metric' \
     -d start=2026-10-23T17:30:00Z -d end=2026-10-23T17:40:00Z -d step=1m
# "values":[[1792776600,"49.92"],[1792776660,"97.517"],[1792776720,"79.463"],[1792776780,"20.817"],
#           [1792776840,"2.443"],[1792776900,"24.143"],[1792776960,"97.522"],...]
```

**The step samples the data; it doesn't summarize it.** `sine.py` writes 4 points per second, and
this query returns 1 point per minute. Because the 1-minute step doesn't line up with the
5-minute wave, you see a jumpy pattern that repeats every 5 points (`97.5, 79.5, 20.8, 2.4,
24.1`). That pattern is an artifact of the sampling, not the actual shape of the data. To
summarize each step, use a **rollup function** over a window `[d]`:

```bash
curl -s $B/api/v1/query_range --data-urlencode 'query=max_over_time(demo_metric[5m])' \
     -d start=2026-10-23T17:30:00Z -d end=2026-10-23T17:40:00Z -d step=5m
# "values":[[1792776600,"99.061"],[1792776900,"100"],[1792777200,"100"]]

q 'count_over_time(demo_metric[1m])' -d time=2026-10-23T17:30:00Z
# "value":[1792776600,"233"]                   # about 4/s, as expected
```

Useful rollups: `avg_/min_/max_/sum_/count_over_time`, `quantile_over_time(0.9, x[d])`,
`last_over_time`, and for counters `rate`/`increase`.

## 5. Export: get the raw samples

Queries always resample onto a grid. To get **exactly what was stored**, use the export endpoints.
They take `match[]` series selectors, not expressions:

```bash
# JSON lines: one line per series, with parallel arrays of values and timestamps (ms)
curl -s $B/api/v1/export -d 'match[]=demo_metric' \
     -d start=2026-10-23T17:30:00Z -d end=2026-10-23T17:30:01Z
# {"metric":{"__name__":"demo_metric","env":"test"},"values":[50.19,50.459,50.73,51],
#  "timestamps":[1792776600181,1792776600438,1792776600696,1792776600955]}

# CSV: you choose the columns
curl -s $B/api/v1/export/csv -d 'format=__name__,env,__timestamp__:rfc3339,__value__' \
     -d 'match[]=demo_metric' -d start=2026-10-23T17:30:00Z -d end=2026-10-23T17:30:01Z
# demo_metric,test,2026-10-23T17:30:00.181Z,50.19
# demo_metric,test,2026-10-23T17:30:00.438Z,50.459
# ...

# Native: VM's compact binary format, for moving data between VM instances (/api/v1/import/native)
curl -s $B/api/v1/export/native -d 'match[]=demo_metric' > demo.bin
```

Use export for analysis in pandas/DuckDB, backfills, and migrations. Use queries for dashboards and
alerts. Export streams its output, so very large dumps work, but narrow them with `match[]` and
`start`/`end`.

## 6. MetricsQL on this box's own metrics

```bash
# Is everything being scraped?
q up                                           # 1 = last scrape succeeded, per instance

# Counters only go up, so take their rate. Busiest HTTP paths on VM, in req/s:
q 'topk(3, sum(rate(vm_http_requests_total[5m])) by (path))'
# /api/v1/query 1.51 · /api/v1/write 0.47 · /metrics 0.30

# Ingestion rate, by protocol: vmagent's remote write, plus sine.py's text import when it's running
q 'sum(rate(vm_rows_inserted_total[5m])) by (type) > 0'
# {"type":"promremotewrite"} 321.38 · {"type":"prometheus"} 0.59

# p99 latency from a histogram. VM histograms use a `vmrange` label instead of `le`:
q 'histogram_quantile(0.99, sum(rate(vm_http_request_duration_seconds_bucket[5m])) by (vmrange, path))'

# Config is exported as metrics too: every command-line flag appears as a label set
q 'flag{name="retentionPeriod"}'
# {"name":"retentionPeriod","value":"1M","is_set":"false",...}

q vm_app_version                               # short_version="v1.154.1" for all three components

# Alerts that vmalert has written back into VM
q ALERTS
```

Some MetricsQL extras that standard PromQL doesn't have:

| Function | Example | Meaning |
|---|---|---|
| `tfirst_over_time` / `tlast_over_time` | `tlast_over_time(demo_metric[30d])` | timestamp of first/last sample (`1792789968` = Oct 23 21:12 UTC) |
| `range_*` | `range_avg(demo_metric)` | aggregate across the whole result range |
| `keep_metric_names` | `rate(x[5m]) keep_metric_names` | don't drop `__name__` after a function |
| `WITH` | `WITH (s = demo_metric{env="test"}) max_over_time(s[1h])` | named sub-expressions |
| implicit `[d]` | `rate(vm_rows_inserted_total)` | window defaults to the step |

## 7. From Python (see `read_vm.py`)

```python
def api(path, **params):
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    body = json.load(urllib.request.urlopen(url))
    return body["data"]

api("/api/v1/query", query="process_resident_memory_bytes")["result"]
api("/api/v1/query_range", query="demo_metric", start=t0, end=t1, step="1m")["result"]
```

Sample output:

```
457 metric names, e.g. ['ALERTS', 'ALERTS_FOR_STATE', 'demo_metric', 'flag', 'go_cgo_calls_count']
jobs: ['victoriametrics', 'vmagent', 'vmalert']
Resident memory per component:
  victoriametrics    425.5 MiB
  vmagent             73.0 MiB
  vmalert             42.0 MiB
Ingest rate: 320 rows/s
demo_metric spans 2026-10-22 05:37:15 .. 2026-10-23 21:12:48 UTC
export: {'__name__': 'demo_metric', 'env': 'test'} -> 1956 raw samples, min 0.00 max 100.00 mean 57.91
```

For dataframes, put the export JSON lines into pandas: `pd.DataFrame({"ts": pd.to_datetime(s["timestamps"], unit="ms"), "v": s["values"]})`.

## 8. Clicking instead of typing

- **vmui** at http://localhost:8428/vmui has a query editor with graph and table views, a cardinality
  explorer (the same data as `/api/v1/status/tsdb`), and a "query analyzer" that explains a query.
- **Grafana** at http://localhost:3000 uses VM as a Prometheus datasource (`http://victoriametrics:8428`).

## 9. Gotchas recap

1. **URL-encode queries.** Otherwise `[5m]` and `{env="test"}` get mangled by curl or the shell.
2. **"No data" may just mean stale.** Instant queries look back only a short distance. Use
   `last_over_time(x[1d])` or set `time=`.
3. **The step samples; it doesn't average.** Use `*_over_time` for honest downsampling.
4. **Use `rate()` on counters**, never their raw values, because counters reset when a process restarts.
5. **Discovery endpoints default to the last 24h.** Pass `start=`.
6. **Export returns raw data and query returns resampled data.** Pick the one that matches what you need.
7. Timestamps are **seconds** in query responses and **milliseconds** in export output.

Docs: https://docs.victoriametrics.com/victoriametrics/url-examples/ ·
MetricsQL: https://docs.victoriametrics.com/victoriametrics/metricsql/
