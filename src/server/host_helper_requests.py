"""The host helper's requests: answering one request through the host's interfaces.

The channel (``server.host_helper``) admits a request and hands it to a worker,
which calls :func:`answer`, or :func:`connect` for ``run.connect``. Each request
reads the registry, the project, or the filesystem afresh through
:class:`HelperHost`, so answering keeps no state between requests. The pure
decisions it needs (:func:`negotiate`, :func:`select_completions`,
:func:`event_page`, :func:`launch_arguments`,
:func:`refused_argument`, :func:`unvetted_harness`) are public so they can be tested on their own.

Light like the channel: the standard library, Pydantic, ``vs_sim.api`` and light
``server`` modules only.

The reads of a project's runs (``ProjectRun``, ``run_summaries``, ``project_runs``,
``usage_window_reply``) are ``server.host_run_reads``', shared with the registered reads.

Public surface: ``answer``, ``apply_registered_project``, ``RegisteredProjectHost``,
``launch_registered_run``, ``registry_start_request``,
``registry_read_source``, ``describe_machine``, ``preflight_facts``, ``connect``, ``negotiate``,
``Negotiated``, ``HelperHost``; the interfaces ``Projects``, ``Directories``,
``ProjectMutationExamples``, ``ProjectMutationDirectories``, ``RunLauncher`` and
``CheckoutProbe`` with the values they exchange (``ScannedEntry``, ``Scan``, ``PathHints``);
``home_relative``, ``without_home``, ``complete_path``, ``select_completions``, ``split_prefix``,
``event_page``, ``EventPage``, ``SpanEventPage``, ``TailPage``,
``launch_arguments``, ``refused_argument``, ``unvetted_harness``; and the limit
``SCAN_LIMIT``.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol, assert_never, overload

from pydantic import TypeAdapter

from server import lazy_module
from server.host_api import (
    CLI_PROVIDER_FLAG,
    ENVIRONMENT_FLAGS,
    HELPER_LINE_MAX_BYTES,
    HELPER_PROTOCOL_VERSIONS,
    MODEL_FLAG,
    PATHS_COMPLETE_CAP,
    RUN_ENVIRONMENT_FLAG,
    SLURM_CONFIG_FLAG,
    UNVETTED_MODEL_FLAG,
    AgentLoginsStartReply,
    AgentLoginsStartRequest,
    Checkout,
    CheckoutInspection,
    CheckoutInspectReply,
    CheckoutInspectRequest,
    ComputeAddClusterReply,
    ComputeAddClusterRequest,
    ComputeRemoveClusterReply,
    ComputeRemoveClusterRequest,
    ComputeTestClusterReply,
    ComputeTestClusterRequest,
    DetachedLaunchFailure,
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
    RunDigest,
    RunPreflightReply,
    RunPreflightRequest,
    RunsListReply,
    RunsListRequest,
    RunsSummaryReply,
    RunsSummaryRequest,
    RunStartReply,
    RunStartRequest,
    RunStopReply,
    RunStopRequest,
)
from server.host_compute import (
    ComputeError,
    MeasurementTarget,
    ModalTarget,
    SameMachine,
    SlurmTarget,
)
from server.host_examples import ProjectLookupError
from server.host_helper_logs import HelperLogs, ReadWithdrawal, list_logs, read_log
from server.host_helper_preflight import (
    PreflightFacts,
    abbreviates,
    conflicting_agent,
    measurement_check,
    option_arguments,
    preflight_checks,
    repeated_option,
    undeclared_agent,
    unvetted_agent,
    unvetted_role,
)
from server.host_machine_facts import (
    DockerReachable,
    DockerUnreachable,
    Hardware,
    MachineCheckout,
    MachineDescription,
    RunCounts,
)
from server.host_run_events import (
    RunEventDocument,
    RunEventKinds,
    RunsEventsForSpanReply,
    RunsEventsForSpanRequest,
    RunsReadEventsReply,
    RunsReadEventsRequest,
    RunsReadTailReply,
    RunsReadTailRequest,
)
from server.host_run_reader import RunReaderReply, RunReaderRequest
from server.host_run_summary import UsageWindowReply, UsageWindowRequest
from server.registry import driving
from server.relay import (
    BridgeResult,
    ChannelEndOutcome,
    dial,
)
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping
    from datetime import tzinfo

    from typing_extensions import TypeIs

    from server import (
        host_harness,
        host_operation_reads,
        host_operation_types_generated,
        host_preflight,
        host_run_reads,
        usage_limits,
    )
    from server.host_agent_logins import AgentLogin, LoginStarted, LoginUnavailable
    from server.host_api import HarnessChoice, RunDigestUnreadable
    from server.host_compute import MeasurementTarget
    from server.host_compute_runtime import MachineCompute
    from server.host_examples import (
        CreatedProject,
        ExampleDestination,
        ExampleList,
        TaskGoal,
        TaskList,
    )
    from server.host_helper_readers import Readers
    from server.host_operation_reads import HostReadSource
    from server.host_operation_types_generated import RegistryReadRequest, UnmigratedHelperRequest
    from server.host_preflight import PreflightReport, RoleOption, RunOptions
    from server.host_run_reads import ProjectRun
    from server.registry import InstanceInspector, LiveRegistry, StopEffects
    from server.run_summary import Summarized
    from vibesys.api.host_operations import reads as read_operations
    from vs_agent.api.usage_limits import LimitsReader
    from vs_project.api import OperatorDialer
    from vs_proto import api as wire
    from vs_proto.api import host_payloads as pb
    from vs_sim.api import Clock, Connection, HttpsGet
else:
    host_harness = lazy_module("server.host_harness")
    host_operation_reads = lazy_module("server.host_operation_reads")
    host_operation_types_generated = lazy_module("server.host_operation_types_generated")
    host_preflight = lazy_module("server.host_preflight")
    usage_limits = lazy_module("server.usage_limits")
    host_run_reads = lazy_module("server.host_run_reads")
    read_operations = lazy_module("vibesys.api.host_operations.reads")
    wire = lazy_module("vs_proto.api")

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


class Projects(Protocol):
    """Reads a project's tasks and runs.

    Contract: each method opens the project afresh and raises ``ProjectLookupError``
    with ``project_missing`` (no such directory), ``project_uninitialized`` (no
    ``.vibesys/tasks``; ``tasks`` and ``require_initialized`` only) or
    ``task_invalid`` (what it found does not parse).
    """

    def stream_runs(self, project: Path) -> tuple[Path, Iterator[ProjectRun]]:
        """The project's resolved root and one lazy scan of its runs, in discovery order.

        The iterator raises ``ProjectLookupError`` as these methods do. A run removed while it
        runs is left out and a run added may be; neither fails the scan.
        """
        ...

    def tasks(self, project: Path) -> TaskList:
        """The project's tasks, ordered by name."""
        ...

    def task_goal(self, project: Path, task: str) -> TaskGoal:
        """The goal text and objectives of the project's task ``task``.

        Raises ``ProjectLookupError`` with ``task_invalid`` when the project has no such task or
        its goal files cannot be read.
        """
        ...

    def named_runs(
        self, project: Path, run_ids: Iterable[str]
    ) -> tuple[Path, tuple[ProjectRun, ...]]:
        """The project's resolved root and those of ``run_ids`` it holds, oldest first.

        The cost follows ``run_ids``, not the project's size. An id it does not hold is left out.
        """
        ...

    def run_digest(self, project_root: Path, run_id: str) -> RunDigest | RunDigestUnreadable | None:
        """What run ``run_id``'s journal says at a glance; ``None`` when this machine holds none.

        ``project_root`` is a root ``named_runs`` returned. Never raises: a journal that exists and
        cannot be read is ``RunDigestUnreadable``. A run recorded in the checkout whose journal
        is not in the log store of ``project_root`` has no digest: an empty one would say the
        run recorded nothing and stopped.
        """
        ...

    def require_initialized(self, project: Path) -> Path:
        """The project's resolved root, once it is known to define tasks."""
        ...

    def run_events(
        self, project: Path, run_id: str, from_sequence: int, limit: int, *, with_totals: bool
    ) -> EventPage:
        """A page of run ``run_id``'s journal (``event_page`` under ``RUN_EVENTS_READ_MAX_BYTES``).

        With ``with_totals`` the page names the journal's complete lines and their bytes. Raises
        ``ProjectLookupError`` with ``unknown_run`` when the project has no such run, or has it
        recorded and this machine holds no journal of it (not an empty page, which would call it a
        run with no events); a run found once is not looked up again while this helper serves.
        """
        ...

    def run_kind_events(
        self, project: Path, run_id: str, kinds: RunEventKinds, from_sequence: int, limit: int
    ) -> EventPage:
        """A page of the events of run ``run_id``'s journal that ``kinds`` selects, with totals.

        Read through the journal's span index (``span_index.kind_page``), so the others are
        neither read nor returned. The page is as ``run_events``' with the totals counting the
        selected events alone; ``next_sequence`` of an incomplete page is the sequence after its
        last event and of a complete one the sequence after the journal's last, and an incomplete
        page is never empty. Raises as ``run_events`` does.
        """
        ...

    def run_span_events(
        self, project: Path, run_id: str, span_id: str, from_sequence: int, limit: int
    ) -> SpanEventPage:
        """A page of the journal events of ``span_id`` and the spans beneath it (``span_index``).

        Contract as ``run_events``; a span the journal never names has an empty, complete page.
        """
        ...

    def run_tail(
        self, project: Path, run_id: str, before_sequence: int | None, limit: int
    ) -> TailPage:
        """The newest at most ``limit`` journal events with a sequence below ``before_sequence``.

        Contract as ``run_events``.
        """
        ...

    def run_summary(self, project: Path, run_id: str) -> Summarized:
        """The fold of run ``run_id``'s whole journal, as it is now (``server.run_summary``).

        Raises ``ProjectLookupError`` with ``unknown_run`` as ``run_events`` does.
        """
        ...

    def run_journal(self, project: Path, run_id: str) -> Path:
        """The journal file of run ``run_id``, which a read-only server serves.

        Raises ``ProjectLookupError`` with ``unknown_run`` as ``run_events`` does.
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


class AgentLogins(Protocol):
    """Each vetted harness's login on this machine, and a request to start one.

    Contract: ``list`` has one entry per provider ``providers`` names, in that order, and never
    raises for a CLI problem (a missing CLI is a result); ``start`` is for a provider ``providers``
    names, never raises, and answers ``unavailable`` when it cannot start the login.
    """

    def providers(self) -> tuple[str, ...]:
        """The providers this host offers harnesses for, in offer order."""
        ...

    def list(self) -> tuple[AgentLogin, ...]:
        """Whether each harness is installed and signed in, and how."""
        ...

    def start(self, provider: str) -> LoginStarted | LoginUnavailable:
        """Start ``provider``'s own login on this machine."""
        ...


