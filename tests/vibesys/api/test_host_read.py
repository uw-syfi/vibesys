"""Registered host reads round-trip through the closed envelope."""

from __future__ import annotations

from typing import cast

from server.host_api import HelperErrorCode
from server.host_helper_requests import read_failure_category
from vibesys.api.host_operations.api import ReadSource, execute_host_read
from vs_proto.api import (
    FailureCategory,
    HostReadRequest,
    HostReadResponse,
    Oneof,
    OperationFailure,
    to_mapping,
)
from vs_proto.api import (
    host_payloads as pb,
)


class _Compute:
    def __init__(self, settings: pb.ComputeSettings) -> None:
        self._settings = settings

    def compute(self) -> pb.ComputeSettings:
        return self._settings


def _settings() -> pb.ComputeSettings:
    return pb.ComputeSettings(
        config_path="/settings",
        clusters=[
            pb.ListedCluster(
                cluster=pb.SlurmClusterSettings(
                    name="campus",
                    workspace="/work",
                    reach=pb.SlurmClusterSettingsReach(
                        value=Oneof("ssh_reach", pb.SshReach(kind="ssh", host="host-a"))
                    ),
                ),
                source=pb.ClusterSource.SLURM_TOML,
            )
        ],
        modal=pb.ModalStatus(token_present=False),
    )


def _source(settings: pb.ComputeSettings) -> ReadSource:
    return cast("ReadSource", _Compute(settings))


def _as_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        message = "projected value is not a mapping"
        raise TypeError(message)
    return value


def _failure(encoded: bytes) -> OperationFailure:
    response = HostReadResponse.from_binary(encoded)
    assert response.result is not None
    assert response.result.field == "failure"
    failure = response.result.value
    assert isinstance(failure, OperationFailure)
    return failure


def _envelope(cli_payload: pb.ListComputeRequest) -> bytes:
    return HostReadRequest(
        checkout="/checkout", method=Oneof("list_compute", cli_payload)
    ).to_binary()


def test_list_compute_keeps_the_cluster_source() -> None:
    payload = _envelope(pb.ListComputeRequest(request_id="request-a"))
    encoded = execute_host_read(payload, _source(_settings()), cli="list-compute")
    response = HostReadResponse.from_binary(encoded)
    assert response.result is not None
    assert response.result.field == "list_compute"
    settings = response.result.value.settings
    assert settings is not None
    projected = to_mapping(settings)
    clusters = projected["clusters"]
    assert isinstance(clusters, list)
    first = _as_dict(clusters[0])
    cluster = _as_dict(first["cluster"])
    assert first["source"] == "slurm_toml"
    assert cluster["reach"] == {"kind": "ssh", "host": "host-a"}
    modal = _as_dict(projected["modal"])
    assert modal["token_present"] is False


def test_a_mismatched_command_or_oversize_payload_is_invalid_input() -> None:
    payload = _envelope(pb.ListComputeRequest(request_id="request-a"))
    mismatched = _failure(execute_host_read(payload, _source(_settings()), cli="list-tasks"))
    assert mismatched.category == FailureCategory.INVALID_INPUT
    oversize = _failure(
        execute_host_read(b"\x00" * ((1 << 20) + 1), _source(_settings()), cli="list-compute")
    )
    assert oversize.category == FailureCategory.INVALID_INPUT
    missing = _failure(
        execute_host_read(
            HostReadRequest(checkout="/checkout").to_binary(),
            _source(_settings()),
            cli="list-compute",
        )
    )
    assert missing.category == FailureCategory.INVALID_INPUT


def test_every_helper_refusal_maps_once_and_read_only_is_permission_denied() -> None:
    categories = {code: read_failure_category(code) for code in HelperErrorCode}
    assert set(categories) == set(HelperErrorCode)
    assert categories[HelperErrorCode.CLUSTER_READ_ONLY] is FailureCategory.PERMISSION_DENIED
    assert categories[HelperErrorCode.DEADLINE_EXCEEDED] is FailureCategory.DEADLINE_EXCEEDED
    assert categories[HelperErrorCode.INVALID_REQUEST] is FailureCategory.INVALID_INPUT
