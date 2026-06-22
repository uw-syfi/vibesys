"""YCSB benchmark: runs against an already-running candidate server.

Requires: Java 8+, YCSB at ./ycsb/ relative to this script.

Usage:
    python benchmark.py --port 6380
    python benchmark.py --port 6380 --workload a --num-ops 50000
"""

import argparse
import subprocess
import sys
from pathlib import Path

YCSB_HOME = Path(__file__).resolve().parent / "ycsb"

WORKLOADS = {
    "a": "workloads/workloada",
    "b": "workloads/workloadb",
    "c": "workloads/workloadc",
}


def _run_ycsb(phase, workload, port, num_ops, num_keys, threads):
    result = subprocess.run(
        [str(YCSB_HOME / "bin" / "ycsb.sh"), phase, "redis", "-s",
         "-P", str(YCSB_HOME / workload),
         "-p", "redis.host=127.0.0.1", "-p", f"redis.port={port}",
         "-p", f"recordcount={num_keys}", "-p", f"operationcount={num_ops}",
         "-p", f"threadcount={threads}"],
        capture_output=True, text=True, timeout=300,
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


def main():
    parser = argparse.ArgumentParser(description="KV store benchmark (YCSB)")
    parser.add_argument("--port", type=int, required=True, help="Port of the candidate server (must be already running)")
    parser.add_argument("--workload", choices=list(WORKLOADS.keys()), default="a")
    parser.add_argument("--num-ops", type=int, default=10000)
    parser.add_argument("--num-keys", type=int, default=1000)
    parser.add_argument("--threads", type=int, default=1)
    args = parser.parse_args()

    _run_ycsb("load", WORKLOADS[args.workload], args.port, args.num_ops, args.num_keys, args.threads)

    output = _run_ycsb("run", WORKLOADS[args.workload], args.port, args.num_ops, args.num_keys, args.threads)
    m = _parse(output)

    print(f"\n{'=' * 50}")
    print(f"  YCSB Workload {args.workload.upper()} Results")
    print(f"{'=' * 50}")
    print(f"Throughput:   {m.get('OVERALL.Throughput(ops/sec)', 0):.1f} ops/sec")
    print(f"Runtime:      {m.get('OVERALL.RunTime(ms)', 0) / 1000:.2f}s")
    for op in ["READ", "UPDATE", "INSERT"]:
        ops = m.get(f"{op}.Operations", 0)
        if ops:
            print(f"\n{op} ({int(ops)} ops):")
            print(f"  Mean: {m.get(f'{op}.AverageLatency(us)', 0) / 1000:.3f} ms")
            print(f"  P95:  {m.get(f'{op}.95thPercentileLatency(us)', 0) / 1000:.3f} ms")
            print(f"  P99:  {m.get(f'{op}.99thPercentileLatency(us)', 0) / 1000:.3f} ms")
    print()


if __name__ == "__main__":
    main()
