"""Shared contract for every :class:`~vibesys.orchestration.gates.GateExecutor`.

Parametrized over the real ``_RealGateExecutor`` (driven through a real
``LocalShellSandbox`` running tiny local commands under ``tmp_path``, so it
never touches a GPU or a container) and
:class:`~vibesys.orchestration.fake_gates.FakeGateExecutor` (in-memory, no
subprocess at all, scripted to the same outcomes). Both satisfy
``GateExecutor`` and are used interchangeably by ``ctx.gates`` (see
``vibesys.orchestration.gates._Evaluator``).

The contract: result types, pass/fail/timeout semantics as observable
through the result (a failing or timed-out command yields a failed result
with feedback, never an exception), ``process_id``/``round_label`` handling,
and benchmark result parsing into declared objectives.
"""

from __future__ import annotations

import contextlib
import sys
from typing import TYPE_CHECKING
from unittest.mock import MagicMock  # test-isolation: inert collaborators below

import pytest

from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkContract,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
)
from vibesys.evaluators.input_manifest import BenchmarkResult
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.events import CoreEvent, CoreEventType
from vibesys.orchestration.fake_gates import FakeGateExecutor
from vibesys.orchestration.gates import _GateInputs, _RealGateExecutor
from vibesys.render.sink import output_sink
from vs_sandbox.fake_sandbox import FakeSandbox
from vs_sandbox.local_shell import LocalShellSandbox

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sandbox.execution import Sandbox

_SCALAR_SPEC = BenchmarkResult(json_argument="--out", metric="tok_per_sec")


def _ctx(
    *, sandbox: Sandbox, accuracy_command: str | None, benchmark_command: str | None
) -> _GateInputs:
    """Bind a `_GateInputs` around *sandbox* with no evaluator-input tampering."""
    # test-isolation: the real executor needs only inert git, events, and view collaborators for this input
    git = MagicMock()
    git.trusted_input_changes.return_value = []
    return _GateInputs(
        # test-isolation: the real executor needs only inert git, events, and view collaborators for this input
        events=MagicMock(),
        judge_backend=sandbox,
        judge_accuracy_command=accuracy_command,
        judge_benchmark_command=benchmark_command,
        # test-isolation: the real executor needs only inert git, events, and view collaborators for this input
        run_environment_view=MagicMock(),
        git=git,
    )


@contextlib.contextmanager
def _captured_gate_events():  # noqa: ANN202  # LW-040119 [ANN202]; the helper is private to this test module and its return type is the local closure type.
    """Collect the process-sink events the real executor publishes."""
    seen: list[CoreEvent] = []
    unsubscribe = output_sink().subscribe(seen.append)
    try:
        yield seen
    finally:
        unsubscribe()


def _gate_round_labels(seen: list[CoreEvent]) -> list[str | None]:
    return [
        event.round_label
        for event in seen
        if event.type in (CoreEventType.GATE_STARTED, CoreEventType.GATE_FINISHED)
    ]


def _writer_command(payload: str, tmp_path: Path, *, fail_after_write: bool = False) -> str:
    """A benchmark command that copies *payload* to the path passed last."""
    source = tmp_path / "payload.json"
    source.write_text(payload)
    tail = "; sys.exit(1)" if fail_after_write else ""
    return (
        f'{sys.executable} -c "import sys, shutil; '
        f"shutil.copyfile('{source}', sys.argv[-1]){tail}\""
    )


# ---------------------------------------------------------------------------
# Executor drivers: build a ready-to-call executor for "real" or "fake".
# ---------------------------------------------------------------------------


def _real_executor() -> _RealGateExecutor:
    return _RealGateExecutor()


def _fake_executor() -> FakeGateExecutor:
    return FakeGateExecutor()


