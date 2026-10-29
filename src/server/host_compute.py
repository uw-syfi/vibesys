"""A machine's compute settings and where a run's results are measured, as helper messages.

``MeasurementTarget`` is what New run sets on ``run.start`` and ``run.preflight`` (feature
``measurement_target``): measure on the same machine, on a named Slurm cluster, or on Modal.
The compute messages (feature ``machine_compute``) are how a client reads and changes what
that machine can offer: ``compute.list`` the Slurm clusters set up on it with the file they
live in and whether a Modal token is present (never the secret), ``compute.test_cluster``
whether a cluster can be reached and what hardware it offers, ``compute.add_cluster`` and
``compute.remove_cluster``. The settings are the machine's, not the client's: the helper
answers for the machine it runs on.

Light: Pydantic, the standard library and ``server.host_envelope`` and
``server.host_machine_facts``. ``server.host_api`` imports the messages into the helper's
request and reply unions. Registry mutations use the generated protobuf boundary on demand.

Public surface: ``ComputeError``, ``ClusterName``, ``SameMachine``, ``SlurmTarget``, ``ModalTarget``,
``MeasurementTarget``, ``SshReach``, ``LoginNodeReach``, ``ClusterReach``, ``ClusterHardware``,
``SlurmClusterSettings``, ``ClusterSource``, ``ListedCluster``, ``ModalTokenSource``,
``ModalStatus``, ``ComputeSettings``, ``ClusterTestStage``, ``ClusterTestPassed``,
``ClusterTestFailed``, ``ClusterTestResult``, ``ClusterCompute``,
``apply_registered_cluster`` and the four requests and replies.
"""

from __future__ import annotations

from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, overload

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    model_validator,
)

from server.host_envelope import HelperErrorCode, HelperReplyBase, HelperRequestBase
from server.host_machine_facts import Accelerator

if TYPE_CHECKING:
    from types import ModuleType

    from vs_proto.api import host_payloads as pb


ClusterName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")]
"""A Slurm cluster's name on its machine: a safe name of at most 63 characters, as ``vs_slurm``'s."""

_Text = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^\n\r]*$")]
_Line = Annotated[str, StringConstraints(min_length=1, max_length=300, pattern=r"^[^\n\r]*$")]


class ComputeError(Exception):
    """A compute request the machine's settings cannot honor; ``code`` says why."""

    def __init__(self, code: HelperErrorCode, message: str, path: str | None = None) -> None:
        """Name the failure ``code``; ``message`` is for a person and ``path`` the file it is about."""
        super().__init__(message)
        self.code = code
        self.path = path


class SameMachine(BaseModel):
    """Measure on the machine the agent works on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["same_machine"] = "same_machine"


class SlurmTarget(BaseModel):
    """Measure on the Slurm cluster ``compute.list`` names ``cluster``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["slurm"] = "slurm"
    cluster: ClusterName


class ModalTarget(BaseModel):
    """Measure on Modal, with the token present on the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["modal"] = "modal"


MeasurementTarget = Annotated[SameMachine | SlurmTarget | ModalTarget, Field(discriminator="kind")]
"""Where a run's results are measured; ``kind`` discriminates."""


class SshReach(BaseModel):
    """The cluster is reached from the machine over SSH, by a host alias in its ``~/.ssh/config``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["ssh"] = "ssh"
    host: Annotated[
        str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$", max_length=255)
    ]


class LoginNodeReach(BaseModel):
    """The machine is a login node of the cluster: its own scheduler commands reach it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["login_node"] = "login_node"


ClusterReach = Annotated[SshReach | LoginNodeReach, Field(discriminator="kind")]
"""How a machine reaches a cluster; ``kind`` discriminates."""


class ClusterHardware(BaseModel):
    """What a cluster's nodes offer, as ``sinfo`` lists it or as its operator states it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accelerator: Accelerator | None = None
    """How its GPUs run work; ``None`` when it has GPUs whose vendor is not known."""
    gpu_models: tuple[_Text, ...] = ()
    gpus_per_node: NonNegativeInt = 0

    @model_validator(mode="after")
    def _cpu_has_no_gpus(self) -> ClusterHardware:
        if self.accelerator is Accelerator.CPU and (self.gpu_models or self.gpus_per_node):
            message = "accelerator cpu offers no GPU: gpu_models and gpus_per_node must be empty"
            raise ValueError(message)
        return self


class SlurmClusterSettings(BaseModel):
    """One Slurm cluster as its machine reaches and uses it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ClusterName
    reach: ClusterReach
    workspace: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    """The directory on the cluster where runs stage their work: an absolute normalized path."""
    partition: _Text | None = None
    nodes: PositiveInt | None = None
    time_limit: Annotated[str, StringConstraints(pattern=r"^[0-9][0-9:-]{0,31}$")] | None = None
    """An ``sbatch --time`` value (minutes, ``H:MM:SS`` or ``D-HH``)."""
    sbatch_options: tuple[_Text, ...] = Field(default=(), max_length=64)
    """Further ``sbatch`` options, one argument per item, as written; never parsed by a shell."""
    hardware: ClusterHardware | None = None
    """What its nodes offer; ``None`` when not stated and not read from ``sinfo`` yet."""


