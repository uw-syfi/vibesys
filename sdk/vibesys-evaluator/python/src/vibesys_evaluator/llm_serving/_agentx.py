"""AgentX replay: the pinned harness's command, its export, and the metrics read from it.

A light wrapper over ``agentx-harness`` (AIPerf): it builds the fixed replay
recipe, runs it through the core's harness runner, and reads AIPerf's JSON
export failing closed: the submission must be valid, the error summary must
say explicitly that there were no errors, every unit must be the expected one
and every value finite and non-negative. Other fields of the export are
ignored (AIPerf's export is open to plugin metrics).
"""

from __future__ import annotations

import importlib.metadata
import json
import math
import re
import sys
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from vibesys_evaluator._private.results import Metric
from vibesys_evaluator._private.sample import SampleError, log

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from vibesys_evaluator._private.harness import Runner

#: The distribution the replay runs, and the one version of it this package speaks to.
HARNESS = "agentx-harness"
HARNESS_VERSION = "1.0.7"
#: The public corpus the recipe replays (date-pinned).
DATASET = "semianalysis_cc_traces_weka_062126"
#: The file AIPerf writes the run's summary to, somewhere under its artifact directory.
EXPORT_NAME = "profile_export_aiperf.json"
METRICS: Mapping[str, Metric] = MappingProxyType(
    {
        "output_tokens_per_s": Metric(unit="tok/s", direction="max"),
        "p90_latency_ms": Metric(unit="ms", direction="min"),
        "ttft_p50_ms": Metric(unit="ms", direction="min"),
        "ttft_p90_ms": Metric(unit="ms", direction="min"),
        "tpot_p50_ms": Metric(unit="ms", direction="min"),
        "tpot_p90_ms": Metric(unit="ms", direction="min"),
    }
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_URL = re.compile(r"^https?://[^\s/]+(?:/\S*)?$")


class AgentxError(SampleError):
    """The replay is not a measurement (the message names the field or step)."""


@dataclass(frozen=True, kw_only=True)
class Replay:
    """One AgentX replay: the task's policy (model, load) and where the server is."""

    model: str
    tokenizer: str
    tokenizer_revision: str
    url: str
    concurrency: int
    max_context_length: int
    mode: Literal["full", "smoke"]
    timeout_secs: float

    def __post_init__(self) -> None:
        """Refuse a replay the harness would run with a meaning the task did not intend."""
        for name in ("model", "tokenizer"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                msg = f"agentx.replay {name} must be a non-empty string, got {value!r}"
                raise ValueError(msg)
        if not isinstance(self.tokenizer_revision, str) or not _REVISION.match(
            self.tokenizer_revision
        ):
            msg = (
                "agentx.replay tokenizer_revision must be a 40-hex-digit commit, "
                f"got {self.tokenizer_revision!r}"
            )
            raise ValueError(msg)
        if not isinstance(self.url, str) or not _URL.match(self.url):
            msg = f"agentx.replay url must be an http(s) URL, got {self.url!r}"
            raise ValueError(msg)
        for name in ("concurrency", "max_context_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                msg = f"agentx.replay {name} must be an int >= 1, got {value!r}"
                raise ValueError(msg)
        if self.mode not in ("full", "smoke"):
            msg = f"agentx.replay mode is 'full' or 'smoke', got {self.mode!r}"
            raise ValueError(msg)
        if (
            isinstance(self.timeout_secs, bool)
            or not isinstance(self.timeout_secs, int | float)
            or not math.isfinite(self.timeout_secs)
            or self.timeout_secs <= 0
        ):
            msg = (
                f"agentx.replay timeout_secs must be a finite number > 0, got {self.timeout_secs!r}"
            )
            raise ValueError(msg)


def command(replay: Replay, artifacts: Path, *, python: str = sys.executable) -> list[str]:
    """The fixed AgentX recipe; only smoke shortens it and relaxes its validity rules."""
    full = replay.mode == "full"
    argv = [
        python,
        "-I",
        "-m",
        "aiperf",
        "profile",
        "--scenario",
        "agentx",
        "--model",
        replay.model,
        "--tokenizer",
        replay.tokenizer,
        "--tokenizer-revision",
        replay.tokenizer_revision,
        "--url",
        replay.url,
        "--public-dataset",
        DATASET,
        "--concurrency",
        str(replay.concurrency),
        "--max-context-length",
        str(replay.max_context_length),
        "--benchmark-duration",
        "900" if full else "60",
        "--random-seed",
        "42",
        "--trajectory-start-min-ratio",
        "0.25",
        "--trajectory-start-max-ratio",
        "0.75",
        "--warmup-requests-per-lane",
        "10" if full else "1",
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
    if not full:
        argv.extend(["--num-dataset-entries", "2", "--unsafe-override"])
    return argv


def _number(summary: Mapping[str, Any], metric: str, statistic: str, unit: str) -> float:
    row = summary.get(metric)
    if not isinstance(row, dict) or row.get("unit") != unit:
        msg = f"AgentX export {metric}.unit must be {unit!r}"
        raise AgentxError(msg)
    value = row.get(statistic)
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"AgentX export {metric}.{statistic} must be a number, got {value!r}"
        raise AgentxError(msg)
    if not math.isfinite(value) or value < 0:
        msg = f"AgentX export {metric}.{statistic} must be finite and non-negative, got {value!r}"
        raise AgentxError(msg)
    return float(value)


def summary_metrics(summary: Mapping[str, Any], *, smoke: bool) -> dict[str, float]:
    """The metrics of ``METRICS`` from AIPerf's export, or ``AgentxError`` naming what is wrong.

    A smoke run is diagnostic: it is not held to the submission-validity rules.
    """
    metadata = summary.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("scenario") != "agentx":
        msg = "AgentX export metadata.scenario must be 'agentx'"
        raise AgentxError(msg)
    if not smoke and metadata.get("submission_valid") is not True:
        msg = f"AgentX submission invalid: {metadata.get('submission_invalid_reasons')}"
        raise AgentxError(msg)
    if summary.get("was_cancelled") is True:
        msg = "AgentX run was cancelled"
        raise AgentxError(msg)
    if summary.get("error_summary") != []:
        msg = "AgentX export error_summary must be an explicit empty list (no errors)"
        raise AgentxError(msg)
    # AIPerf's exclude_unset/exclude_none export omits an unused error counter; that is read
    # as zero only beside the explicit empty error summary checked above.
    if (
        summary.get("error_request_count") is not None
        and _number(summary, "error_request_count", "avg", "requests") != 0
    ):
        msg = "AgentX measured requests failed"
        raise AgentxError(msg)
    if _number(summary, "request_count", "avg", "requests") <= 0:
        msg = "AgentX completed no measured requests"
        raise AgentxError(msg)
    throughput = _number(summary, "output_token_throughput", "avg", "tokens/sec")
    if throughput <= 0:
        msg = "AgentX output throughput must be positive"
        raise AgentxError(msg)
    return {
        "output_tokens_per_s": throughput,
        "p90_latency_ms": _number(summary, "request_latency", "p90", "ms"),
        "ttft_p50_ms": _number(summary, "time_to_first_token", "p50", "ms"),
        "ttft_p90_ms": _number(summary, "time_to_first_token", "p90", "ms"),
        "tpot_p50_ms": _number(summary, "inter_token_latency", "p50", "ms"),
        "tpot_p90_ms": _number(summary, "inter_token_latency", "p90", "ms"),
    }


def installed_harness() -> str | None:
    """The installed version of the harness distribution, or None when it is not installed."""
    try:
        return importlib.metadata.version(HARNESS)
    except importlib.metadata.PackageNotFoundError:
        return None


def check_harness(installed: str | None) -> None:
    """Refuse to replay with any harness but the pinned one (a different one measures differently)."""
    if installed is None:
        msg = (
            f"{HARNESS} is not installed: build the task image FROM vibesys-evaluator-llm-serving, "
            f"which pins {HARNESS}=={HARNESS_VERSION}"
        )
        raise AgentxError(msg)
    if installed != HARNESS_VERSION:
        msg = (
            f"{HARNESS} {installed} is installed, but this vibesys-evaluator replays only with "
            f"{HARNESS_VERSION}: use the vibesys-evaluator-llm-serving image of this version "
            "and do not install the harness yourself"
        )
        raise AgentxError(msg)


def read_export(artifacts: Path) -> dict[str, Any]:
    """The one export AIPerf wrote under ``artifacts``."""
    exports = sorted(artifacts.rglob(EXPORT_NAME))
    if len(exports) != 1:
        msg = f"AgentX expected one {EXPORT_NAME} under {artifacts}, found {len(exports)}"
        raise AgentxError(msg)
    try:
        summary = json.loads(exports[0].read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        msg = f"AgentX export {exports[0]} is not readable JSON: {error}"
        raise AgentxError(msg) from error
    if not isinstance(summary, dict):
        msg = f"AgentX export {exports[0]} must be a JSON object"
        raise AgentxError(msg)
    return summary


def run(
    replay: Replay,
    artifacts: Path,
    *,
    runner: Runner,
    installed: str | None,
    python: str = sys.executable,
) -> dict[str, float]:
    """Replay into ``artifacts`` (empty or absent) and read the metrics from the export."""
    check_harness(installed)
    if artifacts.exists() and any(artifacts.iterdir()):
        msg = f"AgentX artifact directory must be empty: {artifacts}"
        raise AgentxError(msg)
    artifacts.mkdir(parents=True, exist_ok=True)
    argv: Sequence[str] = command(replay, artifacts, python=python)
    log(f"agentx: {replay.mode} replay of {replay.model} at {replay.url}")
    runner(argv, timeout_secs=replay.timeout_secs, cwd=artifacts)
    values = summary_metrics(read_export(artifacts), smoke=replay.mode == "smoke")
    log("agentx: " + ", ".join(f"{name} {value:.3f}" for name, value in values.items()))
    return values
