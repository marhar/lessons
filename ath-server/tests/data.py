"""
Fixed data served by the fake VictoriaMetrics: five minutes from T0, with
values chosen so every expected answer can be worked out by hand.

  demo_metric{env="test"}          every 10s, value i + 0.5 (i = 0..29), split over two export lines
  go_goroutines{job="a"}           every 15s, 100, 101, ... 119
  go_goroutines{job="b"}           every 15s, 200, 199, ... 182, then a staleness marker (null)
  http_requests_total{path="/x"}   every 30s, 0, 10, ... 90
  http_requests_total{path="/y"}   every 30s, 0, 5, ... 45
  weird_metric{city="Zürich"}      three awkward floats, a label value with quotes and a newline
"""
T0 = 1_793_318_400              # 2026-10-30T00:00:00Z, unix seconds
T0MS = T0 * 1000
END = T0 + 300                  # covers every sample


def at(seconds):
    return T0MS + seconds * 1000


DEMO = {'__name__': 'demo_metric', 'env': 'test'}
GO_A = {'__name__': 'go_goroutines', 'job': 'a', 'instance': 'a:1'}
GO_B = {'__name__': 'go_goroutines', 'job': 'b', 'instance': 'b:1'}
REQ_X = {'__name__': 'http_requests_total', 'job': 'a', 'path': '/x'}
REQ_Y = {'__name__': 'http_requests_total', 'job': 'a', 'path': '/y'}
WEIRD = {'__name__': 'weird_metric', 'city': 'Zürich', 'note': 'say "hi"\n'}

WEIRD_VALUES = [1e-05, 0.1 + 0.2, 1.5e16]

LINES = [
    # one series over two lines, as VictoriaMetrics may return it
    {'metric': DEMO, 'timestamps': [at(10 * i) for i in range(15)],
     'values': [i + 0.5 for i in range(15)]},
    {'metric': DEMO, 'timestamps': [at(10 * i) for i in range(15, 30)],
     'values': [i + 0.5 for i in range(15, 30)]},
    {'metric': GO_A, 'timestamps': [at(15 * j) for j in range(20)],
     'values': [100.0 + j for j in range(20)]},
    {'metric': GO_B, 'timestamps': [at(15 * j) for j in range(20)],
     'values': [200.0 - j for j in range(19)] + [None]},
    {'metric': REQ_X, 'timestamps': [at(30 * k) for k in range(10)],
     'values': [10.0 * k for k in range(10)]},
    {'metric': REQ_Y, 'timestamps': [at(30 * k) for k in range(10)],
     'values': [5.0 * k for k in range(10)]},
    {'metric': WEIRD, 'timestamps': [at(60), at(120), at(180)], 'values': WEIRD_VALUES},
]

NAMES = ['demo_metric', 'go_goroutines', 'http_requests_total', 'weird_metric']


def labels(metric):
    return {k: v for k, v in metric.items() if k != '__name__'}
