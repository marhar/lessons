-- Read raw samples from local VictoriaMetrics via its export API (DuckDB 2.0+)
-- Run: duckdb -f v2.sql
-- Repeat match[] to fetch several metrics in one request
WITH samples AS (
    SELECT
        metric.__name__                        AS name,
        metric.env                             AS env,
        metric.job                             AS job,
        make_timestamp_ms(unnest(timestamps))  AS ts,
        unnest("values")                       AS value
    FROM read_json('http://localhost:8428/api/v1/export?start=-1h'
        || '&match[]=demo_metric'
        || '&match[]=process_resident_memory_bytes'
        || '&match[]=process_cpu_seconds_total'
        || '&match[]=go_goroutines')
)
-- latest sample per series, so every metric shows up
SELECT * FROM samples
QUALIFY row_number() OVER (PARTITION BY name, env, job ORDER BY ts DESC) = 1
ORDER BY name, job;
