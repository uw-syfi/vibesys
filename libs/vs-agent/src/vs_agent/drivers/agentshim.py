"""AgentShim implementation of the stateful agent-driver contract.

VibeSys consumes the ``agentshim`` library only through its public API. The
library owns provider knowledge (argv, stream parsing, MCP config files,
schema dialects, resume flags); this module owns VibeSys policy: sandbox
confinement, structured-output fallback, conversation budgets, and the
translation between library events and :mod:`vs_agent.contracts`.

One path runs every session, host or container: build (or look up) a
:class:`~vs_sandbox.WorkspaceSandbox`, wrap a plain ``HostCommandExecutor`` to
confine it, and hand the library the sandbox's own environment. Every path the
agent is told about -- the schema directory, an MCP server's command -- goes
through :meth:`~vs_sandbox.WorkspaceSandbox.agent_path`, so nothing here names
a backend.
"""

from __future__ import annotations

import json
import sys
import threading
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from weakref import WeakSet

import agentshim

from vs_agent.cli_common import build_schema_hint
from vs_agent.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentEventKind,
    AgentObserver,
    AgentOutputSchemaError,
    AgentRateLimit,
    AgentSession,
    AgentSessionSpec,
    AgentSkillUse,
    AgentSpawnError,
    AgentTurnRequest,
    AgentTurnResult,
    AgentTurnTimeoutError,
    AgentUsage,
    AuthStatus,
    MCPServerSpec,
    ProviderReadiness,
    SessionDisposition,
    SteerOutcome,
)
from vs_agent.docker_confinement import DockerSandboxConfinement
from vs_agent.docker_executor import CodexRolloutWatchdogExecutor
from vs_agent.events import CommandResultPayload
from vs_agent.host_resource_declarations import (
    declare_agent_host_resources,
    prepare_provider_state,
)
from vs_agent.provider_policy import CODEX_PROVIDER, SHIPPED_PROVIDERS
from vs_agent.session_environment import (
    dropped_launcher_names,
    session_environment,
    validate_env_names,
)
from vs_agent.session_errors import SessionResumeError
from vs_sandbox.api import build_host_sandbox

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from pydantic import BaseModel

    from vs_sandbox.api import DockerSandbox, WorkspaceSandbox

AGENTSHIM_CAPABILITIES = AgentCapabilities(
    tool_servers=True,
    nested_read_only_paths=True,
    hidden_paths=True,
    timeouts=True,
    session_reuse=True,
    provider_session_resume=True,
)
"""Capabilities invariant across AgentShim host and container execution.

``provider_session_resume``, ``skill_isolation``, ``mcp_isolation`` and
``config_isolation`` are narrowed per provider from
:attr:`agentshim.ProviderProfile.supports_resume`,
:attr:`agentshim.ProviderProfile.skill_scopes`,
:attr:`agentshim.ProviderProfile.mcp_scopes` and
:attr:`agentshim.ProviderProfile.config_scopes` when the driver is built.
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

TRANSIENT_RETRY_DELAYS_S: tuple[float, ...] = (30.0, 60.0, 120.0, 240.0, 480.0)
"""Waits in seconds before each retry of a turn that failed on a transient provider error.

