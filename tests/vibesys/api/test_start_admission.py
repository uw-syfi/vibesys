"""A start request id launches once. Disconnect leaves the stored admission."""

from __future__ import annotations

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
