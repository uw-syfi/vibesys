"""A registry cluster change is the helper's cluster change, field for field."""

from __future__ import annotations

import pytest

from server.host_compute import (
    ClusterHardware,
    ClusterTestFailed,
    ClusterTestPassed,
    ClusterTestResult,
    ClusterTestStage,
    ComputeSettings,
    ModalStatus,
    SlurmClusterSettings,
    apply_registered_cluster,
)
from vs_proto.api import Oneof
from vs_proto.api import host_payloads as pb


def _cluster() -> pb.SlurmClusterSettings:
    return pb.SlurmClusterSettings(
        name="lab",
        reach=pb.SlurmClusterSettingsReach(
            value=Oneof("ssh_reach", pb.SshReach(kind="ssh", host="host-a"))
        ),
        workspace="/work",
    )


def _settings() -> ComputeSettings:
    return ComputeSettings(
        config_path="/settings",
        clusters=(),
        modal=ModalStatus(token_present=False),
    )


class _Compute:
    """The compute surface ``apply_registered_cluster`` calls."""

    def __init__(self, *, result: ClusterTestResult | None = None) -> None:
        self.seen: list[SlurmClusterSettings] = []
        self.removed: list[str] = []
        self._result = result

    def add(self, cluster: SlurmClusterSettings) -> ComputeSettings:
        self.seen.append(cluster)
        return _settings()

    def remove(self, name: str) -> ComputeSettings:
        self.removed.append(name)
        return _settings()

    def test(self, cluster: SlurmClusterSettings) -> ClusterTestResult:
        if self._result is None:
            message = f"{cluster.name} was tested without a scripted failure"
            raise AssertionError(message)
        return self._result


def test_a_registry_put_keeps_the_helper_cluster() -> None:
    compute = _Compute()
    request = pb.PutClusterRequest(request_id="request-a", cluster=_cluster())
    response = apply_registered_cluster("put_cluster", request, compute)

    assert len(compute.seen) == 1
    assert compute.seen[0].name == "lab"
    assert compute.seen[0].reach.kind == "ssh"
    assert compute.seen[0].reach.host == "host-a"
    assert compute.seen[0].workspace == "/work"
    assert response.request_id == "request-a"
    assert response.settings is not None
    assert response.settings.config_path == "/settings"


def test_a_registry_test_keeps_the_helper_failure() -> None:
    failure = ClusterTestFailed(stage=ClusterTestStage.REACH, detail="could not reach host-a")
    request = pb.TestClusterRequest(request_id="request-b", cluster=_cluster())
    response = apply_registered_cluster("test_cluster", request, _Compute(result=failure))

    assert response.result is not None
    assert response.result.field == "failed"
    assert response.result.value.stage == pb.ClusterTestStage.REACH
    assert response.result.value.detail == "could not reach host-a"


def test_a_registry_remove_keeps_its_identity_and_settings() -> None:
    compute = _Compute()
    response = apply_registered_cluster(
        "remove_cluster", pb.RemoveClusterRequest(request_id="remove-a", name="lab"), compute
    )
    assert compute.removed == ["lab"]
    assert response.request_id == "remove-a"
    assert response.settings is not None
    assert response.settings.config_path == "/settings"


def test_a_registry_test_keeps_a_successful_hardware_report() -> None:
    result = ClusterTestPassed(hardware=ClusterHardware(), partitions=("cpu",))
    response = apply_registered_cluster(
        "test_cluster",
        pb.TestClusterRequest(request_id="test-a", cluster=_cluster()),
        _Compute(result=result),
    )
    assert response.result is not None
    assert response.result.field == "passed"
    assert response.result.value.hardware is not None
    assert response.result.value.hardware.gpus_per_node == 0
    assert list(response.result.value.partitions) == ["cpu"]


@pytest.mark.parametrize("slot", ["put_cluster", "test_cluster", "unknown"])
def test_registry_cluster_routes_reject_mismatched_requests(slot: str) -> None:
    with pytest.raises(ValueError, match="does not match its request"):
        apply_registered_cluster(
            slot, pb.RemoveClusterRequest(request_id="remove-a", name="lab"), _Compute()
        )


def test_registry_put_rejects_a_missing_cluster() -> None:
    with pytest.raises(ValueError, match="cluster is required"):
        apply_registered_cluster(
            "put_cluster", pb.PutClusterRequest(request_id="put-a"), _Compute()
        )
