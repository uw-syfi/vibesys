"""The run: every live flow of one search, driven by hand."""

from __future__ import annotations

from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter, ValidationError

from vs_core._registry import ContractError
from vs_core.types.common import THIS_RUN, AttemptRef, DecisionId, InvocationRef, RunFacts, Seconds
from vs_core.types.kinds import EventKind
from vs_core.types.records import OperationResult
from vs_core.types.search import (
    AdoptionFinished,
    CheckpointKnown,
    Event,
    OperationFinished,
    ResumeReady,
    StopRequested,
    TimeReached,
    TurnYielded,
    WorkspaceClosed,
    YieldFailed,
)
from vs_core.types.strategy import (
    Decision,
    Finish,
    ProposeWinner,
    Settle,
    Wait,
    WakeAt,
    Withdraw,
    Work,
)
from vs_core.types.until import Within

from ._defs import FlowDef, _adapter, _Task, definition_of
from ._errors import Deadlock, FlowCodeChanged, Stopped, TaskStopped, TimeUp
from ._flow import Flow
from ._sources import First, Pending, Signal, Task, Timer
from ._state import Dropped, Issued, LiveFlow, LiveFlows, Recorded, RecordedOperation

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from vs_core._ids import IdScheme
    from vs_core._registry import OperationRegistration, OperationRegistry
    from vs_core._strategy_operations import StrategyOperations
    from vs_core.types.agents import Checkpoint
    from vs_core.types.search import RunView
    from vs_core.types.settlement import Selection

    from ._flow import Group
    from ._roles import Role


@dataclass(frozen=True)
class Wiring:
    """What the host and the registration lend a run: its codec and its namespace's shapes."""

    registry: OperationRegistry | None = None
    operations: StrategyOperations | None = None
    ids: IdScheme | None = None
    owned: tuple[OperationRegistration, ...] = ()
    """The operations the search registered for other libraries' owners to perform."""
    roles: Mapping[str, Role] = field(default_factory=dict)
    """The roles the search declared, by name (with the models the operator chose)."""


