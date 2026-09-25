"""Host-level `ctx.gates.run` API tests: sequencing, recording, skip behavior.

`ctx.gates.run` (`vibesys.orchestration.runtime._Evaluator.run`) is the one
place accuracy-then-benchmark gate sequencing, reuse-accuracy, the
stub-backend skip, and progress-board recording now live, replacing four
near-identical hand-rolled copies across the agent strategies (see
`vibesys.loops.multi.session`, `.single`, `.profile_multi`,
`.profile_single`). These tests exercise the host mechanics directly through
a fake host (no real subprocess or Git work): each strategy keeps only its
own policy (when official gates are due) and is covered by its own
session-level tests, which mock `ctx.gates.run` itself.

Gate outcomes are written synchronously to the strategy's progress file (see
`vibesys.orchestration.progress_log`), so these tests assert against that
file's rendered content instead of a `GateRecorder` test double.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkGateResult,
    FrameworkBenchmarkOutcome,
)
from vibesys.orchestration.runtime import GateRunResult, _Evaluator

if TYPE_CHECKING:
    from vibesys.orchestration.runtime import RunContext

_ACCURACY_HEADING = re.compile(
    r"## Round (\d+) — Framework accuracy gate \(attempt (\d+)\)\n- \*\*verdict\*\*: (pass|fail)"
)
_BENCHMARK_HEADING = re.compile(
    r"## Round (\d+) — Framework benchmark \(attempt (\d+)\)\n- \*\*verdict\*\*: (pass|fail)"
)


def _accuracy_verdicts(text: str) -> list[tuple[int, int, str]]:
    return [(int(n), int(r), verdict) for n, r, verdict in _ACCURACY_HEADING.findall(text)]


def _benchmark_verdicts(text: str) -> list[tuple[int, int, str]]:
    return [(int(n), int(r), verdict) for n, r, verdict in _BENCHMARK_HEADING.findall(text)]


def _fake_host() -> SimpleNamespace:
    return SimpleNamespace(
        environment=SimpleNamespace(
            view=SimpleNamespace(
                paths=SimpleNamespace(accuracy_command="check", benchmark_command="bench"),
                deployment_release_env_var="RELEASE",
            ),
            reconcile_model_requests=AsyncMock(return_value=None),
        ),
        request=SimpleNamespace(
            input_bundle=SimpleNamespace(benchmark_result=None, benchmark_result_protocol=None)
        ),
        workspaces=SimpleNamespace(root=SimpleNamespace(snapshot=AsyncMock(return_value="rev"))),
    )


def _evaluator() -> tuple[_Evaluator, SimpleNamespace]:
    host = _fake_host()
    evaluator = _Evaluator(cast("RunContext", host))
    return evaluator, host


def test_stub_backend_skips_both_gates_without_recording_or_calling_out(tmp_path: Path) -> None:
    evaluator, _host = _evaluator()
    progress = tmp_path / "progress.md"
    evaluator.check = AsyncMock()
    evaluator.measure = AsyncMock()

    result = asyncio.run(
        evaluator.run(
            round_number=1,
            retry=1,
            commit="a" * 40,
            objectives=(),
            progress_path=progress,
            agent_backend_name="stub",
        )
    )

    assert result == GateRunResult(
        feedback=None, benchmark=FrameworkBenchmarkOutcome(), accuracy_passed=False
    )
    evaluator.check.assert_not_awaited()
    evaluator.measure.assert_not_awaited()
    assert not progress.exists()


def test_resource_reconciliation_failure_stops_before_any_gate(tmp_path: Path) -> None:
    evaluator, host = _evaluator()
    host.environment.reconcile_model_requests.return_value = "model unavailable"
    progress = tmp_path / "progress.md"
    evaluator.check = AsyncMock()
    evaluator.measure = AsyncMock()

    result = asyncio.run(
        evaluator.run(
            round_number=1,
            retry=1,
            commit="a" * 40,
            objectives=(),
            progress_path=progress,
            agent_backend_name="cli",
        )
    )

    assert result.feedback == "model unavailable"
    assert not result.accuracy_passed
    evaluator.check.assert_not_awaited()
    evaluator.measure.assert_not_awaited()
    assert not progress.exists()


def test_accuracy_failure_records_once_and_skips_benchmark(tmp_path: Path) -> None:
    evaluator, _host = _evaluator()
    progress = tmp_path / "progress.md"
    evaluator.check = AsyncMock(
        return_value=AccuracyGateResult(
            command="check", passed=False, output="bad", feedback="accuracy rejected", executed=True
        )
    )
    evaluator.measure = AsyncMock()

    result = asyncio.run(
        evaluator.run(
            round_number=2,
            retry=1,
            commit="a" * 40,
            objectives=(),
            progress_path=progress,
            agent_backend_name="cli",
        )
    )

    assert result.feedback == "accuracy rejected"
    assert not result.accuracy_passed
    evaluator.measure.assert_not_awaited()
    text = progress.read_text()
    accuracy = _accuracy_verdicts(text)
    assert accuracy == [(2, 1, "fail")]
    assert _benchmark_verdicts(text) == []


def test_accuracy_pass_runs_benchmark_and_records_each_once(tmp_path: Path) -> None:
    evaluator, _host = _evaluator()
    progress = tmp_path / "progress.md"
    evaluator.check = AsyncMock(
        return_value=AccuracyGateResult(
            command="check", passed=True, output="ok", feedback=None, executed=True
        )
    )
    outcome = FrameworkBenchmarkOutcome(metric_name="throughput", metric_value=12.0)
    evaluator.measure = AsyncMock(
        return_value=BenchmarkGateResult(
            command="bench", output="ok", executed=True, outcome=outcome
        )
    )

    result = asyncio.run(
        evaluator.run(
            round_number=3,
            retry=1,
            commit="a" * 40,
            objectives=(),
            progress_path=progress,
            agent_backend_name="cli",
        )
    )

    assert result.feedback is None
    assert result.accuracy_passed
    assert result.benchmark is outcome
    text = progress.read_text()
    assert _accuracy_verdicts(text) == [(3, 1, "pass")]
    assert _benchmark_verdicts(text) == [(3, 1, "pass")]
    assert "**throughput**: 12.0" in text


def test_reuse_accuracy_records_reuse_and_skips_check(tmp_path: Path) -> None:
    evaluator, _host = _evaluator()
    progress = tmp_path / "progress.md"
    evaluator.check = AsyncMock()
    evaluator.reuse_accuracy = AsyncMock(
        return_value=AccuracyGateResult(
            command="check", passed=True, output="reused", feedback=None, executed=False
        )
    )
    evaluator.measure = AsyncMock(
        return_value=BenchmarkGateResult(
            command="bench",
            output="ok",
            executed=True,
            outcome=FrameworkBenchmarkOutcome(metric_value=1.0),
        )
    )

    result = asyncio.run(
        evaluator.run(
            round_number=4,
            retry=2,
            commit="a" * 40,
            objectives=(),
            progress_path=progress,
            reuse_accuracy=True,
            agent_backend_name="cli",
        )
    )

    evaluator.check.assert_not_awaited()
    evaluator.reuse_accuracy.assert_awaited_once_with(label="round-4")
    text = progress.read_text()
    assert _accuracy_verdicts(text) == [(4, 2, "pass")]
    assert "Reused" in text
    assert result.accuracy_passed


@given(
    outcomes=st.lists(
        st.tuples(
            st.sampled_from(["stub", "cli"]),
            st.booleans(),
            st.booleans(),
            st.booleans(),
        ),
        min_size=0,
        max_size=15,
    )
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_gates_run_property_records_correspond_1to1_with_outcomes(
    outcomes: list[tuple[str, bool, bool, bool]],
) -> None:
    """For any sequence of gate outcomes, each executed gate is recorded exactly
    once, and the returned result's pass/fail matches what was recorded."""

    async def exercise() -> None:
        progress = Path(tempfile.mkdtemp()) / "progress.md"
        evaluator, host = _evaluator()
        for index, (backend, resource_ok, accuracy_pass, benchmark_pass) in enumerate(outcomes):
            host.environment.reconcile_model_requests.return_value = (
                None if resource_ok else "resource unavailable"
            )
            evaluator.check = AsyncMock(
                return_value=AccuracyGateResult(
                    command="check",
                    passed=accuracy_pass,
                    output="out",
                    feedback=None if accuracy_pass else "accuracy failed",
                    executed=True,
                )
            )
            evaluator.measure = AsyncMock(
                return_value=BenchmarkGateResult(
                    command="bench",
                    output="out",
                    executed=True,
                    outcome=FrameworkBenchmarkOutcome(
                        metric_value=1.0,
                        feedback=None if benchmark_pass else "benchmark failed",
                    ),
                )
            )
            before_accuracy = len(_accuracy_verdicts(progress.read_text())) if progress.exists() else 0
            before_benchmark = (
                len(_benchmark_verdicts(progress.read_text())) if progress.exists() else 0
            )

            result = await evaluator.run(
                round_number=index,
                retry=1,
                commit="a" * 40,
                objectives=(),
                progress_path=progress,
                agent_backend_name=backend,
            )

            text = progress.read_text() if progress.exists() else ""
            accuracy = _accuracy_verdicts(text)
            benchmark = _benchmark_verdicts(text)

            if backend == "stub":
                assert len(accuracy) == before_accuracy
                assert len(benchmark) == before_benchmark
                assert result.feedback is None
                continue
            if not resource_ok:
                assert len(accuracy) == before_accuracy
                assert len(benchmark) == before_benchmark
                assert result.feedback == "resource unavailable"
                continue
            assert len(accuracy) == before_accuracy + 1
            assert accuracy[-1][2] == ("pass" if accuracy_pass else "fail")
            if not accuracy_pass:
                assert len(benchmark) == before_benchmark
                assert result.feedback == "accuracy failed"
                continue
            assert len(benchmark) == before_benchmark + 1
            assert benchmark[-1][2] == ("pass" if benchmark_pass else "fail")
            assert result.feedback == (None if benchmark_pass else "benchmark failed")

    asyncio.run(exercise())
