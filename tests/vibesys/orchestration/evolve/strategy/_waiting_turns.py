"""Agent turns that wait on their own evaluation through the production bridge.

A turn that waits calls the bridge's submit and wait tools as the evaluation tool server
would, then ends with its waiting reply. The bridge, the shell's admission and the
continuation core records are the production ones; only the workspace snapshot is faked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tests.support.owed_submissions import owed_in
from tests.support.synthesized_evidence import SynthesizedEvidence
from tests.vibesys.orchestration.evolve.strategy._executors import Executors
from tests.vibesys.orchestration.evolve.strategy._run import FACTS

from vs_core.api import (
    ArtifactId,
    ArtifactRef,
    DispatchTurn,
    ObserveOwnedJob,
    ResourceId,
    ResumeSessionTurn,
    SubmitMeasurement,
    TurnObserved,
)
from vs_core.testing.drive import Running, Succeeded
from vs_evaluation.api import SubmitCall, WaitCall
from vs_runtime.api.core import (
    EVALUATION_TOOL_ID,
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    AgentEvaluationRecords,
    ExecutionResult,
    handle_for,
)
from vs_runtime.api.testing import ScriptedExecutors
from vs_runtime.contracts import AgentRole, AgentTool

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_core.api import CoreState, Request, Scope
    from vs_core.testing.drive import Answer
    from vs_runtime.api.core import CoreRuntime, ExecutionContext
    from vs_sim.api.testing import VirtualClock

WAITING = json.dumps({"kind": "waiting_for_evaluation", "handles": ["h"]})
_ROLE = AgentRole(
    id="agent", system_prompt="agent", extra_tools=(AgentTool(id=EVALUATION_TOOL_ID),)
)
_POLICY = AgentEvaluationPolicy(
    evaluator_digest=FACTS.evaluator_digest,
    workload_digest=FACTS.workload_digest,
    environment_digest=FACTS.environment_digest,
    recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
    stages=(("accuracy", 60.0), ("benchmark", 60.0)),
    queue_allowance=10.0,
    accuracy_stage="accuracy",
)


class _Workspace:
    """A workspace whose content is fixed by its attempt, as a retained commit is."""

    def __init__(self, scope: Scope) -> None:
        self._revision = hashlib.sha256(scope.owner.root.encode()).hexdigest()[:40]

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        del label, retention_label
        return self._revision

    async def matches_revision(self, revision: str) -> bool:
        return revision == self._revision


class _Workspaces:
    async def workspace_of(self, scope: Scope) -> _Workspace:
        return _Workspace(scope)


def new_bridge(root: Path) -> AgentEvaluationBridge:
    """The production bridge over a fake workspace lookup; it is called in process."""
    return AgentEvaluationBridge(
        root / "evaluation.sock",
        _POLICY,
        _Workspaces(),
        AgentEvaluationRecords(owed_in(root / "owed"), SynthesizedEvidence()),
    )


@dataclass
class WaitingTurns(Executors):
    """Executors where fresh mutator turns may wait, and evaluation jobs run for a generated time.

    ``decide`` says, for the n-th fresh mutator or judge turn to start, whether it
    submits an evaluation and waits for it. A resumed turn never waits again. ``polls`` is
    how many polls each evaluation job reports running, in submission order; the last
    entry repeats.
    """

    decide: Callable[[int], bool] = lambda _ordinal: True
    ends: Callable[[int], str] = lambda _ordinal: "wait"
    """How the n-th waiting turn ends once it has validated its wait: with the waiting reply
    (``"wait"``), the role's final result (``"result"``) or a reply that is not valid JSON
    (``"invalid"``). Core records the wait either way, because the tool call made it."""
    roles: tuple[str, ...] = ("mutator",)
    delay: Callable[[int], int] = lambda _ordinal: 0
    polls: list[int] = field(default_factory=lambda: [0])
    started: int = 0
    delayed: int = 0
    submitted: int = 0
    waiting: set[str] = field(default_factory=set)
    submit_only: tuple[str, ...] = ()
    """Roles whose fresh turns submit an evaluation and end plainly, leaving it running."""
    submitting: set[str] = field(default_factory=set)
    asked: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    """(attempt, role, reply) of every submit the tool server answered, errors included."""
    running: dict[str, int] = field(default_factory=dict)
    clock: VirtualClock | None = None
    """The run's simulated clock, bound by `attach_for` once the run exists."""

    def __post_init__(self) -> None:
        self.submit = self._start

    def _start(self, request: SubmitMeasurement) -> Answer:
        assert request.request_id is not None
        resource = handle_for(request.request_id)
        self.running[resource] = self.polls[min(self.submitted, len(self.polls) - 1)]
        self.submitted += 1
        return Running(resource_id=ResourceId(root=resource))

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, ResumeSessionTurn):
            self.seen.append(request)
            return self._turn(request)
        if isinstance(request, ObserveOwnedJob) and self.running.get(request.resource_id.root):
            self.running[request.resource_id.root] -= 1
            self.seen.append(request)
            return Running(resource_id=request.resource_id)
        return super().__call__(request, core)

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root
        if isinstance(request, DispatchTurn) and role.endswith(self.submit_only):
            assert request.request_id is not None
            self.submitting.add(request.request_id.root)
        elif isinstance(request, DispatchTurn) and role.endswith(self.roles):
            ordinal = self.started
            self.started += 1
            if self.decide(ordinal):
                assert request.request_id is not None
                self.waiting.add(request.request_id.root)
                lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
                return Succeeded(output_json=self._ending(request, ordinal), resource_id=lease)
        return super()._turn(request)

    def _ending(self, request: DispatchTurn, ordinal: int) -> str:
        """The reply the waiting turn ends with."""
        ending = self.ends(ordinal)
        if ending == "result":
            answer = super()._turn(request)
            assert isinstance(answer, Succeeded)
            assert answer.output_json is not None
            return answer.output_json
        return {"wait": WAITING, "invalid": "not json"}[ending]


