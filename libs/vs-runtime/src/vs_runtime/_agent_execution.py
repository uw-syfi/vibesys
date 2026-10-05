"""Thread-affine ownership of one explicit agent execution."""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from vs_agent.api import (
    AgentExecutionPolicy,
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentSpawnError,
    AgentTurnExecutor,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    InvalidResponse,
    MCPServerSpec,
    SessionConfigurationError,
    SessionResumeError,
    Unknown,
    build_agent_client,
    parse_typed_response,
)
from vs_runtime._agent_lifecycle import (
    AgentExecutionFinished,
    AgentExecutionLifecycleEvent,
    AgentExecutionLifecycleSink,
    AgentExecutionStarted,
    AgentExecutionStatus,
)
from vs_sandbox.api import EnvironmentBindMount, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import (
        AgentCapabilities,
        AgentClientProtocol,
        AgentEventSink,
        AgentInvocationStore,
        AgentSessionCheckpoint,
        AgentSessionKey,
        AgentSpec,
        InvocationOutcome,
        SessionStore,
        SkillSelection,
        ToolServerDescriptor,
    )
    from vs_prompts.api import RenderedPrompt
    from vs_runtime._run_control import RunControlChannel
    from vs_runtime._run_environment import RunEnvironmentRequest, RunEnvironmentSession
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox

ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AgentExecutionConfiguration:
    """Composition-resolved, session-fixed inputs for one agent execution."""

    agent_id: str
    spec: AgentSpec
    resources: tuple[HostResource, ...] = ()
    reasoning_effort: str | None = None


class AgentExecutionEnvironment(Protocol):
    """One scoped environment opened and closed on the agent worker thread."""

    @property
    def skill_source_dirs(self) -> tuple[Path, ...]: ...

    @property
    def skill_selection(self) -> SkillSelection: ...

    @property
    def project_path_policy(self) -> ProjectPathPolicy: ...

    @property
    def host_resources(self) -> tuple[HostResource, ...]: ...

    @property
    def backends(self) -> dict[str, Sandbox] | None: ...

    @property
    def use_docker(self) -> bool: ...

    def close(self) -> None:
        """Release the scoped environment idempotently."""
        ...


class _AgentPathEnvironment(Protocol):
    """Narrow capability used only when exporting an agent-visible path."""

    def agent_path(self, host_path: Path | str) -> str: ...


class SharedAgentEnvironmentConflictError(ValueError):
    """A shared run session cannot satisfy agent-specific environment inputs."""

    def __init__(self) -> None:
        super().__init__("a shared agent environment cannot override backend, provider, or mounts")


@dataclass(slots=True)
class ScopedAgentEnvironment:
    """Runtime-owned agent view over a borrowed or independently opened session."""

    session: RunEnvironmentSession
    skill_source_dirs: tuple[Path, ...]
    skill_selection: SkillSelection
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]
    backends: dict[str, Sandbox] | None
    use_docker: bool
    owns_session: bool
    _closed: bool = False

    def close(self) -> None:
        """Release an owned session exactly once; borrowed sessions remain open."""
        if self._closed:
            return
        self._closed = True
        if self.owns_session:
            self.session.close()

    def agent_path(self, host_path: Path | str) -> str:
        """Translate a host path through this environment's active sandbox."""
        return cast("_AgentPathSandbox", self.session.sandbox).agent_path(host_path)


class _AgentPathSandbox(Protocol):
    """Path translation implemented by every supported agent sandbox."""

    def agent_path(self, host_path: Path | str) -> str: ...


