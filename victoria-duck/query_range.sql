-- Method 3: /api/v1/query_range -> read_json.  PromQL/MetricsQL evaluated by VM, on a grid.
--   duckdb -f query_range.sql
-- Response: {"status":..,"data":{"result":[{"metric":{..},"values":[[unix_s,"value"],..]}]}}
SET TimeZone = 'UTC';
SET force_download = true;

-- Unnest series, then points; each point is [seconds, "string value"] -> cast both.
-- url_encode() keeps PromQL intact: unencoded, `+` would arrive as a space and `*` would be a glob.
WITH series AS (
  SELECT unnest(data.result) AS s
  FROM read_json('http://localhost:8428/api/v1/query_range?query='
                 || url_encode('sum(rate(vm_http_requests_total[5m])) by (job)')
                 || '&start=-10m&step=5m')
), points AS (
  SELECT s.metric AS metric, unnest(s."values") AS p FROM series
)
SELECT metric.job                   AS job,
       to_timestamp(p[1]::DOUBLE)   AS ts,
       round(p[2]::DOUBLE, 3)       AS req_per_s
FROM points
ORDER BY job, ts;
