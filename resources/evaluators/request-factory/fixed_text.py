#!/usr/bin/env python3
"""Run a fixed independent-text workload through Request Factory.

``request-factory-fixed-text-v1`` is the public CLI. Its arguments describe one
saturated workload; this module owns trace/corpus construction, RF summary
validation, and VibeSys result-protocol output.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_POOL_WARNING_PREFIX = "token pool ("
_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_METRICS = {
    "output_token_throughput_per_s": {"unit": "tok/s", "direction": "max"},
    "p90_latency_ms": {"unit": "ms", "direction": "min"},
}


class _FixedTextError(RuntimeError):
    """A configuration, RF execution, or summary-contract failure."""

    @classmethod
    def remote_revision(cls) -> _FixedTextError:
        """Report an absent or mutable remote tokenizer revision."""
        return cls("remote tokenizer requires a full 40-character lowercase revision")

    @classmethod
    def tokenizer_repo(cls, source: str) -> _FixedTextError:
        """Report a noncanonical Hugging Face repository identifier."""
        return cls(f"invalid Hugging Face tokenizer repo: {source!r}")

    @classmethod
    def endpoint(cls) -> _FixedTextError:
        """Report an unsafe Hugging Face endpoint override."""
        return cls("HF_ENDPOINT must be an HTTPS origin without credentials or query")

    @classmethod
    def empty_download(cls, url: str) -> _FixedTextError:
        """Report an empty exact-revision tokenizer response."""
        return cls(f"pinned tokenizer download returned an empty file: {url}")

    @classmethod
    def summary_object(cls) -> _FixedTextError:
        """Report a non-object RF summary."""
        return cls("RF summary must be an object")

    @classmethod
    def summary_replay(cls) -> _FixedTextError:
        """Report a summary for the wrong RF workload family."""
        return cls("RF summary replay must describe independent_requests")

    @classmethod
    def summary_common(cls) -> _FixedTextError:
        """Report an absent common RF summary block."""
        return cls("RF summary is missing replay.common")

    @classmethod
    def summary_count(cls, key: str, actual: object, expected: int) -> _FixedTextError:
        """Report an RF completion-count contract violation."""
        return cls(f"RF summary {key}={actual!r}, expected {expected}")

    @classmethod
    def metric_type(cls, name: str, value: object) -> _FixedTextError:
        """Report an absent or nonnumeric RF metric."""
        return cls(f"RF summary metric {name} is missing or not numeric: {value!r}")

    @classmethod
    def metric_invalid(cls, name: str, value: object) -> _FixedTextError:
        """Report a negative or nonfinite RF metric."""
        return cls(f"RF summary metric {name} is invalid: {value!r}")

    @classmethod
    def throughput(cls) -> _FixedTextError:
        """Report a nonpositive RF output throughput."""
        return cls("RF summary output-token throughput must be positive")

    @classmethod
    def pool_warning(cls) -> _FixedTextError:
        """Report a corpus too short for the requested prompt."""
        return cls("Request Factory token pool is shorter than the longest prompt")

    @classmethod
    def engine_exit(cls, returncode: int) -> _FixedTextError:
        """Report a nonzero RF process exit."""
        return cls(f"Request Factory exited with status {returncode}")


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
    """Write deterministic synthetic text for RF's bounded token pool."""
    with path.open("w", encoding="utf-8") as corpus:
        for start in range(0, token_pool_limit, 128):
            end = min(start + 128, token_pool_limit)
            segments = " ".join(f"segment{index:08d}" for index in range(start, end))
            corpus.write(f"Synthetic benchmark corpus: {segments}.\n")