def attach_for(
    script: WaitingTurns, bridge: AgentEvaluationBridge
) -> Callable[[CoreRuntime[Any], VirtualClock], None]:
    """Bind the shell and the run's clock to the bridge and the script, once they exist."""

    def attach(shell: CoreRuntime[Any], clock: VirtualClock) -> None:
        bridge.attach(shell, clock)
        script.clock = clock

    return attach


def translator_for(script: WaitingTurns, bridge: AgentEvaluationBridge) -> type[ScriptedExecutors]:
    """The translator class whose waiting turns call ``bridge`` as the tool server would."""

    class Translator(ScriptedExecutors):
        async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
            result = await super().execute(request, context)
            if not isinstance(request, DispatchTurn) or request.request_id is None:
                return result
            for _ in range(script.delay(script.delayed)):
                # test-isolation: yields the event loop, no time passes
                await asyncio.sleep(0)
            script.delayed += 1
            if request.request_id.root not in script.waiting | script.submitting:
                return result
            (descriptor,) = bridge.servers(_ROLE, request.scope)
            token = dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]
            reply, acknowledge = await bridge.answer(
                SubmitCall(token=token).model_dump_json().encode()
            )
            acknowledge()  # the scripted agent read the reply
            parsed = json.loads(reply)
            script.asked.append(
                (
                    request.scope.owner.root,
                    request.turn.session.role_id.root,
                    parsed.get("result", parsed),
                )
            )
            handle = parsed.get("result", {}).get("handle_id")
            if handle is not None and request.request_id.root in script.waiting:
                # The loop runs the submission while the agent works: it owns its job by now.
                assert script.clock is not None
                await script.clock.sleep(3.0)
                call = WaitCall(token=token, handles=(handle,))
                await bridge.answer(call.model_dump_json().encode())
            suspension = bridge.yielded(request)
            events = tuple(
                event.model_copy(update={"suspension": suspension})
                if isinstance(event, TurnObserved)
                else event
                for event in result.owner_events
            )
            return ExecutionResult(observation=result.observation, owner_events=events)

    return Translator
