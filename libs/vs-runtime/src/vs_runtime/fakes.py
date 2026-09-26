"""Faithful in-memory implementations of the public runtime contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias, TypeVar, overload

from pydantic import BaseModel

from vs_runtime.contracts import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentRole,
    AgentSession,
    BenchmarkEvaluation,
    BenchmarkObjective,
    OrchestrationPlugin,
    RuntimeContractError,
    SessionClosedError,
    UnknownAgentRoleError,
    Workspace,
    WorkspaceRef,
    validate_member_id,
    validate_objectives,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)
TurnResponder: TypeAlias = Callable[
    [AgentRole, tuple[str, ...], str, type[BaseModel] | None], object
]


def _echo_responder(
    _role: AgentRole,
    _history: tuple[str, ...],
    message: str,
    _response: type[BaseModel] | None,
) -> object:
    return message


class FakeAgentSession:
    """In-memory conversation with the public lifetime and context contract."""

    def __init__(
        self,
        role: AgentRole,
        workspace: Workspace,
        member_id: str | None,
        responder: TurnResponder,
    ) -> None:
        """Bind one fresh session to immutable creation configuration."""
        self._role = role
        self._workspace = workspace
        self._member_id = member_id
        self._responder = responder
        self._history: list[str] = []
        self._closed = False

    @property
    def role(self) -> AgentRole:
        """Return the role bound at creation."""
        return self._role

    @property
    def workspace(self) -> Workspace:
        """Return the workspace bound at creation."""
        return self._workspace

    @property
    def member_id(self) -> str | None:
        """Return optional policy attribution for this instance."""
        return self._member_id

    @property
    def closed(self) -> bool:
        """Return whether cleanup has ended this session."""
        return self._closed

    @property
    def history(self) -> tuple[str, ...]:
        """Return completed user messages in conversation order."""
        return tuple(self._history)

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self, message: str, *, response: type[ResponseT] | None = None
    ) -> str | ResponseT:
        """Respond from prior completed turns, then append this message."""
        if self._closed:
            raise SessionClosedError
        value = self._responder(self._role, tuple(self._history), message, response)
        if response is None:
            if not isinstance(value, str):
                message = "text turn responder must return str"
                raise TypeError(message)
            result: str | ResponseT = value
        else:
            result = response.model_validate(value)
        self._history.append(message)
        return result

    async def close(self) -> None:
        """End the fake conversation idempotently."""
        self._closed = True


class FakeAgentSessions:
    """Run-owned in-memory factory with isolated creation semantics."""

    def __init__(
        self,
        agents: tuple[AgentRole, ...],
        *,
        responder: TurnResponder = _echo_responder,
    ) -> None:
        """Build the private role lookup from the plugin's authoritative tuple."""
        self._roles = {role.id: role for role in agents}
        self._responder = responder
        self._sessions: list[FakeAgentSession] = []
        self._closed = False

    @property
    def sessions(self) -> tuple[FakeAgentSession, ...]:
        """Return created sessions in ownership order."""
        return tuple(self._sessions)

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
    ) -> AgentSession:
        """Validate the declared role and create an independent conversation."""
        if self._closed:
            raise SessionClosedError
        if self._roles.get(role.id) != role:
            raise UnknownAgentRoleError(role.id)
        validate_member_id(member_id)
        session = FakeAgentSession(role, workspace, member_id, self._responder)
        self._sessions.append(session)
        return session

    async def close(self) -> None:
        """Close all sessions in reverse creation order, once."""
        if self._closed:
            return
        self._closed = True
        for session in reversed(self._sessions):
            await session.close()


class FakeWorkspaces:
    """In-memory holder for one fake root workspace."""

    def __init__(self, root: Workspace) -> None:
        """Bind the fake capability to one root workspace."""
        self._root = root

    @property
    def root(self) -> Workspace:
        """Return the configured fake root workspace."""
        return self._root


@dataclass(frozen=True, slots=True)
class FakeAccuracyCall:
    """One recorded accuracy evaluation request."""

    workspace: Workspace
    reuse: AccuracyReceipt | None


