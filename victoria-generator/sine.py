#!/usr/bin/env python3
"""Push a sine wave (period 5min, range 0..100) to VictoriaMetrics."""

import math
import time
import urllib.request

URL = "http://localhost:8428/api/v1/import/prometheus"
PERIOD = 300.0   # seconds
INTERVAL = 0.25  # seconds between samples


def push(value, ts_ms):
    body = f'demo_metric{{env="test"}} {value:.3f} {ts_ms}\n'.encode()
    req = urllib.request.Request(URL, data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        resp.read()


def main():
    while True:
        now = time.time()
        value = 50 + 50 * math.sin(2 * math.pi * now / PERIOD)
        try:
            push(value, int(now * 1000))
            print(f"{value:7.3f}", flush=True)
        except OSError as e:
            print(f"push failed: {e}", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
