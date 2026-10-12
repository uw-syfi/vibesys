"""Light contracts a host reports about its live detached servers.

These are the documents ``vibesys instances``, ``vibesys tasks`` and
``vibesys --detach`` print, the record a detached server publishes in the live
registry (``server.registry``), and every message of the host helper
(``server.host_helper``), the one program the desktop app runs on a host. They import only Pydantic and the standard library, so
a host command that reads or writes them starts in a fraction of the time the
run engine takes to import. ``tests/architecture/test_light_entrypoints.py``
keeps it that way.

Durable documents carry their own ``version``. A reader parses every version
it knows strictly and upgrades it (``parse_record``); a record of a version it
does not know, or one it cannot parse, is ``UnreadableRecord``, never dropped
and never a failure of the whole listing: two checkouts on one host share one
registry.

Public surface: ``PROTOCOL_VERSION``, ``RECORD_VERSION``,
``INSTANCE_ID_PATTERN``, ``InstanceId``, ``RunId``, ``RunStatus``,
``StartupStage``, ``StartupProgress``, ``StartupFact``, ``FailureReason``,
``RunPhase``, ``InstanceStatus``,
``LiveInstanceRecord``, ``UnreadableRecord``, ``parse_record``,
``InstanceList``, ``StopOutcome``, ``StopRoute``, ``InstanceStopResult``,
``DetachedLaunchErrorCode``, ``DetachedLaunchFailure``, ``TaskSummary``,
``TaskList``, the inspection documents, and the host helper's messages:
``HELPER_PROTOCOL_VERSIONS``,
``HelperFeature``, ``HelperRequest`` (``parse_helper_request``) and its
members, ``HelperMessage`` and its members, ``HelperErrorCode``, and a relayed
channel's end: ``ChannelEndOutcome``, ``ChannelEnd``.

The helper's messages follow ``docs/contributing/desktop-connection.md``
("Handshake and versioning"): strict within a negotiated version, additive
changes only behind a ``HelperFeature`` that ``hello`` returned.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

PROTOCOL_VERSION = 1
"""The run protocol version a server speaks on its control socket.

Defined here, not in ``server.api.protocol``, so a record can name it without
importing the run protocol; the protocol module re-exports it.
"""

INSTANCE_ID_PATTERN = r"^[0-9a-f]{12}$"
InstanceId = Annotated[str, StringConstraints(pattern=INSTANCE_ID_PATTERN)]
"""A registry key: 12 lowercase hex digits, so it is never a path fragment."""

RunId = Annotated[str, StringConstraints(min_length=1)]
"""The durable run a server drives; ``vs_project`` owns its full syntax."""

RECORD_VERSION = 4
"""The registry record version this checkout writes; it also reads version 3."""


class RunStatus(StrEnum):
    """Lifecycle status of one run, as frontends observe it.

    This is the authoritative closed set for the ``status`` field of
    ``RunSnapshot`` and of ``RunStatusChangedData``; the generated TypeScript
    protocol types derive their union from it.

    ``PAUSING`` and ``PAUSED`` are distinct because a pause is only applied at
    an invocation boundary: ``/pause`` records the request, and the run keeps
    executing the call already in flight until it reaches that boundary.
    ``STOPPING`` and ``STOPPED`` split the same way for ``/stop``, whose
    boundary is where the run ends instead of where it parks.
    """

    STARTING = "starting"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def has_ended(self) -> bool:
        """Whether the run has settled into a status it never leaves."""
        match self:
            case RunStatus.COMPLETED | RunStatus.FAILED | RunStatus.STOPPED:
                return True
            case (
                RunStatus.STARTING
                | RunStatus.RUNNING
                | RunStatus.PAUSING
                | RunStatus.PAUSED
                | RunStatus.STOPPING
            ):
                return False


class StartupStage(StrEnum):
    """Where a starting run is, in the order it gets there.

    Declaration order is the order: a starting run only moves to a later stage.
    """

    RESOLVING = "resolving"
    """The server validates the request, its configuration, and the project."""
    BUILDING_IMAGE = "building_image"
    """The agent image is being built (the first build on a host takes minutes)."""
    STARTING_CONTAINER = "starting_container"
    """The image is built; the agent's container is being started."""

    @property
    def rank(self) -> int:
        """Position in the startup order."""
        return list(StartupStage).index(self)


