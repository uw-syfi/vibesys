"""Local host for the public custom-orchestration agent capabilities.

``RunContext`` owns one run's resources (workspace, agents, state, events)
and exposes them through small, focused capabilities. The mechanics of each
capability live in a sibling module, split out of this one by what they own:

- `agents.py`: composition for runtime-owned explicit agent sessions.
- `workspaces.py`: `ctx.workspaces` (root/isolated worktrees, snapshots,
  transactions, adoption).
- `state.py`: `ctx.state` (checkpoint/commit and the events they derive).
- `gates.py`: policy over runtime-owned trusted evaluation.
- `control.py`: `ctx.control` (the stop/pause/debug boundary).
- `environment.py`: `ctx.environment` (execution facts, candidate deployment).

This module keeps `RunContext` itself (construction, `open`/`close`,
low-level resource and agent-process ownership) and re-exports the public
names other modules already import from `vibesys.orchestration.runtime`, so
those external imports keep working unchanged.
"""

# Capabilities in this module share one private owner for resource lifetime.
# lint-waiver: LW-040107 [SLF001]; capabilities in this module share one private owner for resource lifetime.
# ruff: noqa: SLF001

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from vibesys.context import (
    RunSetup,
    _StateBinding,
    open_run_resources,
)
from vibesys.events import (
    CoreEventType,
    FrameworkSource,
    FrameworkWarningData,
    RunConfiguredData,
)
from vibesys.orchestration.agents import (
    _Agents,
    _AgentToolResolver,
)
from vibesys.orchestration.commands import _Commands
from vibesys.orchestration.control import _RunControl
from vibesys.orchestration.environment import _Environment
from vibesys.orchestration.gates import _EvaluationAdapter
from vibesys.orchestration.progress import _Progress
from vibesys.orchestration.skills import _Skills
from vibesys.orchestration.state import _StateCommitObserver
from vibesys.orchestration.workspace_resources import WorkspaceResourceProvider
from vs_agent.api import AgentSessionState, DurableSessionStore
from vs_runtime.api import ProfileExecution, RunFacts, State, Workspaces, WorkspaceSourceFact
from vs_runtime.api.infrastructure import OwnedWorkspaces, create_state, create_workspaces

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    from vibesys.context import _RunResources
    from vibesys.orchestration._host import _CommittedStateProjectorLike
    from vibesys.orchestration.environment import AgentEnvironment
    from vibesys.orchestration.request import RunRequest
    from vibesys.run.event_journal import EventJournal
    from vibesys.run.integration import LocalRunIntegration
    from vs_agent.api import AgentClientProtocol
    from vs_runtime.api import AgentRole, OrchestrationPlugin
    from vs_sandbox.api import ComputeBackendImpl

# Re-exported for callers that import these public names from this module
# rather than from the capability module that now owns them.
__all__ = [
    "RunContext",
]


class _RuntimeClosedError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("runtime is closed")


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


