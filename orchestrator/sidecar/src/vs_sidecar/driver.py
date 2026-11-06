"""The one place provider knowledge meets the sidecar: a ``Driver`` runs a session through agentshim.

Everything the engine needs from a provider is in the ``Driver`` protocol. ``AgentshimDriver`` is
the implementation over an ``agentshim.Session``; the harness to provider mapping lives here and
nowhere else. Tests give it an ``AgentFactory`` over agentshim's scripted providers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentshim import (
    Agent,
    AgentShimError,
    ApprovalPolicy,
    CliNotFoundError,
    FailureKind,
    McpScope,
    McpServerState,
    NativePermissions,
    NoRunningTurnError,
    PluginInstallError,
    PluginRef,
    ProviderCapabilityError,
    ProviderUsage,
    SessionPlugins,
    SessionStateError,
    StdioMcpServer,
    TransportKind,
    TurnCancelledError,
    TurnFailedError,
    TurnRequest,
    interactive_env,
)

from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agentshim import AgentEvent, RawFrame, Session, TurnTicket

    from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc

#: The provider name agentshim knows each harness by. The only such table in the sidecar.
PROVIDERS: Mapping[int, str] = {
    orch.HARNESS_CLAUDE_CODE: "claude",
    orch.HARNESS_CODEX: "codex",
}


@dataclass(frozen=True)
class Opened:
    """A session that is ready."""

    conversation_id: str
    capabilities: orch.Capabilities


@dataclass(frozen=True)
class TurnResult:
    """How a turn ended: a final ``TurnStatus``, what it used, and why it failed."""

    status: int
    usage: ProviderUsage | None = None
    detail: str = ""


#: How long, once the agent is up, to wait for its tool servers to connect before the session is
#: called open. A server that has not said by then is unknown, not failed.
TOOLS_WAIT_SECONDS = 30.0


class StartError(Exception):
    """The session could not be opened. ``reason`` is a ``StartFailureReason``; the text says what."""

    def __init__(self, reason: int, detail: str) -> None:
        """Record why the session did not open, in the provider's words where it has them."""
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def classify_start_error(error: BaseException, workdir: str | None) -> int:
    """The ``StartFailureReason`` of an error raised while a session was opening.

    Only what the error's type says is read, never its text. An error that is none of the known
    kinds is ``OTHER``.
    """
    if isinstance(error, PluginInstallError):
        return orch.START_FAILURE_REASON_PLUGIN_UNAVAILABLE
    if isinstance(error, CliNotFoundError):
        return orch.START_FAILURE_REASON_AGENT_NOT_INSTALLED
    if isinstance(error, TurnFailedError) and error.kind is FailureKind.AUTH:
        return orch.START_FAILURE_REASON_NOT_SIGNED_IN
    # A provider may wrap the operating system's refusal to enter the directory, so its cause is
    # read as well.
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, OSError) and workdir is not None and cause.filename == workdir:
            return orch.START_FAILURE_REASON_WORKING_DIRECTORY_UNAVAILABLE
        cause = cause.__cause__
    return orch.START_FAILURE_REASON_OTHER


class SteerRefusedError(Exception):
    """The provider could not take a steer now (no turn is running, or it cannot steer)."""


class Driver(Protocol):
    """A provider session, as the engine drives it.

    A turn is named: ``begin_turn`` announces it, ``run_turn`` runs it, and ``steer`` and
    ``interrupt`` act on a turn by id and only on that turn. One that names a turn that has not
    begun, or has ended, does nothing (``interrupt``) or raises ``SteerRefusedError`` (``steer``),
    so a late command never reaches the turn after it. ``begin_turn`` never blocks. ``steer`` and
    ``interrupt`` may be called from another thread while ``run_turn`` runs.
    """

    def open(self) -> Opened:
        """Start (or resume) the provider session and say what it can do.

        Returns only once the agent is up with the tools it was given. Raises ``StartError``,
        with the reason, when it cannot be, and leaves no process behind.
        """
        ...

    def begin_turn(self, turn_id: str, prompt: str) -> None:
        """Announce the next turn, with the words it sends, and route commands to it."""
        ...

    def run_turn(self, emit: Callable[[AgentEvent], None]) -> TurnResult:
        """Run the begun turn to its end, sending each event to *emit*.

        An interrupt that arrived after ``begin_turn`` ends the turn, even if it arrived before the
        provider had started it. Never raises for a provider failure.
        """
        ...

    def steer(self, turn_id: str, text: str) -> None:
        """Send *text* to turn *turn_id* if it is the one running, or raise ``SteerRefusedError``."""
        ...

    def interrupt(self, turn_id: str) -> None:
        """End turn *turn_id* and keep the conversation; nothing if it is not the current turn."""
        ...

    def poll_idle(self, emit: Callable[[AgentEvent], None]) -> bool:
        """Report what the provider did outside a turn. ``False`` once its process is gone."""
        ...

    def close(self) -> None:
        """End the session and its process."""
        ...


