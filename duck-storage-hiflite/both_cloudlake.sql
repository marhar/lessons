-- Combine Prometheus (query_range API) and VictoriaMetrics (export API) samples (DuckDB 2.0+)
-- Run: duckdb -f both_cloudlake.sql
-- Stores results in DuckLake catalog lake1: DuckDB metadata file cloudlake1.ducklake, Parquet data in s3://dataproducts-ducklake-dev/scratch1_data/
-- No Prometheus is running locally, so prom_url points at VictoriaMetrics, which serves the same API; use :9090 for real Prometheus
SET VARIABLE prom_url = 'http://localhost:8428';
SET VARIABLE vm_url   = 'http://localhost:8428';

CREATE OR REPLACE SECRET aws_s3 (
    TYPE s3,
    PROVIDER credential_chain,
    CHAIN 'sso',
    PROFILE 'data_products',
    REGION 'us-west-2'
);

ATTACH 'ducklake:cloudlake1.ducklake' AS lake1 (DATA_PATH 's3://dataproducts-ducklake-dev/scratch1_data/');
USE lake1;

CREATE OR REPLACE TABLE lake_data AS (
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
);

SELECT * FROM lake_data;
