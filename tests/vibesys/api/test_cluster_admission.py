"""One cluster request id changes the machine once and then replays."""

from __future__ import annotations

from vibesys.api.host_operations.api import ClusterRefusedError, serve_cluster
from vs_project.api.testing import FakeAdmissionStore
from vs_proto.api import MutateRequest, MutateResponse, Oneof, validate
from vs_proto.api import host_payloads as pb


def _cluster() -> pb.SlurmClusterSettings:
    return pb.SlurmClusterSettings(
        name="lab",
        reach=pb.SlurmClusterSettingsReach(
            value=Oneof("ssh_reach", pb.SshReach(kind="ssh", host="host-a"))
        ),
        workspace="/work",
    )


def _settings() -> pb.ComputeSettings:
    return pb.ComputeSettings(config_path="/settings", modal=pb.ModalStatus(token_present=False))


def _payload() -> bytes:
    request = MutateRequest(
        checkout="/src/checkout",
        method=Oneof(
            "put_cluster",
            pb.PutClusterRequest(request_id="request-a", cluster=_cluster()),
        ),
    )
    validate(request)
    return request.to_binary()


def test_putting_a_cluster_twice_applies_once() -> None:
    applied: list[str] = []

    def apply(slot: str, request: object) -> pb.PutClusterResponse:
        applied.append(slot)
        assert isinstance(request, pb.PutClusterRequest)
        return pb.PutClusterResponse(request_id=request.request_id, settings=_settings())

    store = FakeAdmissionStore()
    payload = _payload()
    first = MutateResponse.from_binary(
        serve_cluster(payload, store=store, host_user="host-a", apply=apply)
    )
    second = MutateResponse.from_binary(
        serve_cluster(payload, store=store, host_user="host-a", apply=apply)
    )

    assert applied == ["put_cluster"]
    assert first.result is not None
    assert first.result.field == "put_cluster"
    assert second.result == first.result


def test_a_refused_cluster_change_is_not_applied_again() -> None:
    applied = 0

    def apply(_slot: str, _request: object) -> pb.PutClusterResponse:
        nonlocal applied
        applied += 1
        message = "lab is already set up"
        raise ClusterRefusedError(message)

    store = FakeAdmissionStore()
    payload = _payload()
    first = MutateResponse.from_binary(
        serve_cluster(payload, store=store, host_user="host-a", apply=apply)
    )
    second = MutateResponse.from_binary(
        serve_cluster(payload, store=store, host_user="host-a", apply=apply)
    )

    assert applied == 1
    assert first.result is not None
    assert first.result.field == "failure"
    assert first.result.value.message == "lab is already set up"
    assert second.result == first.result
