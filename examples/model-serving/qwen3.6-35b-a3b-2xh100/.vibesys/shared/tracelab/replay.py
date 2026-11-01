"""Replay the pinned TraceLab recorded sample with trusted Request Factory.

Public API: ``summary_metrics`` validates a completed replay before translating
its native measured throughput and timing percentiles into VibeSys objectives.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import secrets
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

MODEL = "Qwen/Qwen3.6-35B-A3B"
REVISION = "995ad96eacd98c81ed38be0c5b274b04031597b0"
TRACE = Path(__file__).with_name("recorded_sessions.csv")
TRACE_SHA256 = "b49b5e63e53e946e54b068654cfa07795a76e1a2d90abe35c7ebc6ecb15a707b"
METRICS = {
    "output_tokens_per_s": "output_token_throughput_per_s",
    "ttft_p50_ms": "ttft_ms_p50",
    "ttft_p90_ms": "ttft_ms_p90",
    "tpot_p50_ms": "tpot_ms_p50",
    "tpot_p90_ms": "tpot_ms_p90",
}


def summary_metrics(summary: object, rounds: int, output_tokens: int) -> dict[str, float]:
    """Reject incomplete, failed or malformed replays, then expose five axes."""
    if not isinstance(summary, dict) or not isinstance(summary.get("replay"), dict):
        raise ValueError("summary.replay must be an object")
    replay = summary["replay"]
    if replay.get("kind") != "sessions" or not isinstance(replay.get("common"), dict):
        raise ValueError("summary.replay must describe sessions with common metrics")
    common = replay["common"]
    for key, expected in {
        "attempted_steps": rounds,
        "success_steps": rounds,
        "failed_steps": 0,
        "actual_output_tokens": output_tokens,
        "target_output_tokens": output_tokens,
    }.items():
        if type(common.get(key)) is not int or common[key] != expected:
            raise ValueError(f"summary.replay.common.{key} must equal {expected}")
    values = {}
    for name, field in METRICS.items():
        value = common.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"summary.replay.common.{field} must be finite and nonnegative")
        values[name] = float(value)
    if values["output_tokens_per_s"] <= 0:
        raise ValueError("output throughput must be positive")
    return values


def _run(args: argparse.Namespace, temporary: Path) -> dict[str, float]:
    if hashlib.sha256(TRACE.read_bytes()).hexdigest() != TRACE_SHA256:
        raise ValueError("recorded_sessions.csv checksum mismatch")
    with TRACE.open(newline="") as source:
        rows = list(csv.DictReader(source))
    trace = temporary / "trace.csv"
    with trace.open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=["request_id", "arrival_time_ms", *rows[0].keys()]
        )
        writer.writeheader()
        for index, row in enumerate(rows):
            writer.writerow({"request_id": f"round-{index}", "arrival_time_ms": 0, **row})
    tokenizer = temporary / "tokenizer.json"
    with urllib.request.urlopen(
        f"https://huggingface.co/{MODEL}/resolve/{REVISION}/tokenizer.json", timeout=120
    ) as response:
        tokenizer.write_bytes(response.read())
    corpus = temporary / "corpus.txt"
    # TraceLab supplies token lengths/topology, not private developer prompt text.
    # A varied synthetic token pool supplies text without repeating one short prompt.
    nonce = secrets.token_hex(16)
    with corpus.open("w") as stream:
        for index in range(100_000):
            stream.write(
                f"def case_{index}(items):\n    return sorted(set(items))  # {index} {nonce}\n"
            )
    summary = temporary / "summary.json"
    command = [
        args.request_factory_engine,
        "--trace",
        str(trace),
        "--input-file-format",
        "text-generation-session-execution-v2",
        "--text-file",
        str(corpus),
        "--tokenizer",
        str(tokenizer),
        "--token-pool-limit",
        "1000000",
        "--base-url",
        args.url.rstrip("/").removesuffix("/v1") + "/v1",
        "--model",
        MODEL,
        "--backend",
        "openai",
        "--temperature",
        "0",
        "--arrival-mode",
        "saturated",
        "--max-concurrency",
        "8",
        "--max-model-len",
        "262144",
        "--request-log",
        "false",
        "--timeline",
        "false",
        "--summary-path",
        str(summary),
    ]
    # lint-waiver: LW-994901 [S603]; the evaluator injects the trusted binary;
    # argv execution preserves its boundary, whereas a shell adds interpretation.
    subprocess.run(command, check=True, timeout=3000)  # noqa: S603
    return summary_metrics(
        json.loads(summary.read_text()), len(rows), sum(int(row["output_len"]) for row in rows)
    )


def main() -> int:
    """Run one recorded replay; emit a protocol error on any boundary failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--vs-output", type=Path, required=True)
    args = parser.parse_args()
    record: dict[str, Any]
    try:
        with tempfile.TemporaryDirectory(prefix="qwen36-tracelab-") as directory:
            values = _run(args, Path(directory))
        record = {"kind": "result", "label": "", "values": values}
        status = 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        record = {"kind": "error", "message": str(exc)}
        sys.stderr.write(f"TraceLab replay failed: {exc}\n")
        status = 1
    args.vs_output.write_text(json.dumps(record, allow_nan=False) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
