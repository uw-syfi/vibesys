"""The dynamic loop offers the evaluation tool to the roles that measure, and only to them (#1649).

The evaluation tool is the one way a VibeSys agent requests a trusted evaluation. This asks the
run's tool offer, which builds each role's MCP configuration, what a session of each dynamic
role is given.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from tests.support.skeleton_strategy import ATTEMPT, DIGEST
from tests.support.synthesized_evidence import SynthesizedEvidence
from tests.vibesys.orchestration.dynamic.core_policy._inputs import write_project

from vibesys.dynamic_roles import CORE_ROLES, IMPLEMENTER, JUDGE
from vs_core.api import ArtifactId, ArtifactRef, Scope
from vs_evaluation.api.tools import SUBMIT_TOOL, VALIDATE_WAIT_TOOL, build_core_evaluation_tools
from vs_project.api import Project
from vs_runtime.api.core import (
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    AgentEvaluationRecords,
    OwedSubmissions,
    ReceiptStore,
    ScopeWorkspaces,
    StoreWorkspaceReceipts,
)
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces
from vs_sim.api import OsThreads, UnixNetwork
from vs_sim.api.testing import ManualClock, SimNetwork, SimThreads

if TYPE_CHECKING:
    from vs_runtime.api import AgentRole
    from vs_runtime.api.infrastructure import RuntimeWorkspaces


def _bridge(tmp_path: Path, socket: Path | None = None) -> AgentEvaluationBridge:
    root = tmp_path / "project"
    write_project(root)
    namespace = Project.open(root).state.state_store_namespace("offer")
    policy = AgentEvaluationPolicy(
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
        recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
        stages=(("accuracy", 10.0), ("benchmark", 10.0)),
        queue_allowance=880.0,
        accuracy_stage="accuracy",
        accuracy_gated=(),
    )
    return AgentEvaluationBridge(
        socket or tmp_path / "evaluation.sock",
        policy,
        ScopeWorkspaces(
            cast("RuntimeWorkspaces", FakeWorkspaces(FakeWorkspace(path=Path("/workspace")))),
            StoreWorkspaceReceipts(ReceiptStore(namespace, threads=OsThreads())),
        ),
        AgentEvaluationRecords(
            OwedSubmissions(ReceiptStore(namespace, threads=OsThreads())), SynthesizedEvidence()
        ),
        network=UnixNetwork(),
        threads=OsThreads(),
    )


@pytest.mark.parametrize("role", CORE_ROLES, ids=lambda role: role.id)
def test_a_dynamic_role_is_offered_the_evaluation_tool_exactly_when_it_measures(
    tmp_path: Path, role: AgentRole
) -> None:
    bridge = _bridge(tmp_path)

    servers = bridge.servers(role, Scope(owner=ATTEMPT.attempt_id, generation=0))

    measures = role in (IMPLEMENTER, JUDGE)
    assert bool(servers) is measures
    if measures:
        (descriptor,) = servers
        env = dict(descriptor.runtime_env)
        tools = build_core_evaluation_tools(
            socket_path=tmp_path / "evaluation.sock",
            token=env["VS_EVALUATION_TOKEN"],
            network=SimNetwork(SimThreads()),
            clock=ManualClock(),
        )
        assert {tool.name for tool in tools} == {SUBMIT_TOOL, VALIDATE_WAIT_TOOL}


async def test_the_bridge_never_creates_its_sockets_directory(tmp_path: Path) -> None:
    """Serving refuses a missing directory instead of creating one where the socket is.

    ``serve`` once ran ``mkdir(parents=True, exist_ok=True)``, which also accepts a
    directory or symlink another user planted at the shared temporary path, and
    then served inside it. The directory is the caller's to create, through
    ``prepare_evaluation_socket``'s ownership and mode check.
    """
    # A unix socket path is limited (104 bytes on macOS, where pytest's tmp_path is longer), and
    # bind reports a path over the limit before it reports a missing directory. The system
    # temporary directory is short on every platform.
    with tempfile.TemporaryDirectory() as short:
        missing = Path(short) / "absent"
        bridge = _bridge(tmp_path, missing / "evaluation.sock")

        with pytest.raises(FileNotFoundError):
            await bridge.serve()
        assert not missing.exists()
