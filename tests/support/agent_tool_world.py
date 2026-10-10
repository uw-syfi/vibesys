"""A skeleton run whose agent turns call the real evaluation bridge over its socket.

Shared by the composition tests of an agent's tool calls: a scripted agent (the Fake
driver) submits and waits through the bridge's tool handlers while the production loop
drives the run.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_strategy import DIGEST
from tests.support.skeleton_world import (
    IMPLEMENTATION,
    IMPLEMENTER,
    CandidateResolver,
    CandidateWriter,
    Implementation,
    World,
    open_skeleton_world,
)

from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeAgentInvocationStore, FakeProvider
from vs_core.api import ArtifactId, ArtifactRef, Limits, TurnSpec, WorkspaceRef
from vs_evaluation.api.tools import (
    SUBMIT_TOOL,
    VALIDATE_WAIT_TOOL,
    EvaluationServiceClientError,
    build_core_evaluation_tools,
)
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import (
    EVALUATION_TOOL_ID,
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    ReceiptStore,
    ScopeWorkspaces,
    StoreWorkspaceReceipts,
)
from vs_runtime.contracts import AgentRole, AgentTool

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from tests.support.skeleton_strategy import SkeletonStrategy

    from vs_agent.api import AgentSessionSpec, AgentTurnRequest
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import AccessGuardedWorkspace, TurnYields
    from vs_runtime.api.infrastructure import RuntimeWorkspaces

ROLE = AgentRole(
    id="implementer", system_prompt="implement", extra_tools=(AgentTool(id=EVALUATION_TOOL_ID),)
)
POLICY = AgentEvaluationPolicy(
    evaluator_digest=DIGEST,
    workload_digest=DIGEST,
    environment_digest=DIGEST,
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest=DIGEST),
    stages=(("accuracy", 10.0), ("benchmark", 10.0)),
    queue_allowance=880.0,
    accuracy_stage="accuracy",
)

type Step = tuple[Literal["submit"]] | tuple[Literal["wait"], tuple[int, ...]]
type Program = tuple[Step, ...]


@dataclass
class ScriptedAgent(CandidateWriter):
    """The Fake provider's implementer: each turn commits, then runs its generated program."""

    programs: tuple[Program, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    handles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    waits_accepted: int = 0

    def __call__(self, request: AgentTurnRequest) -> None:
        """Commit one change, run this turn's program, and reply waiting if a wait was accepted."""
        super().__call__(request)
        tools = {
            tool.name: tool
            for tool in build_core_evaluation_tools(
                socket_path=Path(self.env["VS_EVALUATION_SOCKET"]),
                token=self.env["VS_EVALUATION_TOKEN"],
            )
        }
        program = self.programs[(self.turns - 1) % len(self.programs)] if self.programs else ()
        waiting = False
        for step in program:
            try:
                match step:
                    case ("submit",):
                        tool = tools[SUBMIT_TOOL]
                        self.handles.append(
                            json.loads(tool.handler(tool.input_schema()))["handle_id"]
                        )
                    case ("wait", picks):
                        tool = tools[VALIDATE_WAIT_TOOL]
                        names = tuple(
                            dict.fromkeys(
                                self.handles[i] if i < len(self.handles) else f"unknown-{i}"
                                for i in picks
                            )
                        )
                        tool.handler(tool.input_schema(handles=names))
                        waiting = True
                        self.waits_accepted += 1
            except EvaluationServiceClientError as error:
                self.errors.append(str(error))
        self.answer["waiting"] = waiting


@dataclass
class ToolResolver(CandidateResolver):
    """Resolves the candidate worktree and offers the bridge's tool server to the agent."""

    bridge: AgentEvaluationBridge | None = None

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The candidate session, after recording the tool server the provider would launch."""
        assert self.bridge is not None
        assert isinstance(self.writer, ScriptedAgent)
        scope = turn.workspace.scope if isinstance(turn.workspace, WorkspaceRef) else turn.workspace
        (descriptor,) = self.bridge.servers(ROLE, scope)
        self.writer.env = dict(descriptor.runtime_env)
        return super().agent_spec(turn, workspace)


@dataclass
class Scenario:
    """One run's agent, bridge and world."""

    world: World
    agent: ScriptedAgent
    bridges: list[AgentEvaluationBridge]

    @property
    def bridge(self) -> AgentEvaluationBridge:
        """The run's bridge, built when the world's runtime is."""
        (bridge,) = self.bridges
        return bridge


@asynccontextmanager
async def scenario(
    tmp_path: Path,
    strategy: SkeletonStrategy,
    limits: Limits,
    agent_factory: Callable[[Path], ScriptedAgent],
    *,
    yields_over: Callable[[AgentEvaluationBridge], TurnYields] | None = None,
) -> AsyncIterator[Scenario]:
    """A run of ``strategy`` whose implementer is ``agent_factory(root)``, over the real bridge.

    ``yields_over`` wraps the bridge as the turn-yield source, for tests that stand in for
    an agent's accepted wait.
    """
    agents: list[ScriptedAgent] = []
    resolvers: list[ToolResolver] = []
    bridges: list[AgentEvaluationBridge] = []

    def host(root: Path) -> SessionHost:
        agent = agent_factory(root)
        agents.append(agent)
        resolver = ToolResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=agent,
        )
        resolvers.append(resolver)
        client = AgentClient(FakeProvider(answer=agent.answer, on_turn=agent))
        return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())

    def yields(workspaces: RuntimeWorkspaces, receipts: StateNamespace) -> TurnYields:
        bridge = AgentEvaluationBridge(
            tmp_path / "evaluation.sock",
            POLICY,
            ScopeWorkspaces(workspaces, StoreWorkspaceReceipts(ReceiptStore(receipts))),
        )
        resolvers[0].bridge = bridge
        bridges.append(bridge)
        return bridge if yields_over is None else yields_over(bridge)

    with open_skeleton_world(tmp_path, strategy, agents=host) as world:
        world.yields = yields
        world.limits = limits
        yield Scenario(world, agents[0], bridges)
