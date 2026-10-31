"""Native read adapters over host-owned facts.

The adapter translates probe/catalog results into generated messages. It owns
neither the helper channel nor a live run. Read refusals retain their closed
category; callers receive a validated registry response.

Public surface: HostReadFacts, HostReadSource, ReadRefusedError,
read_failure_category, answer_registered_read and stream_saved_runs.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from server.host_api import HelperError, RunPreflightRequest
from server.host_compute import ComputeError
from server.host_envelope import HelperErrorCode
from server.host_examples import ProjectLookupError
from server.host_run_summary import UsageWindowRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from types import ModuleType

    from server.host_agent_logins import AgentLogin
    from server.host_api import HarnessOption, RunDigest, RunDigestUnreadable, RunPreflightReply
    from server.host_compute import ComputeSettings
    from server.host_examples import ExampleDestination, ExampleList, TaskGoal, TaskList
    from server.host_machine_facts import MachineDescription
    from server.host_run_reads import ProjectRun
    from server.registry import LiveRegistry
    from server.run_summary import Summarized
    from vs_proto.api import FailureCategory
    from vs_proto.api import host_payloads as pb
    from vs_sim.api import Clock


class _ReadProjects(Protocol):
    def stream_runs(self, project: Path) -> tuple[Path, Iterator[ProjectRun]]: ...
    def tasks(self, project: Path) -> TaskList: ...
    def task_goal(self, project: Path, task: str) -> TaskGoal: ...
    def named_runs(
        self, project: Path, run_ids: Iterable[str]
    ) -> tuple[Path, tuple[ProjectRun, ...]]: ...
    def run_digest(
        self, project_root: Path, run_id: str
    ) -> RunDigest | RunDigestUnreadable | None: ...
    def run_summary(self, project: Path, run_id: str) -> Summarized: ...


class _ReadDirectories(Protocol):
    def expand(self, path: str) -> str: ...
    def home(self) -> str: ...


class _ReadExamples(Protocol):
    def list(self, *, with_catalogue: bool) -> ExampleList: ...
    def destination(
        self, example: str, destination: Path
    ) -> tuple[ExampleDestination, str | None]: ...


class _ReadCompute(Protocol):
    def settings(self) -> ComputeSettings: ...


class _ReadMachine(Protocol):
    def harness_clis(self) -> tuple[str, ...]: ...


class _ReadLogins(Protocol):
    def list(self) -> tuple[AgentLogin, ...]: ...


class HostReadFacts(Protocol):
    """Fresh catalogs and probes, independent of transport and run ownership."""

    @property
    def projects(self) -> _ReadProjects:
        """Project metadata and authored tasks."""
        ...

    @property
    def directories(self) -> _ReadDirectories:
        """Host path expansion and home directory."""
        ...

    @property
    def examples(self) -> _ReadExamples:
        """Checkout examples and destination inspection."""
        ...

    @property
    def compute(self) -> _ReadCompute:
        """Source-aware compute settings."""
        ...

    @property
    def registry(self) -> LiveRegistry:
        """The servers this host runs or ran, which a run's attention is judged against."""
        ...

    @property
    def clock(self) -> Clock:
        """Wall-clock epoch seconds."""
        ...

    @property
    def machine(self) -> _ReadMachine:
        """Installed provider probes."""
        ...

    @property
    def logins(self) -> _ReadLogins:
        """Provider-owned login facts."""
        ...

    def harnesses(self) -> tuple[HarnessOption, ...]:
        """Vetted harness facts from the agent library."""
        ...


class ReadRefusedError(Exception):
    """A probe refused a read with a closed category and diagnostic."""

    def __init__(self, refusal: HelperError) -> None:
        """Retain the original refusal for both transport adapters."""
        self.refusal = refusal
        super().__init__(refusal.message)


