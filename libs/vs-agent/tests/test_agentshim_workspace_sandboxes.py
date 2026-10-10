"""A container turn runs in the container that mounts the turn's workspace.

Regression for #1552: one launcher serves turns in the root and in candidate
workspaces, and each workspace has its own container. A turn sent to another
workspace's container runs ``docker exec -w`` on a directory that is not there,
which exits 127.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast

from agentshim.testing import (
    ClaudePeerTurn,
    ClaudeStreamPeers,
    FakeExecutor,
)
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.fake_docker_sandbox import FakeDockerSandbox

from vs_agent.api import AgentExecutionPolicy, AgentSessionSpec, AgentTurnRequest
from vs_agent.api.testing import fake_stream_launcher

if TYPE_CHECKING:
    from vs_sandbox.api import DockerSandbox


def _spec(workspace: Path) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider="claude",
        workspace=workspace,
        model="test-model",
        policy=AgentExecutionPolicy(containerized=True),
    )


@settings(max_examples=20, deadline=None)
@given(candidates=st.integers(min_value=1, max_value=4))
def test_each_turn_execs_in_the_container_of_its_workspace(candidates: int) -> None:
    root = FakeDockerSandbox(workspace=Path("/srv/project"), container_id="root-container")
    owned = {
        Path(f"/srv/project/worktrees/c{n}/workspace"): FakeDockerSandbox(
            workspace=Path(f"/srv/project/worktrees/c{n}/workspace"),
            container_id=f"candidate-container-{n}",
        )
        for n in range(candidates)
    }
    executor = FakeExecutor(
        [],
        peers=ClaudeStreamPeers([ClaudePeerTurn(text="ok") for _ in range(candidates + 1)]).build,
    )
    launcher = fake_stream_launcher(
        provider="claude",
        executor=executor,
        sandboxes={"implementer": cast("DockerSandbox", root)},
        workspace_sandboxes=lambda path: cast("DockerSandbox | None", owned.get(path)),
    )

    expected = [root.container_id, *(sandbox.container_id for sandbox in owned.values())]
    for workspace in (root.workspace, *owned):
        launcher.launch(_spec(workspace)).run_turn(AgentTurnRequest(message="go"))

    spawned = [list(spawn.argv) for spawn in executor.spawns]
    assert [next(part for part in argv if part in expected) for argv in spawned] == expected
    assert all(argv[argv.index("-w") + 1] == "/workspace" for argv in spawned)
