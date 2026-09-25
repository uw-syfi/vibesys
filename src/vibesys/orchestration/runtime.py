"""Local host for the public custom-orchestration agent capabilities."""

# lint-waiver: LW-020038 [SLF001]; capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, NotRequired, TypedDict, TypeVar, Unpack

from pydantic import BaseModel

from vibesys.agent_spec_config import agent_spec_from_config
from vibesys.context import (
    RunSetup,
    WorkspaceResourceSpec,
    _execution_status,
    borrow_run_agent_environment,
    create_workspace_resources,
    open_run_resources,
    open_scoped_agent_environment,
)
from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkContract,
    BenchmarkGateResult,
    emit_gate_finished,
    emit_gate_started,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.evaluators.metrics import MetricSpace
from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    CoreEventType,
    EventStatus,
    GateFinishedData,
    GateKind,
    InvocationFinishedData,
    InvocationStartedData,
    PhaseData,
    json_value,
)
from vibesys.render.sink import output_sink
from vibesys.run.agent_sessions import SynchronizedSessionStore
from vibesys.run.run_control import splice_steering
from vibesys.runtime import AgentDefinition, AgentHandle, WorkspaceScope
from vibesys.sandbox.model_requests import (
    ModelRequestError,
)
from vibesys.sandbox.model_requests import (
    reconcile_model_requests as stage_model_requests,
)
from vs_agent.api import (
    AgentCapabilities,
    AgentExecutionPolicy,
    AgentProgress,
    AgentSessionKey,
    AgentSessionState,
    SessionScope,
    build_agent_client,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Generator, Mapping, Sequence
    from pathlib import Path
    from typing import TextIO

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.context import _RunResources
    from vibesys.evaluators.input_manifest import WorkspaceSource
    from vibesys.evaluators.metrics import Objective
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.profilers import ProfilerKind
    from vibesys.run.event_journal import EventJournal
    from vibesys.run.git_tracker import GitTracker
    from vibesys.run.integration import LocalRunIntegration
    from vibesys.sandbox.run_environment import CandidateRuntime, RunEnvironmentView
    from vs_agent.api import AgentClientProtocol, MCPServerSpec
    from vs_project.api import StateNamespace, StateSlot
    from vs_sandbox.api import Sandbox, SandboxExecutionResult

T = TypeVar("T", bound=BaseModel)


class _CheckpointOptions(TypedDict):
    publish: NotRequired[BaseModel | None]
    candidate: NotRequired[bool]
    label: NotRequired[str | None]


_active_progress: ContextVar[AgentProgress | None] = ContextVar(
    "vibesys_agent_progress", default=None
)


def _attempt_from_label(label: str) -> int | None:
    match = re.search(r"retry-(\d+)", label)
    return int(match.group(1)) if match else None


class _RuntimeClosedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("runtime is closed")


class _AgentClosedError(RuntimeError):
    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent {agent_id!r} is closed")


class _AgentRegistrationError(ValueError):
    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent ID {agent_id!r} must be nonempty and unique")


class _UnsupportedAgentExecutionPolicyError(ValueError):
    def __init__(self) -> None:
        super().__init__(
            "AgentSpec.execution is not supported by this runtime slice; "
            "declare host grants in AgentDefinition.resources"
        )


async def _wait_until_done(task: asyncio.Task) -> None:
    """Wait for a worker to finish even if the caller is canceled again."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-020027 [BLE001]; the caller inspects the worker's outcome after the wait, so this loop only needs to stop on any failure.
            break


async def _close_runtime(host: RunContext, error: BaseException | None) -> None:
    """Drain cleanup while preserving the policy failure or caller cancellation."""
    cancelled = False
    cleanup = asyncio.create_task(host.close())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-020028 [BLE001]; the completed cleanup task's exception is inspected right after the loop.
            break
    try:
        cleanup.result()
    except BaseException as cleanup_error:
        if error is not None:
            error.add_note(f"runtime cleanup also failed: {cleanup_error}")
            return
        if cancelled and not isinstance(cleanup_error, asyncio.CancelledError):
            cancellation = asyncio.CancelledError()
            cancellation.add_note(f"runtime cleanup also failed: {cleanup_error}")
            raise cancellation from cleanup_error
        raise
    if cancelled and error is None:
        raise asyncio.CancelledError


class _LocalAgentHandle:
    def __init__(  # noqa: PLR0913  # lint-waiver: LW-020029 [PLR0913]; independently owned agent resources are injected by the host, and bundling them would hide ownership.
        self,
        definition: AgentDefinition,
        context: _RunResources,
        client: AgentClientProtocol,
        resources: ExitStack,
        executor: ThreadPoolExecutor,
        scope_id: str | None,
        *,
        use_docker: bool,
    ) -> None:
        self._definition = definition
        self._resource_owner = context
        self._client = client
        self._resources = resources
        self._executor = executor
        self.scope_id = scope_id
        self._use_docker = use_docker
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the features this handle's driver can enforce."""
        return self._client.capabilities

    @property
    def backend_name(self) -> str:
        """Return the selected agent backend."""
        return self._client.backend_name

    @property
    def driver_name(self) -> str | None:
        """Return the selected CLI driver name when one is configured."""
        return self._client.driver_name

    @property
    def provider(self) -> str | None:
        """Return the selected provider, when the backend has one."""
        return self._client.provider

    @property
    def model(self) -> str | None:
        """Return the model used for this handle's role."""
        return self._client.model_for_kind(self._definition.id)

    async def turn(self, message: str, *, system_prompt: str = "", label: str = "") -> str:
        """Run one text turn with run control and attributed lifecycle events."""
        if self._close_task is not None:
            raise _AgentClosedError(self._definition.id)
        kind = self._definition.id
        progress = _active_progress.get()

        def invoke(routed: str, execution_id: str) -> str:
            return self._client.invoke_text(
                kind=kind,
                workspace=self._resource_owner.workspace,
                system_prompt=system_prompt,
                user_prompt=routed,
                round_label=label,
                env=self._agent_env(),
                invocation_id=execution_id,
                session_key=AgentSessionKey(SessionScope.ROLE, kind),
                progress=progress,
            )

        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._run_turn, message, system_prompt=system_prompt, label=label, invoke=invoke
            ),
        )

    async def turn_structured(  # noqa: PLR0913  # lint-waiver: LW-020030 [PLR0913]; this method mirrors AgentHandle.turn_structured, whose independent keyword options are its public contract.
        self,
        message: str,
        *,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        system_prompt: str = "",
        label: str = "",
        session_key: AgentSessionKey | None = None,
        reuse_session: bool | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
    ) -> T:
        """Run a typed turn through the same control and event path as text turns."""
        if self._close_task is not None:
            raise _AgentClosedError(self._definition.id)
        kind = self._definition.id
        progress = _active_progress.get()

        def invoke(routed: str, execution_id: str) -> T:
            return self._client.invoke(
                kind=kind,
                workspace=self._resource_owner.workspace,
                system_prompt=system_prompt,
                user_prompt=routed,
                response_cls=response_cls,
                fallback_factory=fallback_factory,
                round_label=label,
                env=self._agent_env(),
                invocation_id=execution_id,
                session_key=session_key or AgentSessionKey(SessionScope.ROLE, kind),
                reuse_session=reuse_session,
                mcp_servers=mcp_servers,
                progress=progress,
            )

        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._run_turn, message, system_prompt=system_prompt, label=label, invoke=invoke
            ),
        )

    def _agent_env(self) -> dict[str, str]:
        context = self._resource_owner
        return {} if self._use_docker else context.device.gpu_env()

    def _run_turn[Result](
        self,
        message: str,
        *,
        system_prompt: str,
        label: str,
        invoke: Callable[[str, str], Result],
    ) -> Result:
        if self._closed:
            raise _AgentClosedError(self._definition.id)
        context = self._resource_owner
        self._client.set_log_file(context.run_log_file)
        control = context.integration.control
        control.raise_if_stopped()
        control.wait_while_paused()
        steering = control.take_pending_steer()
        message = splice_steering(message, steering)
        execution_id = uuid.uuid4().hex
        kind = self._definition.id
        attempt = _attempt_from_label(label)
        if steering:
            control.notify_steer_consumed(
                agent_kind=kind, round_label=label, execution_id=execution_id
            )
        fields = {"agent_kind": kind, "round_label": label, "execution_id": execution_id}
        events = context.events
        events.emit(
            CoreEventType.AGENT_EXECUTION_STARTED,
            status=EventStatus.ACTIVE,
            data=AgentExecutionStartedData(
                stage=kind,
                attempt=attempt,
                system_prompt=system_prompt,
                user_prompt=message,
                activity=AgentExecutionActivityData(mode="thinking", summary=f"{kind} is working"),
                driver=self._client.driver_name,
                provider=self._client.provider,
                model=self._client.model_for_kind(kind),
            ),
            **fields,
        )
        events.emit(
            CoreEventType.PHASE_STARTED,
            status=EventStatus.ACTIVE,
            data=PhaseData(phase=kind, attempt=attempt),
            **fields,
        )
        events.emit(
            CoreEventType.INVOCATION_STARTED,
            status=EventStatus.ACTIVE,
            data=InvocationStartedData(system_prompt=system_prompt, user_prompt=message),
            **fields,
        )
        result: Result | None = None
        error: BaseException | None = None
        try:
            result = invoke(message, execution_id)
        except BaseException as exc:
            error = exc
            raise
        finally:
            status = _execution_status(error)
            error_text = f"{type(error).__name__}: {error}" if error is not None else None
            events.emit(
                CoreEventType.AGENT_EXECUTION_FINISHED,
                status=status,
                data=AgentExecutionFinishedData(result=json_value(result), error=error_text),
                **fields,
            )
            events.emit(
                CoreEventType.INVOCATION_FINISHED,
                status=status,
                data=InvocationFinishedData(result=json_value(result), error=error_text),
                **fields,
            )
            events.emit(
                CoreEventType.PHASE_FINISHED,
                status=status,
                data=PhaseData(phase=kind, attempt=attempt),
                **fields,
            )
        return result

    def _close(self) -> None:
        """Close the client before its sandbox, including after failed turns."""
        if self._closed:
            return
        self._closed = True
        self._resources.close()

    def set_log_file(self, writer: TextIO) -> None:
        """Queue a log-writer update on the client's own worker thread."""
        if self._close_task is None:
            self._executor.submit(self._client.set_log_file, writer)

    async def close(self) -> None:
        """Close the client and sandbox on the agent's own worker thread."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        """Drain queued turns before releasing their client and sandbox."""
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
        finally:
            await asyncio.to_thread(self._executor.shutdown, wait=True)


class _RunControl:
    """A cooperative boundary between policy steps and paid agent turns."""

    def __init__(self, integration: LocalRunIntegration, *, debug: bool) -> None:
        self._channel = integration.control
        self._debug = debug

    async def boundary(self) -> None:
        """Land stop or pause without consuming steering intended for an agent."""
        self._channel.raise_if_stopped()
        await asyncio.to_thread(self._channel.wait_while_paused)

    async def debug_step(self, message: str) -> None:
        """Pause at a policy step when interactive debug mode was requested."""
        await self.boundary()
        if self._debug:
            await asyncio.to_thread(input, f"\n[debug] {message}. Press Enter to continue...")


class _Agents:
    """Agent creation and per-turn progress for one run."""

    def __init__(self, host: RunContext) -> None:
        self._host = host

    def default_definition(self, role_id: str, *, model: str | None = None) -> AgentDefinition:
        """Build a named role from this run's resolved agent configuration."""
        request = self._host.request
        spec = agent_spec_from_config(
            request.config,
            backend=request.agent_backend,
            provider=request.cli_provider,
            model=model,
        )
        return AgentDefinition(id=role_id, spec=spec)

    async def spawn(
        self, definition: AgentDefinition, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> AgentHandle:
        """Open a thread-affine client and sandbox for a named role."""
        return await self._host._spawn(definition, scope=self._host.workspaces._scope_of(scope))

    @contextmanager
    def progress(self, value: AgentProgress) -> Generator[None]:
        """Attribute turns spawned in the current async task."""
        token = _active_progress.set(value)
        try:
            yield
        finally:
            _active_progress.reset(token)


class _TypedRunStateSlot[T: BaseModel]:
    """Read one declared typed file from the policy's portable namespace."""

    def __init__(self, host: RunContext, slot: StateSlot[T]) -> None:
        self._host = host
        self._slot = slot

    async def load(self) -> T | None:
        """Load and validate the last staged or committed model."""
        return await self._host._run_blocking(self._slot.load_optional)


class _RunState:
    """Policy-bound portable state and machine-local staging paths."""

    def __init__(self, host: RunContext) -> None:
        self._host = host

    @property
    def namespace(self) -> StateNamespace:
        """Return the policy's portable namespace for existing paid-work journals."""
        setup = self._host._setup
        if setup.state_namespace is None:
            message = "policy did not declare a portable state namespace"
            raise TypeError(message)
        context = self._host._resources
        return context.state.portable(setup.state_namespace)

    @property
    def local_namespace(self) -> StateNamespace:
        """Return machine-local state for uncommitted paid-work cursors."""
        setup = self._host._setup
        if setup.state_namespace is None:
            message = "policy did not declare a state namespace"
            raise TypeError(message)
        return self._host._resources.state.local(setup.state_namespace)

    def local_path(self, name: str) -> Path:
        """Return a validated machine-local file path owned by this run."""
        relative = PurePosixPath(name)
        parent = relative.parent
        directory = self.local_namespace.external_directory(
            None if parent == PurePosixPath(".") else parent
        )
        if relative.name in {"", ".", ".."} or relative.is_absolute():
            message = f"invalid local state path {name!r}"
            raise ValueError(message)
        return directory / relative.name

    def artifact_path(self, name: str) -> Path:
        """Return a validated portable artifact directory path."""
        return self.namespace.external_directory(name)

    def slot(self, name: str, model: type[T]) -> _TypedRunStateSlot[T]:
        """Bind one declared typed portable file."""
        setup = self._host._setup
        declared = setup.state_slots or {}
        if declared.get(name) is not model:
            message = f"policy state slot {name!r} is not declared with {model.__name__}"
            raise TypeError(message)
        return _TypedRunStateSlot(self._host, self.namespace.slot(name, model))

    async def load(self, model: type[T]) -> T | None:
        """Load the validated state after interrupted checkpoint recovery."""
        return await self.slot("state.json", model).load()

    async def checkpoint(
        self,
        *,
        sequence: int,
        writes: Mapping[str, BaseModel],
        **options: Unpack[_CheckpointOptions],
    ) -> str:
        """Journal and commit typed writes with candidate edits, then publish."""
        async with self._host._parent_mutation_lock:
            return await self._host._run_blocking(
                self._checkpoint,
                sequence,
                writes,
                options.get("publish"),
                candidate=options.get("candidate", True),
                label=options.get("label"),
            )

    def _checkpoint(
        self,
        sequence: int,
        writes: Mapping[str, BaseModel],
        publish: BaseModel | None,
        *,
        candidate: bool,
        label: str | None,
    ) -> str:
        context = self._host._resources
        namespace = self._host._setup.state_namespace
        if namespace is None:
            message = "policy did not declare a durable state slot"
            raise TypeError(message)
        coordinator = context._round_transaction_coordinator
        if coordinator is None:
            message = "policy did not declare checkpoint slots"
            raise TypeError(message)
        coordinator.begin(sequence, writes=writes, candidate=candidate, label=label).complete()
        committed = publish or writes.get("state.json")
        if committed is not None:
            context.publish_committed_state(namespace, committed)
        revision = context.git.current_sha()
        if revision is None:
            message = "checkpoint completed without a Git revision"
            raise RuntimeError(message)
        return revision


@dataclass(frozen=True, slots=True)
class MeasurementOptions:
    """Policy-selected metric axes, event label, and command override."""

    objectives: Sequence[Objective] = ()
    label: str | None = None
    execution_base: str | None = None


_DEFAULT_MEASUREMENT_OPTIONS = MeasurementOptions()


@dataclass(frozen=True)
class _GateInputs:
    """Bind trusted gate inputs from one workspace's owned resources."""

    events: EventJournal
    judge_backend: Sandbox
    judge_accuracy_command: str | None
    judge_benchmark_command: str | None
    run_environment_view: RunEnvironmentView
    git: GitTracker

    @classmethod
    def from_resources(cls, context: _RunResources) -> _GateInputs:
        """Read the selected environment's immutable command paths."""
        return cls(
            events=context.events,
            judge_backend=context.run_environment_session.sandbox,
            judge_accuracy_command=context.run_environment_view.paths.accuracy_command,
            judge_benchmark_command=context.run_environment_view.paths.benchmark_command,
            run_environment_view=context.run_environment_view,
            git=context.git,
        )

    def trusted_input_changes(self) -> list[str]:
        """Detect edits to evaluator-owned project inputs."""
        return self.git.trusted_input_changes()


class _Evaluator:
    """Trusted checks and measurements in the parent run workspace."""

    def __init__(self, host: RunContext) -> None:
        self._host = host
        self._locks: dict[str | None, asyncio.Lock] = {None: asyncio.Lock()}

    def _lock_for(self, scope: WorkspaceScope | WorkspaceHandle | None) -> asyncio.Lock:
        """Serialize gates only within the workspace they inspect."""
        key = scope.id if scope is not None else None
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _forget(self, scope: WorkspaceScope) -> None:
        """Release a discarded scope's synchronization state."""
        self._locks.pop(scope.id, None)

    async def check(
        self,
        process_id: str,
        *,
        label: str | None = None,
        execution_command: str | None = None,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
    ) -> AccuracyGateResult:
        """Run the bundle's trusted accuracy command and reject input tampering."""
        async with self._lock_for(scope):
            context = _GateInputs.from_resources(self._host.workspaces._resources_for(scope))
            timeout = framework_command_timeout(
                context, self._host.request.input_bundle.manifest.accuracy.timeout_seconds
            )
            return await self._host._run_blocking(
                run_accuracy_gate,
                context,
                process_id=process_id,
                timeout_seconds=timeout,
                execution_command=execution_command,
                round_label=label,
            )

    async def reuse_accuracy(self, *, label: str | None = None) -> AccuracyGateResult:
        """Publish a paired accuracy PASS reused for an exact candidate revision."""
        return await self._host._run_blocking(self._reuse_accuracy, label)

    def _reuse_accuracy(self, label: str | None) -> AccuracyGateResult:
        command = self._host.environment.view.paths.accuracy_command
        emit_gate_started(GateKind.ACCURACY, command=command, round_label=label)
        emit_gate_finished(
            GateFinishedData(gate=GateKind.ACCURACY, reused=True),
            passed=True,
            round_label=label,
        )
        return AccuracyGateResult(
            command=command,
            passed=True,
            output="Reused the prior framework-owned PASS for this exact candidate commit",
            feedback=None,
            executed=False,
        )

    async def measure(
        self,
        output_slug: str,
        *,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
        options: MeasurementOptions = _DEFAULT_MEASUREMENT_OPTIONS,
    ) -> BenchmarkGateResult:
        """Run and parse the bundle's declared trusted benchmark contract."""
        async with self._lock_for(scope):
            context = _GateInputs.from_resources(self._host.workspaces._resources_for(scope))
            bundle = self._host.request.input_bundle
            timeout = framework_command_timeout(context, bundle.manifest.benchmark.timeout_seconds)
            return await self._host._run_blocking(
                run_benchmark_gate,
                context,
                contract=BenchmarkContract(
                    result_spec=bundle.benchmark_result,
                    result_protocol=bundle.benchmark_result_protocol,
                    timeout_seconds=timeout,
                ),
                space=MetricSpace(objectives=tuple(options.objectives)),
                process_id=output_slug,
                output_slug=output_slug,
                execution_base=options.execution_base,
                round_label=options.label,
            )


class WorkspaceHandle:
    """One root or isolated workspace with scoped Git operations."""

    def __init__(self, owner: _Workspaces, scope: WorkspaceScope | None) -> None:
        """Bind a live scope, or the root, to its run workspace manager."""
        self._owner = owner
        self._scope = scope

    @property
    def id(self) -> str | None:
        """Return an isolated scope's identity, if this is a fork."""
        return self._scope.id if self._scope is not None else None

    @property
    def path(self) -> Path:
        """Return this workspace's host path."""
        if self._scope is None:
            return self._owner._host._resources.workspace
        return self._owner._require_scope(self._scope).path

    @property
    def revision(self) -> str | None:
        """Return the retained revision of a fork or current root HEAD."""
        if self._scope is None:
            return self._owner._host._resources.git.current_sha()
        return self._owner._require_scope(self._scope).revision

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the run's immutable trusted-input Git baseline."""
        return self._owner._host._resources.git.trusted_input_baseline

    async def snapshot(self, label: str) -> str:
        """Commit this workspace's changes and retain its revision."""
        return await self._owner._snapshot(label, scope=self._scope)

    async def restore(
        self,
        revision: str,
        *,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
    ) -> None:
        """Restore this workspace to a retained revision."""
        await self._owner._restore(
            revision, scope=self._scope, clean=clean, preserve_paths=preserve_paths
        )

    async def retain(self, name: str, revision: str) -> str:
        """Keep a revision reachable under a policy-owned name."""
        return await self._owner._retain(name, revision)

    async def pending_changes(self) -> list[str]:
        """List uncommitted candidate changes in this workspace."""
        return await self._owner._pending_changes(scope=self._scope)

    async def candidate_patch(self, revision: str) -> str:
        """Return a candidate diff against the trusted baseline."""
        return await self._owner._candidate_patch(revision, scope=self._scope)

    async def trusted_input_changes(self) -> list[str]:
        """List changed evaluator-owned files."""
        return await self._owner._trusted_input_changes(scope=self._scope)

    async def discard(self) -> None:
        """Close an isolated workspace and all of its agent handles."""
        if self._scope is None:
            message = "the run root cannot be discarded"
            raise ValueError(message)
        await self._owner._discard_scope(self._scope)


class _Workspaces:
    """Own isolated worktrees and parent adoption for one run."""

    def __init__(self, host: RunContext) -> None:
        self._host = host
        self._scopes: dict[str, WorkspaceScope] = {}
        self._scoped_resources: dict[str, _RunResources] = {}
        self._scope_locks: dict[str, asyncio.Lock] = {}
        self.root = WorkspaceHandle(self, None)

    def _scope_of(self, scope: WorkspaceScope | WorkspaceHandle | None) -> WorkspaceScope | None:
        """Resolve a public handle to its internal scope identity."""
        if isinstance(scope, WorkspaceHandle):
            if scope._owner is not self:
                message = "workspace handle belongs to another run"
                raise ValueError(message)
            return scope._scope
        return scope

    async def fork(self, revision: str | None = None) -> WorkspaceHandle:
        """Open an isolated worktree at a committed parent revision."""
        async with self._host._parent_mutation_lock:
            scope = await self._host._run_blocking(self._fork, revision)
            return WorkspaceHandle(self, scope)

    def _fork(self, revision: str | None) -> WorkspaceScope:
        parent = self._host._resources
        base = revision or parent.git.current_sha()
        if base is None:
            message = "cannot fork a workspace without a committed revision"
            raise RuntimeError(message)
        if not parent.run_environment_view.supports_parallel_candidate_evaluation:
            message = "run environment cannot open isolated candidate sandboxes"
            raise RuntimeError(message)
        scope_id = f"s{uuid.uuid4().hex}"
        context = create_workspace_resources(
            parent,
            WorkspaceResourceSpec(
                scope_id=scope_id,
                revision=base,
                config=self._host.request.config,
                agent_backend=self._host.request.agent_backend,
                cli_provider=self._host.request.cli_provider,
            ),
        )
        scope = WorkspaceScope(id=scope_id, path=context.workspace, revision=base)
        self._scopes[scope_id] = scope
        self._scoped_resources[scope_id] = context
        return scope

    async def _snapshot(
        self, label: str, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> str:
        """Commit candidate edits and retain a fork's revision for later adoption."""
        scope = self._scope_of(scope)
        if scope is None:
            async with self._host._parent_mutation_lock:
                return await self._host._run_blocking(self._snapshot_parent, label)
        lock = self._scope_lock(scope)
        async with lock:
            revision = await self._host._run_blocking(self._snapshot_scoped, label, scope)
            async with self._host._parent_mutation_lock:
                return await self._host._run_blocking(self._retain_scoped, scope, revision)

    def _snapshot_parent(self, label: str) -> str:
        parent = self._host._resources
        parent.git.snapshot(label)
        revision = parent.git.current_sha()
        if revision is None:
            message = "workspace snapshot completed without a Git revision"
            raise RuntimeError(message)
        return revision

    def _snapshot_scoped(self, label: str, scope: WorkspaceScope) -> str:
        current = self._require_scope(scope)
        git = self._scoped_resources[current.id].git
        git.snapshot(label)
        revision = git.current_sha()
        if revision is None:
            message = "workspace snapshot completed without a Git revision"
            raise RuntimeError(message)
        return revision

    def _retain_scoped(self, scope: WorkspaceScope, revision: str) -> str:
        current = self._require_scope(scope)
        self._host._resources.git.retain_candidate(current.id, revision)
        current.revision = revision
        return revision

    async def adopt(
        self,
        revision: str,
        *,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
    ) -> None:
        """Materialize a retained candidate revision in the parent workspace."""
        async with self._host._parent_mutation_lock:
            adopted = await self._host._run_blocking(
                self._host._resources.git.checkout_tree,
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
            )
        if not adopted:
            message = f"could not adopt candidate revision {revision!r}"
            raise RuntimeError(message)

    async def _restore(
        self,
        revision: str,
        *,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
        clean: bool = True,
        preserve_paths: tuple[str, ...] = (),
    ) -> None:
        """Restore a root or scoped workspace to a committed tree."""
        scope = self._scope_of(scope)
        if scope is None:
            await self.adopt(revision, clean=clean, preserve_paths=preserve_paths)
            return
        async with self._scope_lock(scope):
            context = self._resources_for(scope)
            restored = await self._host._run_blocking(
                context.git.checkout_tree,
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
            )
            if not restored:
                message = f"could not restore candidate revision {revision!r}"
                raise RuntimeError(message)
            scope.revision = revision

    async def _retain(self, name: str, revision: str) -> str:
        """Keep a candidate revision reachable from the parent repository."""
        async with self._host._parent_mutation_lock:
            return await self._host._run_blocking(
                self._host._resources.git.retain_candidate, name, revision
            )

    async def _pending_changes(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> list[str]:
        """List uncommitted changes in one workspace."""
        context = self._resources_for(self._scope_of(scope))
        return await self._host._run_blocking(context.git.pending_changes)

    async def _candidate_patch(
        self, revision: str, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> str:
        """Return a candidate patch using this workspace's Git tracker."""
        context = self._resources_for(self._scope_of(scope))
        return await self._host._run_blocking(context.git.candidate_patch, revision)

    async def _trusted_input_changes(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> list[str]:
        """Detect edits to evaluator-owned inputs in one workspace."""
        context = self._resources_for(self._scope_of(scope))
        return await self._host._run_blocking(context.git.trusted_input_changes)

    async def _discard_scope(self, scope: WorkspaceScope | WorkspaceHandle) -> None:
        """Drain scoped agents and gates before removing their worktree."""
        async with self._host._spawn_lock:  # shared lifecycle lock
            scope = self._require_scope(scope)
            errors: list[BaseException] = []
            for agent in tuple(self._host._agents.values()):
                if agent.scope_id != scope.id:
                    continue
                try:
                    await agent.close()
                except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-020031 [BLE001]; cleanup must continue through every resource, so each failure is collected and raised together afterwards.
                    errors.append(exc)
            async with (
                self._host.evaluator._lock_for(scope),
                self._scope_lock(scope),
                self._host._parent_mutation_lock,
            ):
                try:
                    await self._host._run_blocking(self._discard, scope)
                except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-020032 [BLE001]; cleanup must continue through every resource, so each failure is collected and raised together afterwards.
                    errors.append(exc)
                finally:
                    if scope.id not in self._scopes:
                        self._host.evaluator._forget(scope)
            if errors:
                message = "scoped agent cleanup failed"
                raise BaseExceptionGroup(message, errors)

    def _discard(self, scope: WorkspaceScope) -> None:
        current = self._require_scope(scope)
        self._scoped_resources[current.id].close()
        del self._scopes[current.id]
        del self._scoped_resources[current.id]
        self._scope_locks.pop(current.id, None)

    def _scope_lock(self, scope: WorkspaceScope) -> asyncio.Lock:
        current = self._require_scope(scope)
        return self._scope_locks.setdefault(current.id, asyncio.Lock())

    def _require_scope(self, scope: WorkspaceScope | WorkspaceHandle) -> WorkspaceScope:
        resolved = self._scope_of(scope)
        if resolved is None:
            message = "expected an isolated workspace scope"
            raise ValueError(message)
        current = self._scopes.get(resolved.id)
        if current is not resolved:
            message = "workspace scope is closed or belongs to another run"
            raise ValueError(message)
        return current

    def _resources_for(self, scope: WorkspaceScope | WorkspaceHandle | None) -> _RunResources:
        """Resolve the sandbox-backed context for a live workspace scope."""
        scope = self._scope_of(scope)
        if scope is None:
            return self._host._resources
        current = self._require_scope(scope)
        return self._scoped_resources[current.id]

    async def close(self) -> None:
        """Discard all remaining worktrees in reverse creation order."""
        errors: list[BaseException] = []
        for scope in reversed(tuple(self._scopes.values())):
            try:
                async with (
                    self._host.evaluator._lock_for(scope),
                    self._scope_lock(scope),
                    self._host._parent_mutation_lock,
                ):
                    await asyncio.to_thread(self._discard, scope)
            except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-020033 [BLE001]; cleanup must continue through every resource, so each failure is collected and raised together afterwards.
                errors.append(exc)
        if errors:
            message = "workspace cleanup failed"
            raise BaseExceptionGroup(message, errors)


class _Environment:
    """Generic execution facts and candidate deployment lifecycle."""

    def __init__(self, host: RunContext) -> None:
        self._host = host

    @property
    def view(self) -> RunEnvironmentView:
        """Return the resolved root environment's policy-neutral facts."""
        return self.view_for()

    def view_for(self, scope: WorkspaceScope | WorkspaceHandle | None = None) -> RunEnvironmentView:
        """Return environment facts for one live workspace."""
        return self._host.workspaces._resources_for(scope).run_environment_view

    @property
    def reference_path(self) -> str:
        """Return the prompt-visible reference path."""
        return self._host._resources.ref_name

    @property
    def workspace_sources(self) -> tuple[WorkspaceSource, ...]:
        """Return materialized workspace sources for prompt context."""
        return self._host._resources.workspace_sources

    @property
    def skill_source_paths(self) -> tuple[Path, ...]:
        """Return the resolved skill source directories for this run."""
        return tuple(self._host._resources.skill_source_paths)

    @property
    def profiler_kind(self) -> ProfilerKind:
        """Return the profiler selected after environment preflight."""
        return self._host._resources.profiler_kind

    @property
    def model_name(self) -> str:
        """Return the resolved default model name."""
        return self._host._resources.model_name

    @property
    def run_log_path(self) -> Path:
        """Return the current run log file path."""
        return self._host._resources.run_log_path

    @property
    def log_dir(self) -> Path:
        """Return this run's machine-local log directory."""
        return self._host._resources.log_dir

    def candidate_runtime(
        self,
        generation: int,
        child_idx: int,
        *,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
    ) -> CandidateRuntime:
        """Resolve candidate prompt notes and deployment identity."""
        context = self._host.workspaces._resources_for(scope)
        return context.run_environment.candidate_runtime(
            context.run_environment_view, generation, child_idx
        )

    async def teardown_deployment(self, name: str) -> None:
        """Release a candidate deployment through the selected environment."""
        context = self._host._resources
        await self._host._run_blocking(
            context.run_environment.teardown_deployment, name, log=context.lprint
        )

    async def reconcile_model_requests(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> str | None:
        """Stage candidate-declared Modal model weights before trusted gates."""
        context = self._host.workspaces._resources_for(scope)
        if context.run_environment_view.env_kind != "modal":
            return None
        return await self._host._run_blocking(self._stage_model_requests, context)

    @staticmethod
    def _stage_model_requests(context: _RunResources) -> str | None:
        try:
            volumes = stage_model_requests(context.workspace, log=context.lprint)
        except ModelRequestError as exc:
            context.lprint(f"[model-request] rejected: {exc}")
            return f"Model-weight request could not be satisfied: {exc}"
        if volumes:
            context.lprint(
                f"[model-request] staged {len(volumes)} model volume(s): " + ", ".join(volumes)
            )
        return None

    async def reselect_device(
        self, *, scope: WorkspaceScope | WorkspaceHandle | None = None
    ) -> None:
        """Rebalance the device assigned to a workspace before a paid turn."""
        context = self._host.workspaces._resources_for(scope)
        await self._host._run_blocking(context.reselect_gpu)

    async def execute(
        self,
        command: str,
        *,
        timeout_seconds: int | None = None,
        scope: WorkspaceScope | WorkspaceHandle | None = None,
    ) -> SandboxExecutionResult:
        """Execute a policy-selected command in the selected run environment."""
        context = self._host.workspaces._resources_for(scope)
        return await self._host._run_blocking(
            context.run_environment_session.sandbox.execute, command, timeout=timeout_seconds
        )


class RunContext:
    """One run's resources and focused host capabilities."""

    def __init__(  # noqa: PLR0913  # LW-040004 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
        self,
        request: RunRequest,
        integration: LocalRunIntegration,
        *,
        setup: RunSetup,
        open_agent_environment: Callable[..., AgentEnvironment] | None,
        agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
        backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    ) -> None:
        """Bind request, policy setup, and the application control channel.

        ``agent_client_factory`` and ``backend_factory`` are injection seams a
        test uses in place of monkeypatching this module's real client/backend
        constructors (``build_agent_client``, ``vibesys.backends.get``). Each
        defaults to the real implementation when omitted, so production call
        sites are unchanged. ``agent_client_factory`` overrides
        :func:`vs_agent.api.build_agent_client`, looked up as this module's
        own (still independently patchable) ``build_agent_client`` global when
        no override is given.
        """
        self.request = request
        self._setup = setup
        self._integration = integration
        self._open_agent_environment = open_agent_environment
        self._agent_client_factory = agent_client_factory
        self._backend_factory = backend_factory
        self._resource_owner: _RunResources | None = None
        self._session_store: SynchronizedSessionStore | None = None
        self._agents: dict[tuple[str | None, str], _LocalAgentHandle] = {}
        self._spawn_lock = asyncio.Lock()
        self._parent_mutation_lock = asyncio.Lock()
        self._blocking: set[asyncio.Task] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self.control = _RunControl(integration, debug=request.debug)
        self.state = _RunState(self)
        self.evaluator = _Evaluator(self)
        self.workspaces = _Workspaces(self)
        self.agents = _Agents(self)
        self.environment = _Environment(self)

    @property
    def events(self) -> EventJournal:
        """Return the run's semantic event journal."""
        return self._integration.events

    def log(self, message: str) -> None:
        """Write one line to the active run log."""
        self._resources.lprint(message)

    def switch_log(self, label: int | str) -> None:
        """Select a policy phase log for subsequent output and agent turns."""
        self._resources.switch_log_file(label)
        writer = self._resources.run_log_file
        for agent in self._agents.values():
            agent.set_log_file(writer)

    async def _run_blocking[**P, Result](
        self, operation: Callable[P, Result], *args: P.args, **kwargs: P.kwargs
    ) -> Result:
        """Run synchronous policy or host work without racing resource teardown."""
        if self._closed:
            raise _RuntimeClosedError
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
        self._blocking.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            await _wait_until_done(task)
            if error := task.exception():
                cancelled.add_note(f"blocking operation also failed: {error}")
            raise
        finally:
            self._blocking.discard(task)

    @classmethod
    @asynccontextmanager
    async def open(  # noqa: PLR0913  # LW-040005 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
        cls,
        request: RunRequest,
        integration: LocalRunIntegration,
        *,
        setup: RunSetup,
        open_agent_environment: Callable[..., AgentEnvironment] | None = None,
        agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
        backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    ) -> AsyncIterator[RunContext]:
        """Construct and close the run, including after cancellation or setup failure."""
        host = cls(
            request,
            integration,
            setup=setup,
            open_agent_environment=open_agent_environment,
            agent_client_factory=agent_client_factory,
            backend_factory=backend_factory,
        )
        try:
            prepare = asyncio.create_task(asyncio.to_thread(host._prepare))
            try:
                await asyncio.shield(prepare)
            except asyncio.CancelledError:
                while not prepare.done():
                    try:
                        await asyncio.shield(prepare)
                    except asyncio.CancelledError:
                        continue
                prepare.result()
                raise
            yield host
        finally:
            await _close_runtime(host, sys.exception())

    @property
    def _resources(self) -> _RunResources:
        """Return the host-owned resource assembly after preparation."""
        if self._resource_owner is None:
            raise _RuntimeClosedError
        return self._resource_owner

    def _prepare(self) -> None:
        """Open the canonical run context once."""
        self._ensure_resources()

    async def _spawn(
        self, definition: AgentDefinition, *, scope: WorkspaceScope | None = None
    ) -> _LocalAgentHandle:
        """Open one independently configured agent in a live workspace."""
        async with self._spawn_lock:
            if self._closed:
                raise _RuntimeClosedError
            context = self.workspaces._resources_for(scope)
            executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"vs-agent-{definition.id}"
            )
            opened = asyncio.get_running_loop().run_in_executor(
                executor,
                partial(
                    self._spawn_agent,
                    definition,
                    context,
                    executor,
                    scope_id=scope.id if scope else None,
                ),
            )
            try:
                return await asyncio.shield(opened)
            except asyncio.CancelledError:
                while not opened.done():
                    try:
                        await asyncio.shield(opened)
                    except asyncio.CancelledError:
                        continue
                if opened.exception() is not None:
                    await asyncio.to_thread(executor.shutdown, wait=True)
                raise
            except BaseException:
                await asyncio.to_thread(executor.shutdown, wait=True)
                raise

    def _spawn_agent(
        self,
        definition: AgentDefinition,
        context: _RunResources,
        executor: ThreadPoolExecutor,
        *,
        scope_id: str | None,
    ) -> _LocalAgentHandle:
        """Open one sandbox and one agent client, with requested grants."""
        if self._closed:
            raise _RuntimeClosedError
        key = (scope_id, definition.id)
        if not definition.id or key in self._agents:
            raise _AgentRegistrationError(definition.id)
        if definition.spec.execution != AgentExecutionPolicy():
            raise _UnsupportedAgentExecutionPolicyError
        with ExitStack() as resources:
            if context.run_environment_view.share_agent_session:
                opened = borrow_run_agent_environment(
                    context,
                    mounts=definition.resources,
                    agent_backend=definition.spec.backend.value,
                    cli_provider=definition.spec.provider,
                )
            elif scope_id is None and self._open_agent_environment is not None:
                opened = self._open_agent_environment(
                    mounts=definition.resources,
                    agent_backend=definition.spec.backend.value,
                    cli_provider=definition.spec.provider,
                )
            else:
                opened = open_scoped_agent_environment(
                    context,
                    mounts=definition.resources,
                    agent_backend=definition.spec.backend.value,
                    cli_provider=definition.spec.provider,
                )
            resources.callback(opened.close)
            backends = (
                {definition.id: opened.backends["chat"]} if opened.backends is not None else None
            )
            agent_client_factory = self._agent_client_factory or build_agent_client
            client = agent_client_factory(
                spec=definition.spec,
                session_store=self._session_store,
                backends=backends,
                skill_source_dirs=list(opened.skill_source_dirs),
                skill_selection=opened.skill_selection,
                run_log_file=context.run_log_file,
                use_docker=opened.use_docker,
                log_dir=context.log_dir,
                host_resources=(*opened.host_resources, *definition.resources),
                project_path_policy=opened.project_path_policy,
                require_host_sandbox=not opened.use_docker,
                events=output_sink(),
            )
            resources.callback(client.close)
            handle = _LocalAgentHandle(
                definition,
                context,
                client,
                resources.pop_all(),
                executor,
                scope_id,
                use_docker=opened.use_docker,
            )
        self._agents[key] = handle
        return handle

    def _ensure_resources(self) -> _RunResources:
        if self._closed:
            raise _RuntimeClosedError
        if self._resource_owner is not None:
            return self._resource_owner
        self._resource_owner = open_run_resources(
            self.request,
            self._setup,
            self._integration,
            backend_factory=self._backend_factory,
        )
        self._session_store = SynchronizedSessionStore(
            self._resource_owner.state.local("agent").slot("sessions.json", AgentSessionState),
            log=self._resource_owner.logger.lprint,
        )
        return self._resource_owner

    async def close(self) -> None:
        """Release agents in reverse spawn order, then the run context."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        """Drain in-flight work and release all resources exactly once."""
        self._closed = True
        errors: list[BaseException] = []
        for operation in tuple(self._blocking):
            await _wait_until_done(operation)
            if error := operation.exception():
                errors.append(error)
        async with self._spawn_lock:
            for agent in reversed(tuple(self._agents.values())):
                try:
                    await agent.close()
                except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-020034 [BLE001]; cleanup must continue through every resource, so each failure is collected and raised together afterwards.
                    errors.append(exc)
            try:
                await self.workspaces.close()
            except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-020035 [BLE001]; cleanup must continue through every resource, so each failure is collected and raised together afterwards.
                errors.append(exc)
        if self._resource_owner is not None:
            try:
                await asyncio.to_thread(self._resource_owner.close)
            except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-020036 [BLE001]; cleanup must continue through every resource, so each failure is collected and raised together afterwards.
                errors.append(exc)
        if errors:
            message = "run cleanup failed"
            raise BaseExceptionGroup(message, errors)
