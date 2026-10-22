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

Public surface: ``PROTOCOL_VERSION``, ``RECORD_VERSION``, ``InstanceId``,
``RunId``, ``RunStatus``, ``StartupStage``, ``StartupProgress``, ``StartupFact``, ``FailureReason``,
``RunPhase``, ``InstanceStatus``, ``RunEndOutcome``, ``EndRecord``, ``RunEndingFact``, ``RunClosing``,
``RunDigest``, ``RunDigestUnreadable``, ``one_line``, ``END_REASON_MAX_CHARACTERS``, ``PauseCause``,
``LiveInstanceRecord``, ``UnreadableRecord``, ``parse_record``, ``InstanceList``, ``StopOutcome``,
``StopRoute``, ``InstanceStopResult``, ``DetachedLaunchErrorCode``, ``DetachedLaunchFailure``,
the inspection documents, and the host helper's messages:
the log streams' ``LogStream``, ``LogStreamOutcome``, ``LogChunk``, ``LogPipeSpan``,
``LogPipe``, ``LogOffset``, ``LogTail`` and limits
(``LOGS_READ_MAX_BYTES``, ``LOGS_READ_MIN_BYTES``, ``LOGS_WAIT_MAX_MS``),
``HELPER_PROTOCOL_VERSIONS``, ``HELPER_REQUEST_FEATURES``, ``HELPER_FIELD_FEATURES``,
``request_features``, ``HelperFeature``, ``HelperRequest`` (``parse_helper_request``) and its
members, the run history read's limit (``RUN_EVENTS_READ_MAX_BYTES``), ``RUN_EVENTS_FILE``, the run
history's messages (``server.host_run_events``), the run reader's (``server.host_run_reader``), the run summary's messages
(``server.host_run_summary``, which also holds the usage window's), the run tile's dashboard
(``server.host_run_dashboard``), the machine facts (``server.host_machine_facts``: ``Accelerator``,
``Gpu``, ``Hardware``, ``DockerReachable``, ``DockerUnreachable``, ``MachineCheckout``,
``RunCounts``, ``MachineDescription``) and checkout inspection (``MIN_CHECKOUT_VERSION``, ``CheckoutFound``,
``CheckoutNotFound``, ``CheckoutTooOld``, ``CheckoutInspection``), ``HelperMessage`` and its
members, ``HelperErrorCode``, the harness choice (``HarnessOption``, ``HarnessChoice``,
``RoleAgents``, and the run flags ``CLI_PROVIDER_FLAG``, ``MODEL_FLAG``, ``ROLE_PROVIDER_FLAG``,
``UNVETTED_MODEL_FLAG``, ``RUN_ENVIRONMENT_FLAG``, ``SLURM_CONFIG_FLAG``), where results are
measured, compute settings and agent logins (``server.host_compute``,
``server.host_agent_logins``), and a relayed channel's end: ``ChannelEndOutcome``, ``ChannelEnd``.
An undeliverable successful start whose compensating stop cannot be proved terminal reports
``RunStartCompensationFailure`` on the helper's diagnostic stream.

The helper's messages follow ``docs/contributing/desktop-connection.md``
("Handshake and versioning"): strict within a negotiated version, additive
changes only behind a ``HelperFeature`` that ``hello`` returned.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveInt,
    RootModel,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from server import host_compatibility_generated
from server.host_agent_logins import (
    AgentLoginsListReply,
    AgentLoginsListRequest,
    AgentLoginsStartReply,
    AgentLoginsStartRequest,
)
from server.host_compute import (
    ComputeAddClusterReply,
    ComputeAddClusterRequest,
    ComputeListReply,
    ComputeListRequest,
    ComputeRemoveClusterReply,
    ComputeRemoveClusterRequest,
    ComputeTestClusterReply,
    ComputeTestClusterRequest,
    MeasurementTarget,
)
from server.host_envelope import (
    HelperFeature,
    HelperReplyBase,
    HelperRequestBase,
    InstanceId,
    RequestId,
    RunId,
    SpanId,
)
from server.host_examples import (
    CreatedProject,
    ExampleList,
    ExamplesDestinationReply,
    ExamplesDestinationRequest,
    ProjectTaskGoalReply,
    ProjectTaskGoalRequest,
    TasksListReply,
    TasksListRequest,
)
from server.host_harness import HarnessChoice, HarnessOption, generation_conflict
from server.host_machine_facts import MachineDescription
from server.host_preflight import Backend, Check, RoleName, RunOptions
from server.host_run_dashboard import RunAttention, RunClosing, RunDashboard
from server.host_run_events import (
    RunsEventsForSpanReply,
    RunsEventsForSpanRequest,
    RunsReadEventsReply,
    RunsReadEventsRequest,
    RunsReadTailReply,
    RunsReadTailRequest,
)
from server.host_run_reader import RunReaderReply, RunReaderRequest
from server.host_run_summary import (
    RunsSummaryReply,
    RunsSummaryRequest,
    UsageWindowReply,
    UsageWindowRequest,
)
from vs_decode.api import Unreadable, decode_json

PROTOCOL_VERSION = 1
"""The run protocol version a server speaks on its control socket.

Defined here, not in ``server.api.protocol``, so a record can name it without
importing the run protocol; the protocol module re-exports it.
"""

RECORD_VERSION = 5
"""The registry record version this checkout writes; it also reads versions 3 and 4."""


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
    PROBING_WORKSPACE = "probing_workspace"
    """A scheduler job proves the workspace root is shared with the compute nodes."""
    BUILDING_IMAGE = "building_image"
    """The agent image is being built (the first build on a host takes minutes)."""
    BUILDING_TOOLS = "building_tools"
    """The image is built; the evaluator tools it lacks in cache are being built."""
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

    ``stage`` (and, when the stage reports it, ``progress``) says where a ``starting`` run is;
    ``reason`` says why a ``failed`` one ended. None of them is present for any other status.
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

        The fold is monotone: an ended phase absorbs every fact, and ``failed`` is entered only with
        its reason, from the failure's diagnostic, so a status change to ``failed`` waits for the
        event that says why. A failure while the run is past ``resolving`` names the startup stage
        it failed in. ``starting`` only moves to a later stage (a startup fact of an earlier stage,
        or after ``starting``, changes nothing), and keeps its stage until the run leaves it. The
        statuses themselves move by ``server.run_lifecycle``'s rules; this projects them.
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


class RunEndOutcome(StrEnum):
    """Why a run ended, on its tombstone: vs-core's ``EndReason`` members and one of the shell's.

    The first eight are ``vs_core``'s ``EndReason`` values, copied because this module
    may not import the core (``tests/server/test_run_end_record.py`` keeps them equal).
    """

    ADOPTED = "adopted"
    """The chosen candidate was adopted."""
    KEPT_INPUT = "kept-input"
    """Nothing beat the input, and the input was adopted as it stood."""
    NO_WINNER = "no-winner"
    """The search ended with no winner."""
    ADOPTION_FAILED = "adoption-failed"
    """Core would not adopt the chosen winner."""
    STOPPED = "stopped"
    """The operator stopped the run."""
    DEADLINE = "deadline"
    """The run deadline passed."""
    FINISH_REFUSED = "finish-refused"
    """Core refused the result."""
    FAILED = "failed"
    """The run failed: an error, a crash, or a run environment that could run none of its work."""
    FORCE_STOPPED = "force_stopped"
    """The server was terminated (a force stop) and ended without reaching a safe point."""


END_REASON_MAX_CHARACTERS = 200
"""The longest ``EndRecord.reason``; a longer account is cut to this with an ellipsis."""

OneLine = Annotated[
    str,
    StringConstraints(min_length=1, max_length=END_REASON_MAX_CHARACTERS, pattern=r"^[^\n\r]*$"),
]


def one_line(text: str) -> str | None:
    """The first non-blank line of ``text``, cut to ``END_REASON_MAX_CHARACTERS``; ``None`` if blank."""
    line = next((each.strip() for each in text.splitlines() if each.strip()), None)
    if line is None or len(line) <= END_REASON_MAX_CHARACTERS:
        return line
    return line[: END_REASON_MAX_CHARACTERS - 1] + "\u2026"


class EndRecord(BaseModel):
    """How a run ended, as its tombstone reports it: the sidebar's Recent row and the ended banner.

    ``resumable`` is retained for older readers; current controls permit every ending.
    Launch checks storage and writer exclusion; ``resume_from_round`` is the next round
    (a crashed server did not say). ``uncompared``: the run adopted a candidate never compared
    with the input (it failed its own measurement), whichever ``outcome`` ended it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: RunEndOutcome
    reason: OneLine
    resumable: bool
    resume_from_round: PositiveInt | None = None
    uncompared: bool = False

    @model_validator(mode="after")
    def _round_only_when_resumable(self) -> EndRecord:
        if self.resume_from_round is not None and not self.resumable:
            message = "resume_from_round is set only when the run is resumable"
            raise ValueError(message)
        return self


class RunEndingFact(BaseModel):
    """How a run's durable record stood as it ended, as its server reports it to the record.

    ``outcome`` is core's end reason when the run recorded a result, ``None`` when it ended
    without one; ``summary`` is core's account of that result and ``uncompared`` its fact that
    the adopted candidate was never compared with the input.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: RunEndOutcome | None
    summary: str = ""
    resumable: bool
    rounds_finished: NonNegativeInt
    uncompared: bool = False


class PauseCause(StrEnum):
    """Who paused a paused run."""

    USER = "user"
    """An operator's pause."""
    QUOTA = "quota"
    """A provider capacity limit (``quota_paused``)."""


class InstanceStatus(StrEnum):
    """How far a live server has come; run status itself is on its socket."""

    STARTING = "starting"
    """The server holds its lock; its control socket is not accepting yet."""
    SERVING = "serving"
    """The control socket accepts connections."""


class LiveInstanceRecord(BaseModel):
    """What a detached server publishes about itself (version 5).

    ``run_id`` is allocated by the launcher before the server starts, so the
    record names its run from the first time it is published. ``phase`` is the
    run's lifecycle as the server folds it from its journal, and ``paused`` who
    paused it while it is paused. When the server exits it leaves its last
    record, with an ended phase, ``ended_at`` and ``end``, as a tombstone the
    registry keeps for a day.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[5] = RECORD_VERSION
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
    paused: PauseCause | None = None
    """Who paused the run; a version 5 server sets it whenever ``phase`` is ``paused``."""
    end: EndRecord | None = None
    """How the run ended. A version 5 server sets it on every tombstone, and a crashed
    server's burial does; it is absent on a tombstone upgraded from version 4 and when a
    completed run reported no ending."""
    hostname: str
    protocol_version: Literal[1] = PROTOCOL_VERSION
    vibesys_version: str
    vibesys_root: str | None = None
    """Absolute path of the VibeSys source checkout this server runs from.

    ``None`` when the server runs from an installed distribution rather than a checkout. A client
    uses it to suggest the checkout it should run ``vibesys`` from on this host.
    """

    @model_validator(mode="after")
    def _ended_matches_phase(self) -> LiveInstanceRecord:
        if self.ended_at is not None and not self.phase.status.has_ended:
            message = "ended_at is set only on a record whose phase has ended"
            raise ValueError(message)
        if self.end is not None and not self.phase.status.has_ended:
            message = "end is set only on a record whose phase has ended"
            raise ValueError(message)
        if self.paused is not None and self.phase.status is not RunStatus.PAUSED:
            message = "paused is set only on a record whose phase is paused"
            raise ValueError(message)
        return self


class UnreadableRecord(BaseModel):
    """A registry entry this reader cannot interpret; ``version`` is ``None`` if it has none."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: InstanceId
    version: int | None


_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
MAX_RECORD_BYTES = 1024 * 1024
"""The largest instance record ``parse_record`` reads; a longer one is unreadable."""


def _upgrade_v3(record: LiveInstanceRecord) -> LiveInstanceRecord | None:
    """A version 3 record read as version 4, or ``None`` if it uses what version 3 lacked.

    Version 4 only added startup stages past ``resolving`` and their progress
    to the phase, so every version 3 phase is a version 4 phase.
    """
    stage = record.phase.stage
    if record.phase.progress is not None or stage not in {None, StartupStage.RESOLVING}:
        return None
    return record


def _upgrade_v4(record: LiveInstanceRecord) -> LiveInstanceRecord | None:
    """A version 4 record read as version 5, or ``None`` if it uses what version 4 lacked.

    Version 5 only added ``paused`` and ``end``, both optional, so a version 4
    record is a version 5 record without them.
    """
    if record.paused is not None or record.end is not None:
        return None
    return record


_UPGRADE_STEPS: Final = ((3, _upgrade_v3), (4, _upgrade_v4))
"""Each step reads a record of its version as the next version's, oldest first."""
_UPGRADED_RECORD_VERSIONS: Final = frozenset(since for since, _ in _UPGRADE_STEPS)
"""The earlier record versions this reader still reads, through ``_upgrade``."""


def _upgrade(record: LiveInstanceRecord, version: int) -> LiveInstanceRecord | None:
    """``record``, parsed from a document of ``version``, upgraded step by step to this version."""
    upgraded: LiveInstanceRecord | None = record
    for since, step in _UPGRADE_STEPS:
        if upgraded is not None and version <= since:
            upgraded = step(upgraded)
    return upgraded


def parse_record(instance_id: str, raw: str | bytes) -> LiveInstanceRecord | UnreadableRecord:
    """Parse one stored record, strictly, if it is of a version this reader knows.

    Total: any content, invalid UTF-8 included when given as bytes, is a record
    or an ``UnreadableRecord``.

    A version 3 or 4 record is upgraded (``_upgrade``). Records of earlier
    versions predate phases and tombstones and are not upgraded: their servers
    report no phase this reader could stand behind.
    """
    document = decode_json(
        raw, _JSON_OBJECT, source=f"instance record {instance_id}", max_bytes=MAX_RECORD_BYTES
    )
    if isinstance(document, Unreadable):
        return UnreadableRecord(id=instance_id, version=None)
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        return UnreadableRecord(id=instance_id, version=None)
    record: LiveInstanceRecord | None = None
    if version in _UPGRADED_RECORD_VERSIONS | {RECORD_VERSION}:
        try:
            record = LiveInstanceRecord.model_validate({**document, "version": RECORD_VERSION})
        except ValidationError:
            record = None
    if record is not None and version in _UPGRADED_RECORD_VERSIONS:
        record = _upgrade(record, version)
    if record is None or record.id != instance_id:
        return UnreadableRecord(id=instance_id, version=version)
    return record


class InstanceList(BaseModel):
    """``vibesys instances list --json``: the detached servers on this node (version 5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[5] = 5
    instances: tuple[LiveInstanceRecord, ...]
    """Servers alive now."""
    ended: tuple[LiveInstanceRecord, ...] = ()
    """Servers that exited in the last day, with the phase they ended in and their ``end``."""
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


# --- the host helper ---------------------------------------------------------

HELPER_PROTOCOL_VERSIONS = (1, 1)
"""The lowest and highest helper protocol versions this helper speaks.

Independent of the run protocol's ``PROTOCOL_VERSION``, which ``run.connect``
reports per run.
"""


CLI_PROVIDER_FLAG: Final = "--cli-provider"
"""The run option that names the agent CLI provider; ``run.start`` emits it for ``harness``."""
MODEL_FLAG: Final = "--model"
"""The run option that names the agent model; ``run.start`` emits it for ``harness``."""
UNVETTED_MODEL_FLAG: Final = "--allow-unvetted-model"
"""The terminal's opt-out from the vetted harness list; ``run.start`` refuses it in ``args``."""
MAX_ROUNDS_FLAG: Final = "--max-rounds"
"""The run option that bounds its rounds; ``run.start`` emits it for ``options.rounds``."""
MAX_RUN_SECONDS_FLAG: Final = "--max-run-seconds"
"""Overrides ``[run].max_run_seconds``; ``run.start`` emits it for ``options.time_limit_s``."""
ROLE_PROVIDER_FLAG: Final = "--role-provider"
"""``ROLE=PROVIDER``, overriding ``agent.roles.ROLE.provider``; emitted per ``agents`` entry."""
ROLE_MODEL_FLAG: Final = "--role-model"
"""``ROLE=MODEL``, overriding ``agent.roles.ROLE.model``; emitted per ``options.roles`` model."""
ROLE_REASONING_EFFORT_FLAG: Final = "--role-reasoning-effort"
"""``ROLE=EFFORT``, overriding ``agent.roles.ROLE.reasoning_effort``; emitted per role effort."""
PUBLISH_FLAG: Final = "--publish"
"""Publishes the project to GitHub under the generated name; emitted for ``publish_github``."""
REPO_FLAG: Final = "--repo"
"""Publishes the project to a named GitHub repository; a typed ``results`` refuses it in ``args``."""
RUN_ENVIRONMENT_FLAG: Final = "--run-environment"
"""Selects where evaluation runs; ``run.start`` emits it for ``measure_on``'s Slurm and Modal."""
SLURM_CONFIG_FLAG: Final = "--slurm-config"
"""The operator file of a Slurm run; ``run.start`` emits it for ``measure_on``'s Slurm cluster."""
ENVIRONMENT_FLAGS: Final = (RUN_ENVIRONMENT_FLAG, SLURM_CONFIG_FLAG, "--modal", "--skypilot")
"""The flags that choose where a run measures; ``measure_on`` is their only source in ``args``."""
LOCAL_FLAG: Final = "--local"
"""Keeps the run local; ``run.start`` emits it for ``results: keep``."""


class HelloRequest(HelperRequestBase):
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


class InstancesListRequest(HelperRequestBase):
    """The live and recently ended detached servers on this host."""

    type: Literal["instances.list"] = "instances.list"


class InstancesInspectRequest(HelperRequestBase):
    """Inspect one detached server without starting another host process.

    ``include_log`` is deprecated: it asks for a tail of ``server.log``, the detached
    server's raw output, which is for a person at a terminal. No client UI may depend on
    it; a run's logs come from its log store through ``logs.list`` and ``logs.read``. The
    helper keeps answering it so older clients keep working, and removes it at the next
    helper protocol version (2).
    """

    type: Literal["instances.inspect"] = "instances.inspect"
    instance_id: InstanceId
    include_log: bool = False


class InstancesWatchRequest(HelperRequestBase):
    """Push the listing now and again whenever it changes, until ``instances.unwatch``."""

    type: Literal["instances.watch"] = "instances.watch"


class InstancesUnwatchRequest(HelperRequestBase):
    """Stop the watch that ``watch_id`` (its request's id) started."""

    type: Literal["instances.unwatch"] = "instances.unwatch"
    watch_id: RequestId


RUN_DIGEST_MAX_RUNS: Final = 100
"""The most runs one ``runs.list`` may ask a digest of."""


class RunsListRequest(HelperRequestBase):
    """A project's runs, with the phase of any server on this host that drives one."""

    type: Literal["runs.list"] = "runs.list"
    project: str
    digest_runs: Annotated[tuple[RunId, ...], Field(max_length=RUN_DIGEST_MAX_RUNS)] | None = None
    """The runs to read a ``digest`` of (a pass over each journal), so a client names only the runs
    it shows. Sent only after ``hello`` returned ``run_digest``."""
    with_dashboard: bool | None = None
    """Also each digest's ``dashboard`` and run's ``attention``; needs ``run_dashboard``."""


class ResumeRun(BaseModel):
    """Resume this run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId


class ResumeLatest(BaseModel):
    """Resume the project's current or latest run; the launch resolves it before detaching."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    latest: Literal[True]


_ROLE_NAME: Final = TypeAdapter(RoleName)


def _role_ids(agents: dict[str, HarnessChoice]) -> dict[str, HarnessChoice]:
    """Refuse a key that is not a role id, naming it.

    The keys are checked here and not typed as ``RoleName`` because the helper's generated schema
    says a key's shape with ``patternProperties``, which the desktop's schema walker does not walk.
    """
    for role in agents:
        try:
            _ROLE_NAME.validate_python(role)
        except ValidationError as error:
            message = f"agents.{role}: not a role id ({error.errors()[0]['msg']})"
            raise ValueError(message) from error
    return agents


RoleAgents = Annotated[dict[str, HarnessChoice], AfterValidator(_role_ids)]
"""An agent per role: each key a role id ``run.preflight`` returns with ``with_roles``, each value a
pair ``harnesses.list`` offers. ``run.start`` and ``run.preflight`` refuse an id the run does not
declare and a pair not offered, naming the key."""


class HarnessesListRequest(HelperRequestBase):
    """The vetted provider and model pairs ``run.start``'s ``harness`` accepts."""

    type: Literal["harnesses.list"] = "harnesses.list"
    with_generations: bool | None = None
    """Ask for each pair's ``display.maker`` and ``display.generation``. Sent only after ``hello``
    returned ``harness_generations``."""


class RunStartRequest(HelperRequestBase):
    """Start a detached run (``vibesys --detach``) in ``project``.

    ``args`` never carry ``UNVETTED_MODEL_FLAG`` (or an abbreviation of it): a run
    started through the helper uses only vetted harnesses. With ``harness``, they
    carry neither ``CLI_PROVIDER_FLAG`` nor ``MODEL_FLAG``, so the pair has one source.
    """

    type: Literal["run.start"] = "run.start"
    project: str
    task: str | None = None
    args: tuple[str, ...] = ()
    """Further run options, one argument per item; never parsed by a shell."""
    resume: ResumeRun | ResumeLatest | None = None
    harness: HarnessChoice | None = None
    """A pair ``harnesses.list`` offered; ``None`` keeps the project's configuration.
    Sent only after ``hello`` returned ``harnesses``."""
    options: RunOptions | None = None
    """Typed run options; ``args`` may then not repeat any option set here.
    Sent only after ``hello`` returned ``run_options``."""
    agents: RoleAgents | None = None
    """The agent each named role runs; a role without an entry runs ``harness``. Sent only after
    ``hello`` returned ``run_role_agents``."""
    measure_on: MeasurementTarget | None = None
    """Where results are measured; ``args`` then carry no run-environment flag. Sent only after
    ``hello`` returned ``measurement_target``."""


class RunPreflightRequest(HelperRequestBase):
    """Whether the run ``run.start`` would launch with these inputs can start, check by check."""

    type: Literal["run.preflight"] = "run.preflight"
    project: str
    task: str | None = None
    args: tuple[str, ...] = ()
    harness: HarnessChoice | None = None
    options: RunOptions | None = None
    agents: RoleAgents | None = None
    measure_on: MeasurementTarget | None = None
    """As ``run.start``'s; the reply then ends with the ``measurement_target`` check."""
    with_roles: bool | None = None
    """Ask for ``roles`` in the reply. Sent only after ``hello`` returned ``run_roles``."""
    with_project_clean: bool | None = None
    """Ask for the ``project_clean`` check. Sent only after ``hello`` returned ``project_clean``."""
    with_backend: bool | None = None
    """Ask for ``backend`` in the reply and a ``cause`` on the checks that have one. Sent only
    after ``hello`` returned ``run_backend``."""
    with_causes: bool | None = None
    """Ask for the typed ``cause`` of every other way ``settings_valid`` can fail, and a ``detail``
    that names no flag. Sent only after ``hello`` returned ``run_causes``."""


class RunStopRequest(HelperRequestBase):
    """Stop a live detached server (``vibesys instances stop``)."""

    type: Literal["run.stop"] = "run.stop"
    instance_id: InstanceId
    force: bool = False


class RunRole(StrEnum):
    """Which run-protocol connection a run channel carries (``WP-ROLES``)."""

    CONTROL = "control"
    SUBSCRIBE = "subscribe"
    CHAT = "chat"


class RunConnectRequest(HelperRequestBase):
    """Make this channel a byte relay to a live run's control socket."""

    type: Literal["run.connect"] = "run.connect"
    instance_id: InstanceId
    role: RunRole


class PingRequest(HelperRequestBase):
    """Liveness of the channel."""

    type: Literal["ping"] = "ping"


class PathsCompleteRequest(HelperRequestBase):
    """The directories that complete a typed path on the host."""

    type: Literal["paths.complete"] = "paths.complete"
    prefix: Annotated[str, StringConstraints(max_length=4096)]
    """A path as typed: ``~`` expands on the host, a relative path is under the home
    directory, and a trailing ``/`` lists the directory itself."""


LOGS_READ_MAX_BYTES: Final = 64 * 1024
"""The most bytes one ``logs.read`` returns, so a reply stays a small helper line."""
LOGS_READ_MIN_BYTES: Final = 4
"""The fewest bytes one ``logs.read`` may ask for: one whole UTF-8 character, so every read of
available bytes makes progress."""
LOGS_WAIT_MAX_MS: Final = 30_000
"""The longest a ``logs.read`` waits at the end of an open stream for more bytes."""

LogStreamId = Annotated[str, StringConstraints(pattern=r"^[0-9]{6,12}$")]
"""A recorded stream's id in its run's log store (``vs_logs``): digits, unique within the run."""

LogOwner = dict[str, JsonValue]
"""What a stream is for (a startup step, a gate stage, an agent invocation, ...).

The writer validated it against vibesys's ``LogOwner`` union, whose generated TypeScript
is ``LogOwner`` in the run protocol's bindings; the helper passes it through as JSON so it
never imports vibesys, and a client narrows it with that union.
"""


class LogPipe(StrEnum):
    """Which of a command's output pipes wrote some bytes."""

    STDOUT = "stdout"
    STDERR = "stderr"


class LogStreamOutcome(StrEnum):
    """How a stream was closed."""

    ENDED = "ended"
    """Its writer closed it normally."""
    FAILED = "failed"
    """Its writer closed it because the work it records failed."""
    ABANDONED = "abandoned"
    """Its writer stopped without closing it; a later store found it open."""


class LogOffset(BaseModel):
    """Read from this byte offset of the stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    offset: NonNegativeInt


class LogTail(BaseModel):
    """Read the last ``tail_bytes`` bytes the stream holds, from the first line start in them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tail_bytes: Annotated[int, Field(ge=1, le=LOGS_READ_MAX_BYTES)]


class LogsListRequest(HelperRequestBase):
    """The log streams recorded for one instance, live or ended."""

    type: Literal["logs.list"] = "logs.list"
    instance_id: InstanceId
    span_id: SpanId | None = None
    """Only the streams whose owner names this run-trace span (feature ``logs_by_span``)."""


class LogsReadRequest(HelperRequestBase):
    """One bounded chunk of a stream; at the end of an open stream, wait up to ``wait_ms`` for more.

    A wait ends as soon as bytes arrive or the stream closes, and always before
    the request's deadline, so a follower keeps exactly one read in flight.
    """

    type: Literal["logs.read"] = "logs.read"
    instance_id: InstanceId
    stream_id: LogStreamId
    start: LogOffset | LogTail
    max_bytes: Annotated[int, Field(ge=LOGS_READ_MIN_BYTES, le=LOGS_READ_MAX_BYTES)]
    wait_ms: Annotated[int, Field(ge=0, le=LOGS_WAIT_MAX_MS)] = 0


class LogsCancelRequest(HelperRequestBase):
    """Withdraw the ``logs.read`` whose request id is ``read_id``: a wait it is in ends now.

    The read still answers, with what it holds (an empty chunk at the end of the
    stream), and its worker returns, so the channel can admit another request.
    Naming a read that already answered, or no read, is not an error: withdrawal
    races the reply by nature. Admitted even when the channel's workers are full.
    """

    type: Literal["logs.cancel"] = "logs.cancel"
    read_id: RequestId


RUN_EVENTS_READ_MAX_BYTES: Final = 2 * 1024 * 1024
"""The most journal bytes one ``runs.read_events`` reply carries, so it stays a helper line.

A page stops before the event that would pass it; an event longer than this alone is
returned alone, so every read makes progress."""

RUN_EVENTS_FILE: Final = "run-events.jsonl"
"""The run's event journal, one ``RunEvent`` per line, in the run's log directory."""


class MachineDescribeRequest(HelperRequestBase):
    """What this host offers runs: its checkout, hardware, Docker, harness CLIs and runs."""

    type: Literal["machine.describe"] = "machine.describe"


class CheckoutInspectRequest(HelperRequestBase):
    """Whether ``path`` holds a VibeSys checkout, and whether this app can use it."""

    type: Literal["checkout.inspect"] = "checkout.inspect"
    path: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_PATH_CHARACTERS)]
    """A path as typed: ``~`` expands on the host, a relative path is under the home directory."""


