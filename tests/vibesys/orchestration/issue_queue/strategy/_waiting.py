"""One host's side of agent turns that wait on their own evaluation through the production bridge.

A turn that waits calls the bridge's submit and wait tools as the evaluation tool server would,
then ends with its waiting reply. The bridge, the shell's admission and the continuation core
records are the production ones; only the workspace snapshot is faked.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.support.owed_submissions import owed_in
from tests.support.synthesized_evidence import SynthesizedEvidence

from vibesys.core_wiring import agent_evaluation_policy
from vibesys.orchestration.issue_queue.strategy.api import measurement_recipe
from vs_core.api import DispatchTurn, TurnObserved
from vs_evaluation.api import SubmitCall, WaitCall
from vs_runtime.api.core import (
    EVALUATION_TOOL_ID,
    AgentEvaluationBridge,
    AgentEvaluationRecords,
    ExecutionResult,
)
from vs_runtime.contracts import AgentRole, AgentTool

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.orchestration.issue_queue.strategy.api import IssueQueueConfig
    from vs_core.api import Request, RunFacts, Scope
    from vs_runtime.api.core import CoreRuntime
    from vs_sim.api.testing import VirtualClock

WAITING = json.dumps({"kind": "waiting_for_evaluation", "handles": ["h"]})
"""The reply of a turn that waits for the evaluation it submitted."""

_ROLE = AgentRole(
    id="agent", system_prompt="agent", extra_tools=(AgentTool(id=EVALUATION_TOOL_ID),)
)
# The bridge keeps its owed-submission records in files; a world has no directory of its own.
_SCRATCH = tempfile.TemporaryDirectory(prefix="issue-queue-world-")


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
    """What the turns that wait do on one host, once their executor has answered.

    ``waiting`` holds the identities of the dispatched turns the world made wait; they submit
    through the bridge and yield to it, so core records the wait. ``delays`` is how many
    event-loop yields the n-th dispatched turn of this host takes. The bridge is bound to this
    host's shell and clock.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-994711 [PLR0913]; the host's shell and clock, the run's config and facts, and the world's two facts about its turns are independent inputs.
        # > Bundling them would only move the same six values into a holder built at the one call site.
        self,
        shell: CoreRuntime[Any],
        clock: VirtualClock,
        config: IssueQueueConfig,
        facts: RunFacts,
        waiting: set[str],
        delays: Sequence[int],
    ) -> None:
        """Serve the evaluation tool for ``shell`` under the recipe of ``config``."""
        root = Path(tempfile.mkdtemp(dir=_SCRATCH.name))
        policy = agent_evaluation_policy(
            measurement_recipe(config, facts), accuracy=True, benchmark=True
        )
        self._bridge = AgentEvaluationBridge(
            root / "evaluation.sock",
            policy,
            _Workspaces(),
            AgentEvaluationRecords(owed_in(root / "owed"), SynthesizedEvidence()),
        )
        self._bridge.attach(shell, clock)
        self._clock = clock
        self._waiting = waiting
        self._delays = tuple(delays)
        self._dispatched = 0

    async def __call__(self, request: Request, result: ExecutionResult) -> ExecutionResult:
        """The turn's result, with the suspension the bridge recorded when it waited."""
        if not isinstance(request, DispatchTurn) or request.request_id is None:
            return result
        for _ in range(self._delays[self._dispatched % len(self._delays)]):
            # test-isolation: yields the event loop so turns interleave; no time passes
            await asyncio.sleep(0)
        self._dispatched += 1
        if request.request_id.root not in self._waiting:
            return result
        bridge = self._bridge
        (descriptor,) = bridge.servers(_ROLE, request.scope)
        token = dict(descriptor.runtime_env)["VS_EVALUATION_TOKEN"]
        reply, confirm = await bridge.answer(SubmitCall(token=token).model_dump_json().encode())
        handle = json.loads(reply).get("result", {}).get("handle_id")
        # The agent has read the reply; only now does its client confirm the receipt.
        confirm()
        if handle is not None:
            # The loop runs the submission while the agent works: it owns its job by now.
            await self._clock.sleep(3.0)
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