class RunContext:
    """One run's resources and focused host capabilities."""

    def __init__(  # noqa: PLR0913  # LW-040004 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
        self,
        request: RunRequest,
        integration: LocalRunIntegration,
        *,
        setup: RunSetup,
        open_agent_environment: Callable[..., AgentEnvironment] | None,
        projector: _CommittedStateProjectorLike | None = None,
        agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
        backend_factory: Callable[..., ComputeBackendImpl] | None = None,
        agent_roles: tuple[AgentRole, ...] = (),
        agent_tool_bindings: Mapping[str, _AgentToolResolver] | None = None,
        plugin: OrchestrationPlugin | None = None,
    ) -> None:
        """Bind request, policy setup, and the application control channel.

        ``agent_client_factory`` and ``backend_factory`` are injection seams a
        test uses in place of monkeypatching this module's real client/backend
        constructors (``build_agent_client``, ``create_compute_backend``). Each
        defaults to the real implementation when omitted, so production call
        sites are unchanged. ``agent_client_factory`` overrides
        :func:`vs_agent.api.build_agent_client`, looked up as this module's
        own (still independently patchable) ``build_agent_client`` global when
        no override is given.

        """
        if plugin is not None:
            if request.orchestration.id != plugin.id:
                message = (
                    f"selected orchestration {request.orchestration.id!r} does not match "
                    f"plugin {plugin.id!r}"
                )
                raise ValueError(message)
            if agent_roles and agent_roles != plugin.agents:
                message = "agent roles must come from the orchestration plugin"
                raise ValueError(message)
            agent_roles = plugin.agents
        self.request = request
        self._setup = setup
        self._integration = integration
        self._projector = projector
        self._backend_factory = backend_factory
        self._resource_owner: _RunResources | None = None
        self._facts: RunFacts | None = None
        self._session_store: DurableSessionStore | None = None
        self._blocking: set[asyncio.Task] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self.control = _RunControl(integration, debug=request.debug)
        self.commands = _Commands(self)
        self.skills = _Skills(self)
        self._state_model = plugin.state if plugin is not None else None
        self._state_namespace = (
            plugin.id if plugin is not None and plugin.state is not None else None
        )
        self._state: State | None = None
        self.evaluation = _EvaluationAdapter(self)
        self.agents = _Agents(
            self,
            agent_roles,
            agent_tool_bindings,
            control=integration.control,
            lifecycle_events=integration.agent_execution_event,
            open_agent_environment=open_agent_environment,
            client_factory=agent_client_factory,
        )
        self._workspaces: OwnedWorkspaces | None = None
        self.environment = _Environment(self)
        self.progress = _Progress(self)

    @property
    def events(self) -> EventJournal:
        """Return the run's semantic event journal."""
        return self._integration.events

    @property
    def workspaces(self) -> Workspaces:
        """Return the prepared runtime-owned workspace collection."""
        if self._workspaces is None:
            raise _RuntimeClosedError
        return self._workspaces

    @property
    def state(self) -> State:
        """Return plugin-bound state after the run resources are prepared."""
        if self._state is None:
            raise _RuntimeClosedError
        return self._state

    @property
    def run_id(self) -> str:
        """Return this run's stable identity."""
        return self._resources.run_id

    @property
    def facts(self) -> RunFacts:
        """Return prompt-visible facts fixed when run resources were prepared."""
        if self._facts is None:
            raise _RuntimeClosedError
        return self._facts

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
        self.events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary=summary,
                detail=detail,
                source=source,
                source_label=source_label,
            ),
            round_label=round_label,
        )

    def run_configured(  # noqa: PLR0913  # LW-040110 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
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
        first_line = next(
            (line for line in (objective or "").splitlines() if line.strip()),
            None,
        )
        self.events.emit(
            CoreEventType.RUN_CONFIGURED,
            data=RunConfiguredData(
                run_log_path=run_log_path,
                project_root=project_root,
                model=model,
                objective=first_line,
                search_policy=search_policy,
                benchmark_contract=benchmark_contract,
                pareto_objectives=pareto_objectives,
            ),
        )

    def switch_log(self, label: int | str) -> None:
        """Select a policy phase log for subsequent output and agent turns."""
        self._resources.switch_log_file(label)

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
        setup: RunSetup | None = None,
        open_agent_environment: Callable[..., AgentEnvironment] | None = None,
        projector: _CommittedStateProjectorLike | None = None,
        agent_client_factory: Callable[..., AgentClientProtocol] | None = None,
        backend_factory: Callable[..., ComputeBackendImpl] | None = None,
        agent_roles: tuple[AgentRole, ...] = (),
        agent_tool_bindings: Mapping[str, _AgentToolResolver] | None = None,
        plugin: OrchestrationPlugin | None = None,
    ) -> AsyncIterator[RunContext]:
        """Construct and close the run, including after cancellation or setup failure."""
        if setup is None:
            if plugin is None:
                message = "legacy orchestration hosts require an explicit setup"
                raise TypeError(message)
            setup = RunSetup()
        host = cls(
            request,
            integration,
            setup=setup,
            open_agent_environment=open_agent_environment,
            projector=projector,
            agent_client_factory=agent_client_factory,
            backend_factory=backend_factory,
            agent_roles=agent_roles,
            agent_tool_bindings=agent_tool_bindings,
            plugin=plugin,
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
        resources = self._ensure_resources()
        self._workspaces = create_workspaces(WorkspaceResourceProvider(self))
        self._state = create_state(
            self._state_model,
            resources._round_transaction_coordinator,
            self._workspaces,
            (
                _StateCommitObserver(self, self._state_namespace)
                if self._state_namespace is not None
                else None
            ),
        )
        bundle = self.request.input_bundle
        view = resources.run_environment_view
        self._facts = RunFacts(
            domain_id=bundle.domain.value,
            objective=self.request.objective or bundle.objective,
            environment_notes=view.prompt_notes,
            profile_execution=ProfileExecution(view.profile_execution),
            objective_location=view.paths.objective,
            reference_location=resources.ref_name,
            accuracy_command=view.paths.accuracy_command,
            benchmark_command=view.paths.benchmark_command,
            accuracy_configured=bool(view.paths.accuracy_command),
            benchmark_configured=(
                bundle.benchmark_result is not None or bundle.benchmark_result_protocol is not None
            ),
            profiler_id=resources.profiler_kind.value,
            workspace_sources=tuple(
                WorkspaceSourceFact(name=source.name, dest=source.dest)
                for source in bundle.workspace_sources
            ),
        )

    def _ensure_resources(self) -> _RunResources:
        if self._closed:
            raise _RuntimeClosedError
        if self._resource_owner is not None:
            return self._resource_owner
        self._resource_owner = open_run_resources(
            self.request,
            self._setup,
            self._integration,
            state_binding=(
                _StateBinding(self._state_namespace, self._state_model)
                if self._state_namespace is not None and self._state_model is not None
                else None
            ),
            backend_factory=self._backend_factory,
        )
        self._session_store = DurableSessionStore(
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
        self.agents._mark_closed()
        errors: list[BaseException] = []
        for operation in tuple(self._blocking):
            await _wait_until_done(operation)
            if error := operation.exception():
                errors.append(error)
        try:
            await self.agents.close()
        except BaseException as exc:  # noqa: BLE001  # lint-waiver: LW-040112 [BLE001]; handle cleanup below must continue if explicit-session cleanup fails.
            errors.append(exc)
        if self._workspaces is not None:
            try:
                await self._workspaces.close()
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
