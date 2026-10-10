"""Planned gates run; anything else is refused; the benchmark result is relayed (D18)."""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.conftest import expect_failure_for
from tests.slurm_cluster.harness import CONTAINER_TMP, open_run

if TYPE_CHECKING:
    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


def _framework_result() -> str:
    return f"{CONTAINER_TMP}/vibesys-framework-benchmark-{secrets.token_hex(8)}.json"


def test_the_agent_finds_its_gate_client_by_name_in_any_shell_and_runs_it(run: OpenRun) -> None:
    """Regression for #1646: `which vibesys-gate` was empty; only the absolute path worked."""
    name = Path(run.launcher()).name

    for shell in ("sh -c", "bash -lc"):
        found = run.agent(f"{shell} 'command -v {name}'")
        assert found.exit_code == 0, found.output
        assert found.output.strip() == f"/usr/local/bin/{name}"

    gate = run.agent(f"{name} --gate accuracy")
    assert gate.exit_code == 0, gate.output
    assert "accuracy ok" in gate.output


def test_a_benchmark_result_written_under_tmp_is_relayed_to_the_agents_tmp(
    run: OpenRun, request: pytest.FixtureRequest
) -> None:
    expect_failure_for(request, run, "slurm-gpu", 1601)
    run.set_mode("benchmark", "pass")
    result_path = _framework_result()

    gate = run.gate("benchmark", "--output", result_path)

    assert gate.exit_code == 0, gate.output
    # The file is in the agent container's /tmp, never in the host's.
    assert not Path(result_path).exists()
    relayed = json.loads(run.agent(f"cat {result_path}").output)
    assert relayed["score"] == 42


def test_a_failed_benchmark_still_relays_the_result_it_wrote(
    run: OpenRun, request: pytest.FixtureRequest
) -> None:
    expect_failure_for(request, run, "slurm-gpu", 1601)
    run.set_mode("benchmark", "exit:3")
    result_path = _framework_result()

    gate = run.gate("benchmark", "--output", result_path)

    assert gate.exit_code == 3, gate.output
    assert json.loads(run.agent(f"cat {result_path}").output)["score"] == 42


def test_a_workspace_benchmark_result_lands_in_the_shared_workspace(run: OpenRun) -> None:
    run.set_mode("benchmark", "pass")
    name = f".vibesys-benchmark-{secrets.token_hex(4)}.json"

    gate = run.gate("benchmark", "--output", name)

    assert gate.exit_code == 0, gate.output
    assert json.loads((run.workspace / name).read_text(encoding="utf-8"))["score"] == 42


@pytest.mark.parametrize(
    "arguments",
    [
        ("--output", "/etc/passwd"),
        ("--output", f"{CONTAINER_TMP}/not-the-framework-name.json"),
        ("--output", f"{CONTAINER_TMP}/vibesys-framework-benchmark-../x.json"),
        ("--output", ".vibesys-benchmark-/../escape.json"),
        ("--unplanned-flag", "x"),
        (),
    ],
)
def test_a_benchmark_with_arguments_the_plan_does_not_allow_is_refused_without_a_job(
    run: OpenRun, slurm_cluster: SlurmCluster, arguments: tuple[str, ...]
) -> None:
    gate = run.gate("benchmark", *arguments)

    assert gate.exit_code == 2, gate.output
    assert "invalid arguments" in gate.output
    assert slurm_cluster.queue() == []


@pytest.mark.parametrize("arguments", [("extra",), ("--output", ".vibesys-benchmark-x.json")])
def test_an_accuracy_gate_takes_no_arguments(
    run: OpenRun, slurm_cluster: SlurmCluster, arguments: tuple[str, ...]
) -> None:
    gate = run.gate("accuracy", *arguments)

    assert gate.exit_code == 2, gate.output
    assert slurm_cluster.queue() == []


def test_a_gate_kind_the_client_does_not_know_is_refused(run: OpenRun) -> None:
    gate = run.gate("profile")

    assert gate.exit_code == 2, gate.output


@pytest.mark.parametrize("kind", ["slurm", "slurm-gpu"])
def test_a_gate_the_task_did_not_plan_is_not_offered_and_not_runnable(
    kind: str, slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_run(kind, slurm_cluster, workdir, agent_image_id, accuracy=False) as run:
        assert run.session.view.paths.accuracy_command is None
        assert run.session.view.paths.benchmark_command is not None

        gate = run.gate("accuracy")

        assert gate.exit_code != 0, gate.output
        assert "accuracy ok" not in gate.output
        assert slurm_cluster.queue() == []


def test_steering_a_gate_mode_for_the_next_test_to_trip_over(run: OpenRun) -> None:
    """Leaves `hold` behind on purpose; the next test proves it does not survive."""
    run.set_mode("accuracy", "hold")
    run.set_mode("benchmark", "exit:3")


def test_a_mode_steered_by_an_earlier_test_does_not_reach_this_ones_gate(run: OpenRun) -> None:
    """Regression: `hold` set by a cancellation test made the next test's gate block forever.

    The run is shared and its mode files lived on in the workspace; the isolation
    fixture removes them after every test.
    """
    gate = run.gate("accuracy")

    assert gate.exit_code == 0, gate.output