class ClusterSource(StrEnum):
    """Which file of the machine a listed cluster lives in."""

    VIBESYS = "vibesys"
    """The machine's compute settings file, which this helper edits."""
    SLURM_TOML = "slurm_toml"
    """The Slurm operator file a terminal run reads, which this helper only reads."""


class ListedCluster(BaseModel):
    """A cluster set up on the machine, and the file it comes from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster: SlurmClusterSettings
    source: ClusterSource


class ModalTokenSource(StrEnum):
    """Where the machine holds its Modal token."""

    ENVIRONMENT = "environment"
    """The token id and secret environment variables are both set for runs."""
    PROFILE = "profile"
    """A profile of the Modal config file holds both."""


class ModalStatus(BaseModel):
    """Whether Modal is set up on the machine; the token itself is never sent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    token_present: bool
    source: ModalTokenSource | None = None
    """Where the token is held; ``None`` exactly when ``token_present`` is false."""

    @model_validator(mode="after")
    def _source_iff_present(self) -> ModalStatus:
        if self.token_present != (self.source is not None):
            message = "source is named exactly when a token is present"
            raise ValueError(message)
        return self


class ComputeSettings(BaseModel):
    """What the machine offers as places to measure: its clusters and its Modal token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_path: str
    """The file on the machine that holds the settings this helper edits."""
    clusters: tuple[ListedCluster, ...]
    modal: ModalStatus


class ClusterTestStage(StrEnum):
    """Which step of a connection test failed."""

    REACH = "reach"
    """The machine could not run a command on the cluster."""
    SCHEDULER = "scheduler"
    """The scheduler did not answer ``sinfo``, or knows no such partition."""
    WORKSPACE = "workspace"
    """The workspace directory is missing or not writable on the cluster."""


class ClusterTestPassed(BaseModel):
    """The cluster answered: what its scheduler lists, and the hardware read from it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["passed"] = "passed"
    hardware: ClusterHardware
    partitions: tuple[_Text, ...]


class ClusterTestFailed(BaseModel):
    """The cluster did not pass; ``detail`` is one line saying how."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["failed"] = "failed"
    stage: ClusterTestStage
    detail: _Line


ClusterTestResult = Annotated[ClusterTestPassed | ClusterTestFailed, Field(discriminator="outcome")]
"""What testing a cluster found; ``outcome`` discriminates."""


class ComputeListRequest(HelperRequestBase):
    """The machine's compute settings."""

    type: Literal["compute.list"] = "compute.list"


class ComputeTestClusterRequest(HelperRequestBase):
    """Test whether ``cluster`` can be reached from the machine, and read its hardware."""

    type: Literal["compute.test_cluster"] = "compute.test_cluster"
    cluster: SlurmClusterSettings


class ComputeAddClusterRequest(HelperRequestBase):
    """Save ``cluster``, once it passes the test again; a name already set up is refused."""

    type: Literal["compute.add_cluster"] = "compute.add_cluster"
    cluster: SlurmClusterSettings


class ComputeRemoveClusterRequest(HelperRequestBase):
    """Remove the cluster ``name`` from the settings file this helper edits."""

    type: Literal["compute.remove_cluster"] = "compute.remove_cluster"
    name: ClusterName


class ComputeListReply(HelperReplyBase):
    """The machine's compute settings."""

    type: Literal["compute.list"] = "compute.list"
    settings: ComputeSettings


class ComputeTestClusterReply(HelperReplyBase):
    """What testing the cluster found."""

    type: Literal["compute.test_cluster"] = "compute.test_cluster"
    result: ClusterTestResult


class ComputeAddClusterReply(HelperReplyBase):
    """The settings after the cluster was saved."""

    type: Literal["compute.add_cluster"] = "compute.add_cluster"
    settings: ComputeSettings


class ComputeRemoveClusterReply(HelperReplyBase):
    """The settings after the cluster was removed."""

    type: Literal["compute.remove_cluster"] = "compute.remove_cluster"
    settings: ComputeSettings


