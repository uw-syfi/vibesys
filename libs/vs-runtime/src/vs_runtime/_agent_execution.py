"""Thread-affine ownership of one explicit agent execution."""

from __future__ import annotations

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict

from vs_agent.api import build_agent_client
from vs_runtime.contracts import StructuredResponseError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import (
        AgentCapabilities,
        AgentClientProtocol,
        AgentEventSink,
        AgentSessionKey,
        AgentSpec,
        SessionStore,
        SkillSelection,
        ToolServerDescriptor,
    )
    from vs_runtime._run_control import RunControlChannel
    from vs_sandbox.api import HostResource, ProjectPathPolicy, Sandbox

ResponseT = TypeVar("ResponseT", bound=BaseModel)


def _structured_failure(agent_id: str, response: type[ResponseT]) -> ResponseT:
    raise StructuredResponseError(agent_id, response)


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


class AgentExecutionStatus(StrEnum):
    """Driver-neutral outcome of one agent execution."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class AgentExecutionStarted(BaseModel):
    """Semantic start of one provider invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    label: str
    execution_id: str
    system_prompt: str
    user_prompt: str
    driver: str | None = None
    provider: str | None = None
    model: str | None = None


class AgentExecutionFinished(BaseModel):
    """Semantic terminal observation for one provider invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    label: str
    execution_id: str
    status: AgentExecutionStatus
    result: Any = None
    error: str | None = None


type AgentExecutionLifecycleEvent = AgentExecutionStarted | AgentExecutionFinished


class AgentExecutionLifecycleSink(Protocol):
    """Record one semantic execution lifecycle observation synchronously."""

    def __call__(self, event: AgentExecutionLifecycleEvent) -> object: ...


type AgentMessageRouter = Callable[[str, tuple[str, ...]], str]
type AgentClientFactory = Callable[..., AgentClientProtocol]


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
        except BaseException:  # noqa: BLE001  # lint-waiver: execution construction cleanup must inspect the completed worker outcome.
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

    def __init__(  # noqa: PLR0913  # lint-waiver: the execution owns independently configured lower-layer resources and semantic sinks.
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

    @classmethod
    async def open(  # noqa: PLR0913  # lint-waiver: composition supplies independent lower-layer effects once; callers use the resulting deep execution object.
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
    def _open_sync(  # noqa: PLR0913  # lint-waiver: mirrors open's one-time composition inputs on the worker thread.
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
            client = client_factory(
                spec=configuration.spec,
                session_store=session_store,
                backends=backends,
                skill_source_dirs=list(environment.skill_source_dirs),
                skill_selection=environment.skill_selection,
                run_log_file=scope.current_log_file(),
                use_docker=environment.use_docker,
                log_dir=scope.log_directory,
                host_resources=(*environment.host_resources, *configuration.resources),
                project_path_policy=environment.project_path_policy,
                require_host_sandbox=not environment.use_docker,
                events=agent_events,
            )
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

    @property
    def model(self) -> str | None:
        return self._client.model_for_kind(self._configuration.agent_id)

    @property
    def reasoning_effort(self) -> str | None:
        return self._configuration.reasoning_effort

    async def execute(  # noqa: PLR0913  # lint-waiver: fixed provider-turn inputs belong to the lower client contract and bundling them would expose an extra message object.
        self,
        message: str,
        *,
        system_prompt: str,
        response: type[ResponseT] | None,
        label: str,
        session_key: AgentSessionKey,
        tool_servers: tuple[ToolServerDescriptor, ...] | None,
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
            ),
        )
        try:
            return await asyncio.shield(turn)
        except asyncio.CancelledError as cancelled:
            await _wait_until_done(turn)
            if error := turn.exception():
                cancelled.add_note(f"canceled agent turn also failed: {error}")
            raise

    def _run_turn(  # noqa: PLR0913  # lint-waiver: worker-side execution receives the same fixed provider-turn inputs as execute.
        self,
        message: str,
        *,
        system_prompt: str,
        response: type[ResponseT] | None,
        label: str,
        session_key: AgentSessionKey,
        tool_servers: tuple[ToolServerDescriptor, ...] | None,
    ) -> str | ResponseT:
        if self._closed:
            raise AgentExecutionClosedError(self._configuration.agent_id)
        self._client.set_log_file(self._scope.current_log_file())
        self._control.raise_if_stopped()
        self._control.wait_while_paused()
        steering = tuple(self._control.take_pending_steer())
        routed = self._route_message(message, steering)
        execution_id = uuid.uuid4().hex
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
            if response is None:
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
                    fallback_factory=partial(_structured_failure, agent_id, response),
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

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

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
    "RuntimeAgentExecution",
]
