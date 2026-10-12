"""The host helper's requests: answering one request through the host's interfaces.

The channel (``server.host_helper``) admits a request and hands it to a worker,
which calls :func:`answer`, or :func:`connect` for ``run.connect``. Each request
reads the registry, the project, or the filesystem afresh through
:class:`HelperHost`, so answering keeps no state between requests. The pure
decisions it needs (:func:`negotiate`, :func:`select_completions`,
:func:`run_summaries`, :func:`launch_arguments`) are public so they can be
tested on their own.

Light like the channel: the standard library, Pydantic, ``vs_sim.api`` and light
``server`` modules only.

Public surface: ``answer``, ``connect``, ``negotiate``, ``Negotiated``,
``HelperHost``; the interfaces ``Projects``, ``Directories``, ``RunLauncher`` and
``CheckoutProbe`` with the values they exchange (``ProjectRun``,
``ProjectLookupError``, ``ScannedEntry``, ``Scan``, ``PathHints``);
``complete_path``, ``select_completions``, ``split_prefix``, ``run_summaries``,
``launch_arguments``; and the limit ``SCAN_LIMIT``.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, assert_never

from server.host_api import (
    HELPER_PROTOCOL_VERSIONS,
    PATHS_COMPLETE_CAP,
    Checkout,
    DetachedLaunchFailure,
    HelloReply,
    HelloRequest,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    HelperReply,
    HelperRequest,
    InstanceList,
    InstancesInspectReply,
    InstancesInspectRequest,
    InstancesListReply,
    InstancesListRequest,
    InstancesUnwatchRequest,
    InstancesWatchRequest,
    LiveInstanceRecord,
    PathEntry,
    PathsCompleteReply,
    PathsCompleteRequest,
    PingReply,
    PingRequest,
    ResumeLatest,
    ResumeRun,
    RunConnectReply,
    RunConnectRequest,
    RunList,
    RunsListReply,
    RunsListRequest,
    RunStartReply,
    RunStartRequest,
    RunStopReply,
    RunStopRequest,
    RunSummary,
    TaskList,
    TasksListReply,
    TasksListRequest,
)
from server.relay import (
    BridgeResult,
    ChannelEndOutcome,
    dial,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from server.registry import InstanceInspector, LiveRegistry, StopEffects
    from server.relay import Dialer
    from vs_sim.api import Connection

SCAN_LIMIT: Final = 10_000
"""The most entries ``paths.complete`` reads from one directory."""


@dataclass(frozen=True)
class Negotiated:
    """What ``hello`` settled: the protocol version and the features both ends use."""

    version: int
    features: frozenset[HelperFeature]


def negotiate(request: HelloRequest, vibesys_version: str) -> Negotiated | HelperError:
    """The highest version both ends speak and the features both know, or ``incompatible``."""
    low, high = HELPER_PROTOCOL_VERSIONS
    client_low, client_high = request.client_versions
    version = min(high, client_high)
    if client_low > client_high or version < max(low, client_low):
        return HelperError(
            request_id=request.request_id,
            code=HelperErrorCode.INCOMPATIBLE,
            message=(
                f"the client speaks helper protocol {client_low} to {client_high}; "
                f"this helper speaks {low} to {high}"
            ),
            supported_versions=HELPER_PROTOCOL_VERSIONS,
            vibesys_version=vibesys_version,
        )
    known = {feature.value: feature for feature in HelperFeature}
    return Negotiated(
        version=version,
        features=frozenset(known[name] for name in request.features if name in known),
    )


@dataclass(frozen=True)
class ProjectRun:
    """One run a project's durable state records."""

    run_id: str
    task: str | None
    display_name: str
    created_at: datetime


class ProjectLookupError(Exception):
    """A project's tasks or runs could not be read; ``code`` says why and ``path`` where."""

    def __init__(self, code: HelperErrorCode, path: str, message: str) -> None:
        """Name the failure ``code`` for ``path``; ``message`` is for a person."""
        super().__init__(message)
        self.code = code
        self.path = path


class Projects(Protocol):
    """Reads a project's tasks and runs.

    Contract: each method opens the project afresh and raises ``ProjectLookupError``
    with ``project_missing`` (no such directory), ``project_uninitialized`` (no
    ``.vibesys/tasks``; ``tasks`` and ``require_initialized`` only) or
    ``task_invalid`` (what it found does not parse).
    """

    def tasks(self, project: Path) -> TaskList:
        """The project's tasks, ordered by name."""
        ...

    def runs(self, project: Path) -> tuple[Path, tuple[ProjectRun, ...]]:
        """The project's resolved root and its runs, oldest first."""
        ...

    def require_initialized(self, project: Path) -> Path:
        """The project's resolved root, once it is known to define tasks."""
        ...


@dataclass(frozen=True)
class ScannedEntry:
    """One entry of a directory as read."""

    name: str
    is_directory: bool
    """It is a directory, or a link this user can follow to one."""


@dataclass(frozen=True)
class Scan:
    """What one read of a directory found."""

    entries: tuple[ScannedEntry, ...]
    complete: bool
    """False when the directory held more than the read's limit."""


