"""Local host for the public custom-orchestration agent capabilities.

``RunContext`` owns one run's resources (workspace, agents, state, events)
and exposes them through small, focused capabilities. The mechanics of each
capability live in a sibling module, split out of this one by what they own:

- `agents.py`: agent creation and `ctx.agents.turn` (rendering, isolation,
  retries, timeouts).
- `workspaces.py`: `ctx.workspaces` (root/isolated worktrees, snapshots,
  transactions, adoption).
- `state.py`: `ctx.state` (checkpoint/commit and the events they derive).
- `gates.py`: `ctx.gates`/`ctx.evaluator` (trusted accuracy/benchmark checks).
- `control.py`: `ctx.control` (the stop/pause/debug boundary).
- `environment.py`: `ctx.environment` (execution facts, candidate deployment).

This module keeps `RunContext` itself (construction, `open`/`close`,
low-level resource and agent-process ownership) and re-exports the public
names other modules already import from `vibesys.orchestration.runtime`, so
those external imports keep working unchanged.
"""

# Capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from vibesys.context import (
    borrow_run_agent_environment,
    open_run_resources,
    open_scoped_agent_environment,
)
from vibesys.events import FrameworkSource
from vibesys.orchestration.agents import _Agents, _LocalAgentHandle
from vibesys.orchestration.control import _RunControl
from vibesys.orchestration.environment import _Environment
from vibesys.orchestration.gates import (
    GateRecorder,
    GateRunResult,
    MeasurementOptions,
    _Evaluator,
)
from vibesys.orchestration.state import _RunState
from vibesys.orchestration.workspaces import (
    WorkspaceHandle,
    WorkspaceRestoreError,
    WorkspaceTransaction,
    WorkspaceTransactionKeep,
    _Workspaces,
)
from vibesys.render.sink import output_sink
from vibesys.run.agent_sessions import SynchronizedSessionStore
from vs_agent.api import AgentExecutionPolicy, AgentSessionState, build_agent_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from vibesys.backends.base import ComputeBackendImpl
    from vibesys.context import RunSetup, _RunResources
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.orchestration.state import _CommittedStateProjector
    from vibesys.run.event_journal import EventJournal
    from vibesys.run.integration import LocalRunIntegration
    from vibesys.runtime import AgentDefinition, WorkspaceScope
    from vs_agent.api import AgentClientProtocol

# Re-exported for callers that import these public names from this module
# rather than from the capability module that now owns them.
__all__ = [
    "GateRecorder",
    "GateRunResult",
    "MeasurementOptions",
    "RunContext",
    "WorkspaceHandle",
    "WorkspaceRestoreError",
    "WorkspaceTransaction",
    "WorkspaceTransactionKeep",
]


class _RuntimeClosedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("runtime is closed")


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


class RunContext:
    """One run's resources and focused host capabilities."""

    def __init__(  # noqa: PLR0913  # tracked: #288
        self,
        request: RunRequest,
        integration: LocalRunIntegration,
        *,
        setup: RunSetup,
        open_agent_environment: Callable[..., AgentEnvironment] | None,
        projector: _CommittedStateProjector | None = None,
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
        self._projector = projector
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
        # `gates` is the same instance as `evaluator`, under the name the
        # `run` gate API is meant to be reached by; `evaluator` stays for
        # `check`/`measure`/`reuse_accuracy` callers until they migrate too.
        self.gates = self.evaluator
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

    def warning(
        self,
        summary: str,
        *,
        detail: str | None = None,
        source: FrameworkSource = FrameworkSource.LOOP,
        source_label: str | None = None,
        round_label: str | None = None,
    ) -> None:
        """Publish one non-fatal framework fault as a FRAMEWORK_WARNING event."""
        output_sink().framework_warning(
            summary,
            detail=detail,
            source=source,
            source_label=source_label,
            round_label=round_label,
        )

    def run_configured(  # noqa: PLR0913
        self,
        *,
        run_log_path: str,
        project_root: str,
        model: str | None = None,
        objective: str | None = None,
        search_policy: str | None = None,
        benchmark_contract: bool = False,
        pareto_objectives: str | None = None,
    ) -> None:
        """Publish the one-per-run resolved loop configuration event."""
        output_sink().run_configured(
            run_log_path=run_log_path,
            project_root=project_root,
            model=model,
            objective=objective,
            search_policy=search_policy,
            benchmark_contract=benchmark_contract,
            pareto_objectives=pareto_objectives,
        )

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
    async def open(  # noqa: PLR0913  # tracked: #288
        cls,
        request: RunRequest,
        integration: LocalRunIntegration,
        *,
        setup: RunSetup,
        open_agent_environment: Callable[..., AgentEnvironment] | None = None,
        projector: _CommittedStateProjector | None = None,
        agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
        backend_factory: Callable[..., ComputeBackendImpl] | None = None,
    ) -> AsyncIterator[RunContext]:
        """Construct and close the run, including after cancellation or setup failure."""
        host = cls(
            request,
            integration,
            setup=setup,
            open_agent_environment=open_agent_environment,
            projector=projector,
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
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
            try:
                await self.workspaces.close()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if self._resource_owner is not None:
            try:
                await asyncio.to_thread(self._resource_owner.close)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        if errors:
            raise BaseExceptionGroup("run cleanup failed", errors)  # noqa: TRY003
