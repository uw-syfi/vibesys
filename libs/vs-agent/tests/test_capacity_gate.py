"""``AgentClient`` hands a provider capacity limit to the installed gate."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import agentshim
import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentClient,
    AgentQuotaError,
    AgentTurnRequest,
    CapacityStops,
    QuotaCondition,
)
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable


def _quota(detail: str = "limit") -> AgentQuotaError:
    return AgentQuotaError("claude", QuotaCondition.QUOTA_EXHAUSTED, detail)


@dataclass
class _Gate:
    """A gate that returns at once, or raises ``refusal``, recording each call."""

    refusal: Exception | None = None
    calls: list[tuple[AgentQuotaError, str, str | None, int]] = field(default_factory=list)
    served: list[tuple[str, str | None]] = field(default_factory=list)
    models: list[str | None] = field(default_factory=list)

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, stops: CapacityStops
    ) -> None:
        self.calls.append((error, role, turn.invocation_id, stops.count))
        self.models.append(stops.model)
        if self.refusal is not None:
            raise self.refusal

    def turn_served(self, provider: str, model: str | None) -> None:
        self.served.append((provider, model))


def _invoke(client: AgentClient, workspace: Path) -> str:
    return client.invoke_text(
        kind="implementer",
        workspace=workspace,
        system_prompt="sys",
        user_prompt="go",
        round_label="r1",
        invocation_id="inv-1",
    )


def _client(
    answers: tuple[AgentQuotaError | str, ...],
    on_send: Callable[[AgentTurnRequest], None] | None = None,
) -> AgentClient:
    driver = FakeProvider(
        script=FakeTurnScript(answers=answers), on_send=on_send, threads=SimThreads()
    )
    return AgentClient(
        driver, provider="mock", model_name="m", clock=ManualClock(), threads=SimThreads()
    )


def test_without_a_gate_the_quota_error_reaches_the_caller(tmp_path: Path) -> None:
    client = _client((_quota(), "ok"))

    with pytest.raises(AgentQuotaError):
        _invoke(client, tmp_path)


@given(stops=st.integers(min_value=1, max_value=5))
def test_the_turn_is_sent_again_after_each_gate_return(stops: int) -> None:
    gate = _Gate()
    client = _client((*[_quota(f"stop {n}") for n in range(stops)], "ok"))
    client.set_capacity_gate(gate)

    with TemporaryDirectory() as tmp:
        text = _invoke(client, Path(tmp))

    assert text == "ok"
    assert [(error.detail, role, inv, attempt) for error, role, inv, attempt in gate.calls] == [
        (f"stop {n}", "implementer", "inv-1", n + 1) for n in range(stops)
    ]
    # Only the send that got through counts as a turn the route served, and the gate is told
    # which model the turn runs on, so a down model is told apart from a served one.
    assert gate.served == [("mock", "m")]
    assert gate.models == ["m"] * stops


def test_a_gate_that_raises_ends_the_turn_with_its_error(tmp_path: Path) -> None:
    gate = _Gate(refusal=RuntimeError("give up"))
    client = _client((_quota(), "ok"))
    client.set_capacity_gate(gate)

    with pytest.raises(RuntimeError, match="give up"):
        _invoke(client, tmp_path)

    assert len(gate.calls) == 1


@given(
    conditions=st.lists(st.sampled_from(QuotaCondition), min_size=1, max_size=5),
)
def test_each_capacity_stop_reaches_the_gate_as_the_condition_the_provider_reported(
    conditions: list[QuotaCondition],
) -> None:
    """An outage (a transient failure with no exhausted window) waits at the gate too.

    The scripted provider reports ``UNAVAILABLE`` the way a gateway answering ``503`` does once
    agentshim's retries ran out: a transient failure that names no limit. Before, that failure
    skipped the gate and failed the turn.
    """
    gate = _Gate()
    client = _client(
        (*[AgentQuotaError("claude", c, f"stop {n}") for n, c in enumerate(conditions)], "ok")
    )
    client.set_capacity_gate(gate)

    with TemporaryDirectory() as tmp:
        text = _invoke(client, Path(tmp))

    assert text == "ok"
    assert [(error.condition, count) for error, _, _, count in gate.calls] == [
        (condition, n + 1) for n, condition in enumerate(conditions)
    ]


@dataclass
class _CancellingGate(_Gate):
    """A gate whose wait the caller cancels: it returns, but the turn must not be sent again."""

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, stops: CapacityStops
    ) -> None:
        super().wait_for_capacity(error, turn, role=role, stops=stops)
        stops.cancel()


def test_a_turn_cancelled_while_the_gate_held_it_is_not_sent_again(tmp_path: Path) -> None:
    gate = _CancellingGate()
    client = _client((_quota("first"), "ok"))
    client.set_capacity_gate(gate)

    with pytest.raises(AgentQuotaError, match="first"):
        _invoke(client, tmp_path)

    assert len(gate.calls) == 1
    assert gate.served == []


@given(cancelled_send=st.integers(min_value=2, max_value=4))
def test_a_cancel_after_the_gate_let_the_turn_go_but_before_it_is_sent_is_not_lost(
    cancelled_send: int,
) -> None:
    """The window between the gate's return and the send, when an interrupt stops nothing.

    The cancel reaches the session while the library has no turn in flight, so the
    session's interrupt is a no-op. Before, the next send cleared the turn's cancelled
    flag and ran to completion; now the turn's record keeps the cancel, and the send
    never starts.
    """
    gate = _Gate()
    sends: list[int] = []
    client: AgentClient | None = None

    def on_send(request: AgentTurnRequest) -> None:
        del request
        sends.append(len(sends) + 1)
        if len(sends) == cancelled_send and client is not None:
            client.cancel()

    client = _client(
        (*[_quota(f"stop {n}") for n in range(cancelled_send - 1)], "ok"), on_send=on_send
    )
    client.set_capacity_gate(gate)

    with TemporaryDirectory() as tmp, pytest.raises(agentshim.TurnCancelledError):
        _invoke(client, Path(tmp))

    assert len(gate.calls) == cancelled_send - 1
    assert gate.served == []
