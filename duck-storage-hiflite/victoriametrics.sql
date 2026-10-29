-- Read raw samples from local VictoriaMetrics via its export API (DuckDB 2.0+)
-- Run: duckdb -f victoriametrics.sql
SELECT
    metric.__name__                        AS name,
    metric.env                             AS env,
    make_timestamp_ms(unnest(timestamps))  AS ts,
    unnest("values")                       AS value
FROM read_json('http://localhost:8428/api/v1/export?match[]=demo_metric&start=-1h')
ORDER BY ts DESC
LIMIT 10;
