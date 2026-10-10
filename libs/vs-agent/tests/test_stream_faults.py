"""Process and conversation faults against agentshim's real stream transports.

The far end is agentshim's scripted Claude CLI or Codex app-server, so the real
transports and the real ``Session`` recovery policy run; only the process (or
the conversation above it) is broken by the plan. One scenario runs over every
provider agentshim lists in ``stream_provider_names()``.

Properties, over every position in a turn and every fault:

* the caller gets a typed outcome (a result or an ``AgentShimError``), never a
  hang, a bare exception or a silent wrong answer;
* after the single scheduled fault the next turn succeeds (the session heals);
* a fault never makes the provider receive a prompt twice.
"""

from __future__ import annotations

import agentshim
import pytest
from agentshim.testing import FakeClock, FakeExecutor, SequentialIds
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api.testing import FaultyExecutor, FaultyTransport, StreamPeers, stream_peers
from vs_faults.api import Boundary, ConversationFault, FaultPlan, FaultRule, ProcessFault

PROVIDERS = tuple(agentshim.stream_provider_names())
TURN_BUDGET_S = 5.0


def _plan(boundary: Boundary, at: int, fault: ProcessFault | ConversationFault) -> FaultPlan:
    return FaultPlan(
        seed=1, rules=(FaultRule(boundary=boundary, target="agent", at=at, fault=fault),)
    )


def _agent(
    provider: str, peers: StreamPeers, plan: FaultPlan
) -> tuple[agentshim.Agent, FaultyExecutor]:
    executor = FaultyExecutor(
        FakeExecutor([], peers=peers.build),
        plan,
        on_container_replaced=peers.forget_conversations,
    )
    agent = agentshim.Agent(
        provider,
        executor=executor,
        permissions=agentshim.NativePermissions.bypass(),
        approvals=agentshim.ApprovalPolicy.DENY,
        transport=agentshim.TransportKind.STREAM,
        clock=FakeClock(),
        ids=SequentialIds(),
        retry=agentshim.RetryPolicy(delays=()),
    )
    return agent, executor


def _turn(session: agentshim.Session, prompt: str) -> agentshim.Turn:
    ticket = session.prepare_turn(agentshim.TurnRequest(prompt=prompt, timeout=TURN_BUDGET_S))
    return session.run(ticket)


def _clean_lines(provider: str) -> int:
    """The stdout lines two clean turns produce: the positions a sweep visits."""
    agent, executor = _agent(provider, stream_peers(provider, "one", "two"), FaultPlan(seed=1))
    with agent.session("/work") as session:
        _turn(session, "first")
        _turn(session, "second")
    return executor.lines


@pytest.mark.parametrize("provider", PROVIDERS)
def test_an_empty_plan_changes_nothing(provider: str) -> None:
    peers = stream_peers(provider, "one", "two")
    agent, executor = _agent(provider, peers, FaultPlan(seed=1))
    with agent.session("/work") as session:
        first = _turn(session, "first")
        second = _turn(session, "second")

    assert (first.result.text, second.result.text) == ("one", "two")
    assert executor.injected == []
    assert peers.prompts() == ["first", "second"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("fault", list(ProcessFault))
def test_a_process_fault_at_every_position_ends_typed_and_the_session_heals(
    provider: str, fault: ProcessFault
) -> None:
    for position in range(1, _clean_lines(provider) + 1):
        peers = stream_peers(provider, "one", "two", "three")
        agent, executor = _agent(provider, peers, _plan(Boundary.PROCESS_OUTPUT, position, fault))
        failures: list[agentshim.AgentShimError] = []
        healed = None
        with agent.session("/work") as session:
            for prompt in ("first", "second", "third"):
                try:
                    turn = _turn(session, prompt)
                except agentshim.AgentShimError as error:
                    failures.append(error)
                    continue
                healed = turn.result.text
        where = f"{provider} {fault.value} at line {position}"
        assert executor.injected == [(position, fault)], where
        # The fault costs the turn it struck. A stopped server that ignored an interrupt can
        # also cost the next turn that finds it gone; the session then lives on.
        budget = 2 if fault in (ProcessFault.HANG, ProcessFault.MALFORMED) else 1
        assert len(failures) <= budget, where
        assert healed is not None, where
        # Whatever happened to a turn, the provider never saw a prompt twice.
        received = peers.prompts()
        assert len(received) == len(set(received)), where


@pytest.mark.parametrize("provider", PROVIDERS)
def test_a_replaced_container_loses_its_conversations_and_is_counted(provider: str) -> None:
    peers = stream_peers(provider, "one", "two")
    agent, executor = _agent(
        provider, peers, _plan(Boundary.PROCESS_OUTPUT, 3, ProcessFault.CONTAINER_REPLACED)
    )
    with agent.session("/work") as session:
        outcomes: list[str] = []
        for prompt in ("first", "second", "third"):
            try:
                outcomes.append(_turn(session, prompt).continuity.value)
            except agentshim.AgentShimError as error:
                outcomes.append(type(error).__name__)

    assert executor.replacements == 1
    assert outcomes[-1] != "CliExitError"


@given(
    provider=st.sampled_from(PROVIDERS),
    fault=st.sampled_from(list(ConversationFault)),
    at=st.integers(min_value=1, max_value=3),
)
def test_a_conversation_fault_is_the_error_the_session_expects_and_never_replays(
    provider: str, fault: ConversationFault, at: int
) -> None:
    peers = stream_peers(provider, "one", "two", "three", "four", "five")
    transport = agentshim.Agent(
        provider,
        executor=FakeExecutor([], peers=peers.build),
        permissions=agentshim.NativePermissions.bypass(),
        approvals=agentshim.ApprovalPolicy.DENY,
        transport=agentshim.TransportKind.STREAM,
        clock=FakeClock(),
        ids=SequentialIds(),
    ).transport
    faulty = FaultyTransport(transport, _plan(Boundary.CONVERSATION_TURN, at, fault))
    agent = agentshim.Agent(
        faulty,
        permissions=agentshim.NativePermissions.bypass(),
        approvals=agentshim.ApprovalPolicy.DENY,
        clock=FakeClock(),
        ids=SequentialIds(),
        retry=agentshim.RetryPolicy(delays=(0.0,)),
    )
    results: list[agentshim.Turn | agentshim.AgentShimError] = []
    with agent.session("/work") as session:
        for prompt in ("a", "b", "c", "d"):
            try:
                results.append(_turn(session, prompt))
            except agentshim.AgentShimError as error:
                results.append(error)

    assert faulty.injected[0] == (at, fault)
    assert all(isinstance(r, (agentshim.Turn, agentshim.AgentShimError)) for r in results)
    # A turn that was faulted after the provider answered is lost, not repeated:
    # no prompt reached the provider twice except the transient retry the policy allows.
    received = peers.prompts()
    expected_repeats = 1 if fault is ConversationFault.TRANSIENT else 0
    assert len(received) - len(set(received)) <= expected_repeats + (
        1 if fault is ConversationFault.RESUME_REFUSED else 0
    )
    # The session outlives the fault: the last turn answers.
    assert isinstance(results[-1], agentshim.Turn)
