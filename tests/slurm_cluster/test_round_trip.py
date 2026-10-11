"""Agent command -> host broker -> real Slurm job -> output and exit status back."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


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
