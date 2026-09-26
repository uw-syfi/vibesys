#!/usr/bin/env python3
"""Run the Trainium input-length/concurrency matrix through Request Factory."""

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

_MODEL = "NousResearch/Meta-Llama-3-8B-Instruct"
_METRICS = {"aggregate_throughput": {"unit": "tok/s", "direction": "max"}}


def _write_trace(path: Path, count: int, length: int) -> None:
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(("id", "arrival_time", "input_len", "output_len"))
        for index in range(count):
            writer.writerow((f"request-{index:05d}", 0, length, length))


def _run_point(args: argparse.Namespace, length: int, concurrency: int) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="llama3-trn2-rf-point-") as directory:
        temporary = Path(directory)
        trace, summary_path, log_path = (
            temporary / "requests.csv",
            temporary / "summary.json",
            temporary / "requests.jsonl",
        )
        _write_trace(trace, args.request_count, length)
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
            str(args.temperature),
            "--arrival-mode",
            "saturated",
            "--max-concurrency",
            str(concurrency),
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
        if completed.returncode != 0:
            raise RuntimeError(f"Request Factory exited with status {completed.returncode}")
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        replay = summary.get("replay")
        common = replay.get("common") if isinstance(replay, Mapping) else None
        if not isinstance(common, Mapping):
            raise ValueError("RF summary is missing replay.common")
        expected_counts = {
            "attempted_steps": args.request_count,
            "success_steps": args.request_count,
            "failed_steps": 0,
            "output_mismatch_steps": 0,
        }
        for name, expected in expected_counts.items():
            actual = common.get(name)
            if actual != expected:
                raise ValueError(f"RF summary {name}={actual!r}, expected {expected}")
        throughput = common.get("output_token_throughput_per_s")
        if isinstance(throughput, bool) or not isinstance(throughput, int | float):
            raise ValueError(f"RF throughput is missing or not numeric: {throughput!r}")
        throughput = float(throughput)
        if not math.isfinite(throughput) or throughput <= 0:
            raise ValueError(f"RF throughput is invalid: {throughput!r}")
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        if len(rows) != args.request_count:
            raise ValueError(
                f"RF request log has {len(rows)} records, expected {args.request_count}"
            )
        for row in rows:
            source, outcome = row.get("source", {}).get("data", {}), row.get("outcome", {})
            if source.get("input_len") != length or source.get("output_len_target") != length:
                raise ValueError(f"RF request log has wrong trace lengths: {source!r}")
            if outcome.get("error") is not None or outcome.get("output_len_actual") != length:
                raise ValueError(f"RF request did not complete at target length: {outcome!r}")
        return {
            "input_len": length,
            "output_len": length,
            "concurrency": concurrency,
            "request_count": args.request_count,
            "output_token_throughput_per_s": throughput,
            "request_throughput_per_s": common.get("request_throughput_per_s"),
            "ttft_ms_p50": common.get("ttft_ms_p50"),
            "ttft_ms_p90": common.get("ttft_ms_p90"),
            "tpot_ms_p50": common.get("tpot_ms_p50"),
            "tpot_ms_p90": common.get("tpot_ms_p90"),
        }


def run(args: argparse.Namespace) -> int:
    result_path = Path(args.vs_output) if args.vs_output else None
    if result_path:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps({"kind": "hello", "protocol": 2, "metrics": _METRICS}) + "\n",
            encoding="utf-8",
        )
    try:
        scenarios = [
            _run_point(args, length, concurrency)
            for length in args.lengths
            for concurrency in args.concurrencies
        ]
        best = max(scenarios, key=lambda row: row["output_token_throughput_per_s"])
        values = {"aggregate_throughput": best["output_token_throughput_per_s"]}
        result = {
            "aggregate_throughput": values["aggregate_throughput"],
            "peak_scenario": {key: best[key] for key in ("input_len", "concurrency")},
            "scenarios": scenarios,
        }
        if args.output_json:
            Path(args.output_json).write_text(
                json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8"
            )
        if result_path:
            with result_path.open("a", encoding="utf-8") as output:
                output.write(
                    json.dumps(
                        {
                            "kind": "result",
                            "label": "",
                            "values": values,
                            "artifacts": {"scenarios": scenarios},
                            "metadata": {"peak_scenario": result["peak_scenario"]},
                            "unit": "tok/s",
                        },
                        allow_nan=False,
                    )
                    + "\n"
                )
        print(json.dumps(result, allow_nan=False), flush=True)
        return 0
    except Exception as exc:
        if result_path:
            with result_path.open("a", encoding="utf-8") as output:
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
    parser.add_argument("--tokenizer", default=_MODEL)
    parser.add_argument("--request-count", type=int, default=64)
    parser.add_argument(
        "--lengths",
        type=lambda raw: tuple(int(value) for value in raw.split(",")),
        default=(128, 256, 512),
    )
    parser.add_argument(
        "--concurrencies",
        type=lambda raw: tuple(int(value) for value in raw.split(",")),
        default=(1, 2, 4, 8),
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--output-json")
    parser.add_argument("--vs-output")
    args = parser.parse_args()
    if args.request_count < 1 or any(value < 1 for value in (*args.lengths, *args.concurrencies)):
        parser.error("request count, lengths, and concurrency values must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
