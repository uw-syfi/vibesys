"""Local host for the public custom-orchestration agent capabilities."""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from vibesys.context import _execution_status, create_run_context
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
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
from vibesys.orchestration._common import resolved_run_id
from vibesys.profilers import ProfilerKind
from vibesys.render.sink import output_sink
from vibesys.run.run_control import splice_steering
from vs_agent.api import AgentExecutionPolicy, AgentSessionKey, SessionScope, build_agent_client

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.api.contracts import AgentEnvironment
    from vibesys.api.run_request import RunRequestLike
    from vibesys.context import _RunContext
    from vibesys.run.integration import LocalRunIntegration
    from vibesys.runtime import AgentDefinition
    from vs_agent.api import AgentClientProtocol, MCPServerSpec

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


class _BuiltinRuntimeError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("built-in loops use the legacy integration adapter")


class _LocalAgentHandle:
    def __init__(
        self,
        definition: AgentDefinition,
        context: _RunContext,
        client: AgentClientProtocol,
        resources: ExitStack,
    ) -> None:
        self._definition = definition
        self._context = context
        self._client = client
        self._resources = resources
        self._closed = False

    def turn(self, message: str, *, system_prompt: str = "", label: str = "") -> str:
        """Run one text turn with run control and attributed lifecycle events."""
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

        return self._run_turn(message, system_prompt=system_prompt, label=label, invoke=invoke)

    def turn_structured(  # noqa: PLR0913
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

        return self._run_turn(message, system_prompt=system_prompt, label=label, invoke=invoke)

    def _agent_env(self) -> dict[str, str]:
        context = self._context
        return {} if context.run_environment_view.cli_sandboxed else context.device.gpu_env()

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

    def close(self) -> None:
        """Close the client before its sandbox, including after failed turns."""
        if self._closed:
            return
        self._closed = True
        self._resources.close()


class _LocalVibeSysRuntime:
    def __init__(
        self,
        request: RunRequestLike,
        integration: LocalRunIntegration,
        *,
        open_agent_environment: Callable[..., AgentEnvironment] | None,
    ) -> None:
        self._request = request
        self._integration = integration
        self._open_agent_environment = open_agent_environment
        self._context: _RunContext | None = None
        self._agents: dict[str, _LocalAgentHandle] = {}
        self._closed = False

    def _validate_capabilities(self) -> None:
        request = self._request
        if request.resume is not None:
            raise ConfigurationError(
                ConfigurationDiagnostic(
                    code="custom_orchestration_resume_unsupported",
                    stage="resume_resolution",
                    message="custom orchestration resume needs a policy-owned checkpoint contract",
                )
            )
        if request.profiler_kind not in {
            ProfilerKind.AUTO,
            ProfilerKind.NONE,
        }:
            raise ConfigurationError(
                ConfigurationDiagnostic(
                    code="custom_orchestration_profiler_unsupported",
                    stage="agent_capability_validation",
                    message="custom orchestration runtime does not yet provide a profiler capability",
                )
            )
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
        """Provision and return the run workspace when first needed."""
        return self._ensure_context().workspace

    @property
    def legacy_integration(self) -> LocalRunIntegration:
        """Bridge only the built-in adapters to their existing call contract."""
        return self._integration

    def prepare(self) -> None:
        """Persist runs with an explicit orchestration descriptor."""
        if self._request.orchestration is None:
            return
        self._validate_capabilities()
        self._ensure_context()

    def spawn_agent(self, definition: AgentDefinition) -> _LocalAgentHandle:
        """Open one sandbox and one agent client, with requested grants."""
        if self._closed:
            raise _RuntimeClosedError
        if not definition.id or definition.id in self._agents:
            raise _AgentRegistrationError(definition.id)
        if definition.spec.execution != AgentExecutionPolicy():
            raise _UnsupportedAgentExecutionPolicyError
        if self._open_agent_environment is None:
            raise _MissingAgentHostError
        context = self._ensure_context()
        with ExitStack() as resources:
            opened = self._open_agent_environment(
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
            handle = _LocalAgentHandle(definition, context, client, resources.pop_all())
        self._agents[definition.id] = handle
        return handle

    def _ensure_context(self) -> _RunContext:
        if self._closed:
            raise _RuntimeClosedError
        if self._context is not None:
            return self._context
        request = self._request
        descriptor = request.orchestration
        if descriptor is None:
            raise _BuiltinRuntimeError
        bundle = request.input_bundle
        self._context = create_run_context(
            config=request.config,
            exp_name=resolved_run_id(request),
            runs_dir=request.runs_dir,
            input_path=str(bundle.root),
            task_name=bundle.task_name,
            task_root=bundle.task_root,
            accuracy_command=bundle.accuracy_command_display,
            benchmark_command=bundle.benchmark_command_display,
            workspace_sources=bundle.workspace_sources,
            evaluator_path=bundle.evaluator_path,
            evaluator_package_root=bundle.evaluator_package_root,
            objective=request.objective or bundle.objective,
            orchestration_descriptor=lambda _profiler: descriptor,
            profiler_kind=ProfilerKind.NONE,
            profiler_domain=bundle.domain,
            skills_dirs=request.skills_dirs,
            run_environment=request.run_environment,
            agent_backend=request.agent_backend,
            cli_provider=request.cli_provider,
            backend=request.backend,
            remote_repo=request.remote_repo,
            repo_visibility=request.repo_visibility,
            integration=self._integration,
            build_default_agent_client=False,
        )
        return self._context

    def close(self) -> None:
        """Release agents in reverse spawn order, then the run context."""
        if self._closed:
            return
        self._closed = True
        resources = ExitStack()
        if self._context is not None:
            resources.callback(self._context.close)
        for agent in self._agents.values():
            resources.callback(agent.close)
        resources.close()

    def __enter__(self) -> _LocalVibeSysRuntime:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            self.close()
        except BaseException as cleanup_error:
            if not isinstance(exc, BaseException):
                raise
            exc.add_note(f"runtime cleanup also failed: {cleanup_error}")
