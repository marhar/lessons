"""/api/v1/metrics, and the requests each endpoint sends to VictoriaMetrics."""
from data import NAMES, T0


def test_all_names(server):
    r = server.get('/api/v1/metrics')
    assert (r.status, r.json()) == (200, {'status': 'success', 'data': NAMES})


def test_match(server):
    r = server.get('/api/v1/metrics', ('match[]', '{job="a"}'))
    assert r.json()['data'] == ['go_goroutines', 'http_requests_total']


def test_csv(server):
    r = server.get('/api/v1/metrics', format='csv')
    assert (r.status, r.ctype) == (200, 'text/csv')
    assert r.text.splitlines() == ['name'] + NAMES


def test_no_range_leaves_vm_default(server, vm):
    server.get('/api/v1/metrics')
    assert vm.last_query('/api/v1/label/__name__/values') == []


def test_range_is_passed_as_seconds(server, vm):
    server.get('/api/v1/metrics', ('match[]', 'demo_metric'), start=T0, end='2026-10-30T00:05:00.5Z')
    assert vm.last_query('/api/v1/label/__name__/values') == [
        ('match[]', 'demo_metric'), ('start', '1793318400.000'), ('end', '1793318700.500')]


def test_relative_range_shares_one_now(server, vm):
    server.get('/api/v1/metrics', start='-1h', end='now')
    q = dict(vm.last_query('/api/v1/label/__name__/values'))
    assert round((float(q['end']) - float(q['start'])) * 1000) == 3_600_000


def test_range_must_be_forward(server):
    for start, end in [(T0 + 60, T0), (T0, T0), ('now', '-1h')]:
        r = server.get('/api/v1/metrics', start=start, end=end)
        assert (r.status, r.json()) == (400, {'status': 'error', 'error': 'start must be before end'})


def test_export_request(server, vm):
    server.get('/api/v1/samples', ('match[]', 'demo_metric'), ('match[]', '{__name__=~".*_total"}'),
               start=T0, end=T0 + 60.25)
    assert vm.last_query('/api/v1/export') == [
        ('match[]', 'demo_metric'), ('match[]', '{__name__=~".*_total"}'),
        ('start', '1793318400.000'), ('end', '1793318460.250')]


def test_export_request_is_fully_encoded(server, vm):
    # DuckDB rejects a raw * in an HTTP path, so everything but [A-Za-z0-9_.~-] is escaped
    server.get('/api/v1/latest', ('match[]', '{__name__=~".*_total"}'), start=T0, end=T0 + 60)
    path = [p for p in vm.requests if p.startswith('/api/v1/export')][-1]
    assert path.startswith('/api/v1/export?match%5B%5D=%7B__name__%3D~%22.%2A_total%22%7D&start=')
