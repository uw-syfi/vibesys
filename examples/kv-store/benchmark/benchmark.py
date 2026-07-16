"""Trusted Linux YCSB benchmark for the RESP2 KV-store target.

Requires Java 8+ and Linux procfs. By default the benchmark launches
``./run.sh <port>`` in an isolated process group; ``--port`` targets an
already-running server for diagnostics. The checksum-pinned YCSB 0.17.0 Redis
binding is cached under ``.cache/``.

Two families of metrics are reported, both machine-readable:

  1. Throughput (validity gate) — steady-state ops/sec. One discarded
     topology-matched warmup primes the server/workload path, then several runs are taken and
     their median reported. Throughput is load- and client-dependent: on a fast
     loopback server it saturates the *client* long before the server, so a
     single JVM understates the server. `--client-procs M` drives the server
     from M independent JVMs to reach server saturation.

  2. server CPU-per-op (`cpu_us_per_op`) — server-core-microseconds spent per
     operation, measured across the stable candidate process set via procfs. This is
     the *client-bottleneck-immune, load-independent, workload-agnostic* signal:
     it counts real work the server does per op regardless of how hard the client
     pushes, so a data-path optimization (that a saturated-client throughput
     number cannot see) shows up as fewer core-microseconds per op. `ops_per_cpu_sec`
     (its inverse) is the per-core goodput. `--probe-per-op` additionally isolates
     each op type (READ/UPDATE/SCAN/...) by driving it at 100%, exposing which
     op type an optimization actually made cheaper — the read/update attribution
     a single aggregate scalar hides. These generalize across every YCSB workload.

Machine-readable outputs (no LLM eyeballing):
  - stdout ends with `PERF_METRIC: <score> ops_per_cpu_sec`
  - `PERF_THROUGHPUT: <median_throughput> ops/sec`
  - `PERF_CPU_PER_OP: <median> us` (null if the server PID could not be found)
  - `--output-json PATH` writes all metrics as JSON for the profiler

Usage:
    python benchmark.py                         # launch ./run.sh automatically
    python benchmark.py --port 6380             # use an existing server
    python benchmark.py --port 6380 --threads 1               # single-client latency probe
    python benchmark.py --port 6380 --client-procs 8          # drive to server saturation
    python benchmark.py --port 6380 --probe-per-op            # per-op-type server CPU cost
    python benchmark.py --port 6380 --workload e              # scan-heavy
    python benchmark.py --port 6380 --output-json /tmp/bench.json
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parents[1]
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from evaluator_support import candidate_server  # noqa: E402

YCSB_VERSION = "0.17.0"
YCSB_URL = (
    f"https://github.com/brianfrankcooper/YCSB/releases/download/"
    f"{YCSB_VERSION}/ycsb-redis-binding-{YCSB_VERSION}.tar.gz"
)
YCSB_SHA256 = "353eb96c12a605c30c94928b85780ae4673578a21e2aa13782cd7f591991e484"
YCSB_CACHE = _WORKSPACE / ".cache"
YCSB_HOME = YCSB_CACHE / f"ycsb-redis-binding-{YCSB_VERSION}"

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


def _linux_preflight(proc_root=Path("/proc")) -> None:
    if sys.platform != "linux":
        raise RuntimeError("KV-store CPU scoring requires Linux procfs")
    if not (proc_root / "net" / "tcp").is_file() or not (proc_root / "self" / "stat").is_file():
        raise RuntimeError("KV-store CPU scoring requires readable Linux procfs")


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    root = destination.resolve()
    members = tar.getmembers()
    for member in members:
        target = (destination / member.name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"unsafe YCSB archive path: {member.name}") from exc
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise ValueError(f"unsupported YCSB archive member: {member.name}")
    tar.extractall(destination, members=members)


def _ensure_ycsb() -> None:
    marker = YCSB_HOME / ".vibeserve-sha256"
    if (YCSB_HOME / "bin" / "ycsb.sh").is_file() and marker.is_file():
        if marker.read_text().strip() == YCSB_SHA256:
            return

    YCSB_CACHE.mkdir(parents=True, exist_ok=True)
    lock_path = YCSB_CACHE / f".ycsb-{YCSB_VERSION}.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (YCSB_HOME / "bin" / "ycsb.sh").is_file() and marker.is_file():
            if marker.read_text().strip() == YCSB_SHA256:
                return
        print(f"Downloading YCSB {YCSB_VERSION} Redis binding...", file=sys.stderr)
        with tempfile.TemporaryDirectory(dir=YCSB_CACHE) as tmp_dir:
            tmp = Path(tmp_dir)
            tarball = tmp / "ycsb.tar.gz"
            digest = hashlib.sha256()
            with urllib.request.urlopen(YCSB_URL) as response, tarball.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != YCSB_SHA256:
                raise RuntimeError("YCSB archive checksum mismatch")
            extract_root = tmp / "extract"
            extract_root.mkdir()
            with tarfile.open(tarball, "r:gz") as tar:
                _safe_extract(tar, extract_root)
            extracted = extract_root / f"ycsb-redis-binding-{YCSB_VERSION}"
            if not (extracted / "bin" / "ycsb.sh").is_file():
                raise RuntimeError("YCSB archive is missing bin/ycsb.sh")
            (extracted / ".vibeserve-sha256").write_text(f"{YCSB_SHA256}\n")
            staged = YCSB_CACHE / f".{YCSB_HOME.name}.staged"
            if staged.exists():
                shutil.rmtree(staged)
            shutil.move(str(extracted), staged)
            if YCSB_HOME.exists():
                shutil.rmtree(YCSB_HOME)
            staged.replace(YCSB_HOME)


@dataclass(frozen=True)
class ProcessStat:
    pid: int
    parent_pid: int
    process_group: int
    starttime: int
    cpu_ticks: int

    @property
    def identity(self):
        return (self.pid, self.starttime)


def _read_process_stat(pid, proc_root=Path("/proc")):
    try:
        raw = (proc_root / str(pid) / "stat").read_text()
        fields = raw[raw.rindex(")") + 2 :].split()
        return ProcessStat(
            pid=int(pid),
            parent_pid=int(fields[1]),
            process_group=int(fields[2]),
            cpu_ticks=int(fields[11]) + int(fields[12]),
            starttime=int(fields[19]),
        )
    except (OSError, ValueError, IndexError):
        return None


def _all_process_stats(proc_root=Path("/proc")):
    stats = {}
    for path in proc_root.iterdir():
        if path.name.isdigit() and (stat := _read_process_stat(int(path.name), proc_root)):
            stats[stat.pid] = stat
    return stats


def _listener_pids(port, proc_root=Path("/proc")):
    """Return every process owning a listening socket for ``port``."""
    inodes = set()
    for table in (proc_root / "net" / "tcp", proc_root / "net" / "tcp6"):
        try:
            lines = table.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            # local_address is hex "ADDR:PORT"; st == 0A is LISTEN.
            if len(f) > 9 and f[3] == "0A" and int(f[1].rsplit(":", 1)[1], 16) == port:
                inodes.add(f[9])
    if not inodes:
        return set()
    pids = set()
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        try:
            for fd in (pid_dir / "fd").iterdir():
                target = os.readlink(fd)
                if target.startswith("socket:[") and target[8:-1] in inodes:
                    pids.add(int(pid_dir.name))
                    break
        except OSError:
            continue
    return pids


def _server_processes(port, process_group=None, proc_root=Path("/proc")):
    stats = _all_process_stats(proc_root)
    if process_group is not None:
        return {pid for pid, stat in stats.items() if stat.process_group == process_group}
    roots = _listener_pids(port, proc_root)
    selected = set(roots)
    changed = True
    while changed:
        changed = False
        for pid, stat in stats.items():
            if stat.parent_pid in selected and pid not in selected:
                selected.add(pid)
                changed = True
    return selected


def _cpu_snapshot(port, process_group=None, proc_root=Path("/proc")):
    pids = _server_processes(port, process_group, proc_root)
    if not pids:
        return None
    stats = [_read_process_stat(pid, proc_root) for pid in sorted(pids)]
    if any(stat is None for stat in stats):
        return None
    return {stat.identity: stat.cpu_ticks for stat in stats}


def _cpu_delta_seconds(before, after):
    if before is None or after is None or set(before) != set(after):
        return None
    delta_ticks = sum(after[key] - before[key] for key in before)
    if delta_ticks <= 0:
        return None
    return delta_ticks / _CLK


def _ycsb_cmd(phase, workload, port, num_keys, threads, *, duration=None, extra=(), record=()):
    props = [
        "-p",
        "redis.host=127.0.0.1",
        "-p",
        f"redis.port={port}",
        "-p",
        f"recordcount={num_keys}",
        "-p",
        f"operationcount={_OP_CAP}",
        "-p",
        f"threadcount={threads}",
        "-p",
        "hdrhistogram.percentiles=50,95,99,99.9",
        *record,
    ]
    if duration is not None and phase == "run":
        props += ["-p", f"maxexecutiontime={duration}"]
    props += list(extra)
    return [
        str(YCSB_HOME / "bin" / "ycsb.sh"),
        phase,
        "redis",
        "-s",
        "-P",
        str(YCSB_HOME / workload),
        *props,
    ]


def _run_ycsb(phase, workload, port, num_keys, threads, *, duration=None, extra=(), record=()):
    """Run a single YCSB phase to completion; return stdout (exits on failure)."""
    result = subprocess.run(
        _ycsb_cmd(
            phase, workload, port, num_keys, threads, duration=duration, extra=extra, record=record
        ),
        capture_output=True,
        text=True,
        timeout=600,
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


def _measure(
    workload,
    port,
    num_keys,
    threads,
    duration,
    procs,
    process_group,
    *,
    extra=(),
    record=(),
):
    """One measured round: `procs` YCSB run JVMs in parallel, bracketed by server
    CPU ticks. Aggregates throughput (sum) and ops (sum) across procs; latency
    percentiles are worst-case (p99/p99.9 = max across procs, p50 = median).

    Returns a dict with throughput, total_ops, per-op counts+latency, and
    externally-measured cpu_us_per_op / server_cpu_cores (None if no PID).
    """
    cmds = [
        _ycsb_cmd(
            "run", workload, port, num_keys, threads, duration=duration, extra=extra, record=record
        )
        for _ in range(procs)
    ]
    cpu_before = _cpu_snapshot(port, process_group)
    w0 = time.time()
    ps = [
        subprocess.Popen(c, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for c in cmds
    ]
    outs = []
    for p in ps:
        out, err = p.communicate()
        if p.returncode != 0:
            print(f"YCSB run failed:\n{err[-2000:]}")
            sys.exit(1)
        outs.append(out)
    w1 = time.time()
    cpu_after = _cpu_snapshot(port, process_group)

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

    cpu_s = _cpu_delta_seconds(cpu_before, cpu_after)
    if cpu_s is not None and agg["total_ops"]:
        agg["cpu_us_per_op"] = cpu_s / agg["total_ops"] * 1e6
        agg["server_cpu_cores"] = cpu_s / (w1 - w0)
        agg["server_process_count"] = len(cpu_before)
        agg["cpu_valid"] = True
    else:
        agg["cpu_us_per_op"] = None
        agg["server_cpu_cores"] = None
        agg["server_process_count"] = None
        agg["cpu_valid"] = False
    return agg


def _reduce_pct(runs, op, pct, reducer):
    vals = [r[_pct_key(op, pct)] for r in runs if _pct_key(op, pct) in r]
    return reducer(vals) if vals else None


def _probe_per_op(
    workload,
    port,
    num_keys,
    threads,
    duration,
    procs,
    process_group,
    present_ops,
    record=(),
):
    """Per-op-type server CPU cost: drive each present op type at 100% and measure
    its cpu_us_per_op in isolation. This is the read/update attribution channel —
    a read-path optimization lowers READ's cpu/op without touching UPDATE's."""
    out = {}
    for op in present_ops:
        extra = []
        for other, prop in OP_PROPORTION.items():
            extra += ["-p", f"{prop}={'1' if other == op else '0'}"]
        agg = _measure(
            workload,
            port,
            num_keys,
            threads,
            duration,
            procs,
            process_group,
            extra=extra,
            record=record,
        )
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


def _worst_latency_ms(rounds, operation):
    values = [
        _to_ms(round_["lat"][operation]["p99"])
        for round_ in rounds
        if operation in round_["lat"] and round_["lat"][operation]["p99"] is not None
    ]
    return max(values) if values else None


def _valid_number(value, *, positive=False):
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value > 0 if positive else True)
    )


def _evaluate_validity(
    *,
    throughput,
    cpu_per_op,
    rounds,
    read_p99_ms,
    update_p99_ms,
    saturation_gain_pct,
    min_throughput,
    max_read_p99_ms,
    max_update_p99_ms,
    max_saturation_gain_pct,
):
    checks = {
        "throughput_floor": _valid_number(throughput) and throughput >= min_throughput,
        "read_p99": _valid_number(read_p99_ms) and read_p99_ms < max_read_p99_ms,
        "update_p99": _valid_number(update_p99_ms) and update_p99_ms < max_update_p99_ms,
        "score_available": _valid_number(cpu_per_op, positive=True),
        "cpu_samples": bool(rounds) and all(round_["cpu_valid"] for round_ in rounds),
        "saturation": _valid_number(saturation_gain_pct)
        and abs(saturation_gain_pct) <= max_saturation_gain_pct,
    }
    reasons = []
    labels = {
        "throughput_floor": f"throughput must be >= {min_throughput:.1f} ops/sec",
        "read_p99": f"READ p99 must be < {max_read_p99_ms:.3f} ms",
        "update_p99": f"UPDATE p99 must be < {max_update_p99_ms:.3f} ms",
        "score_available": "server CPU/op must be finite and positive",
        "cpu_samples": "every scored repeat must have stable complete CPU accounting",
        "saturation": (
            f"higher-load throughput change must be within ±{max_saturation_gain_pct:.1f}%"
        ),
    }
    for name, passed in checks.items():
        if not passed:
            reasons.append(labels[name])
    return checks, reasons


def main():
    parser = argparse.ArgumentParser(description="KV store benchmark (YCSB)")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port of an already-running candidate. Omit to launch ./run.sh automatically.",
    )
    parser.add_argument("--workload", choices=list(WORKLOADS.keys()), default="a")
    parser.add_argument(
        "--num-keys", type=int, default=20000, help="recordcount loaded into the store"
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=16,
        help="YCSB client threads per process (pass --threads 1 for the latency probe).",
    )
    parser.add_argument(
        "--client-procs",
        type=int,
        default=1,
        help="Independent YCSB JVMs driven in parallel. >1 defeats the single-client "
        "CPU ceiling on a fast server so the run can reach server saturation.",
    )
    parser.add_argument(
        "--field-count",
        type=int,
        default=None,
        help="YCSB fieldcount (fields per record). Raising it scales the read "
        "(HGETALL-all-fields) server work without changing the one-field update — "
        "amplifying a read-path optimization's per-op-type CPU signal.",
    )
    parser.add_argument(
        "--field-length",
        type=int,
        default=None,
        help="YCSB fieldlength (bytes per field). Grows record/value size.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=5,
        help="Seconds per measured run (fixed-duration, steady-state).",
    )
    parser.add_argument(
        "--repeats", type=int, default=3, help="Measured runs; the median is the headline."
    )
    parser.add_argument("--no-warmup", action="store_true", help="Skip the discarded warmup run.")
    parser.add_argument(
        "--probe-per-op",
        action="store_true",
        help="Also measure per-op-type server CPU cost (isolates each op at 100%%).",
    )
    parser.add_argument(
        "--min-throughput-ops-per-sec",
        type=float,
        default=10000.0,
        help="Minimum accepted median throughput.",
    )
    parser.add_argument("--max-read-p99-ms", type=float, default=1.0)
    parser.add_argument("--max-update-p99-ms", type=float, default=1.0)
    parser.add_argument(
        "--saturation-probe-client-procs",
        type=int,
        default=8,
        help="Client JVM count for the non-scoring saturation probe.",
    )
    parser.add_argument("--max-saturation-gain-pct", type=float, default=10.0)
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write all metrics to this path as JSON.",
    )
    args = parser.parse_args()

    _linux_preflight()
    _ensure_ycsb()

    with candidate_server(workspace=_WORKSPACE, port=args.port) as managed:
        args.port = managed.port if managed is not None else args.port
        process_group = managed.process_group if managed is not None else None
        assert args.port is not None

        workload_path = WORKLOADS[args.workload]
        record = []
        if args.field_count is not None:
            record += ["-p", f"fieldcount={args.field_count}"]
        if args.field_length is not None:
            record += ["-p", f"fieldlength={args.field_length}"]

        _run_ycsb("load", workload_path, args.port, args.num_keys, args.threads, record=record)

        # Warm the server and workload path with the same client topology. Each
        # later measured run starts fresh JVMs, so this does not claim to warm
        # their JITs or connections.
        if not args.no_warmup:
            _measure(
                workload_path,
                args.port,
                args.num_keys,
                args.threads,
                min(args.duration, 3),
                args.client_procs,
                process_group,
                record=record,
            )

        rounds = [
            _measure(
                workload_path,
                args.port,
                args.num_keys,
                args.threads,
                args.duration,
                args.client_procs,
                process_group,
                record=record,
            )
            for _ in range(max(1, args.repeats))
        ]

        throughputs = [round_["throughput"] for round_ in rounds]
        median_throughput, cov_pct = _med_cov(throughputs)
        cpu_per_op, cpu_cov = _med_cov([round_["cpu_us_per_op"] for round_ in rounds])
        server_cores, _ = _med_cov([round_["server_cpu_cores"] for round_ in rounds])
        median_round = min(rounds, key=lambda round_: abs(round_["throughput"] - median_throughput))
        read_p99_ms = _worst_latency_ms(rounds, "READ")
        update_p99_ms = _worst_latency_ms(rounds, "UPDATE")

        saturation = _measure(
            workload_path,
            args.port,
            args.num_keys,
            args.threads,
            args.duration,
            args.saturation_probe_client_procs,
            process_group,
            record=record,
        )
        saturation_gain_pct = (
            (saturation["throughput"] / median_throughput - 1) * 100 if median_throughput else None
        )

        per_op_cpu = None
        if args.probe_per_op:
            per_op_cpu = _probe_per_op(
                workload_path,
                args.port,
                args.num_keys,
                args.threads,
                args.duration,
                args.client_procs,
                process_group,
                list(median_round["ops"]),
                record=record,
            )

        checks, invalid_reasons = _evaluate_validity(
            throughput=median_throughput,
            cpu_per_op=cpu_per_op,
            rounds=rounds,
            read_p99_ms=read_p99_ms,
            update_p99_ms=update_p99_ms,
            saturation_gain_pct=saturation_gain_pct,
            min_throughput=args.min_throughput_ops_per_sec,
            max_read_p99_ms=args.max_read_p99_ms,
            max_update_p99_ms=args.max_update_p99_ms,
            max_saturation_gain_pct=args.max_saturation_gain_pct,
        )
        score = 1e6 / cpu_per_op if _valid_number(cpu_per_op, positive=True) else None

        label_procs = f" x {args.client_procs} procs" if args.client_procs > 1 else ""
        print(f"\n{'=' * 60}")
        print(
            f"  YCSB Workload {args.workload.upper()} — {args.threads} thread"
            f"{'s' if args.threads != 1 else ''}{label_procs}, "
            f"{args.duration}s x {args.repeats} runs"
        )
        print(f"{'=' * 60}")
        print(
            f"Throughput (median): {median_throughput:.1f} ops/sec   "
            f"(CoV {cov_pct:.1f}%: {', '.join(f'{value:.0f}' for value in throughputs)})"
        )
        if cpu_per_op is not None:
            print(
                f"Server CPU/op (median): {cpu_per_op:.3f} us/op   "
                f"(CoV {cpu_cov:.1f}%)   [{score:,.0f} ops/core-sec]"
            )
            print(f"Server busy cores (median): {server_cores:.1f}   (diagnostic)")
        for operation, latency in median_round["lat"].items():
            cpu = (
                f"   cpu {per_op_cpu[operation]:.3f} us/op"
                if per_op_cpu and per_op_cpu.get(operation)
                else ""
            )
            print(
                f"{operation:18s} p50 {_ms(latency['p50'])}  "
                f"p99 {_ms(latency['p99'])}  p99.9 {_ms(latency['p999'])}{cpu}"
            )
        print(
            f"Saturation probe: {args.client_procs}→"
            f"{args.saturation_probe_client_procs} client procs, "
            f"gain {saturation_gain_pct:.1f}%"
            if saturation_gain_pct is not None
            else "Saturation probe: invalid"
        )

        payload = {
            "throughput_ops_per_sec": round(median_throughput, 1),
            "cov_pct": round(cov_pct, 1),
            "cpu_us_per_op": round(cpu_per_op, 3) if cpu_per_op is not None else None,
            "cpu_us_per_op_cov_pct": round(cpu_cov, 1),
            "ops_per_cpu_sec": round(score, 1) if score is not None else None,
            "server_cpu_cores": round(server_cores, 2) if server_cores is not None else None,
            "server_process_count": median_round["server_process_count"],
            "per_op_cpu_us": per_op_cpu,
            "read_p99_ms": read_p99_ms,
            "update_p99_ms": update_p99_ms,
            "latency_ms": {
                operation: {name: _to_ms(value) for name, value in latency.items()}
                for operation, latency in median_round["lat"].items()
            },
            "threads": args.threads,
            "client_procs": args.client_procs,
            "workload": args.workload,
            "runs_ops_per_sec": [round(value, 1) for value in throughputs],
            "saturation_probe_client_procs": args.saturation_probe_client_procs,
            "saturation_gain_pct": (
                round(saturation_gain_pct, 1) if saturation_gain_pct is not None else None
            ),
            "score_valid": not invalid_reasons,
            "invalid_reasons": invalid_reasons,
            "validity_checks": checks,
            "validity_thresholds": {
                "min_throughput_ops_per_sec": args.min_throughput_ops_per_sec,
                "max_read_p99_ms": args.max_read_p99_ms,
                "max_update_p99_ms": args.max_update_p99_ms,
                "max_saturation_gain_pct": args.max_saturation_gain_pct,
            },
        }
        if args.output_json:
            args.output_json.write_text(json.dumps(payload, indent=2))

        print(f"\nPERF_METRIC: {score:.1f} ops_per_cpu_sec" if score else "PERF_METRIC: null")
        print(f"PERF_THROUGHPUT: {median_throughput:.1f} ops/sec")
        print(f"PERF_COV: {cov_pct:.1f}%")
        print(
            f"PERF_CPU_PER_OP: {cpu_per_op:.3f} us"
            if cpu_per_op is not None
            else "PERF_CPU_PER_OP: null"
        )
        if invalid_reasons:
            for reason in invalid_reasons:
                print(f"INVALID: {reason}", file=sys.stderr)
            sys.exit(1)


def _to_ms(us):
    return round(us / 1000, 4) if us is not None else None


def _ms(us):
    return f"{us / 1000:.3f}ms" if us is not None else "  -   "


def _lat_ms(round_, op, pct):
    lat = round_["lat"].get(op)
    return _to_ms(lat[pct]) if lat else None


if __name__ == "__main__":
    main()
