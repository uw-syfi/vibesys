"""Faithful in-memory implementations of the public runtime contracts."""

from __future__ import annotations

import asyncio
import inspect
import threading
import uuid
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, TypeAlias, TypeVar, overload

from pydantic import BaseModel, ValidationError

from vs_agent.api import (
    NULL_SKILL_SELECTION,
    AgentOutputSchemaError,
    AgentSessionKey,
    SessionScope,
    describe_validation_error,
)
from vs_evaluation.api import StoredEvaluation
from vs_runtime._agent_declarations import (
    validate_agent_capabilities,
    validate_extra_tools,
)
from vs_runtime._agent_sessions import await_session_operation
from vs_runtime._local_validation import LocalValidationRecipeError, check_recipe_artifact_path
from vs_runtime._trusted_evaluation import TrustedAccuracyResult, TrustedBenchmarkResult
from vs_runtime._workspace_access import WorkspaceAccessRecovery
from vs_runtime.contracts import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentBinding,
    AgentCapability,
    AgentEvaluation,
    AgentRole,
    AgentSession,
    BenchmarkEvaluation,
    BenchmarkObjective,
    CandidateProfile,
    CandidateProfileStatus,
    CandidateWorkspace,
    CommandResult,
    LocalValidationEvaluation,
    OrchestrationPlugin,
    ReleasedJobs,
    ResolvedSkillResources,
    Run,
    RunFacts,
    RuntimeContractError,
    SessionClosedError,
    SessionTransportUnavailableError,
    SkillCatalogError,
    SkillResolution,
    SkillResourceRequest,
    StateModelError,
    StructuredResponseError,
    UnknownAgentRoleError,
    Workspace,
    WorkspaceAccess,
    WorkspaceRestoreError,
    member_workspace_id,
    validate_command,
    validate_member_id,
    validate_objectives,
    validate_trusted_shell_command,
    validate_workspace_writable_paths,
)

if TYPE_CHECKING:
    from vs_agent.api import AgentSessionCheckpoint, AgentSessions, InvocationOutcome
    from vs_evaluation.api import EvaluationSettlements
    from vs_prompts.api import RenderedPrompt
    from vs_runtime._agent_execution import AgentExecutionLifecycleEvent
    from vs_runtime._run_control import RunControlTransition
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox


class FakeAgentExecutionEnvironment:
    """In-memory scoped environment with observable idempotent cleanup."""

    def __init__(
        self,
        *,
        project_path_policy: ProjectPathPolicy,
        host_resources: tuple[HostResource, ...] = (),
        use_docker: bool = False,
        close_log: list[str] | None = None,
        name: str = "environment",
    ) -> None:
        """Configure environment facts and optional cleanup recording."""
        self.skill_source_dirs: tuple[Path, ...] = ()
        self.skill_selection = NULL_SKILL_SELECTION
        self.project_path_policy = project_path_policy
        self.host_resources = host_resources
        self.backends: dict[str, Sandbox] | None = None
        self.use_docker = use_docker
        self.closed = False
        self._close_log = close_log
        self._name = name

    def close(self) -> None:
        """Record cleanup exactly once."""
        if self.closed:
            return
        self.closed = True
        if self._close_log is not None:
            self._close_log.append(self._name)

    def agent_path(self, host_path: Path | str) -> str:
        """Return the identity mapping used by the in-memory environment."""
        return str(Path(host_path))


class FakeAgentExecutionLifecycleSink:
    """Record semantic execution events in emission order."""

    def __init__(self) -> None:
        """Create an empty lifecycle log."""
        self.events: list[AgentExecutionLifecycleEvent] = []

    def __call__(self, event: AgentExecutionLifecycleEvent) -> None:
        """Record one semantic lifecycle event."""
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class FakeTrustedAccuracyCall:
    """One trusted accuracy invocation observed by the Fake."""

    command_override: str | None


@dataclass(frozen=True, slots=True)
class FakeTrustedBenchmarkCall:
    """One trusted benchmark invocation observed by the Fake."""

    command_override: str | None
    required_metrics: frozenset[str]


class FakeTrustedEvaluationExecutor:
    """Scripted in-memory trusted evaluation mechanism."""

    def __init__(self) -> None:
        """Create an executor with passing, unexecuted defaults."""
        self.accuracy_calls: list[FakeTrustedAccuracyCall] = []
        self.benchmark_calls: list[FakeTrustedBenchmarkCall] = []
        self._accuracy_results: list[TrustedAccuracyResult] = []
        self._benchmark_results: list[TrustedBenchmarkResult] = []

    def script_accuracy(self, *results: TrustedAccuracyResult) -> None:
        """Replace queued accuracy outcomes."""
        self._accuracy_results = list(results)

    def script_benchmark(self, *results: TrustedBenchmarkResult) -> None:
        """Replace queued benchmark outcomes."""
        self._benchmark_results = list(results)

    async def accuracy(self, *, command_override: str | None = None) -> TrustedAccuracyResult:
        """Record one call and return the next scripted outcome."""
        self.accuracy_calls.append(FakeTrustedAccuracyCall(command_override))
        if self._accuracy_results:
            return self._accuracy_results.pop(0)
        return TrustedAccuracyResult(executed=False, passed=True)

    async def benchmark(
        self,
        *,
        command_override: str | None = None,
        required_metrics: frozenset[str] = frozenset(),
    ) -> TrustedBenchmarkResult:
        """Record one call and return the next scripted outcome."""
        self.benchmark_calls.append(FakeTrustedBenchmarkCall(command_override, required_metrics))
        if self._benchmark_results:
            return self._benchmark_results.pop(0)
        return TrustedBenchmarkResult(executed=False, passed=True)


ResponseT = TypeVar("ResponseT", bound=BaseModel)
# A responder may return an awaitable: the turn awaits it, so a test can hold a
# turn open the way a long provider turn is, and end it early.
TurnResponder: TypeAlias = Callable[
    [AgentRole, tuple[str, ...], str, type[BaseModel] | None], object
]


class FakeRunControlEventSink:
    """Record synchronous run-control transitions in emission order."""

    def __init__(
        self,
        *,
        on_transition: Callable[[RunControlTransition], None] | None = None,
    ) -> None:
        """Configure an optional deterministic reaction to each transition."""
        self.transitions: list[RunControlTransition] = []
        self._on_transition = on_transition

    def __call__(self, transition: RunControlTransition) -> None:
        """Record one transition and invoke the configured reaction."""
        self.transitions.append(transition)
        if self._on_transition is not None:
            self._on_transition(transition)


class FakeProjectMaterializationEffects:
    """Deterministic environment effects for project materialization tests."""

    def __init__(self, *, isolated: bool = False, removes_children: bool = True) -> None:
        """Configure symlink and privileged-removal behavior."""
        self._isolated = isolated
        self.removes_children = removes_children
        self.repaired: list[Path] = []
        self.removed: list[tuple[Path, str]] = []

    @property
    def isolated(self) -> bool:
        """Return the configured symlink policy."""
        return self._isolated

    def repair(self, workspace: Path) -> None:
        """Record a requested permission repair."""
        self.repaired.append(workspace)

    def remove_child(self, workspace: Path, name: str) -> bool:
        """Record and return the configured privileged-removal outcome."""
        self.removed.append((workspace, name))
        return self.removes_children


