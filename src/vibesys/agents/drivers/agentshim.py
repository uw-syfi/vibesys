"""AgentShim implementation of the stateful agent-driver contract.

VibeSys consumes the ``agentshim`` library only through its public API. The
library owns provider knowledge (argv, stream parsing, MCP config files,
schema dialects, resume flags); this module owns VibeSys policy: host
confinement, structured-output fallback, conversation budgets, and the
translation between library events and :mod:`vibesys.agents.contracts`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from weakref import WeakSet

import agentshim

from vibesys.agents.cli_common import build_schema_hint
from vibesys.agents.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentObserver,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
    AgentTurnResult,
    AgentUsage,
    MCPServerSpec,
    SessionDisposition,
)
from vibesys.agents.host_resource_declarations import declare_agent_host_resources
from vibesys.run.events import CommandResultPayload
from vs_sandbox import build_host_sandbox

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_sandbox import WorkspaceSandbox

AGENTSHIM_CAPABILITIES = AgentCapabilities(
    mcp_servers=True,
    nested_read_only_paths=True,
    hidden_paths=True,
    timeouts=True,
    session_reuse=True,
    provider_session_resume=True,
)
"""Capabilities invariant across AgentShim host and container execution.

``provider_session_resume`` is narrowed per provider from
:attr:`agentshim.ProviderProfile.supports_resume` when the driver is built.
"""

_SHIPPED_PROVIDERS: tuple[str, ...] = ("claude", "codex", "gemini", "opencode")
"""The CLI providers VibeSys ships.