agentshim classifies the failure (``agentshim.FailureKind.TRANSIENT``: an
overload, a rate limit, or a server error) and its ``Session`` waits these
delays (``agentshim.RetryPolicy``). The provider CLI has already retried inside
the turn before it exits, so by then the outage has lasted minutes; these waits
add about fifteen more before the error reaches the run, which otherwise ends
on the first one.
"""


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
        return _find_host_binary(binary, env)
    except agentshim.CliNotFoundError:
        return None


def _find_host_binary(binary: str, env: Mapping[str, str]) -> str:
    """Resolve a host CLI symlink before passing its path into confinement."""
    path = agentshim.HostCommandExecutor().find_binary(binary, env)
    return str(Path(path).resolve())


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
        env={**dict(spec.env), **dict(spec.runtime_env)},
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
    if not usage.increment_known:
        # Unknown resumed totals are zero placeholders, not measured increments.
        # Duration belongs to this invocation and is independent of its tokens.
        return AgentUsage(duration_ms=duration_ms)
    tokens = usage.tokens
    return AgentUsage(
        input_tokens=tokens.input_tokens,
        cache_creation_input_tokens=tokens.cache_write_input_tokens,
        cache_read_input_tokens=tokens.cache_read_input_tokens,
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
    return (
        _translate_skill(event)
        or _translate_rate_limit(event)
        or _translate_steer(event)
        or _translate_plumbing(event)
    )


def _translate_skill(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Translate a skill load, and log the offered list where a run log shows it.

    Which provider frames mean a skill was offered or loaded is agentshim's
    knowledge; this only maps its typed events.
    """
    if isinstance(event, agentshim.SkillInvoked):
        return AgentEvent(
            kind=AgentEventKind.SKILL,
            text=event.name,
            payload={"skill": event.name, "source_path": event.source_path},
        )
    if isinstance(event, agentshim.SkillsDiscovered):
        return _diagnostic(f"[skills offered] {', '.join(event.names) or '(none)'}")
    return None