class ExamplesListRequest(HelperRequestBase):
    """The examples of the checkout this helper runs from, with their tasks."""

    type: Literal["examples.list"] = "examples.list"
    with_catalogue: bool | None = None
    """Ask for each example's ``task_details``. Sent only after ``hello`` returned
    ``example_catalogue``."""


class ProjectCreateFromExampleRequest(HelperRequestBase):
    """Copy ``example`` into its own new Git repository at ``destination`` (absent or empty)."""

    type: Literal["project.create_from_example"] = "project.create_from_example"
    example: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    """A name ``examples.list`` listed."""
    destination: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_PATH_CHARACTERS)]
    """A path as typed, as for ``paths.complete``."""


class ProjectCopyOutRequest(HelperRequestBase):
    """Copy ``source``, a folder inside another Git repository, into its own new one at ``destination``."""

    type: Literal["project.copy_out"] = "project.copy_out"
    source: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_PATH_CHARACTERS)]
    destination: Annotated[str, StringConstraints(min_length=1, max_length=_MAX_PATH_CHARACTERS)]
    """Must not exist."""


HELPER_REQUEST_FEATURES: Final[Mapping[type[HelperRequestBase], HelperFeature]] = {
    InstancesInspectRequest: HelperFeature.INSTANCES_INSPECT,
    InstancesWatchRequest: HelperFeature.INSTANCES_WATCH,
    InstancesUnwatchRequest: HelperFeature.INSTANCES_WATCH,
    PathsCompleteRequest: HelperFeature.PATHS_COMPLETE,
    LogsListRequest: HelperFeature.LOGS,
    LogsReadRequest: HelperFeature.LOGS,
    LogsCancelRequest: HelperFeature.LOGS_CANCEL,
    HarnessesListRequest: HelperFeature.HARNESSES,
    RunsReadEventsRequest: HelperFeature.RUN_HISTORY,
    RunsEventsForSpanRequest: HelperFeature.RUN_SPAN_EVENTS,
    RunsReadTailRequest: HelperFeature.RUN_TAIL,
    RunsSummaryRequest: HelperFeature.RUN_TRACE,
    MachineDescribeRequest: HelperFeature.MACHINE_DESCRIBE,
    CheckoutInspectRequest: HelperFeature.CHECKOUT_INSPECT,
    RunPreflightRequest: HelperFeature.PREFLIGHT,
    ProjectTaskGoalRequest: HelperFeature.PROJECT_GOAL,
    ExamplesDestinationRequest: HelperFeature.EXAMPLE_DESTINATION,
    ExamplesListRequest: HelperFeature.EXAMPLES,
    ProjectCreateFromExampleRequest: HelperFeature.EXAMPLES,
    ProjectCopyOutRequest: HelperFeature.EXAMPLES,
    ComputeListRequest: HelperFeature.MACHINE_COMPUTE,
    ComputeTestClusterRequest: HelperFeature.MACHINE_COMPUTE,
    ComputeAddClusterRequest: HelperFeature.MACHINE_COMPUTE,
    ComputeRemoveClusterRequest: HelperFeature.MACHINE_COMPUTE,
    AgentLoginsListRequest: HelperFeature.AGENT_LOGINS,
    AgentLoginsStartRequest: HelperFeature.AGENT_LOGINS,
    UsageWindowRequest: HelperFeature.USAGE_WINDOW,
    RunReaderRequest: HelperFeature.CHAT_ENDED_RUNS,
}
"""The feature each gated request needs, exported with the generated wire contract."""

