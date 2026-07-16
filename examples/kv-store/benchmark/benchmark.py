"""Trusted Linux YCSB benchmark for the RESP2 KV-store target.

Requires Java 8+ and Linux procfs. By default launches ``./run.sh <port>`` in an
isolated process group; ``--port`` targets an already-running server.

Scored path: load Workload A, discard one topology-matched warmup, take several
fixed-duration runs, enforce throughput / p99 / saturation gates, then emit
``ops_per_cpu_sec`` from aggregated procfs CPU across the candidate process set.

Machine-readable outputs:
  - ``PERF_METRIC: <score> ops_per_cpu_sec``
  - ``PERF_THROUGHPUT: <median_throughput> ops/sec``
  - ``PERF_CPU_PER_OP: <median> us``
  - ``--output-json PATH`` writes the full payload

Optional diagnostics (not required for scoring): ``--probe-per-op``,
``--field-count``, ``--field-length``. See ``OBJECTIVE.md`` and
``CANDIDATE_CONTRACT.md``.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_WORKSPACE = Path(__file__).resolve().parents[1]
if str(_WORKSPACE) not in sys.path:
    sys.path.insert(0, str(_WORKSPACE))

from evaluator_support import candidate_server  # noqa: E402
from evaluator_support.procfs_cpu import (  # noqa: E402
    cpu_delta_seconds,
    cpu_snapshot,
    linux_preflight,
)
from evaluator_support.validity import evaluate_validity, valid_number  # noqa: E402
from evaluator_support.ycsb import (  # noqa: E402
    OP_PROPORTION,
    THROUGHPUT_KEY,
    WORKLOADS,
    cache_paths,
    ensure_ycsb,
    parse_metrics,
    pct_key,
    run_ycsb,
    ycsb_cmd,
)

_YCSB_CACHE, _YCSB_HOME = cache_paths(_WORKSPACE)


def _reduce_pct(runs: list[dict[str, float]], op: str, pct: str, reducer) -> float | None:
    vals = [r[pct_key(op, pct)] for r in runs if pct_key(op, pct) in r]
    return reducer(vals) if vals else None


def _measure(
    workload: str,
    port: int,
    num_keys: int,
    threads: int,
    duration: int,
    procs: int,
    process_group: int | None,
    *,
    extra: tuple[str, ...] | list[str] = (),
    record: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """One measured round: ``procs`` YCSB JVMs bracketed by server CPU ticks."""
    cmds = [
        ycsb_cmd(
            _YCSB_HOME,
            "run",
            workload,
            port,
            num_keys,
            threads,
            duration=duration,
            extra=extra,
            record=record,
        )
        for _ in range(procs)
    ]
    cpu_before = cpu_snapshot(port, process_group)
    wall0 = time.time()
    processes = [
        subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for cmd in cmds
    ]
    outs: list[str] = []
    for process in processes:
        out, err = process.communicate()
        if process.returncode != 0:
            print(f"YCSB run failed:\n{err[-2000:]}")
            sys.exit(1)
        outs.append(out)
    wall1 = time.time()
    cpu_after = cpu_snapshot(port, process_group)

    runs = [parse_metrics(out) for out in outs]
    agg: dict[str, Any] = {
        "throughput": sum(run.get(THROUGHPUT_KEY, 0.0) for run in runs),
        "total_ops": 0,
        "ops": {},
        "lat": {},
    }
    for op in OP_PROPORTION:
        ops = sum(int(run.get(f"{op}.Operations", 0)) for run in runs)
        if not ops:
            continue
        agg["ops"][op] = ops
        agg["total_ops"] += ops
        agg["lat"][op] = {
            "p50": _reduce_pct(runs, op, "50", statistics.median),
            "p99": _reduce_pct(runs, op, "99", max),
            "p999": _reduce_pct(runs, op, "99.9", max),
        }

    cpu_s = cpu_delta_seconds(cpu_before, cpu_after)
    if cpu_s is not None and cpu_before is not None and agg["total_ops"]:
        agg["cpu_us_per_op"] = cpu_s / agg["total_ops"] * 1e6
        agg["server_cpu_cores"] = cpu_s / (wall1 - wall0)
        agg["server_process_count"] = len(cpu_before)
        agg["cpu_valid"] = True
    else:
        agg["cpu_us_per_op"] = None
        agg["server_cpu_cores"] = None
        agg["server_process_count"] = None
        agg["cpu_valid"] = False
    return agg


def _probe_per_op(
    workload: str,
    port: int,
    num_keys: int,
    threads: int,
    duration: int,
    procs: int,
    process_group: int | None,
    present_ops: list[str],
    record: tuple[str, ...] | list[str] = (),
) -> dict[str, float | None]:
    """Diagnostic: isolate each present op type at 100% and measure cpu_us_per_op."""
    out: dict[str, float | None] = {}
    for op in present_ops:
        extra: list[str] = []
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


def _med_cov(values: list[float | None]) -> tuple[float | None, float]:
    vals = [value for value in values if value is not None]
    if not vals:
        return None, 0.0
    med = statistics.median(vals)
    cov = (statistics.pstdev(vals) / med * 100) if med and len(vals) > 1 else 0.0
    return med, cov


def _worst_latency_ms(rounds: list[dict[str, Any]], operation: str) -> float | None:
    values = [
        _to_ms(round_["lat"][operation]["p99"])
        for round_ in rounds
        if operation in round_["lat"] and round_["lat"][operation]["p99"] is not None
    ]
    return max(values) if values else None


def _to_ms(us: float | None) -> float | None:
    return round(us / 1000, 4) if us is not None else None


def _ms(us: float | None) -> str:
    return f"{us / 1000:.3f}ms" if us is not None else "  -   "


def _record_props(field_count: int | None, field_length: int | None) -> list[str]:
    record: list[str] = []
    if field_count is not None:
        record += ["-p", f"fieldcount={field_count}"]
    if field_length is not None:
        record += ["-p", f"fieldlength={field_length}"]
    return record


def _run_scored_rounds(
    *,
    workload_path: str,
    port: int,
    num_keys: int,
    threads: int,
    duration: int,
    client_procs: int,
    process_group: int | None,
    repeats: int,
    warmup: bool,
    record: list[str],
) -> list[dict[str, Any]]:
    if warmup:
        _measure(
            workload_path,
            port,
            num_keys,
            threads,
            min(duration, 3),
            client_procs,
            process_group,
            record=record,
        )
    return [
        _measure(
            workload_path,
            port,
            num_keys,
            threads,
            duration,
            client_procs,
            process_group,
            record=record,
        )
        for _ in range(max(1, repeats))
    ]


def _build_payload(
    *,
    median_throughput: float,
    cov_pct: float,
    cpu_per_op: float | None,
    cpu_cov: float,
    score: float | None,
    server_cores: float | None,
    median_round: dict[str, Any],
    per_op_cpu: dict[str, float | None] | None,
    read_p99_ms: float | None,
    update_p99_ms: float | None,
    throughputs: list[float],
    args: argparse.Namespace,
    saturation_gain_pct: float | None,
    invalid_reasons: list[str],
    checks: dict[str, bool],
) -> dict[str, Any]:
    return {
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
        "validity_thresholds": {
            "min_throughput_ops_per_sec": args.min_throughput_ops_per_sec,
            "max_read_p99_ms": args.max_read_p99_ms,
            "max_update_p99_ms": args.max_update_p99_ms,
            "max_saturation_gain_pct": args.max_saturation_gain_pct,
        },
        # Kept for tests / diagnostics; prefer invalid_reasons for humans.
        "validity_checks": checks,
    }


def _emit_report(
    *,
    args: argparse.Namespace,
    median_throughput: float,
    cov_pct: float,
    throughputs: list[float],
    cpu_per_op: float | None,
    cpu_cov: float,
    score: float | None,
    server_cores: float | None,
    median_round: dict[str, Any],
    per_op_cpu: dict[str, float | None] | None,
    saturation_gain_pct: float | None,
    invalid_reasons: list[str],
    payload: dict[str, Any],
) -> None:
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
    if saturation_gain_pct is not None:
        print(
            f"Saturation probe: {args.client_procs}→"
            f"{args.saturation_probe_client_procs} client procs, "
            f"gain {saturation_gain_pct:.1f}%"
        )
    else:
        print("Saturation probe: invalid")

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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
        help="Independent YCSB JVMs in parallel (defeats single-client CPU ceiling).",
    )
    parser.add_argument(
        "--field-count",
        type=int,
        default=None,
        help="Optional diagnostic: YCSB fieldcount override.",
    )
    parser.add_argument(
        "--field-length",
        type=int,
        default=None,
        help="Optional diagnostic: YCSB fieldlength override.",
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
        help="Optional diagnostic: measure per-op-type server CPU cost.",
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    linux_preflight()
    ensure_ycsb(cache=_YCSB_CACHE, home=_YCSB_HOME)

    with candidate_server(workspace=_WORKSPACE, port=args.port) as target:
        record = _record_props(args.field_count, args.field_length)
        workload_path = WORKLOADS[args.workload]

        run_ycsb(
            _YCSB_HOME,
            "load",
            workload_path,
            target.port,
            args.num_keys,
            args.threads,
            record=record,
        )

        rounds = _run_scored_rounds(
            workload_path=workload_path,
            port=target.port,
            num_keys=args.num_keys,
            threads=args.threads,
            duration=args.duration,
            client_procs=args.client_procs,
            process_group=target.process_group,
            repeats=args.repeats,
            warmup=not args.no_warmup,
            record=record,
        )

        throughputs = [round_["throughput"] for round_ in rounds]
        median_throughput, cov_pct = _med_cov(throughputs)
        cpu_per_op, cpu_cov = _med_cov([round_["cpu_us_per_op"] for round_ in rounds])
        server_cores, _ = _med_cov([round_["server_cpu_cores"] for round_ in rounds])
        assert median_throughput is not None
        median_round = min(rounds, key=lambda round_: abs(round_["throughput"] - median_throughput))
        read_p99_ms = _worst_latency_ms(rounds, "READ")
        update_p99_ms = _worst_latency_ms(rounds, "UPDATE")

        saturation = _measure(
            workload_path,
            target.port,
            args.num_keys,
            args.threads,
            args.duration,
            args.saturation_probe_client_procs,
            target.process_group,
            record=record,
        )
        saturation_gain_pct = (
            (saturation["throughput"] / median_throughput - 1) * 100 if median_throughput else None
        )

        per_op_cpu = None
        if args.probe_per_op:
            per_op_cpu = _probe_per_op(
                workload_path,
                target.port,
                args.num_keys,
                args.threads,
                args.duration,
                args.client_procs,
                target.process_group,
                list(median_round["ops"]),
                record=record,
            )

        checks, invalid_reasons = evaluate_validity(
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
        score = 1e6 / cpu_per_op if valid_number(cpu_per_op, positive=True) else None

        payload = _build_payload(
            median_throughput=median_throughput,
            cov_pct=cov_pct,
            cpu_per_op=cpu_per_op,
            cpu_cov=cpu_cov,
            score=score,
            server_cores=server_cores,
            median_round=median_round,
            per_op_cpu=per_op_cpu,
            read_p99_ms=read_p99_ms,
            update_p99_ms=update_p99_ms,
            throughputs=throughputs,
            args=args,
            saturation_gain_pct=saturation_gain_pct,
            invalid_reasons=invalid_reasons,
            checks=checks,
        )
        _emit_report(
            args=args,
            median_throughput=median_throughput,
            cov_pct=cov_pct,
            throughputs=throughputs,
            cpu_per_op=cpu_per_op,
            cpu_cov=cpu_cov,
            score=score,
            server_cores=server_cores,
            median_round=median_round,
            per_op_cpu=per_op_cpu,
            saturation_gain_pct=saturation_gain_pct,
            invalid_reasons=invalid_reasons,
            payload=payload,
        )


if __name__ == "__main__":
    main()
