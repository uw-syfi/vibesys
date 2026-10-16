"""The host helper's requests: answering one request through the host's interfaces.

The channel (``server.host_helper``) admits a request and hands it to a worker,
which calls :func:`answer`, or :func:`connect` for ``run.connect``. Each request
reads the registry, the project, or the filesystem afresh through
:class:`HelperHost`, so answering keeps no state between requests. The pure
decisions it needs (:func:`negotiate`, :func:`select_completions`,
:func:`run_summaries`, :func:`event_page`, :func:`launch_arguments`,
:func:`refused_argument`, :func:`unvetted_harness`) are public so they can be tested on their own.

Light like the channel: the standard library, Pydantic, ``vs_sim.api`` and light
``server`` modules only.

Public surface: ``answer``, ``connect``, ``negotiate``, ``Negotiated``,
``HelperHost``; the interfaces ``Projects``, ``Directories``, ``RunLauncher`` and
``CheckoutProbe`` with the values they exchange (``ProjectRun``,
``ProjectLookupError``, ``ScannedEntry``, ``Scan``, ``PathHints``);
``complete_path``, ``select_completions``, ``split_prefix``, ``run_summaries``,
``event_page``, ``EventPage``,
``launch_arguments``, ``refused_argument``, ``unvetted_harness``; and the limit
``SCAN_LIMIT``.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol, assert_never

from pydantic import TypeAdapter

from server.host_api import (
    CLI_PROVIDER_FLAG,
    HELPER_LINE_MAX_BYTES,
    HELPER_PROTOCOL_VERSIONS,
    MODEL_FLAG,
    PATHS_COMPLETE_CAP,
    UNVETTED_MODEL_FLAG,
    Checkout,
    CheckoutInspection,
    CheckoutInspectReply,
    CheckoutInspectRequest,
    DetachedLaunchFailure,
    DockerReachable,
    DockerUnreachable,
    ExamplesListReply,
    ExamplesListRequest,
    Hardware,
    HarnessesListReply,
    HarnessesListRequest,
    HarnessOption,
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
    LogsCancelRequest,
    LogsListRequest,
    LogsReadRequest,
    MachineCheckout,
    MachineDescribeReply,
    MachineDescribeRequest,
    MachineDescription,
    PathEntry,
    PathsCompleteReply,
    PathsCompleteRequest,
    PingReply,
    PingRequest,
    ProjectCopyOutReply,
    ProjectCopyOutRequest,
    ProjectCreateFromExampleReply,
    ProjectCreateFromExampleRequest,
    ResumeLatest,
    ResumeRun,
    RunConnectReply,
    RunConnectRequest,
    RunCounts,
    RunEventDocument,
    RunList,
    RunPreflightReply,
    RunPreflightRequest,
    RunsListReply,
    RunsListRequest,
    RunsReadEventsReply,
    RunsReadEventsRequest,
    RunStartReply,
    RunStartRequest,
    RunStopReply,
    RunStopRequest,
    RunSummary,
    TaskList,
    TasksListReply,
    TasksListRequest,
)
from server.host_helper_logs import HelperLogs, ReadWithdrawal, list_logs, read_log
from server.host_helper_preflight import (
    PreflightFacts,
    abbreviates,
    option_arguments,
    preflight_checks,
    repeated_option,
    unvetted_role,
)
from server.relay import (
    BridgeResult,
    ChannelEndOutcome,
    dial,
)
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from datetime import tzinfo

    from server.host_examples import CreatedProject, ExampleList
    from server.host_preflight import PreflightReport
    from server.registry import InstanceInspector, LiveRegistry, StopEffects
    from server.relay import Dialer
    from vs_sim.api import Clock, Connection

SCAN_LIMIT: Final = 10_000
"""The most entries ``paths.complete`` reads from one directory."""


@dataclass(frozen=True)
class Negotiated:
    """What ``hello`` settled: the protocol version and the features both ends use."""

    version: int
    features: frozenset[HelperFeature]


def negotiate(
    request: HelloRequest, vibesys_version: str, offered: frozenset[HelperFeature]
) -> Negotiated | HelperError:
    """The highest version both ends speak and the ``offered`` features the client asked for.

    Returns ``incompatible`` when no version is shared.
    """
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
    known = {feature.value: feature for feature in offered}
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

    def run_events(self, project: Path, run_id: str, from_sequence: int, limit: int) -> EventPage:
        """A page of run ``run_id``'s journal (``event_page`` under ``RUN_EVENTS_READ_MAX_BYTES``).

        A run that journaled nothing has an empty, complete page. Raises
        ``ProjectLookupError`` with ``unknown_run`` when the project has no such run.
        """
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


class MachineProbe(Protocol):
    """Reads what this machine offers runs, and what a path on it holds.

    Contract: no method raises; a fact that cannot be read is reported as absent
    (no GPU, Docker unreachable with the reason, no CLI, ``not_found``).
    """

    def hardware(self) -> Hardware:
        """The machine's CPU cores and GPUs, and how those GPUs run work."""
        ...

    def docker(self) -> DockerReachable | DockerUnreachable:
        """Whether Docker's daemon answers, with its version or why not."""
        ...

    def harness_clis(self) -> tuple[str, ...]:
        """The vetted harness providers whose CLI is installed, in offer order."""
        ...

    def inspect_checkout(self, path: Path) -> CheckoutInspection:
        """Whether ``path`` holds a VibeSys checkout this app can use."""
        ...

    def repository_root(self, path: Path) -> Path | None:
        """The root of the Git repository ``path`` is in, or ``None`` when it is in none."""
        ...