class FakeGitRunner:
    """In-memory command effect for pinned project-source checkouts."""

    def __init__(self, *, head: str, fail_command: str | None = None) -> None:
        """Configure the resolved commit and optional failing Git command."""
        self.head = head
        self.fail_command = fail_command
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> str:
        """Apply clone/checkout/rev-parse semantics without a subprocess."""
        command = args[0]
        self.calls.append((tuple(args), cwd))
        if command == self.fail_command:
            message = f"git {command} failed: configured fake failure"
            raise RuntimeError(message)
        if command == "clone":
            destination = Path(args[-1])
            (destination / ".git").mkdir(parents=True)
            return ""
        if command == "checkout":
            return ""
        if command == "rev-parse":
            return f"{self.head}\n"
        message = f"unsupported fake Git command: {command}"
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class FakeModelVolumeRequest:
    """One model-volume effect observed by a fake provisioner."""

    model_id: str
    revision: str | None


class FakeModelVolumeProvisioner:
    """Deterministic in-memory model-volume provisioner for composition tests."""

    def __init__(self) -> None:
        """Create an empty successful provisioner."""
        self._requests: list[FakeModelVolumeRequest] = []
        self._volumes: dict[FakeModelVolumeRequest, str] = {}
        self._failure: Exception | None = None

    @property
    def requests(self) -> tuple[FakeModelVolumeRequest, ...]:
        """Return provision attempts in call order."""
        return tuple(self._requests)

    def fail_with(self, error: Exception | None) -> None:
        """Configure an error for subsequent provision attempts."""
        self._failure = error

    def __call__(
        self,
        model_id: str,
        *,
        revision: str | None = None,
        log: Callable[[str], object] = print,
    ) -> str:
        """Record one request and return a stable volume name for its identity."""
        del log
        request = FakeModelVolumeRequest(model_id=model_id, revision=revision)
        self._requests.append(request)
        if self._failure is not None:
            raise self._failure
        if request not in self._volumes:
            self._volumes[request] = f"fake-model-volume-{len(self._volumes) + 1}"
        return self._volumes[request]


class _UnknownWorkspaceRevisionError(ValueError):
    def __init__(self, revision: str) -> None:
        super().__init__(f"workspace revision is not retained: {revision!r}")


class _WorkspaceRetentionLabelError(ValueError):
    def __init__(self) -> None:
        super().__init__("workspace retention label must be nonempty")


@dataclass(frozen=True)
class _FakeSessionConfig:
    """Immutable creation options shared by a fake session."""

    member_id: str | None
    writable_paths: tuple[str, ...]
    writable_directory_paths: tuple[str, ...]
    #: The resumed conversation's history, shared with earlier sessions.
    history: list[str] | None = None
    session_transport: AgentSessions | None = None