The library also carries ``copilot``, which VibeSys has neither host-resource
declarations nor a container install recipe for, so it is not offered here.
"""

_PYTHON_MCP_COMMANDS = frozenset({"python", "python3"})
_SCHEMA_DIR = Path(".cache/vibesys/response-schemas")
_DIAGNOSTIC_PAYLOAD: Mapping[str, object] = {"channel": "diagnostic"}

_MAX_CODEX_SESSION_TURNS = 2
_MAX_CODEX_SESSION_INPUT_TOKENS = 10_000_000
_MAX_CODEX_SESSION_DURATION_MS = 600_000


def supported_providers() -> list[str]:
    """Return the sorted provider names the AgentShim driver can run."""
    return sorted(_SHIPPED_PROVIDERS)


def _ignore_log(_message: str) -> None:
    """Discard a driver diagnostic when no log sink was configured."""


class ExecutorFactory(Protocol):
    """Builds the command executor a host session runs its CLI through."""

    def __call__(self, sandbox: WorkspaceSandbox | None, /) -> agentshim.CommandExecutor:
        """Return an executor confined by *sandbox*, or unconfined when it is ``None``."""
        ...


def confine_to_sandbox(
    executor: agentshim.CommandExecutor,
    sandbox: WorkspaceSandbox,
) -> agentshim.CommandExecutor:
    """Return *executor* with every workspace command wrapped by *sandbox*.

    A command without a working directory is left alone: that is the binary
    health check and, in container mode, the agent itself, both of which run
    outside the confined workspace (bwrap re-establishes the working directory
    inside the namespace via ``--chdir``, so a wrapped command needs one).
    """

    def transform(request: agentshim.CommandRequest) -> agentshim.CommandRequest:
        if request.cwd is None:
            return request
        return replace(request, argv=sandbox.wrap(list(request.argv)))

    return agentshim.TransformingExecutor(executor, transform)


def build_host_executor(sandbox: WorkspaceSandbox | None) -> agentshim.CommandExecutor:
    """Default :class:`ExecutorFactory`: run on this host, confined when possible."""
    host = agentshim.HostCommandExecutor()
    return host if sandbox is None else confine_to_sandbox(host, sandbox)


def _resolve_binary_path(binary: str, env: Mapping[str, str]) -> str | None:
    """Locate *binary* for the host resource declaration, or ``None`` if absent.

    The declaration is built before the agent exists because the sandbox it
    feeds is what the agent's executor is built from. A missing binary is not
    reported here: constructing the agent raises the library's own
    ``CliNotFoundError`` with the provider's name attached.
    """
    try:
        return agentshim.HostCommandExecutor().find_binary(binary, env)
    except agentshim.CliNotFoundError:
        return None


def _as_mcp_server(spec: MCPServerSpec, *, in_container: bool) -> agentshim.StdioMcpServer:
    """Translate one VibeSys MCP spec into the library's stdio spec.

    A host agent inherits a login shell's PATH, where a bare ``python`` may be
    an interpreter without the MCP dependencies, so host runs are pinned to
    the interpreter running VibeSys. A container image resolves its own.
    """
    command = spec.command
    if not in_container and command in _PYTHON_MCP_COMMANDS:
        command = sys.executable
    return agentshim.StdioMcpServer(
        name=spec.name,
        command=command,
        args=tuple(spec.args),
        env=dict(spec.env),
    )


def _usage_from(
    usage: agentshim.ProviderUsage,
    *,
    cost_usd: float | None = None,
    duration_ms: int | None = None,
) -> AgentUsage:
    """Map library token accounting onto the neutral usage contract.

    ``input_tokens`` includes cached tokens on every provider: the library
    folds Anthropic's disjoint cache counts into the input total so the same
    field means the same thing everywhere.
    """
    tokens = usage.tokens
    return AgentUsage(
        input_tokens=tokens.input_tokens,
        cache_creation_input_tokens=tokens.cache_write_input_tokens,
        cache_read_input_tokens=tokens.cached_input_tokens,
        output_tokens=tokens.output_tokens,
        total_cost_usd=cost_usd if cost_usd is not None else usage.total_cost_usd,
        duration_ms=duration_ms,
    )


def _diagnostic(text: str) -> AgentEvent:
    """Report provider plumbing on the diagnostic channel, not as reasoning."""
    return AgentEvent(kind=AgentEventKind.THINKING, text=text, payload=_DIAGNOSTIC_PAYLOAD)


def _translate(event: agentshim.AgentEvent) -> AgentEvent | None:  # one arm per event type
    """Translate one library event, or return ``None`` to drop it."""
    if isinstance(event, agentshim.AssistantText):
        return AgentEvent(kind=AgentEventKind.TEXT, text=event.text)
    if isinstance(event, agentshim.Reasoning):
        return AgentEvent(kind=AgentEventKind.THINKING, text=event.text)
    if isinstance(event, agentshim.ToolCall):
        return AgentEvent(
            kind=AgentEventKind.TOOL_CALL,
            payload={"tool": event.tool, "args": event.args if event.args is not None else {}},
        )
    if isinstance(event, agentshim.ToolResult):
        return AgentEvent(
            kind=AgentEventKind.TOOL_RESULT,
            text=event.stdout or event.stderr,
            payload={
                "tool": event.tool,
                "stdout": event.stdout,
                "stderr": event.stderr,
                "exit_code": event.exit_code,
                "duration": event.duration_s,
                "result_payload": CommandResultPayload(
                    stdout=event.stdout,
                    stderr=event.stderr,
                    exit_code=event.exit_code,
                    duration=event.duration_s,
                ),
            },
        )
    if isinstance(event, agentshim.UsageReport):
        return AgentEvent(
            kind=AgentEventKind.USAGE,
            usage=_usage_from(event.usage, cost_usd=event.cost_usd),
        )
    return _translate_plumbing(event)


def _translate_plumbing(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Translate the events that describe the provider, not the agent."""
    if isinstance(event, agentshim.SessionStarted):
        return _diagnostic(f"[session {event.session_id} started]")
    if isinstance(event, agentshim.Lifecycle):
        return _diagnostic(f"[{event.kind}] {event.detail}" if event.detail else f"[{event.kind}]")
    if isinstance(event, agentshim.Stderr):
        return _diagnostic(f"[stderr] {event.text}")
    if isinstance(event, agentshim.RawOutput):
        return _diagnostic(event.text)
    if isinstance(event, agentshim.ProviderError):
        return _diagnostic(f"[error] {event.message}")
    # RunStarted and RunFinished describe the subprocess, not the agent.
    return None


