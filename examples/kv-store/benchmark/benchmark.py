"""YCSB benchmark against an already-running candidate server.

Requires: Java 8+. Downloads the pinned YCSB 0.17.0 Redis binding to ./ycsb/
on first run if it is not already present.

Two families of metrics are reported, both machine-readable:

  1. Throughput (headline) — steady-state ops/sec. One discarded warmup run
     primes JVM/JIT/connections, then several fixed-duration runs are taken and
     their median reported. Throughput is load- and client-dependent: on a fast
     loopback server it saturates the *client* long before the server, so a
     single JVM understates the server. `--client-procs M` drives the server
     from M independent JVMs to reach server saturation.

  2. server CPU-per-op (`cpu_us_per_op`) — server-core-microseconds spent per
     operation, measured externally from /proc/<pid>/stat over the run. This is
     the *client-bottleneck-immune, load-independent, workload-agnostic* signal:
     it counts real work the server does per op regardless of how hard the client
     pushes, so a data-path optimization (that a saturated-client throughput
     number cannot see) shows up as fewer core-microseconds per op. `ops_per_cpu_sec`
     (its inverse) is the per-core goodput. `--probe-per-op` additionally isolates
     each op type (READ/UPDATE/SCAN/...) by driving it at 100%, exposing which
     op type an optimization actually made cheaper — the read/update attribution
     a single aggregate scalar hides. These generalize across every YCSB workload.

Machine-readable outputs (no LLM eyeballing):
  - stdout ends with `PERF_METRIC: <median_throughput> ops/sec`
  - `PERF_CPU_PER_OP: <median> us` (null if the server PID could not be found)
  - `--output-json PATH` writes all metrics as JSON for the profiler

Usage:
    python benchmark.py --port 6380
    python benchmark.py --port 6380 --threads 1               # single-client latency probe
    python benchmark.py --port 6380 --client-procs 8          # drive to server saturation
    python benchmark.py --port 6380 --probe-per-op            # per-op-type server CPU cost
    python benchmark.py --port 6380 --workload e              # scan-heavy
    python benchmark.py --port 6380 --output-json /tmp/bench.json
"""

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

YCSB_VERSION = "0.17.0"
YCSB_URL = (
    f"https://github.com/brianfrankcooper/YCSB/releases/download/"
    f"{YCSB_VERSION}/ycsb-redis-binding-{YCSB_VERSION}.tar.gz"
)
YCSB_HOME = Path(__file__).resolve().parent / "ycsb"

WORKLOADS = {w: f"workloads/workload{w}" for w in ("a", "b", "c", "d", "e", "f")}

# Metric key YCSB emits for overall throughput; the headline number.
THROUGHPUT_KEY = "OVERALL.Throughput(ops/sec)"

# Op types YCSB reports, and the CoreWorkload proportion knob that drives each
# (used by --probe-per-op to isolate one op type at 100%).
OP_PROPORTION = {
    "READ": "readproportion",
    "UPDATE": "updateproportion",
    "INSERT": "insertproportion",
    "SCAN": "scanproportion",
    "READ-MODIFY-WRITE": "readmodifywriteproportion",
}

# Huge op-count cap so a run ends on maxexecutiontime, not on ops exhausted.
_OP_CAP = 1_000_000_000
_CLK = os.sysconf("SC_CLK_TCK")


def _ensure_ycsb() -> None:
    if (YCSB_HOME / "bin" / "ycsb.sh").is_file():
        return
    print(f"Downloading YCSB {YCSB_VERSION} Redis binding...", file=sys.stderr)
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        tarball = tmp / f"ycsb-redis-binding-{YCSB_VERSION}.tar.gz"
        urllib.request.urlretrieve(YCSB_URL, tarball)
        with tarfile.open(tarball, "r:gz") as tar:
            tar.extractall(tmp)
        extracted = tmp / f"ycsb-redis-binding-{YCSB_VERSION}"
        if YCSB_HOME.exists():
            shutil.rmtree(YCSB_HOME)
        shutil.move(str(extracted), str(YCSB_HOME))


