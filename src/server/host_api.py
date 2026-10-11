"""Light contracts a host reports about its live detached servers.

These are the documents ``vibesys instances`` and ``vibesys --detach`` print,
and the record a detached server publishes in the live registry
(``server.registry``). They import only Pydantic and the standard library, so
a host command that reads or writes them starts in a fraction of the time the
run engine takes to import. ``tests/architecture/test_light_entrypoints.py``
keeps it that way.

Public surface: ``PROTOCOL_VERSION``, ``INSTANCE_ID_PATTERN``, ``InstanceId``,
``InstanceStatus``, ``LiveInstanceRecord``, ``InstanceList``, ``StopOutcome``,
``StopRoute``, ``InstanceStopResult``, ``DetachedLaunchErrorCode``, and
``DetachedLaunchFailure``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints

PROTOCOL_VERSION = 1
"""The run protocol version a server speaks on its control socket.

Defined here, not in ``server.api.protocol``, so a record can name it without
importing the run protocol; the protocol module re-exports it.
"""

INSTANCE_ID_PATTERN = r"^[0-9a-f]{12}$"
InstanceId = Annotated[str, StringConstraints(pattern=INSTANCE_ID_PATTERN)]
"""A registry key: 12 lowercase hex digits, so it is never a path fragment."""


class InstanceStatus(StrEnum):
    """How far a live server has come; run status itself is on its socket."""

    STARTING = "starting"
    """The server holds its lock; its control socket is not accepting yet."""
    SERVING = "serving"
    """The control socket accepts connections."""


class LiveInstanceRecord(BaseModel):
    """What a live detached server publishes about itself (version 1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    id: InstanceId
    status: InstanceStatus
    socket_path: str
    project_root: str
    run_id: str | None = None
    pid: PositiveInt
    started_at: float
    hostname: str
    protocol_version: Literal[1] = PROTOCOL_VERSION
    vibesys_version: str
    vibesys_root: str | None = None
    """Absolute path of the VibeSys source checkout this server runs from.

    ``None`` when the server runs from an installed distribution rather than a
    checkout, and in records written before the field existed (the field is
    additive, so the record stays version 1). A client uses it to suggest the
    checkout it should run ``vibesys`` from on this host.
    """


class InstanceList(BaseModel):
    """``vibesys instances list --json``: the live servers on this node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    instances: tuple[LiveInstanceRecord, ...]
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
