"""Native read adapters over host-owned facts.

The adapter translates probe/catalog results into generated messages. It owns
neither the helper channel nor a live run. Read refusals retain their closed
category; callers receive a validated registry response.

``vibesys host <read>`` is a new process per read, so every module this one loads at
import is paid by every read. It therefore declares the modules only some reads use as lazy
modules (``server.lazy_module``), loaded at the read or refusal that needs them.

Public surface: HostReadFacts, HostReadSource, ReadRefusedError,
read_failure_category, answer_registered_read and stream_saved_runs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from server import lazy_module

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from server import (
        host_api,
        host_compute,
        host_envelope,
        host_examples,
        host_run_reads,
        host_run_summary,
        host_run_wire,
        host_saved_runs,
        usage_scan,
    )
    from server.host_agent_logins import AgentLogin
    from server.host_machine_facts import MachineDescription
    from server.registry import LiveRegistry
    from server.run_summary import Summarized
    from vibesys.api.host_operations import reads as read_operations
    from vibesys.api.host_operations import saved_runs
    from vs_proto import api as wire
    from vs_proto.api import FailureCategory
    from vs_proto.api import host_payloads as pb
    from vs_sim.api import Clock, HttpsGet
else:
    host_api = lazy_module("server.host_api")
    host_compute = lazy_module("server.host_compute")
    host_envelope = lazy_module("server.host_envelope")
    host_examples = lazy_module("server.host_examples")
    host_run_reads = lazy_module("server.host_run_reads")
    host_run_summary = lazy_module("server.host_run_summary")
    host_run_wire = lazy_module("server.host_run_wire")
    host_saved_runs = lazy_module("server.host_saved_runs")
    usage_scan = lazy_module("server.usage_scan")
    read_operations = lazy_module("vibesys.api.host_operations.reads")
    saved_runs = lazy_module("vibesys.api.host_operations.saved_runs")
    wire = lazy_module("vs_proto.api")


class _ReadProjects(Protocol):
    def stream_runs(self, project: Path) -> tuple[Path, Iterator[host_run_reads.ProjectRun]]: ...
    def tasks(self, project: Path) -> host_examples.TaskList: ...
    def task_goal(self, project: Path, task: str) -> host_examples.TaskGoal: ...
    def named_runs(
        self, project: Path, run_ids: Iterable[str]
    ) -> tuple[Path, tuple[host_run_reads.ProjectRun, ...]]: ...
    def run_digest(
        self, project_root: Path, run_id: str
    ) -> host_api.RunDigest | host_api.RunDigestUnreadable | None: ...
    def run_summary(self, project: Path, run_id: str) -> Summarized: ...


class _ReadDirectories(Protocol):
    def expand(self, path: str) -> str: ...
    def home(self) -> str: ...


class _ReadExamples(Protocol):
    def list(self, *, with_catalogue: bool) -> host_examples.ExampleList: ...
    def destination(
        self, example: str, destination: Path
    ) -> tuple[host_examples.ExampleDestination, str | None]: ...


class _ReadCompute(Protocol):
    def settings(self) -> host_compute.ComputeSettings: ...


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
    def logins(self) -> _ReadLogins:
        """Provider-owned login facts."""
        ...

    def harnesses(self) -> tuple[host_api.HarnessOption, ...]:
        """Vetted harness facts from the agent library."""
        ...


class ReadRefusedError(Exception):
    """A probe refused a read with a closed category and diagnostic."""

    def __init__(self, refusal: host_api.HelperError) -> None:
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
        preflight: Callable[
            [host_api.RunPreflightRequest], host_api.RunPreflightReply | host_api.HelperError
        ],
        https: Callable[[], HttpsGet] | None = None,
    ) -> None:
        """Bind host I/O and the existing machine/preflight probe adapters.

        ``https`` builds the host's client for the one fetch it makes, the price list, when a
        refresh asks for it; a host built without one refuses the refresh.
        """
        self._host = host
        self._role_order = role_order
        self._describe = describe
        self._preflight = preflight
        self._https = https

    def _path(self, text: str) -> Path:
        expanded = Path(self._host.directories.expand(text))
        return (
            expanded if expanded.is_absolute() else Path(self._host.directories.home()) / expanded
        )

    def saved_runs(self, project: str) -> tuple[str, Iterable[pb.SavedRun]]:
        """One lazy scan through the existing project catalog owner, in discovery order."""
        root, runs = self._host.projects.stream_runs(self._path(project))
        return str(root), host_saved_runs.saved_run_rows(root, runs)

    def run_digests(self, request: pb.ListRunDigestsRequest) -> pb.ListRunDigestsResponse:
        """Read each run's digest through the owner of the helper's project run listing."""
        runs = host_run_reads.named_run_digests(
            self._host,
            self._path(request.project),
            request.run_ids,
            with_dashboard=request.with_dashboard,
        )
        return host_run_wire.digests_response(request.request_id, runs)

    def usage_window(self, request: pb.UsageWindowRequest) -> pb.UsageWindowResponse:
        """Sum the runs' usage through the owner of the helper's usage window."""
        if request.since is None or request.until is None:
            message = "since and until: a window needs both ends"
            raise ValueError(message)
        legacy = host_run_summary.UsageWindowRequest(
            request_id=request.request_id,
            deadline_ms=wire.host_operations.UsageWindow.total_ms,
            project=request.project,
            run_ids=tuple(request.run_ids),
            since=request.since.to_datetime(),
            until=request.until.to_datetime(),
        )
        reply = host_run_reads.usage_window_reply(legacy, self._host, self._path(request.project))
        return host_run_wire.usage_response(request.request_id, reply)

    def usage_summary(self, request: pb.UsageSummaryRequest) -> pb.UsageSummaryResponse:
        """Cut the host's runs into the request's buckets, through the owners of the rollup and the summary."""
        if request.now is None:
            message = "now: required"
            raise ValueError(message)
        pricing = usage_scan.stored_pricing(
            usage_scan.usage_directory(), now=request.now.to_datetime()
        )
        paths = [self._path(project) for project in request.projects]
        return usage_scan.usage_summary(request, self._host.projects, paths, pricing, os.environ)

    def usage_rates_refresh(
        self, request: pb.UsageRatesRefreshRequest
    ) -> pb.UsageRatesRefreshResponse:
        """Fetch the price list if one is due, through the owner of the host's usage state."""
        if self._https is None or request.now is None:
            raise ReadRefusedError(
                host_api.HelperError(
                    code=host_envelope.HelperErrorCode.INTERNAL,
                    message="this host has no network client to fetch prices with",
                    request_id=request.request_id,
                )
            )
        pricing = usage_scan.refresh_rates(
            usage_scan.usage_directory(),
            usage_scan.HttpsFetcher(self._https()),
            now=request.now.to_datetime(),
            force=request.force,
        )
        return wire.host_payloads.UsageRatesRefreshResponse(
            request_id=request.request_id, pricing=pricing
        )

    def tasks(self, project: str) -> pb.TaskList:
        """Read through the existing project's task owner."""
        return wire.from_mapping(
            wire.host_payloads.TaskList,
            self._host.projects.tasks(self._path(project)).model_dump(mode="json"),
        )

    def logins(self) -> list[pb.AgentLogin]:
        """Project the provider's observed login state without reading secrets."""
        return [
            wire.from_mapping(wire.host_payloads.AgentLogin, login.model_dump(mode="json"))
            for login in self._host.logins.list()
        ]

    def harnesses(self) -> list[pb.HarnessOption]:
        """Project vetted harness facts; the catalog's order is the answer's order."""
        return [
            wire.from_mapping(wire.host_payloads.HarnessOption, option.model_dump(mode="json"))
            for option in self._host.harnesses()
        ]

    def inspect_destination(self, example: str, destination: str) -> pb.InspectDestinationResponse:
        """What creating ``example`` would find at ``destination``."""
        state, project = self._host.examples.destination(example, self._path(destination))
        payloads = wire.host_payloads
        states = {
            host_examples.ExampleDestination.ABSENT: payloads.DestinationState.ABSENT,
            host_examples.ExampleDestination.EMPTY: payloads.DestinationState.EMPTY,
            host_examples.ExampleDestination.EXAMPLE_PROJECT: (
                payloads.DestinationState.EXAMPLE_PROJECT
            ),
            host_examples.ExampleDestination.OCCUPIED: payloads.DestinationState.OCCUPIED,
        }
        return payloads.InspectDestinationResponse(
            state=states[state],
            **({} if project is None else {"project": project}),
        )

    def examples(self, *, with_catalogue: bool) -> pb.ExampleList:
        """Project the checkout owner's example metadata without dropping details."""
        return wire.from_mapping(
            wire.host_payloads.ExampleList,
            self._host.examples.list(with_catalogue=with_catalogue).model_dump(mode="json"),
        )

    def task_goal(self, project: str, task: str) -> pb.TaskGoal:
        """Read the bounded authored task goal."""
        return wire.from_mapping(
            wire.host_payloads.TaskGoal,
            self._host.projects.task_goal(self._path(project), task).model_dump(mode="json"),
        )

    def machine(self) -> pb.MachineDescription:
        """Project the current machine probe snapshot."""
        return wire.from_mapping(
            wire.host_payloads.MachineDescription,
            self._describe().model_dump(mode="json"),
        )

    def compute(self) -> pb.ComputeSettings:
        """Project source-aware settings; this operation performs no mutation."""
        return wire.from_mapping(
            wire.host_payloads.ComputeSettings,
            self._host.compute.settings().model_dump(mode="json"),
        )

    def preflight(self, request: pb.PreflightRunRequest) -> pb.PreflightRunResponse:
        """Revalidate the temporary legacy projection before invoking its probe owner."""
        payload = wire.to_mapping(request)
        # Legacy diagnostics choose the first role in insertion order. Protobuf
        # maps have no ordering, so retain only that display preference here.
        if self._role_order is not None:
            agents = payload["agents"]
            if not isinstance(agents, dict):
                message = "agents: expected a projected role mapping"
                raise TypeError(message)
            payload["agents"] = {role: agents[role] for role in self._role_order}
        legacy = host_api.RunPreflightRequest.model_validate(
            {**payload, "deadline_ms": wire.host_operations.PreflightRun.total_ms}
        )
        result = self._preflight(legacy)
        if isinstance(result, host_api.HelperError):
            raise ReadRefusedError(result)
        return wire.from_mapping(
            wire.host_payloads.PreflightRunResponse,
            result.model_dump(mode="json", exclude={"type"}),
        )


