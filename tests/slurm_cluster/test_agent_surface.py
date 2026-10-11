"""What the agent can reach for trusted evaluation depends on its loop (#1649).

The dynamic loop's agents have the evaluation tool and so no shell gate; the older loops'
agents keep the gate until each is ported to the core path (roadmap #1952).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.slurm_cluster.harness import ALL_KINDS, open_dynamic_run

if TYPE_CHECKING:
    from pathlib import Path

    from tests.slurm_cluster.cluster import SlurmCluster
    from tests.slurm_cluster.harness import OpenRun

pytestmark = pytest.mark.slurm_cluster


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_agents_with_the_evaluation_tool_have_no_gate_client_and_no_evaluation_commands(
    kind: str, slurm_cluster: SlurmCluster, workdir: Path, agent_image_id: str
) -> None:
    with open_dynamic_run(kind, slurm_cluster, workdir, agent_image_id) as run:
        assert run.session.view.paths.accuracy_command is None
        assert run.session.view.paths.benchmark_command is None
        assert "--gate" not in run.session.view.prompt_notes
        assert run.agent("command -v vibesys-gate").exit_code != 0
        found = run.agent("command -v vibesys-gpu")
        assert (found.exit_code == 0) is run.has_gpu_commands, found.output
        if run.has_gpu_commands:
            refused = run.agent("vibesys-gpu --gate accuracy")
            assert refused.exit_code != 0, refused.output
            assert "accuracy ok" not in refused.output
            assert slurm_cluster.queue() == []


def test_an_older_loops_agent_keeps_the_gate_client(run: OpenRun) -> None:
    assert run.session.view.paths.accuracy_command is not None
    assert run.session.view.paths.benchmark_command is not None
    assert run.agent(f"command -v {run.launcher().rsplit('/', 1)[-1]}").exit_code == 0
