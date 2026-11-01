"""
Cross-check server.py and the C++ server against a real VictoriaMetrics.

  pytest -m live [--live-vm http://localhost:8428]

Both servers get the same requests over a fixed window in the past, so they
see the same data. JSON, CSV and error bodies must match byte for byte;
Parquet and Arrow must decode to equal tables.
"""
import io
import math
import time
import urllib.parse
import urllib.request

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from conftest import Server

pytestmark = pytest.mark.live

T = int(time.time()) - 120
A, B = T - 600, T
TOTAL = '%7B__name__%3D~%22.%2A_total%22%7D'
GO = '%7B__name__%3D~%22go_.%2A%22%7D'
CASES = [
    '/api/v1/metrics', '/api/v1/metrics?format=csv', '/api/v1/metrics?match[]=%7Bjob%3D%22vmagent%22%7D',
    f'/api/v1/metrics?start={A}&end={B}', '/api/v1/metrics?format=parquet', '/api/v1/metrics?format=arrow',
    f'/api/v1/latest?match[]=demo_metric&match[]=go_goroutines&start={A}&end={B}',
    f'/api/v1/latest?match[]={TOTAL}&start={A}&end={B}',
    f'/api/v1/latest?match[]={TOTAL}&start={A}&end={B}&format=csv',
    f'/api/v1/latest?match[]=go_goroutines&start={A}&end={B}&format=parquet',
    f'/api/v1/latest?match[]=go_goroutines&start={A}&end={B}&format=arrow',
    f'/api/v1/samples?match[]=go_goroutines&match[]=demo_metric&start={A}&end={B}',
    f'/api/v1/samples?match[]=process_resident_memory_bytes&start={A}&end={B}&format=csv',
    f'/api/v1/samples?match[]={GO}&start={A}&end={B}&format=parquet',
    f'/api/v1/samples?match[]={GO}&start={A}&end={B}&format=arrow',
    *[f'/api/v1/samples?match[]=demo_metric&match[]=go_goroutines&start={A}&end={B}&step=1m&agg={g}'
      for g in ['last', 'first', 'avg', 'min', 'max', 'sum', 'count']],
    f'/api/v1/samples?match[]=demo_metric&start={A}&end={B}&step=90s',
    f'/api/v1/samples?match[]=demo_metric&start={A}.5&end={B}.25',
    '/api/v1/samples?match[]=%7Bbad', '/api/v1/latest?match[]=%7Bbad',
]


@pytest.fixture(scope='module')
def servers(request, tmp_path_factory):
    url = request.config.getoption('live_vm')
    try:
        urllib.request.urlopen(url + '/api/v1/label/__name__/values', timeout=5).read()
    except OSError as e:
        pytest.skip(f'no VictoriaMetrics at {url}: {e}')
    logs = tmp_path_factory.mktemp('live')
    started = [Server(impl, url, logs / f'{impl}.log') for impl in ('py', 'cpp')]
    yield started
    for srv in started:
        srv.stop()


def decode(r):
    if 'parquet' in r.ctype:
        return pq.read_table(io.BytesIO(r.body))
    if 'arrow' in r.ctype:
        return pa.ipc.open_stream(r.body).read_all()


@pytest.mark.parametrize('target', CASES)
def test_same_answer(servers, target):
    py, cpp = (srv.raw(target) for srv in servers)
    assert (py.status, py.ctype) == (cpp.status, cpp.ctype)
    if decode(py) is not None:
        assert decode(py).equals(decode(cpp))
    else:
        assert py.body == cpp.body


@pytest.fixture(scope='module')
def inf_series(request, servers):
    """athtest_inf with ±Inf samples, written into the live VM and deleted afterwards."""
    url = request.config.getoption('live_vm')
    sel = urllib.parse.urlencode({'match[]': 'athtest_inf'})
    # VM drops NaN on import and exports ±Inf as the strings "Infinity"/"-Infinity"
    lines = ''.join(f'athtest_inf {v} {(A + 10 * i) * 1000}\n' for i, v in enumerate(['1', '+Inf', '4', '-Inf', '2']))
    urllib.request.urlopen(url + '/api/v1/import/prometheus', data=lines.encode(), timeout=5).read()
    urllib.request.urlopen(url + '/internal/force_flush', timeout=5).read()
    yield
    urllib.request.urlopen(url + '/api/v1/admin/tsdb/delete_series?' + sel, data=b'', timeout=5).read()


@pytest.mark.parametrize('agg, want', [('max', math.inf), ('min', -math.inf), ('count', 5.0), ('last', 2.0)])
def test_infinity_is_a_value(servers, inf_series, agg, want):
    # one bucket over all five samples; ±Inf counts, like any other sample
    target = f'/api/v1/samples?match[]=athtest_inf&start={A}&end={A + 60}&step=1m&agg={agg}&format=parquet'
    for srv in servers:
        r = srv.raw(target)
        assert r.status == 200, (srv.impl, r)
        assert decode(r).column('value').to_pylist() == [want], srv.impl
