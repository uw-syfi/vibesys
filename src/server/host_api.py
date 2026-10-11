"""Light contracts a host reports about its live detached servers.

These are the documents ``vibesys instances`` and ``vibesys --detach`` print,
and the record a detached server publishes in the live registry
(``server.registry``). They import only Pydantic and the standard library, so
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
``StartupStage``, ``FailureReason``, ``RunPhase``, ``InstanceStatus``,
``LiveInstanceRecord``, ``UnreadableRecord``, ``parse_record``,
``InstanceList``, ``StopOutcome``, ``StopRoute``, ``InstanceStopResult``,
``DetachedLaunchErrorCode``, and ``DetachedLaunchFailure``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    StringConstraints,
    ValidationError,
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

RECORD_VERSION = 3
"""The registry record version this checkout writes and reads."""


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
    """Where a starting run is, in the order it gets there."""

    RESOLVING = "resolving"
    """The server validates the request and its configuration."""


class FailureReason(BaseModel):
    """Why a run failed: the diagnostic it ended with."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    stage: str
    message: str


class RunPhase(BaseModel):
    """A run's lifecycle status with the detail its status needs.

    ``stage`` says where a ``starting`` run is; ``reason`` says why a
    ``failed`` one ended. Neither is present for any other status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RunStatus
    stage: StartupStage | None = None
    reason: FailureReason | None = None

    @model_validator(mode="after")
    def _detail_matches_status(self) -> RunPhase:
        if (self.stage is not None) != (self.status is RunStatus.STARTING):
            message = "stage is present exactly when status is starting"
            raise ValueError(message)
        if (self.reason is not None) != (self.status is RunStatus.FAILED):
            message = "reason is present exactly when status is failed"
            raise ValueError(message)
        return self

    def advance(
        self, *, status: RunStatus | None = None, failure: FailureReason | None = None
    ) -> RunPhase:
        """Return the phase after one observed fact: a status change or a failure.

        The fold is monotone: an ended phase absorbs every fact, and ``failed``
        is entered only with its reason, from the failure's diagnostic, so a
        status change to ``failed`` waits for the event that says why.
        ``starting`` keeps its stage until the run leaves it. The statuses
        themselves move by ``server.run_lifecycle``'s rules; this projects them.
        """
        if self.status.has_ended:
            return self
        if failure is not None:
            return RunPhase(status=RunStatus.FAILED, reason=failure)
        if status is None or status in {RunStatus.STARTING, RunStatus.FAILED, self.status}:
            return self
        return RunPhase(status=status)


class InstanceStatus(StrEnum):
    """How far a live server has come; run status itself is on its socket."""

    STARTING = "starting"
    """The server holds its lock; its control socket is not accepting yet."""
    SERVING = "serving"
    """The control socket accepts connections."""


class LiveInstanceRecord(BaseModel):
    """What a detached server publishes about itself (version 3).

    ``run_id`` is allocated by the launcher before the server starts, so the
    record names its run from the first time it is published. ``phase`` is the
    run's lifecycle as the server folds it from its journal. When the server
    exits it leaves its last record, with an ended phase and ``ended_at``, as a
    tombstone the registry keeps for a day.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[3] = RECORD_VERSION
    id: InstanceId
    status: InstanceStatus
    phase: RunPhase
    socket_path: str
    log_path: str
    project_root: str
    run_id: RunId
    pid: PositiveInt
    started_at: float
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


def parse_record(instance_id: str, raw: str) -> LiveInstanceRecord | UnreadableRecord:
    """Parse one stored record, strictly, if it is of the version this reader knows.

    Records of earlier versions predate phases and tombstones and are not
    upgraded: their servers report no phase this reader could stand behind.
    """
    try:
        document = json.loads(raw)
    except ValueError:
        return UnreadableRecord(id=instance_id, version=None)
    version = document.get("version") if isinstance(document, dict) else None
    if not isinstance(version, int) or isinstance(version, bool):
        return UnreadableRecord(id=instance_id, version=None)
    try:
        record = LiveInstanceRecord.model_validate(document) if version == RECORD_VERSION else None
    except ValidationError:
        record = None
    if record is None or record.id != instance_id:
        return UnreadableRecord(id=instance_id, version=version)
    return record


class InstanceList(BaseModel):
    """``vibesys instances list --json``: the detached servers on this node (version 3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[3] = 3
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