@dataclass
class _Active:
    """The turn the driver is on: announced, then (once the session reserved it) ticketed."""

    turn_id: str
    prompt: str
    ticket: TurnTicket | None = None
    interrupt_requested: bool = False


class _Forward:
    """An agentshim event handler that calls a function."""

    def __init__(self, emit: Callable[[AgentEvent], None]) -> None:
        """Forward events to *emit*."""
        self._emit = emit

    def on_event(self, event: AgentEvent) -> None:
        self._emit(event)


class AgentshimDriver:
    """A ``Driver`` over an agentshim ``Session``.

    *factory* builds the session, given the conversation id to choose for it (or ``None``).
    ``open`` first asks for *preferred_id*, so the id is known before the first turn on a provider
    that lets the caller choose (Claude); a provider that names its own conversations (Codex)
    refuses with ``ProviderCapabilityError`` and the session is rebuilt without it.
    """

    def __init__(
        self,
        factory: Callable[[str | None], Session],
        preferred_id: str | None = None,
        workdir: str | None = None,
    ) -> None:
        """Drive sessions built by *factory*, asking first for *preferred_id*.

        *workdir* is the agent's working directory, which a start error is held against to tell
        a directory that cannot be entered from any other failure.
        """
        self._factory = factory
        self._preferred_id = preferred_id
        self._workdir = workdir
        self._session: Session | None = None
        # Guards ``_active`` only, and is never held while calling into the session, so it cannot
        # wait on a turn.
        self._lock = threading.Lock()
        self._active: _Active | None = None

    def open(self) -> Opened:
        """See ``Driver.open``. Opens the conversation now so its id is known before a turn."""
        try:
            session, conversation_id = self._warm()
        except (AgentShimError, OSError, ValueError) as error:
            reason = classify_start_error(error, self._workdir)
            raise StartError(reason, f"{type(error).__name__}: {error}") from error
        try:
            self._require_tools(session)
        except BaseException:
            session.close()
            raise
        self._session = session
        profile = session.profile
        capabilities = orch.Capabilities(
            streams_text=True, steers=profile.supports_steer, resumes=profile.supports_resume
        )
        return Opened(conversation_id or "", capabilities)

    def _warm(self) -> tuple[Session, str | None]:
        """Build the provider session and open its conversation."""
        session = self._factory(self._preferred_id)
        try:
            return session, session.warm()
        except ProviderCapabilityError:
            session.close()
            if self._preferred_id is None:
                raise
            session = self._factory(None)
            try:
                return session, session.warm()
            except BaseException:
                session.close()
                raise
        except BaseException:
            session.close()
            raise

    @staticmethod
    def _require_tools(session: Session) -> None:
        """Raise ``StartError`` if the provider says a tool server of the session failed.

        A provider that cannot say, a server it has not settled and a state it did not name are
        unknown, and unknown does not fail a session: only what the provider reported does.
        """
        failed = [
            status
            for status in session.report_tools(TOOLS_WAIT_SECONDS) or ()
            if status.state is McpServerState.FAILED
        ]
        if failed:
            detail = "; ".join(
                f"tool server {status.name} failed: {status.error or 'no reason given'}"
                for status in failed
            )
            raise StartError(orch.START_FAILURE_REASON_TOOLS_UNAVAILABLE, detail)

    @property
    def _live(self) -> Session:
        if self._session is None:
            msg = "the driver is not open"
            raise SessionStateError(msg)
        return self._session

    def begin_turn(self, turn_id: str, prompt: str) -> None:
        """See ``Driver.begin_turn``."""
        with self._lock:
            self._active = _Active(turn_id, prompt)

    def run_turn(self, emit: Callable[[AgentEvent], None]) -> TurnResult:
        """See ``Driver.run_turn``."""
        with self._lock:
            active = self._active
        if active is None:
            msg = "no turn has begun"
            raise SessionStateError(msg)
        try:
            # Pinned: a managed agent's conversation holds its brief, so agentshim must never
            # retire it (Codex's two-turn renewal) or replace a refused resume with an empty one.
            ticket = self._live.prepare_turn(TurnRequest(prompt=active.prompt), pin=True)
            with self._lock:
                active.ticket = ticket
                early = active.interrupt_requested
            if early:
                self._live.interrupt(ticket)
            turn = self._live.run(ticket, on_event=_Forward(emit))
        except TurnCancelledError:
            return TurnResult(orch.TURN_STATUS_INTERRUPTED)
        except AgentShimError as error:
            return TurnResult(orch.TURN_STATUS_FAILED, detail=f"{type(error).__name__}: {error}")
        finally:
            with self._lock:
                self._active = None
        status = (
            orch.TURN_STATUS_INTERRUPTED if turn.result.interrupted else orch.TURN_STATUS_COMPLETED
        )
        return TurnResult(status, turn.result.usage)

    def _ticket_of(self, turn_id: str) -> TurnTicket | None:
        """The ticket of *turn_id* if it is the current turn and has one."""
        with self._lock:
            active = self._active
            if active is None or active.turn_id != turn_id:
                return None
            return active.ticket

    def steer(self, turn_id: str, text: str) -> None:
        """See ``Driver.steer``."""
        ticket = self._ticket_of(turn_id)
        if ticket is None:
            msg = f"turn {turn_id} is not running"
            raise SteerRefusedError(msg)
        try:
            self._live.steer(text, ticket)
        except (NoRunningTurnError, ProviderCapabilityError, SessionStateError) as error:
            raise SteerRefusedError(str(error)) from error

    def interrupt(self, turn_id: str) -> None:
        """See ``Driver.interrupt``."""
        with self._lock:
            active = self._active
            if active is None or active.turn_id != turn_id:
                return
            active.interrupt_requested = True
            ticket = active.ticket
        if ticket is not None:
            self._live.interrupt(ticket)

    def poll_idle(self, emit: Callable[[AgentEvent], None]) -> bool:
        """See ``Driver.poll_idle``."""
        return self._live.poll_idle(_Forward(emit))

    def close(self) -> None:
        """See ``Driver.close``."""
        self._live.close()


