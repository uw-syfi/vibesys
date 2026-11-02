"""The sidecar's session logic, with no socket and no thread of its own.

``Engine`` carries out ``SidecarCommand``s, folds what the provider does into numbered events and
keeps them in a ``Store``. The shell (``server.py``, ``main.py``) calls ``send`` from its RPC threads
and ``run_next`` and ``poll_idle`` from one worker thread; tests call all three in a fixed order.

Decisions:

- An effect id is remembered before the command runs, so a repeat is dropped even after a restart.
  A command that crashed the sidecar half-way is not run again; the restart reports the session
  failed and the daemon launches a new one that resumes the conversation.
- A restarted sidecar owns no provider process, so it ends every open turn as FAILED, reports
  ``SessionFailed`` and refuses further work except ``Close``.
- Activity between turns is filed under the last turn's id.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING

import protovalidate
from agentshim import AgentShimError

from vs_sidecar.driver import SteerRefusedError
from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
from vs_sidecar.gen.vibesys.orchestrator.v1 import sidecar_pb2 as sc
from vs_sidecar.normalize import SteerBook, TurnNormalizer
from vs_sidecar.prompts import render
from vs_sidecar.store import FrameDirection

if TYPE_CHECKING:
    from collections.abc import Callable

    from agentshim import AgentEvent, RawFrame

    from vs_sidecar.driver import Driver
    from vs_sidecar.store import Store

#: Turn id given to activity that happens before any turn.
IDLE_TURN = "idle"


class InvalidCommandError(ValueError):
    """A command that breaks its contract, or names another session."""


class Engine:
    """One agent session."""

    def __init__(
        self,
        launch: fx.LaunchSession,
        store: Store,
        driver_factory: Callable[[Callable[[RawFrame], None]], Driver],
        notify: Callable[[], None] = lambda: None,
    ) -> None:
        """Hold the session *launch* describes.

        *driver_factory* receives the sink for raw provider frames and returns the driver; *notify*
        is called after each event is kept, to wake whoever follows the log.
        """
        self._launch = launch
        self._store = store
        self._driver_factory = driver_factory
        self._notify = notify
        self._lock = threading.RLock()
        self._driver: Driver | None = None
        self._pending: deque[tuple[str, str]] = deque()
        self._turn: TurnNormalizer | None = None
        self._steers = SteerBook()
        self._last_turn = IDLE_TURN
        self._closing = False
        self._failed = False
        self._closed = False
        self._validator = protovalidate.Validator()

    # -- start

    def start(self) -> None:
        """Open the session, or, if this store has a history, report that it was lost."""
        with self._lock:
            history = self._store.events_after(0)
            if history:
                self._resume_history(history)
                return
        driver = self._driver_factory(self._on_raw)
        try:
            opened = driver.open()
        except (AgentShimError, OSError, ValueError) as error:
            with self._lock:
                self._fail(f"could not open the session: {type(error).__name__}: {error}")
            return
        with self._lock:
            if not opened.conversation_id:
                self._fail("the provider did not name the conversation")
                return
            self._driver = driver
            self._append(
                sc.SidecarEvent(
                    opened=sc.ConversationOpened(
                        provider_conversation_id=opened.conversation_id,
                        capabilities=opened.capabilities,
                        worktree=self._launch.worktree,
                    )
                )
            )

    def _resume_history(self, history: list[sc.SidecarEvent]) -> None:
        kinds = [event.WhichOneof("body") for event in history]
        if "closed" in kinds:
            self._closed = True
            return
        started = {event.item.turn_id for event in history if event.HasField("item")}
        started |= {event.usage.turn_id for event in history if event.HasField("usage")}
        ended = {event.turn_ended.turn_id for event in history if event.HasField("turn_ended")}
        for turn_id in sorted(started - ended - {IDLE_TURN}):
            self._append(
                sc.SidecarEvent(
                    turn_ended=sc.TurnEnded(turn_id=turn_id, status=orch.TURN_STATUS_FAILED)
                )
            )
        if "failed" not in kinds:
            self._fail("the sidecar restarted and lost the agent process")
        self._failed = True

    def _fail(self, detail: str) -> None:
        self._failed = True
        self._append(sc.SidecarEvent(failed=sc.SessionFailed(detail=detail)))

    # -- reading

    def events_after(self, offset: int) -> list[sc.SidecarEvent]:
        """The kept events after *offset*."""
        with self._lock:
            return self._store.events_after(offset)

    @property
    def finished(self) -> bool:
        """Whether the session is over: closed, or failed with nothing more to say."""
        with self._lock:
            return self._closed

    @property
    def has_work(self) -> bool:
        """Whether ``run_next`` has a turn to run."""
        with self._lock:
            return bool(self._pending) and not self._unusable

    @property
    def _unusable(self) -> bool:
        return self._failed or self._closed or self._closing

    # -- commands

    def send(self, command: sc.SidecarCommand) -> None:
        """Carry out *command*. A repeated effect id changes nothing.

        Raises ``InvalidCommandError`` for a command that breaks its contract or addresses another
        session. ``StartTurn`` only queues the turn; ``run_next`` runs it.
        """
        try:
            self._validator.validate(command, fail_fast=True)
        except protovalidate.ValidationError as error:
            raise InvalidCommandError(str(error)) from error
        body = command.WhichOneof("body")
        ref = getattr(command, body).session
        if (ref.agent_id, ref.session) != (
            self._launch.session.agent_id,
            self._launch.session.session,
        ):
            msg = f"command is for session {ref.agent_id}/{ref.session}, this is {self._launch.session.agent_id}/{self._launch.session.session}"
            raise InvalidCommandError(msg)
        with self._lock:
            if not self._store.remember_effect(command.effect_id):
                return
        if body == "start_turn":
            self._start_turn(command.start_turn)
        elif body == "steer":
            self._steer(command.steer)
        elif body == "interrupt":
            self._interrupt(command.interrupt)
        else:
            self._close(command.close)

    def _start_turn(self, start: fx.StartTurn) -> None:
        prompt = render(start.message)
        with self._lock:
            if self._unusable:
                return
            self._pending.append((start.turn_id, prompt))
        self._notify()

    def _steer(self, steer: fx.Steer) -> None:
        text = render(steer.message)
        with self._lock:
            driver = self._driver
            running = self._turn is not None and self._turn.turn_id == steer.turn_id
            if running:
                self._steers.add(steer.message.id, text)
            elif not self._closed:
                self._append(
                    sc.SidecarEvent(
                        steer=sc.SteerOutcome(message_id=steer.message.id, accepted=False)
                    )
                )
        if not running or driver is None:
            return
        try:
            driver.steer(text)
        except SteerRefusedError:
            # Left in the book: it is answered as not accepted when the turn ends.
            return

    def _interrupt(self, interrupt: fx.Interrupt) -> None:
        with self._lock:
            driver = self._driver
            running = self._turn is not None and self._turn.turn_id == interrupt.turn_id
            self._pending = deque(p for p in self._pending if p[0] != interrupt.turn_id)
        if running and driver is not None:
            driver.interrupt()

    def _close(self, close: fx.CloseSession) -> None:
        with self._lock:
            self._closing = True
            self._pending.clear()
            driver = self._driver
            running = self._turn is not None
        if driver is not None and running:
            driver.interrupt()
            if close.force:
                driver.close()
        if not running:
            self._finish_close()

    def _finish_close(self) -> None:
        with self._lock:
            if self._closed:
                return
            driver = self._driver
        if driver is not None:
            driver.close()
        with self._lock:
            self._closed = True
            self._append(sc.SidecarEvent(closed=sc.SessionClosed()))

    # -- the worker

    def run_next(self) -> bool:
        """Run the oldest queued turn to its end. Returns whether there was one."""
        with self._lock:
            driver = self._driver
            if not self._pending or self._unusable or driver is None:
                return False
            turn_id, prompt = self._pending.popleft()
            normalizer = self._turn = TurnNormalizer(turn_id, self._steers)

        def emit(event: AgentEvent) -> None:
            with self._lock:
                for out in normalizer.feed(event):
                    self._append(out)

        result = driver.run_turn(prompt, emit)
        with self._lock:
            for out in normalizer.finish(result.status, result.usage):
                self._append(out)
            self._turn = None
            self._last_turn = turn_id
            closing = self._closing
        if closing:
            self._finish_close()
        return True

    def poll_idle(self) -> bool:
        """Report what the provider did between turns. Returns whether the session still lives."""
        with self._lock:
            driver = self._driver
            if driver is None or self._unusable or self._turn is not None:
                return not self._unusable
            normalizer = TurnNormalizer(self._last_turn, self._steers)

        def emit(event: AgentEvent) -> None:
            with self._lock:
                for out in normalizer.feed(event):
                    self._append(out)

        alive = driver.poll_idle(emit)
        if not alive:
            with self._lock:
                if not self._unusable:
                    self._fail("the agent process ended")
        return alive

    # -- log

    def _append(self, event: sc.SidecarEvent) -> None:
        self._store.append(event)
        self._notify()

    def _on_raw(self, frame: RawFrame) -> None:
        with self._lock:
            self._store.append_raw(FrameDirection(frame.direction.value), frame.text)
