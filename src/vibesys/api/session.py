"""Session contracts and the `create_session` entry point.

`RunSession` is sliced into three capability sub-protocols (query, workspace,
control) plus lifecycle methods, so a consumer can be granted a narrower
capability than the full session -- for example the chat agent's tool
projection (`vs_agent.expose_as_tools`) grants only the slice a given tool
needs.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol, cast

from vibesys.api._dispatch import dispatch_loop, resolved_run_id
from vibesys.api.contracts import RunResult, RunStatus
from vibesys.domains.environment import EnvironmentBindMount
from vibesys.events import CoreEventType, EventStatus, RunStartedData
from vibesys.run.integration import LocalRunIntegration
from vibesys.skills import platform_skill_selection
from vs_agent.api import MCPServerSpec, expose_as_tools
from vs_project.api import Project
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.api.contracts import AgentEnvironment, EventSink, RunView
    from vibesys.api.run_request import RunRequestLike
    from vibesys.config import Config
    from vibesys.orchestration.contracts import OrchestrationRegistry
    from vibesys.run.integration import RunResourceHandoff
    from vibesys.sandbox.run_environment import RunEnvironmentSession
    from vibesys.skills import SkillSelection
    from vs_sandbox.api import ProjectPathPolicy, Sandbox


class RunQuery(Protocol):
    """Semantic reads of a run's authoritative facts."""

    def view(self) -> RunView:
        """Return the current read-only snapshot of this run."""
        ...


class RunWorkspace(Protocol):
    """Read-only access to a run's workspace handle."""

    def workspace(self) -> HostResource:
        """Return the host resource for this run's workspace."""
        ...


class RunControl(Protocol):
    """Messages to the run loop, the sole writer of run state.

    `steer`/`pause`/`resume`/`stop` are messages to the writer, which holds
    an exclusive write lease, not writes performed by the caller. `pause`
    means: reach the next safe boundary, checkpoint durable state, and
    release the lease. `resume` means: acquire the lease, restore the
    checkpoint, and continue. A cross-process resume is
    `create_session(RunRequest(resume=ResumeRef(run_id)))`.
    """

    def steer(self, text: str) -> None:
        """Send free-text steering input to the active run."""
        ...

    def pause(self) -> None:
        """Request the run reach its next safe boundary and release the write lease."""
        ...

    def resume(self) -> None:
        """Request the run acquire the write lease and continue from checkpoint."""
        ...

    def stop(self) -> None:
        """Request the run terminate."""
        ...