HELPER_FIELD_FEATURES: Final[Mapping[type[HelperRequestBase], Mapping[str, HelperFeature]]] = {
    RunStartRequest: {
        "harness": HelperFeature.HARNESSES,
        "options": HelperFeature.RUN_OPTIONS,
        "agents": HelperFeature.RUN_ROLE_AGENTS,
        "measure_on": HelperFeature.MEASUREMENT_TARGET,
    },
    RunPreflightRequest: {
        "harness": HelperFeature.HARNESSES,
        "options": HelperFeature.RUN_OPTIONS,
        "agents": HelperFeature.RUN_ROLE_AGENTS,
        "measure_on": HelperFeature.MEASUREMENT_TARGET,
        "with_roles": HelperFeature.RUN_ROLES,
        "with_backend": HelperFeature.RUN_BACKEND,
        "with_causes": HelperFeature.RUN_CAUSES,
        "with_project_clean": HelperFeature.PROJECT_CLEAN,
    },
    HarnessesListRequest: {"with_generations": HelperFeature.HARNESS_GENERATIONS},
    LogsListRequest: {"span_id": HelperFeature.LOGS_BY_SPAN},
    RunsReadEventsRequest: {
        "with_totals": HelperFeature.RUN_HISTORY_PAGES,
        "kinds": HelperFeature.RUN_TRACE_EVENTS,
    },
    RunsSummaryRequest: {
        "offset": HelperFeature.RUN_SUMMARY,
        "with_ending": HelperFeature.RUN_ENDING,
    },
    RunsListRequest: {
        "digest_runs": HelperFeature.RUN_DIGEST,
        "with_dashboard": HelperFeature.RUN_DASHBOARD,
    },
    ExamplesListRequest: {"with_catalogue": HelperFeature.EXAMPLE_CATALOGUE},
    RunReaderRequest: {"with_chat": HelperFeature.CHAT_ENDED_RUN_QUESTIONS},
}
"""The feature each gated optional field needs when it is set (not ``None``), exported too."""


