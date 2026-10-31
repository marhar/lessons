"""
A stand-in for VictoriaMetrics: the two endpoints ath-server reads, fixed data.

Mimics what a real VictoriaMetrics sends:
  /api/v1/export                  JSON lines, ints written without '.0', staleness as null,
                                  inclusive start/end, a series may span several lines
  /api/v1/label/__name__/values   {"status":"success","data":[sorted names]}
  bad selector                    400, text/plain 'cannot parse matches[]=...'
  HEAD                            200 with no Content-Length

Selectors support name, name{...} and {...} with = != =~ !~ (regexes fully
anchored, a missing label matches ""), which is all the tests need.

Each request's path is appended to .requests, so tests can check what
ath-server asked for. Set .fail = (status, body) to make every request fail.
"""
import json
import math
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SELECTOR_RE = re.compile(r'([a-zA-Z_:][a-zA-Z0-9_:]*)?(?:\{(.*)\})?$', re.S)
MATCHER_RE = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*(?:,|$)')


def parse_selector(sel):
    """'name{k="v",...}' -> [(label, op, value)]; ValueError if malformed."""
    m = SELECTOR_RE.match(sel)
    if not m or not (m[1] or m[2] is not None):
        raise ValueError(f'cannot parse matches[]={sel}: unexpected token')
    matchers = [('__name__', '=', m[1])] if m[1] else []
    body = m[2] or ''
    pos = 0
    while pos < len(body):
        mm = MATCHER_RE.match(body, pos)
        if not mm:
            raise ValueError(f'cannot parse matches[]={sel}: labelFilterExpr: unexpected token')
        matchers.append((mm[1], mm[2], json.loads(f'"{mm[3]}"')))
        pos = mm.end()
    return matchers


def matches(metric, matchers):
    for label, op, want in matchers:
        have = metric.get(label, '')
        ok = {'=': have == want, '!=': have != want,
              '=~': re.fullmatch(want, have) is not None,
              '!~': re.fullmatch(want, have) is None}[op]
        if not ok:
            return False
    return True


def vm_number(v):
    # VictoriaMetrics writes 72, not 72.0, and staleness markers as null
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return 'null'
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


class FakeVM:
    def __init__(self, lines):
        self.default = lines
        self.reset()
        self.srv = ThreadingHTTPServer(('127.0.0.1', 0), self.handler())
        self.url = f'http://127.0.0.1:{self.srv.server_port}'
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def reset(self):
        self.lines = list(self.default)  # export lines: {"metric", "timestamps", "values"}
        self.requests = []
        self.fail = None

    def close(self):
        self.srv.shutdown()

    def last_query(self, path):
        """Query of the most recent request to path, as a list of (key, value)."""
        for p in reversed(self.requests):
            url = urllib.parse.urlsplit(p)
            if url.path == path:
                return urllib.parse.parse_qsl(url.query)
        raise AssertionError(f'no request to {path}: {self.requests}')

    def select(self, params):
        """Export lines matching any match[], trimmed to [start, end]."""
        sels = [parse_selector(s) for s in params.get('match[]', [])]
        start = float(params['start'][-1]) * 1000 if 'start' in params else -math.inf
        end = float(params['end'][-1]) * 1000 if 'end' in params else math.inf
        out = []
        for line in self.lines:
            if sels and not any(matches(line['metric'], s) for s in sels):
                continue
            keep = [(t, v) for t, v in zip(line['timestamps'], line['values']) if start <= t <= end]
            if keep:
                out.append({'metric': line['metric'], 'timestamps': [t for t, _ in keep],
                            'values': [v for _, v in keep]})
        return out

    def respond(self, path, params):
        if self.fail:
            return self.fail[0], 'text/plain; charset=utf-8', self.fail[1]
        try:
            if path == '/api/v1/export':
                body = ''.join(
                    '{"metric":%s,"values":[%s],"timestamps":[%s]}\n' % (
                        json.dumps(l['metric'], separators=(',', ':'), ensure_ascii=False),
                        ','.join(vm_number(v) for v in l['values']),
                        ','.join(str(t) for t in l['timestamps']))
                    for l in self.select(params))
                return 200, 'application/stream+json; charset=utf-8', body
            if path == '/api/v1/label/__name__/values':
                names = sorted({l['metric']['__name__'] for l in self.select(params)})
                return 200, 'application/json', json.dumps({'status': 'success', 'data': names})
        except ValueError as e:
            return 400, 'text/plain; charset=utf-8', str(e)
        return 404, 'text/plain; charset=utf-8', f'unsupported path requested: {path}'

    def handler(self):
        vm = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, head):
                vm.requests.append(self.path)
                url = urllib.parse.urlsplit(self.path)
                code, ctype, body = vm.respond(url.path, urllib.parse.parse_qs(url.query))
                data = body.encode()
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                if not head:
                    self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if not head:
                    self.wfile.write(data)

            def do_GET(self):
                self.reply(head=False)

            def do_HEAD(self):
                self.reply(head=True)

            def log_message(self, format, *args):
                pass

        return Handler