class HostReadSource:
    """Translate existing host I/O results at the adapter boundary only."""

    def __init__(
        self,
        host: HostReadFacts,
        role_order: tuple[str, ...] | None,
        *,
        describe: Callable[[], MachineDescription],
        preflight: Callable[[RunPreflightRequest], RunPreflightReply | HelperError],
    ) -> None:
        """Bind host I/O and the existing machine/preflight probe adapters."""
        self._host = host
        self._role_order = role_order
        self._describe = describe
        self._preflight = preflight

    def _path(self, text: str) -> Path:
        expanded = Path(self._host.directories.expand(text))
        return (
            expanded if expanded.is_absolute() else Path(self._host.directories.home()) / expanded
        )

    def saved_runs(self, project: str) -> tuple[str, Iterable[pb.SavedRun]]:
        """One lazy scan through the existing project catalog owner, in discovery order."""
        root, runs = self._host.projects.stream_runs(self._path(project))
        return str(root), import_module("server.host_saved_runs").saved_run_rows(root, runs)

    def run_digests(self, request: pb.ListRunDigestsRequest) -> pb.ListRunDigestsResponse:
        """Read each run's digest through the owner of the helper's project run listing."""
        requests = import_module("server.host_run_reads")
        runs = requests.named_run_digests(
            self._host,
            self._path(request.project),
            request.run_ids,
            with_dashboard=request.with_dashboard,
        )
        return import_module("server.host_run_wire").digests_response(request.request_id, runs)

    def usage_window(self, request: pb.UsageWindowRequest) -> pb.UsageWindowResponse:
        """Sum the runs' usage through the owner of the helper's usage window."""
        if request.since is None or request.until is None:
            message = "since and until: a window needs both ends"
            raise ValueError(message)
        legacy = UsageWindowRequest(
            request_id=request.request_id,
            deadline_ms=_wire().host_operations.UsageWindow.total_ms,
            project=request.project,
            run_ids=tuple(request.run_ids),
            since=request.since.to_datetime(),
            until=request.until.to_datetime(),
        )
        requests = import_module("server.host_run_reads")
        reply = requests.usage_window_reply(legacy, self._host, self._path(request.project))
        return import_module("server.host_run_wire").usage_response(request.request_id, reply)

    def tasks(self, project: str) -> pb.TaskList:
        """Read through the existing project's task owner."""
        return _wire().from_mapping(
            _wire().host_payloads.TaskList,
            self._host.projects.tasks(self._path(project)).model_dump(mode="json"),
        )

    def logins(self) -> list[pb.AgentLogin]:
        """Project the provider's observed login state without reading secrets."""
        return [
            _wire().from_mapping(_wire().host_payloads.AgentLogin, login.model_dump(mode="json"))
            for login in self._host.logins.list()
        ]

    def harnesses(self) -> list[pb.HarnessOption]:
        """Project vetted harness facts; ordering belongs to the product handler."""
        return [
            _wire().from_mapping(
                _wire().host_payloads.HarnessOption, option.model_dump(mode="json")
            )
            for option in self._host.harnesses()
        ]

    def installed_harnesses(self) -> tuple[str, ...]:
        """Observe the installed providers once for display ordering."""
        return self._host.machine.harness_clis()

    def inspect_destination(self, example: str, destination: str) -> pb.InspectDestinationResponse:
        """What creating ``example`` would find at ``destination``."""
        models = import_module("server.host_examples")
        state, project = self._host.examples.destination(example, self._path(destination))
        payloads = _wire().host_payloads
        states = {
            models.ExampleDestination.ABSENT: payloads.DestinationState.ABSENT,
            models.ExampleDestination.EMPTY: payloads.DestinationState.EMPTY,
            models.ExampleDestination.EXAMPLE_PROJECT: payloads.DestinationState.EXAMPLE_PROJECT,
            models.ExampleDestination.OCCUPIED: payloads.DestinationState.OCCUPIED,
        }
        return payloads.InspectDestinationResponse(
            state=states[state],
            **({} if project is None else {"project": project}),
        )

    def examples(self, *, with_catalogue: bool) -> pb.ExampleList:
        """Project the checkout owner's example metadata without dropping details."""
        return _wire().from_mapping(
            _wire().host_payloads.ExampleList,
            self._host.examples.list(with_catalogue=with_catalogue).model_dump(mode="json"),
        )

    def task_goal(self, project: str, task: str) -> pb.TaskGoal:
        """Read the bounded authored task goal."""
        return _wire().from_mapping(
            _wire().host_payloads.TaskGoal,
            self._host.projects.task_goal(self._path(project), task).model_dump(mode="json"),
        )

    def machine(self) -> pb.MachineDescription:
        """Project the current machine probe snapshot."""
        return _wire().from_mapping(
            _wire().host_payloads.MachineDescription,
            self._describe().model_dump(mode="json"),
        )

    def compute(self) -> pb.ComputeSettings:
        """Project source-aware settings; this operation performs no mutation."""
        return _wire().from_mapping(
            _wire().host_payloads.ComputeSettings,
            self._host.compute.settings().model_dump(mode="json"),
        )

    def preflight(self, request: pb.PreflightRunRequest) -> pb.PreflightRunResponse:
        """Revalidate the temporary legacy projection before invoking its probe owner."""
        payload = _wire().to_mapping(request)
        # Legacy diagnostics choose the first role in insertion order. Protobuf
        # maps have no ordering, so retain only that display preference here.
        if self._role_order is not None:
            agents = payload["agents"]
            if not isinstance(agents, dict):
                message = "agents: expected a projected role mapping"
                raise TypeError(message)
            payload["agents"] = {role: agents[role] for role in self._role_order}
        legacy = RunPreflightRequest.model_validate(
            {**payload, "deadline_ms": _wire().host_operations.PreflightRun.total_ms}
        )
        result = self._preflight(legacy)
        if isinstance(result, HelperError):
            raise ReadRefusedError(result)
        return _wire().from_mapping(
            _wire().host_payloads.PreflightRunResponse,
            result.model_dump(mode="json", exclude={"type"}),
        )


def _wire() -> ModuleType:
    """Load protobuf contracts only when a migrated read executes, preserving light startup."""
    return import_module("vs_proto.api")


