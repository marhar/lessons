-- Combine Prometheus (query_range API) and VictoriaMetrics (export API) samples (DuckDB 2.0+)
-- Run: duckdb -f both.sql
-- No Prometheus is running locally, so prom_url points at VictoriaMetrics, which serves the same API; use :9090 for real Prometheus
SET VARIABLE prom_url = 'http://localhost:8428';
SET VARIABLE vm_url   = 'http://localhost:8428';

-- S3 credentials from your AWS SSO login (run `aws sso login --profile data_products` if expired)
CREATE OR REPLACE SECRET aws_s3 (
    TYPE s3,
    PROVIDER credential_chain,
    CHAIN 'sso',
    REGION 'us-west-2'
);

COPY (
WITH prometheus AS (
    SELECT
        'prometheus'                AS source,
        s.metric.__name__           AS name,
        s.metric.env                AS env,
        to_timestamp(p[1]::DOUBLE)  AS ts,
        p[2]::DOUBLE                AS value
    FROM (
        SELECT s, unnest(s."values") AS p
        FROM (
            SELECT unnest(data.result) AS s
            FROM read_json(
                getvariable('prom_url') || '/api/v1/query_range?query=demo_metric'
                || '&start=' || (epoch(now()) - 3600)::BIGINT
                || '&end='   || epoch(now())::BIGINT
                || '&step=15s'
            )
        )
    )
),
victoriametrics AS (
    SELECT
        'victoriametrics'                                    AS source,
        metric.__name__                                      AS name,
        metric.env                                           AS env,
        to_timestamp(unnest(timestamps) / 1000)             AS ts,
        unnest("values")                                     AS value
    FROM read_json(getvariable('vm_url') || '/api/v1/export?match[]=demo_metric&start=-1h')
)
SELECT * FROM prometheus
UNION ALL BY NAME
SELECT * FROM victoriametrics
ORDER BY ts DESC
) TO 's3://dataproducts-marhar-dev/demo.parquet';

SELECT * FROM 's3://dataproducts-marhar-dev/demo.parquet';
