"""Method 4: fetch PromQL results in Python, hand the DataFrame to DuckDB.

    python query_range.py [base_url]      # default: http://localhost:8428

Use this when the HTTP side needs more than a URL (auth headers, retries, paging
through long ranges) or when the result feeds pandas code anyway.
"""

import json
import sys
import time
import urllib.parse
import urllib.request

import duckdb
import pandas as pd

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8428"


def query_range(query, start, end="now", step="1m"):
    """One row per (series, point): the labels as columns, plus ts and value."""
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{BASE}/api/v1/query_range?{params}") as resp:
        body = json.load(resp)
    if body["status"] != "success":
        raise RuntimeError(body)
    rows = [{**s["metric"], "ts": t, "value": float(v)}
            for s in body["data"]["result"] for t, v in s["values"]]
    df = pd.DataFrame(rows)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


# A fixed, minute-aligned window: two calls with start="-30m" would each resolve "now"
# separately and their grids wouldn't line up for a join.
END = int(time.time()) // 60 * 60
START = END - 30 * 60

# VM evaluates the PromQL; DuckDB queries the DataFrame by its variable name.
rps = query_range("sum(rate(vm_http_requests_total[5m])) by (job)", START, END, step="1m")
print(duckdb.sql("""
    SELECT job, count(*) AS points,
           round(min(value), 2) AS min, round(avg(value), 2) AS avg, round(max(value), 2) AS max
    FROM rps GROUP BY job ORDER BY job
"""))

# MetricsQL rollup on the VM side, then a join on the DuckDB side.
mem = query_range("max_over_time(process_resident_memory_bytes[5m])", START, END, step="5m")
print(duckdb.sql("""
    SELECT m.job, m.ts, round(m.value / 2^20, 1) AS rss_mib, round(r.value, 2) AS req_per_s
    FROM mem m JOIN rps r USING (job, ts)
    ORDER BY m.job, m.ts DESC LIMIT 6
"""))
