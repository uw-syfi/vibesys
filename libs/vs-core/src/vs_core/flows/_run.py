"""The run: every live flow of one search, driven by hand."""

from __future__ import annotations

from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from vs_core._registry import ContractError
from vs_core._run_pause import DEADLINE_STOP, ENVIRONMENT_STOP
from vs_core.types.common import THIS_RUN, AttemptRef, DecisionId, InvocationRef, RunFacts, Seconds
from vs_core.types.kinds import EventKind
from vs_core.types.measurement import ComparisonSettled
from vs_core.types.records import OperationResult
from vs_core.types.search import (
    AdoptionFinished,
    CheckpointKnown,
    Event,
    OperationFinished,
    ResumeFailed,
    ResumeReady,
    StopLifted,
    StopRequested,
    TimeReached,
    TurnYielded,
    WorkspaceClosed,
    WorkspaceOpened,
    YieldFailed,
)
from vs_core.types.strategy import (
    Decision,
    Finish,
    Measure,
    Operation,
    ProposeWinner,
    RequestTurn,
    Settle,
    StartAttempt,
    Wait,
    WakeAt,
    Withdraw,
    Work,
)
from vs_core.types.until import Failed, Within

from ._defs import FlowDef, _adapter, _Task, definition_of, snapshot_adapter
from ._errors import Deadlock, FlowCodeChanged, Stopped, TaskStopped, TimeUp
from ._flow import Flow
from ._labels import labeled
from ._sources import (
    Channel,
    ChannelCell,
    ChannelKey,
    First,
    Latch,
    LatchCell,
    LatchKey,
    Pending,
    Signal,
    Task,
    Timer,
)
from ._spans import RunTrace, SpanLabel
from ._state import (
    Dropped,
    Held,
    InFlight,
    Issued,
    LiveFlow,
    LiveFlows,
    Recorded,
    RecordedOperation,
    RestartSettled,
    RestartSettleReason,
)
from ._trace import Trace, operation_span
from ._winner import ADOPTED, NOT_ADOPTED

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

    from vs_core._ids import IdScheme
    from vs_core._registry import OperationRegistration, OperationRegistry
    from vs_core._strategy_operations import StrategyOperations
    from vs_core.types.agents import Checkpoint
    from vs_core.types.common import ControlInput
    from vs_core.types.search import RunView
    from vs_core.types.settlement import Selection

    from ._flow import Group
    from ._labels import Label, LabelFields, SpanKind
    from ._roles import Role
    from ._spans import SpanId


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
    spans: Mapping[str, SpanKind] = field(default_factory=dict)
    """The span kinds the search declared, by name: the only ones a label may name."""


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
        self.concluded = False
        """Whether the root asked core to end the run (`Kit.finish`): no flow is left to run."""
        self.closed = False
        self.published: str | None = None
        self.choice: Selection | None = None
        self.choice_label: SpanLabel | None = None
        self.out: list[Decision] = []
        self.dropped: list[Dropped] = []
        self.restart_settled: list[RestartSettled] = []
        self.checkpoints: dict[InvocationRef, Checkpoint] = {}
        self._root = root
        self._config = config
        self._tasks: list[_Task] = [_Task(path="")]  # live flows, the root first
        self._pendings: dict[DecisionId, Pending] = {}
        self._signals: list[Signal] = []
        self._timers: list[Timer] = []
        self._latches: dict[str, LatchCell] = {}
        self._channels: dict[str, ChannelCell] = {}
        self._restorable: dict[str, LiveFlow] = {}
        self._carried: dict[str, list[Held]] = {}
        self._in_flight: list[InFlight] = []
        self._awaited: dict[DecisionId, InFlight] = {}
        self._issued: dict[tuple[str, str], int] = {}
        self._view: RunView | None = None
        self._recorded: deque[str] | None = None
        self._reads: list[str] = []
        self.trace = Trace(RunTrace())

    # -- one step: init, restore or one event ------------------------------------------------

    def start(
        self, view: RunView | None, recorded: tuple[str, ...] | None = None, at: Seconds = 0.0
    ) -> list[Decision]:
        """Run the root flow until it first suspends."""
        self._begin(view, recorded, at)
        root = self._tasks[0]
        root.span = self.trace.flow_started("", None, self._root.name, stopped=False)
        self._launch(root, self._root, self._config)
        return self._end()

    def restore(
        self,
        base: LiveFlows,
        view: RunView | None,
        recorded: tuple[str, ...] | None,
        at: Seconds = 0.0,
    ) -> list[Decision]:
        """Restart the flows of ``base`` from their snapshots with the current code.

        The root starts first. A task starts when its group ``start``s its key again, from the
        latest snapshot of the flow that had that key, with its decision numbering kept. A task
        that is not a registered flow has no snapshot: it is dropped with the old code, and if
        its parent, restarted from its own snapshot, starts that key again, it starts afresh.

        A workspace a resumable flow held when it recorded its latest checkpoint stays open for
        it (``ctx.workspaces.reopen``); every other workspace is settled, by the salvage its flow
        recorded (``ctx.workspaces.salvage``) or else plainly, with the reason recorded. So is a
        kept workspace whose flow the new code does not start again in this step. A workspace
        whose settlement the flow already asked for is left to it. A measurement or a settlement
        in flight goes to the resumed flow that asks for the same one again (``issue``).
        """
        self.trace = Trace(base.trace)
        self._begin(view, recorded, at)
        self.stop, self.published, self.choice = base.stop, base.published, base.choice
        self.choice_label = base.choice_label
        self._issued = {(i.path, i.label): i.count for i in base.issued}
        self._in_flight = list(base.in_flight)
        self._restorable = {
            live.path: live for live in base.flows if live.path and live.name is not None
        }
        root = next(live for live in base.flows if not live.path)
        if root.name != self._root.name or not root.checkpointed:
            raise FlowCodeChanged(0)  # the root resumes only from a snapshot it recorded
        self.trace.restarted(self._restorable)
        self._tasks[0].span = self.trace.flow_started(
            "", None, self._root.name, stopped=base.stop is not None
        )
        for live in base.flows:
            resumable = live.name is not None and live.checkpointed
            for held in live.holds:
                if held.settling is not None and not (resumable and held.carried):
                    self._left_settling(live.path, held)
                elif not resumable:
                    self._settle_held(live.path, held, RestartSettleReason.FLOW_NOT_RESUMABLE)
                elif not held.carried:
                    self._settle_held(live.path, held, RestartSettleReason.OPENED_AFTER_CHECKPOINT)
                else:
                    self._carried.setdefault(live.path, []).append(held)
        if base.stop is not None:
            self._tasks[0].stop.set(base.stop)
        self._tasks[0].carried = self._carried.pop("", [])
        self._launch(self._tasks[0], self._root, self._snapshot_of(self._root, root))
        self._settle()
        for path, held in [(p, h) for p, kept in self._carried.items() for h in kept]:
            if held.settling is not None:
                self._left_settling(path, held)
            else:
                self._settle_held(path, held, RestartSettleReason.NOT_RESTARTED)
        self._carried = {}
        self._drop_unasked()
        return self._end()

    def _drop_unasked(self) -> None:
        """End the restart step: work in flight that the resumed flows did not ask for has no owner.

        The new code decides what it needs, and it has asked for that by now, in this step: a
        request it asked again took over the old one (``issue``). What is left was in flight at the
        change and the new code does not want it, so it is let go: a workspace opening for it is
        settled as unclaimed when it arrives (or now, if it already has), which core carries out
        through the steps of the attempt, so nothing is freed on the record alone. A flow that
        asks for the same work later gets new work. A settlement in flight is not asked for: it
        finishes what the dead flows began, and stays.
        """
        kept: list[InFlight] = []
        for entry in self._in_flight:
            if isinstance(entry.request, Withdraw):
                kept.append(entry)
            elif isinstance(entry.answer, WorkspaceOpened):
                held = Held(attempt=entry.answer.attempt)
                self._settle_held(entry.path, held, RestartSettleReason.UNCLAIMED)
        self._in_flight = kept

    def feed(
        self,
        item: Event | Recorded,
        view: RunView | None,
        recorded: tuple[str, ...] | None,
        at: Seconds = 0.0,
    ) -> list[Decision]:
        """Deliver one event; the flows it unblocks run until all are suspended again."""
        self._begin(view, recorded, at)
        self._route(item)
        return self._end()

    def _begin(self, view: RunView | None, recorded: tuple[str, ...] | None, at: Seconds) -> None:
        """Start a step: live at the ``view``'s time, or replaying the ``recorded`` reads at ``at``."""
        self.trace.at = at if view is None else view.now_at
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
                snapshot_adapter(definition).dump_json(task.snapshot).decode() if definition else ""
            )
            flows.append(
                LiveFlow(
                    path=task.path,
                    name=definition.name if definition else None,
                    snapshot_json=text,
                    holds=(*task.holds, *task.carried),
                    checkpointed=task.checkpointed,
                )
            )
        issued = tuple(Issued(path=p, label=lb, count=n) for (p, lb), n in self._issued.items())
        measuring = tuple(
            item
            for decision, item in self._awaited.items()
            if decision in self._pendings and self._pendings[decision].alive()
        )
        return LiveFlows(
            flows=tuple(flows),
            issued=issued,
            stop=self.stop,
            published=self.published,
            choice=self.choice,
            choice_label=self.choice_label,
            in_flight=(*self._in_flight, *measuring),
            trace=self.trace.snapshot(),
        )

    def waiting(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """What each suspended flow waits for: its path and its sources, described."""
        return tuple(
            (task.path, tuple(s.describe() for s in task.blocked.sources))
            for task in self._tasks
            if task.blocked is not None
        )

    def latch[T](self, key: LatchKey[T]) -> Latch[T]:
        """A new handle on the run's latch ``key``; another key with its name is a bug."""
        cell = self._latches.setdefault(key.name, LatchCell(key))
        if cell.key.declaration is not key.declaration:
            message = "another latch key has this name: declare each key once and share it"
            raise ContractError(("latch", key.name), message)
        return Latch(key, cell)

    def channel[T](self, key: ChannelKey[T]) -> Channel[T]:
        """A handle on the run's channel ``key``; another key with its name is a bug."""
        cell = self._channels.get(key.name)
        if cell is None:
            cell = self._channels[key.name] = ChannelCell(key, lambda: len(self._tasks))
        if cell.key is not key:
            message = "another channel key has this name: declare each key once and share it"
            raise ContractError(("channel", key.name), message)
        return Channel(key, cell)

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

    def awaits(self, decision: DecisionId) -> bool:
        """Whether the flows still stand behind ``decision``.

        A flow waits for it, or it was in flight when the run restarted and the resumed flow
        takes over its answer (``issue``, ``_left_settling``).
        """
        return decision in self._pendings or any(
            item.request.decision_id == decision for item in self._in_flight
        )

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

    def issue(
        self, task: _Task, pending: Pending, decision: Decision, label: Label | None = None
    ) -> Pending:
        """Send a decision out and register the ``Pending`` that its outcome will fill.

        A measurement or a settlement the flow already had in flight when the run restarted is not
        sent again: the ``Pending`` waits for the old one's outcome instead (``_reattached``).
        ``label`` labels the operation's span as it starts.
        """
        if pending.decision in self._pendings:
            raise ContractError(
                ("decision_id",), f"decision id {pending.decision.root} is not unique"
            )
        work = pending.work
        if isinstance(work, Measure | Withdraw | StartAttempt | ProposeWinner):
            reattached = self._reattach(task, pending, work, label)
            if reattached is not None:
                return reattached
        self._pendings[pending.decision] = pending
        task.started.append(pending)
        self.out.append(decision)
        match pending.work:
            case Finish(result=result):
                self.trace.run_ended(self._tasks[0].span_id, result)
            case Operation(request=request):
                self.trace.operation_started(task.span_id, pending.decision, request.kind)
            case RequestTurn(turn=turn) as work:
                invocation = turn.invocation_id.root
                self.trace.operation_started(
                    task.span_id, pending.decision, _label(work), invocation
                )
            case work:
                self.trace.operation_started(task.span_id, pending.decision, _label(work))
        if not isinstance(pending.work, Finish):
            task.last_operation = operation_span(pending.decision)
        if label is not None:
            self.label(operation_span(pending.decision), label.kind, label.fields())
        return pending

    def _reattach(
        self,
        task: _Task,
        pending: Pending,
        work: Measure | Withdraw | StartAttempt | ProposeWinner,
        label: Label | None,
    ) -> Pending | None:
        """The ``Pending`` waiting on the old request ``work`` repeats, or None if it is new.

        The request is registered as awaited either way; only a repeat of one in flight at the
        restart is not sent again.
        """
        issued_as = _label(work)
        earlier = self._reattached(task.path, issued_as, work)
        request = work if earlier is None else earlier.request
        if earlier is not None:
            pending = Pending(request)
        self._awaited[pending.decision] = InFlight(path=task.path, label=issued_as, request=request)
        if earlier is None:
            return None
        self._pendings[pending.decision] = pending
        task.started.append(pending)
        # the restart stopped its span: the resumed flow waits for it again
        self.trace.operation_started(task.span_id, pending.decision, issued_as)
        if label is not None:
            self.label(operation_span(pending.decision), label.kind, label.fields())
        if earlier.answer is not None:  # core answered it already: that is the answer
            pending.set(earlier.answer)
            self.trace.operation_answered(pending.decision, earlier.answer)
        return pending

    def _reattached(
        self, path: str, label: str, request: Measure | Withdraw | StartAttempt | ProposeWinner
    ) -> InFlight | None:
        """The request in flight at the restart that ``request`` asks for again, if any.

        It must come from the same flow, under the same label and in the same scope. A
        measurement is the same with the same plan apart from the time it was submitted; a
        workspace is the same request but its id. A settlement is the same when it closes the
        same workspace: core settles a workspace once, as it was first asked, so the resumed flow
        waits for that settlement whatever it asks now.
        """
        for item in self._in_flight:
            old = item.request
            if item.path == path and item.label == label and _same_request(old, request):
                self._in_flight.remove(item)
                return item
        return None

    def label(self, span_id: SpanId, kind: SpanKind, fields: LabelFields) -> None:
        """Label the span ``span_id`` as ``kind``: ``fields`` replace those they name."""
        current = self.trace.label_of(span_id)
        found = labeled(self.wiring.spans, current, kind, fields, self.trace.exists)
        self.trace.labeled(span_id, found)

    def chose(self, selection: Selection, label: Label | None) -> None:
        """The flow named ``selection`` its winner; ``label`` is how the span that adopts it reads.

        The label is checked as it is given, so a flow that names one the search did not declare
        fails where it chose, not at the end of the run.
        """
        self.choice = selection
        if label is None:
            self.choice_label = None
            return
        missing = {ADOPTED, NOT_ADOPTED} - set(label.kind.verdicts)
        if missing:
            message = f"the kind {label.kind.name!r} of an adoption must declare {sorted(missing)}"
            raise ContractError(("choose", "label", "kind"), message)
        self.choice_label = labeled(
            self.wiring.spans, None, label.kind, label.fields(), self.trace.exists
        )

    def adopting(self, pending: Pending, verdict: str | None, reason: str | None) -> None:
        """Label the span of the adoption ``pending`` with the label given to ``choose``, if any."""
        chosen = self.choice_label
        if chosen is None:
            return
        found = chosen.model_copy(update={"verdict": verdict, "reason": reason})
        self.trace.labeled(operation_span(pending.decision), found)

    def forget(self, pending: Pending) -> None:
        """Abandon a request's outcome: if it arrives later it is dropped and recorded."""
        pending.abandoned = True
        self._pendings.pop(pending.decision, None)
        self._awaited.pop(pending.decision, None)
        if not isinstance(pending.work, Finish):
            self.trace.operation_abandoned(pending.decision)

    def land_stop(self, task: _Task) -> None:
        """Take the task's pending stop: the caller raises ``Stopped`` at its await."""
        task.stop_pending, task.stopped = False, True
        self.trace.stop_landed(task.span_id)

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
        owner: _Task,
        group: Group[Any],
        key: str,
        start: tuple[Callable[..., Coroutine[Any, Any, R]], tuple[object, ...]],
    ) -> Task[R]:
        fn, args = start
        if not key or "/" in key:
            raise ContractError(("group", "key"), "a task key must be non-empty and contain no '/'")
        path = f"{owner.path}{key}/"
        if any(live.path == path for live in self._tasks):
            raise ContractError(("group", "key"), f"a task {path} is already running")
        definition = definition_of(fn)
        name = definition.name if definition else getattr(fn, "__name__", key)
        span = self.trace.flow_started(path, owner.span_id, name, stopped=self.stop is not None)
        task = _Task(path=path, group=group, handle=Task(key), span=span)
        task.handle.span = span
        self._tasks.append(task)
        group.running += 1
        if self.stop is not None:
            task.stop.set(self.stop)
            task.stop_pending = group.stops
        restored = self._restorable.pop(path, None)
        if restored is not None:  # after a code change: resume from the flow's own snapshot
            if definition is None or definition.name != restored.name or not restored.checkpointed:
                raise FlowCodeChanged(
                    0
                )  # something else under this key, or no checkpoint to resume
            task.carried = self._carried.pop(path, [])
            self._launch(task, definition, self._snapshot_of(definition, restored))
        elif definition is not None:
            if len(args) != 1 or not isinstance(args[0], definition.snapshot):
                message = f"{definition.name} takes its {definition.snapshot_name} snapshot"
                raise ContractError(("group", "start"), message)
            self._launch(task, definition, args[0])
        else:
            task.coro = fn(Flow(self, task), *args)
            self._resume(task, None, None)
        return task.handle

    def _settle_held(self, path: str, held: Held, reason: RestartSettleReason) -> None:
        """Settle a workspace a restart does not give back to a flow, so the restart leaks none.

        It settles as the flow's salvage asserts (``ctx.workspaces.salvage``), else plainly as
        cancelled and discarded; the settlement and its reason are recorded. Core's answer
        reaches no waiting flow and is recorded as dropped.
        """
        disposition = held.salvage or Settle(
            assessments=(), eligible=False, retention="discard", outcome="cancelled"
        )
        limit = self.read(lambda view: view.limits.cleanup_bound, float)
        work = self.stamp(
            self._tasks[0],
            Withdraw(
                decision_id=DecisionId(root="restart-settle"),
                scope=THIS_RUN,
                target=held.attempt,
                disposition=disposition,
            ),
        )
        self.out.append(Wait(request=work, until=Within(limit=limit)))
        self.restart_settled.append(
            RestartSettled(
                attempt=held.attempt, flow=path, reason=reason, retention=disposition.retention
            )
        )

    def _left_settling(self, path: str, held: Held) -> None:
        """Leave a workspace whose settlement is in flight to it, and record that."""
        disposition = held.settling
        if disposition is None:
            message = "only a workspace whose settlement is in flight is left to it"
            raise ContractError(("restart",), message)
        self.restart_settled.append(
            RestartSettled(
                attempt=held.attempt,
                flow=path,
                reason=RestartSettleReason.SETTLING,
                retention=disposition.retention,
            )
        )

    @staticmethod
    def _snapshot_of(definition: FlowDef, live: LiveFlow) -> object:
        """The snapshot the old code recorded, as the current code's snapshot type."""
        try:
            return snapshot_adapter(definition).validate_json(live.snapshot_json)
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
        for source in block.sources:
            source.waited_by(task.path)
        if not any(source.alive() for source in block.sources):
            raise Deadlock(task.path, tuple(s.describe() for s in block.sources))
        task.blocked = block
        self.trace.suspended(task.span_id, block.sources)

    def _ended(self, task: _Task, result: object) -> None:
        for held in task.carried:  # resumed, and ended without taking its workspace back
            if held.settling is not None:
                self._left_settling(task.path, held)
            else:
                self._settle_held(task.path, held, RestartSettleReason.NOT_REOPENED)
        task.carried = []
        for entry in [i for i in self._in_flight if i.path == task.path and i.answer is not None]:
            # an answer kept for the flow that it no longer takes: a workspace it opens is settled
            self._in_flight.remove(entry)
            if isinstance(entry.answer, WorkspaceOpened):
                held = Held(attempt=entry.answer.attempt)
                self._settle_held(task.path, held, RestartSettleReason.UNCLAIMED)
        self._tasks.remove(task)
        for pending in task.started:
            if pending.alive():
                self.forget(pending)
        for found in task.signals:
            self.release(found)
        self.trace.flow_ended(task.span_id, result)
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
            case StopRequested():
                self._stop_requested(item)
            case StopLifted():
                if self.stop is not None:
                    self._lift_stop()
            case CheckpointKnown(invocation=invocation, checkpoint=checkpoint):
                self.checkpoints[invocation] = checkpoint
                self._offer(item)
            case (
                TurnYielded() | YieldFailed() | ResumeReady() | ResumeFailed() | ComparisonSettled()
            ):
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
        self._awaited.pop(decision, None)
        if self._answered_early(item, decision, pending):
            return
        self._in_flight = [i for i in self._in_flight if not _answers(item, decision, i.request)]
        if pending is None:
            self.dropped.append(Dropped(event=_kind_of(item), decision=decision))
            if isinstance(item, WorkspaceOpened):  # no flow holds it: settle it, or it leaks
                path = decision.root.rpartition("/")[0]
                held = Held(attempt=item.attempt)
                self._settle_held(f"{path}/" if path else "", held, RestartSettleReason.UNCLAIMED)
            return
        event = self._rebuilt(item)
        pending.set(event)
        if not isinstance(pending.work, Finish):
            self.trace.operation_answered(decision, event)

    def _answered_early(
        self, item: Event | Recorded, decision: DecisionId, pending: Pending | None
    ) -> bool:
        """Keep an opening or a settlement that came before the resumed flow asked for it again.

        A flow resumed after a restart asks again for what it waited for (``issue``); core may
        answer the old request first. An opened workspace is kept for the flow at that path,
        which takes it when it asks, so the workspace opens once; if the flow ends without
        asking, the workspace is settled (``_ended``). A settlement is kept only while a flow
        there holds its workspace: it reopened it and will close it.
        """
        kept = WorkspaceOpened | WorkspaceClosed | AdoptionFinished | Failed
        if pending is not None or not isinstance(item, kept):
            return False
        for index, entry in enumerate(self._in_flight):
            request = entry.request
            if entry.answer is not None or not _answers(item, decision, request):
                continue
            if isinstance(request, Measure) or (
                isinstance(item, WorkspaceClosed) != isinstance(request, Withdraw)
            ):
                # a measurement asked again later is measured again (the flow may ask another);
                # a close answers only a settlement, reported under the opening decision
                continue
            here = [task for task in self._tasks if task.path == entry.path]
            if isinstance(item, WorkspaceClosed):
                attempt = item.settlement.attempt.attempt_id
                here = [
                    task
                    for task in here
                    if any(h.attempt.attempt_id == attempt for h in (*task.holds, *task.carried))
                ]
            if here:
                self._in_flight[index] = entry.model_copy(update={"answer": item})
                return True
        return False

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

    def published_as[M: BaseModel](self, model: type[M]) -> M | None:
        """What the run last published, as ``model``; None before any publish."""
        text = self.published
        return None if text is None else model.model_validate_json(text)

    def _stop_requested(self, item: StopRequested) -> None:
        """Take the run's first stop; a later one only names the ending (``_overrides``).

        The flows already took the first stop, so a later one reaches none of them. Only one of
        the host's own stops that follows an operator's replaces it, as the stop the ending
        reads (``_winner._stop_ending``).
        """
        if item.control.action != "stop":
            return
        if self.stop is None:
            self._stop(item)
        elif _overrides(item.control, self.stop):
            self.stop = item

    def _stop(self, item: StopRequested) -> None:
        self.stop = item
        self.trace.stop_requested()
        for task in self._tasks:
            task.stop.set(item)
            if task.group is not None and task.group.stops and not task.stopped:
                task.stop_pending = True

    def _lift_stop(self) -> None:
        """Take the stop back: flows it has not yet interrupted go on as if it never came.

        Work the stop already ended stays ended (a task that took ``Stopped`` has returned),
        and a flow that took the stop value off ``ctx.stopped`` has moved on. A later stop
        reaches every flow again.
        """
        self.stop = None
        self.trace.stop_lifted()
        for task in self._tasks:
            task.stop.lift()
            task.stop_pending = False

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
            self.land_stop(task)
            self._resume(task, None, Stopped())
            return True
        ready = next((s for s in task.blocked.sources if s.ready()), None)
        if ready is None:
            return False
        self._resume(task, ready.take(), None)
        return True


