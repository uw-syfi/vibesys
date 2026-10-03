"""Benchmark (result protocol 2): calls of count_primes(3000) per second.

It runs for about two seconds. Fewer than two finished calls in that window
is a failed warmup, reported as an ``error`` record with a partial measurement.
"""

import json
import pathlib
import sys
import time

from primes import count_primes

LIMIT = 3000
WINDOW_S = 2.0
REQUIRED_CALLS = 2

output = pathlib.Path(sys.argv[sys.argv.index("--vs-output") + 1])
hello = {"kind": "hello", "protocol": 2, "metrics": {"calls_per_s": {"direction": "max"}}}
start = time.perf_counter()
calls = 0
# test-isolation: this is the input task's own benchmark, which measures wall time by design.
while time.perf_counter() - start < WINDOW_S:
    count_primes(LIMIT)
    calls += 1
rate = calls / (time.perf_counter() - start)
if calls >= REQUIRED_CALLS:
    outcome = {"kind": "result", "values": {"calls_per_s": rate}}
else:
    outcome = {
        "kind": "error",
        "message": f"warmup stopped: {calls}/{REQUIRED_CALLS} calls in {WINDOW_S} s",
        "partial": {
            "name": "warmup_calls_per_s",
            "value": rate,
            "direction": "max",
            "unit": "calls/s",
            "target": REQUIRED_CALLS / WINDOW_S,
            "progress": {"completed": calls, "required": REQUIRED_CALLS, "unit": "calls"},
        },
    }
output.write_text("".join(json.dumps(record) + "\n" for record in (hello, outcome)))
sys.exit(0 if calls >= REQUIRED_CALLS else 1)