def request_features(request: HelperRequestBase) -> tuple[HelperFeature, ...]:
    """The features ``hello`` must have returned for ``request``: its method's and its set fields'."""
    method = HELPER_REQUEST_FEATURES.get(type(request))
    features = [] if method is None else [method]
    for name, feature in HELPER_FIELD_FEATURES.get(type(request), {}).items():
        if getattr(request, name) is not None and feature not in features:
            features.append(feature)
    return tuple(features)


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
    | PathsCompleteRequest
    | LogsListRequest
    | LogsReadRequest
    | LogsCancelRequest
    | HarnessesListRequest
    | RunsReadEventsRequest
    | RunsEventsForSpanRequest
    | RunsReadTailRequest
    | RunsSummaryRequest
    | MachineDescribeRequest
    | CheckoutInspectRequest
    | RunPreflightRequest
    | ExamplesListRequest
    | ProjectCreateFromExampleRequest
    | ProjectCopyOutRequest
    | ComputeListRequest
    | ComputeTestClusterRequest
    | ComputeAddClusterRequest
    | ComputeRemoveClusterRequest
    | AgentLoginsListRequest
    | AgentLoginsStartRequest
    | ProjectTaskGoalRequest
    | ExamplesDestinationRequest
    | UsageWindowRequest
    | RunReaderRequest,
    Field(discriminator="type"),
]
"""Every message a client sends on a helper channel."""

