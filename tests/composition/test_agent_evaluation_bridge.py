"""An agent submits through the evaluation tool and core resumes it once, over the Fake cluster.

The real shell, executors, Git workspaces and Fake Slurm cluster run one scripted attempt.
The implementer's turn calls the core-path tool handlers over the bridge's unix socket, from
the provider thread, as the MCP process would, then ends its turn waiting. Core must admit the
submission, run one measurement, and authorize exactly one resume of that turn.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter
from tests.support.fake_run_clock import FakeRunClock
from tests.support.session_world import ProviderFaults, SessionHost
from tests.support.skeleton_strategy import ATTEMPT, DIGEST, SkeletonState, SkeletonStrategy
from tests.support.skeleton_world import (
    IMPLEMENTATION,
    IMPLEMENTER,
    LEASE,
    CandidateResolver,
    CandidateWriter,
    Implementation,
    Process,
    World,
    drive,
    open_skeleton_world,
)

from vs_agent.api import AgentClient
from vs_agent.api.testing import FakeAgentInvocationStore, FakeDriver
from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    DecisionId,
    Limits,
    Proposal,
    RequestTurn,
    ResumeAuthorized,
    RunStatus,
    Scope,
    TurnResult,
    TurnSpec,
    TurnSuspended,
    WorkspaceRef,
)
from vs_evaluation.api import SocketFailure, SocketSuccess, SubmitCall, WaitCall
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
    from collections.abc import AsyncIterator

    from vs_agent.api import AgentSessionSpec, AgentTurnRequest
    from vs_core.api import RunView, StrategyEvent
    from vs_project.api import StateNamespace
    from vs_runtime.api.core import AccessGuardedWorkspace
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


class WaitingState(SkeletonState):
    """The skeleton's state plus what the yield and resume looked like to the strategy."""

    yielded: bool = False
    suspensions: int = 0
    resumes: int = 0
    resume: ResumeAuthorized | None = None


class WaitingStrategy(SkeletonStrategy):
    """The skeleton, except that the implementer turn may yield and must be resumed once."""

    state: WaitingState = WaitingState(schema_version=1)  # type: ignore[assignment]

    def decide(self, view: RunView) -> Proposal[SkeletonState]:
        """Hold while the turn is suspended; resume it with the invocation core named."""
        state = self.state
        if state.phase == "turn" and state.resume is not None:
            attempt = Scope(owner=ATTEMPT.attempt_id, generation=0)
            event = state.resume
            spec = self._turn_spec(view).model_copy(
                update={
                    "invocation_id": event.next_invocation.invocation_id,
                    "continuation_id": event.continuation_id,
                    "charge_class": "resume",
                }
            )
            return Proposal(
                state=state,
                decisions=(
                    RequestTurn(
                        decision_id=DecisionId(root="turn-resume"), scope=attempt, turn=spec
                    ),
                ),
            )
        if state.phase == "turn" and state.yielded:
            return Proposal(state=state, decisions=())
        return super().decide(view)

    def _turn_spec(self, view: RunView) -> TurnSpec:
        proposal = SkeletonStrategy.decide(self, view)
        (decision,) = proposal.decisions
        assert isinstance(decision, RequestTurn)
        return decision.turn

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        """Record the yield and the one resume; everything else is the skeleton's."""
        state = self.state
        if isinstance(event, TurnSuspended):
            return state.model_copy(update={"suspensions": state.suspensions + 1})
        if isinstance(event, ResumeAuthorized):
            return state.model_copy(update={"resumes": state.resumes + 1, "resume": event})
        if isinstance(event, TurnResult) and state.phase == "turn" and state.resume is None:
            reply = json.loads(event.output_json or "{}")
            if reply.get("waiting"):
                return state.model_copy(update={"yielded": True})
        return super().on_event(view, event)


@dataclass
class ToolUsingWriter(CandidateWriter):
    """The Fake provider's implementer: commits, then submits through the tool on turn one."""

    submissions: int = 1
    handles: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    descriptor_env: dict[str, str] = field(default_factory=dict)

    def __call__(self, request: AgentTurnRequest) -> None:
        """Commit one change; the first turn then submits and validates its wait."""
        super().__call__(request)
        if self.turns > 1:
            self.answer["waiting"] = False
            return
        tools = {
            tool.name: tool
            for tool in build_core_evaluation_tools(
                socket_path=Path(self.descriptor_env["VS_EVALUATION_SOCKET"]),
                token=self.descriptor_env["VS_EVALUATION_TOKEN"],
            )
        }
        submit = tools[SUBMIT_TOOL]
        for _ in range(self.submissions):
            try:
                reply = json.loads(submit.handler(submit.input_schema()))
            except EvaluationServiceClientError as error:
                self.refusals.append(str(error))
            else:
                self.handles.append(reply["handle_id"])
        wait = tools[VALIDATE_WAIT_TOOL]
        wait.handler(wait.input_schema(handles=tuple(self.handles[:1])))
        self.answer["waiting"] = True


@dataclass
class ToolResolver(CandidateResolver):
    """Resolves the candidate worktree and offers the bridge's tool server to the agent."""

    bridge: AgentEvaluationBridge | None = None

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The candidate session, after recording the tool server the provider would launch."""
        assert self.bridge is not None
        assert isinstance(self.writer, ToolUsingWriter)
        scope = turn.workspace.scope if isinstance(turn.workspace, WorkspaceRef) else turn.workspace
        (descriptor,) = self.bridge.servers(ROLE, scope)
        self.writer.descriptor_env = dict(descriptor.runtime_env)  # type: ignore[attr-defined]
        return super().agent_spec(turn, workspace)


@dataclass
class Scenario:
    """One run's agent, bridge and world."""

    world: World
    writer: ToolUsingWriter
    bridge: list[AgentEvaluationBridge]


