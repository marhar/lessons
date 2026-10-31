#!/usr/bin/env python3
"""
REST front end for VictoriaMetrics that shapes the data with DuckDB.

Each request becomes one read_json() over VictoriaMetrics' /api/v1/export
(the same query as ../duckmetrics-demo/v2big.sql), so DuckDB does the
unnesting, bucketing and latest-per-series work.

Endpoints (all GET):
  /api/v1/metrics   metric names            match[], start, end, format
  /api/v1/samples   raw or bucketed samples match[], start, end, step, agg, format
  /api/v1/latest    last sample per series  match[], start, end, format

Setup:
  pip install -r requirements.txt

Usage:
  python server.py                             # :8000 -> http://localhost:8428
  python server.py --port 9000 --vm http://vm:8428
"""
import argparse
import io
import json
import math
import os
import re
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import duckdb

VM_URL = 'http://localhost:8428'
DEFAULT_RANGE_MS = 3_600_000    # start defaults to end - 1h
MAX_POINTS = 11_000             # per series, same cap as Prometheus query_range

FORMATS = {
    'json': 'application/json',
    'csv': 'text/csv',
    'parquet': 'application/vnd.apache.parquet',
    'arrow': 'application/vnd.apache.arrow.stream',
}

# agg names are whitelisted here, never interpolated from the request
AGGS = {
    'last': 'arg_max(value, ts)',
    'first': 'arg_min(value, ts)',
    'avg': 'avg(value)',
    'min': 'min(value)',
    'max': 'max(value)',
    'sum': 'sum(value)',
    'count': 'count(value)::DOUBLE',
}


class BadRequest(Exception):
    pass


# -- time parameters ----------------------------------------------------------
# Accepts what Prometheus/VictoriaMetrics accept: unix seconds, RFC3339,
# 'now', and relative durations ('-1h', 'now-30m'). Everything is resolved
# to epoch ms once per request, so start and end share the same 'now'.

UNITS_MS = {'ms': 1, 's': 1000, 'm': 60_000, 'h': 3_600_000,
            'd': 86_400_000, 'w': 604_800_000, 'y': 31_536_000_000}
DURATION_RE = re.compile(r'(\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)')


def parse_duration(s):
    """'90s', '1h30m' or bare seconds ('60') -> ms."""
    try:
        return int(float(s) * 1000)
    except ValueError:
        pass
    parts = DURATION_RE.findall(s)
    if not parts or ''.join(n + u for n, u in parts) != s:
        raise BadRequest(f'bad duration {s!r}: use e.g. 30s, 5m, 1h30m')
    return int(sum(float(n) * UNITS_MS[u] for n, u in parts))


def parse_time(s, now_ms):
    """unix seconds | RFC3339 | now | -<duration> | now-<duration> -> ms."""
    if s == 'now':
        return now_ms
    if s.startswith('now-'):
        return now_ms - parse_duration(s[4:])
    if s.startswith('-'):
        return now_ms - parse_duration(s[1:])
    try:
        return int(float(s) * 1000)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise BadRequest(f'bad time {s!r}: use unix seconds, RFC3339, now or -1h')
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def time_range(params):
    now_ms = int(time.time() * 1000)
    end = parse_time(params['end'][-1], now_ms) if 'end' in params else now_ms
    start = (parse_time(params['start'][-1], now_ms) if 'start' in params
             else end - DEFAULT_RANGE_MS)
    if start >= end:
        raise BadRequest('start must be before end')
    return start, end


# -- queries --------------------------------------------------------------------

def export_url(matches, start, end):
    # urlencode escapes {, *, ~ etc.; DuckDB rejects a raw * in HTTP paths
    q = [('match[]', m) for m in matches]
    q += [('start', f'{start / 1000:.3f}'), ('end', f'{end / 1000:.3f}')]
    return f'{VM_URL}/api/v1/export?{urllib.parse.urlencode(q)}'


# One row per sample. Explicit columns: labels differ per metric, so a MAP
# beats letting read_json infer a struct of every label key it sees.
SAMPLES = """
WITH series AS (
    SELECT
        metric['__name__']  AS name,
        map_from_entries(list_filter(map_entries(metric),
                                     lambda e: e.key <> '__name__')) AS labels,
        timestamps,
        "values"
    FROM read_json($url, format = 'newline_delimited', columns = {
        'metric': 'MAP(VARCHAR, VARCHAR)',
        'timestamps': 'BIGINT[]',
        'values': 'DOUBLE[]'})
),
samples AS (
    SELECT name, labels, unnest(timestamps) AS ts, unnest("values") AS value
    FROM series
)
"""

# buckets are aligned to start, stamped with the bucket's start time
BUCKETED = """,
bucketed AS (
    SELECT name, labels, $start + (ts - $start) // $step * $step AS bucket,
           {agg} AS value
    FROM samples
    GROUP BY name, labels, bucket
),
points AS (SELECT name, labels, bucket AS ts, value FROM bucketed)
"""

RAW = """,
points AS (SELECT * FROM samples)
"""

# latest sample per series, as in v2big.sql
LATEST = """,
points AS (
    SELECT * FROM samples
    QUALIFY row_number() OVER (PARTITION BY name, labels ORDER BY ts DESC) = 1
)
"""

TABLE = """
SELECT name, labels, make_timestamp_ms(ts) AS ts, value
FROM points
ORDER BY name, labels, ts
"""

# json for /samples: one object per series, timestamps and values as arrays
SERIES = """
SELECT name, labels, list(ts ORDER BY ts) AS timestamps,
       list(value ORDER BY ts) AS values
FROM points
GROUP BY name, labels
ORDER BY name, labels
"""

METRICS = "SELECT unnest(data) AS name FROM read_json($url)"