def over(environment: sc.AgentEnvironment, base: Mapping[str, str]) -> dict[str, str]:
    """Return *base* with *environment* laid over it. Its names mean nothing here."""
    merged = dict(base)
    for name in environment.unset:
        merged.pop(name, None)
    merged.update(environment.set)
    return merged


class AgentFactory(Protocol):
    """Makes the agentshim ``Agent`` of one session, given the provider and the session's environment."""

    def __call__(self, provider: str, environment: sc.AgentEnvironment) -> Agent:
        """Return an ``Agent`` for *provider* whose processes get *environment* over their base one."""
        ...


def session_plugins(request: sc.OpenSessionRequest) -> SessionPlugins:
    """The plugins an opened session loads: exactly the request's, and none of the operator's.

    Passing them isolates the session from the operator's plugins, skills, settings and memory on
    both harnesses, and loads the workspace's own project configuration. Whether the bundle can be
    loaded is agentshim's to find out, and comes back as the session's start failure.
    """
    return SessionPlugins(
        home=Path(request.agent_data_directory),
        refs=(PluginRef(marketplace=Path(request.skills.directory), name=request.skills.name),),
    )


def tool_servers(request: sc.OpenSessionRequest) -> tuple[StdioMcpServer, ...]:
    """The tool servers *request* gives the agent, each with exactly the environment it names."""
    return tuple(
        StdioMcpServer(
            name=tool.name, command=tool.argv[0], args=tuple(tool.argv[1:]), env=dict(tool.env)
        )
        for tool in request.tools
    )


def build_session(
    request: sc.OpenSessionRequest,
    *,
    agent: AgentFactory | None = None,
    raw_sink: Callable[[RawFrame], None] | None = None,
    new_conversation_id: str | None = None,
) -> Session:
    """The agentshim session an ``OpenSessionRequest`` describes.

    Native subagents are off, because the orchestrator's own tools are how a lead spawns agents,
    and text streams. The agent sees only the tool servers and skills given here.

    The session's agent is made here, once per session, with the request's environment laid over
    the login shell's. That is the only way it reaches the agent's shell: agentshim builds a
    provider's environment from that probe and never from this process's, so anything set only on
    this process would not arrive. *agent* makes the ``Agent`` from the provider and the environment
    (tests pass one over scripted providers, with a fixed base environment); by default the real
    provider for the harness.
    """
    name = PROVIDERS.get(request.harness)
    if name is None:
        msg = f"no provider for harness {orch.Harness.Name(request.harness)}"
        raise ValueError(msg)
    make_agent = _real_agent(request) if agent is None else agent
    return make_agent(name, request.environment).session(
        request.working_directory,
        mcp_servers=tool_servers(request),
        # Only the orchestrator's server: never the operator's own MCP servers or account
        # connectors (mail, drives, calendars), which a launched agent has no business reaching.
        mcp_scope=McpScope.SESSION,
        plugins=session_plugins(request),
        reasoning_effort=request.reasoning_effort or None,
        resume_id=request.resume_token.decode() or None,
        partial_text=True,
        disable_subagents=True,
        # The host starts every turn: a wake-up an agent scheduled would never fire.
        disable_self_scheduling=True,
        raw_sink=raw_sink,
        new_conversation_id=new_conversation_id,
    )


def _real_agent(request: sc.OpenSessionRequest) -> AgentFactory:
    """The factory of real provider agents, on the request's model."""

    def make(provider: str, environment: sc.AgentEnvironment) -> Agent:
        return Agent(
            provider,
            transport=TransportKind.STREAM,
            model=request.model or None,
            env=over(environment, interactive_env()),
            permissions=NativePermissions.bypass(),
            approvals=ApprovalPolicy.DENY,
        )

    return make