def _server_pid(port):
    """Discover the PID listening on `port` so its CPU can be sampled externally.

    The benchmark is only told --port (the server is already running), so CPU
    attribution — the reward metric's source — has to rediscover the process.
    Tries `ss` first, then a `/proc/net/tcp` + fd-scan fallback. Returns None if
    neither works (cpu_us_per_op / ops_per_cpu_sec then report null).
    """
    try:
        out = subprocess.run(
            ["ss", "-Htanp", f"sport = :{port}"], capture_output=True, text=True, timeout=5
        ).stdout
        m = re.search(r"pid=(\d+)", out)
        if m:
            return int(m.group(1))
    except (OSError, subprocess.SubprocessError):
        pass
    return _pid_via_proc(port)


def _pid_via_proc(port):
    """Fallback PID discovery: map the listening socket's inode (from /proc/net/tcp[6])
    to the process that owns it by scanning /proc/<pid>/fd. Same-namespace only."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            # local_address is hex "ADDR:PORT"; st == 0A is LISTEN.
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(f[9])
    if not inodes:
        return None
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            for fd in (pid_dir / "fd").iterdir():
                if os.readlink(fd).startswith("socket:[") and os.readlink(fd)[8:-1] in inodes:
                    return int(pid_dir.name)
        except OSError:
            continue
    return None


def _cpu_ticks(pid):
    """Server CPU jiffies (utime+stime) from /proc/<pid>/stat, or None."""
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces/parens; split after the final ')'.
    fields = stat[stat.rindex(")") + 2 :].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def _ycsb_cmd(phase, workload, port, num_keys, threads, *, duration=None, extra=(), record=()):
    props = [
        "-p", "redis.host=127.0.0.1",
        "-p", f"redis.port={port}",
        "-p", f"recordcount={num_keys}",
        "-p", f"operationcount={_OP_CAP}",
        "-p", f"threadcount={threads}",
        "-p", "hdrhistogram.percentiles=50,95,99,99.9",
        *record,
    ]
    if duration is not None and phase == "run":
        props += ["-p", f"maxexecutiontime={duration}"]
    props += list(extra)
    return [
        str(YCSB_HOME / "bin" / "ycsb.sh"), phase, "redis", "-s",
        "-P", str(YCSB_HOME / workload), *props,
    ]


def _run_ycsb(phase, workload, port, num_keys, threads, *, duration=None, extra=(), record=()):
    """Run a single YCSB phase to completion; return stdout (exits on failure)."""
    result = subprocess.run(
        _ycsb_cmd(phase, workload, port, num_keys, threads, duration=duration, extra=extra, record=record),
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


def _pct_key(op, pct):
    # YCSB emits "50thPercentileLatency(us)" but "99.9PercentileLatency(us)" (no 'th').
    suffix = "th" if "." not in pct else ""
    return f"{op}.{pct}{suffix}PercentileLatency(us)"


def _measure(workload, port, num_keys, threads, duration, procs, pid, *, extra=(), record=()):
    """One measured round: `procs` YCSB run JVMs in parallel, bracketed by server
    CPU ticks. Aggregates throughput (sum) and ops (sum) across procs; latency
    percentiles are worst-case (p99/p99.9 = max across procs, p50 = median).

    Returns a dict with throughput, total_ops, per-op counts+latency, and
    externally-measured cpu_us_per_op / server_cpu_cores (None if no PID).
    """
    cmds = [
        _ycsb_cmd("run", workload, port, num_keys, threads, duration=duration, extra=extra, record=record)
        for _ in range(procs)
    ]
    t0 = _cpu_ticks(pid) if pid else None
    w0 = time.time()
    ps = [subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for c in cmds]
    outs = []
    for p in ps:
        out, err = p.communicate()
        if p.returncode != 0:
            print(f"YCSB run failed:\n{err[-2000:]}")
            sys.exit(1)
        outs.append(out)
    w1 = time.time()
    t1 = _cpu_ticks(pid) if pid else None

    runs = [_parse_metrics(o) for o in outs]
    agg = {
        "throughput": sum(r.get(THROUGHPUT_KEY, 0.0) for r in runs),
        "total_ops": 0,
        "ops": {},
        "lat": {},
    }
    for op in OP_PROPORTION:
        ops = sum(int(r.get(f"{op}.Operations", 0)) for r in runs)
        if not ops:
            continue
        agg["ops"][op] = ops
        agg["total_ops"] += ops
        agg["lat"][op] = {
            "p50": _reduce_pct(runs, op, "50", statistics.median),
            "p99": _reduce_pct(runs, op, "99", max),
            "p999": _reduce_pct(runs, op, "99.9", max),
        }

    if t0 is not None and t1 is not None and agg["total_ops"]:
        cpu_s = (t1 - t0) / _CLK
        agg["cpu_us_per_op"] = cpu_s / agg["total_ops"] * 1e6
        agg["server_cpu_cores"] = cpu_s / (w1 - w0)
    else:
        agg["cpu_us_per_op"] = None
        agg["server_cpu_cores"] = None
    return agg


def _reduce_pct(runs, op, pct, reducer):
    vals = [r[_pct_key(op, pct)] for r in runs if _pct_key(op, pct) in r]
    return reducer(vals) if vals else None


def _probe_per_op(workload, port, num_keys, threads, duration, procs, pid, present_ops, record=()):
    """Per-op-type server CPU cost: drive each present op type at 100% and measure
    its cpu_us_per_op in isolation. This is the read/update attribution channel —
    a read-path optimization lowers READ's cpu/op without touching UPDATE's."""
    out = {}
    for op in present_ops:
        extra = []
        for other, prop in OP_PROPORTION.items():
            extra += ["-p", f"{prop}={'1' if other == op else '0'}"]
        agg = _measure(workload, port, num_keys, threads, duration, procs, pid, extra=extra, record=record)
        out[op] = round(agg["cpu_us_per_op"], 3) if agg["cpu_us_per_op"] is not None else None
    return out


def _med_cov(values):
    """(median, coefficient-of-variation %) over non-None values."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None, 0.0
    med = statistics.median(vals)
    cov = (statistics.pstdev(vals) / med * 100) if med and len(vals) > 1 else 0.0
    return med, cov


