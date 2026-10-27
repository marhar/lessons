-- Run a PromQL range query via the Prometheus HTTP API (DuckDB 2.0+)
-- Run: duckdb -f prometheus.sql
-- Works against Prometheus (:9090) or anything serving its API, e.g. VictoriaMetrics (:8428)
SET VARIABLE prom_url = 'http://localhost:8428';

WITH series AS (
    SELECT unnest(data.result) AS s
    FROM read_json(
        getvariable('prom_url') || '/api/v1/query_range?query=demo_metric'
        || '&start=' || (epoch(now()) - 3600)::BIGINT
        || '&end='   || epoch(now())::BIGINT
        || '&step=15s'
    )
)
SELECT
    s.metric.__name__           AS name,
    s.metric.env                AS env,
    to_timestamp(p[1]::DOUBLE)  AS ts,
    p[2]::DOUBLE                AS value
FROM (SELECT s, unnest(s."values") AS p FROM series)
ORDER BY ts DESC
LIMIT 10;