class StartupProgress(BaseModel):
    """How far a startup stage has come: ``done`` of ``total`` steps, now on ``label``.

    ``total`` is ``None`` when the stage does not know it. ``label`` names the
    step now running (an image build's Dockerfile instruction), as data.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    done: NonNegativeInt
    total: int | None = Field(default=None, ge=1)
    label: str = ""


class FailureReason(BaseModel):
    """Why a run failed: the diagnostic it ended with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    stage: str
    message: str


class StartupFact(BaseModel):
    """A starting run reached ``stage``, or progressed within it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: StartupStage
    progress: StartupProgress | None = None


class RunPhase(BaseModel):
    """A run's lifecycle status with the detail its status needs.

    ``stage`` (and, when the stage reports it, ``progress``) says where a
    ``starting`` run is; ``reason`` says why a ``failed`` one ended. None of
    them is present for any other status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RunStatus
    stage: StartupStage | None = None
    progress: StartupProgress | None = None
    reason: FailureReason | None = None

    @model_validator(mode="after")
    def _detail_matches_status(self) -> RunPhase:
        if (self.stage is not None) != (self.status is RunStatus.STARTING):
            message = "stage is present exactly when status is starting"
            raise ValueError(message)
        if self.progress is not None and self.stage is None:
            message = "progress is present only with a startup stage"
            raise ValueError(message)
        if (self.reason is not None) != (self.status is RunStatus.FAILED):
            message = "reason is present exactly when status is failed"
            raise ValueError(message)
        return self

    def advance(
        self,
        *,
        status: RunStatus | None = None,
        failure: FailureReason | None = None,
        startup: StartupFact | None = None,
    ) -> RunPhase:
        """Return the phase after one observed fact: a status change, a failure, or a startup step.

        The fold is monotone: an ended phase absorbs every fact, and ``failed``
        is entered only with its reason, from the failure's diagnostic, so a
        status change to ``failed`` waits for the event that says why. A
        failure while the run is past ``resolving`` names the startup stage it
        failed in. ``starting`` only moves to a later stage (a startup fact of
        an earlier stage, or after ``starting``, changes nothing), and keeps
        its stage until the run leaves it. The statuses themselves move by
        ``server.run_lifecycle``'s rules; this projects them.
        """
        if self.status.has_ended:
            return self
        if failure is not None:
            return RunPhase(status=RunStatus.FAILED, reason=self._failed_in(failure))
        if startup is not None:
            return self._started(startup)
        if status is None or status in {RunStatus.STARTING, RunStatus.FAILED, self.status}:
            return self
        return RunPhase(status=status)

    def _started(self, startup: StartupFact) -> RunPhase:
        if self.stage is None or startup.stage.rank < self.stage.rank:
            return self
        return RunPhase(status=RunStatus.STARTING, stage=startup.stage, progress=startup.progress)

    def _failed_in(self, failure: FailureReason) -> FailureReason:
        if self.stage is None or self.stage is StartupStage.RESOLVING:
            return failure
        return failure.model_copy(update={"stage": self.stage.value})


INSPECTION_LOG_TAIL_BYTES = 64 * 1024
"""Maximum UTF-8 bytes returned by one explicit instance-log inspection."""
ECMASCRIPT_DATE_MAX_SECONDS = 8_640_000_000_000
"""Largest Unix timestamp that ECMAScript ``Date`` can represent, in seconds."""
_MAX_PATH_CHARACTERS = 4096
_MAX_IDENTITY_CHARACTERS = 255
_StartedAt = Annotated[float, Field(ge=0, le=ECMASCRIPT_DATE_MAX_SECONDS, allow_inf_nan=False)]


class InstanceStatus(StrEnum):
    """How far a live server has come; run status itself is on its socket."""

    STARTING = "starting"
    """The server holds its lock; its control socket is not accepting yet."""
    SERVING = "serving"
    """The control socket accepts connections."""