@asynccontextmanager
async def scenario(tmp_path: Path, *, submissions: int = 1) -> AsyncIterator[Scenario]:
    """A run whose implementer submits ``submissions`` times through the tool, then waits."""
    writers: list[ToolUsingWriter] = []
    resolvers: list[ToolResolver] = []
    bridges: list[AgentEvaluationBridge] = []

    def agents(root: Path) -> SessionHost:
        writer = ToolUsingWriter(root, submissions=submissions)
        writers.append(writer)
        resolver = ToolResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=writer,
        )
        resolvers.append(resolver)
        client = AgentClient(FakeDriver(answer=writer.answer, on_turn=writer))
        return SessionHost(resolver, client, FakeAgentInvocationStore(), [], ProviderFaults())

    def yields(workspaces: RuntimeWorkspaces, receipts: StateNamespace) -> AgentEvaluationBridge:
        bridge = AgentEvaluationBridge(
            tmp_path / "evaluation.sock",
            POLICY,
            ScopeWorkspaces(workspaces, StoreWorkspaceReceipts(ReceiptStore(receipts))),
        )
        resolvers[0].bridge = bridge
        bridges.append(bridge)
        return bridge

    with open_skeleton_world(tmp_path, WaitingStrategy(), agents=agents) as world:
        world.yields = yields
        world.limits = Limits(max_turns=2)
        yield Scenario(world, writers[0], bridges)


async def run(played: Scenario) -> tuple[Process, FakeRunClock]:
    """Drive the run to its end with the bridge attached and serving."""
    process = played.world.runtime()
    (bridge,) = played.bridge
    clock = FakeRunClock(0.0)
    process.shell.start("skeleton", now_at=0.0, lease_duration=LEASE)
    bridge.attach(process.shell, clock)
    await bridge.serve()
    try:
        refusal = await drive(process, start=0.0, clock=clock)
    finally:
        await bridge.close()
    assert refusal is None
    return process, clock


@pytest.mark.asyncio
async def test_an_agent_submits_through_the_tool_and_is_resumed_once(tmp_path: Path) -> None:
    async with scenario(tmp_path) as played:
        process, _ = await run(played)
        state = process.shell.record.envelope.core
        strategy = process.shell.record.envelope.strategy
        assert isinstance(strategy, WaitingState)
        assert state.run.status == RunStatus.TERMINAL
        assert len(played.writer.handles) == 1
        assert played.writer.refusals == []
        admitted = [c for c in state.evaluation.agent_calls if c.request_id is not None]
        assert len(admitted) == 1
        # Baseline, the agent's submission, and the strategy's own candidate measurement.
        assert len(played.world.cluster.submissions) == 3
        assert played.writer.turns == 2
        assert strategy.suspensions == 1
        assert strategy.resumes == 1


SOCKET_REPLY = TypeAdapter(SocketSuccess | SocketFailure)
_TOKENS = st.sampled_from(("own", "foreign", "garbage"))
_CALLS = st.one_of(
    st.tuples(st.just("submit"), _TOKENS),
    st.tuples(st.just("wait"), _TOKENS, st.lists(st.integers(0, 3), min_size=1, max_size=3)),
    st.tuples(st.just("raw"), st.binary(max_size=80)),
)


async def _synthesized(tmp_path: Path, calls: list[tuple[Any, ...]]) -> None:
    """Replay generated tool calls against the bridge of a started run, with no agent."""
    async with scenario(tmp_path) as played:
        process = played.world.runtime()
        (bridge,) = played.bridge
        process.shell.start("skeleton", now_at=0.0, lease_duration=LEASE)
        bridge.attach(process.shell, FakeRunClock(0.0))
        scope = Scope(owner=process.shell.record.envelope.core.run.run_id, generation=0)
        (descriptor,) = bridge.servers(ROLE, scope)
        own = dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]
        tokens = {
            "own": own,
            "foreign": own[:-1] + ("0" if own[-1] != "0" else "1"),
            "garbage": "x",
        }
        handles: list[str] = []
        for call in calls:
            match call:
                case ("submit", kind):
                    frame = SubmitCall(token=tokens[kind]).model_dump_json().encode()
                case ("wait", kind, picks):
                    names = tuple(
                        dict.fromkeys(
                            handles[i] if i < len(handles) else f"unknown-{i}" for i in picks
                        )
                    )
                    frame = WaitCall(token=tokens[kind], handles=names).model_dump_json().encode()
                case (_, raw):
                    frame = raw
            before = process.shell.storage_revision
            reply = SOCKET_REPLY.validate_json(await bridge.handle(frame))
            if isinstance(reply, SocketSuccess):
                assert call[0] in ("submit", "wait")
                assert len(call) < 2 or call[1] == "own", "only the issued token is honoured"
                if call[0] == "submit":
                    handles.append(reply.result["handle_id"])  # type: ignore[index]
            assert process.shell.storage_revision == before, "a call commits only through the loop"
        while process.shell.advance():
            pass
        core_state = process.shell.record.envelope.core
        admitted = [c for c in core_state.evaluation.agent_calls if c.request_id is not None]
        assert len(admitted) == len(handles) == len(set(handles))
        assert len(admitted) <= core_state.run.limits.max_measurement_submissions
        assert len(admitted) <= 1, "an unchanged workspace is one identity with one submission"


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(calls=st.lists(_CALLS, max_size=8))
def test_synthesized_tool_calls_never_exceed_the_budget_or_cross_tokens(
    tmp_path: Path, calls: list[tuple[Any, ...]]
) -> None:
    root = tmp_path / uuid.uuid4().hex
    root.mkdir()
    asyncio.run(_synthesized(root, calls))
