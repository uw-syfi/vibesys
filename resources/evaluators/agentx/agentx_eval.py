# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["agentx-harness==1.0.7"]
# ///
"""Pinned AgentX replay and VibeSys protocol-v2 result translation.

Public API: ``parse_args``, ``profile_command``, ``summary_metrics``, ``evaluate``.
Smoke runs exercise the real corpus and server but never emit scored results.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import FrameType

METRICS = {
    "output_tokens_per_s": {"unit": "tok/s", "direction": "max"},
    "p90_latency_ms": {"unit": "ms", "direction": "min"},
    "ttft_p50_ms": {"unit": "ms", "direction": "min"},
    "ttft_p90_ms": {"unit": "ms", "direction": "min"},
    "tpot_p50_ms": {"unit": "ms", "direction": "min"},
    "tpot_p90_ms": {"unit": "ms", "direction": "min"},
}
DATASET = "semianalysis_cc_traces_weka_062126"


class Profile(BaseModel):
    """Validated inputs to the versioned replay recipe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["full", "smoke"] = "full"
    model: str
    tokenizer: str
    tokenizer_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    url: str = "http://localhost:8000"
    concurrency: int = Field(gt=0)
    max_context_length: int = Field(gt=0)
    timeout_seconds: int = Field(default=3300, gt=0)
    vs_output: Path | None = None
    artifact_dir: Path | None = None


