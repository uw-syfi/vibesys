"""The neutral host contracts reject hostile bytes and preserve accepted values."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_proto.api import (
    ChatMessageRequest,
    ChatMessageResponse,
    FailureCategory,
    OperationFailure,
    PostIntentRequest,
    PostIntentResponse,
    RelaunchRequest,
    RelaunchResponse,
    ValidationError,
    from_mapping,
    host_operations,
    to_mapping,
    validate,
)
from vs_proto.api import (
    host_payloads as pb,
)


class _Case(TypedDict):
    name: str
    id: int
    payload: str
    valid: bool


_CORPUS = Path(__file__).resolve().parents[3] / "proto/testdata/operations/requests.json"


@pytest.mark.parametrize("case", json.loads(_CORPUS.read_text()), ids=lambda case: case["name"])
def test_three_language_request_corpus(case: _Case) -> None:
    operations = {op.operation_id: op for op in host_operations.OPERATIONS}
    try:
        operation = operations[case["id"]]
        operation.accept(operation.request.from_binary(bytes.fromhex(case["payload"])))
    except (KeyError, ValueError, ValidationError):
        assert not case["valid"]
    else:
        assert case["valid"]


@given(
    request_id=st.from_regex(r"[A-Za-z0-9]{1,20}", fullmatch=True),
    project=st.text(min_size=1, max_size=60),
)
def test_accepted_requests_own_bytes_and_validate_response_correlation(
    request_id: str, project: str
) -> None:
    builder = pb.ListTasksRequest(request_id=request_id, project=project)
    accepted = host_operations.accept_list_tasks(builder)
    builder.project = "changed"
    decoded = accepted.decode()
    assert decoded.project == project
    decoded.request_id = "changed"
    assert accepted.decode().request_id == request_id
    response = pb.ListTasksResponse(
        request_id=request_id, tasks=pb.TaskList(version=1, project_root=project)
    )
    assert host_operations.ListTasks.validate_response(accepted, response) == response.to_binary()
    response.request_id = request_id + "x"
    with pytest.raises(ValueError, match="correlation"):
        host_operations.ListTasks.validate_response(accepted, response)


def test_wrong_method_and_oversize_requests_are_rejected() -> None:
    accepted = host_operations.accept_list_tasks(
        pb.ListTasksRequest(request_id="a", project="/project")
    )
    with pytest.raises(ValueError, match="method"):
        host_operations.ListTasks.validate_response(
            accepted, cast("Any", pb.ListComputeResponse(request_id="a"))
        )
    with pytest.raises(TypeError, match="request type"):
        host_operations.ListTasks.accept(cast("Any", pb.ListComputeRequest(request_id="a")))
    with pytest.raises(ValidationError):
        host_operations.accept_list_tasks(
            pb.ListTasksRequest(request_id="a", project="x" * (1 << 21))
        )


@pytest.mark.parametrize("category", [0, 999])
def test_unknown_failure_categories_are_never_accepted(category: int) -> None:
    with pytest.raises(ValidationError):
        validate(OperationFailure(category=cast("FailureCategory", category), message="failure"))


@pytest.mark.parametrize("category", list(FailureCategory)[1:])
def test_every_declared_failure_category_validates(category: FailureCategory) -> None:
    validate(OperationFailure(category=category, message="diagnostic text"))


def test_temporary_projection_preserves_optional_empty_lists_and_union_tags() -> None:
    value = {"request_id": "request-a", "checks": [], "roles": [], "backend": None}
    wire = from_mapping(pb.PreflightRunResponse, value)
    assert to_mapping(wire) == value
    value["roles"] = None
    wire = from_mapping(pb.PreflightRunResponse, value)
    assert to_mapping(wire) == value
    with pytest.raises(ValueError, match="unknown keys"):
        from_mapping(
            pb.ListTasksRequest, {"request_id": "a", "project": "/project", "surprise": True}
        )


def test_accepted_input_bytes_are_deterministic_for_every_map_order() -> None:
    pairs = [
        ("role-a", pb.HarnessChoice(provider="codex", model="model-a")),
        ("role-b", pb.HarnessChoice(provider="claude", model="model-b")),
    ]
    left = pb.PreflightRunRequest(
        request_id="request-a",
        project="/project",
        agents=pb.PreflightRunRequestAgents(items=dict(pairs)),
    )
    right = pb.PreflightRunRequest(
        request_id="request-a",
        project="/project",
        agents=pb.PreflightRunRequestAgents(items=dict(reversed(pairs))),
    )
    assert (
        host_operations.accept_preflight_run(left).payload
        == host_operations.accept_preflight_run(right).payload
    )


def test_host_call_dispatch_uses_the_registered_slot_and_cap() -> None:
    assert [(op.slot, op.cli, op.cap_ms) for op in host_operations.HOST_CALLS] == [
        ("post_intent", "post-intent", 10000),
        ("relaunch", "relaunch", 30000),
        ("chat_message", "chat-message", 10000),
    ]
    seen: list[object] = []

    def post_intent(request: PostIntentRequest) -> PostIntentResponse:
        seen.append(request)
        return PostIntentResponse(request_id="posted", ordered_reader=True)

    def relaunch(request: RelaunchRequest) -> RelaunchResponse:
        raise AssertionError(request)

    def chat_message(request: ChatMessageRequest) -> ChatMessageResponse:
        raise AssertionError(request)

    result = host_operations.dispatch_host_call(
        "post_intent",
        "request",
        post_intent=post_intent,
        relaunch=relaunch,
        chat_message=chat_message,
    )
    assert result == PostIntentResponse(request_id="posted", ordered_reader=True)
    assert seen == ["request"]
    assert (
        host_operations.dispatch_host_call(
            "unknown",
            None,
            post_intent=post_intent,
            relaunch=relaunch,
            chat_message=chat_message,
        )
        is None
    )
