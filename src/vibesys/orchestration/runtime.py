"""Local host for the public custom-orchestration agent capabilities."""

from __future__ import annotations

import asyncio
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from vibesys.context import (
    RunSetup,
    WorkspaceContextSpec,
    _execution_status,
    create_workspace_context,
    open_run_context,
    open_scoped_agent_environment,
)
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.evaluators.gates import (
    AccuracyGateResult,
    BenchmarkGateResult,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    CoreEventType,
    EventStatus,
    InvocationFinishedData,
    InvocationStartedData,
    json_value,
)
from vibesys.render.sink import output_sink
from vibesys.run.run_control import splice_steering
from vibesys.runtime import AgentDefinition, WorkspaceScope
from vs_agent.api import AgentExecutionPolicy, AgentSessionKey, SessionScope, build_agent_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence
    from pathlib import Path

    from vibesys.context import _RunContext
    from vibesys.evaluators.metrics import Objective
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol, MCPServerSpec
    from vs_project.api import StateSlot

T = TypeVar("T", bound=BaseModel)


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


class _MissingAgentHostError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("this caller did not provide an agent environment host")


async def _wait_until_done(task: asyncio.Task) -> None:
    """Wait for a worker to finish even if the caller is canceled again."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001
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
        except BaseException:  # noqa: BLE001  # inspect the completed task below
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
    def __init__(  # noqa: PLR0913  # independently owned agent resources
        self,
        definition: AgentDefinition,
        context: _RunContext,
        client: AgentClientProtocol,
        resources: ExitStack,
        executor: ThreadPoolExecutor,
        scope_id: str | None,
        *,
        use_docker: bool,
    ) -> None:
        self._definition = definition
        self._context = context
        self._client = client
        self._resources = resources
        self._executor = executor
        self.scope_id = scope_id
        self._use_docker = use_docker
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def turn(self, message: str, *, system_prompt: str = "", label: str = "") -> str:
        """Run one text turn with run control and attributed lifecycle events."""
        if self._close_task is not None:
            raise _AgentClosedError(self._definition.id)
        kind = self._definition.id

        def invoke(routed: str, execution_id: str) -> str:
            return self._client.invoke_text(
                kind=kind,
                workspace=self._context.workspace,
                system_prompt=system_prompt,
                user_prompt=routed,
                round_label=label,
                env=self._agent_env(),
                invocation_id=execution_id,
                session_key=AgentSessionKey(SessionScope.ROLE, kind),
            )

        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._run_turn, message, system_prompt=system_prompt, label=label, invoke=invoke
            ),
        )

    async def turn_structured(  # noqa: PLR0913
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

        def invoke(routed: str, execution_id: str) -> T:
            return self._client.invoke(
                kind=kind,
                workspace=self._context.workspace,
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
            )

        return await asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._run_turn, message, system_prompt=system_prompt, label=label, invoke=invoke
            ),
        )

    def _agent_env(self) -> dict[str, str]:
        context = self._context
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
        context = self._context
        control = context.integration.control
        control.raise_if_stopped()
        control.wait_while_paused()
        steering = control.take_pending_steer()
        message = splice_steering(message, steering)
        execution_id = uuid.uuid4().hex
        kind = self._definition.id
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
        return result

    def _close(self) -> None:
        """Close the client before its sandbox, including after failed turns."""
        if self._closed:
            return
        self._closed = True
        self._resources.close()

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

    def __init__(self, integration: LocalRunIntegration) -> None:
        self._channel = integration.control

    async def boundary(self) -> None:
        """Land stop or pause without consuming steering intended for an agent."""
        self._channel.raise_if_stopped()
        await asyncio.to_thread(self._channel.wait_while_paused)


class _RunState:
    """Typed durable state for a policy's declared portable slot."""

    def __init__(self, host: RunContext) -> None:
        self._host = host

    def _slot(self, model: type[T]) -> StateSlot[T]:
        setup = self._host.setup
        if setup.state_namespace is None or setup.state_model is not model:
            raise TypeError("policy state model does not match RunSetup")  # noqa: TRY003
        context = self._host.run_context
        return context.state.portable(setup.state_namespace).slot("state.json", model)

    async def load(self, model: type[T]) -> T | None:
        """Load the validated state after interrupted checkpoint recovery."""
        return await self._host.run_blocking(self._slot(model).load_optional)

    async def checkpoint(self, state: T, *, sequence: int) -> str:
        """Journal, commit, and publish one state transition with candidate edits."""
        async with self._host.parent_mutation_lock:
            return await self._host.run_blocking(self._checkpoint, state, sequence)

    def _checkpoint(self, state: T, sequence: int) -> str:
        context = self._host.run_context
        namespace = self._host.setup.state_namespace
        if namespace is None:
            raise TypeError("policy did not declare a durable state slot")  # noqa: TRY003
        transition = self._slot(type(state)).transition(state)
        context.begin_completed_round(sequence, state_transition=transition)
        context.persist_completed_round()
        context.publish_committed_state(namespace, state)
        revision = context.git.current_sha()
        if revision is None:
            raise RuntimeError("checkpoint completed without a Git revision")  # noqa: TRY003
        return revision