def finite(v):
    # JSON has no NaN/Inf (VictoriaMetrics stores staleness markers as NaN)
    return v if v is not None and math.isfinite(v) else None


class Handler(BaseHTTPRequestHandler):
    con: duckdb.DuckDBPyConnection  # set in main; one cursor per request

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        params = urllib.parse.parse_qs(url.query)
        route = {
            '/api/v1/metrics': self.metrics,
            '/api/v1/samples': self.samples,
            '/api/v1/latest': self.latest,
        }.get(url.path)
        if route is None:
            return self.error(404, f'no route {url.path}')
        try:
            fmt = params.get('format', ['json'])[-1]
            if fmt not in FORMATS:
                raise BadRequest(f'format must be one of {", ".join(FORMATS)}')
            route(params, fmt, self.con.cursor())
        except BadRequest as e:
            self.error(400, str(e))
        except duckdb.HTTPException as e:
            # pass VictoriaMetrics' own message through (bad selector, etc.)
            body = (e.body or '').strip()
            code = 400 if e.status_code == 400 else 502
            self.error(code, f'victoriametrics: {body or e}')
        except duckdb.Error as e:
            self.error(500, str(e))

    def metrics(self, params, fmt, cur):
        q = [('match[]', m) for m in params.get('match[]', [])]
        # VictoriaMetrics picks its own default range when none is given
        q += [(k, f'{parse_time(params[k][-1], int(time.time() * 1000)) / 1000:.3f}')
              for k in ('start', 'end') if k in params]
        vm = f'{VM_URL}/api/v1/label/__name__/values?{urllib.parse.urlencode(q)}'
        rel = cur.sql(METRICS, params={'url': vm})
        if fmt == 'json':
            return self.json({'status': 'success',
                              'data': [r[0] for r in rel.fetchall()]})
        self.table(rel, fmt)

    def samples(self, params, fmt, cur):
        start, end = time_range(params)
        args = {'url': export_url(self.matches(params), start, end)}
        step = None
        if 'step' in params:
            step = parse_duration(params['step'][-1])
            if step <= 0:
                raise BadRequest('step must be positive')
            if (end - start) // step > MAX_POINTS:
                raise BadRequest(f'more than {MAX_POINTS} points per series: '
                                 'raise step or shorten the range')
            agg = params.get('agg', ['last'])[-1]
            if agg not in AGGS:
                raise BadRequest(f'agg must be one of {", ".join(AGGS)}')
            sql = SAMPLES + BUCKETED.format(agg=AGGS[agg])
            args |= {'start': start, 'step': step}
        elif 'agg' in params:
            raise BadRequest('agg needs step')
        else:
            sql = SAMPLES + RAW

        if fmt != 'json':
            return self.table(cur.sql(sql + TABLE, params=args), fmt)
        rows = cur.execute(sql + SERIES, args).fetchall()
        self.json({
            'status': 'success',
            'start': start, 'end': end, 'step': step,
            'data': [{'name': n, 'labels': l, 'timestamps': t,
                      'values': [finite(v) for v in vs]}
                     for n, l, t, vs in rows],
        })

    def latest(self, params, fmt, cur):
        start, end = time_range(params)
        args = {'url': export_url(self.matches(params), start, end)}
        sql = SAMPLES + LATEST
        if fmt != 'json':
            return self.table(cur.sql(sql + TABLE, params=args), fmt)
        rows = cur.execute(sql + "SELECT * FROM points ORDER BY name, labels",
                           args).fetchall()
        self.json({
            'status': 'success',
            'start': start, 'end': end,
            'data': [{'name': n, 'labels': l, 'timestamp': t, 'value': finite(v)}
                     for n, l, t, v in rows],
        })

    @staticmethod
    def matches(params):
        m = params.get('match[]')
        if not m:
            raise BadRequest('need at least one match[] series selector')
        return m

    # -- responses ----------------------------------------------------------

    def table(self, rel, fmt):
        """Long format (name, labels, ts, value) as csv, parquet or arrow."""
        if fmt == 'arrow':
            import pyarrow as pa
            buf = io.BytesIO()
            reader = rel.arrow()
            with pa.ipc.new_stream(buf, reader.schema) as w:
                for batch in reader:
                    w.write_batch(batch)
            return self.send(200, FORMATS[fmt], buf.getvalue())
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'out')
            if fmt == 'csv':
                # csv has no MAP type: labels go out as a JSON object string
                cols = [f'to_json("{c}")::VARCHAR AS "{c}"'
                        if t.id == 'map' else f'"{c}"'
                        for c, t in zip(rel.columns, rel.types)]
                rel.select(', '.join(cols)).write_csv(
                    path, timestamp_format='%Y-%m-%dT%H:%M:%S.%gZ')
            else:
                rel.write_parquet(path)
            with open(path, 'rb') as f:
                self.send(200, FORMATS[fmt], f.read())

    def json(self, obj):
        self.send(200, FORMATS['json'], json.dumps(obj).encode())

    def error(self, code, msg):
        body = json.dumps({'status': 'error', 'error': msg}).encode()
        self.send(code, FORMATS['json'], body)

    def send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    global VM_URL
    ap = argparse.ArgumentParser(description='REST front end for VictoriaMetrics, via DuckDB')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=8000)
    ap.add_argument('--vm', default=VM_URL, help='VictoriaMetrics base URL')
    args = ap.parse_args()
    VM_URL = args.vm.rstrip('/')

    # VictoriaMetrics answers are live; never serve a cached HTTP body
    Handler.con = duckdb.connect(config={'enable_external_file_cache': False})

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'listening on http://{args.host}:{args.port} -> {VM_URL}')
    srv.serve_forever()


if __name__ == '__main__':
    main()