def read_failure_category(code: host_envelope.HelperErrorCode) -> FailureCategory:
    """Map one helper refusal onto the closed failure category.

    Callers branch on the category. ``CLUSTER_READ_ONLY`` is permission denied:
    the file's owner is not this helper.
    """
    failure = wire.FailureCategory
    codes = host_envelope.HelperErrorCode
    categories = {
        codes.INVALID_REQUEST: failure.INVALID_INPUT,
        codes.TASK_INVALID: failure.INVALID_INPUT,
        codes.PATH_NOT_DIRECTORY: failure.INVALID_INPUT,
        codes.UNVETTED_HARNESS: failure.INVALID_INPUT,
        codes.CLUSTER_INVALID: failure.INVALID_INPUT,
        codes.INCOMPATIBLE: failure.CONFIGURATION,
        codes.EXAMPLES_UNAVAILABLE: failure.CONFIGURATION,
        codes.COMPUTE_CONFIG_UNREADABLE: failure.CONFIGURATION,
        codes.MODAL_NOT_CONFIGURED: failure.CONFIGURATION,
        codes.DUPLICATE_REQUEST_ID: failure.CONFLICT,
        codes.DESTINATION_EXISTS: failure.CONFLICT,
        codes.DESTINATION_NESTED: failure.CONFLICT,
        codes.NOT_NESTED: failure.CONFLICT,
        codes.CLUSTER_EXISTS: failure.CONFLICT,
        codes.UNKNOWN_WATCH: failure.NOT_FOUND,
        codes.PROJECT_MISSING: failure.NOT_FOUND,
        codes.PROJECT_UNINITIALIZED: failure.NOT_FOUND,
        codes.PATH_MISSING: failure.NOT_FOUND,
        codes.RUN_GONE: failure.NOT_FOUND,
        codes.UNKNOWN_INSTANCE: failure.NOT_FOUND,
        codes.UNKNOWN_LOG_STREAM: failure.NOT_FOUND,
        codes.UNKNOWN_EXAMPLE: failure.NOT_FOUND,
        codes.UNKNOWN_RUN: failure.NOT_FOUND,
        codes.UNKNOWN_CLUSTER: failure.NOT_FOUND,
        codes.UNKNOWN_PROVIDER: failure.NOT_FOUND,
        codes.PERMISSION_DENIED: failure.PERMISSION_DENIED,
        codes.CONNECT_DENIED: failure.PERMISSION_DENIED,
        codes.CLUSTER_READ_ONLY: failure.PERMISSION_DENIED,
        codes.HANDSHAKE_REQUIRED: failure.UNSUPPORTED,
        codes.FEATURE_NOT_NEGOTIATED: failure.UNSUPPORTED,
        codes.CHANNEL_BUSY: failure.RESOURCE_EXHAUSTED,
        codes.CONNECT_FAILED: failure.TRANSIENT,
        codes.DEADLINE_EXCEEDED: failure.DEADLINE_EXCEEDED,
        codes.LOG_STORE_CORRUPT: failure.INTERNAL,
        codes.PROJECT_CREATE_FAILED: failure.INTERNAL,
        codes.CLUSTER_TEST_FAILED: failure.INTERNAL,
        codes.READER_FAILED: failure.INTERNAL,
        codes.INTERNAL: failure.INTERNAL,
    }
    found = categories.get(code)
    if found is None:
        message = f"unmapped helper code {code}"
        raise ValueError(message)
    return found


def answer_registered_read(cli: str, payload: bytes, source: HostReadSource) -> bytes:
    """Run one registered read for ``vibesys host`` and return the envelope bytes."""
    try:
        return read_operations.execute_host_read(payload, source, cli=cli)
    except ReadRefusedError as error:
        category = read_failure_category(error.refusal.code)
        return read_operations.host_read_failure(category, error.refusal.message)
    except (host_examples.ProjectLookupError, host_compute.ComputeError) as error:
        return read_operations.host_read_failure(read_failure_category(error.code), str(error))


def stream_saved_runs(
    payload: bytes, source: HostReadSource, write: Callable[[bytes], object]
) -> None:
    """Run the saved-run stream for ``vibesys host``; a refusal ends it as a typed failure frame."""
    try:
        saved_runs.serve_saved_runs(payload, source, cli="stream-saved-runs", write=write)
    except host_examples.ProjectLookupError as error:
        write(saved_runs.host_watch_failure(read_failure_category(error.code), str(error)))


__all__ = [
    "HostReadFacts",
    "HostReadSource",
    "ReadRefusedError",
    "answer_registered_read",
    "read_failure_category",
    "stream_saved_runs",
]