@dataclass(frozen=True, slots=True)
class MeasurementOptions:
    """Policy-selected metric axes, event label, and command override."""

    objectives: Sequence[Objective] = ()
    label: str | None = None
    execution_base: str | None = None


_DEFAULT_MEASUREMENT_OPTIONS = MeasurementOptions()


class _Evaluator:
    """Trusted checks and measurements in the parent run workspace."""

    def __init__(self, host: RunContext) -> None:
        self._host = host
        self._locks: dict[str | None, asyncio.Lock] = {None: asyncio.Lock()}

    def lock(self, scope: WorkspaceScope | None) -> asyncio.Lock:
        """Serialize gates only within the workspace they inspect."""
        key = scope.id if scope is not None else None
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def forget(self, scope: WorkspaceScope) -> None:
        """Release a discarded scope's synchronization state."""
        self._locks.pop(scope.id, None)

    async def check(
        self,
        process_id: str,
        *,
        label: str | None = None,
        execution_command: str | None = None,
        scope: WorkspaceScope | None = None,
    ) -> AccuracyGateResult:
        """Run the bundle's trusted accuracy command and reject input tampering."""
        async with self.lock(scope):
            context = self._host.workspaces.context(scope)
            timeout = framework_command_timeout(
                context, self._host.request.input_bundle.manifest.accuracy.timeout_seconds
            )
            return await self._host.run_blocking(
                run_accuracy_gate,
                context,
                process_id=process_id,
                timeout_seconds=timeout,
                execution_command=execution_command,
                round_label=label,
            )

    async def measure(
        self,
        output_slug: str,
        *,
        scope: WorkspaceScope | None = None,
        options: MeasurementOptions = _DEFAULT_MEASUREMENT_OPTIONS,
    ) -> BenchmarkGateResult:
        """Run and parse the bundle's declared trusted benchmark contract."""
        async with self.lock(scope):
            context = self._host.workspaces.context(scope)
            bundle = self._host.request.input_bundle
            timeout = framework_command_timeout(context, bundle.manifest.benchmark.timeout_seconds)
            return await self._host.run_blocking(
                run_benchmark_gate,
                context,
                result_spec=bundle.benchmark_result,
                result_protocol=bundle.benchmark_result_protocol,
                objectives=options.objectives,
                process_id=output_slug,
                output_slug=output_slug,
                timeout_seconds=timeout,
                execution_base=options.execution_base,
                round_label=options.label,
            )