@pytest.mark.parametrize("kind", ["real", "fake"])
class TestGateExecutorContract:
    """Every ``GateExecutor``, probed for the same observable contract."""

    # -- accuracy ------------------------------------------------------

    def test_accuracy_pass_is_executed_with_no_feedback(self, kind: str, tmp_path: Path) -> None:
        if kind == "real":
            executor = _real_executor()
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path), accuracy_command="true", benchmark_command=None
            )
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command="true", benchmark_command=None)
            executor.script_accuracy(
                AccuracyGateResult(
                    command="true", passed=True, output="ok", feedback=None, executed=True
                )
            )

        result = executor.run_accuracy(ctx, process_id="accuracy-1", round_label="round-1")

        assert isinstance(result, AccuracyGateResult)
        assert result.passed is True
        assert result.executed is True
        assert result.feedback is None

    def test_accuracy_fail_yields_failed_result_not_exception(
        self, kind: str, tmp_path: Path
    ) -> None:
        if kind == "real":
            executor = _real_executor()
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path),
                accuracy_command="exit 1",
                benchmark_command=None,
            )
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command="exit 1", benchmark_command=None)
            executor.script_accuracy(
                AccuracyGateResult(
                    command="exit 1",
                    passed=False,
                    output="boom",
                    feedback="Framework accuracy gate failed.\nboom",
                    executed=True,
                )
            )

        result = executor.run_accuracy(ctx, process_id="accuracy-2", round_label="round-1")

        assert isinstance(result, AccuracyGateResult)
        assert result.passed is False
        assert result.executed is True
        assert result.feedback is not None

    def test_accuracy_timeout_yields_failed_result_not_exception(
        self, kind: str, tmp_path: Path
    ) -> None:
        sleeper = f'{sys.executable} -c "import time; time.sleep(5)"'
        if kind == "real":
            executor = _real_executor()
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path),
                accuracy_command=sleeper,
                benchmark_command=None,
            )
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command=sleeper, benchmark_command=None)
            executor.script_accuracy(
                AccuracyGateResult(
                    command=sleeper,
                    passed=False,
                    output="Error: Command timed out after 1 seconds.",
                    feedback="Framework accuracy gate failed.\nError: Command timed out after 1 seconds.",
                    executed=True,
                )
            )

        result = executor.run_accuracy(
            ctx, process_id="accuracy-3", timeout_seconds=1, round_label="round-1"
        )

        assert isinstance(result, AccuracyGateResult)
        assert result.passed is False
        assert result.executed is True
        assert result.feedback is not None

    def test_accuracy_skipped_without_a_configured_command(self, kind: str, tmp_path: Path) -> None:
        if kind == "real":
            executor = _real_executor()
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path), accuracy_command=None, benchmark_command=None
            )
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command=None, benchmark_command=None)
            # Left unscripted: `FakeGateExecutor`'s default result must match
            # the real skip outcome with no explicit scripting required.

        result = executor.run_accuracy(ctx, process_id="accuracy-4", round_label="round-1")

        assert isinstance(result, AccuracyGateResult)
        assert result.executed is False
        assert result.passed is True
        assert result.feedback is None

    def test_accuracy_process_id_and_round_label_are_threaded_through(
        self, kind: str, tmp_path: Path
    ) -> None:
        if kind == "real":
            executor = _real_executor()
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path),
                accuracy_command="exit 1",
                benchmark_command=None,
            )
            with _captured_gate_events() as seen:
                executor.run_accuracy(ctx, process_id="accuracy-5", round_label="round-42")
            assert _gate_round_labels(seen) == ["round-42", "round-42"]
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command="exit 1", benchmark_command=None)
            executor.script_accuracy(
                AccuracyGateResult(
                    command="exit 1", passed=False, output="", feedback="x", executed=True
                )
            )
            executor.run_accuracy(ctx, process_id="accuracy-5", round_label="round-42")
            call = executor.accuracy_calls[-1]
            assert call.process_id == "accuracy-5"
            assert call.round_label == "round-42"

    # -- benchmark -------------------------------------------------------

    def test_benchmark_skipped_without_a_declared_contract(self, kind: str, tmp_path: Path) -> None:
        if kind == "real":
            executor = _real_executor()
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path),
                accuracy_command=None,
                benchmark_command="bench",
            )
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command=None, benchmark_command="bench")
            # Unscripted: the fake's default matches the real "not declared" skip.

        result = executor.run_benchmark(
            ctx,
            contract=BenchmarkContract(),
            space=MetricSpace(),
            process_id="bench-1",
            output_slug="bench-1",
        )

        assert isinstance(result, BenchmarkGateResult)
        assert result.executed is False
        assert result.outcome.feedback is None

    def test_benchmark_pass_parses_the_declared_objective(self, kind: str, tmp_path: Path) -> None:
        objectives = (Objective(name="tok_per_sec", direction="max"),)
        if kind == "real":
            executor = _real_executor()
            writer = _writer_command('{"tok_per_sec": 42.5}', tmp_path)
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path), accuracy_command=None, benchmark_command=writer
            )
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command=None, benchmark_command="bench")
            executor.script_benchmark(
                BenchmarkGateResult(
                    command="bench",
                    output="",
                    executed=True,
                    outcome=FrameworkBenchmarkOutcome(
                        metric_name="tok_per_sec", metric_value=42.5, metric_direction="max"
                    ),
                )
            )

        result = executor.run_benchmark(
            ctx,
            contract=BenchmarkContract(result_spec=_SCALAR_SPEC),
            space=MetricSpace(objectives=objectives),
            process_id="bench-2",
            output_slug="bench-2",
        )

        assert isinstance(result, BenchmarkGateResult)
        assert result.executed is True
        assert result.outcome.feedback is None
        assert result.outcome.metric_name == "tok_per_sec"
        assert result.outcome.metric_value == pytest.approx(42.5)

    def test_benchmark_failure_yields_feedback_not_exception(
        self, kind: str, tmp_path: Path
    ) -> None:
        if kind == "real":
            executor = _real_executor()
            writer = _writer_command('{"tok_per_sec": 42.5}', tmp_path, fail_after_write=True)
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path), accuracy_command=None, benchmark_command=writer
            )
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command=None, benchmark_command="bench")
            executor.script_benchmark(
                BenchmarkGateResult(
                    command="bench",
                    output="benchmark failed",
                    executed=True,
                    outcome=FrameworkBenchmarkOutcome(feedback="Framework benchmark failed.\nboom"),
                )
            )

        result = executor.run_benchmark(
            ctx,
            contract=BenchmarkContract(result_spec=_SCALAR_SPEC),
            space=MetricSpace(),
            process_id="bench-3",
            output_slug="bench-3",
        )

        assert isinstance(result, BenchmarkGateResult)
        assert result.executed is True
        assert result.outcome.feedback is not None
        assert result.outcome.metric_value is None

    def test_benchmark_process_id_and_round_label_are_threaded_through(
        self, kind: str, tmp_path: Path
    ) -> None:
        if kind == "real":
            executor = _real_executor()
            writer = _writer_command('{"tok_per_sec": 1.0}', tmp_path)
            ctx = _ctx(
                sandbox=LocalShellSandbox(tmp_path), accuracy_command=None, benchmark_command=writer
            )
            with _captured_gate_events() as seen:
                executor.run_benchmark(
                    ctx,
                    contract=BenchmarkContract(result_spec=_SCALAR_SPEC),
                    space=MetricSpace(),
                    process_id="bench-4",
                    output_slug="bench-4",
                    round_label="round-7",
                )
            assert _gate_round_labels(seen) == ["round-7", "round-7"]
        else:
            executor = _fake_executor()
            ctx = _ctx(sandbox=FakeSandbox(), accuracy_command=None, benchmark_command="bench")
            executor.script_benchmark(
                BenchmarkGateResult(
                    command="bench",
                    output="",
                    executed=True,
                    outcome=FrameworkBenchmarkOutcome(metric_name="m", metric_value=1.0),
                )
            )
            executor.run_benchmark(
                ctx,
                contract=BenchmarkContract(result_spec=_SCALAR_SPEC),
                space=MetricSpace(),
                process_id="bench-4",
                output_slug="bench-4",
                round_label="round-7",
            )
            call = executor.benchmark_calls[-1]
            assert call.process_id == "bench-4"
            assert call.round_label == "round-7"
