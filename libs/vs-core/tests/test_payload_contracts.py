"""Registered values cannot mutate receipts or depend on process hash order."""

import json
import os
import subprocess
import sys
from typing import ClassVar, Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, create_model

from vs_core.api import (
    ContractError,
    LifecycleClass,
    OperationDescriptor,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    SchemaRef,
    Value,
)


class Outcome(Value):
    status: Literal["succeeded"] = "succeeded"


class ImmutableRequest(OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Outcome


def registration(model: type[OperationRequest]) -> OperationRegistration:
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind="test.query",
            request_schema=SchemaRef(name="query", version=1),
            outcome_schema=SchemaRef(name="outcome", version=1),
            lifecycle=LifecycleClass.QUERY,
        ),
        request_model=model,
        outcome_model=Outcome,
    )


@given(st.sampled_from([list[str], dict[str, int], set[str], tuple[list[str], ...]]))
def test_mutable_registered_fields_are_rejected_recursively(annotation: object) -> None:
    nested = create_model("Nested", __base__=Value, payload=(annotation, ...))
    request = create_model("Request", __base__=ImmutableRequest, nested=(nested, ...))
    with pytest.raises(ContractError, match=r"nested\.payload"):
        OperationRegistry((registration(request),))


HASH_SCRIPT = """
import json, sys
from typing import ClassVar, Literal
from pydantic import BaseModel
from vs_core.api import *
class Outcome(Value):
    status: Literal["succeeded"] = "succeeded"
class Request(OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    payload: tuple[frozenset[str], ...]
codec = OperationRegistry((OperationRegistration(
    descriptor=OperationDescriptor(kind="test.query", lifecycle=LifecycleClass.QUERY,
        request_schema=SchemaRef(name="request", version=1),
        outcome_schema=SchemaRef(name="outcome", version=1)),
    request_model=Request, outcome_model=Outcome),))
wire = codec.encode(Request(payload=tuple(frozenset(x) for x in json.loads(sys.argv[1]))))
state = initial_state()
scope = Scope(owner=state.run.run_id, generation=0)
request = ExecuteRegisteredOperation(scope=scope, deadline_at=100.0,
    operation_id=OperationId(root="query"), operation=wire, retry_limit=0)
event = ClockAdvanced(now_at=1.0)
result = trace_step(state, event, ReducerTrace(frames=(TraceFrame(signal=event,
    change=SchedulingChange(state=state.scheduling, requests=(request,))),)))
print(json.dumps([wire.payload_json, result.requests[0].request_id.root]))
"""


@settings(max_examples=12)
@given(
    st.lists(
        st.sets(st.text(alphabet="abcdef", min_size=1, max_size=8), min_size=2, max_size=8),
        min_size=1,
        max_size=4,
    )
)
def test_wire_and_request_digest_are_independent_of_hash_seed(payload: list[set[str]]) -> None:
    argument = json.dumps([sorted(values) for values in payload])
    outputs = [
        # The executable and program are fixed. A shell wrapper adds an unnecessary
        # quoting boundary; generated payload is only a separate argv value.
        subprocess.run(  # noqa: S603
            [sys.executable, "-c", HASH_SCRIPT, argument],
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        for seed in ("1", "23")
    ]
    assert outputs[0] == outputs[1]
