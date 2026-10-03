"""Contract of the fault layer: an empty plan is the identity; a rule fires once, as declared."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vs_agent.api import AgentOutputSchemaError, AgentTurnTimeoutError
from vs_agent.api.testing import FakeAgentClient
from vs_faults.api import (
    AgentCrashError,
    AgentFault,
    Boundary,
    ClusterFault,
    ClusterOperation,
    FaultPlan,
    FaultRule,
    FaultyAgentClient,
    FaultyToolDispatch,
    ReplyGenerator,
    ToolCallFailedError,
    ToolFault,
    classify,
    connector_command,
    generated_replies,
    handle_cluster_request,
    injected_faults,
)
from vs_slurm.fake_connector import executing_cluster


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


@given(seed=st.integers(0, 2**32), bold=st.booleans())
def test_generated_replies_satisfy_the_declared_schema(seed: int, *, bold: bool) -> None:
    plan = FaultPlan(seed=seed)
    generator = ReplyGenerator(plan.rng("t"), ("H1", "a-very-long-identifier"), bold=bold)

    reply = generator.valid(_Reply)

    assert isinstance(reply, _Reply)
    with pytest.raises(ValidationError):
        _Reply.model_validate(generator.invalid(_Reply))


def test_generated_replies_are_reproducible_from_the_seed() -> None:
    def replies(seed: int) -> list[str]:
        client = FakeAgentClient().set_response(_KIND, generated_replies(FaultPlan(seed=seed)))
        return [_turn(client).model_dump_json() for _ in range(4)]

    assert replies(3) == replies(3)
    assert replies(3) != replies(4)


@given(seed=st.integers(0, 2**32), faults=st.integers(0, 6))
def test_a_generated_plan_round_trips_and_is_reproducible(seed: int, faults: int) -> None:
    targets = {Boundary.AGENT_TURN: ("a", "b"), Boundary.TOOL_CALL: ("t",), Boundary.CLUSTER: ()}
    plan = FaultPlan.generate(seed, targets=targets, faults=faults)

    assert plan == FaultPlan.generate(seed, targets=targets, faults=faults)
    assert FaultPlan.model_validate_json(plan.model_dump_json()) == plan
    assert len(plan.rules) == faults


def test_a_plan_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="surprise"):
        FaultPlan.model_validate({"seed": 1, "rules": [], "surprise": True})


@pytest.mark.parametrize("fault", list(ToolFault))
def test_a_tool_fault_delivers_the_call_as_declared(fault: ToolFault) -> None:
    delivered: list[str] = []

    def dispatch(name: str, _arguments: object) -> dict[str, object]:
        delivered.append(name)
        return {"n": len(delivered)}

    rule = FaultRule(boundary=Boundary.TOOL_CALL, target="submit", at=1, fault=fault)
    tools = FaultyToolDispatch(dispatch, FaultPlan(seed=1, rules=(rule,)))

    if fault is ToolFault.DUPLICATE:
        assert tools("submit", {}) == {"n": 2}
    else:
        with pytest.raises(ToolCallFailedError):
            tools("submit", {})
    assert tools("status", {}) == {"n": len(delivered)}
    runs = {ToolFault.ERROR: 0, ToolFault.DROPPED: 0, ToolFault.TIMEOUT: 1, ToolFault.DUPLICATE: 2}
    assert delivered.count("submit") == runs[fault]


def _connector(plan: FaultPlan, base: Path, request: dict[str, object]) -> dict[str, object]:
    inner = [sys.executable, "-m", "vs_slurm.fake_connector", str(executing_cluster(base / "c"))]
    return handle_cluster_request(plan, base / "faults", inner, request)


@settings(max_examples=10, deadline=None)
@given(command=st.sampled_from(["echo hi", "squeue -h -j 5000 -o %T", "sacct -n -P -j 5000"]))
def test_an_empty_plan_answers_every_cluster_call_like_the_inner_connector(
    tmp_path_factory: pytest.TempPathFactory, command: str
) -> None:
    base = tmp_path_factory.mktemp("cluster")
    request: dict[str, object] = {"version": 1, "operation": "exec", "command": command}
    inner = [sys.executable, "-m", "vs_slurm.fake_connector", str(executing_cluster(base / "c"))]
    plan_file = FaultPlan(seed=0).save(base / "plan.json")
    wrapped = subprocess.run(  # noqa: S603  # LW-150010 [S603]; runs the wrapper's own CLI with a fixed argv, as the connector transport does.
        connector_command(plan_file, base / "faults", inner),
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    direct = subprocess.run(  # noqa: S603  # LW-150011 [S603]; the inner connector with a fixed argv, the reference answer.
        inner, input=json.dumps(request), capture_output=True, text=True, check=True
    )

    assert json.loads(wrapped.stdout) == json.loads(direct.stdout)
    assert injected_faults(base / "faults") == []


@pytest.mark.parametrize(
    ("fault", "operation", "command"),
    [
        (ClusterFault.SSH_DOWN, ClusterOperation.EXEC, "echo hi"),
        (ClusterFault.COMMAND_ERROR, ClusterOperation.SCANCEL, "scancel 5000"),
        (ClusterFault.COMMAND_ERROR, ClusterOperation.SBATCH, "cd /x && sbatch job.sh"),
    ],
)
def test_a_cluster_fault_fails_the_scheduled_call_without_reaching_the_cluster(
    tmp_path: Path, fault: ClusterFault, operation: ClusterOperation, command: str
) -> None:
    rule = FaultRule(boundary=Boundary.CLUSTER, target=operation.value, at=1, fault=fault)
    request: dict[str, object] = {"version": 1, "operation": "exec", "command": command}

    response = _connector(FaultPlan(seed=1, rules=(rule,)), tmp_path, request)

    assert classify(request)[0] is operation
    assert response["returncode"] != 0
    assert not (tmp_path / "c" / "requests.jsonl").exists()
    assert injected_faults(tmp_path / "faults") == [
        {"operation": operation.value, "at": 1, "fault": fault.value}
    ]


def test_a_killed_job_is_never_run_and_reports_how_it_died(tmp_path: Path) -> None:
    rule = FaultRule(boundary=Boundary.CLUSTER, target="sbatch", at=1, fault=ClusterFault.KILLED)
    plan = FaultPlan(seed=1, rules=(rule,))

    def call(command: str) -> dict[str, object]:
        return _connector(plan, tmp_path, {"version": 1, "operation": "exec", "command": command})

    submitted = str(call("cd /x && sbatch --output=/x/log job.sh")["stdout"])
    job = submitted.rsplit(maxsplit=1)[-1]

    assert call(f"squeue -h -j {job} -o %T")["stdout"] == ""
    state = str(call(f"sacct -n -P -j {job} -o State,ExitCode")["stdout"]).split()[0]
    assert state in {"OUT_OF_MEMORY", "PREEMPTED", "NODE_FAIL", "FAILED"}
    assert not (tmp_path / "c" / "requests.jsonl").exists()
