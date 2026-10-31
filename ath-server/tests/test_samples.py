"""/api/v1/samples: raw samples and step buckets, JSON format."""
import pytest

from data import DEMO, END, GO_A, GO_B, LINES, T0, T0MS, WEIRD, WEIRD_VALUES, at, labels


def samples(server, *match, **params):
    params = {'start': T0, 'end': END} | params
    r = server.get('/api/v1/samples', *[('match[]', m) for m in match], **params)
    assert r.status == 200, r
    return r.json()


def series(r, metric):
    """The one series in a response with these name and labels."""
    found = [s for s in r['data'] if s['name'] == metric['__name__'] and s['labels'] == labels(metric)]
    assert len(found) == 1, r['data']
    return found[0]


def test_envelope(server):
    r = samples(server, 'demo_metric')
    assert list(r) == ['status', 'start', 'end', 'step', 'data']
    assert (r['status'], r['start'], r['end'], r['step']) == ('success', T0MS, END * 1000, None)
    assert list(r['data'][0]) == ['name', 'labels', 'timestamps', 'values']


def test_raw_merges_a_series_split_over_lines(server):
    s = series(samples(server, 'demo_metric'), DEMO)
    assert s['timestamps'] == [at(10 * i) for i in range(30)]
    assert s['values'] == [i + 0.5 for i in range(30)]


def test_name_is_not_a_label(server):
    s = samples(server, 'demo_metric')['data'][0]
    assert (s['name'], s['labels']) == ('demo_metric', {'env': 'test'})


def test_staleness_marker_is_null(server):
    assert series(samples(server, 'go_goroutines'), GO_B)['values'][-1] is None


def test_series_order_is_name_then_labels(server):
    r = samples(server, 'go_goroutines', 'demo_metric', '{__name__=~"http.*"}')
    assert [(s['name'], s['labels'].get('job'), s['labels'].get('path')) for s in r['data']] == [
        ('demo_metric', None, None),
        ('go_goroutines', 'a', None), ('go_goroutines', 'b', None),
        ('http_requests_total', 'a', '/x'), ('http_requests_total', 'a', '/y'),
    ]


def test_window_trims_samples(server):
    s = series(samples(server, 'demo_metric', start=T0 + 15, end=T0 + 45), DEMO)
    assert s['timestamps'] == [at(20), at(30), at(40)]


def test_no_match_is_empty(server):
    assert samples(server, 'nothing_here')['data'] == []


def test_values_keep_full_precision(server):
    assert series(samples(server, 'weird_metric'), WEIRD)['values'] == WEIRD_VALUES


# demo_metric is i + 0.5 every 10s, so each 1m bucket holds six samples 6k+0.5 .. 6k+5.5
BUCKETS = {
    'last': [5.5, 11.5, 17.5, 23.5, 29.5],
    'first': [0.5, 6.5, 12.5, 18.5, 24.5],
    'avg': [3.0, 9.0, 15.0, 21.0, 27.0],
    'min': [0.5, 6.5, 12.5, 18.5, 24.5],
    'max': [5.5, 11.5, 17.5, 23.5, 29.5],
    'sum': [18.0, 54.0, 90.0, 126.0, 162.0],
    'count': [6.0, 6.0, 6.0, 6.0, 6.0],
}


@pytest.mark.parametrize('agg', BUCKETS)
def test_step_aggregates(server, agg):
    r = samples(server, 'demo_metric', step='1m', agg=agg)
    s = series(r, DEMO)
    assert r['step'] == 60_000
    assert s['timestamps'] == [at(60 * k) for k in range(5)]
    assert s['values'] == BUCKETS[agg]


def test_step_defaults_to_last(server):
    assert series(samples(server, 'demo_metric', step='1m'), DEMO)['values'] == BUCKETS['last']


def test_buckets_align_to_start(server):
    # start 5s after T0: the T0 sample is outside, buckets are [T0+5s, T0+65s), ...
    s = series(samples(server, 'demo_metric', start=T0 + 5, step='1m', agg='avg'), DEMO)
    assert s['timestamps'][:2] == [at(5), at(65)]
    assert s['values'][:2] == [4.0, 10.0]


def test_step_per_series(server):
    r = samples(server, 'go_goroutines', step='1m', agg='max')
    assert series(r, GO_A)['values'] == [103.0, 107.0, 111.0, 115.0, 119.0]
    # job b's last bucket ends in a staleness marker; max ignores it
    assert series(r, GO_B)['values'] == [200.0, 196.0, 192.0, 188.0, 184.0]


def test_count_skips_staleness_markers(server):
    r = samples(server, 'go_goroutines', step='5m', agg='count')
    assert series(r, GO_A)['values'] == [20.0]
    assert series(r, GO_B)['values'] == [19.0]


def test_uneven_step(server):
    s = series(samples(server, 'demo_metric', step='90s', agg='count'), DEMO)
    assert s['timestamps'] == [at(0), at(90), at(180), at(270)]
    assert s['values'] == [9.0, 9.0, 9.0, 3.0]


def test_every_line_is_used(server):
    r = samples(server, '{__name__=~".+"}')
    assert sum(len(s['values']) for s in r['data']) == sum(len(l['values']) for l in LINES)
