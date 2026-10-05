"""An agent's evaluation tool calls can never halt the run.

Tool calls are untrusted input. A call core cannot accept must come back to the agent as a
tool error and the run must go on. A scripted agent (the Fake driver) calls the real tool
handlers over the bridge's socket, from its turns, with generated programs: submit, wait on
own, repeated or stale handles, across fresh turns and resumed turns. The strategy
dispatches a generated number of fresh implementer turns and resumes every suspension.

Properties: the run reaches a terminal state without a refusal or an unhandled error; every
tool call either succeeds or fails with a typed tool error; every suspension core accepted
is resumed exactly once.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
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
    AttemptBudget,
    DecisionId,
    InvocationId,
    Limits,
    Proposal,
    RequestTurn,
    ResumeAuthorized,
    RunStatus,
    Scope,
    StartAttempt,
    TurnResult,
    TurnSpec,
    TurnSuspended,
    WorkspaceRef,
)
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

type Step = tuple[Literal["submit"]] | tuple[Literal["wait"], tuple[int, ...]]
type Program = tuple[Step, ...]


class LoopState(SkeletonState):
    """The skeleton's state plus how far the generated turn schedule has run."""

    fresh: int = 0
    yielded: bool = False
    suspensions: int = 0
    resumes: int = 0
    resume: ResumeAuthorized | None = None


class LoopStrategy(SkeletonStrategy):
    """Dispatch ``total`` fresh implementer turns, resuming every suspension, then measure."""

    state: LoopState = LoopState(schema_version=1)  # type: ignore[assignment]
    total: int = 1

    def decide(self, view: RunView) -> Proposal[SkeletonState]:
        """Resume the authorized turn, hold while suspended, else dispatch the next turn."""
        state = self.state
        if state.phase == "start":
            proposal = super().decide(view)
            (start,) = proposal.decisions
            assert isinstance(start, StartAttempt)
            budget = AttemptBudget(paid_invocation_limit=self.total)
            return proposal.model_copy(
                update={"decisions": (start.model_copy(update={"budget": budget}),)}
            )
        if state.phase != "turn":
            return super().decide(view)
        attempt = Scope(owner=ATTEMPT.attempt_id, generation=0)
        if state.resume is not None:
            event = state.resume
            spec = self._spec(view, "implement-0").model_copy(
                update={
                    "invocation_id": event.next_invocation.invocation_id,
                    "continuation_id": event.continuation_id,
                    "charge_class": "resume",
                }
            )
            decision = RequestTurn(
                decision_id=DecisionId(root=f"turn-resume-{state.resumes}"),
                scope=attempt,
                turn=spec,
            )
            return Proposal(state=state, decisions=(decision,))
        if state.yielded:
            return Proposal(state=state, decisions=())
        decision = RequestTurn(
            decision_id=DecisionId(root=f"turn-{state.fresh}"),
            scope=attempt,
            turn=self._spec(view, f"implement-{state.fresh}"),
        )
        return Proposal(state=state, decisions=(decision,))

    def _spec(self, view: RunView, invocation: str) -> TurnSpec:
        proposal = SkeletonStrategy.decide(self, view)
        (decision,) = proposal.decisions
        assert isinstance(decision, RequestTurn)
        return decision.turn.model_copy(update={"invocation_id": InvocationId(root=invocation)})

    def on_event(self, view: RunView, event: StrategyEvent) -> SkeletonState:
        """Count suspensions and resumes; each finished turn schedules the next or measures."""
        state = self.state
        if isinstance(event, TurnSuspended):
            return state.model_copy(update={"suspensions": state.suspensions + 1})
        if isinstance(event, ResumeAuthorized):
            return state.model_copy(
                update={"resumes": state.resumes + 1, "resume": event, "yielded": False}
            )
        if isinstance(event, TurnResult) and state.phase == "turn":
            reply = json.loads(event.output_json or "{}")
            fresh = state.fresh + (state.resume is None)
            if reply.get("waiting"):
                return state.model_copy(update={"yielded": True, "resume": None, "fresh": fresh})
            if fresh < self.total:
                return state.model_copy(update={"fresh": fresh, "resume": None})
            return super().on_event(view, event).model_copy(update={"resume": None, "fresh": fresh})
        return super().on_event(view, event)


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
    tmp_path: Path, programs: tuple[Program, ...], total: int
) -> AsyncIterator[Scenario]:
    """A run whose implementer runs ``programs`` over ``total`` fresh turns and resumes."""
    agents: list[ScriptedAgent] = []
    resolvers: list[ToolResolver] = []
    bridges: list[AgentEvaluationBridge] = []

    def host(root: Path) -> SessionHost:
        agent = ScriptedAgent(root, programs=programs)
        agents.append(agent)
        resolver = ToolResolver(
            root,
            TemplateRenderer(root),
            roles=frozenset({IMPLEMENTER}),
            schemas={IMPLEMENTATION: Implementation},
            writer=agent,
        )
        resolvers.append(resolver)
        client = AgentClient(FakeDriver(answer=agent.answer, on_turn=agent))
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

    with open_skeleton_world(tmp_path, LoopStrategy(total=total), agents=host) as world:
        world.yields = yields
        world.limits = Limits(max_turns=4 * total + 4, max_measurement_submissions=64)
        yield Scenario(world, agents[0], bridges)


