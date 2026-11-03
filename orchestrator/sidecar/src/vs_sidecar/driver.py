"""The one place provider knowledge meets the sidecar: a ``Driver`` runs a session through agentshim.

Everything the engine needs from a provider is in the ``Driver`` protocol. ``AgentshimDriver`` is
the implementation over an ``agentshim.Session``; the harness to provider mapping lives here and
nowhere else. Tests give it an ``Agent`` built on agentshim's scripted providers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from agentshim import (
    Agent,
    AgentShimError,
    ApprovalPolicy,
    McpScope,
    NativePermissions,
    NoRunningTurnError,
    PluginRef,
    ProviderCapabilityError,
    ProviderUsage,
    SessionPlugins,
    SessionStateError,
    StdioMcpServer,
    TransportKind,
    TurnCancelledError,
    TurnRequest,
)

from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from agentshim import AgentEvent, RawFrame, Session, TurnTicket

    from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx

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
        """Start (or resume) the provider session and say what it can do."""
        ...

    def begin_turn(self, turn_id: str, prompt: str) -> None:
        """Announce the next turn, so a steer or an interrupt addressed to it is routed to it."""
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
        self, factory: Callable[[str | None], Session], preferred_id: str | None = None
    ) -> None:
        """Drive sessions built by *factory*, asking first for *preferred_id*."""
        self._factory = factory
        self._preferred_id = preferred_id
        self._session: Session | None = None
        # Guards ``_active`` only, and is never held while calling into the session, so it cannot
        # wait on a turn.
        self._lock = threading.Lock()
        self._active: _Active | None = None

    def open(self) -> Opened:
        """See ``Driver.open``. Opens the conversation now so its id is known before a turn."""
        session = self._factory(self._preferred_id)
        try:
            conversation_id = session.warm()
        except ProviderCapabilityError:
            if self._preferred_id is None:
                raise
            session.close()
            session = self._factory(None)
            conversation_id = session.warm()
        self._session = session
        profile = session.profile
        capabilities = orch.Capabilities(
            streams_text=True, steers=profile.supports_steer, resumes=profile.supports_resume
        )
        return Opened(conversation_id or "", capabilities)

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
            ticket = self._live.prepare_turn(TurnRequest(prompt=active.prompt))
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


#: The environment every MCP server of a session gets, so the server knows who is calling.
IDENTITY_ENV = ("VIBESYS_AGENT_ID", "VIBESYS_RUN_ID", "VIBESYS_SESSION")


def identity_env(launch: fx.LaunchSession) -> dict[str, str]:
    """The session identity as environment variables, keyed by ``IDENTITY_ENV``."""
    return {
        "VIBESYS_AGENT_ID": launch.session.agent_id,
        "VIBESYS_RUN_ID": launch.run_id,
        "VIBESYS_SESSION": str(launch.session.session),
    }


def session_plugins(launch: fx.LaunchSession) -> SessionPlugins:
    """The plugins a launched session loads: exactly the launch's, and none of the operator's.

    Passing them isolates the session from the operator's plugins, skills, settings and memory on
    both harnesses, and loads the workspace's own project configuration. The launch must name a
    plugin: there is no default, so a launch without one is refused.
    """
    if not launch.HasField("plugin"):
        msg = "launch.plugin is missing: an agent session always loads the vibesys plugin"
        raise ValueError(msg)
    plugin = launch.plugin
    return SessionPlugins(
        home=Path(plugin.home),
        refs=(PluginRef(marketplace=Path(plugin.marketplace), name=plugin.name),),
    )


def build_session(
    launch: fx.LaunchSession,
    *,
    agent: Agent | None = None,
    mcp_servers: Sequence[StdioMcpServer] = (),
    raw_sink: Callable[[RawFrame], None] | None = None,
    new_conversation_id: str | None = None,
) -> Session:
    """The agentshim session a ``LaunchSession`` describes.

    Native subagents are off, because the orchestrator's own tools are how a lead spawns agents,
    and text streams. The agent sees only the MCP servers given here and the launch's plugin
    (``session_plugins``). *agent* is the one to open it on (tests pass one over scripted
    providers); by default the real provider for the harness.
    """
    if agent is None:
        name = PROVIDERS.get(launch.harness)
        if name is None:
            msg = f"no provider for harness {orch.Harness.Name(launch.harness)}"
            raise ValueError(msg)
        agent = Agent(
            name,
            transport=TransportKind.STREAM,
            model=launch.model or None,
            permissions=NativePermissions.bypass(),
            approvals=ApprovalPolicy.DENY,
        )
    # The identity wins over anything the server config says, so a server cannot be told it is
    # another agent.
    identified = tuple(
        replace(server, env={**server.env, **identity_env(launch)}) for server in mcp_servers
    )
    return agent.session(
        launch.worktree,
        mcp_servers=identified,
        # Only the orchestrator's server: never the operator's own MCP servers or account
        # connectors (mail, drives, calendars), which a launched agent has no business reaching.
        mcp_scope=McpScope.SESSION,
        plugins=session_plugins(launch),
        reasoning_effort=launch.reasoning_effort or None,
        resume_id=launch.resume_conversation_id or None,
        partial_text=True,
        disable_subagents=True,
        raw_sink=raw_sink,
        new_conversation_id=new_conversation_id,
    )
