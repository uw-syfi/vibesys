"""One host's side of agent turns that submit an evaluation through the production bridge.

A turn that waits calls the bridge's submit and wait tools as the evaluation tool server would,
then ends with its waiting reply; a turn that only submits leaves its job running. The bridge,
the shell's admission and the continuation core records are the production ones; only the
workspace snapshot is faked.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.owed_submissions import owed_in
from tests.support.synthesized_evidence import SynthesizedEvidence

from vs_core.api import ArtifactId, ArtifactRef, DispatchTurn, TurnObserved
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
    from collections.abc import Mapping

    from vs_core.api import Request, RunFacts, Scope
    from vs_runtime.api.core import CoreRuntime
    from vs_sim.api.testing import VirtualClock

WAITING = json.dumps({"kind": "waiting_for_evaluation", "handles": ["h"]})
"""The reply of a turn that waits for the evaluation it submitted."""
ENVELOPED_WAITING = json.dumps({"reply": json.loads(WAITING)})
"""The wait in the object-rooted form the shared protocol prompt tells the agent to answer with."""

_ROLE = AgentRole(
    id="agent", system_prompt="agent", extra_tools=(AgentTool(id=EVALUATION_TOOL_ID),)
)
# The bridge keeps its owed-submission records in files; a world has no directory of its own.
_SCRATCH = tempfile.TemporaryDirectory(prefix="evolve-world-")


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


class WaitingTurns:
    """What the turns that use the evaluation tool do on one host, once their executor answered.

    ``acting`` maps the identity of each dispatched turn the world made use the tool to whether
    it also waits for the evaluation (otherwise it submits and ends, leaving the job running).
    Every answer the tool server gives to a submit is appended to ``asked`` as
    ``(attempt, role, reply)``. The bridge is bound to this host's shell and clock.
    """

    def __init__(
        self,
        shell: CoreRuntime[Any],
        clock: VirtualClock,
        facts: RunFacts,
        acting: Mapping[str, bool],
        asked: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """Serve the evaluation tool for ``shell`` under the digests of ``facts``."""
        root = Path(tempfile.mkdtemp(dir=_SCRATCH.name))
        policy = AgentEvaluationPolicy(
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
            policy,
            _Workspaces(),
            AgentEvaluationRecords(owed_in(root / "owed"), SynthesizedEvidence()),
        )
        self._bridge.attach(shell, clock)
        self._clock = clock
        self._acting = acting
        self._asked = asked

    async def __call__(self, request: Request, result: ExecutionResult) -> ExecutionResult:
        """The turn's result, with the suspension the bridge recorded when it waited."""
        if (
            not isinstance(request, DispatchTurn)
            or request.request_id is None
            or request.request_id.root not in self._acting
        ):
            return result
        bridge = self._bridge
        (descriptor,) = bridge.servers(_ROLE, request.scope)
        token = dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]
        reply, acknowledge = await bridge.answer(SubmitCall(token=token).model_dump_json().encode())
        acknowledge()  # the scripted agent read the reply
        parsed = json.loads(reply)
        self._asked.append(
            (
                request.scope.owner.root,
                request.turn.session.role_id.root,
                parsed.get("result", parsed),
            )
        )
        handle = parsed.get("result", {}).get("handle_id")
        if handle is not None and self._acting[request.request_id.root]:
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