__all__ = [
    "ClusterHardware",
    "ClusterName",
    "ClusterReach",
    "ClusterSource",
    "ClusterTestFailed",
    "ClusterTestPassed",
    "ClusterTestResult",
    "ClusterTestStage",
    "ComputeAddClusterReply",
    "ComputeAddClusterRequest",
    "ComputeListReply",
    "ComputeListRequest",
    "ComputeRemoveClusterReply",
    "ComputeRemoveClusterRequest",
    "ComputeSettings",
    "ComputeTestClusterReply",
    "ComputeTestClusterRequest",
    "ListedCluster",
    "LoginNodeReach",
    "MeasurementTarget",
    "ModalStatus",
    "ModalTarget",
    "ModalTokenSource",
    "SameMachine",
    "SlurmClusterSettings",
    "SlurmTarget",
    "SshReach",
]


def _wire() -> ModuleType:
    """Load the protobuf boundary only for a registry operation, never hello."""
    return import_module("vs_proto.api")


class ClusterCompute(Protocol):
    """The cluster changes one registered mutation asks of a host."""

    def add(self, cluster: SlurmClusterSettings) -> ComputeSettings:
        """Save ``cluster`` and return the machine's settings."""
        ...

    def remove(self, name: str) -> ComputeSettings:
        """Forget the cluster ``name`` and return the machine's settings."""
        ...

    def test(self, cluster: SlurmClusterSettings) -> ClusterTestResult:
        """How far ``cluster`` can be used from this machine."""
        ...


@overload
def apply_registered_cluster(
    slot: Literal["remove_cluster"],
    request: pb.RemoveClusterRequest,
    compute: ClusterCompute,
) -> pb.RemoveClusterResponse: ...


@overload
def apply_registered_cluster(
    slot: Literal["put_cluster"],
    request: pb.PutClusterRequest,
    compute: ClusterCompute,
) -> pb.PutClusterResponse: ...


@overload
def apply_registered_cluster(
    slot: Literal["test_cluster"],
    request: pb.TestClusterRequest,
    compute: ClusterCompute,
) -> pb.TestClusterResponse: ...


@overload
def apply_registered_cluster(
    slot: str,
    request: pb.PutClusterRequest | pb.RemoveClusterRequest | pb.TestClusterRequest,
    compute: ClusterCompute,
) -> pb.PutClusterResponse | pb.RemoveClusterResponse | pb.TestClusterResponse: ...


def apply_registered_cluster(
    slot: str,
    request: pb.PutClusterRequest | pb.RemoveClusterRequest | pb.TestClusterRequest,
    compute: ClusterCompute,
) -> pb.PutClusterResponse | pb.RemoveClusterResponse | pb.TestClusterResponse:
    """Change or test one cluster and return that slot's registry response.

    ``ComputeError`` and ``ValueError`` stay with the caller, which stores a refusal.
    """
    wire = _wire()
    payloads = wire.host_payloads
    if slot == "remove_cluster" and isinstance(request, payloads.RemoveClusterRequest):
        settings = compute.remove(request.name)
        return payloads.RemoveClusterResponse(
            request_id=request.request_id, settings=_registry_settings(settings)
        )
    if slot == "put_cluster" and isinstance(request, payloads.PutClusterRequest):
        saved = compute.add(_registry_cluster(request.cluster))
        return payloads.PutClusterResponse(
            request_id=request.request_id, settings=_registry_settings(saved)
        )
    if slot == "test_cluster" and isinstance(request, payloads.TestClusterRequest):
        found = compute.test(_registry_cluster(request.cluster))
        return payloads.TestClusterResponse(
            request_id=request.request_id, result=_registry_test(found)
        )
    message = f"cluster slot {slot} does not match its request"
    raise ValueError(message)


def _registry_cluster(cluster: pb.SlurmClusterSettings | None) -> SlurmClusterSettings:
    if cluster is None:
        message = "cluster is required"
        raise ValueError(message)
    return SlurmClusterSettings.model_validate(_wire().to_mapping(cluster))


def _registry_settings(settings: ComputeSettings) -> pb.ComputeSettings:
    wire = _wire()
    return wire.from_mapping(wire.host_payloads.ComputeSettings, settings.model_dump(mode="json"))


def _registry_test(result: ClusterTestResult) -> object:
    wire = _wire()
    payloads = wire.host_payloads
    if isinstance(result, ClusterTestPassed):
        hardware = wire.from_mapping(
            payloads.ClusterHardware, result.hardware.model_dump(mode="json")
        )
        passed = payloads.ClusterTestPassed(hardware=hardware, partitions=list(result.partitions))
        return wire.Oneof("passed", passed)
    stages = {
        ClusterTestStage.REACH: payloads.ClusterTestStage.REACH,
        ClusterTestStage.SCHEDULER: payloads.ClusterTestStage.SCHEDULER,
        ClusterTestStage.WORKSPACE: payloads.ClusterTestStage.WORKSPACE,
    }
    failed = payloads.ClusterTestFailed(stage=stages[result.stage], detail=result.detail)
    return wire.Oneof("failed", failed)