def _translate_rate_limit(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Translate the provider's report of one rate-limit window."""
    if not isinstance(event, agentshim.RateLimitStatus):
        return None
    return AgentEvent(
        kind=AgentEventKind.RATE_LIMIT,
        rate_limit=AgentRateLimit(
            window=event.window,
            limit=event.limit,
            used_fraction=event.used_fraction,
            resets_at=event.resets_at,
            window_minutes=event.window_minutes,
            exhausted=event.exhausted,
        ),
    )


def _translate_steer(event: agentshim.AgentEvent) -> AgentEvent | None:
    """Report what became of an operator message sent into the running turn."""
    if isinstance(event, agentshim.SteerDelivered):
        return _diagnostic("[steer] the provider accepted an operator message mid-turn")
    if isinstance(event, agentshim.SteerConsumed):
        return _diagnostic("[steer] the model took the operator message into the running turn")
    if isinstance(event, agentshim.SteerRejected):
        return _diagnostic(
            f"[steer] the provider refused the operator message ({event.reason}); "
            "it is delivered at the next turn boundary instead"
        )
    return None


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


class _SteerLedger:
    """The steers this session offered whose fate the provider has not yet reported.

    A provider that accepts a message may still refuse it a moment later
    (``SteerRejected``). The ledger remembers each accepted text with the
    caller's fallback, so a refusal reaches the caller exactly once and a text
    the model consumed is forgotten. Thread-safe: ``steer`` runs on a
    caller's thread, the library's events on the turn's.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._waiting: dict[str, deque[Callable[[], None]]] = {}

    def expect(self, text: str, on_rejected: Callable[[], None]) -> None:
        with self._lock:
            self._waiting.setdefault(text, deque()).append(on_rejected)

    def forget(self, text: str, on_rejected: Callable[[], None]) -> None:
        with self._lock:
            callbacks = self._waiting.get(text)
            if callbacks is not None and on_rejected in callbacks:
                callbacks.remove(on_rejected)
                if not callbacks:
                    del self._waiting[text]

    def consumed(self, text: str) -> None:
        self._pop(text)

    def rejected(self, text: str) -> None:
        callback = self._pop(text)
        if callback is not None:
            callback()

    def clear(self) -> None:
        with self._lock:
            self._waiting.clear()

    def _pop(self, text: str) -> Callable[[], None] | None:
        with self._lock:
            callbacks = self._waiting.get(text)
            if not callbacks:
                return None
            callback = callbacks.popleft()
            if not callbacks:
                del self._waiting[text]
            return callback


class _AgentShimEventHandler:
    """Route the library's typed events to the turn's observer."""

    def __init__(self, steers: _SteerLedger) -> None:
        self.observer: AgentObserver | None = None
        #: Whether the turn in flight asked for a response schema.
        self.structured = False
        self._steers = steers

    def on_event(self, event: agentshim.AgentEvent) -> None:
        """Translate and forward one library event, if anyone is listening."""
        if isinstance(event, agentshim.SteerConsumed):
            self._steers.consumed(event.text)
        elif isinstance(event, agentshim.SteerRejected):
            self._steers.rejected(event.text)
        observer = self.observer
        if observer is None:
            return
        translated = _translate(event, structured=self.structured)
        if translated is not None:
            observer.on_event(translated)


class AgentShimSession:
    """One configured AgentShim conversation.

    Recovery policy (transient waits, a refused resume, a thread grown too
    large) belongs to :class:`agentshim.Session`; this class translates between
    its turns and the :class:`~vs_agent.contracts.AgentSession` contract.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-010138 [PLR0913]; Preserve AgentShimSession.__init__'s named-argument contract because callers pass these independent settings directly.
        self,
        *,
        session: agentshim.Session,
        spec: AgentSessionSpec,
        profile: agentshim.ProviderProfile,
        timeout: int | None,
        event_handler: _AgentShimEventHandler,
        steers: _SteerLedger,
        sandbox: _ConfinableSandbox | None,
        log: Callable[[str], None],
    ) -> None:
        """Bind one library session to the VibeSys policy that drives it."""
        self._session = session
        self._spec = spec
        self._profile = profile
        self._timeout = timeout
        self._event_handler = event_handler
        self._steers = steers
        self._sandbox = sandbox
        self._log = log
        self._mcp_servers = tuple(
            _as_mcp_server(server, sandbox, pin_interpreter=not spec.policy.containerized)
            for server in spec.mcp_servers
        )
        self._closed = False

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Add one turn to the conversation and return its raw result."""
        if self._closed:
            message = "agent session is closed"
            raise RuntimeError(message)

        expected = request.expected_provider_session_id
        self._event_handler.observer = observer
        self._event_handler.structured = request.output_schema is not None
        try:
            if expected is None:
                turn = self._run(request, expect_conversation=None)
            else:
                try:
                    turn = self._run(request, expect_conversation=expected)
                except agentshim.ContinuityError as error:
                    raise SessionResumeError(
                        expected, "session has not adopted the expected conversation"
                    ) from error
                except agentshim.AgentShimError as error:
                    raise SessionResumeError(expected, str(error)) from error
        finally:
            self._event_handler.observer = None
            self._event_handler.structured = False
            self._steers.clear()

        result = turn.result
        if result.interrupted:
            # The conversation survives an interrupt, but the caller asked for
            # an answer and there is none: a stopped turn raises.
            message = "turn interrupted"
            raise agentshim.TurnCancelledError(message)
        # RESET and REPLACED both say the conversation the caller was
        # continuing is gone (renewed, or restarted after a refused resume).
        restarted = turn.continuity is not agentshim.Continuity.CONTINUED
        self._log_continuity(turn.continuity)
        return AgentTurnResult(
            text=_result_text(result),
            usage=_usage_from(
                result.usage,
                cost_usd=result.cost_usd,
                duration_ms=result.duration_ms,
            ),
            provider_session_id=result.session_id,
            disposition=(
                SessionDisposition.RESET_REQUIRED if restarted else SessionDisposition.REUSABLE
            ),
            skills=_skill_use(result.skills),
        )

    def steer(self, text: str, *, on_rejected: Callable[[], None]) -> SteerOutcome:
        """Offer *text* to the running turn, on a transport that can take it.

        A one-shot provider process reads no input after launch, so its
        profile says it cannot steer and the library is not asked. A refusal
        the provider reports after accepting the message calls *on_rejected*
        (see :class:`~vs_agent.contracts.SteerableSession`).
        """
        if not self._profile.supports_steer:
            return SteerOutcome.UNSUPPORTED
        self._steers.expect(text, on_rejected)
        try:
            self._session.steer(text)
        except agentshim.NoRunningTurnError:
            self._steers.forget(text, on_rejected)
            return SteerOutcome.NO_RUNNING_TURN
        except agentshim.ProviderCapabilityError:
            self._steers.forget(text, on_rejected)
            return SteerOutcome.UNSUPPORTED
        return SteerOutcome.DELIVERED

    def _log_continuity(self, continuity: agentshim.Continuity) -> None:
        """Tell the operator when the library dropped the conversation behind a turn.

        The library decides these restarts silently, so this is the only record
        that history was lost.
        """
        name = self._profile.name
        if continuity is agentshim.Continuity.RESET:
            self._log(f"renewing {name} thread; durable workspace state remains authoritative.")
        elif continuity is agentshim.Continuity.REPLACED:
            self._log(
                f"{name} session is no longer available; this turn ran in a fresh conversation."
            )

    def cancel(self) -> None:
        """Stop an in-flight turn by terminating the provider process.

        A turn waiting out a transient provider error stops waiting and raises
        that error instead of retrying.
        """
        self._session.interrupt()

    def close(self) -> None:
        """Release this logical session, stopping any turn it still owns."""
        self._session.close()
        self._closed = True
        self._event_handler.observer = None

    def resume_provider_session(self, session_id: str) -> bool:
        """Continue ``session_id`` on the next turn, if the session accepts it.

        A session that already named a conversation keeps it: its history is
        newer than any checkpoint the caller holds. Beyond that the library
        decides, adopting the ID only when the provider has a resume flag and
        no turn is in flight. A stale or deleted transcript is handled later,
        by the library's fresh-conversation retry around the turn.
        """
        return self._session.adopt(session_id)

    def _run(self, request: AgentTurnRequest, *, expect_conversation: str | None) -> agentshim.Turn:
        """Prepare and run one turn, translating library failures to the driver contract.

        A provider that gave up matching the output schema
        (``agentshim.FailureKind.SCHEMA``) raises ``AgentOutputSchemaError``
        with its validation errors. The library has already decided whether to
        retry by then, and keeps the conversation the correction turn continues.
        """
        held = self._session.conversation_id
        ticket = self._session.prepare_turn(
            self._build_request(request),
            expect_conversation=expect_conversation,
            pin=request.require_provider_checkpoint,
        )
        try:
            return self._session.run(ticket)
        except (OSError, ImportError, agentshim.CliNotFoundError) as exc:
            raise AgentSpawnError(self._profile.name, str(exc)) from exc
        except agentshim.TurnTimeoutError as exc:
            # A one-shot process reports the budget as `CliTimeoutError`; a
            # long-lived one raises its parent, `TurnTimeoutError`.
            raise AgentTurnTimeoutError(exc.timeout) from exc
        except agentshim.TurnFailedError as exc:
            if exc.kind is agentshim.FailureKind.SCHEMA:
                raise AgentOutputSchemaError(exc.detail) from exc
            if held is not None and self._session.conversation_id is None:
                self._log(
                    f"the resumed {self._profile.name} turn failed; dropped the conversation "
                    "so the next turn starts fresh."
                )
            raise

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
        # The dialect check runs on the schema the CLI will receive: the
        # normalizer is what makes pydantic's optional fields nullable and
        # closes objects, so checking the raw schema rejects models the
        # provider accepts.
        schema = agentshim.normalize(response_cls.model_json_schema(), dialect)
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
                schema=schema,
                host_dir=host_dir,
                cli_dir=_agent_path(self._sandbox, host_dir),
            ),
            "",
        )


def _skill_use(summary: agentshim.SkillSummary) -> AgentSkillUse:
    """Carry the library's skill summary over, keeping unknown distinct from zero."""
    invocations = summary.invocations
    return AgentSkillUse(
        offered=summary.discovered,
        invoked=None if invocations is None else tuple(event.name for event in invocations),
    )


