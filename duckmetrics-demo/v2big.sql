-- Like v2.sql, plus every *_total counter (DuckDB 2.0+)
-- Run: duckdb -f v2big.sql
-- The regex matcher {__name__=~".*_total"} is URL-encoded: DuckDB rejects a raw * in HTTP paths
WITH samples AS (
    SELECT
        metric.__name__                        AS name,
        metric.env                             AS env,
        metric.job                             AS job,
        metric                                 AS labels,
        make_timestamp_ms(unnest(timestamps))  AS ts,
        unnest("values")                       AS value
    FROM read_json('http://localhost:8428/api/v1/export?start=-1h'
        || '&match[]=demo_metric'
        || '&match[]=process_resident_memory_bytes'
        || '&match[]=go_goroutines'
        || '&match[]=%7B__name__%3D~%22.%2A_total%22%7D')
)
-- latest sample per series; counters have extra labels (path, type, ...), so partition on all of them
SELECT * EXCLUDE (labels) FROM samples
QUALIFY row_number() OVER (PARTITION BY labels ORDER BY ts DESC) = 1
ORDER BY name, job;
