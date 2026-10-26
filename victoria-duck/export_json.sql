-- Method 2: /api/v1/export -> read_json.  Raw samples, every label kept.
--   duckdb -f export_json.sql
-- Each line is one series: {"metric":{...labels},"values":[...],"timestamps":[ms,...]}
SET TimeZone = 'UTC';
SET force_download = true;

-- Two unnests in one SELECT are zipped together, giving one row per (timestamp, value).
-- Declare the columns: inference makes `metric` a STRUCT of whichever labels it saw
-- first (breaks across series) and can type `values` as BIGINT[] if they look integral.
SELECT metric['__name__']                           AS name,
       metric                                       AS labels,
       make_timestamptz(unnest(timestamps) * 1000)  AS ts,      -- ms -> us
       unnest("values")                             AS value
FROM read_json('http://localhost:8428/api/v1/export?match[]=demo_metric'
               '&start=2026-10-23T17:30:00Z&end=2026-10-23T17:30:01Z',
               format = 'newline_delimited',
               columns = {metric: 'MAP(VARCHAR, VARCHAR)',
                          "values": 'DOUBLE[]', timestamps: 'BIGINT[]'});

-- Several series with different label sets. A `*` in the URL is taken as a file glob,
-- so write it as %2A (or SET allow_asterisks_in_http_paths = true).
SELECT metric['job']                         AS job,
       count(*)                              AS samples,
       round(max(v) / 2^20, 1)               AS max_rss_mib
FROM (SELECT metric, unnest("values") AS v
      FROM read_json('http://localhost:8428/api/v1/export'
                     '?match[]={__name__="process_resident_memory_bytes",job=~"v.%2A"}&start=-5m',
                     format = 'newline_delimited',
                     columns = {metric: 'MAP(VARCHAR, VARCHAR)',
                                "values": 'DOUBLE[]', timestamps: 'BIGINT[]'}))
GROUP BY ALL ORDER BY job;