_REQUEST: TypeAdapter[HelperRequest] = TypeAdapter(HelperRequest)


HELPER_LINE_MAX_BYTES: Final = 1 << 20
"""The longest line either end of a helper channel parses."""


def parse_helper_request(line: bytes) -> HelperRequest | Unreadable:
    """Parse one request line strictly, or say what is wrong with it."""
    return decode_json(line, _REQUEST, source="request", max_bytes=HELPER_LINE_MAX_BYTES)


class Checkout(BaseModel):
    """The VibeSys source checkout the helper runs from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str
    commit: str | None
    """``HEAD``'s commit, or ``None`` when git could not say."""
    dirty: bool | None
    """Whether tracked files differ from ``HEAD``; ``None`` when git could not say."""


class HelloReply(HelperReplyBase):
    """The negotiated version and features, and what will start runs on this host."""

    type: Literal["hello"] = "hello"
    protocol_version: PositiveInt
    features: tuple[HelperFeature, ...]
    vibesys_version: str
    checkout: Checkout | None
    """``None`` when the helper runs from an installed distribution."""


class InstancesListReply(HelperReplyBase):
    """The registry's listing."""

    type: Literal["instances.list"] = "instances.list"
    listing: InstanceList


class InstancesInspectReply(HelperReplyBase):
    """One coherent inspection of a detached server and its retained artifacts."""

    type: Literal["instances.inspect"] = "instances.inspect"
    inspection: InstanceInspection


