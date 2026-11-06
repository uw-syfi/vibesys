"""The sidecar's session logic, with no socket and no thread of its own.

``Engine`` carries out ``SessionCommand``s, folds what the provider does into numbered events and
keeps them in a ``Store``. The shell (``server.py``, ``main.py``) calls ``send`` from its RPC threads
and ``run_next`` and ``poll_idle`` from one worker thread; tests call all three in a fixed order.

Decisions:

- An effect id is remembered before the command runs, so a repeat is dropped even after a restart.
  A command that crashed the sidecar half-way is not run again; the restart reports the session
  failed and the daemon launches a new one that resumes the conversation.
- A restarted sidecar owns no provider process, so it ends every open turn as FAILED, reports
  ``SessionFailed`` and refuses further work except ``Close``.
- Activity between turns is filed under the last turn's id. One normalizer folds a whole stretch of
  idle activity, so an item that spans polls keeps its arguments; items still running when the idle
  stretch ends (a turn begins, the session fails or closes) are settled as failed. Idle activity
  reports no usage: the provider gives a total only for a turn's result.
- An interrupt or a steer names its turn. The engine checks the name under its lock, the driver
  checks it again, and an interrupt that arrives before the provider has started the turn is kept
  and applied when it starts, so neither is lost nor lands on the turn after.
- A failed write to the store is fatal: it propagates, the shell ends the process, and a restart
  reports the session lost.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import TYPE_CHECKING

import protovalidate
from agentshim import AgentShimError

from vs_sidecar.driver import StartError, SteerRefusedError
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.gen.vibesys.orchestrator.v1 import effects_pb2 as fx
from vs_sidecar.gen.vibesys.orchestrator.v1 import orchestrator_pb2 as orch
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


class InvalidEventError(RuntimeError):
    """The engine built an event that breaks the event contract: a bug, never kept."""


class Engine:
    """One agent session."""

    def __init__(
        self,
        session: fx.SessionRef,
        store: Store,
        driver_factory: Callable[[Callable[[RawFrame], None]], Driver],
        notify: Callable[[], None] = lambda: None,
    ) -> None:
        """Hold the session *session*.

        *driver_factory* receives the sink for raw provider frames and returns the driver; *notify*
        is called after each event is kept, to wake whoever follows the log.
        """
        self._session = session
        self._store = store
        self._driver_factory = driver_factory
        self._notify = notify
        self._lock = threading.RLock()
        self._driver: Driver | None = None
        self._pending: deque[tuple[str, str, orch.Message]] = deque()
        self._turn: TurnNormalizer | None = None
        self._idle: TurnNormalizer | None = None
        self._idle_runs = 0
        self._steers = SteerBook()
        self._last_turn = IDLE_TURN
        self._closing = False
        self._failed = False
        self._closed = False
        self._store_failure: OSError | None = None
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
        except StartError as failure:
            with self._lock:
                self._fail(f"could not start the agent: {failure.detail}", failure.reason)
            return
        except (AgentShimError, OSError, ValueError) as error:
            with self._lock:
                self._fail(
                    f"could not start the agent: {type(error).__name__}: {error}",
                    orch.START_FAILURE_REASON_OTHER,
                )
            return
        with self._lock:
            if opened.capabilities.resumes and not opened.conversation_id:
                self._fail(
                    "could not start the agent: the provider did not name the conversation",
                    orch.START_FAILURE_REASON_OTHER,
                )
                driver.close()
                return
            self._driver = driver
            token = opened.conversation_id.encode() if opened.capabilities.resumes else b""
            self._append(
                sc.SessionEvent(
                    opened=sc.SessionOpened(resume_token=token, capabilities=opened.capabilities)
                )
            )

    def _resume_history(self, history: list[sc.SessionEvent]) -> None:
        kinds = [event.WhichOneof("body") for event in history]
        if "closed" in kinds:
            self._closed = True
            return
        last_item: dict[str, sc.SessionItem] = {}
        for event in history:
            if event.HasField("item"):
                last_item[event.item.id] = event.item
        started = {item.turn_id for item in last_item.values()}
        started |= {event.usage.turn_id for event in history if event.HasField("usage")}
        ended = {event.turn_ended.turn_id for event in history if event.HasField("turn_ended")}
        # Nothing will report on a running item again: settle every one, each open turn's before
        # that turn's end.
        for turn_id in [*sorted(started - ended - {IDLE_TURN}), None]:
            for item in last_item.values():
                if item.status == orch.ITEM_STATUS_RUNNING and (
                    turn_id is None or item.turn_id == turn_id
                ):
                    lost = sc.SessionItem()
                    lost.CopyFrom(item)
                    lost.status = orch.ITEM_STATUS_FAILED
                    last_item[item.id] = lost
                    self._append(sc.SessionEvent(item=lost))
            if turn_id is not None:
                self._append(
                    sc.SessionEvent(
                        turn_ended=sc.TurnEnded(turn_id=turn_id, status=orch.TURN_STATUS_FAILED)
                    )
                )
        if "failed" not in kinds:
            self._fail("the agent session restarted and lost the agent process")
        self._failed = True

    def _fail(
        self, detail: str, start_failure: int = orch.START_FAILURE_REASON_UNSPECIFIED
    ) -> None:
        """End the session as failed. *start_failure* is why it never opened, when it did not."""
        self._failed = True
        self._end_idle(orch.TURN_STATUS_FAILED)
        self._append(
            sc.SessionEvent(
                failed=sc.SessionFailed(detail=detail, start_failure_reason=start_failure)
            )
        )

    # -- reading

    def events_after(self, offset: int) -> list[sc.SessionEvent]:
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

    def send(self, command: sc.SessionCommand) -> None:
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
        if (ref.agent_id, ref.session) != (self._session.agent_id, self._session.session):
            msg = f"command is for session {ref.agent_id}/{ref.session}, this is {self._session.agent_id}/{self._session.session}"
            raise InvalidCommandError(msg)
        try:
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
        except OSError as error:
            # A store failure is fatal wherever it happens: the worker raises it (``check``), so
            # the process closes the agent and exits, and a restart reports the session lost.
            with self._lock:
                self._store_failure = self._store_failure or error
            self._notify()
            raise

    def check(self) -> None:
        """Raise the store failure a command met on another thread, if there was one."""
        with self._lock:
            failure = self._store_failure
        if failure is not None:
            raise failure

    def _start_turn(self, start: fx.StartTurn) -> None:
        prompt = render(start.message)
        with self._lock:
            if self._unusable:
                return
            self._pending.append((start.turn_id, prompt, start.message))
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
                    sc.SessionEvent(
                        steer=sc.SteerOutcome(message_id=steer.message.id, accepted=False)
                    )
                )
        if not running or driver is None:
            return
        try:
            driver.steer(steer.turn_id, text, message=steer.message)
        except SteerRefusedError:
            with self._lock:
                if self._steers.take(text) is not None:
                    self._append(
                        sc.SessionEvent(
                            steer=sc.SteerOutcome(message_id=steer.message.id, accepted=False)
                        )
                    )

    def _cancel_pending(self, matches: Callable[[str], bool]) -> None:
        """End the queued turns *matches* picks as interrupted, so none is left without an end.

        Call with the lock held.
        """
        kept: deque[tuple[str, str, orch.Message]] = deque()
        for turn_id, prompt, message in self._pending:
            if matches(turn_id):
                self._append(
                    sc.SessionEvent(
                        turn_ended=sc.TurnEnded(
                            turn_id=turn_id, status=orch.TURN_STATUS_INTERRUPTED
                        )
                    )
                )
            else:
                kept.append((turn_id, prompt, message))
        self._pending = kept

    def _interrupt(self, interrupt: fx.Interrupt) -> None:
        with self._lock:
            driver = self._driver
            running = self._turn is not None and self._turn.turn_id == interrupt.turn_id
            self._cancel_pending(lambda queued: queued == interrupt.turn_id)
        if running and driver is not None:
            driver.interrupt(interrupt.turn_id)

    def _close(self, close: fx.CloseSession) -> None:
        with self._lock:
            self._closing = True
            self._cancel_pending(lambda _queued: True)
            driver = self._driver
            turn = self._turn
        if driver is not None and turn is not None:
            driver.interrupt(turn.turn_id)
            if close.force:
                driver.close()
        if turn is None:
            self._finish_close()

    def shutdown(self) -> None:
        """End the session as ``Close(force)`` does: interrupt the turn, stop the agent, close."""
        self._close(fx.CloseSession(force=True))

    def release(self) -> None:
        """Stop the provider process and write nothing: for a process that is ending."""
        with self._lock:
            driver = self._driver
        if driver is not None:
            driver.close()

    def _finish_close(self) -> None:
        with self._lock:
            if self._closed:
                return
            driver = self._driver
        if driver is not None:
            driver.close()
        with self._lock:
            self._end_idle(orch.TURN_STATUS_FAILED)
            self._append(sc.SessionEvent(closed=sc.SessionClosed()))
            # Only once the close is on disk: a failed write leaves the session open, so the
            # daemon's retried close is not dropped as already done.
            self._closed = True

    # -- the worker

    def run_next(self) -> bool:
        """Run the oldest queued turn to its end. Returns whether there was one."""
        with self._lock:
            driver = self._driver
            if not self._pending or self._unusable or driver is None:
                return False
            turn_id, prompt, message = self._pending.popleft()
            self._end_idle(orch.TURN_STATUS_FAILED)
            normalizer = TurnNormalizer(turn_id, self._steers)
            # The driver learns the turn before anyone can address it (``_turn`` is what
            # ``_interrupt`` and ``_steer`` check), so an interrupt is never sent to a driver that
            # has not heard of the turn, and the driver keeps one that comes before the provider
            # starts it.
            driver.begin_turn(turn_id, prompt, message=message)
            self._turn = normalizer

        def emit(event: AgentEvent) -> None:
            with self._lock:
                for out in normalizer.feed(event):
                    self._append(out)

        result = driver.run_turn(emit)
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
            if self._idle is None:
                self._idle_runs += 1
                self._idle = TurnNormalizer(
                    self._last_turn, self._steers, f"{self._last_turn}:idle{self._idle_runs}"
                )
            normalizer = self._idle

        def emit(event: AgentEvent) -> None:
            with self._lock:
                for out in normalizer.feed(event):
                    self._append(out)

        alive = driver.poll_idle(emit)
        if not alive:
            with self._lock:
                if not self._unusable:
                    self._fail("the agent session ended: its process is gone")
        return alive

    def _end_idle(self, status: int) -> None:
        """Settle the items of the idle stretch that is over. Call with the lock held."""
        if self._idle is not None:
            for out in self._idle.settle(status):
                self._append(out)
            self._idle = None

    # -- log

    def _append(self, event: sc.SessionEvent) -> None:
        # The offset is the store's to give, so a copy with a placeholder is what is checked.
        probe = sc.SessionEvent()
        probe.CopyFrom(event)
        probe.offset = 1
        try:
            self._validator.validate(probe, fail_fast=True)
        except protovalidate.ValidationError as error:
            msg = f"the sidecar made an event that breaks its contract: {error}"
            raise InvalidEventError(msg) from error
        self._store.append(event)
        self._notify()

    def _on_raw(self, frame: RawFrame) -> None:
        # No engine lock: the store is thread-safe and a raw frame must not wait for, or make
        # wait, anything that reads the event log.
        self._store.append_raw(FrameDirection(frame.direction.value), frame.text)