class ExampleProjects(Protocol):
    """The example catalog of the checkout this helper runs from, and new projects.

    Contract: ``create`` and ``copy_out`` leave nothing at ``destination`` when they fail,
    and every failure is ``ProjectLookupError`` naming its path: ``examples_unavailable``,
    ``unknown_example``, ``destination_exists``, ``destination_nested``, ``path_missing``,
    ``not_nested`` or ``project_create_failed``.
    """

    def list(self) -> ExampleList:
        """Every catalogued example with its tasks."""
        ...

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into its own new Git repository at ``destination``."""
        ...

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source``'s tracked files into their own new Git repository at ``destination``."""
        ...


class RunPreflight(Protocol):
    """Runs the run's own preflight (``vibesys preflight``) in a project.

    Contract: never raises; a preflight that prints no report is ``preflight_unavailable``.
    """

    def report(self, project: Path, arguments: tuple[str, ...]) -> PreflightReport:
        """What ``vibesys preflight ARGUMENTS`` reports, run in ``project``."""
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
    harnesses: Callable[[], tuple[HarnessOption, ...]]
    """The vetted provider and model pairs, read when a request needs them: the list
    lives with the agent library, which the helper does not load to answer ``hello``."""
    logs: HelperLogs | None
    """The run log store; ``None`` on a host that has none, which then does not offer ``logs``."""
    machine: MachineProbe
    preflight: RunPreflight
    examples: ExampleProjects
    clock: Clock
    """Wall-clock epoch seconds, with ``timezone``: when ``machine.describe``'s day began."""
    timezone: tzinfo

    def offered_features(self) -> frozenset[HelperFeature]:
        """The features this host can serve: every one, less ``logs`` without a log store."""
        absent = {HelperFeature.LOGS} if self.logs is None else set()
        return frozenset(HelperFeature) - absent


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


@dataclass(frozen=True)
class EventPage:
    """One page of recorded events and where the next page starts."""

    events: tuple[RunEventDocument, ...]
    next_sequence: int
    complete: bool


_EVENT_DOCUMENT: TypeAdapter[RunEventDocument] = TypeAdapter(RunEventDocument)
_SEQUENCE_PREFIX = re.compile(rb'"sequence":(\d+)[,}]')
_SEQUENCE_PREFIX_BYTES = 96


def event_page(lines: Iterable[bytes], from_sequence: int, limit: int, max_bytes: int) -> EventPage:
    """The journal events in ``lines`` from ``from_sequence`` on: at most ``limit`` and ``max_bytes``.

    A line before ``from_sequence`` is skipped by the sequence near its start, without
    decoding the rest. A line that is not a JSON object with an integer ``sequence``
    ends the journal there: it is the tail a live writer has not finished, or damage no
    later read gets past. An event longer than ``max_bytes`` is still returned when it
    is the page's first, so every read makes progress.
    """
    events: list[RunEventDocument] = []
    size = 0
    next_sequence = from_sequence
    for line in lines:
        if not line.strip():
            continue
        skipped = _sequence_prefix(line)
        if skipped is not None and skipped < from_sequence:
            continue
        event = _journal_event(line)
        if event is None:
            break
        sequence = event["sequence"]
        if not isinstance(sequence, int) or sequence < from_sequence:
            continue
        if len(events) == limit or (events and size + len(line) > max_bytes):
            return EventPage(tuple(events), next_sequence, complete=False)
        events.append(event)
        size += len(line)
        next_sequence = sequence + 1
    return EventPage(tuple(events), next_sequence, complete=True)