class RunAgentHost(Protocol):
    """Capability to open a live agent-construction environment for this run."""

    def open_agent_environment(
        self,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> AgentEnvironment:
        """Open this run's environment for agent construction, plus extra *mounts*.

        *mounts* are folded into the run's own environment request the same
        way `server.chat.factory.build_chat_agent` folds its one
        server-evidence mount today: each becomes an
        `vibesys.domains.environment.EnvironmentBindMount` at the mount's own
        `HostResource.agent_path` (or the host path unchanged, when unset).
        Backend and provider default to the run's selection; a spawned agent
        may override them so container authentication matches its `AgentSpec`.
        """
        ...


class RunSession(RunQuery, RunWorkspace, RunControl, RunAgentHost, Protocol):
    """One live or resumable run: query + workspace + control + lifecycle."""

    def start(self) -> None:
        """Begin executing the run."""
        ...

    async def await_result(self) -> RunResult:
        """Wait for the run to reach a terminal state and return its outcome."""
        ...

    def on_committed_view(
        self, listener: Callable[[RunView, tuple[str, ...] | None], None]
    ) -> None:
        """Register a listener for policy views and policy-defined changed keys."""
        ...

    def on_run_resources(self, listener: Callable[[RunResourceHandoff], None]) -> None:
        """Register the sole application consumer of this run's resource handoff."""
        ...


def create_session(
    request: RunRequestLike,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry | None = None,
) -> RunSession:
    """Build a session for *request*, publishing its event stream to *sink*.

    Headless calls `create_session(req, sink=renderer.handle).start()`
    then `await session.await_result()`; server does the same with its own
    presentation sink, and reaches optional committed-state/resource-handoff
    seams through `on_committed_view`/`on_run_resources` instead of an
    injected integration object. Pass a registry to execute a custom
    orchestration ID; otherwise the built-in registry is used.
    """
    return _LocalRunSession(request, sink=sink, registry=registry)


class _LocalRunSession:
    """`RunSession` that runs one loop function in-process via `asyncio.to_thread`.

    `RunControl` methods write to `self._integration.control`, the same
    `vibesys.run.run_control.RunControlChannel` `_RunContext.invoke` reads at
    each invocation boundary (see `vibesys.context._RunContext.invoke`).
    """

    def __init__(
        self,
        request: RunRequestLike,
        *,
        sink: EventSink,
        registry: OrchestrationRegistry | None,
    ) -> None:
        self._request = request
        self._sink = sink
        if registry is None:
            from vibesys.loops.registry import (  # noqa: PLC0415
                built_in_orchestrations,
            )

            registry = built_in_orchestrations()
        self._registry = registry
        self._policy = self._registry.resolve(request.orchestration_id)
        self._integration = LocalRunIntegration()
        self._integration.add_committed_state_listener(self._handle_committed_state)
        self._integration.add_resource_listener(self._handle_resources)
        self._committed_view_listener: Callable[[RunView, tuple[str, ...] | None], None] | None = (
            None
        )
        self._resource_listener: Callable[[RunResourceHandoff], None] | None = None
        self._resource_handoff: RunResourceHandoff | None = None
        self._unsubscribe: Callable[[], None] | None = None
        # A session exists to run its request, so it reads as active from
        # construction (before `start()`/`await_result()`) through to
        # `_run_sync` recording its terminal outcome below.
        self._status: RunStatus = RunStatus.ACTIVE

    def on_committed_view(
        self, listener: Callable[[RunView, tuple[str, ...] | None], None]
    ) -> None:
        """Register a listener for policy views and policy-defined changed keys."""
        self._committed_view_listener = listener

    def _handle_committed_state(
        self,
        namespace: str,
        state: BaseModel,
        changed_keys: tuple[str, ...] | None,
    ) -> None:
        if self._committed_view_listener is None:
            return
        view = self._policy.project_committed(namespace, state, run_id=self._run_id())
        if view is not None:
            self._committed_view_listener(view, changed_keys)

    def on_run_resources(self, listener: Callable[[RunResourceHandoff], None]) -> None:
        """Register the sole application consumer of this run's resource handoff."""
        self._resource_listener = listener

    def _handle_resources(self, handoff: RunResourceHandoff) -> None:
        self._resource_handoff = handoff
        if self._resource_listener is not None:
            self._resource_listener(handoff)

    def _run_id(self) -> str:
        """Use the provisioned ID once a custom runtime has created its run."""
        if self._resource_handoff is not None:
            return self._resource_handoff.run_id
        return resolved_run_id(self._request)

    def open_agent_environment(
        self,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> AgentEnvironment:
        """Open this run's environment for agent construction, plus extra *mounts*.

        Reads the run-resource facts this session already captured from its
        own `on_run_resources` seam (`_handle_resources`), so this can only
        be called once the run has published them -- in practice, from a
        listener registered through `on_run_resources` itself. Opens through
        the same `RunEnvironment`/`RunEnvironmentRequest` the run itself
        used, folding *mounts* into the request the way
        `server.chat.factory.build_chat_agent` folds its one server-evidence
        mount today. `backends`/`use_docker`/`isolated` stay at their local
        defaults unless this run's environment is sandboxed, matching that
        function's sandboxed branch.
        """
        handoff = self._resource_handoff
        if handoff is None:
            raise RuntimeError(  # noqa: TRY003
                "open_agent_environment() called before this run published its resources "
                "(register a listener via on_run_resources first)"
            )
        request = replace(
            handoff.environment_request,
            agent_backend=(
                agent_backend
                if agent_backend is not None
                else handoff.environment_request.agent_backend
            ),
            cli_provider=(
                cli_provider
                if cli_provider is not None
                else handoff.environment_request.cli_provider
            ),
            environment_bind_mounts=(
                *handoff.environment_request.environment_bind_mounts,
                *(_environment_bind_mount(mount) for mount in mounts),
            ),
        )
        opened = handoff.environment.open(request)
        backends: dict[str, Sandbox] | None = None
        use_docker = False
        isolated = False
        if handoff.run_environment_sandboxed:
            backends = {"chat": opened.sandbox}
            use_docker = opened.view.cli_sandboxed
            isolated = opened.view.isolated
        return _OpenedAgentEnvironment(
            opened,
            config=handoff.config,
            skill_selection=platform_skill_selection(handoff.compute_backend),
            skill_source_dirs=handoff.skill_source_dirs,
            project_path_policy=handoff.project_path_policy,
            host_resources=handoff.host_resources,
            backends=backends,
            use_docker=use_docker,
            isolated=isolated,
            run_id=handoff.run_id,
            project=handoff.project,
        )

    def start(self) -> None:
        """Subscribe `sink` to this run's event stream."""
        self._unsubscribe = self._integration.events.subscribe(self._sink)

    async def await_result(self) -> RunResult:
        """Run the selected loop function on a worker thread and await its outcome."""
        return await asyncio.to_thread(self._run_sync)

    def _run_sync(self) -> RunResult:
        request = self._request
        try:
            description = self._policy.describe(request)
            self._integration.events.emit(
                CoreEventType.RUN_STARTED,
                status=EventStatus.ACTIVE,
                data=RunStartedData(
                    outer_loop=request.orchestration_id,
                    input=str(request.input_bundle.root),
                    max_rounds=description.max_rounds,
                    expected_roles=description.expected_roles,
                ),
            )
            succeeded = dispatch_loop(
                request,
                self._integration,
                registry=self._registry,
                open_agent_environment=self.open_agent_environment,
                implementation=self._policy,
            )
        except BaseException as exc:
            self._status = RunStatus.FAILED
            self._integration.events.emit(
                CoreEventType.RUN_FAILED,
                f"{type(exc).__name__}: {exc}",
                status=EventStatus.FAILED,
            )
            raise
        else:
            self._status = RunStatus.COMPLETED if succeeded else RunStatus.FAILED
            self._integration.events.emit(
                CoreEventType.RUN_FINISHED if succeeded else CoreEventType.RUN_FAILED,
                status=EventStatus.COMPLETED if succeeded else EventStatus.FAILED,
            )
            return RunResult(
                run_id=self._run_id(),
                loop=request.orchestration_id,
                succeeded=succeeded,
            )
        finally:
            if self._unsubscribe is not None:
                self._unsubscribe()
            self._integration.close()

    def view(self) -> RunView:
        """Project this session's live run into a read-only `RunView`.

        Reopens `Project`/agent state on every call rather than caching: this
        session has no subscription to its own run's writes, so a cache could
        only go stale. `status` reflects `_run_sync`'s own progress
        (`ACTIVE` until it returns or raises), not a re-derivation from state.
        """
        return self._policy.view(
            Project.open(self._request.project_root),
            run_id=self._run_id(),
            status=self._status,
            loop=self._request.orchestration_id,
        )

    def workspace(self) -> HostResource:
        """Return this session's project root as a read-only host resource.

        The run's workspace *is* the project's Git worktree (see
        `vibesys.run.git_tracker.GitTracker`); there is no separate per-run
        checkout directory to point at instead.
        """
        return HostResource(
            path=self._request.project_root,
            access=HostResourceAccess.READ_ONLY,
            purpose=f"live workspace for run {resolved_run_id(self._request)!r}",
        )

    def steer(self, text: str) -> None:
        """Send free-text steering input to the active run."""
        self._integration.control.queue_steer(text)

    def pause(self) -> None:
        """Request the run reach its next safe boundary and release the write lease."""
        self._integration.control.request_pause()

    def resume(self) -> None:
        """Request the run acquire the write lease and continue from checkpoint."""
        self._integration.control.resume()

    def stop(self) -> None:
        """Request the run terminate."""
        self._integration.control.request_stop()


class _AgentPathSandbox(Protocol):
    """The one lookup `_OpenedAgentEnvironment.agent_path` needs from a sandbox.

    Mirrors `vibesys.sandbox.run_environment._AgentPathSandbox`: every
    sandbox kind `RunEnvironment.open` can return (host-only or Docker)
    implements this, even though `vs_sandbox.execution.Sandbox` itself does
    not declare it.
    """

    def agent_path(self, host_path: Path | str) -> str: ...


@dataclass(frozen=True, slots=True)
class _OpenedAgentEnvironment:
    """`AgentEnvironment` backed by one already-opened `RunEnvironmentSession`."""

    _session: RunEnvironmentSession
    config: Config
    skill_selection: SkillSelection
    skill_source_dirs: tuple[Path, ...]
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]
    backends: dict[str, Sandbox] | None
    use_docker: bool
    isolated: bool
    run_id: str
    project: Project

    def agent_path(self, host: Path) -> str:
        """Map a host path to its path inside this environment's sandbox."""
        return cast("_AgentPathSandbox", self._session.sandbox).agent_path(host)

    def investigation_tools(self) -> tuple[MCPServerSpec, ...]:
        """Build the read-only MCP tool server for investigating this run's history.

        Launches `vibesys.api.chat_tools_server` with the project root
        translated into this environment's own sandbox path
        (`self.agent_path`), so the subprocess -- which the agent's own
        driver spawns inside that sandbox -- can resolve it.
        """
        descriptor = expose_as_tools(
            name="vibesys-run",
            entrypoint_module="vibesys.api.chat_tools_server",
            entrypoint_args=(
                "--run-id",
                self.run_id,
                "--project-root",
                self.agent_path(self.project.root),
            ),
        )
        return (
            MCPServerSpec(
                name=descriptor.name,
                command=descriptor.command,
                args=descriptor.args,
                env=descriptor.env,
            ),
        )

    def close(self) -> None:
        """Release the opened environment session."""
        self._session.close()


def _environment_bind_mount(mount: HostResource) -> EnvironmentBindMount:
    """Fold one requested host mount into an `EnvironmentBindMount`.

    `HostResource.agent_path` names the fixed container path a caller wants
    a resource presented at (its own docstring: unset means "imported at its
    own host path"), so that is exactly the container path this mount asks
    for, falling back to the host path unchanged when unset.
    """
    return EnvironmentBindMount(
        mount.path,
        mount.agent_path if mount.agent_path is not None else str(mount.path),
        read_only=mount.access is HostResourceAccess.READ_ONLY,
    )
