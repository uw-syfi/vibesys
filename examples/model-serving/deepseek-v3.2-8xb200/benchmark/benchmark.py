#!/usr/bin/env python3
"""Run the fixed DeepSeek-V3.2 workload through Request Factory."""

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

_MODEL = "deepseek-ai/DeepSeek-V3.2"
_REQUESTS = 256
_INPUT_TOKENS = 8192
_OUTPUT_TOKENS = 1024
_CONCURRENCY = 64
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


def _summary_metrics(summary: Mapping[str, Any], expected: int) -> dict[str, float]:
    replay = summary.get("replay")
    common = replay.get("common") if isinstance(replay, Mapping) else None
    if not isinstance(common, Mapping):
        raise ValueError("RF summary is missing replay.common")
    for key, value in {
        "failed_steps": 0,
        "attempted_steps": expected,
        "success_steps": expected,
        "output_mismatch_steps": 0,
    }.items():
        actual = common.get(key)
        if actual != value:
            raise ValueError(f"RF summary {key}={actual!r}, expected {value}")

    raw_values = {
        "output_token_throughput_per_s": common.get("output_token_throughput_per_s"),
        "p90_latency_ms": common.get("total_duration_ms_p90"),
    }
    values: dict[str, float] = {}
    for name, raw in raw_values.items():
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise ValueError(f"RF summary metric {name} is missing or not numeric: {raw!r}")
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"RF summary metric {name} is invalid: {raw!r}")
        values[name] = value
    if values["output_token_throughput_per_s"] <= 0:
        raise ValueError("RF output-token throughput must be positive")
    return values


def run(args: argparse.Namespace) -> int:
    output_path = Path(args.vs_output) if args.vs_output else None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        _write_record(output_path, {"kind": "hello", "protocol": 2, "metrics": _METRICS})
    try:
        with tempfile.TemporaryDirectory(prefix="vibesys-rf-deepseek-") as directory:
            temporary = Path(directory)
            trace = temporary / "requests.csv"
            summary_path = temporary / "summary.json"
            _write_trace(trace, args.request_count, args.input_tokens, args.output_tokens)
            command = [
                args.request_factory_engine,
                "--trace",
                str(trace),
                "--input-file-format",
                "text-generation-independent",
                "--text-file",
                str(Path(__file__).with_name("corpus.txt")),
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
            if completed.returncode != 0:
                raise RuntimeError(f"Request Factory exited with status {completed.returncode}")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            metrics = _summary_metrics(summary, args.request_count)
            _write_record(output_path, {"kind": "result", "label": "", "values": metrics})
            print(json.dumps({"metrics": metrics}, sort_keys=True))
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
    parser.add_argument("--request-count", type=int, default=_REQUESTS)
    parser.add_argument("--input-tokens", type=int, default=_INPUT_TOKENS)
    parser.add_argument("--output-tokens", type=int, default=_OUTPUT_TOKENS)
    parser.add_argument("--concurrency", type=int, default=_CONCURRENCY)
    args = parser.parse_args()
    if min(args.request_count, args.input_tokens, args.output_tokens, args.concurrency) <= 0:
        parser.error("request count, token lengths, and concurrency must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