@dataclass(frozen=True)
class _FakeCandidateConfig:
    """Creation state copied into one isolated fake workspace."""

    workspace_id: str
    path: Path
    revision: str
    trusted_input_baseline: str | None
    known_revisions: set[str]
    revision_prefix: str


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
        workspace: FakeWorkspace,
        binding: AgentBinding,
        responder: TurnResponder,
        config: _FakeSessionConfig,
    ) -> None:
        """Bind a session to its configuration and, if resumed, its conversation."""
        self._session_transport = config.session_transport
        self._session_key = AgentSessionKey(
            SessionScope.MEMBER if config.member_id is not None else SessionScope.ROLE,
            f"{role.id}:{config.member_id}"
            if config.member_id is not None
            else f"session:{uuid.uuid4().hex}",
        )
        self._role = role
        self._workspace = workspace
        self._member_id = config.member_id
        self._writable_paths = config.writable_paths
        self._writable_directory_paths = config.writable_directory_paths
        self._binding = binding
        self._responder = responder
        self._history: list[str] = [] if config.history is None else config.history
        self._turn_number = 0
        self._turn_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

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
        """Return the durable policy identity for this instance."""
        return self._member_id

    @property
    def writable_paths(self) -> tuple[str, ...]:
        """Return the immutable session-specific write grants."""
        return self._writable_paths

    @property
    def binding(self) -> AgentBinding:
        """Return the configured immutable runtime attribution."""
        return self._binding

    @property
    def closed(self) -> bool:
        """Return whether cleanup has ended this session."""
        return self._closed

    @property
    def history(self) -> tuple[str, ...]:
        """Return completed user messages in conversation order."""
        return tuple(self._history)

    @property
    def retains_session_key(self) -> bool:
        """Retain exclusive ownership through pending or failed cleanup."""
        task = self._close_task
        return task is None or not task.done() or task.cancelled() or task.exception() is not None

    @property
    def session_key(self) -> AgentSessionKey:
        """Return the same identity production binds for member sessions."""
        return self._session_key

    def _transport(self) -> AgentSessions:
        if self._session_transport is None:
            message = "durable agent session transport is not configured"
            raise SessionTransportUnavailableError(message)
        return self._session_transport

    def checkpoint(self) -> AgentSessionCheckpoint:
        """Read checkpoint identity from the injected agent session interface."""
        return self._transport().checkpoint(self._session_key)

    def inspect(self, invocation_id: str) -> InvocationOutcome:
        """Preserve the owning interface's explicit invocation outcome."""
        return self._transport().inspect(self._session_key, invocation_id)

    async def resume(
        self,
        message: RenderedPrompt,
        invocation_id: str,
        *,
        response: type[BaseModel] | None = None,
    ) -> InvocationOutcome:
        """Resume with production-equivalent workspace isolation."""
        del response
        if self._closed:
            raise SessionClosedError
        async with self._turn_lock:
            if self._closed:
                raise SessionClosedError
            transport = self._transport()
            revision = await self._workspace.snapshot("session-resume-input")
            try:
                outcome = await await_session_operation(
                    asyncio.create_task(
                        asyncio.to_thread(
                            transport.resume, self._session_key, message, invocation_id
                        )
                    )
                )
            finally:
                await await_session_operation(
                    asyncio.create_task(self._enforce_workspace_access(revision))
                )
            if (
                self._role.workspace_access is WorkspaceAccess.READ_WRITE
                or await self._workspace.pending_changes()
            ):
                await self._workspace.snapshot("session-resume")
            return outcome

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self, message: str, *, response: type[ResponseT] | None = None
    ) -> str | ResponseT:
        """Serialize turns, respond from completed history, and enforce access."""
        if self._closed:
            raise SessionClosedError
        async with self._turn_lock:
            if self._closed:
                raise SessionClosedError
            return await self._turn_once(message, response=response)

    async def _turn_once(
        self,
        message: str,
        *,
        response: type[ResponseT] | None,
    ) -> str | ResponseT:
        self._turn_number += 1
        label = f"{self._role.id}-session-turn-{self._turn_number}"
        revision = await self._workspace.snapshot(f"{label}-input")
        try:
            result = await self._respond(message, response)
        except StructuredResponseError:
            # Production keeps the conversation after an invalid structured
            # reply, so the correction turn sees this message in its history.
            self._history.append(message)
            raise
        finally:
            remaining_changes = await self._enforce_workspace_access(revision)
        self._history.append(message)
        if self._role.workspace_access is WorkspaceAccess.READ_WRITE or remaining_changes:
            await self._workspace.snapshot(label)
        return result

    async def _respond(self, message: str, response: type[ResponseT] | None) -> str | ResponseT:
        """Answer one turn, reporting invalid structured output as production does."""
        try:
            value = self._responder(self._role, tuple(self._history), message, response)
            if inspect.isawaitable(value):
                value = await value
        except AgentOutputSchemaError as error:
            if response is None:
                raise
            raise StructuredResponseError(self._role.id, response, detail=error.detail) from error
        if response is None:
            if not isinstance(value, str):
                error = "text turn responder must return str"
                raise TypeError(error)
            return value
        try:
            return response.model_validate(value)
        except ValidationError as error:
            raise StructuredResponseError(
                self._role.id, response, detail=describe_validation_error(error)
            ) from error

    async def _enforce_workspace_access(self, revision: str) -> list[str]:
        if self._role.workspace_access is not WorkspaceAccess.READ_WRITE:
            limited = self._role.workspace_access is WorkspaceAccess.LIMITED
            self._workspace.access_recovery.begin(
                revision,
                self._role.id,
                self._writable_paths if limited else (),
                self._writable_directory_paths if limited else (),
            )
        if self._role.workspace_access is WorkspaceAccess.READ_WRITE:
            return await self._workspace.pending_changes()
        result = await self._workspace.access_recovery.reconcile(self._workspace)
        return result.pending_changes

    async def close(self) -> None:
        """Reject more work and wait for the active turn before closing."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        async with self._turn_lock:
            return

    def mark_closed(self) -> None:
        """Reject new and already-queued turns before owner cleanup starts."""
        self._closed = True


class FakeWorkspaceAgentSessions:
    """Run-owned in-memory factory with isolated creation semantics."""

    def __init__(
        self,
        agents: tuple[AgentRole, ...],
        *,
        responder: TurnResponder = _echo_responder,
        bindings: dict[str, AgentBinding] | None = None,
        supported_extra_tools: Collection[str] | None = None,
        supported_agent_capabilities: Collection[AgentCapability] | None = None,
    ) -> None:
        """Build role lookup, optionally restricting simulated driver support."""
        self._session_transport: AgentSessions | None = None
        self._roles = {role.id: role for role in agents}
        self._responder = responder
        self._bindings = bindings or {
            role.id: AgentBinding(backend="fake", driver="fake", provider="fake") for role in agents
        }
        self._supported_extra_tools = frozenset(supported_extra_tools or ())
        default_capabilities = {AgentCapability.SESSION_REUSE}
        if self._supported_extra_tools:
            default_capabilities.add(AgentCapability.MCP_SERVERS)
        self._supported_agent_capabilities = frozenset(
            default_capabilities
            if supported_agent_capabilities is None
            else supported_agent_capabilities
        )
        # Keep observations separate from live ownership. Candidate teardown
        # removes sessions from the live set, but tests still need to inspect
        # what the public fake created and whether runtime ownership closed it.
        self._sessions: list[FakeAgentSession] = []
        self._active_sessions: list[FakeAgentSession] = []
        # Provider conversations by (role, member): the working directory the
        # conversation ran in and its shared history. Providers key resumable
        # history by working directory, so a member session continues only
        # from the same path.
        self._conversations: dict[tuple[str, str], tuple[Path, list[str]]] = {}
        self._creation_results: list[BaseException | None] = []
        self._closing = False
        self._closed = False

    def bind_session_transport(self, transport: AgentSessions) -> None:
        """Bind the owning agent interface before creating workspace sessions."""
        self._session_transport = transport

    @property
    def sessions(self) -> tuple[FakeAgentSession, ...]:
        """Return created sessions in ownership order."""
        return tuple(self._sessions)

    def script_creation(self, *results: BaseException | None) -> None:
        """Queue deterministic session-creation successes or failures."""
        self._creation_results.extend(results)

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        """Validate the declared role and create an independent conversation."""
        if self._closing:
            raise SessionClosedError
        if self._roles.get(role.id) != role:
            raise UnknownAgentRoleError(role.id)
        if (
            member_id is not None
            and AgentCapability.DURABLE_TURN_CONTINUATION in role.required_capabilities
            and any(
                session.retains_session_key
                and session.role == role
                and session.member_id == member_id
                for session in self._active_sessions
            )
        ):
            message = f"durable session {role.id}:{member_id} already has a live owner"
            raise RuntimeContractError(message)
        validate_member_id(member_id)
        bound_tool_ids = validate_extra_tools(role, self._supported_extra_tools)
        validate_agent_capabilities(
            role,
            self._supported_agent_capabilities,
            member_id=member_id,
            has_bound_tools=bool(bound_tool_ids),
        )
        validated_paths = validate_workspace_writable_paths(
            role.workspace_access,
            writable_paths,
        )
        if not isinstance(workspace, FakeWorkspace):
            message = "workspace is not owned by this fake runtime"
            raise TypeError(message)
        if self._creation_results:
            failure = self._creation_results.pop(0)
            if failure is not None:
                raise failure
        session = FakeAgentSession(
            role,
            workspace,
            self._bindings[role.id],
            self._responder,
            _FakeSessionConfig(
                member_id,
                validated_paths,
                tuple(path for path in validated_paths if workspace.is_directory(path)),
                self._member_history(role, member_id, workspace.path),
                self._session_transport,
            ),
        )
        self._sessions.append(session)
        self._active_sessions.append(session)
        return session

    def _member_history(
        self, role: AgentRole, member_id: str | None, path: Path
    ) -> list[str] | None:
        """Return the conversation a member session resumes, or start one."""
        if member_id is None:
            return None
        key = (role.id, member_id)
        previous = self._conversations.get(key)
        if previous is not None and previous[0] == path:
            return previous[1]
        history: list[str] = []
        self._conversations[key] = (path, history)
        return history

    async def close(self) -> None:
        """Close all sessions in reverse creation order, once."""
        if self._closed:
            return
        self._closing = True
        self._closed = True
        for session in self._active_sessions:
            session.mark_closed()
        for session in reversed(self._active_sessions):
            await session.close()
        self._active_sessions.clear()

    def begin_close(self) -> None:
        """Reject new sessions before asynchronous teardown starts."""
        self._closing = True
        for session in self._active_sessions:
            session.mark_closed()

    async def close_workspace_sessions(self, workspace: Workspace) -> None:
        """Invalidate every public session bound to a discarded workspace."""
        sessions = [session for session in self._active_sessions if session.workspace is workspace]
        for session in sessions:
            session.mark_closed()
        for session in reversed(sessions):
            await session.close()
        selected = set(sessions)
        self._active_sessions = [
            session for session in self._active_sessions if session not in selected
        ]


class FakeWorkspaces:
    """In-memory owner of one root and its isolated candidate workspaces."""

    def __init__(
        self,
        root: FakeWorkspace,
        *,
        supports_parallel_candidates: bool = False,
        sessions: FakeWorkspaceAgentSessions | None = None,
    ) -> None:
        """Bind the fake capability to one root and a fixed isolation capability."""
        self._root = root
        self._supports_parallel_candidates = supports_parallel_candidates
        self._sessions = sessions
        self._candidates: list[FakeCandidateWorkspace] = []
        self._patches: dict[str, str] = {}
        self._default_patch: str | None = None
        self._candidate_retains: list[tuple[BaseException | None, bool]] = []
        self.export_patch_calls: list[str] = []
        self._closing = False
        self._closed = False

    def script_candidate_retain(
        self, *results: BaseException | None, after_retention: bool = False
    ) -> None:
        """Script retention acknowledgements for the next created candidate."""
        self._candidate_retains.extend((result, after_retention) for result in results)

    @property
    def root(self) -> FakeWorkspace:
        """Return the configured fake root workspace."""
        return self._root

    @property
    def supports_parallel_candidates(self) -> bool:
        """Return the fixed candidate-isolation capability."""
        return self._supports_parallel_candidates

    @property
    def candidates(self) -> tuple[FakeCandidateWorkspace, ...]:
        """Return candidates in creation order, including discarded ones."""
        return tuple(self._candidates)

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
    ) -> CandidateWorkspace:
        """Create one isolated workspace from a known root revision."""
        if self._closing or self._closed:
            raise SessionClosedError
        if not self._supports_parallel_candidates:
            message = "this run does not support parallel candidate workspaces"
            raise RuntimeContractError(message)
        revision = from_revision or self._root.revision
        if revision is None or not self._root.knows_revision(revision):
            raise WorkspaceRestoreError(from_revision or "")
        if member_id is None:
            workspace_id = f"candidate-{len(self._candidates) + 1}"
        else:
            workspace_id = member_workspace_id(member_id)
            if any(
                candidate.id == workspace_id and not candidate.discarded
                for candidate in self._candidates
            ):
                message = f"member {member_id!r} already has a live candidate workspace"
                raise RuntimeContractError(message)
        candidate = FakeCandidateWorkspace(
            owner=self,
            invalidate_sessions=(
                self._sessions.close_workspace_sessions if self._sessions is not None else None
            ),
            config=_FakeCandidateConfig(
                workspace_id=workspace_id,
                path=self._root.path / workspace_id,
                revision=revision,
                trusted_input_baseline=self._root.trusted_input_baseline,
                known_revisions=self._root.known_revisions,
                revision_prefix=f"candidate-{len(self._candidates) + 1}",
            ),
        )
        for result, after_retention in self._candidate_retains:
            candidate.script_retain(result, after_retention=after_retention)
        self._candidate_retains.clear()
        self._candidates.append(candidate)
        return candidate

    async def adopt(self, revision: str) -> None:
        """Adopt a retained revision even after its candidate was discarded."""
        if not self._root.knows_revision(revision):
            message = f"candidate revision is not retained by this run: {revision!r}"
            raise RuntimeContractError(message)
        await self._root.restore(revision)

    async def export_patch(self, revision: str) -> str:
        """Export one retained revision against the fake trusted baseline."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self.export_patch_calls.append(revision)
        default = self._default_patch or f"patch for {revision}"
        return self._patches.get(revision, default)

    def set_patch(self, revision: str, patch: str) -> None:
        """Configure the canonical patch exported for a retained revision."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self._patches[revision] = patch

    def set_default_patch(self, patch: str) -> None:
        """Export ``patch`` for every revision without its own patch.

        Models snapshots that differ only in commits, not in content.
        """
        self._default_patch = patch

    def retain_candidate_revision(self, revision: str) -> None:
        """Keep a snapshotted candidate revision reachable from the root."""
        self._root.add_retained_revision(revision)

    def begin_close(self) -> None:
        """Reject new candidates and sessions before asynchronous teardown."""
        if self._closing:
            return
        self._closing = True
        if self._sessions is not None:
            self._sessions.begin_close()

    async def close(self) -> None:
        """Discard candidates, then close remaining root sessions, once."""
        if self._closed:
            return
        self.begin_close()
        self._closed = True
        for candidate in reversed(self._candidates):
            await candidate.discard()
        if self._sessions is not None:
            await self._sessions.close()


@dataclass(frozen=True, slots=True)
class FakeCommandCall:
    """One recorded sandboxed command request."""

    argv: tuple[str, ...]
    workspace: Workspace
    timeout_seconds: int | None
    output_argument: str | None = None


@dataclass(frozen=True, slots=True)
class FakeTrustedShellCall:
    """One recorded trusted shell recipe request."""

    command: str
    workspace: Workspace
    timeout_seconds: int | None


@dataclass(slots=True)
class FakeCommands:
    """Scriptable in-memory command execution with contract validation."""

    results: list[CommandResult | BaseException] = field(default_factory=list)
    calls: list[FakeCommandCall] = field(default_factory=list)
    trusted_shell_calls: list[FakeTrustedShellCall] = field(default_factory=list)

    def script(self, *results: CommandResult | BaseException) -> None:
        """Queue command results in invocation order."""
        self.results.extend(results)

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Validate and record one command, returning its scripted result."""
        validate_command(argv, timeout_seconds)
        self.calls.append(FakeCommandCall(argv, workspace, timeout_seconds))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Record one runtime-managed output capture and return its script."""
        validate_command(argv, timeout_seconds)
        validate_command((output_argument,), None)
        self.calls.append(FakeCommandCall(argv, workspace, timeout_seconds, output_argument))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)

    async def run_trusted_shell(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Validate and record one audited shell recipe."""
        validate_trusted_shell_command(command, timeout_seconds)
        self.trusted_shell_calls.append(FakeTrustedShellCall(command, workspace, timeout_seconds))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)