def _result_text(result: agentshim.TurnResult) -> str:
    """Return the turn's answer, preferring the schema-conformant payload.

    Callers parse a structured turn's text back into their response model, so
    a provider that reported the payload out of band still has to deliver it
    as text.
    """
    if result.structured_output is not None:
        return json.dumps(result.structured_output)
    return result.text


def _skill_scope(profile: agentshim.ProviderProfile) -> agentshim.SkillScope:
    """Offer a session only the run's skills wherever the provider can enforce it.

    A run's behavior must not depend on who launches it, so the operator's
    personal and plugin skills stay out (``SkillScope.PROJECT``). A provider
    with no mechanism for that keeps ``ALL``; the driver reports it through
    ``AgentCapabilities.skill_isolation`` and logs it per session rather than
    refusing to run.
    """
    if agentshim.SkillScope.PROJECT in profile.skill_scopes:
        return agentshim.SkillScope.PROJECT
    return agentshim.SkillScope.ALL


def _mcp_scope(profile: agentshim.ProviderProfile) -> agentshim.McpScope:
    """Connect a session only to the MCP servers the run configured.

    The operator's own MCP servers (user or project configuration, plugins,
    account connectors) must not change what a run's agents can call, so a
    session gets ``McpScope.SESSION`` wherever the provider can enforce it. A
    provider with no mechanism keeps ``ALL``; the driver reports it through
    ``AgentCapabilities.mcp_isolation`` and logs it per session rather than
    refusing to run.
    """
    if agentshim.McpScope.SESSION in profile.mcp_scopes:
        return agentshim.McpScope.SESSION
    return agentshim.McpScope.ALL