class ExampleProjects(Protocol):
    """The example catalog of the checkout this helper runs from, and new projects.

    Contract: ``create`` and ``copy_out`` leave nothing at ``destination`` when they fail,
    and every failure is ``ProjectLookupError`` naming its path: ``examples_unavailable``,
    ``unknown_example``, ``destination_exists``, ``destination_nested``, ``path_missing``,
    ``not_nested`` or ``project_create_failed``.
    """

    def list(self, *, with_catalogue: bool = False) -> ExampleList:
        """Every catalogued example with its tasks, and with their catalogue facts if asked."""
        ...

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into its own new Git repository at ``destination``."""
        ...

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source``'s tracked files into their own new Git repository at ``destination``."""
        ...

    def destination(self, example: str, destination: Path) -> tuple[ExampleDestination, str | None]:
        """What ``create`` would find at ``destination``, and the project's directory if it is one.

        Raises ``ProjectLookupError`` with ``examples_unavailable`` or ``unknown_example``.
        """
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
    readers: Readers
    """The read-only servers this host started so Chat can read runs that have no live one."""
    inspector: InstanceInspector
    stop_effects: StopEffects
    projects: Projects
    directories: Directories
    launcher: RunLauncher
    checkout: CheckoutProbe
    dialer: OperatorDialer
    vibesys_version: str
    harnesses: Callable[[], tuple[HarnessOption, ...]]
    """The vetted provider and model pairs, read when a request needs them: the list
    lives with the agent library, which the helper does not load to answer ``hello``."""
    logs: HelperLogs | None
    """The run log store; ``None`` on a host that has none, which then does not offer ``logs``."""
    machine: MachineProbe
    logins: AgentLogins
    compute: MachineCompute
    """The machine's Slurm clusters and Modal token: where a run can measure besides here."""
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


