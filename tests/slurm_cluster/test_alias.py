"""The deprecated ``--run-environment slurm-gpu`` still works: its gates and a GPU command (smoke)."""

from __future__ import annotations

import json
import secrets
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.harness import CONTAINER_TMP, parse_env

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def test_opening_the_alias_does_not_log_the_run_level_deprecation(alias_run: OpenRun) -> None:
    """The launching run reports the deprecation once; every open of the environment would repeat it."""
    assert [line for line in alias_run.log_lines if "deprecated" in line] == []


def test_the_aliass_gates_run_as_jobs_staged_into_its_stage_root(
    alias_run: OpenRun, slurm_cluster: SlurmCluster
) -> None:
    alias_run.set_mode("accuracy", "pass")

    for _ in range(2):
        accuracy = alias_run.gate("accuracy")
        assert accuracy.exit_code == 0, accuracy.output
        assert "accuracy ok" in accuracy.output
    stage = slurm_cluster.root / "alias-home" / ".cache" / "vibesys" / "slurm-gpu-stage"
    # Runs sharing the root stage into separate invocation directories.
    invocations = [
        path for path in (stage / "slurm-gpu").iterdir() if (path / "workspace").is_dir()
    ]
    assert len(invocations) >= 2
    assert slurm_cluster.queue() == []


def test_the_aliass_benchmark_result_is_relayed_to_the_agents_tmp(alias_run: OpenRun) -> None:
    alias_run.set_mode("benchmark", "pass")
    result_path = f"{CONTAINER_TMP}/vibesys-framework-benchmark-{secrets.token_hex(8)}.json"

    gate = alias_run.gate("benchmark", "--output", result_path)

    assert gate.exit_code == 0, gate.output
    assert json.loads(alias_run.agent(f"cat {result_path}").output)["score"] == 42


def test_the_aliass_gpu_command_runs_on_the_compute_node_with_the_requested_gpus(
    alias_run: OpenRun,
) -> None:
    result = alias_run.agent("\"$VIBESYS_GPU\" --gpus 2 -- sh -c 'hostname; env'")

    assert result.exit_code == 0, result.output
    assert "node1" in result.output.splitlines()
    devices = parse_env(result.output)["CUDA_VISIBLE_DEVICES"].split(",")
    assert len([device for device in devices if device]) == 2