@dataclass(slots=True)
class FakeSkills:
    """In-memory skill catalog with the production resolver's observable rules."""

    installed_resources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    catalog_error: str | None = None

    async def resolve(self, requests: tuple[SkillResourceRequest, ...]) -> SkillResolution:
        """Resolve requests against installed names and their available files."""
        if not requests:
            return SkillResolution()
        if self.catalog_error is not None:
            raise SkillCatalogError(self.catalog_error)
        if not self.installed_resources:
            return SkillResolution(diagnostics=("no skill sources are installed",))

        return _resolve_fake_skills(requests, self.installed_resources)


def _resolve_fake_skills(
    requests: tuple[SkillResourceRequest, ...], installed: dict[str, tuple[str, ...]]
) -> SkillResolution:
    """Mirror production name merging and partial resource validation in memory."""
    merged: dict[str, tuple[str, list[str]]] = {}
    diagnostics: list[str] = []
    for index, request in enumerate(requests, start=1):
        name = request.name.strip()
        if name not in installed:
            diagnostics.append(f"selection #{index}: unknown installed skill {name!r}")
            continue
        if name not in merged:
            merged[name] = (request.purpose.strip(), [])
        purpose, resources = merged[name]
        diagnostics.extend(
            _resolve_fake_resources(
                request.resource_paths,
                name=name,
                selection_index=index,
                available=set(installed[name]),
                resources=resources,
            )
        )
        merged[name] = purpose, resources

    return SkillResolution(
        resolved=tuple(
            ResolvedSkillResources(
                name=name,
                router_path=f"{name}/SKILL.md",
                resource_paths=tuple(f"{name}/{path}" for path in resources),
                purpose=purpose,
            )
            for name, (purpose, resources) in merged.items()
        ),
        diagnostics=tuple(diagnostics),
    )


