"""
Every test that takes `server` runs once per implementation (server.py and
the C++ build), each pointed at a fake VictoriaMetrics serving tests/data.py.

  pytest                 both implementations
  pytest --impl=py       just server.py
  pytest -m live         cross-check both against a real VictoriaMetrics
"""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from data import LINES
from fake_vm import FakeVM

ROOT = Path(__file__).resolve().parent.parent
COMMANDS = {
    'py': [sys.executable, str(ROOT / 'server.py')],
    'cpp': [str(ROOT / 'cpp' / 'build' / 'ath-server')],
}


def pytest_addoption(parser):
    parser.addoption('--impl', default='py,cpp', help='implementations to test: py, cpp or py,cpp')
    parser.addoption('--live-vm', default='http://localhost:8428', help='VictoriaMetrics for -m live')


def pytest_generate_tests(metafunc):
    if 'server' in metafunc.fixturenames:
        impls = metafunc.config.getoption('impl').split(',')
        metafunc.parametrize('server', impls, indirect=True, scope='session')


class Response:
    def __init__(self, status, ctype, body):
        self.status, self.ctype, self.body = status, ctype, body

    @property
    def text(self):
        return self.body.decode()

    def json(self):
        return json.loads(self.body)

    def __repr__(self):
        return f'<{self.status} {self.ctype} {self.body[:200]!r}>'


class Server:
    """A running ath-server; get() never raises on HTTP errors."""

    def __init__(self, impl, vm_url, log):
        if not Path(COMMANDS[impl][-1]).exists():
            pytest.skip(f'{COMMANDS[impl][-1]} not built: run make build')
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.impl = impl
        self.url = f'http://127.0.0.1:{self.port}'
        self.log = log
        self.proc = subprocess.Popen(COMMANDS[impl] + ['--port', str(self.port), '--vm', vm_url],
                                     stdout=subprocess.DEVNULL, stderr=open(log, 'w'))
        deadline = time.time() + 30  # the C++ server installs nanoarrow on first start
        while True:
            try:
                socket.create_connection(('127.0.0.1', self.port), timeout=1).close()
                return
            except OSError:
                if self.proc.poll() is not None or time.time() > deadline:
                    self.stop()
                    raise RuntimeError(f'{impl} server did not start:\n{Path(log).read_text()}')
                time.sleep(0.1)

    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=10)

    def get(self, path, *params, **kw):
        """get('/api/v1/latest', ('match[]', 'x'), start=1) -> Response"""
        query = urllib.parse.urlencode(list(params) + list(kw.items()))
        return self.raw(path + ('?' + query if query else ''))

    def raw(self, target):
        """GET an already-encoded path and query."""
        try:
            with urllib.request.urlopen(self.url + target, timeout=30) as r:
                return Response(r.status, r.headers['Content-Type'], r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, e.headers['Content-Type'], e.read())


@pytest.fixture(scope='session')
def vm():
    fake = FakeVM(LINES)
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def reset_vm(request):
    yield
    if 'vm' in request.fixturenames:
        request.getfixturevalue('vm').reset()


@pytest.fixture(scope='session')
def server(request, vm, tmp_path_factory):
    srv = Server(request.param, vm.url, tmp_path_factory.mktemp('logs') / f'{request.param}.log')
    yield srv
    srv.stop()


@pytest.fixture
def spawn(request, tmp_path):
    """Start an extra server of the same implementation against another VM URL."""
    started = []

    def start(vm_url):
        impl = request.node.callspec.params['server']
        srv = Server(impl, vm_url, tmp_path / f'{impl}.log')
        started.append(srv)
        return srv

    yield start
    for srv in started:
        srv.stop()