class LiveInstanceRecord(BaseModel):
    """What a detached server publishes about itself (version 4).

    ``run_id`` is allocated by the launcher before the server starts, so the
    record names its run from the first time it is published. ``phase`` is the
    run's lifecycle as the server folds it from its journal. When the server
    exits it leaves its last record, with an ended phase and ``ended_at``, as a
    tombstone the registry keeps for a day.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[4] = RECORD_VERSION
    id: InstanceId
    status: InstanceStatus
    phase: RunPhase
    socket_path: str
    log_path: str
    project_root: str
    run_id: RunId
    pid: PositiveInt
    started_at: _StartedAt
    ended_at: float | None = None
    hostname: str
    protocol_version: Literal[1] = PROTOCOL_VERSION
    vibesys_version: str
    vibesys_root: str | None = None
    """Absolute path of the VibeSys source checkout this server runs from.

    ``None`` when the server runs from an installed distribution rather than a
    checkout. A client uses it to suggest the checkout it should run
    ``vibesys`` from on this host.
    """

    @model_validator(mode="after")
    def _ended_matches_phase(self) -> LiveInstanceRecord:
        if self.ended_at is not None and not self.phase.status.has_ended:
            message = "ended_at is set only on a record whose phase has ended"
            raise ValueError(message)
        return self


class UnreadableRecord(BaseModel):
    """A registry entry this reader cannot interpret; ``version`` is ``None`` if it has none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: InstanceId
    version: int | None


_UPGRADED_RECORD_VERSION = 3
"""The earlier record version this reader still reads, through ``_upgrade_v3``."""


def _upgrade_v3(record: LiveInstanceRecord) -> LiveInstanceRecord | None:
    """A version 3 record read as version 4, or ``None`` if it uses what version 3 lacked.

    Version 4 only added startup stages past ``resolving`` and their progress
    to the phase, so every version 3 phase is a version 4 phase.
    """
    stage = record.phase.stage
    if record.phase.progress is not None or stage not in {None, StartupStage.RESOLVING}:
        return None
    return record


def parse_record(instance_id: str, raw: str) -> LiveInstanceRecord | UnreadableRecord:
    """Parse one stored record, strictly, if it is of a version this reader knows.

    A version 3 record is upgraded (``_upgrade_v3``). Records of earlier
    versions predate phases and tombstones and are not upgraded: their servers
    report no phase this reader could stand behind.
    """
    try:
        document = json.loads(raw)
    except ValueError:
        return UnreadableRecord(id=instance_id, version=None)
    version = document.get("version") if isinstance(document, dict) else None
    if not isinstance(version, int) or isinstance(version, bool):
        return UnreadableRecord(id=instance_id, version=None)
    record: LiveInstanceRecord | None = None
    if version in {_UPGRADED_RECORD_VERSION, RECORD_VERSION}:
        try:
            record = LiveInstanceRecord.model_validate({**document, "version": RECORD_VERSION})
        except ValidationError:
            record = None
    if record is not None and version == _UPGRADED_RECORD_VERSION:
        record = _upgrade_v3(record)
    if record is None or record.id != instance_id:
        return UnreadableRecord(id=instance_id, version=version)
    return record