class _Workspaces:
    """Own isolated worktrees and parent adoption for one run."""

    def __init__(self, host: RunContext) -> None:
        self._host = host
        self._scopes: dict[str, WorkspaceScope] = {}
        self._contexts: dict[str, _RunContext] = {}
        self._scope_locks: dict[str, asyncio.Lock] = {}

    async def fork(self, revision: str | None = None) -> WorkspaceScope:
        """Open an isolated worktree at a committed parent revision."""
        async with self._host.parent_mutation_lock:
            return await self._host.run_blocking(self._fork, revision)

    def _fork(self, revision: str | None) -> WorkspaceScope:
        parent = self._host.run_context
        base = revision or parent.git.current_sha()
        if base is None:
            raise RuntimeError("cannot fork a workspace without a committed revision")  # noqa: TRY003
        if not parent.run_environment_view.supports_parallel_candidate_evaluation:
            raise RuntimeError("run environment cannot open isolated candidate sandboxes")  # noqa: TRY003
        scope_id = f"s{uuid.uuid4().hex}"
        context = create_workspace_context(
            parent,
            WorkspaceContextSpec(
                scope_id=scope_id,
                revision=base,
                config=self._host.request.config,
                agent_backend=self._host.request.agent_backend,
                cli_provider=self._host.request.cli_provider,
            ),
        )
        scope = WorkspaceScope(id=scope_id, path=context.workspace, revision=base)
        self._scopes[scope_id] = scope
        self._contexts[scope_id] = context
        return scope

    async def snapshot(self, label: str, scope: WorkspaceScope | None = None) -> str:
        """Commit candidate edits and retain a fork's revision for later adoption."""
        if scope is None:
            async with self._host.parent_mutation_lock:
                return await self._host.run_blocking(self._snapshot_parent, label)
        lock = self._scope_lock(scope)
        async with lock:
            revision = await self._host.run_blocking(self._snapshot_scoped, label, scope)
            async with self._host.parent_mutation_lock:
                return await self._host.run_blocking(self._retain_scoped, scope, revision)

    def _snapshot_parent(self, label: str) -> str:
        parent = self._host.run_context
        parent.git.snapshot(label)
        revision = parent.git.current_sha()
        if revision is None:
            raise RuntimeError("workspace snapshot completed without a Git revision")  # noqa: TRY003
        return revision

    def _snapshot_scoped(self, label: str, scope: WorkspaceScope) -> str:
        current = self._require_scope(scope)
        git = self._contexts[current.id].git
        git.snapshot(label)
        revision = git.current_sha()
        if revision is None:
            raise RuntimeError("workspace snapshot completed without a Git revision")  # noqa: TRY003
        return revision

    def _retain_scoped(self, scope: WorkspaceScope, revision: str) -> str:
        current = self._require_scope(scope)
        self._host.run_context.git.retain_candidate(current.id, revision)
        current.revision = revision
        return revision

    async def adopt(self, revision: str, *, clean: bool = True) -> None:
        """Materialize a retained candidate revision in the parent workspace."""
        async with self._host.parent_mutation_lock:
            adopted = await self._host.run_blocking(
                self._host.run_context.git.checkout_tree, revision, clean=clean
            )
        if not adopted:
            raise RuntimeError(f"could not adopt candidate revision {revision!r}")  # noqa: TRY003

    async def discard(self, scope: WorkspaceScope) -> None:
        """Drain scoped agents and gates before removing their worktree."""
        async with self._host._spawn_lock:  # noqa: SLF001  # shared lifecycle lock
            self._require_scope(scope)
            errors: list[BaseException] = []
            for agent in tuple(self._host._agents.values()):  # noqa: SLF001
                if agent.scope_id != scope.id:
                    continue
                try:
                    await agent.close()
                except Exception as exc:  # noqa: BLE001  # finish scope cleanup
                    errors.append(exc)
            async with (
                self._host.evaluator.lock(scope),
                self._scope_lock(scope),
                self._host.parent_mutation_lock,
            ):
                try:
                    await self._host.run_blocking(self._discard, scope)
                except Exception as exc:  # noqa: BLE001  # report all cleanup errors
                    errors.append(exc)
                finally:
                    if scope.id not in self._scopes:
                        self._host.evaluator.forget(scope)
            if errors:
                raise BaseExceptionGroup("scoped agent cleanup failed", errors)  # noqa: TRY003

    def _discard(self, scope: WorkspaceScope) -> None:
        current = self._require_scope(scope)
        self._contexts[current.id].close()
        del self._scopes[current.id]
        del self._contexts[current.id]
        self._scope_locks.pop(current.id, None)

    def _scope_lock(self, scope: WorkspaceScope) -> asyncio.Lock:
        current = self._require_scope(scope)
        return self._scope_locks.setdefault(current.id, asyncio.Lock())

    def _require_scope(self, scope: WorkspaceScope) -> WorkspaceScope:
        current = self._scopes.get(scope.id)
        if current is not scope:
            raise ValueError("workspace scope is closed or belongs to another run")  # noqa: TRY003
        return current

    def context(self, scope: WorkspaceScope | None) -> _RunContext:
        """Resolve the sandbox-backed context for a live workspace scope."""
        if scope is None:
            return self._host.run_context
        current = self._require_scope(scope)
        return self._contexts[current.id]

    async def close(self) -> None:
        """Discard all remaining worktrees in reverse creation order."""
        errors: list[BaseException] = []
        for scope in reversed(tuple(self._scopes.values())):
            try:
                async with self._scope_lock(scope), self._host.parent_mutation_lock:
                    await asyncio.to_thread(self._discard, scope)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("workspace cleanup failed", errors)  # noqa: TRY003


