"""The agent-turn fault wrapper: an empty plan is the identity; a rule fires once, as declared."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Annotated, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vs_agent.api import (
    AgentClientProtocol,
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentTurnExecutor,
    AgentTurnRequest,
    AgentTurnTimeoutError,
)
from vs_agent.api.testing import (
    AgentCrashError,
    FakeAgentClient,
    FaultyAgentClient,
    generated_replies,
)
from vs_faults.api import (
    AgentFault,
    Boundary,
    FaultPlan,
    FaultRule,
)


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=12)
    weight: float = Field(ge=0.0, le=1.0)


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["plan"] = "plan"
    steps: list[_Step] = Field(min_length=1, max_length=3)
    parent: str | None = None
    final: bool = False


_KIND = "planner"


_ANSWER = _Reply(steps=[_Step(name="a", weight=0.5)])


def _turn(client: FaultyAgentClient | FakeAgentClient, prompt: str = "Use `H1`.") -> _Reply:
    return client.invoke(
        kind=_KIND,
        workspace=Path(),
        system_prompt="",
        user_prompt=prompt,
        response_cls=_Reply,
        round_label="r",
    )


@given(seed=st.integers(0, 2**32), turns=st.integers(1, 6))
def test_a_plan_without_agent_rules_passes_every_turn_through(seed: int, turns: int) -> None:
    plan = FaultPlan.generate(
        seed, targets={Boundary.TOOL_CALL: ("x",), Boundary.CLUSTER: ()}, faults=3
    )
    client = FaultyAgentClient(FakeAgentClient().set_response(_KIND, _ANSWER), plan)

    assert [_turn(client) for _ in range(turns)] == [_ANSWER] * turns
    assert client.injected == []


@pytest.mark.parametrize("fault", list(AgentFault))
def test_an_agent_fault_fires_on_its_turn_only(fault: AgentFault) -> None:
    rule = FaultRule(boundary=Boundary.AGENT_TURN, target=_KIND, at=2, fault=fault)
    inner = FakeAgentClient().set_response(_KIND, _ANSWER)
    client = FaultyAgentClient(inner, FaultPlan(seed=7, rules=(rule,)))

    assert _turn(client) == _ANSWER
    expected = {
        AgentFault.CRASH: AgentCrashError,
        AgentFault.TIMEOUT: AgentTurnTimeoutError,
        AgentFault.MALFORMED: AgentOutputSchemaError,
        AgentFault.SCHEMA_INVALID: AgentOutputSchemaError,
        AgentFault.EXTRA_KEYS: AgentOutputSchemaError,
    }.get(fault)
    if expected is None:
        assert isinstance(_turn(client), _Reply)
    else:
        with pytest.raises(expected):
            _turn(client)
    assert _turn(client) == _ANSWER
    assert client.injected == [(_KIND, 2, fault)]
    # Transport faults strike after the agent worked; output faults replace its answer.
    worked = fault in {AgentFault.CRASH, AgentFault.TIMEOUT, AgentFault.EXTRA_KEYS}
    assert len(inner.calls) == (3 if worked else 2)


def _raw_turn(client: FaultyAgentClient | FakeAgentClient, prompt: str = "Use `H1`.") -> _Reply:
    """One durable-journal turn: the path the runtime takes for every turn with an invocation id."""
    result = client.run(
        session_spec=AgentSessionSpec(
            role=_KIND, provider="fake", workspace=Path(), policy=AgentExecutionPolicy()
        ),
        turn=AgentTurnRequest(message=prompt, output_schema=_Reply, invocation_id="i"),
    )
    return _Reply.model_validate_json(result.text)


def _as_protocols(client: FaultyAgentClient) -> tuple[AgentClientProtocol, AgentTurnExecutor]:
    return client, client


def _public_members(protocol: type) -> dict[str, object]:
    return {
        name: member
        for name, member in vars(protocol).items()
        if not name.startswith("_") and (callable(member) or isinstance(member, property))
    }


@pytest.mark.parametrize("protocol", [AgentClientProtocol, AgentTurnExecutor])
def test_the_wrapper_matches_every_member_of_the_interfaces_the_runtime_dispatches_through(
    protocol: type,
) -> None:
    """Same members, same parameters: a future interface change cannot leave the wrapper behind."""
    for name, member in _public_members(protocol).items():
        mine = getattr(FaultyAgentClient, name, None)
        assert mine is not None, f"FaultyAgentClient lacks {protocol.__name__}.{name}"
        if isinstance(member, property):
            assert isinstance(mine, property), name
            continue
        expected = inspect.signature(getattr(protocol, name))
        actual = inspect.signature(mine)
        # Annotations are source text (postponed evaluation), so a spelling
        # difference fails loudly rather than slipping through.
        assert _shape(actual) == _shape(expected), name


def _shape(signature: inspect.Signature) -> tuple[object, ...]:
    return (
        tuple((p.name, p.kind, p.default, p.annotation) for p in signature.parameters.values()),
        signature.return_annotation,
    )


def test_a_wrapped_turn_executor_stays_a_turn_executor() -> None:
    """The runtime refuses a durable turn on a client that is not an AgentTurnExecutor."""
    client = FaultyAgentClient(FakeAgentClient().set_response(_KIND, _ANSWER), FaultPlan(seed=1))

    assert isinstance(client, AgentTurnExecutor)
    assert _as_protocols(client) == (client, client)
    assert [_raw_turn(client) for _ in range(3)] == [_ANSWER] * 3


@pytest.mark.parametrize("fault", list(AgentFault))
def test_an_agent_fault_fires_on_a_durable_turn_too(fault: AgentFault) -> None:
    rule = FaultRule(boundary=Boundary.AGENT_TURN, target=_KIND, at=2, fault=fault)
    inner = FakeAgentClient().set_response(_KIND, _ANSWER)
    client = FaultyAgentClient(inner, FaultPlan(seed=7, rules=(rule,)))

    assert _raw_turn(client) == _ANSWER
    # A raw turn returns text; the caller parses it, so output faults surface as parse errors.
    expected = {
        AgentFault.CRASH: AgentCrashError,
        AgentFault.TIMEOUT: AgentTurnTimeoutError,
        AgentFault.MALFORMED: ValidationError,
        AgentFault.SCHEMA_INVALID: ValidationError,
        AgentFault.EXTRA_KEYS: ValidationError,
    }.get(fault)
    if expected is None:
        assert isinstance(_raw_turn(client), _Reply)
    else:
        with pytest.raises(expected):
            _raw_turn(client)
    assert _raw_turn(client) == _ANSWER
    assert client.injected == [(_KIND, 2, fault)]


_SEEN_ID = "0123456789abcdef" * 4


class _Report(BaseModel):
    """A report whose cross-field rule the schema does not state."""

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["observed", "unsupported"]
    evidence_ids: tuple[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")], ...] = Field(
        default=(), max_length=4
    )
    reason: str | None = None

    def model_post_init(self, __context: object) -> None:
        if (self.outcome == "observed") == (not self.evidence_ids):
            message = "observed cites evidence; unsupported cites none"
            raise ValueError(message)
        if (self.outcome == "unsupported") != (self.reason is not None):
            message = "unsupported needs its reason"
            raise ValueError(message)


def test_generated_replies_are_reproducible_from_the_seed() -> None:
    def replies(seed: int) -> list[str]:
        client = FakeAgentClient().set_response(_KIND, generated_replies(FaultPlan(seed=seed)))
        return [_turn(client).model_dump_json() for _ in range(4)]

    assert replies(3) == replies(3)
    assert replies(3) != replies(4)


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str | None = Field(description="An existing id, or null.")
