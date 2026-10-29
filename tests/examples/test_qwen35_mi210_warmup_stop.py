"""The stop of a warmup that cannot finish, parsed from session_runner's recorded stderr.

Regression for r19: ten warmups stopped at 7 to 16 tokens/s, all with no partial measurement,
so agents saw only the prose of the stop. ``golden/warmup_stop.stderr`` is what r19's eval_eb00846
printed up to the stop (a workload header and a progress line every 5 s from a real machine). The
bundle's own watch decides from those lines when to stop, and its parse of the same text becomes
the structured partial measurement the framework's reader accepts: the achieved rate, the rate
that finishes in time and the rounds done out of the rounds required. How the framework carries
that partial measurement to the planner is tested in ``libs/vs-runtime`` and
``tests/vibesys/orchestration/dynamic``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.examples.example_task import task_root

from vs_evaluator_protocol.api import (
    ErrorKind,
    Measurement,
    PartialMeasurement,
    Progress,
    parse_records,
    read_measurement,
)

if TYPE_CHECKING:
    from types import ModuleType

_PROJECT = Path(__file__).parents[2] / "examples" / "model-serving" / "qwen3.5-9b-mi210"
_GOLDEN = Path(__file__).with_name("golden") / "warmup_stop.stderr"


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the bundle's stdlib-only benchmark/run.py as a module."""
    name = "qwen35_mi210_run_warmup_stop"
    spec = importlib.util.spec_from_file_location(
        name, task_root(_PROJECT) / "benchmark" / "run.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    # test-isolation: registering the script's module name is how a loaded script is made importable; it sets up the input, not a stand-in.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _printed_until_the_stop(run: ModuleType) -> tuple[str, str]:
    """What the recorded run printed up to the line the watch stopped it at, and why it stopped."""
    watch = run.WarmupWatch(
        run.WARMUP_TIMEOUT_S, run.WARMUP_SESSION_CEILING_TOK_S, label="warmup sub-run"
    )
    printed: list[str] = []
    for line in _GOLDEN.read_text(encoding="utf-8").splitlines(keepends=True):
        printed.append(line)
        if (why := watch.feed(line)) is not None:
            return "".join(printed), why
    message = "the recorded warmup was never stopped"
    raise AssertionError(message)


def test_the_recorded_warmup_is_stopped_and_reported_as_the_rate_it_achieved(
    run: ModuleType, tmp_path: Path
) -> None:
    printed, why = _printed_until_the_stop(run)
    stream = tmp_path / "result.jsonl"
    error = run.HarnessError(
        why, run.partial_measurement(printed, run.WARMUP_TIMEOUT_S, "warmup sub-run")
    )

    run.ProtocolReport(stream).fail(str(error), error.partial)

    measurement: Measurement = read_measurement(parse_records(stream.read_text(encoding="utf-8")))
    assert measurement.failure is not None
    assert "warmup sub-run stopped at 176s" in measurement.failure
    # eval_eb00846: 1246 output tokens in 176 s, 9 of 72 rounds, against 14343 tokens in 180 s.
    assert measurement.partial == PartialMeasurement(
        name="warmup_output_tokens_per_s",
        value=1246 / 176,
        direction="max",
        unit="output tokens/s",
        target=14343 / 180,
        progress=Progress(completed=9, required=72, unit="rounds"),
    )


def test_a_preflight_refusal_reaches_the_reader_as_an_unmet_prefix_cache(
    run: ModuleType, tmp_path: Path
) -> None:
    """The reference engine has no prefix cache, so its baseline fails the same way every time."""
    stream = tmp_path / "result.jsonl"
    error = run.warmup_failure("full", "error: prefix-cache preflight: cached_tokens=0")

    run.ProtocolReport(stream).fail(str(error), error.partial, error.requirement)

    measurement = read_measurement(parse_records(stream.read_text(encoding="utf-8")))
    assert measurement.failed
    assert measurement.error_kind is ErrorKind.REQUIREMENT_NOT_MET
    assert measurement.requirement == "prefix_cache"


def test_any_other_warmup_failure_reaches_the_reader_as_transient(
    run: ModuleType, tmp_path: Path
) -> None:
    stream = tmp_path / "result.jsonl"
    error = run.warmup_failure("full", "connection reset by peer")

    run.ProtocolReport(stream).fail(str(error), error.partial, error.requirement)

    measurement = read_measurement(parse_records(stream.read_text(encoding="utf-8")))
    assert measurement.error_kind is ErrorKind.TRANSIENT
    assert measurement.requirement is None
