"""Error responses: request validation, and VictoriaMetrics failing or missing."""
import urllib.parse

import pytest

from data import T0


def error(r):
    assert r.ctype == 'application/json'
    return r.status, r.json()


@pytest.mark.parametrize('target, status, msg', [
    ('/', 404, 'no route /'),
    ('/nope?format=csv', 404, 'no route /nope'),
    ('/api/v1/metrics/', 404, 'no route /api/v1/metrics/'),
    ('/api/v1/samples', 400, 'need at least one match[] series selector'),
    ('/api/v1/latest?start=-1h', 400, 'need at least one match[] series selector'),
    ('/api/v1/samples?match[]=', 400, 'need at least one match[] series selector'),   # blank = absent
    ('/api/v1/samples?match[]=x&format=xml', 400, 'format must be one of json, csv, parquet, arrow'),
    ('/api/v1/metrics?format=xml', 400, 'format must be one of json, csv, parquet, arrow'),
    ('/api/v1/samples?format=csv&format=xml&match[]=x', 400, 'format must be one of json, csv, parquet, arrow'),
    ('/api/v1/samples?match[]=x&agg=avg', 400, 'agg needs step'),
    ('/api/v1/samples?match[]=x&step=1m&agg=median', 400, 'agg must be one of last, first, avg, min, max, sum, count'),
    ('/api/v1/metrics?start=bad', 400, "bad time 'bad': use unix seconds, RFC3339, now or -1h"),
    ('/nope?start=bad', 404, 'no route /nope'),                    # routing comes first
    ('/api/v1/samples?format=xml', 400, 'format must be one of json, csv, parquet, arrow'),  # then format
    ('/api/v1/samples?start=bad', 400, "bad time 'bad': use unix seconds, RFC3339, now or -1h"),  # then times
])
def test_validation(server, target, status, msg):
    assert error(server.raw(target)) == (status, {'status': 'error', 'error': msg})


def test_last_repeated_parameter_wins(server):
    assert server.raw('/api/v1/metrics?format=csv&format=json').ctype == 'application/json'


def test_vm_400_passes_through(server):
    r = server.get('/api/v1/samples', ('match[]', '{bad'), start=T0, end=T0 + 60)
    status, body = error(r)
    assert status == 400
    assert body['error'].startswith('victoriametrics: cannot parse matches[]={bad: ')


@pytest.mark.parametrize('path', ['/api/v1/samples', '/api/v1/latest', '/api/v1/metrics'])
@pytest.mark.parametrize('code', [500, 503])
def test_vm_5xx_is_502(server, vm, path, code):
    vm.fail = (code, 'storage on fire\n')
    r = server.get(path, ('match[]', 'demo_metric'), start=T0, end=T0 + 60)
    assert error(r) == (502, {'status': 'error', 'error': 'victoriametrics: storage on fire'})


@pytest.mark.parametrize('code, status', [(422, 400), (413, 400), (404, 502), (429, 502)])
def test_vm_4xx(server, vm, code, status):
    # VM rejecting the query is the client's fault; a wrong --vm path or rate limit is not
    vm.fail = (code, 'nope')
    r = server.get('/api/v1/samples', ('match[]', 'demo_metric'), start=T0, end=T0 + 60)
    assert error(r) == (status, {'status': 'error', 'error': 'victoriametrics: nope'})


@pytest.mark.parametrize('raw', [
    '%F7%BF%BF%BF',       # past U+10FFFF
    '%ED%A0%80',          # surrogate
    '%C0%AF',             # overlong
    '%E0%A0',             # truncated
    '%E2%82%AC%80x',      # valid, then a stray continuation byte
    '%F0%9F%98%80',       # valid 4-byte
])
def test_bad_utf8_in_params_decodes_like_python(server, raw):
    want = urllib.parse.unquote(raw, errors='replace')
    r = server.raw(f'/api/v1/samples?match[]=x&start={raw}')
    assert error(r) == (400, {'status': 'error', 'error': f'bad time {want!r}: use unix seconds, RFC3339, now or -1h'})


@pytest.mark.parametrize('fmt', ['json', 'csv', 'parquet', 'arrow'])
def test_vm_errors_in_every_format(server, vm, fmt):
    vm.fail = (500, 'storage on fire')
    r = server.get('/api/v1/samples', ('match[]', 'demo_metric'), start=T0, end=T0 + 60, format=fmt)
    assert error(r)[0] == 502


def test_vm_unreachable_is_500(server, spawn):
    srv = spawn('http://127.0.0.1:9')  # the discard port: nothing listens
    status, body = error(srv.get('/api/v1/latest', ('match[]', 'x'), start=T0, end=T0 + 60))
    assert status == 500
    assert 'Could not connect' in body['error']