async def play(
    tmp_path: Path, programs: tuple[Program, ...], total: int
) -> tuple[Process, ScriptedAgent]:
    """Drive the run to its end; it must not halt, refuse or raise."""
    async with scenario(tmp_path, programs, total) as played:
        process = played.world.runtime()
        clock = FakeRunClock(0.0)
        process.shell.start("skeleton", now_at=0.0, lease_duration=LEASE)
        played.bridge.attach(process.shell, clock)
        await played.bridge.serve()
        try:
            refusal = await drive(process, start=0.0, clock=clock)
        finally:
            await played.bridge.close()
        assert refusal is None
        return process, played.agent


def check(process: Process, agent: ScriptedAgent) -> None:
    """The run ended terminally, and every accepted suspension was resumed once."""
    state = process.shell.record.envelope.core
    strategy = process.shell.record.envelope.strategy
    assert isinstance(strategy, LoopState)
    assert state.run.status == RunStatus.TERMINAL
    assert strategy.suspensions == strategy.resumes
    assert strategy.suspensions <= agent.waits_accepted


def run_play(tmp_path: Path, programs: tuple[Program, ...], total: int) -> ScriptedAgent:
    root = tmp_path / uuid.uuid4().hex[:6]
    root.mkdir()

    async def go() -> ScriptedAgent:
        process, agent = await play(root, programs, total)
        check(process, agent)
        return agent

    return asyncio.run(go())


SUBMIT_AND_WAIT: Program = (("submit",), ("wait", (0,)))


def test_a_fresh_turn_after_a_resumed_turn_may_submit_and_wait_again(tmp_path: Path) -> None:
    """The live-1 crash: turn, resume that ends plainly, then a fresh turn waits again."""
    agent = run_play(tmp_path, (SUBMIT_AND_WAIT, (), (("submit",), ("wait", (1,))), ()), total=2)
    assert agent.errors == []
    assert agent.waits_accepted == 2


_STEPS = st.one_of(
    st.just(("submit",)),
    st.tuples(st.just("wait"), st.lists(st.integers(0, 4), min_size=1, max_size=3).map(tuple)),
)
_PROGRAMS = st.lists(st.lists(_STEPS, max_size=4).map(tuple), min_size=1, max_size=5).map(tuple)


@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(programs=_PROGRAMS, total=st.integers(1, 3))
def test_no_tool_call_sequence_halts_the_run(
    tmp_path: Path, programs: tuple[Program, ...], total: int
) -> None:
    agent = run_play(tmp_path, programs, total)
    assert all(isinstance(text, str) and text for text in agent.errors)