def _overrides(control: ControlInput, taken: StopRequested) -> bool:
    """Whether ``control``, a later stop, names the run's ending over the stop already taken.

    A host deadline or environment failure overrides an earlier operator shutdown.
    Between two host causes, the first determines this epoch's result.
    """
    return control.control_id.root.partition(":")[0] in {
        DEADLINE_STOP,
        ENVIRONMENT_STOP,
    } and taken.control.control_id.root.partition(":")[0] not in {DEADLINE_STOP, ENVIRONMENT_STOP}


def _same_plan(old: Measure, new: Measure) -> bool:
    """Whether two measurements measure the same thing (they may be asked at different times)."""
    when = {"submitted_at": 0.0}
    return old.plan.model_copy(update=when) == new.plan.model_copy(update=when)


def _same_request(
    old: Measure | Withdraw | StartAttempt | ProposeWinner,
    new: Measure | Withdraw | StartAttempt | ProposeWinner,
) -> bool:
    """Whether ``new`` asks again for what ``old`` asked (see ``_Run._reattached``)."""
    if old.scope != new.scope:
        return False
    match old, new:
        case Measure(), Measure():
            return _same_plan(old, new)
        case Withdraw(), Withdraw():
            return old.target == new.target
        case StartAttempt(), StartAttempt():
            return old.model_copy(update={"decision_id": new.decision_id}) == new
        case ProposeWinner(), ProposeWinner():
            return old.selection == new.selection
        case _:
            return False


def _answers(
    item: Event | Recorded,
    decision: DecisionId | None,
    request: Measure | Withdraw | StartAttempt | ProposeWinner,
) -> bool:
    """Whether ``item``, answering ``decision``, answers ``request`` (a close: by its workspace)."""
    if isinstance(item, WorkspaceClosed) and isinstance(request, Withdraw):
        target = request.target
        attempt = item.settlement.attempt.attempt_id
        return isinstance(target, AttemptRef) and target.attempt_id == attempt
    return request.decision_id == decision


def _label(work: Work) -> str:
    """The id root the aspect issued ``work`` under: ``a/worker.2`` was issued as ``worker``."""
    return work.decision_id.root.rpartition("/")[2].rpartition(".")[0]


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
