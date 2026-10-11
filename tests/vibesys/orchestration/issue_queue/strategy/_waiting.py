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
from tests.vibesys.orchestration.issue_queue.strategy._executors import Executors, WorldTranslator
from tests.vibesys.orchestration.issue_queue.strategy._run import FACTS, config

from vibesys.core_wiring import agent_evaluation_policy
from vibesys.orchestration.issue_queue.strategy.api import measurement_recipe
from vs_core.api import DispatchTurn, ResourceId, ResumeSessionTurn, TurnObserved
from vs_core.testing.drive import Succeeded
from vs_evaluation.api import SubmitCall, WaitCall
from vs_runtime.api.core import EVALUATION_TOOL_ID, AgentEvaluationBridge, ExecutionResult
from vs_runtime.contracts import AgentRole, AgentTool

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_core.api import Request, Scope
    from vs_core.testing.drive import Answer
    from vs_runtime.api.core import CoreRuntime, ExecutionContext
    from vs_sim.api.testing import VirtualClock

WAITING = json.dumps({"kind": "waiting_for_evaluation", "handles": ["h"]})
_ROLE = AgentRole(
    id="agent", system_prompt="agent", extra_tools=(AgentTool(id=EVALUATION_TOOL_ID),)
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
    policy = agent_evaluation_policy(
        measurement_recipe(config(), FACTS), accuracy=True, benchmark=True
    )
    return AgentEvaluationBridge(
        root / "evaluation.sock", policy, _Workspaces(), owed_in(root / "owed")
    )


@dataclass
class WaitingTurns(Executors):
    """Executors where fresh turns may wait on an evaluation of their own.

    ``decide`` says, for the n-th fresh implementer or judge turn to start, whether it submits
    an evaluation and waits for it. A resumed turn never waits again.
    """

    decide: Callable[[int], bool] = lambda _ordinal: True
    roles: tuple[str, ...] = ("implementer", "judge")
    delay: Callable[[int], int] = lambda _ordinal: 0
    started: int = 0
    delayed: int = 0
    waiting: set[str] = field(default_factory=set)
    clock: VirtualClock | None = None
    """The run's simulated clock, bound by `attach_for` once the run exists."""

    def _turn(self, request: DispatchTurn | ResumeSessionTurn) -> Answer:
        role = request.turn.session.role_id.root
        if isinstance(request, DispatchTurn) and role.endswith(self.roles):
            ordinal = self.started
            self.started += 1
            if self.decide(ordinal):
                assert request.request_id is not None
                self.waiting.add(request.request_id.root)
                lease = ResourceId(root=f"lease:{request.turn.session.session_id.root}")
                return Succeeded(output_json=WAITING, resource_id=lease)
        return super()._turn(request)


def attach_for(
    script: WaitingTurns, bridge: AgentEvaluationBridge
) -> Callable[[CoreRuntime[Any], VirtualClock], None]:
    """Bind the shell and the run's clock to the bridge and the script, once they exist."""

    def attach(shell: CoreRuntime[Any], clock: VirtualClock) -> None:
        bridge.attach(shell, clock)
        script.clock = clock

    return attach


def translator_for(script: WaitingTurns, bridge: AgentEvaluationBridge) -> type[WorldTranslator]:
    """The translator class whose waiting turns call ``bridge`` as the tool server would."""

    class Translator(WorldTranslator):
        async def execute(self, request: Request, context: ExecutionContext) -> ExecutionResult:
            result = await super().execute(request, context)
            if not isinstance(request, DispatchTurn) or request.request_id is None:
                return result
            for _ in range(script.delay(script.delayed)):
                # test-isolation: yields the event loop, no time passes
                await asyncio.sleep(0)
            script.delayed += 1
            if request.request_id.root not in script.waiting:
                return result
            (descriptor,) = bridge.servers(_ROLE, request.scope)
            token = dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]
            reply, confirm = await bridge.answer(SubmitCall(token=token).model_dump_json().encode())
            handle = json.loads(reply).get("result", {}).get("handle_id")
            # The agent has read the reply; only now does its client confirm the receipt.
            confirm()
            if handle is not None:
                # The loop runs the submission while the agent works: it owns its job by now.
                assert script.clock is not None
                await script.clock.sleep(3.0)
                call = WaitCall(token=token, handles=(handle,))
                _, confirm_wait = await bridge.answer(call.model_dump_json().encode())
                confirm_wait()
            suspension = bridge.yielded(request)
            events = tuple(
                event.model_copy(update={"suspension": suspension})
                if isinstance(event, TurnObserved)
                else event
                for event in result.owner_events
            )
            return ExecutionResult(observation=result.observation, owner_events=events)

    return Translator