def open_agent_execution_environment(  # noqa: PLR0913  # lint-waiver: LW-954433 [PLR0913]; these are independent resolved lower-layer resources; bundling them would create the broad environment DTO this composition seam replaces.
    base_request: RunEnvironmentRequest,
    shared_session: RunEnvironmentSession,
    *,
    share_session: bool,
    skill_source_dirs: tuple[Path, ...],
    skill_selection: SkillSelection,
    host_resources: tuple[HostResource, ...],
    mounts: tuple[HostResource, ...] = (),
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession],
) -> ScopedAgentEnvironment:
    """Open one agent environment, borrowing the run session when required.

    A shared session fixes backend, provider, and mounts at workspace-open time.
    Independently opened sessions inherit that request and add only the explicit
    per-agent overrides and resources supplied here.
    """
    if share_session:
        if (
            mounts
            or (agent_backend is not None and agent_backend != base_request.agent_backend)
            or (cli_provider is not None and cli_provider != base_request.cli_provider)
        ):
            raise SharedAgentEnvironmentConflictError
        session = shared_session
        request = base_request
        owns_session = False
    else:
        request = replace(
            base_request,
            agent_backend=(
                agent_backend if agent_backend is not None else base_request.agent_backend
            ),
            cli_provider=(cli_provider if cli_provider is not None else base_request.cli_provider),
            environment_bind_mounts=(
                *base_request.environment_bind_mounts,
                *(
                    EnvironmentBindMount(
                        mount.path,
                        mount.agent_path if mount.agent_path is not None else str(mount.path),
                        read_only=mount.access is HostResourceAccess.READ_ONLY,
                    )
                    for mount in mounts
                ),
            ),
        )
        session = open_session(request)
        owns_session = True

    sandboxed = session.view.cli_sandboxed
    return ScopedAgentEnvironment(
        session=session,
        skill_source_dirs=skill_source_dirs,
        skill_selection=skill_selection,
        project_path_policy=request.project_path_policy,
        host_resources=host_resources,
        backends={"chat": session.sandbox} if sandboxed else None,
        use_docker=sandboxed,
        owns_session=owns_session,
    )


type ScopedAgentEnvironmentOpener = Callable[
    [AgentExecutionConfiguration], AgentExecutionEnvironment
]


@dataclass(frozen=True, slots=True)
class AgentExecutionScope:
    """Run-owned effects and paths fixed for one agent execution."""

    workspace_path: Path
    log_directory: Path
    open_environment: ScopedAgentEnvironmentOpener
    current_log_file: Callable[[], TextIO]
    environment_variables: Callable[[], Mapping[str, str]]
    invocation_store: Callable[[AgentSessionKey], AgentInvocationStore] | None = None
    #: Root of the run's dedicated agent CLI homes (see ``build_agent_client``).
    agent_homes_directory: Path | None = None


type AgentMessageRouter = Callable[[str, tuple[str, ...]], str]
type AgentClientFactory = Callable[..., AgentClientProtocol]


@dataclass(frozen=True, slots=True)
class AgentResumeConfiguration:
    """Requested schema and tool configuration for continuation and replay."""

    system_prompt: str
    response: type[BaseModel] | None
    tool_servers: tuple[ToolServerDescriptor, ...]