class InstanceList(BaseModel):
    """``vibesys instances list --json``: the detached servers on this node (version 4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[4] = 4
    instances: tuple[LiveInstanceRecord, ...]
    """Servers alive now."""
    ended: tuple[LiveInstanceRecord, ...] = ()
    """Servers that exited in the last day, with the phase they ended in."""
    unreadable: tuple[UnreadableRecord, ...] = ()
    """Live servers whose record this reader cannot interpret (another version, or corrupt)."""
    unverified: tuple[InstanceId, ...] = ()
    """Records whose lock this host could not read; neither live nor removed."""


class StopOutcome(StrEnum):
    """What ``vibesys instances stop`` observed."""

    STOPPED = "stopped"
    """The server was live, was asked or signalled to stop, and released its lock."""
    STOPPING = "stopping"
    """The server accepted a stop request but its run has not reached a safe boundary yet.

    Distinct from ``STILL_RUNNING``: this is the normal path for a run whose
    active agent call outlasts the wait, and the server exits on its own once
    the call ends. Poll ``instances list`` rather than stopping again.
    """
    NOT_RUNNING = "not_running"
    """No live server holds this id; nothing was signalled."""
    STILL_RUNNING = "still_running"
    """The server was signalled but still held its lock when the wait ended."""
    UNSUPPORTED = "unsupported"
    """The server did not answer its control socket, and this host cannot signal
    a process without risking a reused pid."""


class StopRoute(StrEnum):
    """How ``vibesys instances stop`` reached the server."""

    CONTROL_SOCKET = "control_socket"
    """A ``command.stop`` request, acknowledged on the server's control socket."""
    SIGNAL = "signal"
    """SIGTERM through a stable process reference, after the socket did not answer."""


class InstanceStopResult(BaseModel):
    """``vibesys instances stop --json``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    id: InstanceId
    outcome: StopOutcome
    route: StopRoute | None = None
    """How the server was reached; ``None`` when nothing was sent."""


class AvailableLogTail(BaseModel):
    """A bounded suffix of one instance's regular ``server.log`` file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["available"] = "available"
    text: str = Field(json_schema_extra={"x-vibesys-max-utf8-bytes": INSPECTION_LOG_TAIL_BYTES})
    truncated: bool

    @field_validator("text")
    @classmethod
    def _bounded_text(cls, value: str) -> str:
        if len(value.encode("utf-8")) > INSPECTION_LOG_TAIL_BYTES:
            message = f"log tail exceeds {INSPECTION_LOG_TAIL_BYTES} UTF-8 bytes"
            raise ValueError(message)
        return value


