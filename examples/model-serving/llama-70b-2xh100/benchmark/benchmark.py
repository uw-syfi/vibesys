#!/usr/bin/env python3
"""Run the fixed text-serving workload through Request Factory.

The RF engine owns request generation and measurements. This adapter writes
its deterministic trace, rejects incomplete/failed runs, and maps RF's summary
to the VibeSys evaluator result protocol.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_MODEL = "meta-llama/Llama-3.3-70B-Instruct"
_DEFAULT_REQUESTS = 64
_DEFAULT_INPUT_TOKENS = 256
_DEFAULT_OUTPUT_TOKENS = 128
_DEFAULT_CONCURRENCY = 8
_TOKEN_POOL_WARNING = "token pool ("
_METRICS = {
    "output_token_throughput_per_s": {"unit": "tok/s", "direction": "max"},
    "p90_latency_ms": {"unit": "ms", "direction": "min"},
}


def _write_record(path: Path | None, record: Mapping[str, Any]) -> None:
    if path is None:
        return
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, allow_nan=False) + "\n")
        output.flush()


def _write_trace(path: Path, count: int, input_tokens: int, output_tokens: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as trace:
        writer = csv.writer(trace)
        writer.writerow(("id", "arrival_time", "input_len", "output_len"))
        for index in range(count):
            writer.writerow((f"request-{index:04d}", 0, input_tokens, output_tokens))


def _write_corpus(path: Path, token_pool_limit: int) -> None:
    """Write deterministic synthetic text; RF tokenizes it into the bounded pool."""
    with path.open("w", encoding="utf-8") as corpus:
        for start in range(0, token_pool_limit, 128):
            end = min(start + 128, token_pool_limit)
            segments = " ".join(f"segment{index:08d}" for index in range(start, end))
            corpus.write(f"Synthetic benchmark corpus: {segments}.\n")


def _summary_metrics(summary: Mapping[str, Any], expected_requests: int) -> dict[str, float]:
    replay = summary.get("replay")
    common = replay.get("common") if isinstance(replay, Mapping) else None
    if not isinstance(common, Mapping):
        raise ValueError("RF summary is missing replay.common")

    checks = {
        "failed_steps": 0,
        "attempted_steps": expected_requests,
        "success_steps": expected_requests,
        "output_mismatch_steps": 0,
    }
    for key, expected in checks.items():
        actual = common.get(key)
        if actual != expected:
            raise ValueError(f"RF summary {key}={actual!r}, expected {expected}")

    values = {
        "output_token_throughput_per_s": common.get("output_token_throughput_per_s"),
        "p90_latency_ms": common.get("total_duration_ms_p90"),
    }
    result: dict[str, float] = {}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"RF summary metric {name} is missing or not numeric: {value!r}")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"RF summary metric {name} is invalid: {value!r}")
        result[name] = number
    if result["output_token_throughput_per_s"] <= 0:
        raise ValueError("RF summary output-token throughput must be positive")
    return result


def run(args: argparse.Namespace) -> int:
    output_path = Path(args.vs_output) if args.vs_output else None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        _write_record(output_path, {"kind": "hello", "protocol": 2, "metrics": _METRICS})

    try:
        with tempfile.TemporaryDirectory(prefix="vibesys-rf-llama70b-") as directory:
            temporary = Path(directory)
            trace_path = temporary / "requests.csv"
            corpus_path = temporary / "corpus.txt"
            summary_path = temporary / "summary.json"
            _write_trace(trace_path, args.request_count, args.input_tokens, args.output_tokens)
            token_pool_limit = max(2 * args.input_tokens, args.request_count)
            _write_corpus(corpus_path, token_pool_limit)
            command = [
                args.request_factory_engine,
                "--trace",
                str(trace_path),
                "--input-file-format",
                "text-generation-independent",
                "--text-file",
                str(corpus_path),
                "--token-pool-limit",
                str(token_pool_limit),
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
                str(args.concurrency),
                "--request-log",
                "false",
                "--timeline",
                "false",
                "--summary-path",
                str(summary_path),
            ]
            completed = subprocess.run(command, check=False, text=True, capture_output=True)
            if completed.stdout:
                print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, end="", file=sys.stderr)
            if _TOKEN_POOL_WARNING in completed.stderr:
                raise RuntimeError("Request Factory token pool is shorter than the longest prompt")
            if completed.returncode != 0:
                raise RuntimeError(f"Request Factory exited with status {completed.returncode}")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            values = _summary_metrics(summary, args.request_count)
            _write_record(output_path, {"kind": "result", "label": "", "values": values})
            print(json.dumps({"metrics": values}, sort_keys=True))
            return 0
    except Exception as exc:
        _write_record(output_path, {"kind": "error", "message": str(exc)})
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    parser.add_argument("--vs-output")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--model", default=_MODEL)
    parser.add_argument("--tokenizer", default=_MODEL)
    parser.add_argument("--request-count", type=int, default=_DEFAULT_REQUESTS)
    parser.add_argument("--input-tokens", type=int, default=_DEFAULT_INPUT_TOKENS)
    parser.add_argument("--output-tokens", type=int, default=_DEFAULT_OUTPUT_TOKENS)
    parser.add_argument("--concurrency", type=int, default=_DEFAULT_CONCURRENCY)
    args = parser.parse_args()
    if min(args.request_count, args.input_tokens, args.output_tokens, args.concurrency) <= 0:
        parser.error("request count, token lengths, and concurrency must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