def main():
    parser = argparse.ArgumentParser(description="KV store benchmark (YCSB)")
    parser.add_argument(
        "--port", type=int, required=True,
        help="Port of the candidate server (must be already running)",
    )
    parser.add_argument("--workload", choices=list(WORKLOADS.keys()), default="a")
    parser.add_argument(
        "--num-keys", type=int, default=20000, help="recordcount loaded into the store"
    )
    parser.add_argument(
        "--threads", type=int, default=16,
        help="YCSB client threads per process (pass --threads 1 for the latency probe).",
    )
    parser.add_argument(
        "--client-procs", type=int, default=1,
        help="Independent YCSB JVMs driven in parallel. >1 defeats the single-client "
        "CPU ceiling on a fast server so the run can reach server saturation.",
    )
    parser.add_argument(
        "--field-count", type=int, default=None,
        help="YCSB fieldcount (fields per record). Raising it scales the read "
        "(HGETALL-all-fields) server work without changing the one-field update — "
        "amplifying a read-path optimization's per-op-type CPU signal.",
    )
    parser.add_argument(
        "--field-length", type=int, default=None,
        help="YCSB fieldlength (bytes per field). Grows record/value size.",
    )
    parser.add_argument(
        "--duration", type=int, default=5,
        help="Seconds per measured run (fixed-duration, steady-state).",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Measured runs; the median is the headline."
    )
    parser.add_argument("--no-warmup", action="store_true", help="Skip the discarded warmup run.")
    parser.add_argument(
        "--probe-per-op", action="store_true",
        help="Also measure per-op-type server CPU cost (isolates each op at 100%%).",
    )
    parser.add_argument(
        "--output-json", type=Path, default=None,
        help="Write all metrics to this path as JSON.",
    )
    args = parser.parse_args()

    _ensure_ycsb()

    workload_path = WORKLOADS[args.workload]
    pid = _server_pid(args.port)
    if pid is None:
        print(
            f"WARNING: could not find server PID on port {args.port} (ss/proc); "
            "cpu_us_per_op will be null.",
            file=sys.stderr,
        )

    # Record-shape knobs (fieldcount/fieldlength) must match between load and run.
    record = []
    if args.field_count is not None:
        record += ["-p", f"fieldcount={args.field_count}"]
    if args.field_length is not None:
        record += ["-p", f"fieldlength={args.field_length}"]

    _run_ycsb("load", workload_path, args.port, args.num_keys, args.threads, record=record)

    # Discarded warmup: primes JVM/JIT/connections/page-cache so measured runs
    # reflect steady state rather than a cold-start transient.
    if not args.no_warmup:
        _run_ycsb(
            "run", workload_path, args.port, args.num_keys, args.threads,
            duration=min(args.duration, 3), record=record,
        )

    rounds = [
        _measure(
            workload_path, args.port, args.num_keys, args.threads,
            args.duration, args.client_procs, pid, record=record,
        )
        for _ in range(max(1, args.repeats))
    ]

    throughputs = [r["throughput"] for r in rounds]
    median_throughput, cov_pct = _med_cov(throughputs)
    cpu_per_op, cpu_cov = _med_cov([r["cpu_us_per_op"] for r in rounds])
    server_cores, _ = _med_cov([r["server_cpu_cores"] for r in rounds])
    # Report per-op latency from the round whose throughput is closest to median.
    median_round = min(rounds, key=lambda r: abs(r["throughput"] - median_throughput))

    per_op_cpu = None
    if args.probe_per_op and pid is not None:
        per_op_cpu = _probe_per_op(
            workload_path, args.port, args.num_keys, args.threads,
            args.duration, args.client_procs, pid, list(median_round["ops"]), record=record,
        )

    label_procs = f" x {args.client_procs} procs" if args.client_procs > 1 else ""
    print(f"\n{'=' * 60}")
    print(
        f"  YCSB Workload {args.workload.upper()} — {args.threads} thread"
        f"{'s' if args.threads != 1 else ''}{label_procs}, {args.duration}s x {args.repeats} runs"
    )
    print(f"{'=' * 60}")
    print(
        f"Throughput (median): {median_throughput:.1f} ops/sec   (CoV {cov_pct:.1f}%: "
        f"{', '.join(f'{t:.0f}' for t in throughputs)})"
    )
    if cpu_per_op is not None:
        print(
            f"Server CPU/op (median): {cpu_per_op:.3f} us/op   (CoV {cpu_cov:.1f}%)   "
            f"[{1e6 / cpu_per_op:,.0f} ops/core-sec]"
        )
        print(f"Server busy cores (median): {server_cores:.1f}   (saturation check)")
    for op in median_round["lat"]:
        lat = median_round["lat"][op]
        cpu = f"   cpu {per_op_cpu[op]:.3f} us/op" if per_op_cpu and per_op_cpu.get(op) else ""
        print(
            f"{op:18s} p50 {_ms(lat['p50'])}  p99 {_ms(lat['p99'])}  "
            f"p99.9 {_ms(lat['p999'])}{cpu}"
        )

    if args.output_json:
        args.output_json.write_text(
            json.dumps(
                {
                    "throughput_ops_per_sec": round(median_throughput, 1),
                    "cov_pct": round(cov_pct, 1),
                    "cpu_us_per_op": round(cpu_per_op, 3) if cpu_per_op is not None else None,
                    "cpu_us_per_op_cov_pct": round(cpu_cov, 1),
                    "ops_per_cpu_sec": round(1e6 / cpu_per_op, 1) if cpu_per_op else None,
                    "server_cpu_cores": round(server_cores, 2) if server_cores is not None else None,
                    "per_op_cpu_us": per_op_cpu,
                    "read_p99_ms": _lat_ms(median_round, "READ", "p99"),
                    "update_p99_ms": _lat_ms(median_round, "UPDATE", "p99"),
                    "latency_ms": {
                        op: {k: _to_ms(v) for k, v in lat.items()}
                        for op, lat in median_round["lat"].items()
                    },
                    "threads": args.threads,
                    "client_procs": args.client_procs,
                    "workload": args.workload,
                    "runs_ops_per_sec": [round(t, 1) for t in throughputs],
                },
                indent=2,
            )
        )

    # Machine-readable headline (parse these lines verbatim; do not eyeball the text above).
    print(f"\nPERF_METRIC: {median_throughput:.1f} ops/sec")
    print(f"PERF_COV: {cov_pct:.1f}%")
    print(f"PERF_CPU_PER_OP: {cpu_per_op:.3f} us" if cpu_per_op is not None else "PERF_CPU_PER_OP: null")


def _to_ms(us):
    return round(us / 1000, 4) if us is not None else None


def _ms(us):
    return f"{us / 1000:.3f}ms" if us is not None else "  -   "


def _lat_ms(round_, op, pct):
    lat = round_["lat"].get(op)
    return _to_ms(lat[pct]) if lat else None


if __name__ == "__main__":
    main()
