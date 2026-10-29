"""A start request id launches once. Disconnect leaves the stored admission."""

from __future__ import annotations

import pytest

from vibesys.api.host_operations.api import StartRefusedError, StartUnknownError, serve_start
from vs_project.api.testing import FakeAdmissionStore
from vs_proto.api import (
    FailureCategory,
    MutateRequest,
    MutateResponse,
    Oneof,
    OperationFailure,
    validate,
)
from vs_proto.api import host_payloads as pb


def _payload(request_id: str = "request-a") -> bytes:
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "start_run",
            pb.StartRunRequest(request_id=request_id, project="/project", task="mpmc"),
        ),
    )
    validate(request)
    return request.to_binary()


def _response(payload: bytes) -> MutateResponse:
    response = MutateResponse.from_binary(payload)
    validate(response)
    return response


def test_the_same_request_launches_once_and_replays_the_run() -> None:
    launches: list[str] = []

    def launch(request: pb.StartRunRequest) -> tuple[str, str]:
        launches.append(request.request_id)
        return "run-1", "inst-1"

    store = FakeAdmissionStore()
    payload = _payload()
    first = _response(serve_start(payload, store=store, launch=launch, host_user="host-a"))
    second = _response(serve_start(payload, store=store, launch=launch, host_user="host-a"))

    assert launches == ["request-a"]
    assert first.result is not None
    assert first.result.field == "start_run"
    assert second.result == first.result
    started = first.result.value
    assert isinstance(started, pb.StartRunResponse)
    assert (started.run_id, started.instance_id) == ("run-1", "inst-1")


def test_an_unknown_launch_outcome_is_unconfirmed_and_does_not_relaunch() -> None:
    launches = 0

    def launch(_request: pb.StartRunRequest) -> tuple[str, str]:
        nonlocal launches
        launches += 1
        message = "outcome unknown"
        raise StartUnknownError(message)

    store = FakeAdmissionStore()
    payload = _payload()
    first = _response(serve_start(payload, store=store, launch=launch, host_user="host-a"))
    second = _response(serve_start(payload, store=store, launch=launch, host_user="host-a"))

    assert launches == 1
    for response in (first, second):
        assert response.result is not None
        assert response.result.field == "failure"
        failure = response.result.value
        assert isinstance(failure, OperationFailure)
        assert failure.category == FailureCategory.ADMISSION_UNCONFIRMED


def test_a_refusal_is_stored_and_not_launched_again() -> None:
    launches = 0

    def launch(_request: pb.StartRunRequest) -> tuple[str, str]:
        nonlocal launches
        launches += 1
        message = "the project is not ready"
        raise StartRefusedError(message)

    store = FakeAdmissionStore()
    payload = _payload()
    first = _response(serve_start(payload, store=store, launch=launch, host_user="host-a"))
    second = _response(serve_start(payload, store=store, launch=launch, host_user="host-a"))

    assert launches == 1
    assert first.result is not None
    assert first.result.field == "failure"
    assert first.result.value.message == "the project is not ready"
    assert second.result == first.result


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        b"\0" * ((1 << 20) + 1),
        MutateRequest(checkout="/checkout").to_binary(),
        MutateRequest(
            checkout="/checkout",
            method=Oneof(
                "remove_cluster", pb.RemoveClusterRequest(request_id="other-a", name="lab")
            ),
        ).to_binary(),
    ],
)
def test_invalid_start_envelopes_never_launch(payload: bytes) -> None:
    launches: list[str] = []

    def launch(request: pb.StartRunRequest) -> tuple[str, str]:
        launches.append(request.request_id)
        return "run-1", "inst-1"

    response = _response(
        serve_start(payload, store=FakeAdmissionStore(), launch=launch, host_user="host-a")
    )
    assert response.result is not None
    assert response.result.field == "failure"
    assert response.result.value.category == FailureCategory.INVALID_INPUT
    assert launches == []


def test_an_owned_start_and_a_changed_request_identity_are_conflicts() -> None:
    store = FakeAdmissionStore()
    launches: list[str] = []

    def launch(request: pb.StartRunRequest) -> tuple[str, str]:
        launches.append(request.request_id)
        busy = _response(serve_start(_payload(), store=store, launch=launch, host_user="host-a"))
        assert busy.result is not None
        assert busy.result.field == "failure"
        assert busy.result.value.category == FailureCategory.CONFLICT
        return "run-1", "inst-1"

    serve_start(_payload(), store=store, launch=launch, host_user="host-a")
    changed = MutateRequest.from_binary(_payload())
    assert changed.method is not None
    assert changed.method.field == "start_run"
    changed.method.value.task = "different-task"
    conflict = _response(
        serve_start(changed.to_binary(), store=store, launch=launch, host_user="host-a")
    )
    assert conflict.result is not None
    assert conflict.result.field == "failure"
    assert conflict.result.value.category == FailureCategory.CONFLICT
    assert launches == ["request-a"]
