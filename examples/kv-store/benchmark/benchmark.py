"""YCSB benchmark: runs against an already-running candidate server.

Requires: Java 8+, YCSB at ./ycsb/ relative to this script.

The headline metric is throughput under concurrent load, measured in
**steady state** so it reflects the server, not JVM/JIT warmup: a discarded
warmup run primes the JVM/JIT/connections, then several **fixed-duration**
(`maxexecutiontime`) runs are taken and the **median** is reported with its
coefficient of variation, so the window stays constant as the server speeds up
across rounds and a single noisy sample can't mislead.

Two machine-readable outputs (no LLM eyeballing):
  - stdout ends with `PERF_METRIC: <median_throughput> ops/sec`,
  - `--output-json PATH` writes the same metrics as JSON for the profiler.

Usage:
    python benchmark.py --port 6380
    python benchmark.py --port 6380 --threads 1          # single-client latency probe
    python benchmark.py --port 6380 --duration 8 --repeats 5
    python benchmark.py --port 6380 --output-json /tmp/bench.json
"""

import argparse
import json
import statistics
import subprocess
import sys
from pathlib import Path

YCSB_HOME = Path(__file__).resolve().parent / "ycsb"

WORKLOADS = {"a": "workloads/workloada", "b": "workloads/workloadb", "c": "workloads/workloadc"}

# Large op-count cap so a run is bounded by time (maxexecutiontime), not ops.
_OP_CAP = 1_000_000_000


def _run_ycsb(phase, workload, port, num_keys, threads, *, duration=None):
    props = [
        "-p", "redis.host=127.0.0.1", "-p", f"redis.port={port}",
        "-p", f"recordcount={num_keys}",
        "-p", f"operationcount={_OP_CAP}",
        "-p", f"threadcount={threads}",
    ]
    if duration is not None and phase == "run":
        props += ["-p", f"maxexecutiontime={duration}"]
    result = subprocess.run(
        [str(YCSB_HOME / "bin" / "ycsb.sh"), phase, "redis", "-s",
         "-P", str(YCSB_HOME / workload), *props],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        print(f"YCSB {phase} failed:\n{result.stderr[-2000:]}")
        sys.exit(1)
    return result.stdout


def _parse(output):
    metrics = {}
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 3 and parts[0].startswith("["):
            try:
                metrics[f"{parts[0].strip('[]')}.{parts[1]}"] = float(parts[2])
            except ValueError:
                pass
    return metrics


def _p99_ms(run, op):
    return run.get(f"{op}.99thPercentileLatency(us)", 0.0) / 1000 if run.get(f"{op}.Operations", 0) else None


def main():
    parser = argparse.ArgumentParser(description="KV store benchmark (YCSB)")
    parser.add_argument("--port", type=int, required=True, help="Port of the candidate server (must be already running)")
    parser.add_argument("--workload", choices=list(WORKLOADS.keys()), default="a")
    parser.add_argument("--num-keys", type=int, default=20000, help="recordcount loaded into the store")
    parser.add_argument("--threads", type=int, default=16,
                        help="Concurrent YCSB client threads (headline load). "
                             "Pass --threads 1 for the single-client latency probe.")
    parser.add_argument("--duration", type=int, default=5, help="Seconds per measured run (fixed-duration, steady-state).")
    parser.add_argument("--repeats", type=int, default=3, help="Measured runs; the median is the headline.")
    parser.add_argument("--no-warmup", action="store_true", help="Skip the discarded warmup run.")
    parser.add_argument("--output-json", type=Path, default=None, help="Write the headline metrics to this path as JSON.")
    args = parser.parse_args()

    wl = WORKLOADS[args.workload]
    _run_ycsb("load", wl, args.port, args.num_keys, args.threads)

    # Warmup run (discarded): primes JVM/JIT/connections/page-cache so the
    # measured runs reflect steady state rather than a cold-start transient.
    if not args.no_warmup:
        _run_ycsb("run", wl, args.port, args.num_keys, args.threads, duration=min(args.duration, 3))

    runs = [
        _parse(_run_ycsb("run", wl, args.port, args.num_keys, args.threads, duration=args.duration))
        for _ in range(max(1, args.repeats))
    ]

    thr = [m.get("OVERALL.Throughput(ops/sec)", 0.0) for m in runs]
    median_thr = statistics.median(thr)
    cov = (statistics.pstdev(thr) / median_thr * 100) if median_thr and len(thr) > 1 else 0.0
    # Report latencies from the run closest to the median throughput.
    med_run = min(runs, key=lambda m: abs(m.get("OVERALL.Throughput(ops/sec)", 0.0) - median_thr))

    print(f"\n{'=' * 56}")
    print(f"  YCSB Workload {args.workload.upper()} — {args.threads} client thread"
          f"{'s' if args.threads != 1 else ''}, {args.duration}s x {args.repeats} runs")
    print(f"{'=' * 56}")
    print(f"Throughput (median): {median_thr:.1f} ops/sec   (CoV {cov:.1f}% over {len(thr)} runs: "
          f"{', '.join(f'{t:.0f}' for t in thr)})")
    for op in ["READ", "UPDATE", "INSERT"]:
        if med_run.get(f"{op}.Operations", 0):
            print(f"{op:7s} p99: {med_run.get(f'{op}.99thPercentileLatency(us)', 0) / 1000:.3f} ms   "
                  f"p95: {med_run.get(f'{op}.95thPercentileLatency(us)', 0) / 1000:.3f} ms")

    if args.output_json:
        args.output_json.write_text(json.dumps({
            "throughput_ops_per_sec": round(median_thr, 1),
            "cov_pct": round(cov, 1),
            "read_p99_ms": _p99_ms(med_run, "READ"),
            "update_p99_ms": _p99_ms(med_run, "UPDATE"),
            "threads": args.threads,
            "workload": args.workload,
            "runs_ops_per_sec": [round(t, 1) for t in thr],
        }, indent=2))

    # Machine-readable headline (parse this line verbatim; do not eyeball the text above).
    print(f"\nPERF_METRIC: {median_thr:.1f} ops/sec")
    print(f"PERF_COV: {cov:.1f}%")


if __name__ == "__main__":
    main()
