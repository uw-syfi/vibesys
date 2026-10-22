"""A registry cluster change is the helper's cluster change, field for field."""

from __future__ import annotations

from server.host_compute import (
    ClusterTestFailed,
    ClusterTestResult,
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


def _settings() -> ComputeSettings:
    return ComputeSettings(
        config_path="/settings",
        clusters=(),
        modal=ModalStatus(token_present=False),
    )


class _Compute:
    """The compute surface ``apply_registered_cluster`` calls."""

    def __init__(self, *, failure: ClusterTestFailed | None = None) -> None:
        self.seen: list[SlurmClusterSettings] = []
        self.removed: list[str] = []
        self._failure = failure

    def add(self, cluster: SlurmClusterSettings) -> ComputeSettings:
        self.seen.append(cluster)
        return _settings()

    def remove(self, name: str) -> ComputeSettings:
        self.removed.append(name)
        return _settings()

    def test(self, cluster: SlurmClusterSettings) -> ClusterTestResult:
        if self._failure is None:
            message = f"{cluster.name} was tested without a scripted failure"
            raise AssertionError(message)
        return self._failure


class _Host:
    """A host whose compute surface is the test's."""

    def __init__(self, compute: _Compute) -> None:
        self.compute = compute


def test_a_registry_put_keeps_the_helper_cluster() -> None:
    compute = _Compute()
    request = pb.PutClusterRequest(request_id="request-a", cluster=_cluster())
    response = apply_registered_cluster("put_cluster", request, _Host(compute))

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
    response = apply_registered_cluster("test_cluster", request, _Host(_Compute(failure=failure)))

    assert response.result is not None
    assert response.result.field == "failed"
    assert response.result.value.stage == pb.ClusterTestStage.REACH
    assert response.result.value.detail == "could not reach host-a"
