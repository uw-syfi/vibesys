"""The host helper: one light program per channel that answers the desktop app's requests.

The desktop app runs ``python -m entrypoints.host_helper`` once per channel, locally
over stdio or remotely over one ssh exec channel. A channel carries typed JSON lines
(``server.host_api``): a ``hello`` first, then requests answered concurrently, each
reply echoing its request's id. A run channel ends with ``run.connect``, after which
it is a byte relay to the run's control socket (``server.stdio_bridge``).
``docs/contributing/desktop-connection.md`` ("The host helper") owns the design.

The module is a pure core and a thin shell:

- :func:`step` folds one observation of a channel (a line arrived, a request was
  answered, a watch observed the registry, time passed, input ended) into
  :class:`ChannelState` and returns the outputs the shell performs: lines to send,
  requests to execute, watches to start or stop, a connection to relay or release.
  It owns the handshake, request-id uniqueness, deadlines, feature gating, and the
  one hand-over of a run channel.
- :func:`answer` executes one request through the host's interfaces
  (:class:`HelperHost`), with the pure decisions it needs (:func:`negotiate`,
  :func:`select_completions`, :func:`run_summaries`, :func:`launch_arguments`).
- :func:`serve_channel` is the shell: one reader of the client's bytes, one worker
  per request, one watcher per watch, and the supervisor that runs :func:`step`.

The helper is stateless across requests: every request reads the registry, the
project, or the filesystem afresh. Like every host command it is light:
``tests/architecture/test_light_entrypoints.py`` holds its imports to the
standard library, Pydantic, ``vs_sim.api``, ``vs_project.api``, and light
``server`` modules.

Public surface: ``step``, ``ChannelState``, ``ChannelEvent`` and its members,
``ChannelOutput`` and its members, ``accepts_input``, ``next_deadline``,
``negotiate``, ``Negotiated``; ``answer``, ``connect``, ``HelperHost``, the
interfaces ``Projects``, ``Directories``, ``RunLauncher``, ``CheckoutProbe`` and the
values they exchange; ``select_completions``, ``split_prefix``, ``run_summaries``,
``launch_arguments``; ``serve_channel``; and the limits ``WATCH_INTERVAL_SECONDS``,
``SCAN_LIMIT``, ``MAX_LINE_BYTES``.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, assert_never

from pydantic import ValidationError

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
    HelperMessage,
    HelperReply,
    HelperRequest,
    InstanceList,
    InstancesChanged,
    InstancesListReply,
    InstancesListRequest,
    InstancesUnwatchReply,
    InstancesUnwatchRequest,
    InstancesWatchReply,
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
    parse_helper_request,
)
from server.stdio_bridge import (
    BridgeOutcome,
    BridgeResult,
    ClientStreams,
    dial,
    relay,
)
from server.stdio_bridge_report import BridgeReport

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from server.registry import LiveRegistry, StopEffects
    from server.stdio_bridge import ByteSink, ByteSource, Dialer
    from vs_sim.api import Connection, Event, Threads

WATCH_INTERVAL_SECONDS: Final = 1.0
"""How often a watch reads the registry; a change is pushed at most this late."""
SCAN_LIMIT: Final = 10_000
"""The most entries ``paths.complete`` reads from one directory."""
MAX_LINE_BYTES: Final = 1 << 20
"""The longest request line; a longer one is read in pieces, each answered ``invalid_request``."""
_READ_BYTES: Final = 64 * 1024
_MAX_ERROR_DETAIL: Final = 2000
_MAX_REQUEST_ID: Final = 128


# ----- the pure core: one channel -----


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
class ChannelState:
    """What one channel knows: the handshake, requests in flight, watches, the hand-over."""

    negotiated: Negotiated | None = None
    pending: Mapping[str, float] = field(default_factory=dict)
    """Requests in flight, by id, with the time their deadline passes."""
    watches: Mapping[str, InstanceList | None] = field(default_factory=dict)
    """Open watches, by id, with the listing last pushed on each."""
    connecting: str | None = None
    """The ``run.connect`` in flight; input waits until it is answered."""
    relaying: bool = False
    """The channel was handed to the relay; nothing else happens on it."""
    input_ended: bool = False


@dataclass(frozen=True)
class Received:
    """One complete line arrived at time ``at``."""

    line: bytes
    at: float


@dataclass(frozen=True)
class Answered:
    """An executed request's reply is ready."""

    request_id: str
    reply: HelperReply | HelperError


