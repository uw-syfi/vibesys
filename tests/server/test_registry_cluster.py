"""A registry cluster change is the helper's cluster change, field for field."""

from __future__ import annotations

from server.host_compute import (
    ClusterTestFailed,
    ClusterTestStage,
    ComputeSettings,
    ModalStatus,
    SlurmClusterSettings,
)
from server.host_helper_requests import apply_registered_cluster
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


class _Host:
    """The compute surface ``apply_registered_cluster`` uses."""

    def __init__(self, compute: object) -> None:
        self.compute = compute


def test_a_registry_put_keeps_the_helper_cluster() -> None:
    seen: list[SlurmClusterSettings] = []

    class _Compute:
        def add(self, cluster: SlurmClusterSettings) -> ComputeSettings:
            seen.append(cluster)
            return ComputeSettings(
                config_path="/settings",
                clusters=(),
                modal=ModalStatus(token_present=False),
            )

    request = pb.PutClusterRequest(request_id="request-a", cluster=_cluster())
    response = apply_registered_cluster("put_cluster", request, _Host(_Compute()))

    assert len(seen) == 1
    assert seen[0].name == "lab"
    assert seen[0].reach.kind == "ssh"
    assert seen[0].reach.host == "host-a"
    assert seen[0].workspace == "/work"
    assert response.request_id == "request-a"
    assert response.settings is not None
    assert response.settings.config_path == "/settings"


def test_a_registry_test_keeps_the_helper_failure() -> None:
    class _Compute:
        def test(self, _cluster: SlurmClusterSettings) -> ClusterTestFailed:
            return ClusterTestFailed(stage=ClusterTestStage.REACH, detail="could not reach host-a")

    request = pb.TestClusterRequest(request_id="request-b", cluster=_cluster())
    response = apply_registered_cluster("test_cluster", request, _Host(_Compute()))

    assert response.result is not None
    assert response.result.field == "failed"
    assert response.result.value.stage == pb.ClusterTestStage.REACH
    assert response.result.value.detail == "could not reach host-a"
