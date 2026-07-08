"""YCSB throughput benchmark against an already-running candidate server.

Requires: Java 8+. Uses the bundled YCSB 0.17.0 Redis binding at ./ycsb/.

Reports steady-state throughput so the number reflects the server, not JVM/JIT
warmup: one discarded warmup run primes the JVM/JIT/connections, then several
fixed-duration (`maxexecutiontime`) runs are taken and their median reported.
A fixed window keeps the sample comparable as the server speeds up across rounds,
and the reported coefficient of variation flags when a single run is noisy.

Machine-readable outputs (no LLM eyeballing):
  - stdout ends with `PERF_METRIC: <median_throughput> ops/sec`
  - `--output-json PATH` writes the same metrics as JSON for the profiler

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

# Metric key YCSB emits for overall throughput; the headline number.
THROUGHPUT_KEY = "OVERALL.Throughput(ops/sec)"

# Huge op-count cap so a run ends on maxexecutiontime, not on ops exhausted.
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


def _parse_metrics(output):
    """Turn YCSB's `[GROUP], metric, value` CSV lines into {'GROUP.metric': value}."""
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
    """p99 latency in ms for an operation, or None if it wasn't exercised."""
    if not run.get(f"{op}.Operations", 0):
        return None
    return run.get(f"{op}.99thPercentileLatency(us)", 0.0) / 1000


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

    workload_path = WORKLOADS[args.workload]
    _run_ycsb("load", workload_path, args.port, args.num_keys, args.threads)

    # Discarded warmup: primes JVM/JIT/connections/page-cache so measured runs
    # reflect steady state rather than a cold-start transient.
    if not args.no_warmup:
        _run_ycsb("run", workload_path, args.port, args.num_keys, args.threads, duration=min(args.duration, 3))

    runs = [
        _parse_metrics(_run_ycsb("run", workload_path, args.port, args.num_keys, args.threads, duration=args.duration))
        for _ in range(max(1, args.repeats))
    ]

    throughputs = [run.get(THROUGHPUT_KEY, 0.0) for run in runs]
    median_throughput = statistics.median(throughputs)
    cov_pct = (statistics.pstdev(throughputs) / median_throughput * 100) if median_throughput and len(throughputs) > 1 else 0.0
    # Report latencies from the run whose throughput is closest to the median.
    median_run = min(runs, key=lambda run: abs(run.get(THROUGHPUT_KEY, 0.0) - median_throughput))

    print(f"\n{'=' * 56}")
    print(f"  YCSB Workload {args.workload.upper()} — {args.threads} client thread"
          f"{'s' if args.threads != 1 else ''}, {args.duration}s x {args.repeats} runs")
    print(f"{'=' * 56}")
    print(f"Throughput (median): {median_throughput:.1f} ops/sec   (CoV {cov_pct:.1f}% over {len(throughputs)} runs: "
          f"{', '.join(f'{t:.0f}' for t in throughputs)})")
    for op in ["READ", "UPDATE", "INSERT"]:
        if median_run.get(f"{op}.Operations", 0):
            print(f"{op:7s} p99: {median_run.get(f'{op}.99thPercentileLatency(us)', 0) / 1000:.3f} ms   "
                  f"p95: {median_run.get(f'{op}.95thPercentileLatency(us)', 0) / 1000:.3f} ms")

    if args.output_json:
        args.output_json.write_text(json.dumps({
            "throughput_ops_per_sec": round(median_throughput, 1),
            "cov_pct": round(cov_pct, 1),
            "read_p99_ms": _p99_ms(median_run, "READ"),
            "update_p99_ms": _p99_ms(median_run, "UPDATE"),
            "threads": args.threads,
            "workload": args.workload,
            "runs_ops_per_sec": [round(t, 1) for t in throughputs],
        }, indent=2))

    # Machine-readable headline (parse this line verbatim; do not eyeball the text above).
    print(f"\nPERF_METRIC: {median_throughput:.1f} ops/sec")
    print(f"PERF_COV: {cov_pct:.1f}%")


if __name__ == "__main__":
    main()
