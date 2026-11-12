"""Replay the pinned TraceLab recorded sample with trusted Request Factory.

Public API: ``canonical_rows`` turns the recorded sample into the runner's
session-execution-v2 rows, ``runner_command`` is the one ``session_runner``
invocation a replay makes, ``run_replay`` makes it, and ``summary_metrics``
validates a completed replay before translating its native measured throughput
and timing percentiles into VibeSys objectives.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

MODEL = "Qwen/Qwen3.6-35B-A3B"
REVISION = "995ad96eacd98c81ed38be0c5b274b04031597b0"
TRACE = Path(__file__).with_name("recorded_sessions.csv")
TRACE_SHA256 = "b49b5e63e53e946e54b068654cfa07795a76e1a2d90abe35c7ebc6ecb15a707b"
CONCURRENCY = 8
MAX_MODEL_LEN = 262144
TOKEN_POOL_LIMIT = 1_000_000
CORPUS_LINES = 100_000
RUNNER_TIMEOUT_SECS = 3000  # inside the task's 3600 s benchmark budget
METRICS = {
    "output_tokens_per_s": "output_token_throughput_per_s",
    "ttft_p50_ms": "ttft_ms_p50",
    "ttft_p90_ms": "ttft_ms_p90",
    "tpot_p50_ms": "tpot_ms_p50",
    "tpot_p90_ms": "tpot_ms_p90",
}
# The protocol-2 declaration: exactly the task's five objectives, each with its unit.
HELLO = {
    "kind": "hello",
    "protocol": 2,
    "metrics": {
        name: {"unit": "tok/s", "direction": "max"}
        if name == "output_tokens_per_s"
        else {"unit": "ms", "direction": "min"}
        for name in METRICS
    },
}


def request_id(session_id: str, round_idx: int) -> str:
    """The id the pinned runner requires for one round, and rejects any other.

    Request Factory's ``request_id`` in ``src/schema/format/text_generation/session.rs``
    at the pinned revision: the session id verbatim and the round index within the
    session, zero-padded to six digits.
    """
    return f"session_{session_id}_round_{round_idx:06d}"


def canonical_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Recorded rows with the request ids and zero arrival times the v2 schema needs."""
    return [
        {
            "request_id": request_id(row["session_id"], int(row["round_idx"])),
            "arrival_time_ms": "0",
            **row,
        }
        for row in rows
    ]


def runner_command(
    engine: str, url: str, trace: Path, corpus: Path, tokenizer: Path, summary: Path
) -> list[str]:
    """The saturated session replay at eight active sessions, with no request log."""
    return [
        engine,
        "--trace", str(trace),
        "--input-file-format", "text-generation-session-execution-v2",
        "--text-file", str(corpus),
        "--tokenizer", str(tokenizer),
        "--token-pool-limit", str(TOKEN_POOL_LIMIT),
        "--base-url", url.rstrip("/").removesuffix("/v1") + "/v1",
        "--model", MODEL,
        "--backend", "openai",
        "--temperature", "0",
        "--arrival-mode", "saturated",
        "--max-concurrency", str(CONCURRENCY),
        "--max-model-len", str(MAX_MODEL_LEN),
        "--request-log", "false",
        "--timeline", "false",
        "--summary-path", str(summary),
    ]  # fmt: skip


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


def _tokenizer() -> Path:
    """The exact-revision tokenizer.json from the operator's read-only Hugging Face cache.

    The task image runs offline: the cache is the mount HF_HOME names, laid out as
    huggingface_hub lays it out (hub/models--<org>--<name>/snapshots/<revision>/).
    """
    home = os.environ.get("HF_HOME")
    if not home:
        raise ValueError("HF_HOME is not set: the task needs the Hugging Face cache mount")
    path = (
        Path(home)
        / "hub"
        / f"models--{MODEL.replace('/', '--')}"
        / "snapshots"
        / REVISION
        / "tokenizer.json"
    )
    if not path.is_file():
        raise ValueError(f"{path} is missing from the Hugging Face cache")
    return path


def run_replay(
    args: argparse.Namespace,
    temporary: Path,
    run: Callable[..., object] = subprocess.run,
) -> dict[str, float]:
    """Replay the sample once against ``args.url`` with ``args.request_factory_engine``.

    ``run`` executes the runner command (``subprocess.run`` outside tests).
    """
    if hashlib.sha256(TRACE.read_bytes()).hexdigest() != TRACE_SHA256:
        raise ValueError("recorded_sessions.csv checksum mismatch")
    with TRACE.open(newline="") as source:
        rows = list(csv.DictReader(source))
    trace = temporary / "trace.csv"
    canonical = canonical_rows(rows)
    with trace.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(canonical[0]))
        writer.writeheader()
        writer.writerows(canonical)
    corpus = temporary / "corpus.txt"
    # TraceLab supplies token lengths/topology, not private developer prompt text.
    # A varied synthetic token pool supplies text without repeating one short prompt.
    nonce = secrets.token_hex(16)
    with corpus.open("w") as stream:
        for index in range(CORPUS_LINES):
            stream.write(
                f"def case_{index}(items):\n    return sorted(set(items))  # {index} {nonce}\n"
            )
    summary = temporary / "summary.json"
    command = runner_command(
        args.request_factory_engine, args.url, trace, corpus, _tokenizer(), summary
    )
    # The task image holds the trusted binary; argv execution, never a shell.
    run(command, check=True, timeout=RUNNER_TIMEOUT_SECS)
    return summary_metrics(
        json.loads(summary.read_text()), len(rows), sum(int(row["output_len"]) for row in rows)
    )


def _write_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, allow_nan=False) + "\n")
        output.flush()


def main(
    argv: Sequence[str] | None = None,
    replay: Callable[[argparse.Namespace, Path], dict[str, float]] = run_replay,
) -> int:
    """Run one recorded replay as a protocol-2 stream: hello first, then result or error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    # vibesys-cli gives the candidate's server address in VIBESYS_SERVICE_URL.
    parser.add_argument(
        "--url", default=os.environ.get("VIBESYS_SERVICE_URL", "http://localhost:8000")
    )
    parser.add_argument("--vs-output", type=Path, required=True)
    args = parser.parse_args(argv)
    # Declare the schema before measuring, so a replay killed mid-run reads as no outcome.
    args.vs_output.write_text("", encoding="utf-8")
    _write_record(args.vs_output, HELLO)
    record: dict[str, Any]
    try:
        with tempfile.TemporaryDirectory(prefix="qwen36-tracelab-") as directory:
            values = replay(args, Path(directory))
        record = {"kind": "result", "label": "", "values": values}
        status = 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        record = {"kind": "error", "message": str(exc) or type(exc).__name__}
        sys.stderr.write(f"TraceLab replay failed: {exc}\n")
        status = 1
    _write_record(args.vs_output, record)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
