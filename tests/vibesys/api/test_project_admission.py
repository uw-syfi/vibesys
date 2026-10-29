"""One project request id changes the machine once and then replays."""

from vibesys.api.host_operations.api import ProjectRefusedError, serve_project
from vs_project.api.testing import FakeAdmissionStore
from vs_proto.api import MutateRequest, MutateResponse, Oneof, validate
from vs_proto.api import host_payloads as pb


def _payload() -> bytes:
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "create_example",
            pb.CreateExampleRequest(
                request_id="request-a", example="queue", destination="/work/queue"
            ),
        ),
    )
    validate(request)
    return request.to_binary()


def _created() -> pb.CreateExampleResponse:
    return pb.CreateExampleResponse(
        request_id="request-a",
        result=Oneof("created", pb.CreatedProject(project="/work/queue", tasks=["a"])),
    )


def test_creating_a_project_twice_applies_once() -> None:
    applied: list[str] = []

    def apply(slot: str, request: object) -> pb.CreateExampleResponse:
        applied.append(slot)
        assert isinstance(request, pb.CreateExampleRequest)
        return _created()

    store = FakeAdmissionStore()
    payload = _payload()
    first = MutateResponse.from_binary(
        serve_project(payload, store=store, host_user="host-a", apply=apply)
    )
    second = MutateResponse.from_binary(
        serve_project(payload, store=store, host_user="host-a", apply=apply)
    )

    assert applied == ["create_example"]
    assert first.result is not None
    assert first.result.field == "create_example"
    assert second.result == first.result


def test_a_refused_project_change_is_not_applied_again() -> None:
    applied = 0

    def apply(_slot: str, _request: object) -> pb.CreateExampleResponse:
        nonlocal applied
        applied += 1
        message = "queue is not an example"
        raise ProjectRefusedError(message)

    store = FakeAdmissionStore()
    payload = _payload()
    first = MutateResponse.from_binary(
        serve_project(payload, store=store, host_user="host-a", apply=apply)
    )
    second = MutateResponse.from_binary(
        serve_project(payload, store=store, host_user="host-a", apply=apply)
    )

    assert applied == 1
    assert first.result is not None
    assert first.result.field == "failure"
    assert first.result.value.message == "queue is not an example"
    assert second.result == first.result