@dataclass(frozen=True)
class EventPage:
    """One page of recorded events and where the next page starts."""

    events: tuple[RunEventDocument, ...]
    next_sequence: int
    complete: bool
    total_events: int | None = None
    """The journal's complete lines when the page was read; ``None`` unless asked for."""
    total_bytes: int | None = None
    """The bytes of those lines; ``None`` with ``total_events``."""


@dataclass(frozen=True)
class SpanEventPage:
    """One page of a span's recorded events, and where the next page starts."""

    events: tuple[RunEventDocument, ...]
    next_sequence: int
    complete: bool
    total: int


@dataclass(frozen=True)
class TailPage:
    """The newest recorded events before a sequence, oldest first."""

    events: tuple[RunEventDocument, ...]
    complete: bool
    last_sequence: int


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
    ``options`` no typed option is repeated as a flag, and with ``measure_on`` no run-environment
    flag either.
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
        if request.measure_on is not None:
            for flag in ENVIRONMENT_FLAGS:
                if abbreviates(token, flag):
                    return f"{token} selects {flag}, which measure_on already sets"
    return repeated_option(request.args, request.options, request.agents)


def environment_arguments(
    target: MeasurementTarget | None, compute: MachineCompute
) -> tuple[tuple[str, ...], SlurmTarget | ModalTarget | None]:
    """The run flags that measure on ``target``, and the target when it is not set up here.

    The same machine adds none: the run's own environment choice stands (Docker, or the host
    environment where a machine has no Docker). A cluster is the existing Slurm environment
    over that cluster's operator file, and Modal the existing Modal environment. A target not
    set up gives no flags and is returned, so ``run.start`` refuses it and ``run.preflight``
    reports it.
    """
    match target:
        case None | SameMachine():
            return (), None
        case SlurmTarget(cluster=name):
            path = compute.slurm_config_path(name)
            if path is None:
                return (), target
            return (RUN_ENVIRONMENT_FLAG, "slurm", SLURM_CONFIG_FLAG, path), None
        case ModalTarget():
            if not compute.modal().token_present:
                return (), target
            return (RUN_ENVIRONMENT_FLAG, "modal"), None
        case _:
            assert_never(target)


