"""/api/v1/latest: newest sample per series (the v2big.sql query), JSON format."""
from data import END, T0, T0MS, at


def latest(server, *match, **params):
    params = {'start': T0, 'end': END} | params
    r = server.get('/api/v1/latest', *[('match[]', m) for m in match], **params)
    assert r.status == 200, r
    return r.json()


def test_envelope(server):
    r = latest(server, 'demo_metric')
    assert r == {'status': 'success', 'start': T0MS, 'end': END * 1000, 'data': [
        {'name': 'demo_metric', 'labels': {'env': 'test'}, 'timestamp': at(290), 'value': 29.5}]}


def test_v2big_selectors(server):
    r = latest(server, 'demo_metric', 'go_goroutines', '{__name__=~".*_total"}')
    assert [(d['name'], d['labels'], d['timestamp'], d['value']) for d in r['data']] == [
        ('demo_metric', {'env': 'test'}, at(290), 29.5),
        ('go_goroutines', {'job': 'a', 'instance': 'a:1'}, at(285), 119.0),
        ('go_goroutines', {'job': 'b', 'instance': 'b:1'}, at(285), None),   # staleness marker
        ('http_requests_total', {'job': 'a', 'path': '/x'}, at(270), 90.0),
        ('http_requests_total', {'job': 'a', 'path': '/y'}, at(270), 45.0),
    ]


def test_end_picks_an_earlier_sample(server):
    d = latest(server, 'demo_metric', end=T0 + 105)['data'][0]
    assert (d['timestamp'], d['value']) == (at(100), 10.5)


def test_label_matchers(server):
    r = latest(server, '{job="b"}', 'http_requests_total{path!="/x"}')
    assert [(d['name'], d['labels'].get('job'), d['labels'].get('path')) for d in r['data']] == [
        ('go_goroutines', 'b', None), ('http_requests_total', 'a', '/y')]


def test_no_match_is_empty(server):
    assert latest(server, 'nothing_here')['data'] == []
