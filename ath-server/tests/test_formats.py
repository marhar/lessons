"""format=json|csv|parquet|arrow: content types, exact text, decoded tables."""
import csv
import io
import json
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data import END, T0, WEIRD, labels

TYPES = {'json': 'application/json', 'csv': 'text/csv',
         'parquet': 'application/vnd.apache.parquet', 'arrow': 'application/vnd.apache.arrow.stream'}


def get(server, path, *match, **params):
    params = {'start': T0, 'end': END} | params
    r = server.get(path, *[('match[]', m) for m in match], **params)
    assert r.status == 200, r
    return r


def decode(r):
    if r.ctype == TYPES['parquet']:
        return pq.read_table(io.BytesIO(r.body))
    return pa.ipc.open_stream(r.body).read_all()


@pytest.mark.parametrize('fmt', TYPES)
@pytest.mark.parametrize('path', ['/api/v1/metrics', '/api/v1/samples', '/api/v1/latest'])
def test_content_types(server, path, fmt):
    assert get(server, path, 'demo_metric', format=fmt).ctype == TYPES[fmt]


def test_json_is_written_like_python(server):
    # ", " and ": " separators, floats as repr(), non-ASCII and control characters escaped
    body = get(server, '/api/v1/samples', 'weird_metric').text
    assert '"labels": {"city": "Z\\u00fcrich", "note": "say \\"hi\\"\\n"}' in body
    assert '"values": [1e-05, 0.30000000000000004, 1.5e+16]' in body


def test_csv_exact(server):
    r = get(server, '/api/v1/latest', 'go_goroutines', format='csv')
    assert r.text == (
        'name,labels,ts,value\n'
        'go_goroutines,"{""job"":""a"",""instance"":""a:1""}",2026-10-30T00:04:45.000Z,119.0\n'
        'go_goroutines,"{""job"":""b"",""instance"":""b:1""}",2026-10-30T00:04:45.000Z,\n')


def test_csv_labels_are_json(server):
    rows = list(csv.DictReader(io.StringIO(get(server, '/api/v1/samples', 'weird_metric', format='csv').text)))
    assert [json.loads(r['labels']) for r in rows] == [labels(WEIRD)] * 3
    assert [float(r['value']) for r in rows] == [1e-05, 0.1 + 0.2, 1.5e16]


def test_csv_step(server):
    rows = list(csv.reader(io.StringIO(
        get(server, '/api/v1/samples', 'demo_metric', step='1m', agg='avg', format='csv').text)))
    assert [r[2:] for r in rows] == [
        ['ts', 'value'],
        ['2026-10-30T00:00:00.000Z', '3.0'], ['2026-10-30T00:01:00.000Z', '9.0'],
        ['2026-10-30T00:02:00.000Z', '15.0'], ['2026-10-30T00:03:00.000Z', '21.0'],
        ['2026-10-30T00:04:00.000Z', '27.0']]


SCHEMA = pa.schema([('name', pa.string()), ('labels', pa.map_(pa.string(), pa.string())),
                    ('ts', pa.timestamp('us')), ('value', pa.float64())])


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
def test_binary_latest(server, fmt):
    t = decode(get(server, '/api/v1/latest', 'go_goroutines', 'weird_metric', format=fmt))
    assert t.schema.equals(SCHEMA)
    assert t.to_pylist() == [
        {'name': 'go_goroutines', 'labels': [('job', 'a'), ('instance', 'a:1')],
         'ts': datetime(2026, 10, 30, 0, 4, 45), 'value': 119.0},
        {'name': 'go_goroutines', 'labels': [('job', 'b'), ('instance', 'b:1')],
         'ts': datetime(2026, 10, 30, 0, 4, 45), 'value': None},
        {'name': 'weird_metric', 'labels': list(labels(WEIRD).items()),
         'ts': datetime(2026, 10, 30, 0, 3), 'value': 1.5e16},
    ]


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
def test_binary_samples(server, fmt):
    t = decode(get(server, '/api/v1/samples', 'demo_metric', 'go_goroutines', step='1m', agg='sum', format=fmt))
    assert t.schema.equals(SCHEMA)
    assert t.num_rows == 15
    assert t.column('value').to_pylist()[:5] == [18.0, 54.0, 90.0, 126.0, 162.0]
    assert t.column('ts').to_pylist()[:2] == [datetime(2026, 10, 30, 0, 0), datetime(2026, 10, 30, 0, 1)]


@pytest.mark.parametrize('fmt', ['parquet', 'arrow'])
def test_binary_metrics(server, fmt):
    t = decode(get(server, '/api/v1/metrics', format=fmt))
    assert t.column_names == ['name']
    assert t.column('name').to_pylist()[0] == 'demo_metric'


@pytest.mark.parametrize('fmt', ['csv', 'parquet', 'arrow'])
def test_empty_tables(server, fmt):
    r = get(server, '/api/v1/samples', 'nothing_here', format=fmt)
    if fmt == 'csv':
        assert r.text == 'name,labels,ts,value\n'
    else:
        assert decode(r).num_rows == 0
