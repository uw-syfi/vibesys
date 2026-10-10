"""vs-agent's Fakes and clients take their locks, events and sleeps from an injected ``Threads``.

Run on the cooperative simulator, a held turn, a cancel and a stub client's delay cost no
wall time and give the same outcome for every interleaving the schedule seed picks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_agent.api import (
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeProvider, FakeProviderError

# test-isolation: the stub client is deliberately outside the facade; its delay is the seam under test
from vs_agent.stub_runner import StubAgentClient
from vs_sim.api.testing import HANG_GUARD_S, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

SEEDS = st.one_of(st.none(), st.integers(0, 2**32))
CANCEL_DELAYS = st.sampled_from([0.25, 1.0, 4.0])
GUARDS = st.sampled_from([0.5, 2.0, 8.0])  # never equal to a cancel delay: no timer tie


class _Response(BaseModel):
    value: int


@given(seed=SEEDS, cancel_after=CANCEL_DELAYS, guard=GUARDS)
def test_a_held_turn_ends_in_a_cancel_exactly_when_the_cancel_beats_its_guard(
    seed: int | None, cancel_after: float, guard: float, tmp_path_factory: pytest.TempPathFactory
) -> None:
    workspace = tmp_path_factory.mktemp("workspace")
    threads = SimThreads(schedule_seed=seed)
    provider: FakeProvider

    def hold(_request: AgentTurnRequest) -> None:
        provider.hold_until_cancelled(guard)

    provider = FakeProvider(answer="done", on_turn=hold, threads=threads)
    session = provider.launch(
        AgentSessionSpec(
            role="implementer",
            provider="codex",
            workspace=workspace,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )
    )
    outcomes: list[str] = []
    started_at = threads.now()
    ended_at: list[float] = []

    def turn() -> None:
        try:
            outcomes.append(session.run_turn(AgentTurnRequest(message="go")).text)
        except FakeProviderError:
            outcomes.append("cancelled")
        ended_at.append(threads.now() - started_at)

    def scenario() -> None:
        worker = threads.spawn(turn, name="turn")
        threads.sleep(cancel_after)
        session.cancel()
        worker.join(HANG_GUARD_S)

    threads.run(scenario)

    assert threads.errors == []
    # A cancel ends the turn only while the guard still holds; after it the turn is over and
    # the cancel finds nothing running.
    cancel_won = cancel_after < guard
    assert outcomes == (["cancelled"] if cancel_won else ["done"])
    assert provider.cancel_count == (1 if cancel_won else 0)
    # Waiting costs virtual time only: the turn ended at the cancel or at the guard.
    assert ended_at == [pytest.approx(min(cancel_after, guard))]


def test_the_stub_client_delays_on_the_threads_it_is_given(tmp_path: Path) -> None:
    threads = SimThreads()
    stub = StubAgentClient(threads=threads)

    def invoke() -> None:
        with pytest.raises(AgentOutputSchemaError):
            stub.invoke(
                kind="worker",
                workspace=tmp_path,
                system_prompt="system",
                user_prompt="user",
                response_cls=_Response,
                round_label="stub-worker",
            )

    before = threads.now()
    threads.run(invoke)

    assert threads.now() - before == pytest.approx(0.05)
