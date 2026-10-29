"""A dispatched turn that asks the role's own prompt may leave a conversation near its window.

The SESSIONS executor tells the agent session which turns stand alone: a paid or free turn
carries the role's whole prompt, so it may run in a fresh conversation when the one it would
continue reached ``CONTEXT_RENEWAL_FRACTION`` of the model's window (S185). A correction
answers the reply before it, so it always continues that conversation.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support.session_world import (
    SESSION,
    FakeSessionResolver,
    SessionHost,
    dispatch_request,
    ensure_request,
    open_host,
    turn_spec,
)

from vs_agent.api import AgentClient, AgentSessionSpec, AgentTurnRequest
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_core.api import DispatchTurn, InvocationId, InvocationRef, ObservationStatus, RequestId
from vs_project.api import Project
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import ExecutionResult, ReceiptStore
from vs_sim.api.testing import ManualClock, SimThreads, examples, run_virtual

if TYPE_CHECKING:
    from vs_core.api import TurnSpec
    from vs_runtime.api.core import AccessGuardedWorkspace

_MODEL = "claude-opus-4-7"
_WINDOW = 1_000_000


@dataclass
class _OpusResolver(FakeSessionResolver):
    """The world's resolver, with a model whose context window agentshim knows."""

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        spec = super().agent_spec(turn, workspace)
        return None if spec is None else replace(spec, provider="claude", model=_MODEL)


def _correction(invocation: str, follows: str) -> DispatchTurn:
    """A correction of *follows*: it answers that turn's reply."""
    turn = turn_spec(invocation).model_copy(
        update={
            "charge_class": "correction",
            "predecessor": InvocationRef(
                session_id=SESSION, invocation_id=InvocationId(root=follows), generation=0
            ),
        }
    )
    return dispatch_request(f"req-{invocation}", invocation).model_copy(
        update={"turn": turn, "request_id": RequestId(root=f"req-{invocation}")}
    )


async def _three_turns(host: SessionHost, base: Path) -> list[ExecutionResult]:
    """Two free turns of one session, then a correction of the second."""
    (base / "project").mkdir()
    store = ReceiptStore(
        Project.open(base / "project").state.state_store_namespace("run"), threads=SimThreads()
    )
    await host.run(ensure_request(), store)
    return [
        await host.run(dispatch_request("req-1", "inv-1"), store),
        await host.run(dispatch_request("req-2", "inv-2"), store),
        await host.run(_correction("inv-3", "inv-2"), store),
    ]


@settings(max_examples=examples(pr=8, nightly=60))
@given(tokens=st.integers(0, _WINDOW))
@example(tokens=850_000)
@example(tokens=799_999)
def test_a_free_turn_leaves_a_full_conversation_and_a_correction_continues_the_new_one(
    tokens: int,
) -> None:
    sent: list[AgentTurnRequest] = []
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        (base / "workspace").mkdir()
        host = open_host(base / "workspace")
        host.resolver = _OpusResolver(base / "workspace", TemplateRenderer(base / "workspace"))
        sim = SimThreads()
        provider = FakeProvider(
            script=FakeTurnScript(({"value": 7},), context_tokens=(tokens,)),
            on_turn=sent.append,
            threads=sim,
        )
        host.client = AgentClient(provider, clock=ManualClock(), threads=sim)
        results = run_virtual(sim.clock, _three_turns(host, base), driver=sim)
        host.client.close()

    statuses = [result.observation.observation.status for result in results]
    assert statuses == [ObservationStatus.SUCCEEDED] * 3
    first, second, correction = sent
    assert first.expected_provider_session_id is None
    renewed = tokens >= _WINDOW * 4 // 5
    assert (second.expected_provider_session_id is None) == renewed
    # The correction continues whichever conversation answered the turn it corrects.
    assert correction.expected_provider_session_id == (
        "fake-worker-2" if renewed else "fake-worker-1"
    )
