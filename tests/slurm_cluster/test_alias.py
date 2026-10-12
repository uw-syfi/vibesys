"""The deprecated ``--run-environment slurm-gpu`` still works: opening it and a GPU command (smoke)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.harness import parse_env

if TYPE_CHECKING:
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def test_opening_the_alias_does_not_log_the_run_level_deprecation(alias_run: OpenRun) -> None:
    """The launching run reports the deprecation once; every open of the environment would repeat it."""
    assert [line for line in alias_run.log_lines if "deprecated" in line] == []


def test_the_aliass_gpu_command_runs_on_the_compute_node_with_the_requested_gpus(
    alias_run: OpenRun,
) -> None:
    result = alias_run.agent("\"$VIBESYS_GPU\" --gpus 2 -- sh -c 'hostname; env'")

    assert result.exit_code == 0, result.output
    assert "node1" in result.output.splitlines()
    devices = parse_env(result.output)["CUDA_VISIBLE_DEVICES"].split(",")
    assert len([device for device in devices if device]) == 2