@dataclass(frozen=True)
class Observed:
    """A watch read the registry."""

    watch_id: str
    listing: InstanceList


@dataclass(frozen=True)
class Tick:
    """Time has reached ``at``."""

    at: float


@dataclass(frozen=True)
class InputEnded:
    """The client's input reached end of stream."""


type ChannelEvent = Received | Answered | Observed | Tick | InputEnded


@dataclass(frozen=True)
class Send:
    """Write one message line to the client."""

    message: HelperMessage


@dataclass(frozen=True)
class Execute:
    """Run ``request`` through the host and report ``Answered``."""

    request: HelperRequest


@dataclass(frozen=True)
class Watch:
    """Start reporting ``Observed`` for ``watch_id``."""

    watch_id: str


@dataclass(frozen=True)
class Unwatch:
    """Stop reporting for ``watch_id``."""

    watch_id: str


@dataclass(frozen=True)
class Relay:
    """Hand the channel to the relay over the connection ``request_id`` opened."""

    request_id: str


@dataclass(frozen=True)
class Release:
    """Close the connection ``request_id`` opened; its reply came too late."""

    request_id: str


@dataclass(frozen=True)
class Close:
    """The channel is done: input ended and nothing is in flight."""


type ChannelOutput = Send | Execute | Watch | Unwatch | Relay | Release | Close

_GATED: Final[Mapping[type[object], HelperFeature]] = {
    InstancesWatchRequest: HelperFeature.INSTANCES_WATCH,
    InstancesUnwatchRequest: HelperFeature.INSTANCES_WATCH,
    PathsCompleteRequest: HelperFeature.PATHS_COMPLETE,
}


def accepts_input(state: ChannelState) -> bool:
    """Whether the shell should read lines: not while a run channel is being handed over."""
    return state.connecting is None and not state.relaying and not state.input_ended


def next_deadline(state: ChannelState) -> float | None:
    """When the earliest request in flight runs out of time."""
    return min(state.pending.values(), default=None)


