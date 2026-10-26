-- Method 1: /api/v1/export/csv -> read_csv.  Raw samples, one row per sample, flat.
--   duckdb -f export_csv.sql
-- Data: demo_metric from ../victoria-generator/sine.py (4 samples/s, 5-min sine, 0..100).
SET TimeZone = 'UTC';
SET force_download = true;   -- live endpoint: HEAD and GET can return different sizes

-- VM sends a header row named after the `format` fields; DuckDB sniffs the types
-- (__timestamp__:rfc3339 -> TIMESTAMPTZ, __timestamp__:unix_ms -> BIGINT).
SELECT *
FROM read_csv('http://localhost:8428/api/v1/export/csv'
              '?format=__name__,env,__timestamp__:rfc3339,__value__'
              '&match[]={__name__="demo_metric",env="test"}'
              '&start=2026-10-23T17:30:00Z&end=2026-10-23T17:30:01Z');

-- Rename the columns with `names` and do real analysis: per-minute stats over 10 minutes.
-- (Compare max with VM's max_over_time(demo_metric[1m]); count is ~4/s * 60 = ~233.)
SELECT time_bucket(INTERVAL 1 minute, ts) AS minute,
       count(*)                AS n,
       round(min(value), 2)    AS min,
       round(avg(value), 2)    AS avg,
       round(max(value), 2)    AS max
FROM read_csv('http://localhost:8428/api/v1/export/csv'
              '?format=__name__,env,__timestamp__:rfc3339,__value__'
              '&match[]=demo_metric'
              '&start=2026-10-23T17:30:00Z&end=2026-10-23T17:40:00Z',
              names = ['name', 'env', 'ts', 'value'])
GROUP BY ALL ORDER BY minute;
