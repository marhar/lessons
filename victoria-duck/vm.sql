-- Reusable table macros for VictoriaMetrics.  Load with `.read vm.sql` (CLI) or run it first.
--   FROM vm_export('{__name__="demo_metric"}', '-1h', 'now');
--   FROM vm_query_range('rate(vm_rows_inserted_total[5m])', '-1h', 'now', '1m');
-- Arguments are url_encode()d, so PromQL can contain + * & # [] {} and quotes.
SET TimeZone = 'UTC';
SET force_download = true;
SET VARIABLE vm = 'http://localhost:8428';

-- Raw samples: one row per sample, labels as a MAP.
CREATE OR REPLACE MACRO vm_export(match, start_t, end_t) AS TABLE
SELECT metric['__name__']                          AS name,
       metric                                      AS labels,
       make_timestamptz(unnest(timestamps) * 1000) AS ts,
       unnest("values")                            AS value
FROM read_json(getvariable('vm') || '/api/v1/export?match[]=' || url_encode(match)
               || '&start=' || url_encode(start_t) || '&end=' || url_encode(end_t),
               format = 'newline_delimited',
               columns = {metric: 'MAP(VARCHAR, VARCHAR)',
                          "values": 'DOUBLE[]', timestamps: 'BIGINT[]'});

-- PromQL/MetricsQL on a grid: one row per (series, step), labels as a MAP.
-- json_transform pins the type, so an empty result gives 0 rows instead of a type error.
CREATE OR REPLACE MACRO vm_query_range(query, start_t, end_t, step) AS TABLE
WITH series AS (
  SELECT unnest(json_transform(data, '{"result":[{"metric":"MAP(VARCHAR,VARCHAR)","values":"JSON[]"}]}').result) AS s
  FROM read_json(getvariable('vm') || '/api/v1/query_range?query=' || url_encode(query)
                 || '&start=' || url_encode(start_t) || '&end=' || url_encode(end_t)
                 || '&step=' || url_encode(step),
                 columns = {data: 'JSON'})
), points AS (
  SELECT s.metric AS labels, unnest(s."values") AS p FROM series
)
SELECT labels,
       to_timestamp((p->>0)::DOUBLE) AS ts,
       (p->>1)::DOUBLE               AS value
FROM points;