class InstancesWatchReply(HelperReplyBase):
    """The watch started; ``instances.changed`` for ``watch_id`` follows at once and on change."""

    type: Literal["instances.watch"] = "instances.watch"
    watch_id: RequestId


class InstancesUnwatchReply(HelperReplyBase):
    """The watch ended; no ``instances.changed`` for ``watch_id`` follows."""

    type: Literal["instances.unwatch"] = "instances.unwatch"
    watch_id: RequestId


class InstancesChanged(BaseModel):
    """A push on a watch: the listing as it is now, sent first and then on each change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["instances.changed"] = "instances.changed"
    watch_id: RequestId
    listing: InstanceList


class RunDigest(BaseModel):
    """What a run's journal says at a glance, for a list row; never a replay of the run.

    ``ideas`` counts the ideas the run page counts (one per hypothesis, probes excluded) and
    ``adopted_ratio`` is the result the run's adoption span names, as the run page headlines it:
    an improvement over the input (for a ``min`` objective, the input's value over the result's);
    ``None`` when the run adopted nothing. The best raw reading of a gate is not it: that may be
    an idea that later failed. ``best_so_far_ratio`` is what the page headlines before the run has
    opened an adoption (while it works): the best idea that passed and beat the input, worded as an
    improvement the same way; ``None`` before one does and once an adoption exists. ``ideas`` is
    ``None`` for a journal that predates the run trace (``TraceFormat.PREDATES_TRACE``): it
    records rounds, not ideas, so no count is true. ``duration_s`` runs from the journal's first
    event to the last step it recorded; ``None`` for a run with no events. ``ending`` is the run's
    ending record and ``closing`` its terminal event: both ``None`` for a run still going or one
    whose server died without a word; they are how an ending is known once its tombstone expired.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["digest"] = "digest"
    ideas: NonNegativeInt | None
    adopted_ratio: float | None
    best_so_far_ratio: float | None
    duration_s: NonNegativeFloat | None
    ending: RunEndingFact | None
    closing: RunClosing | None
    dashboard: Annotated[RunDashboard | None, Field(exclude_if=lambda each: each is None)] = None
    """The tile's data when ``runs.list`` asked ``with_dashboard``; absent otherwise."""


class RunDigestUnreadable(BaseModel):
    """A run whose journal exists and could not be read; ``message`` says why, in one line."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["unreadable"] = "unreadable"
    message: OneLine


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
    digest: Annotated[
        Annotated[RunDigest | RunDigestUnreadable, Field(discriminator="kind")] | None,
        Field(exclude_if=lambda digest: digest is None),
    ] = None
    """What the journal says of each run named in ``digest_runs`` (feature ``run_digest``); absent for
    the others, so a strict client that did not ask never sees it."""
    attention: Annotated[RunAttention | None, Field(exclude_if=lambda each: each is None)] = None
    """Why the run needs its user when ``runs.list`` asked ``with_dashboard``; absent otherwise."""


class RunList(BaseModel):
    """A project's runs, oldest first."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_root: str
    runs: tuple[RunSummary, ...]


class RunsListReply(HelperReplyBase):
    """The project's runs."""

    type: Literal["runs.list"] = "runs.list"
    runs: RunList


class RunStartReply(HelperReplyBase):
    """The started server's record, or why nothing started."""

    type: Literal["run.start"] = "run.start"
    launch: LiveInstanceRecord | DetachedLaunchFailure


class RunStopReply(HelperReplyBase):
    """What the stop observed."""

    type: Literal["run.stop"] = "run.stop"
    result: InstanceStopResult


class RunConnectReply(HelperReplyBase):
    """The channel is now a byte relay carrying the run protocol to this run."""

    type: Literal["run.connect"] = "run.connect"
    run_id: RunId
    protocol_version: PositiveInt
    """The run protocol version the server speaks."""


class PingReply(HelperReplyBase):
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


class PathsCompleteReply(HelperReplyBase):
    """Matching directories of ``directory``, sorted by name, at most ``PATHS_COMPLETE_CAP``."""

    type: Literal["paths.complete"] = "paths.complete"
    directory: str
    """The absolute directory that was listed."""
    entries: tuple[PathEntry, ...]
    truncated: bool
    """More directories match than were returned, or the directory was too large to read whole."""


class LogStream(BaseModel):
    """One recorded log stream of an instance, as its store indexes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: LogStreamId
    owner: LogOwner
    command: str
    """What wrote the stream, for display: a command line, or the narration's name."""
    retained_start: NonNegativeInt
    """The oldest offset the store still holds; bytes before it were evicted."""
    size: NonNegativeInt
    """How many bytes were ever written: the offset the next byte will have."""
    outcome: LogStreamOutcome | None
    """How the stream was closed; ``None`` while it is open."""

    @model_validator(mode="after")
    def _retained(self) -> LogStream:
        if self.retained_start > self.size:
            message = "retained_start must not pass size"
            raise ValueError(message)
        return self


class LogPipeSpan(BaseModel):
    """Bytes ``[offset, offset + length)`` of a stream were written by ``pipe``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    offset: NonNegativeInt
    """A stream offset, like ``LogChunk.offset``."""
    length: PositiveInt
    pipe: LogPipe


class LogsListReply(HelperReplyBase):
    """The instance's streams, oldest first."""

    type: Literal["logs.list"] = "logs.list"
    streams: tuple[LogStream, ...]