def _sequence_prefix(line: bytes) -> int | None:
    match = _SEQUENCE_PREFIX.search(line, 0, _SEQUENCE_PREFIX_BYTES)
    return None if match is None else int(match.group(1))


def _journal_event(line: bytes) -> RunEventDocument | None:
    event = decode_json(line, _EVENT_DOCUMENT, source="run event", max_bytes=HELPER_LINE_MAX_BYTES)
    if isinstance(event, Unreadable) or type(event.get("sequence")) is not int:
        return None
    return event


def refused_argument(request: RunStartRequest | RunPreflightRequest) -> str | None:
    """Why ``request``'s ``args`` cannot start a run, or ``None`` when they can.

    A run started through the helper never opts out of the vetted harness list,
    with ``harness`` its provider and model come from nowhere else, and with
    ``options`` no typed option is repeated as a flag.
    """
    refused: tuple[str, ...] = (UNVETTED_MODEL_FLAG,)
    if request.harness is not None:
        refused = (*refused, CLI_PROVIDER_FLAG, MODEL_FLAG)
    for token in request.args:
        for flag in refused:
            if abbreviates(token, flag):
                if flag == UNVETTED_MODEL_FLAG:
                    return f"{token} selects {flag}; runs started here use only vetted harnesses"
                return f"{token} selects {flag}, which the chosen harness already sets"
    return repeated_option(request.args, request.options)


def unvetted_harness(
    request: RunStartRequest | RunPreflightRequest, harnesses: tuple[HarnessOption, ...]
) -> HelperError | None:
    """``unvetted_harness`` naming the allowed models: for a harness not offered, or a role model.

    A role's model must be one of the chosen harness's vetted models.
    """
    role = unvetted_role(request.options, request.harness, harnesses)
    if role is not None:
        return _error(request.request_id, HelperErrorCode.UNVETTED_HARNESS, role)
    choice = request.harness
    if choice is None or any(
        (option.provider, option.model) == (choice.provider, choice.model) for option in harnesses
    ):
        return None
    allowed = [option.model for option in harnesses if option.provider == choice.provider]
    message = (
        f"model {choice.model!r} is not vetted for provider {choice.provider!r}; "
        f"vetted models: {', '.join(allowed) if allowed else 'none'}"
    )
    return _error(request.request_id, HelperErrorCode.UNVETTED_HARNESS, message)


def run_arguments(request: RunStartRequest | RunPreflightRequest) -> tuple[str, ...]:
    """The ``vibesys`` run arguments ``request`` names: task, harness, typed options, then ``args``."""
    task = ("--task", request.task) if request.task is not None else ()
    harness = (
        ()
        if request.harness is None
        else (CLI_PROVIDER_FLAG, request.harness.provider, MODEL_FLAG, request.harness.model)
    )
    return (*task, *harness, *option_arguments(request.options), *request.args)


def launch_arguments(request: RunStartRequest) -> tuple[str, ...]:
    """The ``vibesys`` arguments that start ``request`` detached; the resume target comes last."""
    match request.resume:
        case None:
            resume: tuple[str, ...] = ()
        case ResumeRun(run_id=run_id):
            resume = ("--resume", run_id)
        case ResumeLatest():
            resume = ("--resume",)
        case _:
            assert_never(request.resume)
    return ("--detach", *run_arguments(request), *resume)


def _host_path(text: str, directories: Directories) -> str:
    expanded = directories.expand(text)
    return expanded if posixpath.isabs(expanded) else posixpath.join(directories.home(), expanded)


def answer(
    request: HelperRequest, host: HelperHost, withdrawal: ReadWithdrawal
) -> HelperReply | HelperError:
    """Execute one request (other than ``run.connect``, see :func:`connect`) on the host.

    ``withdrawal`` is how a waiting ``logs.read`` learns that ``logs.cancel`` withdrew it.
    """
    request_id = request.request_id
    try:
        return _answer(request, host, withdrawal)
    except ProjectLookupError as error:
        return HelperError(
            request_id=request_id, code=error.code, message=str(error), path=error.path
        )