def _resolve_tokenizer(source: str, revision: str | None) -> Path:
    local = Path(source)
    if local.exists():
        return local
    if revision is None or not _REVISION_PATTERN.fullmatch(revision):
        raise _FixedTextError.remote_revision()
    if not _REPO_PATTERN.fullmatch(source) or ".." in source.split("/"):
        raise _FixedTextError.tokenizer_repo(source)

    hf_home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    snapshot = hf_home / "hub" / ("models--" + source.replace("/", "--")) / "snapshots"
    tokenizer = snapshot / revision / "tokenizer.json"
    if tokenizer.is_file():
        return tokenizer

    tokenizer.parent.mkdir(parents=True, exist_ok=True)
    endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")
    parsed_endpoint = urlparse(endpoint)
    if (
        parsed_endpoint.scheme != "https"
        or not parsed_endpoint.netloc
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise _FixedTextError.endpoint()
    url = f"{endpoint}/{source}/resolve/{revision}/tokenizer.json"
    request = urllib.request.Request(url)  # noqa: S310  # lint-waiver: LW-031729 [S310]; the origin is restricted to validated HTTPS before this request is built.
    # > A higher-level HTTP dependency is unavailable in evaluator target images; accepting
    # > arbitrary schemes would weaken the boundary, so the local validation is kept explicit.
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token and token.strip():
        request.add_header("Authorization", f"Bearer {token.strip()}")
    staging: Path | None = None
    try:
        with (
            urllib.request.urlopen(request, timeout=120) as response,  # noqa: S310  # lint-waiver: LW-031728 [S310]; the validated HTTPS origin downloads one exact-revision tokenizer file.
            # > A requests dependency is unavailable in evaluator target images; allowing arbitrary
            # > URL schemes would weaken this boundary, so endpoint validation rejects them above.
            tempfile.NamedTemporaryFile(dir=tokenizer.parent, delete=False) as temporary,
        ):
            staging = Path(temporary.name)
            while chunk := response.read(1024 * 1024):
                temporary.write(chunk)
        if staging.stat().st_size == 0:
            raise _FixedTextError.empty_download(url)
        staging.replace(tokenizer)
    finally:
        if staging is not None and staging.exists():
            staging.unlink()
    return tokenizer


def _summary_metrics(summary: object, expected_requests: int) -> dict[str, float]:
    if not isinstance(summary, Mapping):
        raise _FixedTextError.summary_object()
    replay = summary.get("replay")
    if not isinstance(replay, Mapping) or replay.get("kind") != "independent_requests":
        raise _FixedTextError.summary_replay()
    common = replay.get("common")
    if not isinstance(common, Mapping):
        raise _FixedTextError.summary_common()

    checks = {
        "failed_steps": 0,
        "attempted_steps": expected_requests,
        "success_steps": expected_requests,
        "output_mismatch_steps": 0,
    }
    for key, expected in checks.items():
        actual = common.get(key)
        if actual != expected:
            raise _FixedTextError.summary_count(key, actual, expected)

    values = {
        "output_token_throughput_per_s": common.get("output_token_throughput_per_s"),
        "p90_latency_ms": common.get("total_duration_ms_p90"),
    }
    result: dict[str, float] = {}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _FixedTextError.metric_type(name, value)
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise _FixedTextError.metric_invalid(name, value)
        result[name] = number
    if result["output_token_throughput_per_s"] <= 0:
        raise _FixedTextError.throughput()
    return result


def _check_engine_result(completed: subprocess.CompletedProcess[str]) -> None:
    if _POOL_WARNING_PREFIX in completed.stderr:
        raise _FixedTextError.pool_warning()
    if completed.returncode != 0:
        raise _FixedTextError.engine_exit(completed.returncode)


def run(args: argparse.Namespace) -> int:
    """Execute the configured fixed workload and emit its protocol-v2 outcome."""
    output_path = Path(args.vs_output) if args.vs_output else None
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("", encoding="utf-8")
        _write_record(output_path, {"kind": "hello", "protocol": 2, "metrics": _METRICS})

    try:
        tokenizer = _resolve_tokenizer(args.tokenizer, args.tokenizer_revision)
        with tempfile.TemporaryDirectory(prefix="vibesys-rf-fixed-text-") as directory:
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
                str(tokenizer),
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
            # lint-waiver: LW-031730 [S603]; the trusted evaluator injects the executable path and
            # every argument is passed as an argv element; a shell would weaken this boundary.
            completed = subprocess.run(  # noqa: S603
                command, check=False, text=True, capture_output=True
            )
            if completed.stdout:
                sys.stdout.write(completed.stdout)
            if completed.stderr:
                sys.stderr.write(completed.stderr)
            _check_engine_result(completed)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            values = _summary_metrics(summary, args.request_count)
            _write_record(output_path, {"kind": "result", "label": "", "values": values})
            sys.stdout.write(json.dumps({"metrics": values}, sort_keys=True) + "\n")
            return 0
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-031731 [BLE001]; every boundary failure must become a protocol error record rather than abort result extraction.
        # > Enumerating filesystem, JSON, subprocess, and network exceptions duplicates this one
        # > translation policy; letting them escape would discard actionable evaluator feedback.
        _write_record(output_path, {"kind": "error", "message": str(exc)})
        sys.stderr.write(f"benchmark failed: {exc}\n")
        return 1


def main() -> int:
    """Parse the versioned fixed-text CLI and run it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request-factory-engine", required=True)
    parser.add_argument("--vs-output")
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--model", required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--tokenizer-revision")
    parser.add_argument("--request-count", type=int, required=True)
    parser.add_argument("--input-tokens", type=int, required=True)
    parser.add_argument("--output-tokens", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    args = parser.parse_args()
    if min(args.request_count, args.input_tokens, args.output_tokens, args.concurrency) <= 0:
        parser.error("request count, token lengths, and concurrency must be positive")
    if not Path(args.tokenizer).exists() and args.tokenizer_revision is None:
        parser.error("remote --tokenizer requires --tokenizer-revision")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
