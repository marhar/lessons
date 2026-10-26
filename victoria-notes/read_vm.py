"""Hello-world VictoriaMetrics reader (stdlib only).

    python read_vm.py [base_url]      # default: http://localhost:8428
"""

import json
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8428"


def get(path, **params):
    """GET an API path; params are URL-encoded (so PromQL [..] and {..} are safe)."""
    url = f"{BASE}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode()


def api(path, **params):
    body = json.loads(get(path, **params))
    if body["status"] != "success":
        raise RuntimeError(body)
    return body["data"]


def iso(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --- 1. Discover what's inside (index lookups only, no samples read) --------
names = api("/api/v1/label/__name__/values", start="-30d")
print(f"{len(names)} metric names, e.g. {names[:5]}")
print("jobs:", api("/api/v1/label/job/values"))

tsdb = api("/api/v1/status/tsdb", topN=5)
print(f"total series: {tsdb['totalSeries']}; biggest metrics:")
for row in tsdb["seriesCountByMetricName"]:
    print(f"  {row['name']:<45} {row['value']:>5} series")

# --- 2. Instant query: one value per series, "as of now" ---------------------
print("\nResident memory per component:")
for r in api("/api/v1/query", query="process_resident_memory_bytes")["result"]:
    ts, val = r["value"]
    print(f"  {r['metric']['job']:<16} {float(val) / 2**20:7.1f} MiB  (at {iso(ts)})")

print("Ingest rate:", end=" ")
r = api("/api/v1/query", query="sum(rate(vm_rows_inserted_total[5m]))")["result"]
print(f"{float(r[0]['value'][1]):.0f} rows/s")

# --- 3. Find when demo_metric (victoria-generator/sine.py) has data ----------
first = api("/api/v1/query", query="tfirst_over_time(demo_metric[30d])")["result"]
last = api("/api/v1/query", query="tlast_over_time(demo_metric[30d])")["result"]
if not first:
    sys.exit("\nno demo_metric in the last 30d; run ../victoria-generator/sine.py for a while")
t0, t1 = float(first[0]["value"][1]), float(last[0]["value"][1])
print(f"\ndemo_metric spans {iso(t0)} .. {iso(t1)} UTC")

# --- 4. Range query: values on a regular grid, last 10 minutes of the data --
start, end = t1 - 600, t1
res = api("/api/v1/query_range", query="demo_metric", start=start, end=end, step="1m")["result"]
print("query_range step=1m:")
for ts, val in res[0]["values"]:
    print(f"  {iso(ts)}  {float(val):7.3f}")

# --- 5. Export: every raw sample, as JSON lines (one line per series) -------
raw = get("/api/v1/export", **{"match[]": "demo_metric", "start": start, "end": end})
for line in raw.splitlines():
    series = json.loads(line)
    vals = series["values"]
    print(f"export: {series['metric']} -> {len(vals)} raw samples, "
          f"min {min(vals):.2f} max {max(vals):.2f} mean {statistics.fmean(vals):.2f}")