class RunContext:
    """One run's resources and focused host capabilities."""

    def __init__(
        self,
        request: RunRequest,
        integration: LocalRunIntegration,
        *,
        setup: RunSetup,
        open_agent_environment: Callable[..., AgentEnvironment] | None,
    ) -> None:
        """Bind request, policy setup, and the application control channel."""
        self.request = request
        self.setup = setup
        self._integration = integration
        self._open_agent_environment = open_agent_environment
        self._context: _RunContext | None = None
        self._agents: dict[str, _LocalAgentHandle] = {}
        self._spawn_lock = asyncio.Lock()
        self.parent_mutation_lock = asyncio.Lock()
        self._blocking: set[asyncio.Task] = set()
        self._closed = False
        self.control = _RunControl(integration)
        self.state = _RunState(self)
        self.evaluator = _Evaluator(self)
        self.workspaces = _Workspaces(self)

    async def run_blocking[**P, Result](
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
    async def open(
        cls,
        request: RunRequest,
        integration: LocalRunIntegration,
        *,
        setup: RunSetup,
        open_agent_environment: Callable[..., AgentEnvironment] | None = None,
    ) -> AsyncIterator[RunContext]:
        """Construct and close the run, including after cancellation or setup failure."""
        host = cls(
            request,
            integration,
            setup=setup,
            open_agent_environment=open_agent_environment,
        )
        try:
            prepare = asyncio.create_task(asyncio.to_thread(host.prepare))
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

    def _validate_capabilities(self) -> None:
        request = self.request
        if request.run_environment is not None and request.run_environment.name == "skypilot":
            raise ConfigurationError(
                ConfigurationDiagnostic(
                    code="custom_orchestration_skypilot_unsupported",
                    stage="agent_capability_validation",
                    message=(
                        "custom orchestration agents are not supported on SkyPilot "
                        "until per-agent bridge ownership is implemented"
                    ),
                )
            )

    @property
    def workspace(self) -> Path:
        """Return the run's writable workspace."""
        return self.run_context.workspace

    @property
    def run_context(self) -> _RunContext:
        """Expose the current core context to policy-owned migration helpers."""
        if self._context is None:
            raise _RuntimeClosedError
        return self._context

    @property
    def agents(self) -> RunContext:
        """Return this host's agent capability."""
        return self

    def prepare(self) -> None:
        """Open the canonical run context once."""
        if not self.setup.use_default_agent:
            self._validate_capabilities()
        self._ensure_context()

    async def spawn(
        self, definition: AgentDefinition, *, scope: WorkspaceScope | None = None
    ) -> _LocalAgentHandle:
        """Open one independently configured agent in a live workspace."""
        async with self._spawn_lock:
            if self._closed:
                raise _RuntimeClosedError
            context = self.workspaces.context(scope)
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
        context: _RunContext,
        executor: ThreadPoolExecutor,
        *,
        scope_id: str | None,
    ) -> _LocalAgentHandle:
        """Open one sandbox and one agent client, with requested grants."""
        if self._closed:
            raise _RuntimeClosedError
        if not definition.id or definition.id in self._agents:
            raise _AgentRegistrationError(definition.id)
        if definition.spec.execution != AgentExecutionPolicy():
            raise _UnsupportedAgentExecutionPolicyError
        if scope_id is None and self._open_agent_environment is None:
            raise _MissingAgentHostError
        self._validate_capabilities()
        with ExitStack() as resources:
            if scope_id is None:
                opener = self._open_agent_environment
                if opener is None:
                    raise _MissingAgentHostError
                opened = opener(
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
            client = build_agent_client(
                spec=definition.spec,
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
        self._agents[definition.id] = handle
        return handle

    def _ensure_context(self) -> _RunContext:
        if self._closed:
            raise _RuntimeClosedError
        if self._context is not None:
            return self._context
        self._context = open_run_context(self.request, self.setup, self._integration)
        return self._context

    async def close(self) -> None:
        """Release agents in reverse spawn order, then the run context."""
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for operation in tuple(self._blocking):
            await _wait_until_done(operation)
            if error := operation.exception():
                errors.append(error)
        for agent in reversed(tuple(self._agents.values())):
            try:
                await agent.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        try:
            await self.workspaces.close()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        if self._context is not None:
            try:
                await asyncio.to_thread(self._context.close)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("run cleanup failed", errors)  # noqa: TRY003
