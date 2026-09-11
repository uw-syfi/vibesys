"""AgentShim implementation of the stateful agent-driver contract.

VibeSys consumes the ``agentshim`` library only through its public API. The
library owns provider knowledge (argv, stream parsing, MCP config files,
schema dialects, resume flags); this module owns VibeSys policy: host
confinement, structured-output fallback, conversation budgets, and the
translation between library events and :mod:`vibesys.agents.contracts`.
"""

from __future__ import annotations

import json
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
from vibesys.agents.provider_policy import SHIPPED_PROVIDERS, is_codex
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

_PYTHON_MCP_COMMANDS = frozenset({"python", "python3"})
_SCHEMA_DIR = Path(".cache/vibesys/response-schemas")
_DIAGNOSTIC_PAYLOAD: Mapping[str, object] = {"channel": "diagnostic"}

_HOST_BINARY_CHECK_TIMEOUT_S = 15.0
"""agentshim's own default: a host binary answers ``--help`` immediately."""

_CONTAINER_BINARY_CHECK_TIMEOUT_S = 60.0
"""How long a container's ``<binary> --help`` may take before it counts as dead.

The check crosses a ``docker exec``, so it waits on the daemon as well as the
CLI. A daemon busy starting or stopping other containers regularly takes tens
of seconds to attach, and a false negative here ends the run (see
``docs/contributing/agent-drivers.md``), so the container budget is four times
the host one.
"""

_MAX_CODEX_SESSION_TURNS = 2
_MAX_CODEX_SESSION_INPUT_TOKENS = 10_000_000
_MAX_CODEX_SESSION_DURATION_MS = 600_000


def supported_providers() -> list[str]:
    """Return the sorted provider names the AgentShim driver can run."""
    return sorted(SHIPPED_PROVIDERS)


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


def _translate(  # one arm per event type
    event: agentshim.AgentEvent,
    *,
    structured: bool,
) -> AgentEvent | None:
    """Translate one library event, or return ``None`` to drop it.

    *structured* says the turn asked for a response schema. The assistant text
    of such a turn is the raw schema payload, which reaches the caller through
    :attr:`AgentTurnResult.text` and is rendered from the parsed model. Putting
    it on the assistant channel as well would stream unformatted JSON and then
    repeat it, so a structured turn's text goes to the diagnostic channel.
    """
    if isinstance(event, agentshim.AssistantText):
        return (
            _diagnostic(event.text)
            if structured
            else AgentEvent(kind=AgentEventKind.TEXT, text=event.text)
        )
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
        #: Whether the turn in flight asked for a response schema.
        self.structured = False

    def on_event(self, event: agentshim.AgentEvent) -> None:
        """Translate and forward one library event, if anyone is listening."""
        observer = self.observer
        if observer is None:
            return
        translated = _translate(event, structured=self.structured)
        if translated is not None:
            observer.on_event(translated)


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
        in_container: bool,
        log: Callable[[str], None],
    ) -> None:
        """Bind one library session to the VibeSys policy that drives it."""
        self._session = session
        self._spec = spec
        self._profile = profile
        self._timeout = timeout
        self._event_handler = event_handler
        self._turn_env = dict(turn_env) if turn_env else None
        self._in_container = in_container
        self._log = log
        self._mcp_servers = tuple(
            _as_mcp_server(server, in_container=self._in_container) for server in spec.mcp_servers
        )
        self._turn_count = 0
        # Set when the provider conversation was dropped and restarted while
        # serving the current turn, so the turn's result can report it.
        self._restarted = False
        self._closed = False

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Add one turn to the conversation and return its raw result."""
        if self._closed:
            raise RuntimeError("agent session is closed")  # noqa: TRY003  # tracked: #288

        self._event_handler.observer = observer
        self._event_handler.structured = request.output_schema is not None
        self._restarted = False
        try:
            result = self._turn_with_restart(self._build_request(request))
            self._turn_count += 1
        finally:
            self._event_handler.observer = None
            self._event_handler.structured = False

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

        A resumed turn that fails some other way drops the conversation too,
        without a retry: see :meth:`_drop_conversation_after_failed_resume`.
        """
        resumed = self._session.session_id is not None
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
        except agentshim.CliExitError:
            self._drop_conversation_after_failed_resume(resumed=resumed)
            raise

    def _drop_conversation_after_failed_resume(self, *, resumed: bool) -> None:
        """Forget the conversation a failed resumed turn was continuing.

        A raise carries no ``AgentTurnResult``, so this turn cannot report
        ``RESET_REQUIRED``; forgetting is the only way the session can say the
        conversation is not to be offered again. It matters because a provider
        whose CLI gives a resume failure no distinguishing message raises a
        plain ``CliExitError`` instead of ``SessionResumeError``, and retrying
        the same dead conversation forever is worse than losing it: the next
        turn starts fresh and the run continues.

        The cost is that a genuine agent failure on a resumed turn also drops
        the conversation. That is the deliberate trade: an unusable
        conversation wedges every later turn, while a dropped one costs the
        history of one round.
        """
        if not (resumed and self._profile.supports_resume):
            return
        self._log(
            f"the resumed {self._profile.name} turn failed; dropping the conversation "
            "so the next turn starts fresh."
        )
        self._session.forget()
        self._turn_count = 0

    def _turn(self, request: agentshim.TurnRequest) -> agentshim.TurnResult:
        """Run one turn, reporting a timeout the way every VibeSys caller expects."""
        try:
            return self._session.turn(request)
        except agentshim.CliTimeoutError as exc:
            # Loops fail closed on ``subprocess.TimeoutExpired`` and read its
            # ``timeout``; the library raises its own type, so translate here
            # rather than teaching every catch site a second exception.
            #
            # Only the provider name goes in ``cmd``. ``str(TimeoutExpired)``
            # renders the whole command, and the argv the library timed out on
            # is the transformed one: in container mode that is a
            # ``docker exec -e KEY=VALUE ...`` line carrying every forwarded
            # environment value, which callers log verbatim.
            raise subprocess.TimeoutExpired(
                cmd=[self._profile.binary], timeout=exc.timeout
            ) from exc

    def _renew_codex_thread_if_needed(self, result: agentshim.TurnResult) -> bool:
        """Retire an over-budget Codex thread, reporting whether it was dropped.

        Evaluated after a turn rather than before one, so the decision reads the
        usage of the turn that just finished and the caller learns about the
        restart from that turn's result instead of discovering it on the next.
        """
        if not is_codex(self._profile.name):
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


