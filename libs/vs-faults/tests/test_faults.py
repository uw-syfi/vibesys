"""Contract of the fault layer: an empty plan is the identity; a rule fires once, as declared."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Annotated, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vs_faults.api import (
    Boundary,
    ClusterFault,
    ClusterOperation,
    FaultPlan,
    FaultRule,
    ReplyGenerator,
    classify,
    connector_command,
    handle_cluster_request,
    handle_cluster_request_with,
    injected_faults,
)
from vs_slurm.fake_connector import executing_cluster
from vs_slurm.fake_connector import handle as fake_cluster_handle

if TYPE_CHECKING:
    from pathlib import Path


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


@given(seed=st.integers(0, 2**32), bold=st.booleans())
def test_generated_replies_satisfy_the_declared_schema(seed: int, *, bold: bool) -> None:
    plan = FaultPlan(seed=seed)
    generator = ReplyGenerator(plan.rng("t"), ("H1", "a-very-long-identifier"), bold=bold)

    reply = generator.valid(_Reply)

    assert isinstance(reply, _Reply)
    with pytest.raises(ValidationError):
        _Reply.model_validate(generator.invalid(_Reply))


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


@given(seed=st.integers(0, 2**32))
def test_a_careful_reply_cites_only_identifiers_it_has_seen(seed: int) -> None:
    """A generated observed report could not cite evidence, so every one was rejected."""
    generator = ReplyGenerator(FaultPlan(seed=seed).rng("t"), ("H1", _SEEN_ID))

    reply = generator.valid(_Report)

    if reply is not None:
        assert set(reply.evidence_ids) <= {_SEEN_ID}


def test_careful_replies_reach_every_outcome_a_cross_field_rule_allows() -> None:
    outcomes = {
        reply.outcome
        for seed in range(64)
        if (reply := ReplyGenerator(FaultPlan(seed=seed).rng("t"), (_SEEN_ID,)).valid(_Report))
    }

    assert outcomes == {"observed", "unsupported"}


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


@given(command=st.sampled_from(["echo hi", "squeue -h -j 5000 -o %T", "sacct -n -P -j 5000"]))
@settings(max_examples=10, deadline=None)
def test_an_in_process_forward_is_asked_once_by_an_empty_plan_and_never_by_a_faulted_call(
    tmp_path_factory: pytest.TempPathFactory, command: str
) -> None:
    """The in-process entry point answers like the process one, and a fault skips the cluster."""
    base = tmp_path_factory.mktemp("cluster")
    state = executing_cluster(base / "c")
    request: dict[str, object] = {"version": 1, "operation": "exec", "command": command}
    forwarded: list[dict[str, object]] = []

    def forward(sent: dict[str, object]) -> dict[str, object]:
        forwarded.append(sent)
        return fake_cluster_handle(state, sent)

    quiet = handle_cluster_request_with(FaultPlan(seed=0), base / "quiet", forward, request)

    assert forwarded == [request]
    assert quiet == fake_cluster_handle(state, request)
    operation = classify(request)[0]
    rule = FaultRule(
        boundary=Boundary.CLUSTER, target=operation.value, at=1, fault=ClusterFault.SSH_DOWN
    )
    faulted = handle_cluster_request_with(
        FaultPlan(seed=0, rules=(rule,)), base / "faulted", forward, request
    )

    assert forwarded == [request]
    assert faulted["returncode"] != 0


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


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str | None = Field(description="An existing id, or null.")


@given(seed=st.integers(0, 2**32))
def test_a_careful_agent_answers_null_rather_than_invent_a_reference(seed: int) -> None:
    reply = ReplyGenerator(FaultPlan(seed=seed).rng("t"), ("H1",)).valid(_Target)

    assert reply == _Target(target=None)
