"""``AgentClient`` hands a provider capacity limit to the installed gate."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

import agentshim
import pytest
from hypothesis import example, given
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


# --- a client no run holds: its own gate resends a transient stop once ----------------------

_TRANSIENT = (QuotaCondition.UNAVAILABLE, QuotaCondition.RATE_LIMITED)
_RESEND_WAIT_S = 30.0
"""The wait before the one resend, as before #3300 (``vs_agent.ungated_retry``)."""


def _ungated_outcome(conditions: list[QuotaCondition]) -> tuple[QuotaCondition | None, int]:
    """What a client with no gate installed ends with: the condition it raises, and its sends.

    Only a transient first stop is sent again, once; ``None`` is the scripted answer.
    """
    if not conditions:
        return None, 1
    if conditions[0] not in _TRANSIENT:
        return conditions[0], 1
    return (conditions[1] if len(conditions) > 1 else None), 2


@pytest.mark.regression
@example(conditions=[QuotaCondition.UNAVAILABLE])  # one 529 ended a chat turn (#3300)
@given(conditions=st.lists(st.sampled_from(QuotaCondition), max_size=4))
def test_a_client_no_run_holds_resends_one_transient_stop_after_a_wait(
    conditions: list[QuotaCondition],
) -> None:
    """A client with no gate (the server's chat agent) rides out one overload.

    A run's gate replaces this resend; a client no run holds has nothing else between a
    single 529 and a failed turn. An exhausted quota cannot return within the wait, so it is
    never sent again, and a stop that outlasts the one resend reaches the caller.
    """
    threads = SimThreads()
    sends: list[float] = []
    driver = FakeProvider(
        script=FakeTurnScript(
            answers=(
                *[AgentQuotaError("claude", c, f"stop {n}") for n, c in enumerate(conditions)],
                "ok",
            )
        ),
        on_send=lambda _request: sends.append(threads.clock.now()),
        threads=threads,
    )
    client = AgentClient(
        driver, provider="mock", model_name="m", clock=ManualClock(), threads=threads
    )
    raised: list[QuotaCondition] = []

    def main() -> str | None:
        with TemporaryDirectory() as tmp:
            try:
                return _invoke(client, Path(tmp))
            except AgentQuotaError as error:
                raised.append(error.condition)
                return None

    text = threads.run(main)

    expected, sent = _ungated_outcome(conditions)
    assert (raised[0] if raised else None) == expected
    assert (text == "ok") == (expected is None)
    assert len(sends) == sent
    assert [later - earlier for earlier, later in itertools.pairwise(sends)] == [_RESEND_WAIT_S] * (
        sent - 1
    )


def test_cancelling_a_turn_while_it_waits_to_be_resent_ends_it_unsent() -> None:
    threads = SimThreads()
    sends: list[float] = []
    driver = FakeProvider(
        script=FakeTurnScript(
            answers=(AgentQuotaError("claude", QuotaCondition.UNAVAILABLE, "529"), "ok")
        ),
        on_send=lambda _request: sends.append(threads.clock.now()),
        threads=threads,
    )
    client = AgentClient(
        driver, provider="mock", model_name="m", clock=ManualClock(), threads=threads
    )

    def cancel_during_the_wait() -> None:
        threads.sleep(_RESEND_WAIT_S / 3)
        client.cancel()

    def main() -> float:
        threads.spawn(cancel_during_the_wait, name="canceller")
        with TemporaryDirectory() as tmp, pytest.raises(AgentQuotaError, match="529"):
            _invoke(client, Path(tmp))
        return threads.clock.now()

    ended = threads.run(main)

    assert len(sends) == 1
    assert ended - sends[0] == _RESEND_WAIT_S / 3
