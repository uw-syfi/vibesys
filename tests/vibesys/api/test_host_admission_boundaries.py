"""Admission boundaries preserve identity and never repeat uncertain effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.api.host_operations.api import (
    ClusterRefusedError,
    ClusterUnknownError,
    ProjectRefusedError,
    ProjectUnknownError,
    serve_cluster,
    serve_project,
)
from vs_project.api.testing import FakeAdmissionStore
from vs_proto.api import FailureCategory, MutateRequest, MutateResponse, Oneof, validate
from vs_proto.api import host_payloads as pb

if TYPE_CHECKING:
    from collections.abc import Callable

_SLOTS = ("put_cluster", "remove_cluster", "test_cluster", "create_example", "copy_project")


def _request(slot: str, request_id: str = "request-a") -> bytes:
    cluster = pb.SlurmClusterSettings(
        name="lab",
        reach=pb.SlurmClusterSettingsReach(
            value=Oneof("ssh_reach", pb.SshReach(kind="ssh", host="host-a"))
        ),
        workspace="/work",
    )
    if slot == "put_cluster":
        request = MutateRequest(
            checkout="/checkout",
            method=Oneof(
                "put_cluster", pb.PutClusterRequest(request_id=request_id, cluster=cluster)
            ),
        )
    elif slot == "remove_cluster":
        request = MutateRequest(
            checkout="/checkout",
            method=Oneof(
                "remove_cluster", pb.RemoveClusterRequest(request_id=request_id, name="lab")
            ),
        )
    elif slot == "test_cluster":
        request = MutateRequest(
            checkout="/checkout",
            method=Oneof(
                "test_cluster", pb.TestClusterRequest(request_id=request_id, cluster=cluster)
            ),
        )
    elif slot == "create_example":
        request = MutateRequest(
            checkout="/checkout",
            method=Oneof(
                "create_example",
                pb.CreateExampleRequest(
                    request_id=request_id, example="queue", destination="/work/queue"
                ),
            ),
        )
    else:
        assert slot == "copy_project"
        request = MutateRequest(
            checkout="/checkout",
            method=Oneof(
                "copy_project",
                pb.CopyProjectRequest(
                    request_id=request_id, source="/work/source", destination="/work/queue"
                ),
            ),
        )
    validate(request)
    return request.to_binary()


def _answer(slot: str, request: object) -> object:
    assert isinstance(
        request,
        pb.PutClusterRequest
        | pb.RemoveClusterRequest
        | pb.TestClusterRequest
        | pb.CreateExampleRequest
        | pb.CopyProjectRequest,
    )
    request_id = request.request_id
    settings = pb.ComputeSettings(
        config_path="/settings", modal=pb.ModalStatus(token_present=False)
    )
    return {
        "put_cluster": pb.PutClusterResponse(request_id=request_id, settings=settings),
        "remove_cluster": pb.RemoveClusterResponse(request_id=request_id, settings=settings),
        "test_cluster": pb.TestClusterResponse(
            request_id=request_id,
            result=Oneof("passed", pb.ClusterTestPassed(hardware=pb.ClusterHardware())),
        ),
        "create_example": pb.CreateExampleResponse(
            request_id=request_id,
            result=Oneof("created", pb.CreatedProject(project="/work/queue", tasks=["task-a"])),
        ),
        "copy_project": pb.CopyProjectResponse(
            request_id=request_id,
            created=pb.CreatedProject(project="/work/queue", tasks=[]),
        ),
    }[slot]


def _serve(
    slot: str, payload: bytes, store: FakeAdmissionStore, apply: Callable[[str, object], object]
) -> MutateResponse:
    serve = serve_project if slot in {"create_example", "copy_project"} else serve_cluster
    response = MutateResponse.from_binary(
        serve(payload, store=store, host_user="host-a", apply=apply)
    )
    validate(response)
    return response


@pytest.mark.parametrize("slot", _SLOTS)
def test_every_mutation_replays_a_confirmed_result_and_refuses_a_busy_owner(slot: str) -> None:
    store = FakeAdmissionStore()
    payload = _request(slot)
    applied: list[str] = []

    def apply(name: str, request: object) -> object:
        applied.append(name)
        busy = _serve(slot, payload, store, _answer)
        assert busy.result is not None
        assert busy.result.field == "failure"
        assert busy.result.value.category == FailureCategory.CONFLICT
        return _answer(name, request)

    first = _serve(slot, payload, store, apply)
    assert first.result is not None
    assert first.result.field == slot
    assert _serve(slot, payload, store, apply) == first
    assert applied == [slot]


@pytest.mark.parametrize("slot", _SLOTS)
def test_an_uncertain_effect_is_not_reapplied_or_displaced_by_another_request(slot: str) -> None:
    store = FakeAdmissionStore()
    applied: list[str] = []

    def apply(name: str, _request: object) -> object:
        applied.append(name)
        error = (
            ProjectUnknownError
            if name in {"create_example", "copy_project"}
            else ClusterUnknownError
        )
        raise error

    first = _serve(slot, _request(slot), store, apply)
    assert first.result is not None
    assert first.result.field == "failure"
    assert first.result.value.category == FailureCategory.ADMISSION_UNCONFIRMED
    assert _serve(slot, _request(slot), store, apply) == first
    other = _serve(slot, _request(slot, "request-b"), store, apply)
    assert other.result is not None
    assert other.result.field == "failure"
    assert other.result.value.category == FailureCategory.CONFLICT
    assert applied == [slot]


@pytest.mark.parametrize("slot", _SLOTS)
@pytest.mark.parametrize(
    "message", ["  ", pytest.param("é" * 40000, id="40000-two-byte-characters")]
)
def test_refusals_are_bounded_valid_utf8_and_replay_without_applying(
    slot: str, message: str
) -> None:
    applied: list[str] = []

    def apply(name: str, _request: object) -> object:
        applied.append(name)
        error = (
            ProjectRefusedError
            if name in {"create_example", "copy_project"}
            else ClusterRefusedError
        )
        raise error(message)

    store = FakeAdmissionStore()
    response = _serve(slot, _request(slot), store, apply)
    assert response.result is not None
    assert response.result.field == "failure"
    assert response.result.value.message.strip()
    assert len(response.result.value.message.encode()) <= 65536
    assert _serve(slot, _request(slot), store, apply) == response
    assert applied == [slot]


@pytest.mark.parametrize("slot", ["put_cluster", "create_example"])
@pytest.mark.parametrize(
    "payload", [b"\xff", pytest.param(b"\0" * ((1 << 20) + 1), id="over-one-mebibyte"), b""]
)
def test_invalid_envelopes_do_not_reach_the_effect(slot: str, payload: bytes) -> None:
    applied: list[str] = []

    def apply(name: str, request: object) -> object:
        applied.append(name)
        return _answer(name, request)

    response = _serve(slot, payload, FakeAdmissionStore(), apply)
    assert response.result is not None
    assert response.result.field == "failure"
    assert response.result.value.category == FailureCategory.INVALID_INPUT
    assert applied == []
