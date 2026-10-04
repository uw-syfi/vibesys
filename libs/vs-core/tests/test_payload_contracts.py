"""Registered values cannot mutate receipts or depend on process hash order."""

import json
import os
import subprocess
import sys
from enum import Enum
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
from pydantic import BaseModel, ConfigDict, Field
from vs_core.api import (
    Capabilities, ExecuteRegisteredOperation, IntentsChange, LifecycleClass, OperationDescriptor,
    OperationId, OperationRegistration, OperationRegistry, OperationRequest,
    ReducerTrace, RequestPrepared, SchemaRef, Scope, TraceFrame, Value,
    initial_state, trace_step,
)
class Outcome(Value):
    status: Literal["succeeded"] = "succeeded"
class Request(OperationRequest):
    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Outcome
    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True)
    payload: tuple[frozenset[str], ...] = Field(alias="values")
codec = OperationRegistry((OperationRegistration(
    descriptor=OperationDescriptor(kind="test.query", lifecycle=LifecycleClass.QUERY,
        request_schema=SchemaRef(name="request", version=1),
        outcome_schema=SchemaRef(name="outcome", version=1)),
    request_model=Request, outcome_model=Outcome),))
wire = codec.encode(Request(payload=tuple(frozenset(x) for x in json.loads(sys.argv[1]))))
state = initial_state()
state = state.model_copy(update={
    "registry": codec.descriptors,
    "run": state.run.model_copy(update={"capabilities": Capabilities(operations=codec.descriptors)}),
})
scope = Scope(owner=state.run.run_id, generation=0)
request = ExecuteRegisteredOperation(scope=scope, deadline_at=100.0,
    operation_id=OperationId(root="query"), operation=wire, retry_limit=0)
event = RequestPrepared(request=request, lifecycle=LifecycleClass.QUERY)
result = trace_step(state, event, ReducerTrace(frames=(TraceFrame(signal=event,
    change=IntentsChange(state=state.intents, requests=(request,))),)))
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
        # LW-126001 [S603]; Fixed interpreter/program; generated payload is a separate argv.
        # > A shell wrapper adds quoting risks; multiprocessing inherits an initialized
        # > hash seed instead of starting an interpreter with the required seed.
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


@given(st.sampled_from([list[str], dict[str, int], set[str], tuple[list[str], ...]]))
def test_mutable_registered_outcome_fields_are_rejected(annotation: object) -> None:
    outcome = create_model("MutableOutcome", __base__=Value, payload=(annotation, ...))
    request = type(
        "Request",
        (ImmutableRequest,),
        {
            "__module__": __name__,
            "__annotations__": {"outcome_model": ClassVar[type[BaseModel]]},
            "outcome_model": outcome,
        },
    )
    entry = registration(request)
    entry = OperationRegistration(
        descriptor=entry.descriptor, request_model=request, outcome_model=outcome
    )
    with pytest.raises(ContractError, match="payload"):
        OperationRegistry((entry,))


@given(st.lists(st.text(), max_size=4))
def test_mutable_default_cannot_hide_behind_immutable_field_annotation(default: list[str]) -> None:
    request = create_model("Request", __base__=ImmutableRequest, payload=(tuple[str, ...], default))
    with pytest.raises(ContractError, match="payload"):
        OperationRegistry((registration(request),))


def test_mutable_enum_and_literal_values_are_rejected() -> None:
    enum = Enum("enum", {"VALUE": ["mutable"]})
    for annotation in (enum, Literal[enum.VALUE]):
        request = create_model("Request", __base__=ImmutableRequest, payload=(annotation, ...))
        with pytest.raises(ContractError, match="payload"):
            OperationRegistry((registration(request),))
