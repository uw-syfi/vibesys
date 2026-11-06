"""kv-store with its workload in Go: bench.py's task, the requests and checks from generator/.

The same task as bench.py (dataset, system, benchmark, metrics, validity);
only the workload is another program: the Go generator in `generator/`
(built on the Go SDK, `go/workload`), which writes the same request
stream as bench.py's WORKLOAD and checks the responses with the same rules.
The task image builds it next to this script:

    go build -o kv-generator ./generator

and the phase pod runs `kv-generator generate ...` under loadgen and
`kv-generator check ...` after it. The dataset reaches the generator as the
dataset file, the JSON of bench.py's Data.
"""

from __future__ import annotations

import bench

import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms

if __name__ == "__main__":
    ms.main(
        dataset=bench.Data,
        system=bench.SYSTEM,
        workload=ms.Generator(["./kv-generator"]),  # relative to this script's directory
        benchmark=bench.benchmark,
        metrics={
            "latency_ms.p50": vse.Metric(unit="ms", direction="min"),
            "latency_ms.p99": vse.Metric(unit="ms", direction="min"),
            "operations_per_second": vse.Metric(unit="ops/s", direction="max"),
        },
        validity=vse.Validity(max_error_rate=0.001, min_sustained_ratio=0.99),
    )
