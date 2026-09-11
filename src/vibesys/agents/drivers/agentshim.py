"""AgentShim implementation of the stateful agent-driver contract.

VibeSys consumes the ``agentshim`` library only through its public API. The
library owns provider knowledge (argv, stream parsing, MCP config files,
schema dialects, resume flags); this module owns VibeSys policy: sandbox
confinement, structured-output fallback, conversation budgets, and the
translation between library events and :mod:`vibesys.agents.contracts`.

One path runs every session, host or container: build (or look up) a
:class:`~vs_sandbox.WorkspaceSandbox`, wrap a plain ``HostCommandExecutor`` to
confine it, and hand the library the sandbox's own environment. Every path the
agent is told about -- the schema directory, an MCP server's command -- goes
through :meth:`~vs_sandbox.WorkspaceSandbox.agent_path`, so nothing here names
a backend.
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
from vibesys.agents.provider_policy import CODEX_PROVIDER, SHIPPED_PROVIDERS, is_codex
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
    """Builds the unconfined command executor :func:`confine_to_sandbox` wraps."""

    def __call__(self) -> agentshim.CommandExecutor:
        """Return a fresh, unconfined executor for one session."""
        ...


class _ConfinableSandbox(Protocol):
    """The shape :func:`confine_to_sandbox` needs, real or a test double alike.

    Both ``vs_sandbox.WorkspaceSandbox`` (a host confinement policy) and
    ``vs_sandbox.DockerSandbox`` satisfy this structurally, and so does any
    lightweight double a test builds for either: nothing here requires the
    concrete class.
    """

    def wrap(self, argv: list[str], cwd: Path | str | None = None, /) -> list[str]:
        """Return *argv* rewritten to run confined to one workspace."""
        ...

    def agent_path(self, path: Path | str, /) -> str:
        """Return the path the confined process sees for *path*."""
        ...

    @property
    def env(self) -> Mapping[str, str]:
        """Return the environment variables the confined process runs with."""
        ...


def confine_to_sandbox(
    executor: agentshim.CommandExecutor,
    sandbox: _ConfinableSandbox,
    *,
    find_binary: Callable[[str, Mapping[str, str]], str] | None = None,
) -> agentshim.CommandExecutor:
    """Return *executor* with every command rewritten through *sandbox*.

    This is the one chokepoint through which the provider CLI launches, for a
    host confinement policy and an already-running Docker sandbox alike:
    *sandbox* supplies the ``wrap`` call, so the caller never branches on which
    kind of sandbox it was given. Every ``wrap`` implementation accepts the
    request's ``cwd``; a host sandbox confines to exactly one workspace
    already and ignores it, while a Docker sandbox serves every turn from one
    container regardless of working directory and maps the argument to ``-w``.

    *find_binary*, when given, replaces the default host-side lookup
    (``shutil.which`` against the request's own environment). A Docker
    sandbox's ``env`` carries the *container's* ``PATH``, which resolves to
    nothing on this host, so its caller passes an override that trusts the
    bare name to the far side of ``docker exec`` instead.
    """

    def transform(request: agentshim.CommandRequest) -> agentshim.CommandRequest:
        return replace(request, argv=sandbox.wrap(list(request.argv), request.cwd))

    return agentshim.TransformingExecutor(executor, transform, find_binary=find_binary)


def build_host_executor(sandbox: WorkspaceSandbox | None) -> agentshim.CommandExecutor:
    """Run on this host, confined to *sandbox* when one is given.

    A thin convenience over :func:`confine_to_sandbox` for callers (notably
    the host-confinement test suite) that want the driver's own default
    executor policy without going through :class:`AgentShimDriver` itself.
    """
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


def _bare_binary_name(name: str, env: Mapping[str, str]) -> str:
    """Trust *name* to resolve on the far side of ``docker exec``.

    A host-side lookup against a container's own ``PATH`` would search
    directories that only exist inside the image; the container's shell
    resolves its own binaries once the wrapped command actually runs there.
    """
    del env
    return name


def _agent_path(sandbox: _ConfinableSandbox | None, path: Path | str) -> str:
    """Map *path* through *sandbox*, or return it unchanged with no sandbox."""
    return sandbox.agent_path(path) if sandbox is not None else str(Path(path))


def _as_mcp_server(
    spec: MCPServerSpec,
    sandbox: _ConfinableSandbox | None,
    *,
    pin_interpreter: bool,
) -> agentshim.StdioMcpServer:
    """Translate one VibeSys MCP spec into the library's stdio spec.

    A host agent inherits a login shell's PATH, where a bare ``python`` may be
    an interpreter without the MCP dependencies, so *pin_interpreter* (true
    only on the host) substitutes the interpreter running VibeSys itself. A
    container image resolves its own, so nothing is substituted there. Every
    absolute path in the command or args -- including that pinned interpreter
    -- is then mapped through ``sandbox.agent_path`` so a file the sandbox
    presents at a different location (a bind-mounted workspace, say) still
    resolves; a relative argument such as a flag or a workspace-relative path
    is left untouched.
    """
    command = (
        sys.executable if pin_interpreter and spec.command in _PYTHON_MCP_COMMANDS else spec.command
    )
    return agentshim.StdioMcpServer(
        name=spec.name,
        command=_agent_path_if_absolute(command, sandbox),
        args=tuple(_agent_path_if_absolute(arg, sandbox) for arg in spec.args),
        env=dict(spec.env),
    )


def _agent_path_if_absolute(value: str, sandbox: _ConfinableSandbox | None) -> str:
    return _agent_path(sandbox, value) if value.startswith("/") else value


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
        sandbox: _ConfinableSandbox | None,
        log: Callable[[str], None],
    ) -> None:
        """Bind one library session to the VibeSys policy that drives it."""
        self._session = session
        self._spec = spec
        self._profile = profile
        self._timeout = timeout
        self._event_handler = event_handler
        self._sandbox = sandbox
        self._log = log
        self._mcp_servers = tuple(
            _as_mcp_server(server, sandbox, pin_interpreter=not spec.policy.containerized)
            for server in spec.mcp_servers
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
        """Translate one VibeSys turn into the library's request."""
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
            mcp_servers=self._mcp_servers,
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
                cli_dir=_agent_path(self._sandbox, host_dir),
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
            # is the transformed one: a containerized turn's is a
            # ``docker exec -e KEY=VALUE ...`` line carrying every environment
            # value the sandbox was built with, which callers log verbatim.
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
    stale value makes them miss ``<workspace>/opencode.json``.
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
        """Configure one provider; ``executor_factory`` replaces the base executor.

        ``docker_sandboxes`` maps a session's role to an already-started
        :class:`~vs_sandbox.DockerSandbox` (built and started by the run
        environment, not by this driver). Its presence is what selects
        container execution: every session this driver creates then looks up
        its sandbox there instead of building a host one.

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
        self._executor_factory: ExecutorFactory = executor_factory or agentshim.HostCommandExecutor
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
        """Create one configured AgentShim conversation.

        Every session takes the same route: look up or build the sandbox for
        this role, confine a fresh executor to it, and hand the library the
        sandbox's own environment. A container session additionally runs
        through the Codex rollout watchdog, which no-ops for every other
        provider and every non-``exec --json`` command.
        """
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
        sandbox, find_binary = self._sandbox_for(spec)

        executor: agentshim.CommandExecutor = self._executor_factory()
        if sandbox is not None:
            executor = confine_to_sandbox(executor, sandbox, find_binary=find_binary)
        if in_container:
            from vibesys.agents.docker_executor import CodexRolloutWatchdogExecutor  # noqa: PLC0415

            executor = CodexRolloutWatchdogExecutor(
                executor,
                self._container_id_resolver(spec),
                rollout_sessions_root=_codex_rollout_sessions_root(sandbox),
                log=self._log,
            )

        env = sandbox.env if sandbox is not None else self._unconfined_host_env(spec)
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
            session=agent.start_session(cwd=str(spec.workspace), timeout=self._timeout),
            spec=spec,
            profile=agent.profile,
            timeout=self._timeout,
            event_handler=event_handler,
            sandbox=sandbox,
            log=self._log,
        )
        self._sessions.add(session)
        return session

    def _sandbox_for(
        self,
        spec: AgentSessionSpec,
    ) -> tuple[Any, Callable[[str, Mapping[str, str]], str] | None]:
        """Return the sandbox this session confines to, and its binary lookup.

        A container session's sandbox already exists, started by the run
        environment; a host session's is built fresh from the declared
        resources (and may come back ``None`` where confinement is
        unavailable or disabled). A container's binary lookup trusts the bare
        name to ``docker exec``'s own ``PATH`` instead of searching this host,
        which the container's environment does not describe.
        """
        if self._docker_sandboxes is not None:
            return self._docker_sandbox_for(spec), _bare_binary_name
        env = self._unconfined_host_env(spec)
        return self._host_sandbox(spec, env), None

    def _unconfined_host_env(self, spec: AgentSessionSpec) -> dict[str, str]:
        """Return the host session environment before any sandbox is applied.

        Used both to build the host sandbox (whose own ``env`` then reflects
        it) and as the session environment when confinement came back
        unavailable.
        """
        return _without_stale_pwd({**agentshim.interactive_env(), **dict(spec.environment)})

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

    def _docker_sandbox_for(self, spec: AgentSessionSpec) -> Any:  # noqa: ANN401  # tracked: #288
        assert self._docker_sandboxes is not None  # noqa: S101  # tracked: #288
        sandbox = self._docker_sandboxes.get(spec.role)
        if sandbox is None:
            raise ValueError(  # noqa: TRY003  # tracked: #288
                f"no AgentShim Docker sandbox configured for role {spec.role!r}"
            )
        return sandbox

    def _container_id_resolver(self, spec: AgentSessionSpec) -> Callable[[], str]:
        """Return the sandbox's current container ID, read at every call.

        Read through the sandbox rather than captured, because a GPU reselect
        replaces the container and nothing should then have to rebuild the
        executor or the cleanup hook.
        """

        def resolve() -> str:
            return str(self._docker_sandbox_for(spec).container_id)

        return resolve

    def close(self) -> None:
        """Close every session created by this driver, idempotently."""
        if self._closed:
            return
        self._closed = True
        for session in self._sessions:
            session.close()
        self._sessions.clear()


def _codex_rollout_sessions_root(sandbox: _ConfinableSandbox | None) -> str:
    """Return the sessions directory a resumed Codex thread writes its rollout to.

    Derived from the sandbox's own ``HOME`` and the Codex provider's state
    directory convention, never a hardcoded ``/root`` or ``/home/agent``: the
    watchdog only ever polls a container sandbox, but the value is computed
    generically so nothing here has to know that in advance.
    """
    home = "" if sandbox is None else sandbox.env.get("HOME", "")
    state_dir = agentshim.get_provider(CODEX_PROVIDER).profile.state_dirs[0]
    return f"{home}/{state_dir}/sessions"