class MissingLogTail(BaseModel):
    """The instance has no retained ``server.log`` file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["missing"] = "missing"


InstanceLogTail = Annotated[AvailableLogTail | MissingLogTail, Field(discriminator="state")]


class InspectionState(StrEnum):
    """What one coherent registry observation proved about an instance."""

    LIVE = "live"
    ENDED = "ended"
    UNVERIFIED = "unverified"


BoundedPath = Annotated[str, StringConstraints(min_length=1, max_length=_MAX_PATH_CHARACTERS)]
BoundedIdentity = Annotated[
    str, StringConstraints(min_length=1, max_length=_MAX_IDENTITY_CHARACTERS)
]
ProjectIdentity = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")]


class InstanceInspectionMetadata(BaseModel):
    """Immutable detached-process facts retained after its live record disappears."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    id: InstanceId
    project_root: BoundedPath
    run_id: ProjectIdentity
    task: ProjectIdentity | None = None
    pid: PositiveInt
    started_at: _StartedAt
    hostname: BoundedIdentity
    protocol_version: Literal[1] = PROTOCOL_VERSION
    vibesys_version: BoundedIdentity
    vibesys_root: BoundedPath | None = None
    control_socket_path: BoundedPath
    server_log_path: BoundedPath

    @field_validator("project_root", "vibesys_root", "control_socket_path", "server_log_path")
    @classmethod
    def _absolute_path(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            message = f"inspection path must be absolute: {value!r}"
            raise ValueError(message)
        return value


class InstanceInspection(BaseModel):
    """``vibesys instances inspect --json``: identity plus an optional diagnostic tail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    id: InstanceId
    state: InspectionState
    record: LiveInstanceRecord | None
    metadata: InstanceInspectionMetadata | None
    control_socket_path: BoundedPath
    server_log_path: BoundedPath
    log: InstanceLogTail | None = None

    @field_validator("control_socket_path", "server_log_path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute():
            message = f"inspection path must be absolute: {value!r}"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _coherent_identity(self) -> InstanceInspection:
        if self.record is not None and self.record.id != self.id:
            message = "inspection record id does not match inspection id"
            raise ValueError(message)
        if self.metadata is not None and self.metadata.id != self.id:
            message = "inspection metadata id does not match inspection id"
            raise ValueError(message)
        if self.metadata is not None and (
            self.metadata.control_socket_path != self.control_socket_path
            or self.metadata.server_log_path != self.server_log_path
        ):
            message = "inspection paths do not match retained metadata"
            raise ValueError(message)
        if self.state is InspectionState.LIVE and self.record is None:
            message = "a live inspection requires a live record"
            raise ValueError(message)
        return self


class DetachedLaunchErrorCode(StrEnum):
    """Failure codes ``vibesys --detach`` owns; configuration codes pass through as is."""

    REGISTRY_UNAVAILABLE = "registry_unavailable"
    """The per-user runtime root is missing, foreign, or open to other users."""
    RUN_ALREADY_LIVE = "run_already_live"
    """``--resume`` named a run a live detached server on this node is driving."""
    SERVER_START_FAILED = "server_start_failed"
    """The detached server exited, or did not serve, before it was ready."""


class DetachedLaunchFailure(BaseModel):
    """What ``vibesys --detach`` prints on stdout, as one line, when it starts nothing.

    On success the line is a ``LiveInstanceRecord`` instead; a reader tells them
    apart by ``outcome``, which a record never has. ``code`` is a
    ``DetachedLaunchErrorCode`` or the code of the run's configuration
    diagnostic (``invalid_arguments``, ``resume_not_found``, ...), and
    ``exit_code`` is the process's exit status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    outcome: Literal["failed"] = "failed"
    code: str
    stage: str
    message: str
    exit_code: PositiveInt
    log_path: str | None = None
    """The detached server's log, when one was started."""
    live_instance: LiveInstanceRecord | None = None
    """The server already driving the run, for ``run_already_live``: attach to it."""


class TaskSummary(BaseModel):
    """One task of a project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """The task's directory name, the value ``--task`` accepts."""


class TaskList(BaseModel):
    """``vibesys tasks --json``: the tasks of one project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    project_root: str
    """The resolved absolute project directory the tasks were read from."""
    tasks: tuple[TaskSummary, ...]


# --- the host helper ---------------------------------------------------------

HELPER_PROTOCOL_VERSIONS = (1, 1)
"""The lowest and highest helper protocol versions this helper speaks.

Independent of the run protocol's ``PROTOCOL_VERSION``, which ``run.connect``
reports per run.
"""


class HelperFeature(StrEnum):
    """Optional capabilities negotiated by ``hello``; a closed set per protocol version.

    A client calls a gated method only after ``hello`` returned its feature, so
    a strict peer never sees a method it does not know.
    """

    INSTANCES_WATCH = "instances_watch"
    """``instances.watch`` and ``instances.unwatch``: registry changes pushed as they happen."""
    PATHS_COMPLETE = "paths_complete"
    """``paths.complete``: directory completion for a typed path on the host."""
    INSTANCES_INSPECT = "instances_inspect"
    """``instances.inspect``: retained metadata and an optional bounded log tail."""


RequestId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
"""Chosen by the client, unique while the channel owns its request or watch worker."""

MAX_DEADLINE_MS = 3_600_000


class _HelperRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId
    deadline_ms: Annotated[int, Field(gt=0, le=MAX_DEADLINE_MS)]
    """How long the client waits for the reply; the helper answers ``deadline_exceeded`` after it."""


class HelloRequest(_HelperRequest):
    """The first message on every channel: which versions and features the client speaks.

    Its shape never changes across protocol versions, so any helper can parse
    any client's ``hello`` and answer ``incompatible`` with a reason.
    """

    type: Literal["hello"] = "hello"
    client_versions: tuple[PositiveInt, PositiveInt]
    """The lowest and highest helper protocol versions the client speaks."""
    features: tuple[str, ...] = ()
    """Features the client would use; ones this helper does not know are dropped."""
    app_version: str


class InstancesListRequest(_HelperRequest):
    """The live and recently ended detached servers on this host."""

    type: Literal["instances.list"] = "instances.list"


class InstancesInspectRequest(_HelperRequest):
    """Inspect one detached server without starting another host process."""

    type: Literal["instances.inspect"] = "instances.inspect"
    instance_id: InstanceId
    include_log: bool = False


class InstancesWatchRequest(_HelperRequest):
    """Push the listing now and again whenever it changes, until ``instances.unwatch``."""

    type: Literal["instances.watch"] = "instances.watch"


class InstancesUnwatchRequest(_HelperRequest):
    """Stop the watch that ``watch_id`` (its request's id) started."""

    type: Literal["instances.unwatch"] = "instances.unwatch"
    watch_id: RequestId


class TasksListRequest(_HelperRequest):
    """The tasks a project defines under ``.vibesys/tasks/``."""

    type: Literal["tasks.list"] = "tasks.list"
    project: str
    """The project directory on the host; ``~`` expands to the helper's home."""


class RunsListRequest(_HelperRequest):
    """A project's runs, with the phase of any server on this host that drives one."""

    type: Literal["runs.list"] = "runs.list"
    project: str


class ResumeRun(BaseModel):
    """Resume this run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId


class ResumeLatest(BaseModel):
    """Resume the project's current or latest run; the launch resolves it before detaching."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latest: Literal[True]


class RunStartRequest(_HelperRequest):
    """Start a detached run (``vibesys --detach``) in ``project``."""

    type: Literal["run.start"] = "run.start"
    project: str
    task: str | None = None
    args: tuple[str, ...] = ()
    """Further run options, one argument per item; never parsed by a shell."""
    resume: ResumeRun | ResumeLatest | None = None


class RunStopRequest(_HelperRequest):
    """Stop a live detached server (``vibesys instances stop``)."""

    type: Literal["run.stop"] = "run.stop"
    instance_id: InstanceId
    force: bool = False


class RunRole(StrEnum):
    """Which run-protocol connection a run channel carries (``WP-ROLES``)."""

    CONTROL = "control"
    SUBSCRIBE = "subscribe"
    CHAT = "chat"


class RunConnectRequest(_HelperRequest):
    """Make this channel a byte relay to a live run's control socket."""

    type: Literal["run.connect"] = "run.connect"
    instance_id: InstanceId
    role: RunRole


class PingRequest(_HelperRequest):
    """Liveness of the channel."""

    type: Literal["ping"] = "ping"


class PathsCompleteRequest(_HelperRequest):
    """The directories that complete a typed path on the host."""

    type: Literal["paths.complete"] = "paths.complete"
    prefix: Annotated[str, StringConstraints(max_length=4096)]
    """A path as typed: ``~`` expands on the host, a relative path is under the home
    directory, and a trailing ``/`` lists the directory itself."""


HelperRequest = Annotated[
    HelloRequest
    | InstancesListRequest
    | InstancesInspectRequest
    | InstancesWatchRequest
    | InstancesUnwatchRequest
    | TasksListRequest
    | RunsListRequest
    | RunStartRequest
    | RunStopRequest
    | RunConnectRequest
    | PingRequest
    | PathsCompleteRequest,
    Field(discriminator="type"),
]
"""Every message a client sends on a helper channel."""

_REQUEST: TypeAdapter[HelperRequest] = TypeAdapter(HelperRequest)


def parse_helper_request(line: bytes) -> HelperRequest:
    """Parse one request line strictly; ``ValidationError`` names what is wrong."""
    return _REQUEST.validate_json(line)


class Checkout(BaseModel):
    """The VibeSys source checkout the helper runs from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str
    commit: str | None
    """``HEAD``'s commit, or ``None`` when git could not say."""
    dirty: bool | None
    """Whether tracked files differ from ``HEAD``; ``None`` when git could not say."""


class _HelperReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: RequestId


class HelloReply(_HelperReply):
    """The negotiated version and features, and what will start runs on this host."""

    type: Literal["hello"] = "hello"
    protocol_version: PositiveInt
    features: tuple[HelperFeature, ...]
    vibesys_version: str
    checkout: Checkout | None
    """``None`` when the helper runs from an installed distribution."""


class InstancesListReply(_HelperReply):
    """The registry's listing."""

    type: Literal["instances.list"] = "instances.list"
    listing: InstanceList


class InstancesInspectReply(_HelperReply):
    """One coherent inspection of a detached server and its retained artifacts."""

    type: Literal["instances.inspect"] = "instances.inspect"
    inspection: InstanceInspection


class InstancesWatchReply(_HelperReply):
    """The watch started; ``instances.changed`` for ``watch_id`` follows at once and on change."""

    type: Literal["instances.watch"] = "instances.watch"
    watch_id: RequestId


class InstancesUnwatchReply(_HelperReply):
    """The watch ended; no ``instances.changed`` for ``watch_id`` follows."""

    type: Literal["instances.unwatch"] = "instances.unwatch"
    watch_id: RequestId


class InstancesChanged(BaseModel):
    """A push on a watch: the listing as it is now, sent first and then on each change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["instances.changed"] = "instances.changed"
    watch_id: RequestId
    listing: InstanceList


class TasksListReply(_HelperReply):
    """The project's tasks."""

    type: Literal["tasks.list"] = "tasks.list"
    tasks: TaskList


class RunSummary(BaseModel):
    """One run of a project, from its durable manifest and this host's registry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    task: str | None
    display_name: str
    created_at: datetime
    phase: RunPhase | None
    """The phase of the server on this host that drives or last drove the run, if one is listed."""
    instance_id: InstanceId | None
    """That server, while it is live: attach to it rather than resume."""


class RunList(BaseModel):
    """A project's runs, oldest first."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_root: str
    runs: tuple[RunSummary, ...]


class RunsListReply(_HelperReply):
    """The project's runs."""

    type: Literal["runs.list"] = "runs.list"
    runs: RunList


class RunStartReply(_HelperReply):
    """The started server's record, or why nothing started."""

    type: Literal["run.start"] = "run.start"
    launch: LiveInstanceRecord | DetachedLaunchFailure


class RunStopReply(_HelperReply):
    """What the stop observed."""

    type: Literal["run.stop"] = "run.stop"
    result: InstanceStopResult


class RunConnectReply(_HelperReply):
    """The channel is now a byte relay carrying the run protocol to this run."""

    type: Literal["run.connect"] = "run.connect"
    run_id: RunId
    protocol_version: PositiveInt
    """The run protocol version the server speaks."""


class PingReply(_HelperReply):
    """The channel is alive."""

    type: Literal["ping"] = "ping"


class PathEntry(BaseModel):
    """One directory that completes the typed path, with what it looks like."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    path: str
    """The absolute path on the host."""
    vibesys_checkout: bool
    """Its ``pyproject.toml`` declares the ``vibesys`` project."""
    vibesys_project: bool
    """It has ``.vibesys/tasks``."""
    git_repository: bool
    """It has a ``.git`` directory or file."""


PATHS_COMPLETE_CAP = 200
"""The most entries one ``paths.complete`` reply carries."""


class PathsCompleteReply(_HelperReply):
    """Matching directories of ``directory``, sorted by name, at most ``PATHS_COMPLETE_CAP``."""

    type: Literal["paths.complete"] = "paths.complete"
    directory: str
    """The absolute directory that was listed."""
    entries: tuple[PathEntry, ...]
    truncated: bool
    """More directories match than were returned, or the directory was too large to read whole."""


class HelperErrorCode(StrEnum):
    """Why a request failed; a closed set."""

    HANDSHAKE_REQUIRED = "handshake_required"
    """A request other than ``hello`` came before a successful ``hello``."""
    INCOMPATIBLE = "incompatible"
    """No protocol version both sides speak, or ``hello`` was sent twice."""
    INVALID_REQUEST = "invalid_request"
    """The line is not a request of the negotiated version."""
    DUPLICATE_REQUEST_ID = "duplicate_request_id"
    """Another request or watch with this id is open on the channel."""
    FEATURE_NOT_NEGOTIATED = "feature_not_negotiated"
    """The method needs a feature ``hello`` did not return."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The reply was not ready within the request's ``deadline_ms``."""
    CHANNEL_BUSY = "channel_busy"
    """The channel's worker limit is full, or ``run.connect`` needs an idle channel."""
    UNKNOWN_WATCH = "unknown_watch"
    """``instances.unwatch`` named no open watch."""
    PROJECT_MISSING = "project_missing"
    """The project directory does not exist or is not a directory."""
    PROJECT_UNINITIALIZED = "project_uninitialized"
    """The directory has no ``.vibesys/tasks``."""
    TASK_INVALID = "task_invalid"
    """The project's tasks or runs could not be read."""
    PATH_MISSING = "path_missing"
    """The directory to complete in does not exist."""
    PATH_NOT_DIRECTORY = "path_not_directory"
    """The path to complete in is not a directory."""
    PERMISSION_DENIED = "permission_denied"
    """This user may not list the directory."""
    RUN_GONE = "run_gone"
    """No live server has the id, or nothing listens at its socket."""
    CONNECT_DENIED = "connect_denied"
    """The run's socket exists but this user may not connect to it."""
    CONNECT_FAILED = "connect_failed"
    """Connecting to the run's socket failed another way."""
    INTERNAL = "internal"
    """The helper failed; ``message`` says how."""


class HelperError(BaseModel):
    """A failed request, or a line that was not one (``request_id`` is ``None`` then)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["error"] = "error"
    request_id: RequestId | None
    code: HelperErrorCode
    message: str
    path: str | None = None
    """The path the error is about, for project and path errors."""
    supported_versions: tuple[PositiveInt, PositiveInt] | None = None
    """For ``incompatible``: the range this helper speaks."""
    vibesys_version: str | None = None
    """For ``incompatible``: the VibeSys this helper belongs to."""


HelperReply = (
    HelloReply
    | InstancesListReply
    | InstancesInspectReply
    | InstancesWatchReply
    | InstancesUnwatchReply
    | TasksListReply
    | RunsListReply
    | RunStartReply
    | RunStopReply
    | RunConnectReply
    | PingReply
    | PathsCompleteReply
)
"""A request's result; its ``type`` is the request's."""

HelperMessage = Annotated[HelperReply | InstancesChanged | HelperError, Field(discriminator="type")]
"""Every message the helper sends on a channel before the channel becomes a relay."""

_MESSAGE: TypeAdapter[HelperMessage] = TypeAdapter(HelperMessage)


def parse_helper_message(line: bytes) -> HelperMessage:
    """Parse one helper message line strictly, as a client does."""
    return _MESSAGE.validate_json(line)


class ChannelEndOutcome(StrEnum):
    """How a run channel's relay ended; each value has its own exit status.

    The relay core (``server.relay``) decides the outcome and maps it to the
    helper's exit status; a client reads it from the ``ChannelEnd`` line.
    """

    CLIENT_CLOSED = "client_closed"
    """Stdin reached end of stream or stdout was closed: the client went away."""
    SERVER_CLOSED = "server_closed"
    """The server closed the connection; everything it sent was relayed first."""
    RUN_GONE = "run_gone"
    """Nothing listens at the socket path (no file, or a stale one): the run is not there."""
    CONNECT_DENIED = "connect_denied"
    """The socket exists but this user may not connect to it."""
    CONNECT_FAILED = "connect_failed"
    """Connecting failed another way, including the connect deadline running out."""
    SERVER_STALLED = "server_stalled"
    """A write toward the server made no progress within the write deadline."""
    CLIENT_STALLED = "client_stalled"
    """A write toward stdout made no progress within the write deadline."""
    SERVER_FAILED = "server_failed"
    """Reading from or writing to the socket failed with an error other than a close."""
    CLIENT_FAILED = "client_failed"
    """Reading stdin or writing stdout failed with an error other than a close."""


class ChannelEnd(BaseModel):
    """The one stderr line a relayed run channel writes when it ends with a nonzero status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ChannelEndOutcome
    exit_status: int
    detail: str

    def line(self) -> str:
        """The report as one newline-terminated JSON line."""
        return self.model_dump_json() + "\n"