class LogChunk(BaseModel):
    """Bytes ``[offset, next_offset)`` of a stream, decoded as UTF-8 text.

    The helper cuts a chunk that more bytes follow after the last line end in
    its window, or, with no line end there, before a character the window
    splits; so ``data`` decodes without loss except for bytes that are not
    UTF-8, which become U+FFFD. ``next_offset - offset`` counts bytes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    offset: NonNegativeInt
    """Where ``data`` starts: later than asked when the asked-for bytes were evicted
    (``truncated_before``), or when a tail read skipped to the next line start."""
    data: str = Field(json_schema_extra={"x-vibesys-max-utf8-bytes": 3 * LOGS_READ_MAX_BYTES})
    next_offset: NonNegativeInt
    """Where the next read continues."""
    eof: bool
    """``next_offset`` is the end of what the stream holds now (an unfinished last
    character of an open stream is held back until it is complete)."""
    closed: bool
    """The stream is closed: once ``eof``, no more bytes will come."""
    truncated_before: bool
    """Bytes the read asked for, before ``offset``, were evicted and are gone."""
    pipes: tuple[LogPipeSpan, ...]
    """Which pipe wrote each byte of ``[offset, next_offset)``: adjacent spans, in order,
    in stream offsets (the coordinates of ``offset``), covering the chunk exactly."""

    @model_validator(mode="after")
    def _ordered(self) -> LogChunk:
        span = self.next_offset - self.offset
        if span < 0 or span > LOGS_READ_MAX_BYTES:
            message = "next_offset must follow offset by at most LOGS_READ_MAX_BYTES"
            raise ValueError(message)
        cursor = self.offset
        for pipe in self.pipes:
            if pipe.offset != cursor:
                message = "pipe spans must be adjacent and start at offset"
                raise ValueError(message)
            cursor += pipe.length
        if cursor != self.next_offset:
            message = "pipe spans must cover offset to next_offset exactly"
            raise ValueError(message)
        return self


class LogsReadReply(HelperReplyBase):
    """One chunk of the stream."""

    type: Literal["logs.read"] = "logs.read"
    chunk: LogChunk


class LogsCancelReply(HelperReplyBase):
    """The withdrawal was taken: a read ``read_id`` named that was still waiting stops waiting."""

    type: Literal["logs.cancel"] = "logs.cancel"
    read_id: RequestId


class HarnessesListReply(HelperReplyBase):
    """The vetted pairs, grouped by provider with each provider's default first."""

    type: Literal["harnesses.list"] = "harnesses.list"
    harnesses: tuple[HarnessOption, ...]

    @model_validator(mode="after")
    def _generations_group_without_contradiction(self) -> HarnessesListReply:
        conflict = generation_conflict(self.harnesses)
        if conflict is not None:
            raise ValueError(conflict)
        return self


class MachineDescribeReply(HelperReplyBase):
    """The machine's description."""

    type: Literal["machine.describe"] = "machine.describe"
    machine: MachineDescription


MIN_CHECKOUT_VERSION: Final = host_compatibility_generated.MIN_CHECKOUT_VERSION


class CheckoutFound(BaseModel):
    """A VibeSys checkout at least ``MIN_CHECKOUT_VERSION``.

    ``compatible`` says whether it ships the host helper this app starts from a checkout.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["found"] = "found"
    root: str
    version: str
    commit: str | None
    dirty: bool | None
    compatible: bool


class CheckoutNotFound(BaseModel):
    """The path holds no VibeSys checkout (or does not exist)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["not_found"] = "not_found"


class CheckoutTooOld(BaseModel):
    """A VibeSys checkout older than this app supports: ``version`` is below ``min``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: Literal["too_old"] = "too_old"
    version: str
    """The version it declares; empty when it declares none."""
    min: str


CheckoutInspection = Annotated[
    CheckoutFound | CheckoutNotFound | CheckoutTooOld, Field(discriminator="state")
]


class ExamplesListReply(HelperReplyBase):
    """The checkout's example catalog."""

    type: Literal["examples.list"] = "examples.list"
    listing: ExampleList


class ProjectCreateFromExampleReply(HelperReplyBase):
    """The project made from the example."""

    type: Literal["project.create_from_example"] = "project.create_from_example"
    created: CreatedProject


class ProjectCopyOutReply(HelperReplyBase):
    """The project copied out into its own repository."""

    type: Literal["project.copy_out"] = "project.copy_out"
    created: CreatedProject


class RunPreflightReply(HelperReplyBase):
    """Every check, in ``CheckId`` order; Start is enabled exactly when none is ``fail``."""

    type: Literal["run.preflight"] = "run.preflight"
    checks: tuple[Check, ...]
    roles: Annotated[tuple[RoleName, ...] | None, Field(exclude_if=lambda roles: roles is None)] = (
        None
    )
    """The role ids the run's orchestration declares, in declaration order: what a
    ``RoleOption.role`` may name. Present only when the request set ``with_roles`` (feature
    ``run_roles``), so a strict client that did not ask never sees the field."""
    backend: Annotated[Backend | None, Field(exclude_if=lambda backend: backend is None)] = None
    """The compute backend the run resolved to, which its task declares; ``None`` when the
    settings resolved none. Present only when the request set ``with_backend`` (feature
    ``run_backend``), as are the checks' ``cause`` fields."""