def _config_scope(profile: agentshim.ProviderProfile, *, has_home: bool) -> agentshim.ConfigScope:
    """Keep the operator's own CLI configuration out of a session where possible.

    Settings, hooks, global instructions, notify commands and memory in the
    operator's provider state must not change what a run's agents do, so a
    session gets ``ConfigScope.PROJECT`` wherever the provider can enforce
    it. A provider that can only enforce it in a dedicated state root
    (``profile.config_home_files``) needs *has_home*: a run-owned host home
    the driver can prepare. Otherwise the session keeps ``ALL``; the driver
    reports it through ``AgentCapabilities.config_isolation`` and logs it per
    session rather than refusing to run.
    """
    if agentshim.ConfigScope.PROJECT not in profile.config_scopes:
        return agentshim.ConfigScope.ALL
    if profile.config_home_files and not has_home:
        return agentshim.ConfigScope.ALL
    return agentshim.ConfigScope.PROJECT


def _without_stale_pwd(env: Mapping[str, str]) -> dict[str, str]:
    """Drop ``PWD`` so the CLI trusts its real working directory.

    A subprocess cwd does not rewrite ``$PWD``, and bun-based CLIs (opencode)
    trust ``$PWD`` over the real cwd for workspace config discovery, so a
    stale value makes them miss ``<workspace>/opencode.json``.
    """
    return {key: value for key, value in env.items() if key != "PWD"}


@dataclass(frozen=True, slots=True)
class _Launch:
    """What one session (or readiness probe) runs the provider CLI with."""

    executor: agentshim.CommandExecutor
    env: Mapping[str, str]
    sandbox: _ConfinableSandbox | None
    config_scope: agentshim.ConfigScope
    transport: agentshim.TransportKind
    confinement: agentshim.Confinement | None = None
    """Set for a long-lived transport in a container: agentshim confines, maps and reaps.

    ``executor`` is then the plain executor and ``env`` is unused (the
    confinement supplies the environment). Without it the executor is already
    confined and ``env`` is the agent's environment.
    """


_AUTH_STATUS = {
    agentshim.AuthState.KNOWN_OK: AuthStatus.OK,
    agentshim.AuthState.FAILED: AuthStatus.FAILED,
    agentshim.AuthState.UNKNOWN: AuthStatus.UNKNOWN,
}


def _readiness_from(status: agentshim.ProviderStatus) -> ProviderReadiness:
    """Translate the library's probe result into the neutral readiness contract."""
    return ProviderReadiness(
        provider=status.provider,
        binary_found=status.binary_found,
        path=status.path,
        version=status.version,
        auth=_AUTH_STATUS[status.auth],
        detail=status.auth_detail,
    )


