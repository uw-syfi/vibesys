"""Agent command -> host broker -> real Slurm job -> output and exit status back."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


@pytest.mark.parametrize("gate", ["accuracy", "benchmark"])
def test_a_gate_run_by_the_agent_runs_in_a_real_job_and_reports_success(
    run: OpenRun, gate: str
) -> None:
    run.set_mode("accuracy", "pass")
    run.set_mode("benchmark", "pass")
    command = getattr(run.session.view.paths, f"{gate}_command")
    assert command is not None
    suffix = " --output .vibesys-benchmark-ok.json" if gate == "benchmark" else ""

    result = run.agent(command + suffix)

    assert result.exit_code == 0, result.output
    assert ("accuracy ok" if gate == "accuracy" else "benchmark wrote") in result.output


@pytest.mark.parametrize("code", [1, 42])
@pytest.mark.parametrize("gate", ["accuracy", "benchmark"])
def test_a_failing_gate_exit_status_reaches_the_agent_unchanged(
    run: OpenRun, gate: str, code: int
) -> None:
    run.set_mode(gate, f"exit:{code}")
    suffix = ["--output", ".vibesys-benchmark-fail.json"] if gate == "benchmark" else []

    result = run.gate(gate, *suffix)

    assert result.exit_code == code, result.output


@pytest.mark.parametrize("code", [0, 1, 7, 42, 255])
def test_the_agents_gpu_command_returns_its_output_and_exit_status(
    gpu_run: OpenRun, code: int
) -> None:
    result = gpu_run.agent(
        f"\"$VIBESYS_GPU\" -- sh -c 'echo to-stdout; echo to-stderr >&2; exit {code}'"
    )

    assert result.exit_code == code, result.output
    assert "to-stdout" in result.output
    assert "to-stderr" in result.output
    assert "[vibesys-gpu] srun partition=main gpus=1" in result.output


def test_the_agents_gpu_command_streams_arguments_without_shell_reinterpretation(
    gpu_run: OpenRun,
) -> None:
    result = gpu_run.agent('"$VIBESYS_GPU" -- printf "[%s]" "a b" \'$HOME\' "*" ""')

    assert result.exit_code == 0, result.output
    assert result.output.endswith("[a b][$HOME][*][]")