class _Run:
    """The live flows of one run, driven by hand."""

    def __init__(
        self,
        root: FlowDef,
        config: object,
        facts: RunFacts,
        wiring: Wiring,
    ) -> None:
        self.facts = facts
        self.wiring = wiring
        self.stop: StopRequested | None = None
        self.closed = False
        self.published: str | None = None
        self.choice: Selection | None = None
        self.out: list[Decision] = []
        self.dropped: list[Dropped] = []
        self.checkpoints: dict[InvocationRef, Checkpoint] = {}
        self._root = root
        self._config = config
        self._tasks: list[_Task] = [_Task(path="")]  # live flows, the root first
        self._pendings: dict[DecisionId, Pending] = {}
        self._signals: list[Signal] = []
        self._timers: list[Timer] = []
        self._restorable: dict[str, LiveFlow] = {}
        self._issued: dict[tuple[str, str], int] = {}
        self._view: RunView | None = None
        self._recorded: deque[str] | None = None
        self._reads: list[str] = []

    # -- one step: init, restore or one event ------------------------------------------------

    def start(
        self, view: RunView | None, recorded: tuple[str, ...] | None = None
    ) -> list[Decision]:
        """Run the root flow until it first suspends."""
        self._begin(view, recorded)
        self._launch(self._tasks[0], self._root, self._config)
        return self._end()

    def restore(
        self, base: LiveFlows, view: RunView | None, recorded: tuple[str, ...] | None
    ) -> list[Decision]:
        """Restart the flows of ``base`` from their snapshots with the current code.

        The root starts first. A task starts when its group ``start``s its key again, from the
        latest snapshot of the flow that had that key, with its decision numbering kept. A task
        that is not a registered flow has no snapshot: it is dropped with the old code, and if
        its parent, restarted from its own snapshot, starts that key again, it starts afresh.
        """
        self._begin(view, recorded)
        self.stop, self.published, self.choice = base.stop, base.published, base.choice
        self._issued = {(i.path, i.label): i.count for i in base.issued}
        self._restorable = {
            live.path: live for live in base.flows if live.path and live.name is not None
        }
        root = next(live for live in base.flows if not live.path)
        if root.name != self._root.name or not root.checkpointed:
            raise FlowCodeChanged(0)  # the root resumes only from a snapshot it recorded
        for live in base.flows:
            for held in live.holds:
                self._settle_held(held)
        if base.stop is not None:
            self._tasks[0].stop.set(base.stop)
        self._launch(self._tasks[0], self._root, self._snapshot_of(self._root, root))
        return self._end()

    def feed(
        self, item: Event | Recorded, view: RunView | None, recorded: tuple[str, ...] | None
    ) -> list[Decision]:
        """Deliver one event; the flows it unblocks run until all are suspended again."""
        self._begin(view, recorded)
        self._route(item)
        return self._end()

    def _begin(self, view: RunView | None, recorded: tuple[str, ...] | None) -> None:
        self.out = []
        self._reads = []
        self._view = view
        self._recorded = None if recorded is None else deque(recorded)

    def _end(self) -> list[Decision]:
        self._settle()
        if self._recorded:
            raise FlowCodeChanged(-1)  # replay made fewer reads than the journal holds
        return self.out

    def close(self) -> None:
        """Retire the run: unwind its suspended flows so their cleanup cannot start new work."""
        self.closed = True
        for task in tuple(self._tasks):
            if task.coro is not None:
                with suppress(ContractError, RuntimeError):
                    task.coro.close()

    @property
    def reads(self) -> tuple[str, ...]:
        """The reads made in the current step, as JSON."""
        return tuple(self._reads)

    def checkpoint(self) -> LiveFlows:
        """Where every running flow can be restarted from."""
        flows = []
        for task in self._tasks:
            definition = task.flow
            text = (
                _adapter(definition.snapshot).dump_json(task.snapshot).decode()
                if definition
                else ""
            )
            flows.append(
                LiveFlow(
                    path=task.path,
                    name=definition.name if definition else None,
                    snapshot_json=text,
                    holds=tuple(task.holds),
                    checkpointed=task.checkpointed,
                )
            )
        issued = tuple(Issued(path=p, label=lb, count=n) for (p, lb), n in self._issued.items())
        return LiveFlows(
            flows=tuple(flows),
            issued=issued,
            stop=self.stop,
            published=self.published,
            choice=self.choice,
        )

    def waiting(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """What each suspended flow waits for: its path and its sources, described."""
        return tuple(
            (task.path, tuple(s.describe() for s in task.blocked.sources))
            for task in self._tasks
            if task.blocked is not None
        )

    # -- reading and naming ------------------------------------------------------------------

    def read[T](self, fn: Callable[[RunView], T], as_: type[T] | TypeAdapter[T]) -> T:
        """A value of the run's view, journaled: live and replay both decode the same text."""
        adapter = as_ if isinstance(as_, TypeAdapter) else _adapter(as_)
        if self._recorded is not None:
            if not self._recorded:
                raise FlowCodeChanged(-1)  # replay reads more than the journal holds
            text = self._recorded.popleft()
        elif self._view is None:
            raise ContractError(("read",), "the view is not available while the run starts")
        else:
            text = adapter.dump_json(fn(self._view)).decode()
        self._reads.append(text)
        return self._decoded(lambda: adapter.validate_json(text))

    def _decoded[V](self, decode: Callable[[], V]) -> V:
        """``decode()`` of a recorded value; on replay, a value it rejects is a code change.

        The journal and the snapshots hold values the old code wrote. When the current code
        reads one as a type it no longer fits (the old code read the clock where the new code
        reads the task), replay has diverged at that point: the driver restarts the flows instead
        of failing the resume. A value the run produced in this very step must decode, so a
        failure there stays the error it is.
        """
        if self._recorded is None:
            return decode()
        try:
            return decode()
        except ValidationError as unreadable:
            raise FlowCodeChanged(-1) from unreadable  # the driver names the journal step

    def count(self, task: _Task, label: str) -> int:
        """The next number under ``label`` in ``task``'s flow (1, 2, ...); ids never repeat."""
        number = self._issued.get((task.path, label), 0) + 1
        self._issued[task.path, label] = number
        return number

    def stamp[W: Work | Finish](self, task: _Task, work: W) -> W:
        """``work`` with its decision id root turned into ``<path><root>.<n>``."""
        if self.closed:
            raise ContractError(("run",), "the run is closed")
        root = work.decision_id.root
        number = self.count(task, root)
        return work.model_copy(
            update={"decision_id": DecisionId(root=f"{task.path}{root}.{number}")}
        )

    def issue(self, task: _Task, pending: Pending, decision: Decision) -> Pending:
        """Send a decision out and register the ``Pending`` that its outcome will fill."""
        if pending.decision in self._pendings:
            raise ContractError(
                ("decision_id",), f"decision id {pending.decision.root} is not unique"
            )
        self._pendings[pending.decision] = pending
        task.started.append(pending)
        self.out.append(decision)
        return pending

    def forget(self, pending: Pending) -> None:
        """Abandon a request's outcome: if it arrives later it is dropped and recorded."""
        pending.abandoned = True
        self._pendings.pop(pending.decision, None)

    def timer(self, at: Seconds) -> Timer:
        """A timer for ``at``, and the wake-up that makes core deliver the time."""
        if self.closed:
            raise ContractError(("run",), "the run is closed")
        timer = Timer(at)
        self._timers.append(timer)
        self.out.append(WakeAt(time=at))
        return timer

    def signal(self, task: _Task, label: str, wanted: Callable[[Event], bool]) -> Signal:
        """A source of the turn-plumbing events ``wanted`` accepts, closed with ``task``."""
        found = Signal(label, wanted)
        self._signals.append(found)
        task.signals.append(found)
        return found

    def release(self, found: Signal) -> None:
        """Stop offering events to ``found``."""
        found.closed = True
        if found in self._signals:
            self._signals.remove(found)

    # -- tasks -------------------------------------------------------------------------------

    def begin_task[R](
        self,
        owner_path: str,
        group: Group[Any],
        key: str,
        start: tuple[Callable[..., Coroutine[Any, Any, R]], tuple[object, ...]],
    ) -> Task[R]:
        fn, args = start
        if not key or "/" in key:
            raise ContractError(("group", "key"), "a task key must be non-empty and contain no '/'")
        path = f"{owner_path}{key}/"
        if any(live.path == path for live in self._tasks):
            raise ContractError(("group", "key"), f"a task {path} is already running")
        task = _Task(path=path, group=group, handle=Task(key))
        self._tasks.append(task)
        group.running += 1
        if self.stop is not None:
            task.stop.set(self.stop)
            task.stop_pending = group.stops
        restored = self._restorable.pop(path, None)
        definition = definition_of(fn)
        if restored is not None:  # after a code change: resume from the flow's own snapshot
            if definition is None or definition.name != restored.name or not restored.checkpointed:
                raise FlowCodeChanged(
                    0
                )  # something else under this key, or no checkpoint to resume
            self._launch(task, definition, self._snapshot_of(definition, restored))
        elif definition is not None:
            if len(args) != 1 or not isinstance(args[0], definition.snapshot):
                message = f"{definition.name} takes its {definition.snapshot.__name__} snapshot"
                raise ContractError(("group", "start"), message)
            self._launch(task, definition, args[0])
        else:
            task.coro = fn(Flow(self, task), *args)
            self._resume(task, None, None)
        return task.handle

    def _settle_held(self, held: AttemptRef) -> None:
        """Settle a workspace a restarted flow had open, so the restart leaks none.

        The flow that held it starts over from its checkpoint and opens its workspaces again;
        the old one is closed plainly as cancelled. Its answer reaches no waiting flow and is
        recorded as dropped.
        """
        limit = self.read(lambda view: view.limits.cleanup_bound, float)
        work = self.stamp(
            self._tasks[0],
            Withdraw(
                decision_id=DecisionId(root="restart-settle"),
                scope=THIS_RUN,
                target=held,
                disposition=Settle(
                    assessments=(), eligible=False, retention="discard", outcome="cancelled"
                ),
            ),
        )
        self.out.append(Wait(request=work, until=Within(limit=limit)))

    @staticmethod
    def _snapshot_of(definition: FlowDef, live: LiveFlow) -> object:
        """The snapshot the old code recorded, as the current code's snapshot type."""
        try:
            return _adapter(definition.snapshot).validate_json(live.snapshot_json)
        except ValidationError as unreadable:
            raise FlowCodeChanged(0) from unreadable  # the snapshot type changed incompatibly

    def _launch(self, task: _Task, definition: FlowDef, argument: object) -> None:
        """Start ``task`` as a registered flow on ``argument`` (its first snapshot)."""
        task.flow, task.snapshot = definition, argument
        task.coro = definition.fn(Flow(self, task), argument)
        self._resume(task, None, None)

    def _resume(self, task: _Task, value: object, error: BaseException | None) -> None:
        """Run ``task`` to its next suspension, or to its end."""
        if task.coro is None:
            raise ContractError(("flow",), "a flow is resumed only after it was started")
        task.blocked = None
        try:
            block = task.coro.send(value) if error is None else task.coro.throw(error)
        except StopIteration as returned:
            self._ended(task, returned.value)
        except Stopped:
            if task.group is None:
                raise ContractError(("flow",), "the root flow is never stopped") from None
            self._ended(task, TaskStopped(key=task.handle.key))
        else:
            self._suspend(task, block)

    def _suspend(self, task: _Task, block: object) -> None:
        if not isinstance(block, First):
            raise ContractError(("await",), "a flow may await only ctx operations and sources")
        if not any(source.alive() for source in block.sources):
            raise Deadlock(task.path, tuple(s.describe() for s in block.sources))
        task.blocked = block

    def _ended(self, task: _Task, result: object) -> None:
        self._tasks.remove(task)
        for pending in task.started:
            if pending.alive():
                self.forget(pending)
        for found in task.signals:
            self.release(found)
        task.handle.set(result)
        if task.group is not None:
            task.group.running -= 1
            task.group.results.append(result)

    # -- events ------------------------------------------------------------------------------

    def _route(self, item: Event | Recorded) -> None:
        match item:
            case TimeReached(time=now):
                for timer in self._timers:
                    if timer.at <= now:
                        timer.set(TimeUp(at=timer.at))
                self._timers = [t for t in self._timers if not t.ready()]
            case StopRequested(control=control):
                if control.action == "stop" and self.stop is None:
                    self._stop(item)
            case CheckpointKnown(invocation=invocation, checkpoint=checkpoint):
                self.checkpoints[invocation] = checkpoint
                self._offer(item)
            case TurnYielded() | YieldFailed() | ResumeReady():
                self._offer(item)
            case _:
                self._answer(item)

    def _offer(self, event: Event) -> None:
        for found in tuple(self._signals):
            found.offer(event)

    def _answer(self, item: Event | Recorded) -> None:
        decision = self._answered_decision(item)
        if decision is None:
            return  # an event that answers nothing of ours (an agent's own measurement)
        pending = self._pendings.pop(decision, None)
        if pending is None:
            self.dropped.append(Dropped(event=_kind_of(item), decision=decision))
        else:
            pending.set(self._rebuilt(item))

    def _answered_decision(self, item: Event | Recorded) -> DecisionId | None:
        """The request this event answers, by the table ``_ANSWER_RULES``.

        A close answers the ``Withdraw`` of its attempt (core reports it under the decision that
        opened the attempt); an adoption without a decision answers the ``ProposeWinner``;
        anything else answers the decision it names.
        """
        for rule in _ANSWER_RULES:
            found = rule(item, self._pendings)
            if found is not None:
                return found
        return None

    def _rebuilt(self, item: Event | Recorded) -> Event:
        """The event a recorded item stands for: live and replay both go through here."""
        if isinstance(item, RecordedOperation):
            context = {"operation_registry": self.wiring.registry, "persisted_operation": True}
            result = self._decoded(
                lambda: OperationResult.model_validate_json(item.result_json, context=context)
            )
            return OperationFinished(decision=item.decision, result=result)
        return item

    def _stop(self, item: StopRequested) -> None:
        self.stop = item
        for task in self._tasks:
            task.stop.set(item)
            if task.group is not None and task.group.stops and not task.stopped:
                task.stop_pending = True

    def _settle(self) -> None:
        """Resume flows that can move, in the order they started, until none can."""
        while True:
            for task in tuple(self._tasks):
                if self._move(task):
                    break
            else:
                return

    def _move(self, task: _Task) -> bool:
        """Resume ``task`` if it has a stop to take or a ready source; whether it moved."""
        if task.blocked is None:
            return False
        if task.stop_pending and task.shield == 0:
            task.stop_pending, task.stopped = False, True
            self._resume(task, None, Stopped())
            return True
        ready = next((s for s in task.blocked.sources if s.ready()), None)
        if ready is None:
            return False
        self._resume(task, ready.take(), None)
        return True


def _kind_of(item: Event | Recorded) -> EventKind:
    if isinstance(item, RecordedOperation):
        return EventKind.OPERATION_FINISHED
    return EventKind(item.kind)


def _closed_withdraw(
    item: Event | Recorded, waiting: Mapping[DecisionId, Pending]
) -> DecisionId | None:
    if not isinstance(item, WorkspaceClosed):
        return None
    attempt = item.settlement.attempt.attempt_id
    for decision, pending in waiting.items():
        work = pending.work
        if (
            isinstance(work, Withdraw)
            and isinstance(work.target, AttemptRef)
            and work.target.attempt_id == attempt
        ):
            return decision
    return None


def _adoption_of_proposal(
    item: Event | Recorded, waiting: Mapping[DecisionId, Pending]
) -> DecisionId | None:
    if not isinstance(item, AdoptionFinished) or item.decision is not None:
        return None
    return next((d for d, p in waiting.items() if isinstance(p.work, ProposeWinner)), None)


def _own_decision(
    item: Event | Recorded, _waiting: Mapping[DecisionId, Pending]
) -> DecisionId | None:
    return getattr(item, "decision", None)


_ANSWER_RULES = (_closed_withdraw, _adoption_of_proposal, _own_decision)
"""Which waiting request an event answers, tried in order; see ``_Run._answered_decision``."""