@dataclass(frozen=True)
class PathHints:
    """What a directory looks like to a person picking a project or checkout."""

    vibesys_checkout: bool
    vibesys_project: bool
    git_repository: bool


class Directories(Protocol):
    """Reads directories on the host, with this user's permissions.

    Contract: ``scan`` reads at most ``limit`` entries and raises
    ``FileNotFoundError``, ``NotADirectoryError`` or ``PermissionError``; ``hints``
    never raises.
    """

    def home(self) -> str:
        """This user's home directory."""
        ...

    def expand(self, path: str) -> str:
        """``path`` with a leading ``~`` or ``~user`` expanded."""
        ...

    def scan(self, path: str, limit: int) -> Scan:
        """Up to ``limit`` entries of ``path``."""
        ...

    def hints(self, path: str) -> PathHints:
        """What ``path`` holds; every hint is False when it cannot be read."""
        ...


class RunLauncher(Protocol):
    """Starts a detached run as ``vibesys --detach`` does.

    Contract: returns the started server's record or the launch's typed failure,
    never raises for a failed launch, and settles within the detached launcher's
    bounded startup period.
    """

    def start(
        self, project: Path, arguments: tuple[str, ...]
    ) -> LiveInstanceRecord | DetachedLaunchFailure:
        """Run ``vibesys ARGUMENTS`` in ``project`` and read the one line it prints."""
        ...


class CheckoutProbe(Protocol):
    """Describes the VibeSys checkout this helper runs from."""

    def describe(self) -> Checkout | None:
        """The checkout, or ``None`` for an installed distribution."""
        ...


@dataclass(frozen=True)
class HelperHost:
    """Everything a request reads or changes on the host."""

    registry: LiveRegistry
    inspector: InstanceInspector
    stop_effects: StopEffects
    projects: Projects
    directories: Directories
    launcher: RunLauncher
    checkout: CheckoutProbe
    dialer: Dialer
    vibesys_version: str


def split_prefix(prefix: str, home: str) -> tuple[str, str]:
    """The directory to list and the name prefix to match, for an expanded typed path."""
    path = prefix if posixpath.isabs(prefix) else posixpath.join(home, prefix)
    if prefix == "" or path.endswith("/"):
        return path, ""
    return posixpath.split(path)


def select_completions(
    entries: Iterable[ScannedEntry], partial: str, cap: int
) -> tuple[tuple[str, ...], bool]:
    """The directory names that start with ``partial``, sorted, at most ``cap``; and whether more matched."""
    matches = sorted(
        entry.name for entry in entries if entry.is_directory and entry.name.startswith(partial)
    )
    return tuple(matches[:cap]), len(matches) > cap


def complete_path(
    request: PathsCompleteRequest, directories: Directories
) -> PathsCompleteReply | HelperError:
    """Answer ``paths.complete`` from what ``directories`` reads."""
    directory, partial = split_prefix(directories.expand(request.prefix), directories.home())
    try:
        scan = directories.scan(directory, SCAN_LIMIT)
    except FileNotFoundError:
        return _path_error(request, HelperErrorCode.PATH_MISSING, directory, "does not exist")
    except NotADirectoryError:
        return _path_error(
            request, HelperErrorCode.PATH_NOT_DIRECTORY, directory, "is not a directory"
        )
    except PermissionError:
        return _path_error(
            request, HelperErrorCode.PERMISSION_DENIED, directory, "cannot be listed"
        )
    names, more = select_completions(scan.entries, partial, PATHS_COMPLETE_CAP)
    entries = []
    for name in names:
        path = posixpath.join(directory, name)
        hints = directories.hints(path)
        entries.append(
            PathEntry(
                name=name,
                path=path,
                vibesys_checkout=hints.vibesys_checkout,
                vibesys_project=hints.vibesys_project,
                git_repository=hints.git_repository,
            )
        )
    return PathsCompleteReply(
        request_id=request.request_id,
        directory=directory,
        entries=tuple(entries),
        truncated=more or not scan.complete,
    )


def _path_error(
    request: PathsCompleteRequest, code: HelperErrorCode, path: str, what: str
) -> HelperError:
    return HelperError(
        request_id=request.request_id, code=code, message=f"{path} {what}", path=path
    )


def run_summaries(root: Path, runs: Iterable[ProjectRun], listing: InstanceList) -> RunList:
    """Join a project's runs with this host's registry: each run's server and its phase.

    A live server outranks a tombstone; among tombstones the latest ended wins.
    """
    project_root = str(root)
    live = {
        record.run_id: record for record in listing.instances if record.project_root == project_root
    }
    ended: dict[str, LiveInstanceRecord] = {}
    for record in sorted(listing.ended, key=lambda record: record.ended_at or 0.0):
        if record.project_root == project_root:
            ended[record.run_id] = record
    summaries = []
    for run in runs:
        server = live.get(run.run_id) or ended.get(run.run_id)
        summaries.append(
            RunSummary(
                run_id=run.run_id,
                task=run.task,
                display_name=run.display_name,
                created_at=run.created_at,
                phase=None if server is None else server.phase,
                instance_id=None if run.run_id not in live else live[run.run_id].id,
            )
        )
    return RunList(project_root=project_root, runs=tuple(summaries))


