#!/usr/bin/env python3
"""Run a closed-loop concurrency sweep through Request Factory."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_MODEL = "meta-llama/Llama-3.1-8B-Instruct"
_DEFAULT_CONCURRENCIES = (1, 2, 4, 8, 16, 32, 64, 128)
_METRICS = {
    "aggregate_throughput": {"unit": "tok/s", "direction": "max"},
    "p99_latency_ms": {"unit": "ms", "direction": "min"},
}
_UNDERSIZED_POOL_WARNING = "synthetic content will repeat within a single request"


def _write_trace(path: Path, count: int, input_tokens: int, output_tokens: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("id", "arrival_time", "input_len", "output_len"))
        for index in range(count):
            writer.writerow((f"request-{index:05d}", 0, input_tokens, output_tokens))


def _write_corpus(path: Path, token_pool_limit: int) -> None:
    seed = "The server processes a synthetic request and returns generated text"
    with path.open("w", encoding="utf-8") as corpus:
        for index in range(token_pool_limit):
            corpus.write(f"{seed} sample {index} token sequence {index}\n")


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot compute a percentile without successful request timings")
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _request_p99(log_path: Path, expected: int) -> float:
    latencies: list[float] = []
    for line_number, line in enumerate(log_path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
            outcome = record["outcome"]
            if outcome.get("error") is None:
                duration = outcome["total_duration_ms"]
                if isinstance(duration, bool) or not isinstance(duration, int | float):
                    raise ValueError("total_duration_ms is not numeric")
                latencies.append(float(duration))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid RF request log at line {line_number}: {exc}") from exc
    if len(latencies) != expected:
        raise ValueError(
            f"RF request log has {len(latencies)} successful timings, expected {expected}"
        )
    return _percentile(latencies, 0.99)


def _metrics(summary: Mapping[str, Any], request_count: int, log_path: Path) -> dict[str, float]:
    replay = summary.get("replay")
    common = replay.get("common") if isinstance(replay, Mapping) else None
    if not isinstance(common, Mapping):
        raise ValueError("RF summary is missing replay.common")
    expected = {
        "attempted_steps": request_count,
        "success_steps": request_count,
        "failed_steps": 0,
        "output_mismatch_steps": 0,
    }
    for name, value in expected.items():
        actual = common.get(name)
        if actual != value:
            raise ValueError(f"RF summary {name}={actual!r}, expected {value}")
    throughput = common.get("output_token_throughput_per_s")
    if isinstance(throughput, bool) or not isinstance(throughput, int | float):
        raise ValueError(f"RF output-token throughput is missing or not numeric: {throughput!r}")
    throughput = float(throughput)
    if not math.isfinite(throughput) or throughput <= 0:
        raise ValueError(f"RF output-token throughput is invalid: {throughput!r}")
    return {
        "aggregate_throughput": throughput,
        "p99_latency_ms": _request_p99(log_path, request_count),
    }


def _run_point(args: argparse.Namespace, concurrency: int, label: str) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="llama3-rf-point-") as directory:
        temporary = Path(directory)
        trace, summary_path, log_path = (
            temporary / "requests.csv",
            temporary / "summary.json",
            temporary / "requests.jsonl",
        )
        _write_trace(trace, args.request_count, args.input_tokens, args.output_tokens)
        token_pool_limit = max(2 * args.input_tokens, args.request_count)
        corpus = temporary / "corpus.txt"
        _write_corpus(corpus, token_pool_limit)
        command = [
            args.request_factory_engine,
            "--trace",
            str(trace),
            "--input-file-format",
            "text-generation-independent",
            "--text-file",
            str(corpus),
            "--tokenizer",
            args.tokenizer,
            "--model",
            args.model,
            "--backend",
            "openai",
            "--dialect",
            "openai",
            "--base-url",
            args.url.rstrip("/") + "/v1",
            "--temperature",
            "0",
            "--arrival-mode",
            "saturated",
            "--max-concurrency",
            str(concurrency),
            "--token-pool-limit",
            str(token_pool_limit),
            "--request-log",
            "true",
            "--log-path",
            str(log_path),
            "--timeline",
            "false",
            "--summary-path",
            str(summary_path),
        ]
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        if completed.stdout:
            print(completed.stdout, end="", flush=True)
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr, flush=True)
        if _UNDERSIZED_POOL_WARNING in completed.stderr:
            raise ValueError(
                "Request Factory token pool is shorter than the longest prompt; "
                "increase the generated corpus or token-pool limit"
            )
        if completed.returncode != 0:
            raise RuntimeError(f"Request Factory exited with status {completed.returncode}")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        replay = summary.get("replay")
        common = replay.get("common") if isinstance(replay, Mapping) else None
        if isinstance(common, Mapping) and common.get("failed_steps", 0):
            details = [
                json.loads(line).get("outcome", {}).get("error")
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if json.loads(line).get("outcome", {}).get("error")
            ]
            raise ValueError(f"RF reported {common['failed_steps']} failed requests: {details[:3]}")
        values = _metrics(summary, args.request_count, log_path)
        return {
            "label": label,
            "concurrency": concurrency,
            "request_count": args.request_count,
            "input_tokens": args.input_tokens,
            "output_tokens": args.output_tokens,
            "metrics": values,
        }


def _median_metric(rows: list[dict[str, Any]], name: str) -> float:
    return statistics.median(row["metrics"][name] for row in rows)


def _overload(rows: list[dict[str, Any]]) -> int | None:
    sustainable: list[dict[str, Any]] = []
    best_throughput = 0.0
    last_latency: float | None = None
    for index, row in enumerate(rows):
        metrics = row.get("metrics", row)
        throughput, latency = metrics["aggregate_throughput"], metrics["p99_latency_ms"]
        if sustainable and (
            throughput < best_throughput * 0.95
            or (
                throughput <= best_throughput * 1.03 and last_latency and latency > last_latency * 2
            )
        ):
            return index
        sustainable.append(row)
        best_throughput = max(best_throughput, throughput)
        last_latency = latency
    return None


def run(args: argparse.Namespace) -> int:
    output_path = Path(args.vs_output) if args.vs_output else None
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps({"kind": "hello", "protocol": 2, "metrics": _METRICS}) + "\n",
            encoding="utf-8",
        )
    try:
        rows = [
            _run_point(args, concurrency, f"coarse-c{concurrency}")
            for concurrency in args.concurrencies
        ]
        first_overload = _overload(rows)
        if first_overload is not None:
            if first_overload == 0:
                raise ValueError("the lowest concurrency point was already overloaded")
            lower = rows[first_overload - 1]["concurrency"]
            upper = rows[first_overload]["concurrency"]
            middle = (lower + upper) // 2
            if middle > lower:
                rows.append(_run_point(args, middle, "boundary-midpoint"))
            for index in {first_overload - 1, first_overload}:
                concurrency = rows[index]["concurrency"]
                rows.append(_run_point(args, concurrency, f"confirm-c{concurrency}"))
        else:
            prior_rows = rows[:-1] or rows
            best_prior = max(row["metrics"]["aggregate_throughput"] for row in prior_rows)
            if len(rows) > 1 and rows[-1]["metrics"]["aggregate_throughput"] > best_prior * 1.03:
                raise ValueError(
                    "concurrency sweep ended while throughput was still rising by more than 3%"
                )
            best_row = max(rows, key=lambda row: row["metrics"]["aggregate_throughput"])
            best_index = rows.index(best_row)
            for index in {max(0, best_index - 1), best_index, min(len(rows) - 1, best_index + 1)}:
                concurrency = rows[index]["concurrency"]
                rows.append(_run_point(args, concurrency, f"confirm-c{concurrency}"))

        by_concurrency: dict[int, list[dict[str, Any]]] = {}
        for row in rows:
            by_concurrency.setdefault(row["concurrency"], []).append(row)
        points = []
        for concurrency, repetitions in sorted(by_concurrency.items()):
            points.append(
                {
                    "concurrency": concurrency,
                    "repetitions": len(repetitions),
                    "aggregate_throughput": _median_metric(repetitions, "aggregate_throughput"),
                    "p99_latency_ms": _median_metric(repetitions, "p99_latency_ms"),
                    "runs": repetitions,
                }
            )
        suspected = _overload(points)
        limit = points[suspected]["concurrency"] if suspected is not None else None
        sustainable = [point for point in points if limit is None or point["concurrency"] < limit]
        if not sustainable:
            raise ValueError("no sustainable concurrency point was measured")
        selected = max(sustainable, key=lambda point: point["aggregate_throughput"])
        values = {
            "aggregate_throughput": selected["aggregate_throughput"],
            "p99_latency_ms": selected["p99_latency_ms"],
        }
        result = {
            "metrics": values,
            "selected_concurrency": selected["concurrency"],
            "sweep": points,
        }
        if args.output_json:
            Path(args.output_json).write_text(
                json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
            )
        if output_path:
            with output_path.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps(
                        {
                            "kind": "result",
                            "label": "",
                            "values": values,
                            "artifacts": {"sweep": points},
                            "metadata": {"selected_concurrency": selected["concurrency"]},
                            "unit": "tok/s",
                        },
                        allow_nan=False,
                    )
                    + "\n"
                )
        print(json.dumps(result, allow_nan=False), flush=True)
        return 0
    except Exception as exc:
        if output_path:
            with output_path.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps({"kind": "error", "message": str(exc)}, allow_nan=False) + "\n"
                )
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", default="session_runner")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--model", default=_MODEL)
    parser.add_argument("--tokenizer", default="meta-llama/Llama-3.1-8B-Instruct")
    parser.add_argument("--request-count", type=int, default=512)
    parser.add_argument("--input-tokens", type=int, default=256)
    parser.add_argument("--output-tokens", type=int, default=128)
    parser.add_argument(
        "--concurrencies",
        type=lambda raw: tuple(int(v) for v in raw.split(",")),
        default=_DEFAULT_CONCURRENCIES,
    )
    parser.add_argument("--output-json")
    parser.add_argument("--vs-output")
    args = parser.parse_args()
    if args.request_count < 1 or args.input_tokens < 1 or args.output_tokens < 1:
        parser.error("request and token counts must be positive")
    if (
        not args.concurrencies
        or any(value < 1 for value in args.concurrencies)
        or tuple(sorted(set(args.concurrencies))) != args.concurrencies
    ):
        parser.error("concurrencies must be unique positive integers in ascending order")
    args.request_factory_engine = args.request_factory_engine
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