class _AgentShimEventHandler:
    """Route the library's typed events to the turn's observer."""

    def __init__(self) -> None:
        self.observer: AgentObserver | None = None

    def on_event(self, event: agentshim.AgentEvent) -> None:
        """Translate and forward one library event, if anyone is listening."""
        observer = self.observer
        if observer is None:
            return
        translated = _translate(event)
        if translated is not None:
            observer.on_event(translated)


class _ContainerCleanup(Protocol):
    """The container-side cleanup a container session owns.

    The driver binds this to
    :func:`vibesys.agents.docker_executor.repair_workspace_ownership` for the
    session's container; the session only knows there is one to run.
    """

    def __call__(self) -> None:
        """Return bind-mounted workspace files to the host user."""
        ...


class AgentShimSession:
    """One configured AgentShim conversation."""

    def __init__(  # noqa: PLR0913  # tracked: #288
        self,
        *,
        session: agentshim.AgentSession,
        spec: AgentSessionSpec,
        profile: agentshim.ProviderProfile,
        timeout: int | None,
        event_handler: _AgentShimEventHandler,
        turn_env: Mapping[str, str] | None,
        container_cleanup: _ContainerCleanup | None,
        log: Callable[[str], None],
    ) -> None:
        """Bind one library session to the VibeSys policy that drives it."""
        self._session = session
        self._spec = spec
        self._profile = profile
        self._timeout = timeout
        self._event_handler = event_handler
        self._turn_env = dict(turn_env) if turn_env else None
        self._container_cleanup = container_cleanup
        self._log = log
        self._mcp_servers = tuple(
            _as_mcp_server(server, in_container=self._in_container) for server in spec.mcp_servers
        )
        self._turn_count = 0
        # Set when the provider conversation was dropped and restarted while
        # serving the current turn, so the turn's result can report it.
        self._restarted = False
        self._closed = False

    @property
    def _in_container(self) -> bool:
        """Whether this session's CLI runs inside a container.

        The container-side cleanup hook is the one thing only a container
        session owns, so its presence is what the mode is read from.
        """
        return self._container_cleanup is not None

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Add one turn to the conversation and return its raw result."""
        if self._closed:
            raise RuntimeError("agent session is closed")  # noqa: TRY003  # tracked: #288

        self._event_handler.observer = observer
        self._restarted = False
        turn_error: BaseException | None = None
        try:
            result = self._turn_with_restart(self._build_request(request))
            self._turn_count += 1
        except BaseException as exc:
            turn_error = exc
            raise
        finally:
            cleanup_error: Exception | None = None
            try:
                self._repair_workspace_ownership()
            except Exception as exc:  # noqa: BLE001  # tracked: #288
                cleanup_error = exc
                if turn_error is not None:
                    self._log(
                        "workspace ownership repair failed while preserving the original "
                        f"agent error: {exc}"
                    )
            self._event_handler.observer = None
            if turn_error is None and cleanup_error is not None:
                raise cleanup_error

        # Read the conversation ID before the thread-budget check, which may
        # drop it: the caller still deserves to know which conversation ran.
        provider_session_id = result.session_id
        restarted = self._restarted or self._renew_codex_thread_if_needed(result)
        return AgentTurnResult(
            text=_result_text(result),
            usage=_usage_from(
                result.usage,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
            ),
            provider_session_id=provider_session_id,
            disposition=(
                SessionDisposition.RESET_REQUIRED if restarted else SessionDisposition.REUSABLE
            ),
        )

    def cancel(self) -> None:
        """Stop an in-flight turn by terminating the provider process."""
        self._session.cancel()

    def close(self) -> None:
        """Release this logical session, stopping any turn it still owns."""
        self.cancel()
        self._closed = True
        self._event_handler.observer = None

    def resume_provider_session(self, session_id: str) -> bool:
        """Continue ``session_id`` on the next turn, if the session accepts it.

        A session that already named a conversation keeps it: its history is
        newer than any checkpoint the caller holds. Beyond that the library
        decides, adopting the ID only when the provider has a resume flag and
        no turn is in flight. A stale or deleted transcript is handled later,
        by the restart fallback around the turn.
        """
        if self._session.session_id is not None:
            return False
        return self._session.adopt(session_id)

    def _build_request(self, request: AgentTurnRequest) -> agentshim.TurnRequest:
        """Translate one VibeSys turn into the library's request.

        A config-file provider writes its MCP config into the workspace, and
        agentshim derives that directory from the turn's ``cwd``. A container
        turn names none, so it points ``mcp_workspace`` at the host workspace
        that is bind-mounted into the container: the file the library writes on
        the host is the one the CLI reads at ``/workspace``. A host turn already
        runs in the workspace, so it leaves the field unset.
        """
        schema, schema_hint = self._output_schema(request.output_schema)
        timeout = self._timeout
        if request.timeout is not None:
            timeout = max(1, int(request.timeout.total_seconds()))
        return agentshim.TurnRequest(
            prompt=f"{request.instructions}\n\n{request.message}{schema_hint}",
            timeout=timeout,
            output_schema=schema,
            reasoning_effort=(
                self._spec.reasoning_effort if self._profile.supports_reasoning_effort else None
            ),
            env=self._turn_env,
            mcp_servers=self._mcp_servers,
            mcp_workspace=self._spec.workspace if self._in_container else None,
        )

    def _output_schema(
        self,
        response_cls: type[BaseModel] | None,
    ) -> tuple[agentshim.OutputSchema | None, str]:
        """Choose between the provider's native schema and the prompt contract.

        The prompt-level instruction is the portable fallback: it is used when
        the provider has no structured-output flag, and when the response model
        needs JSON Schema constructs the provider's dialect rejects. Falling
        back is logged because the two paths fail differently.
        """
        if response_cls is None:
            return None, ""
        profile = self._profile
        if profile.output_schema is agentshim.OutputSchemaStyle.NONE:
            self._log(
                f"[structured-output] {profile.name} has no native output schema for "
                f"{response_cls.__name__}; using prompt fallback"
            )
            return None, build_schema_hint(response_cls)

        dialect = (
            profile.schema_dialect
            if profile.schema_dialect is not None
            else agentshim.SchemaDialect.STRICT
        )
        schema = response_cls.model_json_schema()
        problems = agentshim.dialect_problems(schema, dialect)
        if problems:
            self._log(
                f"[structured-output] native schema unavailable for "
                f"{response_cls.__name__}; using prompt fallback: {'; '.join(problems)}"
            )
            return None, build_schema_hint(response_cls)

        host_dir = self._spec.workspace / _SCHEMA_DIR
        return (
            agentshim.OutputSchema(
                schema=agentshim.normalize(schema, dialect),
                host_dir=host_dir,
                # The container resolves the schema path against its own
                # workspace mount, which is not the host path it was written to.
                cli_dir=_SCHEMA_DIR.as_posix() if self._in_container else str(host_dir),
            ),
            "",
        )

    def _turn_with_restart(self, request: agentshim.TurnRequest) -> agentshim.TurnResult:
        """Run one turn, retrying once from a fresh conversation if a resume failed.

        Only a resumed turn is retried, and only once: with the conversation
        dropped, the retry takes the fresh-session branch, so a second failure
        is a real agent failure and propagates. The retry loses the earlier
        conversation, which ``self._restarted`` reports to the caller.
        """
        try:
            return self._turn(request)
        except agentshim.SessionResumeError:
            self._log(
                f"{self._profile.name} session is no longer available; "
                "retrying this turn with a fresh conversation."
            )
            self._session.forget()
            self._turn_count = 0
            self._restarted = True
            return self._turn(request)

    def _turn(self, request: agentshim.TurnRequest) -> agentshim.TurnResult:
        """Run one turn, reporting a timeout the way every VibeSys caller expects."""
        try:
            return self._session.turn(request)
        except agentshim.CliTimeoutError as exc:
            # Loops fail closed on ``subprocess.TimeoutExpired`` and read its
            # ``timeout``; the library raises its own type, so translate here
            # rather than teaching every catch site a second exception.
            raise subprocess.TimeoutExpired(cmd=list(exc.argv), timeout=exc.timeout) from exc

    def _renew_codex_thread_if_needed(self, result: agentshim.TurnResult) -> bool:
        """Retire an over-budget Codex thread, reporting whether it was dropped.

        Evaluated after a turn rather than before one, so the decision reads the
        usage of the turn that just finished and the caller learns about the
        restart from that turn's result instead of discovering it on the next.
        """
        if self._profile.name != "codex":
            return False
        reason = (
            f"{_MAX_CODEX_SESSION_TURNS} successful turns"
            if self._turn_count >= _MAX_CODEX_SESSION_TURNS
            else _heavy_codex_turn_reason(result)
        )
        if reason is None:
            return False
        self._log(
            f"renewing Codex thread after {reason}; durable workspace state remains authoritative."
        )
        self._session.forget()
        self._turn_count = 0
        return True

    def _repair_workspace_ownership(self) -> None:
        if self._container_cleanup is None:
            return
        self._container_cleanup()


def _result_text(result: agentshim.TurnResult) -> str:
    """Return the turn's answer, preferring the schema-conformant payload.

    Callers parse a structured turn's text back into their response model, so
    a provider that reported the payload out of band still has to deliver it
    as text.
    """
    if result.structured_output is not None:
        return json.dumps(result.structured_output)
    return result.text


def _heavy_codex_turn_reason(result: agentshim.TurnResult) -> str | None:
    reasons: list[str] = []
    if result.usage.tokens.input_tokens >= _MAX_CODEX_SESSION_INPUT_TOKENS:
        reasons.append(f"{result.usage.tokens.input_tokens} input tokens")
    if result.duration_ms >= _MAX_CODEX_SESSION_DURATION_MS:
        reasons.append(f"{result.duration_ms} ms duration")
    return " and ".join(reasons) or None


class AgentShimDriver:
    """Create AgentShim sessions and translate VibeSys execution policy."""

    def __init__(
        self,
        *,
        provider: str,
        timeout: int | None = None,
        docker_sandboxes: dict[str, Any] | None = None,
        log: Callable[[str], None] | None = None,
        executor_factory: ExecutorFactory | None = None,
    ) -> None:
        """Configure one provider; ``executor_factory`` replaces host execution."""
        if provider not in _SHIPPED_PROVIDERS:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"unknown AgentShim provider {provider!r}; expected one of: {supported_providers()}"
            )
        self._provider = provider
        self._timeout = timeout
        self._docker_sandboxes = docker_sandboxes
        self._log = log or _ignore_log
        self._executor_factory: ExecutorFactory = executor_factory or build_host_executor
        self._sessions: WeakSet[AgentShimSession] = WeakSet()
        self._closed = False

    @property
    def capabilities(self) -> AgentCapabilities:
        """Describe the policy and lifecycle features this driver enforces."""
        return replace(
            AGENTSHIM_CAPABILITIES,
            host_path_grants=self._docker_sandboxes is None,
            container_execution=self._docker_sandboxes is not None,
            provider_session_resume=(
                agentshim.get_provider(self._provider).profile.supports_resume
            ),
        )

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create one configured AgentShim conversation."""
        if self._closed:
            raise RuntimeError("agent driver is closed")  # noqa: TRY003  # tracked: #288
        if spec.provider != self._provider:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"AgentShimDriver for {self._provider!r} cannot create a {spec.provider!r} session"
            )
        in_container = self._docker_sandboxes is not None
        if spec.policy.containerized != in_container:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                "agent session container policy does not match the configured "
                "AgentShim execution mode"
            )

        provider = agentshim.get_provider(spec.provider)
        event_handler = _AgentShimEventHandler()
        overlay = dict(spec.environment)
        container_cleanup: _ContainerCleanup | None = None
        turn_env: Mapping[str, str] | None = None
        executor: agentshim.CommandExecutor
        if in_container:
            executor = self._container_executor(spec, forward_env=tuple(overlay))
            container_cleanup = self._container_cleanup(spec)
            # The container's own environment is built by the image and the
            # exec invocation; the session overlay is forwarded per turn so it
            # reaches the CLI inside the container instead of the docker client.
            env = agentshim.interactive_env()
            turn_env = overlay
        else:
            env = {**agentshim.interactive_env(), **overlay}
            executor = self._executor_factory(self._host_sandbox(spec, env))

        agent = agentshim.CliAgent(
            provider,
            model=spec.model,
            executor=executor,
            env=env,
            event_handler=event_handler,
            log=self._log,
        )
        session = AgentShimSession(
            # A container command names its own working directory, so the
            # session leaves cwd unset there and the executor supplies it.
            session=agent.start_session(
                cwd=None if in_container else str(spec.workspace),
                timeout=self._timeout,
            ),
            spec=spec,
            profile=agent.profile,
            timeout=self._timeout,
            event_handler=event_handler,
            turn_env=turn_env,
            container_cleanup=container_cleanup,
            log=self._log,
        )
        self._sessions.add(session)
        return session

    def _host_sandbox(
        self,
        spec: AgentSessionSpec,
        env: Mapping[str, str],
    ) -> WorkspaceSandbox | None:
        profile = agentshim.get_provider(spec.provider).profile
        resources = declare_agent_host_resources(
            env,
            binary_path=_resolve_binary_path(profile.binary, env),
            provider=spec.provider,
            additional=spec.policy.host_resources,
        )
        return build_host_sandbox(
            spec.workspace,
            env=dict(env),
            resources=resources,
            log=self._log,
            project_path_policy=spec.policy.project_paths,
            require_enforcement=spec.policy.require_enforcement,
        )

    def _container_id_resolver(self, spec: AgentSessionSpec) -> Callable[[], str]:
        """Return the sandbox's current container ID, read at every call.

        Read through the sandbox rather than captured, because a GPU reselect
        replaces the container and nothing should then have to rebuild the
        executor or the cleanup hook.
        """
        assert self._docker_sandboxes is not None  # noqa: S101  # tracked: #288
        sandbox = self._docker_sandboxes.get(spec.role)
        if sandbox is None:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"no AgentShim Docker sandbox configured for role {spec.role!r}"
            )
        return lambda: str(sandbox.container_id)

    def _container_executor(
        self,
        spec: AgentSessionSpec,
        *,
        forward_env: tuple[str, ...] = (),
    ) -> agentshim.CommandExecutor:
        """Build the ``docker exec`` transport this session's turns run through.

        *forward_env* names the session-overlay variables that cross into the
        container. Their values arrive per turn through ``TurnRequest.env``,
        which is why the transport is told the names rather than the values.
        """
        resolve = self._container_id_resolver(spec)
        # Imported here because the Docker executor is only reachable in
        # container mode and pulls in the docker command plumbing with it.
        from vibesys.agents.docker_executor import (  # noqa: PLC0415
            CodexRolloutWatchdogExecutor,
            DockerCommandExecutor,
        )

        executor: agentshim.CommandExecutor = DockerCommandExecutor(
            resolve, forward_env=forward_env
        )
        if self._provider == "codex":
            # A resumed containerized `codex exec --json` regularly finishes
            # its work and then never exits; the watchdog recovers the answer
            # from the rollout file and stops the process. Provider-behaviour
            # compensation, so it wraps the transport rather than replacing it.
            executor = CodexRolloutWatchdogExecutor(executor, resolve, log=self._log)
        return executor

    def _container_cleanup(self, spec: AgentSessionSpec) -> _ContainerCleanup:
        """Bind the module-level ownership repair to this session's container."""
        resolve = self._container_id_resolver(spec)
        from vibesys.agents.docker_executor import repair_workspace_ownership  # noqa: PLC0415

        def repair() -> None:
            repair_workspace_ownership(resolve(), uid=os.getuid(), gid=os.getgid())

        return repair

    def close(self) -> None:
        """Close every session created by this driver, idempotently."""
        if self._closed:
            return
        self._closed = True
        for session in self._sessions:
            session.close()
        self._sessions.clear()