def launch_arguments(request: RunStartRequest) -> tuple[str, ...]:
    """The ``vibesys`` arguments that start ``request`` detached; the resume target comes last."""
    task = ("--task", request.task) if request.task is not None else ()
    match request.resume:
        case None:
            resume: tuple[str, ...] = ()
        case ResumeRun(run_id=run_id):
            resume = ("--resume", run_id)
        case ResumeLatest():
            resume = ("--resume",)
        case _:
            assert_never(request.resume)
    return ("--detach", *task, *request.args, *resume)


def _host_path(text: str, directories: Directories) -> str:
    expanded = directories.expand(text)
    return expanded if posixpath.isabs(expanded) else posixpath.join(directories.home(), expanded)


def answer(request: HelperRequest, host: HelperHost) -> HelperReply | HelperError:
    """Execute one request (other than ``run.connect``, see :func:`connect`) on the host."""
    request_id = request.request_id
    try:
        return _answer(request, host)
    except ProjectLookupError as error:
        return HelperError(
            request_id=request_id, code=error.code, message=str(error), path=error.path
        )


def _answer(request: HelperRequest, host: HelperHost) -> HelperReply | HelperError:
    request_id = request.request_id
    reply: HelperReply | HelperError
    match request:
        case HelloRequest():
            negotiated = negotiate(request, host.vibesys_version)
            reply = (
                negotiated
                if isinstance(negotiated, HelperError)
                else HelloReply(
                    request_id=request_id,
                    protocol_version=negotiated.version,
                    features=tuple(sorted(negotiated.features)),
                    vibesys_version=host.vibesys_version,
                    checkout=host.checkout.describe(),
                )
            )
        case InstancesListRequest() | InstancesInspectRequest():
            reply = _instances_reply(request, host)
        case TasksListRequest(project=project):
            tasks = host.projects.tasks(Path(_host_path(project, host.directories)))
            reply = TasksListReply(request_id=request_id, tasks=tasks)
        case RunsListRequest(project=project):
            root, runs = host.projects.runs(Path(_host_path(project, host.directories)))
            summaries = run_summaries(root, runs, host.registry.list())
            reply = RunsListReply(request_id=request_id, runs=summaries)
        case RunStartRequest(project=project):
            root = host.projects.require_initialized(Path(_host_path(project, host.directories)))
            launch = host.launcher.start(root, launch_arguments(request))
            reply = RunStartReply(request_id=request_id, launch=launch)
        case RunStopRequest(instance_id=instance_id, force=force):
            result = host.registry.stop(instance_id, host.stop_effects, force=force)
            reply = RunStopReply(request_id=request_id, result=result)
        case PingRequest():
            reply = PingReply(request_id=request_id)
        case PathsCompleteRequest():
            reply = complete_path(request, host.directories)
        case InstancesWatchRequest() | InstancesUnwatchRequest() | RunConnectRequest():
            message = f"{request.type} is answered by the channel, not executed"
            reply = _error(request_id, HelperErrorCode.INTERNAL, message)
        case _:
            assert_never(request)
    return reply


def _instances_reply(
    request: InstancesListRequest | InstancesInspectRequest, host: HelperHost
) -> InstancesListReply | InstancesInspectReply:
    """Answer one registry read through the host's shared registry interfaces."""
    match request:
        case InstancesListRequest(request_id=request_id):
            return InstancesListReply(request_id=request_id, listing=host.registry.list())
        case InstancesInspectRequest(
            request_id=request_id, instance_id=instance_id, include_log=include_log
        ):
            inspection = host.inspector.inspect(instance_id, include_log=include_log)
            return InstancesInspectReply(request_id=request_id, inspection=inspection)


_CONNECT_ERRORS: Final[Mapping[ChannelEndOutcome, HelperErrorCode]] = {
    ChannelEndOutcome.RUN_GONE: HelperErrorCode.RUN_GONE,
    ChannelEndOutcome.CONNECT_DENIED: HelperErrorCode.CONNECT_DENIED,
    ChannelEndOutcome.CONNECT_FAILED: HelperErrorCode.CONNECT_FAILED,
}


def connect(
    request: RunConnectRequest, host: HelperHost
) -> tuple[RunConnectReply, Connection] | HelperError:
    """Open the run's control socket for ``run.connect``; the caller owns the connection."""
    record = host.registry.find(request.instance_id)
    if record is None:
        message = f"no live server has id {request.instance_id}"
        return _error(request.request_id, HelperErrorCode.RUN_GONE, message)
    connection = dial(host.dialer, record.socket_path)
    if isinstance(connection, BridgeResult):
        return _error(request.request_id, _CONNECT_ERRORS[connection.outcome], connection.detail)
    reply = RunConnectReply(
        request_id=request.request_id,
        run_id=record.run_id,
        protocol_version=record.protocol_version,
    )
    return reply, connection


def _error(request_id: str | None, code: HelperErrorCode, message: str) -> HelperError:
    return HelperError(request_id=request_id, code=code, message=message)