class CheckoutInspectReply(HelperReplyBase):
    """What the path holds."""

    type: Literal["checkout.inspect"] = "checkout.inspect"
    inspection: CheckoutInspection


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
    UNKNOWN_INSTANCE = "unknown_instance"
    """``logs.list`` or ``logs.read`` named an instance this host recorded no logs for."""
    UNKNOWN_LOG_STREAM = "unknown_log_stream"
    """``logs.read`` named a stream the instance does not have."""
    LOG_STORE_CORRUPT = "log_store_corrupt"
    """``logs.list`` or ``logs.read`` found the run's log index corrupt; asking again does not help."""
    UNKNOWN_EXAMPLE = "unknown_example"
    """``project.create_from_example`` named no example, or one ambiguously; the message lists."""
    EXAMPLES_UNAVAILABLE = "examples_unavailable"
    """The helper runs from no checkout with an example catalog, or the catalog is invalid."""
    DESTINATION_EXISTS = "destination_exists"
    """A new project's destination exists (and, for an example, is not an empty directory)."""
    DESTINATION_NESTED = "destination_nested"
    """A new project's destination lies inside a Git repository or the VibeSys checkout."""
    NOT_NESTED = "not_nested"
    """``project.copy_out``'s source is in no other Git repository: it can run where it is."""
    PROJECT_CREATE_FAILED = "project_create_failed"
    """Copying or committing failed; nothing is left at the destination, and the message says why."""
    UNKNOWN_RUN = "unknown_run"
    """``runs.read_events`` or ``runs.summary`` named a run the project does not have; the message names it."""
    UNVETTED_HARNESS = "unvetted_harness"
    """``run.start``'s ``harness`` is not one ``harnesses.list`` offers; only sent with ``harnesses``."""
    UNKNOWN_CLUSTER = "unknown_cluster"
    """``compute.remove_cluster`` or ``measure_on`` named a cluster not set up; the message names it."""
    CLUSTER_EXISTS = "cluster_exists"
    """``compute.add_cluster`` named a cluster already set up on this machine."""
    CLUSTER_READ_ONLY = "cluster_read_only"
    """``compute.remove_cluster`` named a cluster of a file this helper only reads."""
    CLUSTER_TEST_FAILED = "cluster_test_failed"
    """``compute.add_cluster``'s cluster did not pass its connection test; nothing was saved."""
    CLUSTER_INVALID = "cluster_invalid"
    """A cluster's settings are rejected by the Slurm configuration; the message names the keys."""
    COMPUTE_CONFIG_UNREADABLE = "compute_config_unreadable"
    """The compute settings file cannot be read or parsed; ``path`` names it."""
    MODAL_NOT_CONFIGURED = "modal_not_configured"
    """``run.start``'s ``measure_on`` is Modal and the machine holds no token."""
    UNKNOWN_PROVIDER = "unknown_provider"
    """``agent_logins.start`` named a provider with no harness here; the message names it."""
    READER_FAILED = "reader_failed"
    """``run.reader`` could not start the read-only server of a run with no live one; the message says why."""
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
    | LogsListReply
    | LogsReadReply
    | LogsCancelReply
    | HarnessesListReply
    | RunsReadEventsReply
    | RunsEventsForSpanReply
    | RunsReadTailReply
    | RunsSummaryReply
    | MachineDescribeReply
    | CheckoutInspectReply
    | RunPreflightReply
    | ExamplesListReply
    | ProjectCreateFromExampleReply
    | ProjectCopyOutReply
    | ComputeListReply
    | ComputeTestClusterReply
    | ComputeAddClusterReply
    | ComputeRemoveClusterReply
    | AgentLoginsListReply
    | AgentLoginsStartReply
    | ProjectTaskGoalReply
    | ExamplesDestinationReply
    | UsageWindowReply
    | RunReaderReply
)
"""A request's result; its ``type`` is the request's."""

HelperMessage = Annotated[HelperReply | InstancesChanged | HelperError, Field(discriminator="type")]
"""Every message the helper sends on a channel before the channel becomes a relay."""

_MESSAGE: TypeAdapter[HelperMessage] = TypeAdapter(HelperMessage)


def parse_helper_message(line: bytes) -> HelperMessage | Unreadable:
    """Parse one helper message line strictly, as a client does, or say what is wrong."""
    return decode_json(line, _MESSAGE, source="message", max_bytes=HELPER_LINE_MAX_BYTES)


COMPENSATION_FAILURE_EXIT_STATUS: Final = 12
"""The helper could not prove cleanup of a successful start the client never received."""
COMPENSATION_FAILURE_TYPE: Final = "run.start_compensation_failed"
"""The discriminant of a failed compensating-stop diagnostic."""
COMPENSATION_FAILURE_DETAIL_CHARACTERS: Final = 2000
"""The most diagnostic characters retained from a failed compensating stop."""


class CompensationFailureKind(StrEnum):
    """Why cleanup of an undisclosed successful start could not be proved complete."""

    WORKER_START_REFUSED = "worker_start_refused"
    """The helper could not start the worker that owns the compensating stop."""
    STOP_FAILED = "stop_failed"
    """The registry stop operation raised before returning a typed result."""
    STOP_UNCONFIRMED = "stop_unconfirmed"
    """The registry returned a typed result that does not prove terminal cleanup."""


class _RunStartCompensationFailureFields(BaseModel):
    """Fields shared by every failed compensating-stop diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["run.start_compensation_failed"]
    exit_status: Literal[12]
    request_id: RequestId
    detail: Annotated[
        str, StringConstraints(min_length=1, max_length=COMPENSATION_FAILURE_DETAIL_CHARACTERS)
    ]


class WorkerStartRefusedCompensationFailure(_RunStartCompensationFailureFields):
    """The helper could not start the worker that owns the compensating stop."""

    kind: Literal[CompensationFailureKind.WORKER_START_REFUSED]
    instance_id: InstanceId


class StopFailedCompensationFailure(_RunStartCompensationFailureFields):
    """The registry stop raised before returning typed evidence."""

    kind: Literal[CompensationFailureKind.STOP_FAILED]
    instance_id: InstanceId


class UnconfirmedInstanceStopResult(InstanceStopResult):
    """A complete stop result whose outcome does not prove terminal cleanup."""

    outcome: Literal[
        StopOutcome.STOPPING,
        StopOutcome.STILL_RUNNING,
        StopOutcome.UNSUPPORTED,
    ]


class StopUnconfirmedCompensationFailure(_RunStartCompensationFailureFields):
    """The registry returned a typed result that does not prove terminal cleanup.

    The stop result is the one source of the instance identity in this variant,
    so a serialized report cannot carry two identities that disagree.
    """

    kind: Literal[CompensationFailureKind.STOP_UNCONFIRMED]
    stop_result: UnconfirmedInstanceStopResult


type RunStartCompensationEvidence = Annotated[
    WorkerStartRefusedCompensationFailure
    | StopFailedCompensationFailure
    | StopUnconfirmedCompensationFailure,
    Field(discriminator="kind"),
]


class RunStartCompensationFailure(RootModel[RunStartCompensationEvidence]):
    """One strict stderr line preserving an undisclosed instance cleanup failure.

    A successful ``run.start`` reply could not reach the client, so only this
    diagnostic can tell an operator which live instance may need recovery. The
    discriminated variants make result presence and nonterminal outcomes part of
    the generated schema rather than Python-only validation.
    """

    root: RunStartCompensationEvidence

    @property
    def request_id(self) -> str:
        """The start request whose successful reply was undisclosed."""
        return self.root.request_id

    @property
    def instance_id(self) -> str:
        """The undisclosed instance an operator may need to recover."""
        if isinstance(self.root, StopUnconfirmedCompensationFailure):
            return self.root.stop_result.id
        return self.root.instance_id

    @property
    def kind(self) -> CompensationFailureKind:
        """Why terminal cleanup could not be proved."""
        return self.root.kind

    @property
    def stop_result(self) -> UnconfirmedInstanceStopResult | None:
        """The complete nonterminal stop result, when the stop returned one."""
        if isinstance(self.root, StopUnconfirmedCompensationFailure):
            return self.root.stop_result
        return None

    @property
    def exit_status(self) -> Literal[12]:
        """The process status paired with this diagnostic."""
        return self.root.exit_status

    def line(self) -> str:
        """The report as one newline-terminated JSON line."""
        return self.model_dump_json() + "\n"


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
    SERVER_UNRESPONSIVE = "server_unresponsive"
    """Reading from the control socket made no progress within the read deadline: the server is wedged."""


class ChannelEnd(BaseModel):
    """The one stderr line a relayed run channel writes when it ends with a nonzero status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ChannelEndOutcome
    exit_status: int
    detail: str

    def line(self) -> str:
        """The report as one newline-terminated JSON line."""
        return self.model_dump_json() + "\n"