def unvetted_harness(
    request: RunStartRequest | RunPreflightRequest, harnesses: tuple[HarnessOption, ...]
) -> HelperError | None:
    """``unvetted_harness`` naming the allowed models: for a harness not offered, or a role's.

    A role's model must be one of the chosen harness's vetted models, and each ``agents`` entry
    a pair ``harnesses`` offers.
    """
    role = unvetted_role(request.options, request.harness, harnesses) or unvetted_agent(
        request.agents, harnesses
    )
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


def run_arguments(
    request: RunStartRequest | RunPreflightRequest, environment: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """The ``vibesys`` run arguments ``request`` names: task, harness, ``environment``, typed options, then ``args``."""
    task = ("--task", request.task) if request.task is not None else ()
    harness = (
        ()
        if request.harness is None
        else (CLI_PROVIDER_FLAG, request.harness.provider, MODEL_FLAG, request.harness.model)
    )
    return (
        *task,
        *harness,
        *environment,
        *option_arguments(request.options, request.agents),
        *request.args,
    )


def launch_arguments(
    request: RunStartRequest, environment: tuple[str, ...] = ()
) -> tuple[str, ...]:
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
    return ("--detach", *run_arguments(request, environment), *resume)


def home_relative(text: str, home: str) -> str:
    """``text`` with each path under ``home`` written ``~/...``, and ``home`` itself ``~``.

    The one place an error message stops naming this account's home directory (and so its user
    name): a client shows messages to a person who may not own the account.
    """
    prefix = home.rstrip("/")
    if not prefix:
        return text
    return re.sub(rf"(?<![\w.~-]){re.escape(prefix)}(?!\w|-|\.\w)", "~", text)


def without_home(reply: HelperReply | HelperError, host: HelperHost) -> HelperReply | HelperError:
    """``reply``, an error with its message in ``~`` terms (:func:`home_relative`); else as it is."""
    if not isinstance(reply, HelperError):
        return reply
    message = home_relative(reply.message, host.directories.home())
    return reply.model_copy(update={"message": message})


class ProjectMutationDirectories(Protocol):
    """The path expansion a registered project mutation uses."""

    def home(self) -> str:
        """This user's home directory."""
        ...

    def expand(self, path: str) -> str:
        """``path`` with a leading ``~`` or ``~user`` expanded."""
        ...


def _host_path(text: str, directories: ProjectMutationDirectories) -> str:
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
        if isinstance(request, HelloRequest):
            return _answer(request, host, withdrawal)
        if is_registry_read(request):
            return read_reply(
                request,
                registry_read_source(host, request),
            )
        return _answer(request, host, withdrawal)
    except (ProjectLookupError, ComputeError) as error:
        return HelperError(
            request_id=request_id, code=error.code, message=str(error), path=error.path
        )


def is_registry_read(request: HelperRequest) -> TypeIs[RegistryReadRequest]:
    """Load the generated narrowing only when a caller goes beyond hello."""
    return host_operation_types_generated.is_registry_read(request)


def _answer(
    request: UnmigratedHelperRequest, host: HelperHost, withdrawal: ReadWithdrawal
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
        case RunsListRequest():
            reply = _project_reply(request, host)
        case (
            RunsReadEventsRequest()
            | RunsEventsForSpanRequest()
            | RunsReadTailRequest()
            | RunsSummaryRequest()
            | UsageWindowRequest()
        ):
            reply = _run_history_reply(request, host)
        case (
            CheckoutInspectRequest()
            | ProjectCreateFromExampleRequest()
            | ProjectCopyOutRequest()
            | ComputeTestClusterRequest()
            | ComputeAddClusterRequest()
            | ComputeRemoveClusterRequest()
            | AgentLoginsStartRequest()
        ):
            reply = _machine_reply(request, host)
        case RunStartRequest() | RunReaderRequest():
            reply = _launching_reply(request, host)
        case RunStopRequest(instance_id=instance_id, force=force):
            result = host.registry.stop(instance_id, host.stop_effects, force=force)
            reply = RunStopReply(request_id=request_id, result=result)
        case PingRequest() | PathsCompleteRequest() | LogsListRequest() | LogsReadRequest():
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


def _project_reply(request: RunsListRequest, host: HelperHost) -> RunsListReply:
    """Answer a project's saved-run listing."""
    project = Path(_host_path(request.project, host.directories))
    runs = host_run_reads.project_runs(
        host, project, request.digest_runs or (), with_dashboard=bool(request.with_dashboard)
    )
    return RunsListReply(request_id=request.request_id, runs=runs)


def _logins_reply(
    request: AgentLoginsStartRequest, logins: AgentLogins
) -> AgentLoginsStartReply | HelperError:
    """Answer one read of the harnesses' logins, or a request to start one."""
    request_id = request.request_id
    if request.provider not in logins.providers():
        message = f"this host offers no harness for provider {request.provider!r}"
        return _error(request_id, HelperErrorCode.UNKNOWN_PROVIDER, message)
    return AgentLoginsStartReply(request_id=request_id, result=logins.start(request.provider))


def _compute_reply(
    request: ComputeTestClusterRequest | ComputeAddClusterRequest | ComputeRemoveClusterRequest,
    compute: MachineCompute,
) -> HelperReply:
    """Answer one read or change of this machine's compute settings."""
    request_id = request.request_id
    match request:
        case ComputeTestClusterRequest(cluster=cluster):
            return ComputeTestClusterReply(request_id=request_id, result=compute.test(cluster))
        case ComputeAddClusterRequest(cluster=cluster):
            return ComputeAddClusterReply(request_id=request_id, settings=compute.add(cluster))
        case ComputeRemoveClusterRequest(name=name):
            return ComputeRemoveClusterReply(request_id=request_id, settings=compute.remove(name))


def _examples_reply(
    request: ProjectCreateFromExampleRequest | ProjectCopyOutRequest,
    host: HelperHost,
) -> ProjectCreateFromExampleReply | ProjectCopyOutReply:
    """Answer making one new project from an example, or copying one out."""
    match request:
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
    request: CheckoutInspectRequest
    | ProjectCreateFromExampleRequest
    | ProjectCopyOutRequest
    | ComputeTestClusterRequest
    | ComputeAddClusterRequest
    | ComputeRemoveClusterRequest
    | AgentLoginsStartRequest,
    host: HelperHost,
) -> HelperReply | HelperError:
    """Answer what this machine offers, what a path on it holds, or make a project on it."""
    match request:
        case AgentLoginsStartRequest():
            return _logins_reply(request, host.logins)
        case (
            ComputeTestClusterRequest() | ComputeAddClusterRequest() | ComputeRemoveClusterRequest()
        ):
            return _compute_reply(request, host.compute)
        case ProjectCreateFromExampleRequest() | ProjectCopyOutRequest():
            return _examples_reply(request, host)
        case CheckoutInspectRequest(request_id=request_id, path=path):
            inspection = host.machine.inspect_checkout(Path(_host_path(path, host.directories)))
            return CheckoutInspectReply(request_id=request_id, inspection=inspection)


def describe_machine(host: HelperHost) -> MachineDescription:
    """What this machine offers runs: its checkout, hardware, Docker, harness CLIs and runs."""
    harness_clis = host.machine.harness_clis()
    names = {
        option.provider: option.display.provider_name
        for option in host.harnesses()
        if option.display is not None
    }
    return MachineDescription(
        checkout=_machine_checkout(host),
        hardware=host.machine.hardware(),
        docker=host.machine.docker(),
        harness_clis=harness_clis,
        harness_cli_names={
            provider: names[provider] for provider in harness_clis if provider in names
        },
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


def _run_history_reply(
    request: RunsReadEventsRequest
    | RunsEventsForSpanRequest
    | RunsReadTailRequest
    | RunsSummaryRequest
    | UsageWindowRequest,
    host: HelperHost,
) -> (
    RunsReadEventsReply
    | RunsEventsForSpanReply
    | RunsReadTailReply
    | RunsSummaryReply
    | UsageWindowReply
):
    """Answer one read of a run's recorded history.

    Its events (from a sequence, of a span, or the newest), its summary, or what its turns spent
    in a window.
    """
    if isinstance(request, RunsSummaryRequest):
        return _run_summary_reply(request, host)
    if isinstance(request, UsageWindowRequest):
        return host_run_reads.usage_window_reply(
            request, host, Path(_host_path(request.project, host.directories))
        )
    project = Path(_host_path(request.project, host.directories))
    if isinstance(request, RunsEventsForSpanRequest):
        span_page = host.projects.run_span_events(
            project, request.run_id, request.span_id, request.from_sequence, request.max
        )
        return RunsEventsForSpanReply(
            request_id=request.request_id,
            events=span_page.events,
            next_sequence=span_page.next_sequence,
            complete=span_page.complete,
            total=span_page.total,
        )
    if isinstance(request, RunsReadTailRequest):
        tail = host.projects.run_tail(
            project, request.run_id, request.before_sequence, request.tail
        )
        return RunsReadTailReply(
            request_id=request.request_id,
            events=tail.events,
            complete=tail.complete,
            last_sequence=tail.last_sequence,
        )
    return _run_events_reply(request, host)


def _run_summary_reply(request: RunsSummaryRequest, host: HelperHost) -> RunsSummaryReply:
    """Answer one page of a run's summary."""
    project = Path(_host_path(request.project, host.directories))
    summarized = host.projects.run_summary(project, request.run_id)
    fold = summarized.fold
    page = fold.page(request.offset)
    return RunsSummaryReply(
        request_id=request.request_id,
        through_sequence=fold.through_sequence,
        event_count=fold.event_count,
        journal_bytes=summarized.journal_bytes,
        trace=fold.trace,
        rounds_finished=fold.rounds_finished,
        span_count=fold.span_count,
        reply_count=fold.reply_count,
        usage_count=fold.usage_count,
        record_count=fold.record_count,
        tokens=fold.tokens,
        overview=fold.overview,
        input_gates=fold.input_gates if request.with_ending else None,
        terminal=fold.terminal if request.with_ending else None,
        spans=page.spans,
        replies=page.replies,
        schemas=page.schemas,
        usage=page.usage,
        records=page.records,
        next_offset=page.next_offset,
    )


def _run_events_reply(request: RunsReadEventsRequest, host: HelperHost) -> RunsReadEventsReply:
    """Answer one page of a run's recorded events."""
    project = Path(_host_path(request.project, host.directories))
    with_totals = bool(request.with_totals)
    if request.kinds is None:
        page = host.projects.run_events(
            project, request.run_id, request.from_sequence, request.max, with_totals=with_totals
        )
    else:
        page = host.projects.run_kind_events(
            project, request.run_id, request.kinds, request.from_sequence, request.max
        )
    return RunsReadEventsReply(
        request_id=request.request_id,
        events=page.events,
        next_sequence=page.next_sequence,
        complete=page.complete,
        total_events=page.total_events if with_totals else None,
        total_bytes=page.total_bytes if with_totals else None,
    )


def _start(request: RunStartRequest, host: HelperHost) -> RunStartReply | HelperError:
    """Launch ``request`` once its arguments and harness are acceptable."""
    refusal = _refusal(request, host)
    if refusal is not None:
        return refusal
    root = host.projects.require_initialized(Path(_host_path(request.project, host.directories)))
    environment, missing = environment_arguments(request.measure_on, host.compute)
    if missing is not None:
        return _unconfigured_target(request.request_id, missing)
    if request.agents:
        # Only the run's own validation knows which roles its orchestration declares.
        undeclared = undeclared_agent(
            request.agents, host.preflight.report(root, run_arguments(request, environment))
        )
        if undeclared is not None:
            return _error(request.request_id, HelperErrorCode.INVALID_REQUEST, undeclared)
    launch = host.launcher.start(root, launch_arguments(request, environment))
    return RunStartReply(request_id=request.request_id, launch=launch)


def _launching_reply(
    request: RunStartRequest | RunReaderRequest, host: HelperHost
) -> RunStartReply | RunPreflightReply | RunReaderReply | HelperError:
    """Answer a request that starts a run, checks that one could start, or reads one with a server."""
    match request:
        case RunStartRequest():
            return _start(request, host)
        case RunReaderRequest():
            return _reader(request, host)


def reader_arguments(journal: Path, run_id: str) -> tuple[str, ...]:
    """The ``vibesys`` arguments that serve the journal of ``run_id`` read-only, detached."""
    return ("--detach", "--reopen", str(journal.parent), "--run-id", run_id)


def _reader(request: RunReaderRequest, host: HelperHost) -> RunReaderReply | HelperError:
    """The server Chat reads ``request``'s run through: its live one, else a read-only one.

    A run with a live server is read through it. A run without one is served by the read-only
    server this host already runs for it, or by one started now from the run's journal, which
    ends by itself once no client holds it.
    """
    live = driving(host.registry.list(), request.run_id)
    if live is not None:
        return RunReaderReply(
            request_id=request.request_id,
            instance_id=live.id,
            read_only=False,
            chat=_chat_answer(request),
        )
    project = host.projects.require_initialized(Path(_host_path(request.project, host.directories)))
    journal = host.projects.run_journal(project, request.run_id)
    started = host.readers.serve(
        request.run_id,
        lambda: host.launcher.start(project, reader_arguments(journal, request.run_id)),
    )
    if isinstance(started, DetachedLaunchFailure):
        return _error(request.request_id, HelperErrorCode.READER_FAILED, started.message)
    return RunReaderReply(
        request_id=request.request_id,
        instance_id=started.id,
        read_only=True,
        chat=_chat_answer(request),
    )


def _chat_answer(request: RunReaderRequest) -> bool | None:
    """Every server this helper reads a run through answers chat: said when the client asked."""
    return True if request.with_chat else None


def _unconfigured_target(request_id: str | None, target: SlurmTarget | ModalTarget) -> HelperError:
    if isinstance(target, SlurmTarget):
        message = f"no Slurm cluster named {target.cluster!r} is set up on this machine"
        return _error(request_id, HelperErrorCode.UNKNOWN_CLUSTER, message)
    return _error(
        request_id, HelperErrorCode.MODAL_NOT_CONFIGURED, "Modal has no token on this machine"
    )


def preflight_facts(
    request: RunPreflightRequest, host: HelperHost
) -> RunPreflightReply | HelperError:
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
    environment, _missing = environment_arguments(request.measure_on, host.compute)
    report = host.preflight.report(root, run_arguments(request, environment))
    undeclared = undeclared_agent(request.agents, report)
    if undeclared is not None:
        return _error(request.request_id, HelperErrorCode.INVALID_REQUEST, undeclared)
    checks = preflight_checks(
        facts,
        report,
        options=request.options,
        agents=request.agents,
        with_causes=bool(request.with_causes),
    )
    if request.measure_on is not None:
        measured = measurement_check(request.measure_on, host.compute.settings(), report.backend)
        checks = (*checks, measured)
    return RunPreflightReply(
        request_id=request.request_id,
        checks=checks,
        roles=report.roles,
        backend=report.backend,
    )


def _refusal(
    request: RunStartRequest | RunPreflightRequest, host: HelperHost
) -> HelperError | None:
    """Why ``request``'s arguments, harness or role models cannot start a run, if they cannot."""
    refusal = refused_argument(request)
    if refusal is not None:
        return _error(request.request_id, HelperErrorCode.INVALID_REQUEST, refusal)
    conflict = conflicting_agent(request.options, request.agents)
    if conflict is not None:
        return _error(request.request_id, HelperErrorCode.INVALID_REQUEST, conflict)
    if request.harness is None and request.options is None and request.agents is None:
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
    request: PingRequest | PathsCompleteRequest | LogsListRequest | LogsReadRequest,
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
    record = host.registry.find(request.instance_id) or host.readers.find(request.instance_id)
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


_REPLY: TypeAdapter[HelperReply] = TypeAdapter(HelperReply)


def read_reply(request: HelperRequest, source: HostReadSource) -> HelperReply | HelperError:
    """Execute a registered helper read; pairing and routing come from descriptors."""
    operation = wire.host_operations.ADAPTER_OPERATIONS[request.type]
    payload = request.model_dump(mode="json", exclude={"type", "deadline_ms"})
    accepted = operation.accept(wire.from_mapping(operation.request, payload))
    try:
        encoded = read_operations.execute_read(accepted, source)
    except host_operation_reads.ReadRefusedError as error:
        return error.refusal
    response = operation.response.from_binary(encoded)
    return _REPLY.validate_python({"type": request.type, **wire.to_mapping(response)})


def _limits_reader() -> LimitsReader:
    """The reader of provider limits; the agent library loads when a limits read asks."""
    return usage_limits.reader_here()


def registry_read_source(
    host: HelperHost,
    request: HelperRequest | None = None,
    *,
    https: HttpsGet | None = None,
    limits: Callable[[], LimitsReader] = _limits_reader,
) -> HostReadSource:
    """Bind shared read facts; preserve only a legacy request's role display order.

    ``https`` is the client for the price list's fetch; a read source without one answers every
    read but that refresh (``entrypoints.host_reads`` builds the real one when a refresh asks).
    ``limits`` builds the reader of provider limits: the host's own CLIs, unless a test hands in
    a fake.
    """
    role_order = (
        tuple(request.agents)
        if isinstance(request, RunPreflightRequest) and request.agents is not None
        else None
    )
    return host_operation_reads.HostReadSource(
        host,
        role_order,
        describe=lambda: describe_machine(host),
        preflight=lambda value: preflight_facts(value, host),
        outside=host_operation_reads.Outside(
            https=None if https is None else (lambda: https),
            limits=limits,
        ),
    )


def launch_registered_run(
    request: RunStartRequest, host: HelperHost
) -> RunStartReply | HelperError:
    """Launch one already-admitted start through the helper's launcher."""
    return _start(request, host)


class ProjectMutationExamples(Protocol):
    """The project changes one registered mutation asks of a host."""

    def create(self, example: str, destination: Path) -> CreatedProject:
        """Copy ``example`` into a new repository at ``destination``."""
        ...

    def copy_out(self, source: Path, destination: Path) -> CreatedProject:
        """Copy ``source`` into a new repository at ``destination``."""
        ...


class RegisteredProjectHost(Protocol):
    """A host that can create or copy one project."""

    @property
    def examples(self) -> ProjectMutationExamples:
        """The example catalog and project copies."""
        ...

    @property
    def directories(self) -> ProjectMutationDirectories:
        """How a destination path is expanded."""
        ...


@overload
def apply_registered_project(
    slot: Literal["create_example"],
    request: pb.CreateExampleRequest,
    host: RegisteredProjectHost,
) -> pb.CreateExampleResponse: ...


@overload
def apply_registered_project(
    slot: Literal["copy_project"],
    request: pb.CopyProjectRequest,
    host: RegisteredProjectHost,
) -> pb.CopyProjectResponse: ...


@overload
def apply_registered_project(
    slot: str,
    request: pb.CreateExampleRequest | pb.CopyProjectRequest,
    host: RegisteredProjectHost,
) -> pb.CreateExampleResponse | pb.CopyProjectResponse: ...


def apply_registered_project(
    slot: str,
    request: pb.CreateExampleRequest | pb.CopyProjectRequest,
    host: RegisteredProjectHost,
) -> pb.CreateExampleResponse | pb.CopyProjectResponse:
    """Create or copy one project and return that slot's registry response.

    A destination that already holds something is the create answer ``taken``.
    Every other ``ProjectLookupError`` stays with the caller, which stores a refusal.
    """
    payloads = wire.host_payloads
    if slot == "create_example" and isinstance(request, payloads.CreateExampleRequest):
        try:
            created = host.examples.create(
                request.example, Path(_host_path(request.destination, host.directories))
            )
        except ProjectLookupError as exc:
            if exc.code is not HelperErrorCode.DESTINATION_EXISTS:
                raise
            return payloads.CreateExampleResponse(
                request_id=request.request_id,
                result=wire.Oneof("taken", payloads.DestinationTaken()),
            )
        return payloads.CreateExampleResponse(
            request_id=request.request_id,
            result=wire.Oneof("created", _registry_created(created)),
        )
    if slot == "copy_project" and isinstance(request, payloads.CopyProjectRequest):
        created = host.examples.copy_out(
            Path(_host_path(request.source, host.directories)),
            Path(_host_path(request.destination, host.directories)),
        )
        return payloads.CopyProjectResponse(
            request_id=request.request_id, created=_registry_created(created)
        )
    message = f"project slot {slot} does not match its request"
    raise ValueError(message)


def _registry_created(created: CreatedProject) -> pb.CreatedProject:
    return wire.from_mapping(wire.host_payloads.CreatedProject, created.model_dump(mode="json"))


def registry_start_request(request: pb.StartRunRequest) -> RunStartRequest:
    """The helper start body for one registry ``StartRunRequest``."""
    agents = _start_agents(request.agents)
    return RunStartRequest(
        request_id=request.request_id,
        deadline_ms=60_000,
        project=request.project,
        task=request.task or None,
        args=tuple(request.args),
        harness=_start_harness(request.harness),
        options=_start_options(request.options),
        agents=agents,
        measure_on=_start_measure(request.measure_on),
    )


def _start_harness(harness: pb.StartRunHarness | None) -> HarnessChoice | None:
    if harness is None:
        return None
    choice = host_harness.HarnessChoice
    return choice(provider=harness.provider, model=harness.model)


def _start_agents(agents: Iterable[pb.StartRunAgent]) -> dict[str, HarnessChoice] | None:
    choice = host_harness.HarnessChoice
    mapped = {}
    for agent in agents:
        harness = agent.harness
        if harness is None:
            message = f"agents.{agent.role} has no harness"
            raise ValueError(message)
        mapped[agent.role] = choice(provider=harness.provider, model=harness.model)
    return mapped or None


def _start_options(options: pb.StartRunOptions | None) -> RunOptions | None:
    if options is None:
        return None
    payloads = wire.host_payloads
    rounds = options.rounds or None
    limit = options.time_limit_s or None
    chosen = {
        payloads.StartRunResults.KEEP: host_preflight.RunResults.KEEP,
        payloads.StartRunResults.PUBLISH_GITHUB: host_preflight.RunResults.PUBLISH_GITHUB,
    }.get(options.results)
    roles = tuple(_start_role(role) for role in options.roles)
    if rounds is None and limit is None and chosen is None and not roles:
        return None
    return host_preflight.RunOptions(rounds=rounds, time_limit_s=limit, results=chosen, roles=roles)


def _start_role(role: pb.StartRunRole) -> RoleOption:
    payloads = wire.host_payloads
    effort = {
        payloads.StartRunReasoning.DEFAULT: host_preflight.ReasoningEffort.DEFAULT,
        payloads.StartRunReasoning.LOW: host_preflight.ReasoningEffort.LOW,
        payloads.StartRunReasoning.MEDIUM: host_preflight.ReasoningEffort.MEDIUM,
        payloads.StartRunReasoning.HIGH: host_preflight.ReasoningEffort.HIGH,
    }.get(role.reasoning, host_preflight.ReasoningEffort.DEFAULT)
    return host_preflight.RoleOption(
        role=role.role, model=role.model or None, reasoning_effort=effort
    )


def _start_measure(measure: pb.StartRunMeasure | None) -> MeasurementTarget | None:
    if measure is None or measure.target is None:
        return None
    match measure.target.field:
        case "same_machine":
            return SameMachine()
        case "slurm_cluster":
            return SlurmTarget(cluster=measure.target.value)
        case "modal":
            return ModalTarget()
        case _:
            message = "unknown measure target"
            raise ValueError(message)
