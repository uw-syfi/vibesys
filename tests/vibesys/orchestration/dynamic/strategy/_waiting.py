"""One host's side of agent turns that use the evaluation tool through the production bridge.

A turn that waits calls the bridge's submit and wait tools as the evaluation tool server would,
then ends with its waiting reply; a turn that measures its workspace asks core to admit the
measurement. The bridge, the shell's admission and the continuation core records are the
production ones; only the workspace snapshot is faked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.owed_submissions import owed_in
from tests.support.synthesized_evidence import SynthesizedEvidence

from vs_core.api import (
    AgentMeasurementRequested,
    ArtifactId,
    ArtifactRef,
    DispatchTurn,
    RevisionRef,
    TurnObserved,
)
from vs_evaluation.api import SubmitCall, WaitCall
from vs_runtime.api.core import (
    EVALUATION_TOOL_ID,
    AgentEvaluationBridge,
    AgentEvaluationPolicy,
    AgentEvaluationRecords,
    ExecutionResult,
)
from vs_runtime.contracts import AgentRole, AgentTool

if TYPE_CHECKING:
    from vs_core.api import Request, RunFacts, Scope
    from vs_runtime.api.core import CoreRuntime
    from vs_sim.api.testing import VirtualClock

WAITING = json.dumps({"kind": "waiting_for_evaluation", "handles": ["h"]})
"""The reply of a turn that waits for the evaluation it submitted."""

_ROLE = AgentRole(
    id="agent", system_prompt="agent", extra_tools=(AgentTool(id=EVALUATION_TOOL_ID),)
)
# The bridge keeps its owed-submission records in files; a world has no directory of its own.
_SCRATCH = tempfile.TemporaryDirectory(prefix="dynamic-world-")


@dataclass(frozen=True)
class Measure:
    """What an implementer turn that measures its workspace asks core to admit."""

    candidate: RevisionRef
    now_at: float
    call: int


@dataclass
class Hosting:
    """What a world decided about its turns that use the evaluation tool, as plain data.

    The sets hold the identities of the dispatched turns that wait for their own evaluation,
    that submit one and end plainly, and that measure their workspace (with what they ask).
    """

    delays: tuple[int, ...] = (0,)
    waiting: set[str] = field(default_factory=set)
    submitting: set[str] = field(default_factory=set)
    measuring: dict[str, Measure] = field(default_factory=dict)
    asked: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    """(attempt, role, reply) of every submit the tool server answered, errors included."""
    dispatched: int = 0


class _Workspace:
    """A workspace whose content is fixed by its attempt, as a retained commit is."""

    def __init__(self, scope: Scope) -> None:
        self._revision = hashlib.sha256(scope.owner.root.encode()).hexdigest()[:40]

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        del label, retention_label
        return self._revision

    async def matches_snapshot(self, revision: str) -> bool:
        return revision == self._revision


class _Workspaces:
    async def workspace_of(self, scope: Scope) -> _Workspace:
        return _Workspace(scope)


class WaitingTurns:
    """What the turns that use the evaluation tool do on one host, once their executor answered.

    ``hosting`` says which dispatched turns wait, submit or measure. The bridge is bound to
    this host's shell and clock.
    """

    def __init__(
        self,
        shell: CoreRuntime[Any],
        clock: VirtualClock,
        facts: RunFacts,
        hosting: Hosting,
    ) -> None:
        """Serve the evaluation tool for ``shell`` under the digests of ``facts``."""
        root = Path(tempfile.mkdtemp(dir=_SCRATCH.name))
        self._policy = AgentEvaluationPolicy(
            evaluator_digest=facts.evaluator_digest,
            workload_digest=facts.workload_digest,
            environment_digest=facts.environment_digest,
            recipe=ArtifactRef(artifact_id=ArtifactId(root="recipe"), digest="recipe"),
            stages=(("accuracy", 60.0), ("benchmark", 60.0)),
            queue_allowance=10.0,
            accuracy_stage="accuracy",
        )
        self._bridge = AgentEvaluationBridge(
            root / "evaluation.sock",
            self._policy,
            _Workspaces(),
            AgentEvaluationRecords(owed_in(root / "owed"), SynthesizedEvidence()),
        )
        self._bridge.attach(shell, clock)
        self._shell = shell
        self._clock = clock
        self._hosting = hosting

    async def __call__(self, request: Request, result: ExecutionResult) -> ExecutionResult:
        """The turn's result, with the suspension the bridge recorded when it waited."""
        measure = self._hosting.measuring.get(request.request_id.root if request.request_id else "")
        if measure is not None:
            self._shell.admit(
                AgentMeasurementRequested(
                    scope=request.scope,
                    plan=self._policy.plan(measure.candidate, now_at=measure.now_at),
                    call_id=f"agent-call-{measure.call}",
                ),
                now_at=measure.now_at,
            )
        if not isinstance(request, DispatchTurn) or request.request_id is None:
            return result
        for _ in range(self._delay()):
            # test-isolation: yields the event loop, no time passes
            await asyncio.sleep(0)
        if request.request_id.root not in self._hosting.waiting | self._hosting.submitting:
            return result
        return await self._use_tool(request, result)

    def _delay(self) -> int:
        """The event-loop yields the next dispatched turn takes before it ends."""
        delays = self._hosting.delays
        ordinal = self._hosting.dispatched
        self._hosting.dispatched += 1
        return delays[ordinal % len(delays)]

    async def _use_tool(self, request: DispatchTurn, result: ExecutionResult) -> ExecutionResult:
        assert request.request_id is not None
        bridge = self._bridge
        (descriptor,) = bridge.servers(_ROLE, request.scope)
        token = dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]
        reply, acknowledge = await bridge.answer(SubmitCall(token=token).model_dump_json().encode())
        acknowledge()  # the scripted agent read the reply
        parsed = json.loads(reply)
        self._hosting.asked.append(
            (
                request.scope.owner.root,
                request.turn.session.role_id.root,
                parsed.get("result", parsed),
            )
        )
        handle = parsed.get("result", {}).get("handle_id")
        if handle is not None and request.request_id.root in self._hosting.waiting:
            # The loop runs the submission while the agent works: it owns its job by now.
            await self._clock.sleep(3.0)
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