@dataclass(frozen=True, slots=True)
class FakeBenchmarkCall:
    """One recorded benchmark evaluation request."""

    workspace: Workspace
    objectives: tuple[BenchmarkObjective, ...]


@dataclass(slots=True)
class FakeEvaluation:
    """Scriptable in-memory implementation of trusted evaluation effects."""

    default_accuracy: AccuracyEvaluation = field(
        default_factory=lambda: AccuracyEvaluation(executed=False)
    )
    default_benchmark: BenchmarkEvaluation = field(
        default_factory=lambda: BenchmarkEvaluation(executed=False)
    )
    accuracy_results: list[AccuracyEvaluation] = field(default_factory=list)
    benchmark_results: list[BenchmarkEvaluation] = field(default_factory=list)
    accuracy_calls: list[FakeAccuracyCall] = field(default_factory=list)
    benchmark_calls: list[FakeBenchmarkCall] = field(default_factory=list)
    run_id: str = "test-run"

    def script_accuracy(self, *results: AccuracyEvaluation) -> None:
        """Queue accuracy results in call order."""
        self.accuracy_results.extend(results)

    def script_benchmark(self, *results: BenchmarkEvaluation) -> None:
        """Queue benchmark results in call order."""
        self.benchmark_results.extend(results)

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        """Record the request and return the next scripted result."""
        self.accuracy_calls.append(FakeAccuracyCall(workspace, reuse))
        if reuse is not None:
            if reuse.run_id != self.run_id:
                message = "accuracy receipt belongs to another run"
                raise RuntimeContractError(message)
            if reuse.workspace_id != workspace.id:
                message = "accuracy receipt belongs to another workspace"
                raise RuntimeContractError(message)
            if reuse.revision != "fake-revision":
                message = "accuracy receipt does not match the current workspace revision"
                raise RuntimeContractError(message)
            return AccuracyEvaluation(executed=False, receipt=reuse)
        result = self.accuracy_results.pop(0) if self.accuracy_results else self.default_accuracy
        if not result.passed:
            return result
        receipt = result.receipt or AccuracyReceipt(
            run_id=self.run_id,
            workspace_id=workspace.id,
            revision="fake-revision",
        )
        return result.model_copy(update={"receipt": receipt})

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
    ) -> BenchmarkEvaluation:
        """Record the request and return the next scripted result."""
        validate_objectives(objectives)
        self.benchmark_calls.append(FakeBenchmarkCall(workspace, objectives))
        if self.benchmark_results:
            return self.benchmark_results.pop(0)
        return self.default_benchmark


class FakeRunHost:
    """In-memory run host that owns fake sessions and captured log lines."""

    def __init__(
        self,
        plugin: OrchestrationPlugin,
        *,
        run_id: str = "test-run",
        project_root: Path = Path(),
        responder: TurnResponder = _echo_responder,
    ) -> None:
        """Create a host whose private role map derives from ``plugin.agents``."""
        self._run_id = run_id
        self._workspaces = FakeWorkspaces(WorkspaceRef(path=project_root))
        self._agents = FakeAgentSessions(plugin.agents, responder=responder)
        self._evaluation = FakeEvaluation(run_id=run_id)
        self._logs: list[str] = []
        self._closed = False

    @property
    def run_id(self) -> str:
        """Return this fake run's stable identity."""
        return self._run_id

    @property
    def workspaces(self) -> FakeWorkspaces:
        """Return the fake live-workspace capability."""
        return self._workspaces

    @property
    def agents(self) -> FakeAgentSessions:
        """Return the run-owned fake session factory."""
        return self._agents

    @property
    def evaluation(self) -> FakeEvaluation:
        """Return the scriptable trusted evaluation capability."""
        return self._evaluation

    @property
    def logs(self) -> tuple[str, ...]:
        """Return recorded log messages in call order."""
        return tuple(self._logs)

    def log(self, message: str) -> None:
        """Capture one log message."""
        self._logs.append(message)

    async def close(self) -> None:
        """Close all run-owned resources idempotently."""
        if self._closed:
            return
        self._closed = True
        await self._agents.close()