def _answer(
    request: HelperRequest, host: HelperHost, withdrawal: ReadWithdrawal
) -> HelperReply | HelperError:
    request_id = request.request_id
    reply: HelperReply | HelperError
    match request:
        case HelloRequest():
            negotiated = negotiate(request, host.vibesys_version, host.offered_features())
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
        case TasksListRequest() | RunsListRequest():
            reply = _project_reply(request, host)
        case RunsReadEventsRequest():
            reply = _run_events_reply(request, host)
        case (
            MachineDescribeRequest()
            | CheckoutInspectRequest()
            | ExamplesListRequest()
            | ProjectCreateFromExampleRequest()
            | ProjectCopyOutRequest()
        ):
            reply = _machine_reply(request, host)
        case RunStartRequest() | RunPreflightRequest():
            reply = (
                _start(request, host)
                if isinstance(request, RunStartRequest)
                else _preflight(request, host)
            )
        case RunStopRequest(instance_id=instance_id, force=force):
            result = host.registry.stop(instance_id, host.stop_effects, force=force)
            reply = RunStopReply(request_id=request_id, result=result)
        case (
            PingRequest()
            | PathsCompleteRequest()
            | HarnessesListRequest()
            | LogsListRequest()
            | LogsReadRequest()
        ):
            reply = _host_read_reply(request, host, withdrawal)
        case (
            InstancesWatchRequest()
            | InstancesUnwatchRequest()
            | LogsCancelRequest()
            | RunConnectRequest()
        ):
            message = f"{request.type} is answered by the channel, not executed"
            reply = _error(request_id, HelperErrorCode.INTERNAL, message)
        case _:
            assert_never(request)
    return reply


def _project_reply(
    request: TasksListRequest | RunsListRequest, host: HelperHost
) -> TasksListReply | RunsListReply:
    """Answer one read of a project's tasks or runs."""
    project = Path(_host_path(request.project, host.directories))
    match request:
        case TasksListRequest(request_id=request_id):
            return TasksListReply(request_id=request_id, tasks=host.projects.tasks(project))
        case RunsListRequest(request_id=request_id):
            root, runs = host.projects.runs(project)
            summaries = run_summaries(root, runs, host.registry.list())
            return RunsListReply(request_id=request_id, runs=summaries)


def _examples_reply(
    request: ExamplesListRequest | ProjectCreateFromExampleRequest | ProjectCopyOutRequest,
    host: HelperHost,
) -> ExamplesListReply | ProjectCreateFromExampleReply | ProjectCopyOutReply:
    """Answer one read of the example catalog, or make one new project."""
    match request:
        case ExamplesListRequest(request_id=request_id):
            return ExamplesListReply(request_id=request_id, listing=host.examples.list())
        case ProjectCreateFromExampleRequest(
            request_id=request_id, example=example, destination=destination
        ):
            created = host.examples.create(example, Path(_host_path(destination, host.directories)))
            return ProjectCreateFromExampleReply(request_id=request_id, created=created)
        case ProjectCopyOutRequest(request_id=request_id, source=source, destination=destination):
            created = host.examples.copy_out(
                Path(_host_path(source, host.directories)),
                Path(_host_path(destination, host.directories)),
            )
            return ProjectCopyOutReply(request_id=request_id, created=created)


def _machine_reply(
    request: MachineDescribeRequest
    | CheckoutInspectRequest
    | ExamplesListRequest
    | ProjectCreateFromExampleRequest
    | ProjectCopyOutRequest,
    host: HelperHost,
) -> HelperReply:
    """Answer what this machine offers, what a path on it holds, or make a project on it."""
    match request:
        case ExamplesListRequest() | ProjectCreateFromExampleRequest() | ProjectCopyOutRequest():
            return _examples_reply(request, host)
        case MachineDescribeRequest(request_id=request_id):
            return MachineDescribeReply(request_id=request_id, machine=describe_machine(host))
        case CheckoutInspectRequest(request_id=request_id, path=path):
            inspection = host.machine.inspect_checkout(Path(_host_path(path, host.directories)))
            return CheckoutInspectReply(request_id=request_id, inspection=inspection)