def read_failure_category(code: HelperErrorCode) -> FailureCategory:
    """Map one helper refusal onto the closed failure category.

    Callers branch on the category. ``CLUSTER_READ_ONLY`` is permission denied:
    the file's owner is not this helper.
    """
    failure = _wire().FailureCategory
    categories = {
        HelperErrorCode.INVALID_REQUEST: failure.INVALID_INPUT,
        HelperErrorCode.TASK_INVALID: failure.INVALID_INPUT,
        HelperErrorCode.PATH_NOT_DIRECTORY: failure.INVALID_INPUT,
        HelperErrorCode.UNVETTED_HARNESS: failure.INVALID_INPUT,
        HelperErrorCode.CLUSTER_INVALID: failure.INVALID_INPUT,
        HelperErrorCode.INCOMPATIBLE: failure.CONFIGURATION,
        HelperErrorCode.EXAMPLES_UNAVAILABLE: failure.CONFIGURATION,
        HelperErrorCode.COMPUTE_CONFIG_UNREADABLE: failure.CONFIGURATION,
        HelperErrorCode.MODAL_NOT_CONFIGURED: failure.CONFIGURATION,
        HelperErrorCode.DUPLICATE_REQUEST_ID: failure.CONFLICT,
        HelperErrorCode.DESTINATION_EXISTS: failure.CONFLICT,
        HelperErrorCode.DESTINATION_NESTED: failure.CONFLICT,
        HelperErrorCode.NOT_NESTED: failure.CONFLICT,
        HelperErrorCode.CLUSTER_EXISTS: failure.CONFLICT,
        HelperErrorCode.UNKNOWN_WATCH: failure.NOT_FOUND,
        HelperErrorCode.PROJECT_MISSING: failure.NOT_FOUND,
        HelperErrorCode.PROJECT_UNINITIALIZED: failure.NOT_FOUND,
        HelperErrorCode.PATH_MISSING: failure.NOT_FOUND,
        HelperErrorCode.RUN_GONE: failure.NOT_FOUND,
        HelperErrorCode.UNKNOWN_INSTANCE: failure.NOT_FOUND,
        HelperErrorCode.UNKNOWN_LOG_STREAM: failure.NOT_FOUND,
        HelperErrorCode.UNKNOWN_EXAMPLE: failure.NOT_FOUND,
        HelperErrorCode.UNKNOWN_RUN: failure.NOT_FOUND,
        HelperErrorCode.UNKNOWN_CLUSTER: failure.NOT_FOUND,
        HelperErrorCode.UNKNOWN_PROVIDER: failure.NOT_FOUND,
        HelperErrorCode.PERMISSION_DENIED: failure.PERMISSION_DENIED,
        HelperErrorCode.CONNECT_DENIED: failure.PERMISSION_DENIED,
        HelperErrorCode.CLUSTER_READ_ONLY: failure.PERMISSION_DENIED,
        HelperErrorCode.HANDSHAKE_REQUIRED: failure.UNSUPPORTED,
        HelperErrorCode.FEATURE_NOT_NEGOTIATED: failure.UNSUPPORTED,
        HelperErrorCode.CHANNEL_BUSY: failure.RESOURCE_EXHAUSTED,
        HelperErrorCode.CONNECT_FAILED: failure.TRANSIENT,
        HelperErrorCode.DEADLINE_EXCEEDED: failure.DEADLINE_EXCEEDED,
        HelperErrorCode.LOG_STORE_CORRUPT: failure.INTERNAL,
        HelperErrorCode.PROJECT_CREATE_FAILED: failure.INTERNAL,
        HelperErrorCode.CLUSTER_TEST_FAILED: failure.INTERNAL,
        HelperErrorCode.READER_FAILED: failure.INTERNAL,
        HelperErrorCode.INTERNAL: failure.INTERNAL,
    }
    found = categories.get(code)
    if found is None:
        message = f"unmapped helper code {code}"
        raise ValueError(message)
    return found


def answer_registered_read(cli: str, payload: bytes, source: HostReadSource) -> bytes:
    """Run one registered read for ``vibesys host`` and return the envelope bytes."""
    operations = import_module("vibesys.api.host_operations.api")
    try:
        return operations.execute_host_read(payload, source, cli=cli)
    except ReadRefusedError as error:
        category = read_failure_category(error.refusal.code)
        return operations.host_read_failure(category, error.refusal.message)
    except (ProjectLookupError, ComputeError) as error:
        return operations.host_read_failure(read_failure_category(error.code), str(error))


def stream_saved_runs(
    payload: bytes, source: HostReadSource, write: Callable[[bytes], object]
) -> None:
    """Run the saved-run stream for ``vibesys host``; a refusal ends it as a typed failure frame."""
    operations = import_module("vibesys.api.host_operations.api")
    try:
        operations.serve_saved_runs(payload, source, cli="stream-saved-runs", write=write)
    except ProjectLookupError as error:
        write(operations.host_watch_failure(read_failure_category(error.code), str(error)))


__all__ = [
    "HostReadFacts",
    "HostReadSource",
    "ReadRefusedError",
    "answer_registered_read",
    "read_failure_category",
    "stream_saved_runs",
]