def _without_stale_pwd(env: Mapping[str, str]) -> dict[str, str]:
    """Drop ``PWD`` so the CLI trusts its real working directory.

    A subprocess cwd does not rewrite ``$PWD``, and bun-based CLIs (opencode)
    trust ``$PWD`` over the real cwd for workspace config discovery, so a
    stale value makes them miss ``<workspace>/opencode.json``. Applied to the
    session environment on the host and to the overlay forwarded into a
    container, so the cwd wins in both.
    """
    return {key: value for key, value in env.items() if key != "PWD"}


class AgentShimDriver:
    """Create AgentShim sessions and translate VibeSys execution policy."""

    def __init__(  # noqa: PLR0913  # tracked: #288
        self,
        *,
        provider: str,
        timeout: int | None = None,
        docker_sandboxes: dict[str, Any] | None = None,
        log: Callable[[str], None] | None = None,
        executor_factory: ExecutorFactory | None = None,
        check_timeout: float | None = None,
    ) -> None:
        """Configure one provider; ``executor_factory`` replaces host execution.

        ``check_timeout`` bounds the one-off ``<binary> --help`` health check
        each session runs before its first turn. It defaults to the execution
        mode's budget: a container check crosses a ``docker exec`` and is given
        four times as long as a host one.
        """
        if provider not in SHIPPED_PROVIDERS:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"unknown AgentShim provider {provider!r}; expected one of: {supported_providers()}"
            )
        self._provider = provider
        self._timeout = timeout
        self._docker_sandboxes = docker_sandboxes
        self._log = log or _ignore_log
        self._executor_factory: ExecutorFactory = executor_factory or build_host_executor
        self._check_timeout = (
            check_timeout
            if check_timeout is not None
            else (
                _CONTAINER_BINARY_CHECK_TIMEOUT_S
                if docker_sandboxes is not None
                else _HOST_BINARY_CHECK_TIMEOUT_S
            )
        )
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
        turn_env: Mapping[str, str] | None = None
        executor: agentshim.CommandExecutor
        if in_container:
            executor = self._container_executor(spec, forward_env=tuple(overlay))
            # The container's own environment is built by the image and the
            # exec invocation; the session overlay is forwarded per turn so it
            # reaches the CLI inside the container instead of the docker client.
            env = _without_stale_pwd(agentshim.interactive_env())
            turn_env = _without_stale_pwd(overlay)
        else:
            env = _without_stale_pwd({**agentshim.interactive_env(), **overlay})
            executor = self._executor_factory(self._host_sandbox(spec, env))

        agent = agentshim.CliAgent(
            provider,
            model=spec.model,
            executor=executor,
            env=env,
            event_handler=event_handler,
            check_timeout=self._check_timeout,
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
            in_container=in_container,
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
        if is_codex(self._provider):
            # A resumed containerized `codex exec --json` regularly finishes
            # its work and then never exits; the watchdog recovers the answer
            # from the rollout file and stops the process. Provider-behaviour
            # compensation, so it wraps the transport rather than replacing it.
            executor = CodexRolloutWatchdogExecutor(executor, resolve, log=self._log)
        return executor

    def close(self) -> None:
        """Close every session created by this driver, idempotently."""
        if self._closed:
            return
        self._closed = True
        for session in self._sessions:
            session.close()
        self._sessions.clear()
