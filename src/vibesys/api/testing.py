"""Fakes for public run handles, launch collections and persisted records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vibesys.api.profilers import ProfilerKind, ProfilerPreflightResult
from vibesys.api.store import (
    RunDocument,
    RunRecordFacts,
    RunRecordReadError,
    WorkspaceChange,
)
from vs_runtime.api.testing import FakeRunHandle, FakeRuns, FakeStopTimer

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.auxiliary import AuxiliaryAgentLaunch, AuxiliaryAgents, ManagedAgent, RunReady
    from vibesys.api.contracts import CoreEvent, RunView
    from vs_runtime.api.infrastructure import SteerTargets


@dataclass
class FakeRunRecord:
    """In-memory semantic run record with deterministic history failures."""

    run_id: str
    run_view: RunView
    record_facts: RunRecordFacts
    targets: SteerTargets | None = None
    """What a steer may name in this run; None is a run with no committed state."""
    identity: str = ""
    history: tuple[RunDocument, ...] = ()
    portable: tuple[RunDocument, ...] = ()
    workspace_change_requests: list[tuple[str, str]] = field(default_factory=list, init=False)
    workspace_patch_requests: list[tuple[str, str, tuple[str, ...]]] = field(
        default_factory=list,
        init=False,
    )
    _changes: dict[tuple[str, str], tuple[WorkspaceChange, ...] | RunRecordReadError] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _patches: dict[tuple[str, str, tuple[str, ...]], str | RunRecordReadError] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        """Fill a deterministic cache identity when none was supplied."""
        if not self.identity:
            self.identity = f"fake:{self.run_id}"

    def view(self) -> RunView:
        """Return the configured run projection."""
        return self.run_view

    def facts(self) -> RunRecordFacts:
        """Return the configured manifest facts."""
        return self.record_facts

    def steer_targets(self) -> SteerTargets | None:
        """Return the configured steer targets."""
        return self.targets

    def history_documents(self) -> tuple[RunDocument, ...]:
        """Return configured policy-selected history documents."""
        return self.history

    def portable_documents(self) -> tuple[RunDocument, ...]:
        """Return configured portable documents."""
        return self.portable

    def workspace_changes(self, base: str, head: str) -> tuple[WorkspaceChange, ...]:
        """Record and answer one semantic workspace-change request."""
        self.workspace_change_requests.append((base, head))
        result = self._changes.get((base, head), ())
        if isinstance(result, RunRecordReadError):
            raise result
        return result

    def workspace_patch(self, base: str, head: str, paths: tuple[str, ...]) -> str:
        """Record and answer one semantic workspace-patch request."""
        self.workspace_patch_requests.append((base, head, paths))
        result = self._patches.get((base, head, paths), "")
        if isinstance(result, RunRecordReadError):
            raise result
        return result

    def set_workspace_changes(
        self,
        base: str,
        head: str,
        *changes: WorkspaceChange,
    ) -> None:
        """Configure semantic changes for one immutable range."""
        self._changes[(base, head)] = changes

    def set_workspace_patch(
        self,
        base: str,
        head: str,
        paths: tuple[str, ...],
        patch: str,
    ) -> None:
        """Configure patch text for one immutable range and path set."""
        self._patches[(base, head, paths)] = patch

    def fail_workspace_changes(
        self,
        base: str,
        head: str,
        error: RunRecordReadError,
    ) -> None:
        """Configure a typed change-read failure for one range."""
        self._changes[(base, head)] = error

    def fail_workspace_patch(
        self,
        base: str,
        head: str,
        paths: tuple[str, ...],
        error: RunRecordReadError,
    ) -> None:
        """Configure a typed patch-read failure for one range and path set."""
        self._patches[(base, head, paths)] = error


@dataclass
class FakeRunStore:
    """A run history of in-memory records, read as the product store reads a project."""

    records: dict[str, FakeRunRecord] = field(default_factory=dict)

    def list_runs(self) -> list[RunView]:
        """Every record's view, most recently added first."""
        return [record.view() for record in reversed(self.records.values())]

    def get_run(self, run_id: str) -> RunView:
        """Return one record's view."""
        return self.get_record(run_id).view()

    def get_record(self, run_id: str) -> FakeRunRecord:
        """Return one record; an unknown run fails as the product store's read does."""
        try:
            return self.records[run_id]
        except KeyError:
            message = f"no run {run_id!r}"
            raise LookupError(message) from None


@dataclass
class FakeProfilerPreflight:
    """A native profiler probe that touches no host: every probe reports ``usable``."""

    usable: bool = True
    probed: list[ProfilerKind] = field(default_factory=list)

    def __call__(self, kind: ProfilerKind, /) -> ProfilerPreflightResult:
        """Record ``kind`` and answer with the scripted usability."""
        self.probed.append(kind)
        return ProfilerPreflightResult(kind, self.usable)


__all__ = [
    "FakeManagedAgent",
    "FakeProfilerPreflight",
    "FakeRunHandle",
    "FakeRunRecord",
    "FakeRunStore",
    "FakeRuns",
    "FakeStopTimer",
]


@dataclass
class FakeManagedAgent:
    """An in-memory conversation with explicit, input-derived model behavior."""

    respond: Callable[[str, str | None], str]
    messages: list[tuple[str, str | None]] = field(default_factory=list, init=False)
    closed: bool = field(default=False, init=False)

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        """Serialize one question into this conversation, refusing use after close."""
        if self.closed:
            detail = "auxiliary conversation is closed"
            raise RuntimeError(detail)
        self.messages.append((message, invocation_id))
        return self.respond(message, invocation_id)

    def close(self) -> None:
        """Idempotently release the conversation."""
        self.closed = True


@dataclass
class FakeAuxiliaryAgents:
    """An explicit auxiliary scope that creates input-derived Fake conversations."""

    create: Callable[[AuxiliaryAgentLaunch], ManagedAgent]
    launches: list[AuxiliaryAgentLaunch] = field(default_factory=list, init=False)
    agents: list[ManagedAgent] = field(default_factory=list, init=False)
    closed: bool = field(default=False, init=False)

    def create_auxiliary_agent(self, launch: AuxiliaryAgentLaunch) -> ManagedAgent:
        if self.closed:
            detail = "auxiliary scope is closed"
            raise RuntimeError(detail)
        self.launches.append(launch)
        agent = self.create(launch)
        self.agents.append(agent)
        return agent

    def close(self) -> None:
        self.closed = True
        for agent in self.agents:
            agent.close()


@dataclass
class FakeRunReader:
    """A read scope with explicit readiness facts and auxiliary ownership."""

    facts: RunReady
    agents: AuxiliaryAgents
    sinks: list[Callable[[CoreEvent], object] | None] = field(default_factory=list, init=False)
    closed: bool = field(default=False, init=False)

    def ready(self) -> RunReady:
        return self.facts

    def open_auxiliary_agents(
        self, *, sink: Callable[[CoreEvent], object] | None = None
    ) -> AuxiliaryAgents:
        self.sinks.append(sink)
        return self.agents

    def close(self) -> None:
        self.closed = True