class AgentExecutionClosedError(RuntimeError):
    """Raised when a turn is attempted after execution teardown began."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent {agent_id!r} is closed")


async def _wait_until_done(task: asyncio.Future[Any]) -> None:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-837205 [BLE001]; execution construction cleanup must inspect the completed worker outcome.
            break


def _status(error: BaseException | None) -> AgentExecutionStatus:
    if error is None:
        return AgentExecutionStatus.COMPLETED
    if isinstance(error, asyncio.CancelledError) or type(error).__name__ == "CancelledError":
        return AgentExecutionStatus.CANCELLED
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return AgentExecutionStatus.INTERRUPTED
    return AgentExecutionStatus.FAILED


class RuntimeAgentExecution:
    """One environment and client confined to a single worker thread."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-837206 [PLR0913]; the execution owns independently configured lower-layer resources and semantic sinks.
        self,
        configuration: AgentExecutionConfiguration,
        scope: AgentExecutionScope,
        client: AgentClientProtocol,
        resources: ExitStack,
        executor: ThreadPoolExecutor,
        environment: AgentExecutionEnvironment,
        *,
        control: RunControlChannel,
        lifecycle: AgentExecutionLifecycleSink,
        route_message: AgentMessageRouter,
    ) -> None:
        self._configuration = configuration
        self._scope = scope
        self._client = client
        self._resources = resources
        self._executor = executor
        self._environment = environment
        self._control = control
        self._lifecycle = lifecycle
        self._route_message = route_message
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._sessions: ClientAgentSessions | None = None
        self._session_specs: dict[AgentSessionKey, AgentSessionSpec] = {}

    @classmethod
    async def open(  # noqa: PLR0913  # lint-waiver: LW-837207 [PLR0913]; composition supplies independent lower-layer effects once; callers use the resulting deep execution object.
        cls,
        configuration: AgentExecutionConfiguration,
        scope: AgentExecutionScope,
        *,
        session_store: SessionStore | None,
        control: RunControlChannel,
        lifecycle: AgentExecutionLifecycleSink,
        agent_events: AgentEventSink,
        route_message: AgentMessageRouter,
        client_factory: AgentClientFactory = build_agent_client,
    ) -> RuntimeAgentExecution:
        executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"vs-agent-{configuration.agent_id}",
        )
        opened = asyncio.get_running_loop().run_in_executor(
            executor,
            partial(
                cls._open_sync,
                configuration,
                scope,
                executor,
                session_store=session_store,
                control=control,
                lifecycle=lifecycle,
                agent_events=agent_events,
                route_message=route_message,
                client_factory=client_factory,
            ),
        )
        try:
            return await asyncio.shield(opened)
        except asyncio.CancelledError as cancelled:
            await _wait_until_done(opened)
            if opened.exception() is not None:
                await asyncio.to_thread(executor.shutdown, wait=True)
            else:
                cleanup = asyncio.create_task(opened.result().close())
                await _wait_until_done(cleanup)
                if error := cleanup.exception():
                    cancelled.add_note(f"canceled agent construction cleanup failed: {error}")
            raise
        except BaseException:
            await asyncio.to_thread(executor.shutdown, wait=True)
            raise

    @classmethod
    def _open_sync(  # noqa: PLR0913  # lint-waiver: LW-837208 [PLR0913]; mirrors open's one-time composition inputs on the worker thread.
        cls,
        configuration: AgentExecutionConfiguration,
        scope: AgentExecutionScope,
        executor: ThreadPoolExecutor,
        *,
        session_store: SessionStore | None,
        control: RunControlChannel,
        lifecycle: AgentExecutionLifecycleSink,
        agent_events: AgentEventSink,
        route_message: AgentMessageRouter,
        client_factory: AgentClientFactory,
    ) -> RuntimeAgentExecution:
        with ExitStack() as resources:
            environment = scope.open_environment(configuration)
            resources.callback(environment.close)
            backends = (
                {configuration.agent_id: environment.backends["chat"]}
                if environment.backends is not None
                else None
            )
            try:
                client = client_factory(
                    spec=configuration.spec,
                    session_store=session_store,
                    backends=backends,
                    skill_source_dirs=list(environment.skill_source_dirs),
                    skill_selection=environment.skill_selection,
                    run_log_file=scope.current_log_file(),
                    use_docker=environment.use_docker,
                    log_dir=scope.log_directory,
                    agent_homes_dir=scope.agent_homes_directory,
                    host_resources=(*environment.host_resources, *configuration.resources),
                    project_path_policy=environment.project_path_policy,
                    require_host_sandbox=not environment.use_docker,
                    events=agent_events,
                )
            except (OSError, ImportError) as error:
                raise AgentSpawnError(configuration.spec.provider, str(error)) from error
            resources.callback(client.close)
            return cls(
                configuration,
                scope,
                client,
                resources.pop_all(),
                executor,
                environment,
                control=control,
                lifecycle=lifecycle,
                route_message=route_message,
            )

    @property
    def has_session_transport(self) -> bool:
        """Whether composition supplied durable invocation persistence."""
        return self._scope.invocation_store is not None

    @property
    def capabilities(self) -> AgentCapabilities:
        return self._client.capabilities

    @property
    def backend_name(self) -> str:
        return self._client.backend_name

    @property
    def driver_name(self) -> str | None:
        return self._client.driver_name

    @property
    def provider(self) -> str | None:
        return self._client.provider

    def agent_path(self, host_path: Path | str) -> str:
        """Translate a host resource path through this execution's environment."""
        return cast("_AgentPathEnvironment", self._environment).agent_path(host_path)

    @property
    def model(self) -> str | None:
        return self._client.model_for_kind(self._configuration.agent_id)

    @property
    def reasoning_effort(self) -> str | None:
        return self._configuration.reasoning_effort

    async def execute(  # noqa: PLR0913  # lint-waiver: LW-837209 [PLR0913]; fixed provider-turn inputs belong to the lower client contract and bundling them would expose an extra message object.
        self,
        message: str,
        *,
        system_prompt: str,
        response: type[ResponseT] | None,
        label: str,
        session_key: AgentSessionKey,
        tool_servers: tuple[ToolServerDescriptor, ...] | None,
        invocation_id: str | None = None,
    ) -> str | ResponseT:
        if self._close_task is not None:
            raise AgentExecutionClosedError(self._configuration.agent_id)
        turn = asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._run_turn,
                message,
                system_prompt=system_prompt,
                response=response,
                label=label,
                session_key=session_key,
                tool_servers=tool_servers,
                invocation_id=invocation_id,
            ),
        )
        try:
            return await asyncio.shield(turn)
        except asyncio.CancelledError as cancelled:
            # Stop the provider turn instead of waiting out its own timeout:
            # the worker thread ends only when the turn does.
            self._client.cancel()
            await _wait_until_done(turn)
            if error := turn.exception():
                cancelled.add_note(f"canceled agent turn also failed: {error}")
            raise

    def _run_turn(  # noqa: PLR0913  # lint-waiver: LW-837210 [PLR0913]; worker-side execution receives the same fixed provider-turn inputs as execute.
        self,
        message: str,
        *,
        system_prompt: str,
        response: type[ResponseT] | None,
        label: str,
        session_key: AgentSessionKey,
        tool_servers: tuple[ToolServerDescriptor, ...] | None,
        invocation_id: str | None = None,
    ) -> str | ResponseT:
        if self._closed:
            raise AgentExecutionClosedError(self._configuration.agent_id)
        self._client.set_log_file(self._scope.current_log_file())
        self._control.raise_if_stopped()
        self._control.wait_while_paused()
        steering = tuple(self._control.take_pending_steer())
        routed = self._route_message(message, steering)
        execution_id = invocation_id or uuid.uuid4().hex
        agent_id = self._configuration.agent_id
        if steering:
            self._control.notify_steer_consumed(
                agent_kind=agent_id,
                round_label=label,
                execution_id=execution_id,
            )
        self._lifecycle(
            AgentExecutionStarted(
                agent_id=agent_id,
                label=label,
                execution_id=execution_id,
                system_prompt=system_prompt,
                user_prompt=routed,
                driver=self._client.driver_name,
                provider=self._client.provider,
                model=self._client.model_for_kind(agent_id),
            )
        )
        result: str | ResponseT | None = None
        error: BaseException | None = None
        try:
            environment = (
                {} if self._environment.use_docker else dict(self._scope.environment_variables())
            )
            resolved_tools = list(tool_servers) if tool_servers is not None else None
            if invocation_id is not None:
                transport = self._transport(session_key)
                outcome = transport.start(
                    session_key,
                    self._session_spec(session_key, tool_servers),
                    AgentTurnRequest(
                        message=routed,
                        instructions=system_prompt,
                        output_schema=response,
                        timeout=self._turn_timeout(),
                        label=label,
                        invocation_id=invocation_id,
                    ),
                )
                result = self._initial_result(session_key, outcome, response)
            elif response is None:
                result = self._client.invoke_text(
                    kind=agent_id,
                    workspace=self._scope.workspace_path,
                    system_prompt=system_prompt,
                    user_prompt=routed,
                    round_label=label,
                    env=environment,
                    invocation_id=execution_id,
                    session_key=session_key,
                    reuse_session=True,
                    tool_servers=resolved_tools,
                )
            else:
                result = self._client.invoke(
                    kind=agent_id,
                    workspace=self._scope.workspace_path,
                    system_prompt=system_prompt,
                    user_prompt=routed,
                    round_label=label,
                    env=environment,
                    invocation_id=execution_id,
                    session_key=session_key,
                    reuse_session=True,
                    tool_servers=resolved_tools,
                    response_cls=response,
                )
        except BaseException as exc:
            error = exc
            raise
        else:
            return result
        finally:
            self._lifecycle(
                AgentExecutionFinished(
                    agent_id=agent_id,
                    label=label,
                    execution_id=execution_id,
                    status=_status(error),
                    result=result,
                    error=(f"{type(error).__name__}: {error}" if error is not None else None),
                )
            )

    @staticmethod
    def _initial_result(
        key: AgentSessionKey, outcome: InvocationOutcome, response: type[ResponseT] | None
    ) -> str | ResponseT:
        if isinstance(outcome, InvalidResponse):
            raise AgentOutputSchemaError(outcome.detail)
        if not isinstance(outcome, Completed):
            detail = (
                outcome.detail
                if isinstance(outcome, Unknown)
                else "initial dispatch has no acknowledgement"
            )
            raise SessionResumeError(str(key), detail)
        return (
            outcome.result.text
            if response is None
            else parse_typed_response(outcome.result.text, response)
        )

    def _session_spec(
        self, key: AgentSessionKey, tool_servers: tuple[ToolServerDescriptor, ...] | None
    ) -> AgentSessionSpec:
        if key not in self._session_specs:
            agent_id = self._configuration.agent_id
            environment = (
                {} if self._environment.use_docker else dict(self._scope.environment_variables())
            )
            self._session_specs[key] = AgentSessionSpec(
                role=agent_id,
                provider=self._client.provider or self._configuration.spec.provider,
                workspace=self._scope.workspace_path,
                policy=AgentExecutionPolicy(
                    project_paths=self._environment.project_path_policy,
                    host_resources=(
                        *self._environment.host_resources,
                        *self._configuration.resources,
                    ),
                    require_enforcement=not self._environment.use_docker,
                    containerized=self._environment.use_docker,
                ),
                model=self._client.model_for_kind(agent_id),
                mcp_servers=tuple(
                    MCPServerSpec(item.name, item.command, item.args, item.env, item.runtime_env)
                    for item in tool_servers or ()
                ),
                skills=self._environment.skill_source_dirs,
                environment=tuple(sorted(environment.items())),
                reasoning_effort=self._configuration.spec.role_reasoning_efforts.get(
                    agent_id, self._configuration.spec.reasoning_effort
                ),
            )
        return self._session_specs[key]

    def _turn_timeout(self) -> timedelta | None:
        seconds = self._configuration.spec.cli_timeout
        return timedelta(seconds=seconds) if seconds is not None else None

    def _transport(self, key: AgentSessionKey) -> ClientAgentSessions:
        if self._sessions is None:
            if self._scope.invocation_store is None:
                raise AgentExecutionClosedError(self._configuration.agent_id)
            if not isinstance(self._client, AgentTurnExecutor):
                detail = "durable session client must implement AgentTurnExecutor"
                raise SessionConfigurationError.because(detail)
            self._sessions = ClientAgentSessions(self._client, self._scope.invocation_store(key))
        return self._sessions

    def checkpoint(self, key: AgentSessionKey) -> AgentSessionCheckpoint:
        return self._executor.submit(lambda: self._transport(key).checkpoint(key)).result()

    def release_interrupted(self, key: AgentSessionKey, invocation_id: str) -> None:
        self._executor.submit(
            lambda: self._transport(key).release_interrupted(key, invocation_id)
        ).result()

    def inspect(self, key: AgentSessionKey, invocation_id: str) -> InvocationOutcome:
        # Once bound on the owning thread, ledger inspection uses only the
        # transport's lock and store. It must not queue behind a provider turn.
        if self._sessions is not None:
            return self._sessions.inspect(key, invocation_id)
        return self._executor.submit(
            lambda: self._transport(key).inspect(key, invocation_id)
        ).result()

    async def resume(
        self,
        key: AgentSessionKey,
        message: RenderedPrompt,
        invocation_id: str,
        configuration: AgentResumeConfiguration,
    ) -> InvocationOutcome:
        operation = asyncio.get_running_loop().run_in_executor(
            self._executor,
            partial(
                self._resume_sync,
                key,
                message,
                invocation_id,
                configuration=configuration,
            ),
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            self._client.cancel()
            await _wait_until_done(operation)
            raise

    def _resume_sync(
        self,
        key: AgentSessionKey,
        message: RenderedPrompt,
        invocation_id: str,
        configuration: AgentResumeConfiguration,
    ) -> InvocationOutcome:
        transport = self._transport(key)
        previous = transport.inspect(key, invocation_id)
        checkpoint = previous.checkpoint or transport.checkpoint(key)
        turn = AgentTurnRequest(
            message="",
            instructions=configuration.system_prompt,
            output_schema=configuration.response,
            timeout=self._turn_timeout(),
            label="evaluation-resume",
        )
        transport.bind(
            key,
            self._session_spec(key, configuration.tool_servers),
            replace(turn, expected_provider_session_id=checkpoint.provider_session_id),
        )
        if isinstance(previous, Completed) or previous.checkpoint is not None:
            # resume still validates the recorded digest before returning its
            # acknowledgement. A replay does not emit a second invocation.
            return transport.resume(key, message, invocation_id)
        self._client.set_log_file(self._scope.current_log_file())
        self._control.raise_if_stopped()
        self._control.wait_while_paused()
        agent_id = self._configuration.agent_id
        self._lifecycle(
            AgentExecutionStarted(
                agent_id=agent_id,
                label="evaluation-resume",
                execution_id=invocation_id,
                system_prompt=configuration.system_prompt,
                user_prompt=message,
                driver=self.driver_name,
                provider=self.provider,
                model=self.model,
            )
        )
        result: BaseModel | str | None = None
        status = AgentExecutionStatus.INTERRUPTED
        detail: str | None = None
        try:
            outcome = transport.resume(key, message, invocation_id)
            if isinstance(outcome, Completed):
                status = AgentExecutionStatus.COMPLETED
                result = outcome.result.text
                if configuration.response is not None:
                    # Keep malformed output observable; the caller owns reply
                    # validation and the transition for a malformed response.
                    with suppress(ValidationError):
                        result = configuration.response.model_validate_json(outcome.result.text)
            elif isinstance(outcome, (Unknown, InvalidResponse)):
                detail = outcome.detail
        except BaseException as error:
            status = _status(error)
            detail = f"{type(error).__name__}: {error}"
            raise
        else:
            return outcome
        finally:
            self._lifecycle(
                AgentExecutionFinished(
                    agent_id=agent_id,
                    label="evaluation-resume",
                    execution_id=invocation_id,
                    status=status,
                    result=result,
                    error=detail,
                )
            )

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    def cancel(self) -> None:
        """Ask the client to stop its active provider turn."""
        self._client.cancel()

    async def _close_once(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
        finally:
            await asyncio.to_thread(self._executor.shutdown, wait=True)

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._resources.close()


__all__ = [
    "AgentExecutionConfiguration",
    "AgentExecutionEnvironment",
    "AgentExecutionFinished",
    "AgentExecutionLifecycleEvent",
    "AgentExecutionLifecycleSink",
    "AgentExecutionScope",
    "AgentExecutionStarted",
    "AgentExecutionStatus",
    "AgentMessageRouter",
    "AgentResumeConfiguration",
    "RuntimeAgentExecution",
    "ScopedAgentEnvironment",
    "SharedAgentEnvironmentConflictError",
    "open_agent_execution_environment",
]