def step(
    state: ChannelState, event: ChannelEvent, vibesys_version: str
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Fold one observation into the channel; return the new state and the outputs."""
    if state.relaying:
        return state, ()
    match event:
        case Received(line=line, at=at):
            return _received(state, line, at, vibesys_version)
        case Answered(request_id=request_id, reply=reply):
            return _answered(state, request_id, reply)
        case Observed(watch_id=watch_id, listing=listing):
            return _observed(state, watch_id, listing)
        case Tick(at=at):
            return _tick(state, at)
        case InputEnded():
            outputs: list[ChannelOutput] = [Unwatch(watch_id) for watch_id in state.watches]
            return _maybe_close(replace(state, input_ended=True, watches={}), outputs)
        case _:
            assert_never(event)


def _observed(
    state: ChannelState, watch_id: str, listing: InstanceList
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Push a watch's listing when it is the first or differs from the last one pushed."""
    if watch_id not in state.watches or state.watches[watch_id] == listing:
        return state, ()
    watches = {**state.watches, watch_id: listing}
    pushed = Send(InstancesChanged(watch_id=watch_id, listing=listing))
    return replace(state, watches=watches), (pushed,)


def _tick(state: ChannelState, at: float) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    """Answer ``deadline_exceeded`` for every request whose deadline has passed."""
    expired = sorted(rid for rid, deadline in state.pending.items() if deadline <= at)
    if not expired:
        return state, ()
    pending = {rid: d for rid, d in state.pending.items() if rid not in expired}
    connecting = None if state.connecting in expired else state.connecting
    outputs: list[ChannelOutput] = [
        Send(_error(rid, HelperErrorCode.DEADLINE_EXCEEDED, "no reply within the deadline"))
        for rid in expired
    ]
    return _maybe_close(replace(state, pending=pending, connecting=connecting), outputs)


def _maybe_close(
    state: ChannelState, outputs: list[ChannelOutput]
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    if state.input_ended and not state.pending:
        outputs.append(Close())
    return state, tuple(outputs)


def _error(request_id: str | None, code: HelperErrorCode, message: str) -> HelperError:
    return HelperError(request_id=request_id, code=code, message=message)


def _request_id_of(line: bytes) -> str | None:
    """The ``request_id`` of a line that is not a valid request, when it carries a usable one."""
    try:
        document = json.loads(line)
    except ValueError:
        return None
    value = document.get("request_id") if isinstance(document, dict) else None
    if isinstance(value, str) and 0 < len(value) <= _MAX_REQUEST_ID:
        return value
    return None


def _received(
    state: ChannelState, line: bytes, at: float, vibesys_version: str
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    if not accepts_input(state):
        return state, ()
    try:
        request = parse_helper_request(line)
    except ValidationError as error:
        detail = str(error)[:_MAX_ERROR_DETAIL]
        refused = _error(_request_id_of(line), HelperErrorCode.INVALID_REQUEST, detail)
        return state, (Send(refused),)
    state, refusal = _admission(state, request, vibesys_version)
    if refusal is not None:
        return state, (Send(refusal),)
    return _admit(state, request, at)


def _admission(
    state: ChannelState, request: HelperRequest, vibesys_version: str
) -> tuple[ChannelState, HelperError | None]:
    """Whether the channel takes ``request`` now; a successful ``hello`` negotiates here."""
    request_id = request.request_id
    refusal: HelperError | None = None
    if state.negotiated is None:
        if isinstance(request, HelloRequest):
            negotiated = negotiate(request, vibesys_version)
            if isinstance(negotiated, HelperError):
                refusal = negotiated
            else:
                state = replace(state, negotiated=negotiated)
        else:
            message = "the first request on a channel must be hello"
            refusal = _error(request_id, HelperErrorCode.HANDSHAKE_REQUIRED, message)
    elif isinstance(request, HelloRequest):
        message = "hello was already negotiated on this channel"
        refusal = _error(request_id, HelperErrorCode.INCOMPATIBLE, message)
    elif request_id in state.pending or request_id in state.watches:
        message = f"request id {request_id!r} is already open on this channel"
        refusal = _error(request_id, HelperErrorCode.DUPLICATE_REQUEST_ID, message)
    else:
        feature = _GATED.get(type(request))
        if feature is not None and feature not in state.negotiated.features:
            message = (
                f"{request.type} needs the {feature.value} feature, which hello did not return"
            )
            refusal = _error(request_id, HelperErrorCode.FEATURE_NOT_NEGOTIATED, message)
    return state, refusal


def _admit(
    state: ChannelState, request: HelperRequest, at: float
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    request_id = request.request_id
    match request:
        case InstancesWatchRequest():
            watches = {**state.watches, request_id: None}
            return replace(state, watches=watches), (
                Send(InstancesWatchReply(request_id=request_id, watch_id=request_id)),
                Watch(request_id),
            )
        case InstancesUnwatchRequest(watch_id=watch_id):
            if watch_id not in state.watches:
                message = f"no watch {watch_id!r} is open on this channel"
                return state, (Send(_error(request_id, HelperErrorCode.UNKNOWN_WATCH, message)),)
            watches = {wid: last for wid, last in state.watches.items() if wid != watch_id}
            return replace(state, watches=watches), (
                Unwatch(watch_id),
                Send(InstancesUnwatchReply(request_id=request_id, watch_id=watch_id)),
            )
        case RunConnectRequest():
            if state.pending or state.watches:
                message = "run.connect needs a channel with no other request or watch open"
                return state, (Send(_error(request_id, HelperErrorCode.CHANNEL_BUSY, message)),)
            state = replace(state, connecting=request_id)
        case _:
            pass
    pending = {**state.pending, request_id: at + request.deadline_ms / 1000}
    return replace(state, pending=pending), (Execute(request),)


def _answered(
    state: ChannelState, request_id: str, reply: HelperReply | HelperError
) -> tuple[ChannelState, tuple[ChannelOutput, ...]]:
    if request_id not in state.pending:
        # Its deadline already answered for it; a connection it opened has no taker.
        return state, ((Release(request_id),) if isinstance(reply, RunConnectReply) else ())
    pending = {rid: d for rid, d in state.pending.items() if rid != request_id}
    state = replace(state, pending=pending)
    outputs: list[ChannelOutput] = [Send(reply)]
    if request_id == state.connecting:
        if isinstance(reply, RunConnectReply):
            return replace(state, connecting=None, relaying=True), (*outputs, Relay(request_id))
        state = replace(state, connecting=None)
    return _maybe_close(state, outputs)


# ----- executing one request -----


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
    never raises for a failed launch.
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
        case InstancesListRequest():
            reply = InstancesListReply(request_id=request_id, listing=host.registry.list())
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


_CONNECT_ERRORS: Final[Mapping[BridgeOutcome, HelperErrorCode]] = {
    BridgeOutcome.RUN_GONE: HelperErrorCode.RUN_GONE,
    BridgeOutcome.CONNECT_DENIED: HelperErrorCode.CONNECT_DENIED,
    BridgeOutcome.CONNECT_FAILED: HelperErrorCode.CONNECT_FAILED,
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


# ----- the shell -----


class _Inbox:
    """The client's bytes, read by one pump and taken by lines or, once relaying, as bytes."""

    def __init__(self, threads: Threads) -> None:
        self.cond = threads.condition()
        self.buffer = bytearray()
        self.ended = False

    def pump(self, source: ByteSource) -> None:
        """Read the client until end of stream, holding at most ``MAX_LINE_BYTES`` unread."""
        while True:
            with self.cond:
                self.cond.wait_for(lambda: len(self.buffer) < MAX_LINE_BYTES)
            try:
                chunk = source.read(_READ_BYTES)
            except OSError:
                chunk = b""
            with self.cond:
                if chunk:
                    self.buffer += chunk
                else:
                    self.ended = True
                self.cond.notify_all()
            if not chunk:
                return

    def take_line(self) -> bytes | None:
        """The next complete line, or an oversized prefix; call with ``cond`` held."""
        end = self.buffer.find(b"\n")
        if end < 0:
            if len(self.buffer) < MAX_LINE_BYTES:
                return None
            end = len(self.buffer) - 1
        line = bytes(self.buffer[:end])
        del self.buffer[: end + 1]
        self.cond.notify_all()
        return line


class _InboxSource:
    """The relay's view of the client: what the inbox holds, then what the pump reads."""

    def __init__(self, inbox: _Inbox) -> None:
        self._inbox = inbox

    def read(self, max_bytes: int) -> bytes:
        inbox = self._inbox
        with inbox.cond:
            inbox.cond.wait_for(lambda: bool(inbox.buffer) or inbox.ended)
            chunk = bytes(inbox.buffer[:max_bytes])
            del inbox.buffer[:max_bytes]
            inbox.cond.notify_all()
            return chunk


class _Channel:
    def __init__(self, host: HelperHost, inbox: _Inbox, output: ByteSink, threads: Threads) -> None:
        self._host = host
        self._inbox = inbox
        self._output = output
        self._threads = threads
        self._state = ChannelState()
        self._events: list[ChannelEvent] = []
        self._held: dict[str, Connection] = {}
        self._watches: dict[str, Event] = {}
        self._handed_over: str | None = None
        self._closed = False

    def post(self, event: ChannelEvent) -> None:
        with self._inbox.cond:
            self._events.append(event)
            self._inbox.cond.notify_all()

    def supervise(self) -> Connection | None:
        """Run the channel until it closes (``None``) or is handed to the relay."""
        cond = self._inbox.cond
        with cond:
            while True:
                self._take_input()
                while self._events and not (self._closed or self._handed_over):
                    self._apply(self._events.pop(0))
                if self._closed:
                    return None
                if self._handed_over is not None:
                    return self._held.pop(self._handed_over)
                deadline = next_deadline(self._state)
                if not self._events and not self._input_ready():
                    timeout = None if deadline is None else max(deadline - self._threads.now(), 0.0)
                    cond.wait(timeout)
                self._events.append(Tick(self._threads.now()))

    def _input_ready(self) -> bool:
        inbox = self._inbox
        if not accepts_input(self._state):
            return False
        return b"\n" in inbox.buffer or len(inbox.buffer) >= MAX_LINE_BYTES or inbox.ended

    def _take_input(self) -> None:
        while accepts_input(self._state):
            line = self._inbox.take_line()
            if line is None:
                if self._inbox.ended:
                    self._apply(InputEnded())
                return
            self._apply(Received(line, self._threads.now()))

    def _apply(self, event: ChannelEvent) -> None:
        self._state, outputs = step(self._state, event, self._host.vibesys_version)
        for output in outputs:
            self._perform(output)

    def _perform(self, output: ChannelOutput) -> None:
        match output:
            case Send(message=message):
                try:
                    self._output.write(message.model_dump_json().encode() + b"\n")
                except OSError:
                    # The client stopped reading: nobody is left to answer.
                    self._closed = True
            case Execute(request=request):
                self._threads.spawn(lambda: self._execute(request), name=f"helper-{request.type}")
            case Watch(watch_id=watch_id):
                stop = self._threads.event()
                self._watches[watch_id] = stop
                self._threads.spawn(lambda: self._watch(watch_id, stop), name="helper-watch")
            case Unwatch(watch_id=watch_id):
                self._watches.pop(watch_id).set()
            case Relay(request_id=request_id):
                self._handed_over = request_id
            case Release(request_id=request_id):
                self._held.pop(request_id).close()
            case Close():
                self._closed = True
            case _:
                assert_never(output)

    def _execute(self, request: HelperRequest) -> None:
        try:
            if isinstance(request, RunConnectRequest):
                opened = connect(request, self._host)
                if isinstance(opened, HelperError):
                    reply: HelperReply | HelperError = opened
                else:
                    reply, connection = opened
                    with self._inbox.cond:
                        self._held[request.request_id] = connection
            else:
                reply = answer(request, self._host)
        # lint-waiver: LW-188705 [BLE001]; one request's failure is its own typed reply.
        # > Letting it escape kills the worker silently, and the client waits out its whole
        # > deadline for a `deadline_exceeded` that hides the cause; catching named types
        # > instead would miss exactly the unexpected failures this exists to report.
        except Exception as error:  # noqa: BLE001
            reply = _error(
                request.request_id, HelperErrorCode.INTERNAL, f"{type(error).__name__}: {error}"
            )
        self.post(Answered(request.request_id, reply))

    def _watch(self, watch_id: str, stop: Event) -> None:
        while not stop.is_set():
            self.post(Observed(watch_id, self._host.registry.list()))
            stop.wait(WATCH_INTERVAL_SECONDS)


def serve_channel(
    *,
    host: HelperHost,
    source: ByteSource,
    output: ByteSink,
    errors: ByteSink,
    threads: Threads,
) -> int:
    """Serve one channel until its input ends or it is relayed; the process exit status.

    ``0`` when the client ended a control channel; a relayed channel ends with the
    relay's status and, when that is not ``0``, one ``BridgeReport`` line on
    ``errors``. Workers and the input pump may still be blocked when this returns;
    the caller ends the process.
    """
    inbox = _Inbox(threads)
    threads.spawn(lambda: inbox.pump(source), name="helper-input")
    channel = _Channel(host, inbox, output, threads)
    connection = channel.supervise()
    if connection is None:
        return 0
    result = relay(connection, client=ClientStreams(_InboxSource(inbox), output), threads=threads)
    if result.outcome is not BridgeOutcome.CLIENT_CLOSED:
        errors.write(BridgeReport.of(result).line().encode())
    return result.exit_status