def describe_machine(host: HelperHost) -> MachineDescription:
    """What this machine offers runs: its checkout, hardware, Docker, harness CLIs and runs."""
    return MachineDescription(
        checkout=_machine_checkout(host),
        hardware=host.machine.hardware(),
        docker=host.machine.docker(),
        harness_clis=host.machine.harness_clis(),
        runs=run_counts(host.registry.read(), host.clock.now(), host.timezone),
    )


def _machine_checkout(host: HelperHost) -> MachineCheckout | None:
    """The helper's own checkout with its version; ``None`` from an installed distribution."""
    checkout = host.checkout.describe()
    if checkout is None:
        return None
    return MachineCheckout(
        root=checkout.root,
        version=host.vibesys_version,
        commit=checkout.commit,
        dirty=checkout.dirty,
    )


def run_counts(listing: InstanceList, now: float, timezone: tzinfo) -> RunCounts:
    """Live runs, and runs ended since local midnight in ``timezone`` at ``now``.

    A crashed server not yet buried has no end time; it was found dead now, so it counts.
    """
    midnight = (
        datetime.fromtimestamp(now, timezone)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    ended_today = sum(
        1 for record in listing.ended if record.ended_at is None or record.ended_at >= midnight
    )
    return RunCounts(live=len(listing.instances), ended_today=ended_today)


def _run_events_reply(request: RunsReadEventsRequest, host: HelperHost) -> RunsReadEventsReply:
    """Answer one page of a run's recorded events."""
    project = Path(_host_path(request.project, host.directories))
    page = host.projects.run_events(project, request.run_id, request.from_sequence, request.max)
    return RunsReadEventsReply(
        request_id=request.request_id,
        events=page.events,
        next_sequence=page.next_sequence,
        complete=page.complete,
    )


def _start(request: RunStartRequest, host: HelperHost) -> RunStartReply | HelperError:
    """Launch ``request`` once its arguments and harness are acceptable."""
    refusal = _refusal(request, host)
    if refusal is not None:
        return refusal
    root = host.projects.require_initialized(Path(_host_path(request.project, host.directories)))
    launch = host.launcher.start(root, launch_arguments(request))
    return RunStartReply(request_id=request.request_id, launch=launch)


def _preflight(request: RunPreflightRequest, host: HelperHost) -> RunPreflightReply | HelperError:
    """Check whether the run ``run.start`` would launch with these inputs can start."""
    refusal = _refusal(request, host)
    if refusal is not None:
        return refusal
    root = host.projects.require_initialized(Path(_host_path(request.project, host.directories)))
    facts = PreflightFacts(
        project_root=root,
        repository_root=host.machine.repository_root(root),
        checkout=_machine_checkout(host),
        vibesys_version=host.vibesys_version,
        docker=host.machine.docker(),
    )
    report = host.preflight.report(root, run_arguments(request))
    return RunPreflightReply(request_id=request.request_id, checks=preflight_checks(facts, report))


def _refusal(
    request: RunStartRequest | RunPreflightRequest, host: HelperHost
) -> HelperError | None:
    """Why ``request``'s arguments, harness or role models cannot start a run, if they cannot."""
    refusal = refused_argument(request)
    if refusal is not None:
        return _error(request.request_id, HelperErrorCode.INVALID_REQUEST, refusal)
    if request.harness is None and request.options is None:
        return None
    return unvetted_harness(request, host.harnesses())


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


def _host_read_reply(
    request: PingRequest
    | PathsCompleteRequest
    | HarnessesListRequest
    | LogsListRequest
    | LogsReadRequest,
    host: HelperHost,
    withdrawal: ReadWithdrawal,
) -> HelperReply | HelperError:
    """Answer a request that only reads the channel or the host.

    The channel admits a ``logs`` request only when ``hello`` offered the feature,
    which it does only for a host with a log store.
    """
    match request:
        case PingRequest(request_id=request_id):
            return PingReply(request_id=request_id)
        case PathsCompleteRequest():
            return complete_path(request, host.directories)
        case HarnessesListRequest(request_id=request_id):
            return HarnessesListReply(request_id=request_id, harnesses=host.harnesses())
        case LogsListRequest() | LogsReadRequest() if host.logs is None:
            message = f"{request.type} needs a log store, which this host does not have"
            return _error(request.request_id, HelperErrorCode.INTERNAL, message)
        case LogsListRequest():
            return list_logs(request, host.logs)
        case LogsReadRequest():
            return read_log(request, host.logs, withdrawal)


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
