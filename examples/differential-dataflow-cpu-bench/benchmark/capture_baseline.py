"""Capture the round-0 CPU baseline for the differential-dataflow `bfs` target.

The baseline here is NOT an external engine — it is **round 0 of this very
target**: the vanilla vendored differential-dataflow source in `ref_engine/`,
unmodified. This script builds that pristine engine offline, times it on the
fixed metric workload exactly as `benchmark.py` will time every candidate, and
writes `baseline.json`. Because every later round starts from this same source
and only micro-optimizes it, `cpu_reduction_ratio = baseline / candidate` is a
same-code, same-guarantees number: round 0 measures ≈ 1.0, and any improvement
is purely cycles the agent shaved.

`baseline.json` is machine-specific (absolute CPU-seconds depend on this box's
CPU) and is captured once, offline against the warm ~/.cargo cache. Re-run it if
the vendored `ref_engine/` source or the host changes.

Usage (run once, from the example dir or anywhere):
  python3 bench/capture_baseline.py
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXAMPLE = os.path.normpath(os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_EXAMPLE, "reference"))
import workload  # noqa: E402

sys.path.insert(0, _HERE)
import benchmark  # noqa: E402  (measure_cpu, BASELINE_JSON)

_REF_ENGINE = os.path.join(_EXAMPLE, "ref_engine")
_REF_MANIFEST = os.path.join(_REF_ENGINE, "Cargo.toml")
_REF_BIN = os.path.join(_REF_ENGINE, workload.BFS_BINARY_RELPATH)


def main():
    print("capturing round-0 (vanilla ref_engine) CPU baseline")
    print(f"  ref_engine : {_REF_ENGINE}")
    if not os.path.isdir(_REF_ENGINE):
        print(f"ERROR: vendored ref_engine not found at {_REF_ENGINE}", file=sys.stderr)
        return 2

    print("  building (offline)...")
    build = subprocess.run(workload.build_cmd(_REF_MANIFEST), capture_output=True, text=True)
    if build.returncode != 0:
        print(f"ERROR: build failed:\n{(build.stderr or build.stdout)[-800:]}", file=sys.stderr)
        return 2

    wl = workload.METRIC_WORKLOAD
    print(f"  timing on metric workload: {' '.join(wl)}")
    cpu_seconds, samples = benchmark.measure_cpu(_REF_BIN, wl)

    data = {
        "baseline_cpu_seconds": round(cpu_seconds, 6),
        "workload": wl,
        "samples": [round(s, 6) for s in samples],
        "reps": benchmark.DEFAULT_REPS,
        "warmups": benchmark.DEFAULT_WARMUPS,
        "engine": "differential-dataflow (vanilla ref_engine, round 0)",
        "host": platform.node(),
        "cpu": platform.processor() or platform.machine(),
    }
    with open(benchmark.BASELINE_JSON, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  baseline_cpu_seconds = {data['baseline_cpu_seconds']}  (samples {data['samples']})")
    print(f"  wrote {benchmark.BASELINE_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
