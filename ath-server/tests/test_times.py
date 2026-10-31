"""start, end and step parsing, checked through the resolved values each JSON response echoes."""
import time

import pytest

from data import T0, T0MS

FAR = 4_000_000_000  # an end time after every start below


def resolved(server, **params):
    r = server.get('/api/v1/samples', ('match[]', 'none'), **params)
    assert r.status == 200, r
    return r.json()


@pytest.mark.parametrize('start, ms', [
    ('1793318400', T0MS),
    ('1793318400.5', T0MS + 500),
    ('1793318400.123456', T0MS + 123),
    (' 1793318400 ', T0MS),
    ('1_793_318_400', T0MS),
    ('1.7933184e9', T0MS),
    ('2026-10-30T00:00:00Z', T0MS),
    ('2026-10-30T00:00:00', T0MS),             # no offset: UTC
    ('2026-10-30', T0MS),
    ('2026-10-30 00:00:00', T0MS),
    ('2026-10-30T00:00', T0MS),
    ('20261030T000000', T0MS),
    ('2026-10-30T02:00:00+02:00', T0MS),
    ('2026-10-29T19:00:00-05:00', T0MS),
    ('2026-10-30T05:30:00+0530', T0MS),
    ('2026-10-30T00:00:00.250Z', T0MS + 250),
    ('2026-10-30T00:00:00.123456789Z', T0MS + 123),
])
def test_absolute_start(server, start, ms):
    assert resolved(server, start=start, end=FAR)['start'] == ms


def test_absolute_end(server):
    r = resolved(server, start=T0, end='2026-10-30T01:00:00Z')
    assert (r['start'], r['end']) == (T0MS, T0MS + 3_600_000)


@pytest.mark.parametrize('start, ms', [
    ('-1h', 3_600_000),
    ('now-1h', 3_600_000),
    ('-1h30m', 5_400_000),
    ('-1.5h', 5_400_000),
    ('-90', 90_000),
    ('-90s', 90_000),
    ('-500ms', 500),
    ('-1d12h', 129_600_000),
    ('-1w', 604_800_000),
    ('-1y', 31_536_000_000),
])
def test_relative_start(server, start, ms):
    # start and end share one 'now', so the gap is exact
    r = resolved(server, start=start, end='now')
    assert r['end'] - r['start'] == ms


def test_defaults_are_the_last_hour(server):
    before = int(time.time() * 1000)
    r = resolved(server)
    after = int(time.time() * 1000)
    assert before - 5 <= r['end'] <= after + 5
    assert r['end'] - r['start'] == 3_600_000


def test_default_start_follows_end(server):
    r = resolved(server, end=T0)
    assert (r['start'], r['end']) == (T0MS - 3_600_000, T0MS)


@pytest.mark.parametrize('step, ms', [
    ('30s', 30_000), ('1m', 60_000), ('1h30m', 5_400_000), ('90', 90_000), ('1.5m', 90_000), ('250ms', 250),
])
def test_step(server, step, ms):
    assert resolved(server, start=T0, end=T0 + 60, step=step)['step'] == ms


def test_no_step_is_null(server):
    assert resolved(server, start=T0, end=T0 + 60)['step'] is None


def bad_time(s):
    return f"bad time {s!r}: use unix seconds, RFC3339, now or -1h"


def bad_duration(s):
    return f"bad duration {s!r}: use e.g. 30s, 5m, 1h30m"


@pytest.mark.parametrize('params, error', [
    ({'start': 'yesterday'}, bad_time('yesterday')),
    ({'start': 'nan'}, bad_time('nan')),
    ({'start': 'inf'}, bad_time('inf')),
    ({'start': '2026-02-30'}, bad_time('2026-02-30')),
    ({'start': '2026-1030'}, bad_time('2026-1030')),
    ({'start': '0x10'}, bad_time('0x10')),
    ({'start': "it's"}, bad_time("it's")),             # repr switches to double quotes
    ({'start': 'été\n'}, bad_time('été\n')),
    ({'start': '-1x'}, bad_duration('1x')),
    ({'start': 'now-'}, bad_duration('')),
    ({'start': '-5m2'}, bad_duration('5m2')),
    ({'start': '-' + '9' * 400 + 's'}, bad_duration('9' * 400 + 's')),
    ({'end': 'bad', 'start': 'bad2'}, bad_time('bad')),  # end is parsed first
    ({'start': T0 + 60, 'end': T0}, 'start must be before end'),
    ({'start': T0, 'end': T0}, 'start must be before end'),
    ({'start': 'now', 'end': '-1h'}, 'start must be before end'),
    ({'step': '0'}, 'step must be positive'),
    ({'step': '-5'}, 'step must be positive'),
    ({'step': '1q'}, bad_duration('1q')),
    ({'step': 'inf'}, bad_duration('inf')),
])
def test_bad_times(server, params, error):
    r = server.get('/api/v1/samples', ('match[]', 'none'), **params)
    assert (r.status, r.json()) == (400, {'status': 'error', 'error': error})


def test_points_cap_is_inclusive(server):
    ok = server.get('/api/v1/samples', ('match[]', 'none'), start=T0, end=T0 + 11_000, step='1s')
    over = server.get('/api/v1/samples', ('match[]', 'none'), start=T0, end=T0 + 11_001, step='1s')
    assert ok.status == 200
    assert over.json()['error'] == 'more than 11000 points per series: raise step or shorten the range'