def parse_args(argv: Sequence[str] | None = None) -> Profile:
    """Reject unknown CLI options and invalid workload sizes before replay."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("full", "smoke"), default="full")
    parser.add_argument("--model", required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--tokenizer-revision", required=True)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--max-context-length", type=int, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3300)
    parser.add_argument("--vs-output", type=Path)
    parser.add_argument("--artifact-dir", type=Path)
    return Profile.model_validate(vars(parser.parse_args(argv)))


def profile_command(profile: Profile, artifacts: Path) -> list[str]:
    """Build the fixed AgentX recipe; only smoke may relax validity constraints."""
    command = [
        sys.executable,
        "-I",
        "-m",
        "aiperf",
        "profile",
        "--scenario",
        "agentx",
        "--model",
        profile.model,
        "--tokenizer",
        profile.tokenizer,
        "--tokenizer-revision",
        profile.tokenizer_revision,
        "--url",
        profile.url,
        "--public-dataset",
        DATASET,
        "--concurrency",
        str(profile.concurrency),
        "--max-context-length",
        str(profile.max_context_length),
        "--benchmark-duration",
        "900" if profile.mode == "full" else "60",
        "--random-seed",
        "42",
        "--trajectory-start-min-ratio",
        "0.25",
        "--trajectory-start-max-ratio",
        "0.75",
        "--warmup-requests-per-lane",
        "10" if profile.mode == "full" else "1",
        "--warmup-grace-period",
        "900",
        "--benchmark-grace-period",
        "120",
        "--output-artifact-dir",
        str(artifacts),
        "--no-gpu-telemetry",
        "--ui",
        "simple",
    ]
    if profile.mode == "smoke":
        command.extend(["--num-dataset-entries", "2", "--unsafe-override"])
    return command


def _number(summary: Mapping[str, Any], metric: str, statistic: str, unit: str) -> float:
    row = summary.get(metric)
    if not isinstance(row, dict) or row.get("unit") != unit:
        message = f"AgentX {metric}.unit must be {unit!r}"
        raise ValueError(message)
    value = row.get(statistic)
    if isinstance(value, bool) or not isinstance(value, int | float):
        message = f"AgentX {metric}.{statistic} must be a number"
        raise TypeError(message)
    if not math.isfinite(value) or value < 0:
        message = f"AgentX {metric}.{statistic} must be finite and nonnegative"
        raise ValueError(message)
    return float(value)


def summary_metrics(summary: Mapping[str, Any], *, smoke: bool = False) -> dict[str, float]:
    """Validate required fields in AIPerf's extensible export, failing closed.

    Other upstream metrics are intentionally ignored: AIPerf's export schema is
    open to plugin metrics. Required metadata, counts, units and values are checked.
    """
    metadata = summary.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("scenario") != "agentx":
        message = "AgentX metadata.scenario must be 'agentx'"
        raise ValueError(message)
    if not smoke and metadata.get("submission_valid") is not True:
        message = f"AgentX submission invalid: {metadata.get('submission_invalid_reasons')}"
        raise ValueError(message)
    if summary.get("was_cancelled") is True:
        message = "AgentX run was cancelled"
        raise ValueError(message)
    if summary.get("error_summary") != []:
        message = "AgentX error_summary must explicitly report no errors"
        raise ValueError(message)
    # AIPerf's exclude_unset/exclude_none export omits an unused error counter.
    # Only accept that representation with an explicit empty error summary.
    errors = summary.get("error_request_count")
    if errors is not None and _number(summary, "error_request_count", "avg", "requests") != 0:
        message = "AgentX measured requests failed"
        raise ValueError(message)
    if _number(summary, "request_count", "avg", "requests") <= 0:
        message = "AgentX completed no measured requests"
        raise ValueError(message)
    throughput = _number(summary, "output_token_throughput", "avg", "tokens/sec")
    if throughput <= 0:
        message = "AgentX output throughput must be positive"
        raise ValueError(message)
    return {
        "output_tokens_per_s": throughput,
        "p90_latency_ms": _number(summary, "request_latency", "p90", "ms"),
        "ttft_p50_ms": _number(summary, "time_to_first_token", "p50", "ms"),
        "ttft_p90_ms": _number(summary, "time_to_first_token", "p90", "ms"),
        "tpot_p50_ms": _number(summary, "inter_token_latency", "p50", "ms"),
        "tpot_p90_ms": _number(summary, "inter_token_latency", "p90", "ms"),
    }


def _run_harness(command: list[str], timeout: int) -> None:
    # lint-waiver: LW-392001 [S603]; argv starts with this interpreter and a fixed
    # module; shell execution would turn model names and URLs into executable text.
    with subprocess.Popen(  # noqa: S603
        command, cwd=Path(__file__).resolve().parent, start_new_session=True
    ) as process:
        try:
            returncode = process.wait(timeout=timeout)
            if returncode:
                raise subprocess.CalledProcessError(returncode, command)
        finally:
            # AIPerf starts child services. A timeout or interrupted evaluator
            # must release the whole process group, including orphan services.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)


class ProfileError(RuntimeError):
    """The harness could not start, exceeded its deadline, or exited unsuccessfully."""


def _terminate(signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(128 + signum)


def run_profile(command: list[str], timeout: int) -> None:
    """Run the harness with bounded cleanup, translating launcher failures once."""
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        _run_harness(command, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProfileError(str(exc)) from exc
    finally:
        signal.signal(signal.SIGTERM, previous)


def _record(path: Path | None, value: dict[str, Any]) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value, allow_nan=False) + "\n")


def _measure(profile: Profile, runner: Callable[[list[str], int], None]) -> dict[str, float]:
    with tempfile.TemporaryDirectory(prefix="vibesys-agentx-") as temporary:
        artifacts = profile.artifact_dir or Path(temporary) / "artifacts"
        if artifacts.exists() and any(artifacts.iterdir()):
            message = f"AgentX artifact directory must be empty: {artifacts}"
            raise ValueError(message)
        runner(profile_command(profile, artifacts), profile.timeout_seconds)
        exports = list(artifacts.rglob("profile_export_aiperf.json"))
        if len(exports) != 1:
            message = f"AgentX expected one export, found {len(exports)} in {artifacts}"
            raise ValueError(message)
        summary = json.loads(exports[0].read_text(encoding="utf-8"))
        if not isinstance(summary, dict):
            message = "AgentX export must be a JSON object"
            raise TypeError(message)
        return summary_metrics(summary, smoke=profile.mode == "smoke")


def evaluate(profile: Profile, runner: Callable[[list[str], int], None] = run_profile) -> int:
    """Replay through an injected runner and emit a result only for valid full runs."""
    output = profile.vs_output
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("", encoding="utf-8")
        _record(output, {"kind": "hello", "protocol": 2, "metrics": METRICS})
    try:
        values = _measure(profile, runner)
    except (OSError, ValueError, TypeError, ProfileError) as exc:
        _record(output, {"kind": "error", "message": str(exc)})
        sys.stderr.write(f"AgentX evaluation failed: {exc}\n")
        return 1
    else:
        if profile.mode == "smoke":
            _record(output, {"kind": "error", "message": "smoke is diagnostic, unscored"})
            sys.stdout.write(json.dumps({"smoke_metrics": values, "scored": False}) + "\n")
        else:
            _record(output, {"kind": "result", "label": "", "values": values})
            sys.stdout.write(json.dumps({"metrics": values}) + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(evaluate(parse_args()))