class AgentShimDriver:
    """Create AgentShim sessions and translate VibeSys execution policy."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-010139 [PLR0913]; Preserve AgentShimDriver.__init__'s named-argument contract because callers pass these independent settings directly.
        self,
        *,
        provider: str,
        timeout: int | None = None,
        docker_sandboxes: dict[str, DockerSandbox] | None = None,
        log: Callable[[str], None] | None = None,
        executor_factory: ExecutorFactory | None = None,
        check_timeout: float | None = None,
        transient_retry_delays: Sequence[float] = TRANSIENT_RETRY_DELAYS_S,
        agent_homes: Path | None = None,
        env_passthrough: Sequence[str] = (),
        launcher_env: Callable[[], Mapping[str, str]] = agentshim.interactive_env,
        transport: agentshim.TransportKind | None = None,
        clock: agentshim.Clock | None = None,
        ids: agentshim.IdAllocator | None = None,
    ) -> None:
        """Configure one provider; ``executor_factory`` replaces the base executor.

        ``transport`` fixes how every session reaches the provider. Left as
        ``None`` it is derived: a container session of a provider that
        agentshim lists in ``stream_provider_names()`` keeps one long-lived
        process per conversation (``TransportKind.STREAM``); every other
        session runs one process per turn (``TransportKind.ONE_SHOT``).

        ``agent_homes`` is the run's root for dedicated provider CLI homes
        (one subdirectory per provider, shared by every session the run opens
        so a conversation resumes across candidates). A host session of a
        provider that keeps the operator's configuration in its state root
        runs against that home; see :func:`_config_scope`.

        ``launcher_env`` reads the environment VibeSys was launched with (by
        default the interactive login shell's); a session inherits only the
        allowlisted part of it plus ``env_passthrough`` names (see
        :mod:`vs_agent.session_environment`).

        ``docker_sandboxes`` maps a session's role to an already-started
        :class:`~vs_sandbox.DockerSandbox` (built and started by the run
        environment, not by this driver). Its presence is what selects
        container execution: every session this driver creates then looks up
        its sandbox there instead of building a host one.

        ``check_timeout`` bounds the one-off ``<binary> --help`` health check
        each session runs before its first turn. It defaults to the execution
        mode's budget: a container check crosses a ``docker exec`` and is given
        four times as long as a host one.

        ``clock`` and ``ids`` replace agentshim's wall clock and random turn ids,
        so a test measures turn timeouts and retry waits on a fake clock and
        names turns reproducibly; production leaves them ``None``.

        ``transient_retry_delays`` are the waits before each retry of a turn
        that failed on a transient provider error; see
        :data:`TRANSIENT_RETRY_DELAYS_S`.
        """
        if provider not in SHIPPED_PROVIDERS:
            message = (
                f"unknown AgentShim provider {provider!r}; expected one of: {supported_providers()}"
            )
            raise ValueError(message)
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
        self._transient_retry_delays = tuple(transient_retry_delays)
        self._agent_homes = agent_homes
        self._env_passthrough = validate_env_names(env_passthrough)
        self._dropped_names_logged = False
        self._dropped_names_lock = threading.Lock()
        self._launcher_env = launcher_env
        self._transport = transport
        self._clock = clock
        self._ids = ids
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
            skill_isolation=_skill_scope(agentshim.get_provider(self._provider).profile)
            is agentshim.SkillScope.PROJECT,
            mcp_isolation=_mcp_scope(agentshim.get_provider(self._provider).profile)
            is agentshim.McpScope.SESSION,
            config_isolation=self._config_scope_for(agentshim.get_provider(self._provider).profile)
            is agentshim.ConfigScope.PROJECT,
        )

    def _config_scope_for(self, profile: agentshim.ProviderProfile) -> agentshim.ConfigScope:
        # A container keeps its own state root inside the container, which a
        # host-side home cannot replace.
        has_home = self._agent_homes is not None and self._docker_sandboxes is None
        return _config_scope(profile, has_home=has_home)

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create a session, classifying process setup failures as retryable faults."""
        try:
            return self._create_session(spec)
        except (OSError, ImportError, agentshim.CliNotFoundError, agentshim.CliCheckError) as exc:
            raise AgentSpawnError(spec.provider, str(exc)) from exc

    def probe_readiness(self, spec: AgentSessionSpec) -> ProviderReadiness:
        """Report whether the CLI *spec* would launch is installed and logged in.

        The probe runs on the executor, confinement and environment
        :meth:`create_session` builds for the same *spec* (one code path,
        :meth:`_launch_for`), so a container or sandbox is probed where the
        agent would run. No model is called. A missing binary is a result,
        not an exception; the caller decides what a problem means.
        """
        try:
            launch = self._launch_for(spec)
        except (OSError, ImportError) as exc:
            raise AgentSpawnError(spec.provider, str(exc)) from exc
        status = agentshim.probe_provider(
            spec.provider,
            executor=launch.executor,
            confinement=launch.confinement,
            env=None if launch.confinement is not None else launch.env,
            timeout=self._check_timeout,
        )
        return _readiness_from(status)

    def _launch_for(self, spec: AgentSessionSpec) -> _Launch:
        """Validate *spec* and build the executor, environment and sandbox it runs with.

        Every session takes the same route: look up or build the sandbox for
        this role, confine a fresh executor to it, and hand the library the
        sandbox's own environment. A container session additionally runs
        through the Codex rollout watchdog, which no-ops for every other
        provider and every non-``exec --json`` command.
        """
        if self._closed:
            message = "agent driver is closed"
            raise RuntimeError(message)
        if spec.provider != self._provider:
            message = (
                f"AgentShimDriver for {self._provider!r} cannot create a {spec.provider!r} session"
            )
            raise ValueError(message)
        in_container = self._docker_sandboxes is not None
        if spec.policy.containerized != in_container:
            message = (
                "agent session container policy does not match the configured "
                "AgentShim execution mode"
            )
            raise ValueError(message)

        provider = agentshim.get_provider(spec.provider)
        config_scope = self._config_scope_for(provider.profile)
        sandbox, find_binary, host_env = self._sandbox_for(spec, config_scope)
        transport = self._transport_for(spec)

        if transport is agentshim.TransportKind.STREAM and in_container:
            # agentshim confines the long-lived process itself, so it can mark
            # it for `reap` and map the working directory, MCP commands and
            # schema directory the way the container sees them. A rollout
            # watchdog guards one-shot `codex exec` runs; no such run exists.
            confinement = DockerSandboxConfinement(
                self._docker_sandbox_for(spec), runner=self._executor_factory()
            )
            return _Launch(
                executor=self._executor_factory(),
                env=confinement.env,
                sandbox=sandbox,
                config_scope=config_scope,
                transport=transport,
                confinement=confinement,
            )

        executor: agentshim.CommandExecutor = self._executor_factory()
        if sandbox is not None:
            executor = confine_to_sandbox(executor, sandbox, find_binary=find_binary)
        if in_container:
            executor = CodexRolloutWatchdogExecutor(
                executor,
                self._container_id_resolver(spec),
                rollout_sessions_root=_codex_rollout_sessions_root(sandbox),
                log=self._log,
            )
        env = sandbox.env if sandbox is not None else host_env
        return _Launch(
            executor=executor,
            env=env,
            sandbox=sandbox,
            config_scope=config_scope,
            transport=transport,
        )

    def _transport_for(self, spec: AgentSessionSpec) -> agentshim.TransportKind:
        """Choose the transport from the execution mode and agentshim's registry."""
        if self._transport is not None:
            return self._transport
        if spec.policy.containerized and spec.provider in agentshim.stream_provider_names():
            return agentshim.TransportKind.STREAM
        return agentshim.TransportKind.ONE_SHOT

    def _create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create one configured AgentShim conversation."""
        launch = self._launch_for(spec)
        sandbox = launch.sandbox
        config_scope = launch.config_scope
        steers = _SteerLedger()
        event_handler = _AgentShimEventHandler(steers)
        agent = agentshim.Agent(
            spec.provider,
            model=spec.model,
            executor=launch.executor,
            confinement=launch.confinement,
            transport=launch.transport,
            permissions=agentshim.NativePermissions.bypass(),
            approvals=agentshim.ApprovalPolicy.DENY,
            retry=agentshim.RetryPolicy(delays=self._transient_retry_delays),
            event_handlers=[event_handler],
            env=None if launch.confinement is not None else launch.env,
            log=self._log,
            check_timeout=self._check_timeout,
            clock=self._clock,
            ids=self._ids,
        )
        skill_scope = _skill_scope(agent.profile)
        if skill_scope is not agentshim.SkillScope.PROJECT:
            self._log(
                f"{agent.profile.display_name} cannot hide the operator's own skills; "
                "this session is offered them beside the run's"
            )
        mcp_scope = _mcp_scope(agent.profile)
        if mcp_scope is not agentshim.McpScope.SESSION:
            self._log(
                f"{agent.profile.display_name} cannot hide the operator's own MCP servers; "
                "this session is connected to them beside the run's"
            )
        if config_scope is not agentshim.ConfigScope.PROJECT:
            self._log(
                f"{agent.profile.display_name} cannot hide the operator's own CLI configuration; "
                "this session loads their settings, hooks and global instructions"
            )
        session = AgentShimSession(
            session=agent.session(
                str(spec.workspace),
                skill_scope=skill_scope,
                mcp_scope=mcp_scope,
                config_scope=config_scope,
            ),
            spec=spec,
            profile=agent.profile,
            timeout=self._timeout,
            event_handler=event_handler,
            steers=steers,
            # agentshim maps paths itself when it confines the process.
            sandbox=None if launch.confinement is not None else sandbox,
            log=self._log,
        )
        self._sessions.add(session)
        return session

    def _sandbox_for(
        self,
        spec: AgentSessionSpec,
        config_scope: agentshim.ConfigScope,
    ) -> tuple[
        WorkspaceSandbox | None,
        Callable[[str, Mapping[str, str]], str] | None,
        dict[str, str],
    ]:
        """Return the sandbox this session confines to, its binary lookup, and host env.

        The host environment (empty for a container session) is what the
        session runs with when host confinement came back ``None``.

        A container session's sandbox already exists, started by the run
        environment; a host session's is built fresh from the declared
        resources (and may come back ``None`` where confinement is
        unavailable or disabled). A container's binary lookup trusts the bare
        name to ``docker exec``'s own ``PATH`` instead of searching this host,
        which the container's environment does not describe.
        """
        if self._docker_sandboxes is not None:
            return self._docker_sandbox_for(spec), _bare_binary_name, {}
        env = self._unconfined_host_env(spec, config_scope)
        return self._host_sandbox(spec, env), _find_host_binary, env

    def _unconfined_host_env(
        self, spec: AgentSessionSpec, config_scope: agentshim.ConfigScope
    ) -> dict[str, str]:
        """Return the host session environment before any sandbox is applied.

        The allowlisted launcher environment, the run's own variables, and,
        for ``ConfigScope.PROJECT``, the variables that point the CLI at the
        run's dedicated home (which this call prepares). Used both to build
        the host sandbox (whose own ``env`` then reflects it) and as the
        session environment when confinement came back unavailable.
        """
        profile = agentshim.get_provider(spec.provider).profile
        launcher = self._launcher_env()
        self._log_dropped_names_once(launcher, profile)
        env = session_environment(
            launcher,
            profile=profile,
            passthrough=self._env_passthrough,
            run=dict(spec.environment),
        )
        if config_scope is agentshim.ConfigScope.PROJECT and self._agent_homes is not None:
            home = self._agent_homes / spec.provider
            env.update(agentshim.prepare_config_home(profile, home, env))
        prepare_provider_state(env, profile=profile)
        return _without_stale_pwd(env)

    def _log_dropped_names_once(
        self, launcher: Mapping[str, str], profile: agentshim.ProviderProfile
    ) -> None:
        """Log, once per driver (one run), which launcher variables sessions do not inherit.

        Names only, never values. An operator who needs one adds it to
        ``[agent] env_passthrough``.
        """
        with self._dropped_names_lock:
            if self._dropped_names_logged:
                return
            self._dropped_names_logged = True
        dropped = dropped_launcher_names(
            launcher, profile=profile, passthrough=self._env_passthrough
        )
        if dropped:
            self._log(
                "[env] agent sessions do not inherit these launcher variables "
                f"(add names to [agent] env_passthrough to pass them): {', '.join(dropped)}"
            )

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

    def _docker_sandbox_for(self, spec: AgentSessionSpec) -> DockerSandbox:
        if self._docker_sandboxes is None:
            message = "container execution has no Docker sandbox registry"
            raise AssertionError(message)
        sandbox = self._docker_sandboxes.get(spec.role)
        if sandbox is None:
            message = f"no AgentShim Docker sandbox configured for role {spec.role!r}"
            raise ValueError(message)
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
