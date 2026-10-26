-- Export once to Parquet, then query locally (no HTTP on every query).
--   duckdb -f snapshot.sql
.read vm.sql

COPY (FROM vm_export('demo_metric', '2026-10-23T17:30:00Z', '2026-10-23T18:30:00Z'))
TO 'demo_metric.parquet' (FORMAT parquet);

SELECT count(*) AS samples, min(ts) AS first, max(ts) AS last,
       round(min(value), 2) AS min, round(max(value), 2) AS max
FROM 'demo_metric.parquet';

-- Gaps: where sine.py stopped writing (expected spacing is ~0.25 s).
SELECT ts - gap AS gap_start, ts AS gap_end, gap
FROM (SELECT ts, ts - lag(ts) OVER (ORDER BY ts) AS gap FROM 'demo_metric.parquet')
WHERE gap > INTERVAL 2 seconds
ORDER BY gap DESC LIMIT 5;

-- The macros also cover MetricsQL with characters that break hand-built URLs.
SELECT labels['job'] AS job, ts, round(value, 1) AS value
FROM vm_query_range('sum(rate(vm_rows_inserted_total[5m])) by (job) * 60 + 0', '-10m', 'now', '5m')
ORDER BY job, ts;