def _resolve_fake_resources(
    paths: tuple[str, ...],
    *,
    name: str,
    selection_index: int,
    available: set[str],
    resources: list[str],
) -> list[str]:
    """Validate one request's paths using the catalog's in-memory file set."""
    diagnostics: list[str] = []
    for raw_path in paths:
        resource = raw_path.strip()
        path = PurePosixPath(resource)
        if (
            not resource
            or "\\" in resource
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
        ):
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource path must be relative and stay within the skill"
            )
            continue
        if any(part in {".git", "repos", "__pycache__"} for part in path.parts):
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource path is excluded from agent skill materialization"
            )
            continue
        resource = path.as_posix()
        if resource not in available:
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource file does not exist"
            )
            continue
        if resource != "SKILL.md" and resource not in resources:
            resources.append(resource)
    return diagnostics


class FakeWorkspace:
    """In-memory live workspace with revision and retention semantics."""

    def __init__(
        self,
        *,
        path: Path = Path(),
        workspace_id: str | None = None,
        revision: str | None = "fake-revision",
        trusted_input_baseline: str | None = None,
        known_revisions: set[str] | None = None,
    ) -> None:
        """Create a workspace at one recorded tree and immutable baseline."""
        self.access_recovery = WorkspaceAccessRecovery()
        self._path = path
        self._id = workspace_id
        self._revision = revision
        self._tree_revision = revision
        self._trusted_input_baseline = (
            revision if trusted_input_baseline is None else trusted_input_baseline
        )
        self._known_revisions = set(known_revisions or ()) | {
            value for value in (revision, self._trusted_input_baseline) if value
        }
        self._snapshot_count = 0
        # Revision names stay unique when a member-keyed candidate reuses an ID.
        self._revision_prefix = workspace_id or "fake"
        self._retained: dict[str, str] = {}
        self._retain_results: list[tuple[BaseException | None, bool]] = []
        self._pending_changes: list[list[str]] = []
        self._directories: set[str] = set()
        self.restore_calls: list[tuple[str, bool]] = []
        self.agent_restore_calls: list[tuple[str, tuple[str, ...]]] = []

    @property
    def id(self) -> str | None:
        """Return the configured workspace identity."""
        return self._id

    @property
    def path(self) -> Path:
        """Return the configured host path without touching the filesystem."""
        return self._path

    @property
    def revision(self) -> str | None:
        """Return the latest recorded fake revision."""
        return self._revision

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the immutable configured trusted-input baseline."""
        return self._trusted_input_baseline

    @property
    def retained(self) -> dict[str, str]:
        """Return policy labels and revisions retained so far."""
        return dict(self._retained)

    async def snapshot(self, label: str) -> str:
        """Record a deterministic new revision for the current fake tree."""
        await self.access_recovery.reconcile(self)
        del label
        self._snapshot_count += 1
        revision = f"{self._revision_prefix}-revision-{self._snapshot_count}"
        self._revision = revision
        self._tree_revision = revision
        self._known_revisions.add(revision)
        return revision

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Materialize a known tree while leaving recorded history unchanged."""
        self.restore_calls.append((revision, clean))
        if revision not in self._known_revisions:
            raise WorkspaceRestoreError(revision)
        self._tree_revision = revision

    def script_pending_changes(self, *changes: list[str]) -> None:
        """Queue workspace mutation observations in invocation order."""
        self._pending_changes.extend(changes)

    async def pending_changes(self) -> list[str]:
        """Return the next scripted uncommitted-change listing."""
        if self._pending_changes:
            return self._pending_changes.pop(0)
        return []

    async def restore_for_agent(
        self,
        revision: str,
        *,
        preserve_paths: tuple[str, ...],
    ) -> None:
        """Record an isolation restore while preserving only explicit grants."""
        if revision not in self._known_revisions:
            raise WorkspaceRestoreError(revision)
        self._tree_revision = revision
        self.agent_restore_calls.append((revision, preserve_paths))

    def declare_directories(self, *paths: str) -> None:
        """Declare which validated writable paths represent directories."""
        self._directories.update(paths)

    def is_directory(self, path: str) -> bool:
        """Return whether a writable grant covers descendants of *path*."""
        return path in self._directories

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Return whether a known tree could be materialized."""
        try:
            await self.restore(revision, clean=clean)
        except WorkspaceRestoreError:
            return False
        return True

    def script_retain(self, *results: BaseException | None, after_retention: bool = False) -> None:
        """Script errors before retention or after its durable acknowledgement boundary."""
        self._retain_results.extend((result, after_retention) for result in results)

    async def retain(self, revision: str, *, label: str) -> None:
        """Retain a known revision under a nonempty semantic label."""
        if revision not in self._known_revisions:
            raise _UnknownWorkspaceRevisionError(revision)
        if not label:
            raise _WorkspaceRetentionLabelError
        error, after_retention = (
            self._retain_results.pop(0) if self._retain_results else (None, False)
        )
        if error is not None and not after_retention:
            raise error
        self._retained[label] = revision
        if error is not None:
            raise error

    def knows_revision(self, revision: str) -> bool:
        """Return whether this fake can materialize a revision."""
        return revision in self._known_revisions

    @property
    def known_revisions(self) -> set[str]:
        """Return a copy of the revisions reachable from this fake workspace."""
        return set(self._known_revisions)

    def add_retained_revision(self, revision: str) -> None:
        """Make an externally retained revision materializable."""
        self._known_revisions.add(revision)


class FakeCandidateWorkspace(FakeWorkspace):
    """Faithful isolated fake with explicit, idempotent lifetime."""

    def __init__(
        self,
        *,
        owner: FakeWorkspaces,
        invalidate_sessions: Callable[[Workspace], Awaitable[None]] | None,
        config: _FakeCandidateConfig,
    ) -> None:
        """Bind a candidate to the one fake run that created it."""
        super().__init__(
            workspace_id=config.workspace_id,
            path=config.path,
            revision=config.revision,
            trusted_input_baseline=config.trusted_input_baseline,
            known_revisions=config.known_revisions,
        )
        self._owner = owner
        self._invalidate_sessions = invalidate_sessions
        self._discarded = False
        self._revision_prefix = config.revision_prefix

    @property
    def discarded(self) -> bool:
        """Return whether the isolated workspace has been released."""
        return self._discarded

    @property
    def path(self) -> Path:
        """Return the isolated path while its resources are live."""
        self._require_open()
        return super().path

    @property
    def revision(self) -> str | None:
        """Return the recorded candidate revision while resources are live."""
        self._require_open()
        return super().revision

    async def snapshot(self, label: str) -> str:
        """Record a candidate revision while the workspace is live."""
        self._require_open()
        revision = await super().snapshot(label)
        self._owner.retain_candidate_revision(revision)
        return revision

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Restore a candidate revision while the workspace is live."""
        self._require_open()
        await super().restore(revision, clean=clean)

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Try to restore a candidate revision while the workspace is live."""
        self._require_open()
        return await super().try_restore(revision, clean=clean)

    async def retain(self, revision: str, *, label: str) -> None:
        """Retain a candidate revision while the workspace is live."""
        self._require_open()
        await super().retain(revision, label=label)

    async def discard(self) -> None:
        """Release this fake candidate idempotently."""
        if self._discarded:
            return
        if self._invalidate_sessions is not None:
            await self._invalidate_sessions(self)
        self._discarded = True

    def _require_open(self) -> None:
        """Reject operations whose isolated resources no longer exist."""
        if self._discarded:
            message = "candidate workspace is closed"
            raise RuntimeContractError(message)


class FakeControl:
    """Deterministic cooperative-control boundary for plugin tests."""

    def __init__(self) -> None:
        """Create an active control with no pending failure."""
        self._failure: BaseException | None = None
        self._checkpoints = 0

    @property
    def checkpoints(self) -> int:
        """Return how many checkpoints plugin control flow reached."""
        return self._checkpoints

    def fail_with(self, error: BaseException | None) -> None:
        """Configure the error raised at subsequent checkpoints, or clear it."""
        self._failure = error

    async def checkpoint(self) -> None:
        """Record the boundary and raise its configured stop failure, if any."""
        self._checkpoints += 1
        if self._failure is not None:
            raise self._failure


@dataclass(frozen=True, slots=True)
class FakeStateCommit:
    """One validated state replacement recorded by :class:`FakeState`."""

    value: BaseModel
    workspace: Workspace | None
    label: str | None


class FakeState:
    """Faithful in-memory plugin-state durability with detached values."""

    def __init__(self, model: type[BaseModel] | None, root: Workspace) -> None:
        """Bind the exact plugin declaration and its only live workspace."""
        self._model = model
        self._root = root
        self._value: BaseModel | None = None
        self._commits: list[FakeStateCommit] = []
        self._commit_results: list[BaseException | None] = []
        self._commit_labels: dict[str, list[BaseException | None]] = {}

    @property
    def commits(self) -> tuple[FakeStateCommit, ...]:
        """Return detached commit records in durability order."""
        return tuple(
            FakeStateCommit(
                type(commit.value).model_validate_json(
                    commit.value.model_dump_json(round_trip=True), strict=True
                ),
                commit.workspace,
                commit.label,
            )
            for commit in self._commits
        )

    def script_commit(self, *results: BaseException | None) -> None:
        """Queue deterministic durable-commit successes or failures."""
        self._commit_results.extend(results)

    def script_commit_at(self, label: str, *results: BaseException | None) -> None:
        """Inject one-shot durable-write outcomes at an explicit transition barrier."""
        self._commit_labels.setdefault(label, []).extend(results)

    async def load(self, model: type[ResponseT]) -> ResponseT | None:
        """Return a detached value after validating the exact declared model."""
        self._require_model(model)
        if self._value is None:
            return None
        return model.model_validate_json(self._value.model_dump_json(round_trip=True), strict=True)

    async def commit(
        self,
        value: BaseModel,
        *,
        workspace: Workspace | None = None,
        label: str | None = None,
    ) -> None:
        """Record one deep-validated replacement and optional root association."""
        self._require_model(type(value))
        if workspace is not None and workspace is not self._root:
            message = "state can commit only the live root workspace for this run"
            raise RuntimeContractError(message)
        model = self._model
        if model is None:
            raise StateModelError(None, type(value))
        snapshot = model.model_validate_json(value.model_dump_json(round_trip=True), strict=True)
        labelled = self._commit_labels.get(label or "", [])
        if labelled:
            failure = labelled.pop(0)
            if failure is not None:
                raise failure
        elif self._commit_results:
            failure = self._commit_results.pop(0)
            if failure is not None:
                raise failure
        self._value = snapshot
        recorded = model.model_validate_json(snapshot.model_dump_json(round_trip=True), strict=True)
        self._commits.append(FakeStateCommit(recorded, workspace, label))

    def _require_model(self, model: type[BaseModel]) -> None:
        if model is not self._model:
            raise StateModelError(self._model, model)


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


@dataclass(frozen=True, slots=True)
class FakeProfileCall:
    """One recorded policy-requested profile."""

    revision: str
    request: str
    member_id: str


@dataclass(frozen=True, slots=True)
class FakeLocalValidationCall:
    """One recorded candidate-authored local validation request."""

    workspace: Workspace
    recipe_artifact: str
    report_location: str


FakeEvaluationKind: TypeAlias = Literal["accuracy", "benchmark"]


class FakeEvaluationGate:
    """Holds one trusted evaluation call until the test releases it.

    The gated call sets :attr:`entered` when it starts and then waits for
    :meth:`release`, so a test can order events across concurrent work without
    yielding a counted number of times or reading a clock.
    """

    def __init__(self) -> None:
        """Create an unreleased gate."""
        self.entered = asyncio.Event()
        self._release = asyncio.Event()
        self.cancelled_while_live = False
        self.finished = False

    @property
    def released(self) -> bool:
        """Return whether the test has released the held call."""
        return self._release.is_set()

    def release(self) -> None:
        """Let the held call continue to its scripted outcome."""
        self._release.set()

    async def hold(self, workspace: Workspace) -> None:
        """Wait for release, noting a cancellation that arrives while ``workspace`` is live."""
        self.entered.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            discarded = isinstance(workspace, FakeCandidateWorkspace) and workspace.discarded
            self.cancelled_while_live = not discarded
            raise
        finally:
            self.finished = True


@dataclass(slots=True)
class FakeEvaluation:
    """Scriptable in-memory implementation of trusted evaluation effects.

    Scripted results are consumed in call order; a scripted exception is raised
    by the call that consumes it. Benchmarks of the root workspace (the run's
    input, whose ``id`` is ``None``) consume :attr:`root_benchmark_results`
    first, so a test can script the input and candidates independently of how
    their calls interleave. A gate holds the n-th call of one kind (counted
    from zero across all workspaces) until the test releases it.
    """

    default_accuracy: AccuracyEvaluation = field(
        default_factory=lambda: AccuracyEvaluation(executed=False)
    )
    default_benchmark: BenchmarkEvaluation = field(
        default_factory=lambda: BenchmarkEvaluation(executed=False)
    )
    default_local_validation: LocalValidationEvaluation = field(
        default_factory=lambda: LocalValidationEvaluation(passed=True)
    )
    accuracy_results: list[AccuracyEvaluation | BaseException] = field(default_factory=list)
    benchmark_results: list[BenchmarkEvaluation | BaseException] = field(default_factory=list)
    root_benchmark_results: list[BenchmarkEvaluation | BaseException] = field(default_factory=list)
    local_validation_results: list[LocalValidationEvaluation] = field(default_factory=list)
    accuracy_calls: list[FakeAccuracyCall] = field(default_factory=list)
    benchmark_calls: list[FakeBenchmarkCall] = field(default_factory=list)
    local_validation_calls: list[FakeLocalValidationCall] = field(default_factory=list)
    run_id: str = "test-run"
    settlement_observations: EvaluationSettlements | None = None
    deadline_time: float = 0.0
    deadline_wait_started: asyncio.Event = field(default_factory=asyncio.Event)
    _deadline_waiters: list[tuple[float, asyncio.Event]] = field(default_factory=list)
    submitted_revisions: dict[str, str] = field(default_factory=dict)
    submitted_generations: dict[str, int] = field(default_factory=dict)
    submitted_deadlines: dict[str, float] = field(default_factory=dict)
    cancelled_submissions: list[str] = field(default_factory=list)
    submitted_reports: dict[str, str] = field(default_factory=dict)
    accepted_evidence: dict[str, tuple[str, ...]] = field(default_factory=dict)
    _gates: dict[tuple[FakeEvaluationKind, int], FakeEvaluationGate] = field(default_factory=dict)
    _agent_evaluations: dict[str | None, list[AgentEvaluation]] = field(default_factory=dict)
    # Scripted profile outcomes, consumed in call order. Each is returned for
    # the requested revision. Unscripted, a profile fails as it does in a run
    # without a provisioned profiler.
    profile_results: list[CandidateProfile | BaseException] = field(default_factory=list)
    profile_calls: list[FakeProfileCall] = field(default_factory=list)
    # Whether the evaluation executor this Fake stands in for produces profile
    # evidence. The default matches the production executors, which do not
    # unless their plan carries a profile capture; a test that profiles sets it
    # to what the production executor of its run environment reports.
    profiling_supported: bool = False
    # Every release_jobs call, in call order, including repeats.
    released: list[str] = field(default_factory=list)
    _released_members: set[str] = field(default_factory=set)

    async def reopen_jobs(self, member_id: str) -> None:
        """Reconcile a completed release and open a fresh generation for resumed work."""
        self._released_members.discard(member_id)

    async def jobs_released(self, member_id: str) -> bool:
        """Project whether the member's durable scope refuses ordinary admission.

        Closing and completed releases both fence new work. Recovery can
        reconcile cleanup before opening a fresh scope generation.
        """
        return member_id in self._released_members

    async def release_jobs(self, member_id: str) -> ReleasedJobs:
        """Record the release; the first one for a member refuses its later profiles.

        The Fake runs no cluster jobs, so a release cancels nothing. As in
        production, only the first release of a member reports
        ``first_release`` and a profile for a released member fails typed.
        """
        self.released.append(member_id)
        first_release = member_id not in self._released_members
        self._released_members.add(member_id)
        return ReleasedJobs(
            member_id=member_id,
            evaluations=(),
            profiler_operations=(),
            first_release=first_release,
        )

    def script_profile(self, *results: CandidateProfile | BaseException) -> None:
        """Queue profile outcomes or failures in call order."""
        self.profile_results.extend(results)

    async def can_profile(self) -> bool:
        """Return the configured executor capability, as production derives it."""
        return self.profiling_supported

    async def profile(self, revision: str, request: str, *, member_id: str) -> CandidateProfile:
        """Return the next scripted outcome for ``revision``, or the unprovisioned failure.

        Without :attr:`profiling_supported` every profile ends unsupported,
        whatever is scripted, as a production profiler reports when the run's
        executor cannot produce profile evidence.
        """
        self.profile_calls.append(FakeProfileCall(revision, request, member_id))
        if member_id in self._released_members:
            return CandidateProfile(
                revision=revision,
                status=CandidateProfileStatus.FAILED,
                failure="the member's jobs were released, so no profile started",
            )
        if not self.profiling_supported:
            return CandidateProfile(
                revision=revision,
                status=CandidateProfileStatus.UNSUPPORTED,
                diagnosis="this run's evaluation executor cannot produce evidence kind: profile",
            )
        if not self.profile_results:
            return CandidateProfile(
                revision=revision,
                status=CandidateProfileStatus.FAILED,
                failure="no profiler agent is provisioned",
            )
        result = self.profile_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return CandidateProfile.model_validate(
            {**result.model_dump(mode="json"), "revision": revision}
        )

    def record_agent_evaluation(self, workspace: Workspace, evaluation: AgentEvaluation) -> None:
        """Record that an agent's evaluation of ``workspace`` reached ``evaluation``'s state."""
        self._agent_evaluations.setdefault(workspace.id, []).append(evaluation)

    async def agent_evaluations(self, workspace: Workspace) -> tuple[AgentEvaluation, ...]:
        """Return the evaluations recorded for ``workspace``'s identity, oldest first."""
        return tuple(self._agent_evaluations.get(workspace.id, ()))

    def settlements(self) -> EvaluationSettlements:
        """Expose the explicitly injected, ownership-validating settlement interface."""
        if self.settlement_observations is None:
            message = "agent evaluation settlements are unavailable"
            raise RuntimeContractError(message)
        return self.settlement_observations

    def current_time(self) -> float:
        """Read an explicit logical UTC clock without wall-clock time."""
        return self.deadline_time

    def advance_time(self, seconds: float) -> None:
        """Advance host time and release every reached deadline barrier."""
        self.deadline_time += seconds
        for deadline, barrier in self._deadline_waiters:
            if self.deadline_time >= deadline:
                barrier.set()

    async def wait_until(self, deadline_at_s: float) -> None:
        """Wait on an explicit barrier; tests control every advance."""
        if self.deadline_time >= deadline_at_s:
            return
        barrier = asyncio.Event()
        waiter = (deadline_at_s, barrier)
        self._deadline_waiters.append(waiter)
        self.deadline_wait_started.set()
        try:
            await barrier.wait()
        finally:
            self._deadline_waiters.remove(waiter)

    async def submitted_generation(self, handle_id: str) -> int:
        """Reject missing ownership rather than silently assigning generation zero."""
        if handle_id not in self.submitted_generations:
            message = f"evaluation {handle_id!r} has no submitted generation"
            raise RuntimeContractError(message)
        return self.submitted_generations[handle_id]

    async def submitted_deadline(self, handle_id: str) -> float:
        """Read the scripted immutable deadline, rejecting missing capture."""
        if handle_id not in self.submitted_deadlines:
            message = f"evaluation {handle_id!r} has no submitted deadline"
            raise RuntimeContractError(message)
        return self.submitted_deadlines[handle_id]

    async def cancel_submitted(self, handle_id: str) -> None:
        """Record cancellation of a known submitted evaluation."""
        await self.submitted_generation(handle_id)
        self.cancelled_submissions.append(handle_id)

    async def accepted_evidence_ids(self, handle_id: str) -> tuple[str, ...]:
        """Return the recorded backend-accepted IDs for one exact handle."""
        return self.accepted_evidence.get(handle_id, ())

    async def submitted_report(self, handle_id: str) -> str:
        """Validate the configured canonical record as strictly as production."""
        if handle_id not in self.submitted_reports:
            message = f"evaluation {handle_id!r} has no submitted report"
            raise RuntimeContractError(message)
        return StoredEvaluation.model_validate_json(
            self.submitted_reports[handle_id]
        ).model_dump_json()

    async def submitted_revision(self, handle_id: str) -> str:
        """Read the recorded exact capture, rejecting unrecorded handles."""
        if handle_id not in self.submitted_revisions:
            message = f"evaluation {handle_id!r} has no submitted revision"
            raise RuntimeContractError(message)
        return self.submitted_revisions[handle_id]

    def script_accuracy(self, *results: AccuracyEvaluation | BaseException) -> None:
        """Queue accuracy results or failures in call order."""
        self.accuracy_results.extend(results)

    def script_benchmark(self, *results: BenchmarkEvaluation | BaseException) -> None:
        """Queue benchmark results or failures in call order."""
        self.benchmark_results.extend(results)

    def script_root_benchmark(self, *results: BenchmarkEvaluation | BaseException) -> None:
        """Queue results or failures for benchmarks of the root workspace only."""
        self.root_benchmark_results.extend(results)

    def script_local_validation(self, *results: LocalValidationEvaluation) -> None:
        """Queue local-validation results in call order."""
        self.local_validation_results.extend(results)

    def gate(self, kind: FakeEvaluationKind, call: int) -> FakeEvaluationGate:
        """Hold the ``call``-th evaluation of ``kind`` (from zero) until released."""
        key = (kind, call)
        if key in self._gates:
            message = f"{kind} call {call} is already gated"
            raise ValueError(message)
        gate = FakeEvaluationGate()
        self._gates[key] = gate
        return gate

    async def _pass_gate(self, kind: FakeEvaluationKind, call: int, workspace: Workspace) -> None:
        gate = self._gates.get((kind, call))
        if gate is not None:
            await gate.hold(workspace)

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        """Record the request and return the next scripted result."""
        self.accuracy_calls.append(FakeAccuracyCall(workspace, reuse))
        await self._pass_gate("accuracy", len(self.accuracy_calls) - 1, workspace)
        if reuse is not None:
            if reuse.run_id != self.run_id:
                message = "accuracy receipt belongs to another run"
                raise RuntimeContractError(message)
            if reuse.workspace_id != workspace.id:
                message = "accuracy receipt belongs to another workspace"
                raise RuntimeContractError(message)
            if reuse.revision != workspace.revision:
                message = "accuracy receipt does not match the current workspace revision"
                raise RuntimeContractError(message)
            return AccuracyEvaluation(executed=False, receipt=reuse)
        result = self.accuracy_results.pop(0) if self.accuracy_results else self.default_accuracy
        if isinstance(result, BaseException):
            raise result
        if not result.passed:
            return result
        revision = workspace.revision
        if revision is None:
            message = "accuracy requires a recorded workspace revision"
            raise RuntimeContractError(message)
        receipt = result.receipt or AccuracyReceipt(
            run_id=self.run_id,
            workspace_id=workspace.id,
            revision=revision,
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
        await self._pass_gate("benchmark", len(self.benchmark_calls) - 1, workspace)
        if workspace.id is None and self.root_benchmark_results:
            result = self.root_benchmark_results.pop(0)
        elif self.benchmark_results:
            result = self.benchmark_results.pop(0)
        else:
            result = self.default_benchmark
        if isinstance(result, BaseException):
            raise result
        return result

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Record one semantic local-validation request and return its script.

        Like the product evaluation, an unusable recipe reference is a failed
        outcome the agent can correct, while a bad report location is a contract error.
        """
        validate_workspace_writable_paths(WorkspaceAccess.LIMITED, (report_location,))
        try:
            check_recipe_artifact_path(recipe_artifact)
        except LocalValidationRecipeError as error:
            return LocalValidationEvaluation(
                passed=False, feedback=str(error), recipe_unusable=True
            )
        self.local_validation_calls.append(
            FakeLocalValidationCall(workspace, recipe_artifact, report_location)
        )
        if self.local_validation_results:
            return self.local_validation_results.pop(0)
        return self.default_local_validation


@dataclass(frozen=True, slots=True)
class ObservationCall:
    """One ordered call captured by :class:`FakeObservations`."""

    kind: Literal["note", "warning"]
    message: str


class FakeObservations:
    """In-memory observation capability preserving kind and call order."""

    def __init__(self) -> None:
        """Create an empty ordered observation log."""
        self._calls: list[ObservationCall] = []

    @property
    def calls(self) -> tuple[ObservationCall, ...]:
        """Return observation calls in publication order."""
        return tuple(self._calls)

    def note(self, message: str) -> None:
        """Capture one informational note."""
        self._calls.append(ObservationCall("note", message))

    def warning(self, message: str) -> None:
        """Capture one non-fatal warning."""
        self._calls.append(ObservationCall("warning", message))


class FakeRun(Run):
    """In-memory run value with scriptable capabilities and owned cleanup."""

    agents: FakeWorkspaceAgentSessions
    workspaces: FakeWorkspaces
    evaluation: FakeEvaluation
    state: FakeState
    control: FakeControl
    commands: FakeCommands
    skills: FakeSkills
    observations: FakeObservations
    _closed: bool

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-040117 [PLR0913]; these are independent fake inputs, and a fake-host options bundle would add a second public configuration shape solely to shorten this signature.
        self,
        plugin: OrchestrationPlugin,
        *,
        run_id: str = "test-run",
        project_root: Path = Path(),
        facts: RunFacts | None = None,
        responder: TurnResponder = _echo_responder,
        agent_bindings: dict[str, AgentBinding] | None = None,
        supported_extra_tools: Collection[str] | None = None,
        supported_agent_capabilities: Collection[AgentCapability] | None = None,
        supports_parallel_candidates: bool = False,
    ) -> None:
        """Create a run whose private role map derives from ``plugin.agents``."""
        facts = (
            RunFacts(domain_id="generic", objective="Test objective.") if facts is None else facts
        )
        agents = FakeWorkspaceAgentSessions(
            plugin.agents,
            responder=responder,
            bindings=agent_bindings,
            supported_extra_tools=supported_extra_tools,
            supported_agent_capabilities=supported_agent_capabilities,
        )
        workspaces = FakeWorkspaces(
            FakeWorkspace(path=project_root),
            supports_parallel_candidates=supports_parallel_candidates,
            sessions=agents,
        )
        super().__init__(
            run_id=run_id,
            facts=facts,
            agents=agents,
            workspaces=workspaces,
            evaluation=FakeEvaluation(run_id=run_id),
            state=FakeState(plugin.state, workspaces.root),
            control=FakeControl(),
            commands=FakeCommands(),
            skills=FakeSkills(),
            observations=FakeObservations(),
        )
        object.__setattr__(self, "_closed", False)

    async def close(self) -> None:
        """Close all run-owned resources idempotently."""
        if self._closed:
            return
        object.__setattr__(self, "_closed", True)
        self.workspaces.begin_close()
        await self.workspaces.close()


class FakeStopTimer:
    """A stop timer whose waits end only when a test expires them.

    Production times a stop's grace period with ``asyncio.sleep``; this Fake
    records each requested delay and lets the test decide when it has passed,
    from any thread, so no test waits on the wall clock.
    """

    def __init__(self) -> None:
        """Start with no recorded or pending waits."""
        self.delays: list[float] = []
        self._condition = threading.Condition()
        self._pending: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    async def __call__(self, seconds: float) -> None:
        """Record *seconds* and wait until :meth:`expire`."""
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        entry = (loop, waiter)
        with self._condition:
            self.delays.append(seconds)
            self._pending.append(entry)
            self._condition.notify_all()
        try:
            await waiter
        finally:
            with self._condition:
                if entry in self._pending:
                    self._pending.remove(entry)

    def wait_armed(self, timeout: float) -> bool:
        """Block until a wait is pending; *timeout* is a deadlock guard."""
        with self._condition:
            return self._condition.wait_for(lambda: bool(self._pending), timeout)

    def expire(self) -> None:
        """End every pending wait, as if its delay had elapsed."""
        with self._condition:
            pending, self._pending = self._pending, []
        for loop, waiter in pending:
            loop.call_soon_threadsafe(_resolve, waiter)


def _resolve(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)
