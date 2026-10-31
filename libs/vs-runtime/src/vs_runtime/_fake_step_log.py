"""An in-memory ``StepLog``: core's own records, appended through the confirmed log and folded.

Nothing about the log is invented here: every method appends the record core defines for it
through ``vs_runlog.ConfirmedLog`` and folds it with ``vs_core.api.recovery.fold``, so the state
is what a real run.log would give. A record the fold refuses is never appended. Nothing is stood
in for: the end of a run is core's ``RunEnded`` record, and a parked run resumes by a restart (a
new log over the same records) and a resume control pushed into it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, ClassVar, Literal

import vs_core.api as core
from vs_core.api.recovery import (
    RecordRefusal,
    Run,
    ended_message,
    finished_message,
    fold,
    input_message,
    interrupted_message,
    open_step,
    start_due,
    started_message,
)
from vs_proto.api import EpochStarted, Record, Timestamp
from vs_runlog.api import Appended, ConfirmedLog, CoreMessage
from vs_runtime._step_loop import OpenStep
from vs_sim.api.testing import Changes

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreEvent, CoreState, OperationRegistry, RequestId
    from vs_core.api.recovery import End, Interruption, StepResult
    from vs_sim.api import Clock


class FakeLogUncertainError(Exception):
    """The in-memory log reported an append as uncertain, which its writer never does."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the in-memory run.log could not confirm an append")


class FakeRunRecords:
    """The durable side of a run.log, kept in memory: what survives the death of a host."""

    def __init__(self) -> None:
        """Start with an empty log."""
        self.records: list[Record] = []
        self.changes = Changes()
        """Notified at every commit, for a test that waits on the log."""

    def append(self, record: Record) -> None:
        """Take one record (the writer surface of ``ConfirmedLog``)."""
        self.records.append(record)

    def commit(self) -> None:
        """Make the appended records durable, and say the log changed."""
        self.changes.notify()

    def read(self) -> tuple[Record, ...]:
        """Every durable record, in order."""
        return tuple(self.records)

    def count(self, field: str) -> int:
        """How many records have the body ``field``."""
        return sum(1 for r in self.records if r.body is not None and r.body.field == field)


class _Scratch:
    """A writer that keeps nothing, to build the next record before the log takes it."""

    def __init__(self, records: tuple[Record, ...]) -> None:
        self._records = list(records)

    def append(self, record: Record) -> None:
        self._records.append(record)

    def commit(self) -> None:
        pass

    def read(self) -> tuple[Record, ...]:
        return tuple(self._records)


class FakeStepLog:
    """One process's view of a run.log: it opens a new epoch, then appends and folds.

    Building one over the same ``FakeRunRecords`` is a restart: the epoch is one past the last
    record's, and the state is the fold of everything the earlier processes left.
    """

    def __init__(
        self,
        records: FakeRunRecords,
        seed: CoreState,
        registry: OperationRegistry,
        clock: Clock,
    ) -> None:
        """Fold ``records`` over ``seed`` and open the next epoch."""
        self._records = records
        self._registry = registry
        self._clock = clock
        self._seed = seed
        self._log = ConfirmedLog(records)
        self._inputs = Changes()
        self._inputs_seen = 0
        last = records.records[-1].epoch if records.records else 0
        self.epoch = last + 1
        self._state = self._refolded()
        self._append(EpochStarted(epoch=self.epoch))

    @property
    def state(self) -> CoreState:
        """The fold of everything appended, by this process or an earlier one."""
        return self._state

    def push(self, event: CoreEvent) -> None:
        """Append an input that arrived from outside the loop, and wake ``next_input``."""
        self._append(input_message(event))
        self._inputs.notify()

    def open_step(self, tag: RequestId) -> OpenStep | None:
        """The step ``tag`` as core has it, while it has no result."""
        view = open_step(self._state, tag)
        return None if view is None else OpenStep(view.step, view.deadline_at, view.started)

    async def step_started(self, run: Run) -> Run | RecordRefusal:
        """Append ``StepStarted`` if the rule would run the step now, with the limit it gives now."""
        at = self._clock.now()
        due = start_due(self._state, run.tag, at)
        if isinstance(due, RecordRefusal):
            return due
        recorded = replace(run, deadline_at=due.deadline_at)
        self._append(started_message(recorded), clock=at)
        return recorded

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> None:
        """Append ``StepFinished``."""
        self._append(finished_message(tag, attempt, result))

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Append ``AttemptInterrupted``."""
        self._append(interrupted_message(tag, attempt, why))

    async def run_ended(self, end: End) -> None:
        """Append ``RunEnded``."""
        self._append(ended_message(end))

    async def clock_advanced(self, at: float) -> None:
        """Append the input that the run clock reached ``at``."""
        self._append(input_message(core.ClockAdvanced(now_at=at)))

    async def next_input(self) -> None:
        """Return once ``push`` was called since this last returned."""
        await self._inputs.changed(self._inputs_seen)
        self._inputs_seen = self._inputs.version

    def _refolded(self) -> CoreState:
        return fold(self._records.read(), self._seed, registry=self._registry)

    def _append(self, message: CoreMessage, *, clock: float | None = None) -> None:
        """Fold the next record first, so a record the fold refuses is never appended.

        The record is stamped with ``clock`` when the caller read it for its own check, so the
        check and the stamp are one instant.
        """
        stamp = self._clock.now() if clock is None else clock
        scratch = ConfirmedLog(_Scratch(self._records.read()))
        built = scratch.append(message, epoch=self.epoch, at=Timestamp(), clock=stamp)
        if not isinstance(built, Appended):
            raise FakeLogUncertainError
        folded = fold([built.record], self._state, registry=self._registry)
        if not isinstance(self._log.extend(built.record), Appended):
            raise FakeLogUncertainError
        self._state = folded


class FakeQueryOutcome(core.Value):
    """What the fake query operation reports."""

    status: Literal["succeeded"] = "succeeded"


class FakeQuery(core.OperationRequest):
    """A registered read-only operation: a step core can open that needs no real executor."""

    kind: Literal["test.query"] = "test.query"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[FakeQueryOutcome]] = FakeQueryOutcome
    value: int


@dataclass(frozen=True)
class FakeRunSeed:
    """A core state with open steps, the registry that decodes them, and their tags in order."""

    state: CoreState
    registry: OperationRegistry
    tags: tuple[RequestId, ...]


def fake_run_seed(
    limits_s: Sequence[float],
    retries: int,
    *,
    standing: core.RequestPhase = core.RequestPhase.PREPARED,
) -> FakeRunSeed:
    """A run with one open step per entry of ``limits_s``, each allowed that many seconds.

    ``retries`` is the bound on re-sends of a step after its first attempt (core's
    ``Limits.request_retries``): a step whose charged attempts pass it ends as
    ``ATTEMPTS_EXHAUSTED``. ``standing`` is how a log of an earlier version leaves each step:
    ``PREPARED`` has not been sent, and ``DISPATCHED`` or ``RECONCILING`` was in flight with no
    attempt in this log, so its work may exist.
    """
    descriptor = core.OperationDescriptor(
        kind="test.query",
        lifecycle=core.LifecycleClass.QUERY,
        request_schema=core.SchemaRef(name="query", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
    )
    registry = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=FakeQuery, outcome_model=FakeQueryOutcome
            ),
        )
    )
    state = core.initial_state()
    bound = state.run.limits.request_retries.model_copy(update={"limit": retries})
    run = state.run.model_copy(
        update={
            "capabilities": core.Capabilities(operations=registry.descriptors),
            "limits": state.run.limits.model_copy(update={"request_retries": bound}),
        }
    )
    state = state.model_copy(update={"registry": registry.descriptors, "run": run})
    scope = core.Scope(owner=state.run.run_id, generation=0)
    for index, limit in enumerate(limits_s):
        decision = registry.validate_decision(
            core.Operation(
                decision_id=core.DecisionId(root=f"d:{index}"),
                scope=scope,
                request=FakeQuery(value=index),
            )
        )
        submitted = core.DecisionSubmitted(
            decision=core.Wait(request=decision, until=core.Within(limit=limit)),
            expected_revision=state.revision,
        )
        state = core.step(state, submitted).state
    if standing is not core.RequestPhase.PREPARED:
        sent = tuple(
            row.model_copy(
                update={"phase": standing, "dispatched_at": 0.0, "until": core.Until(at=limit)}
            )
            for row, limit in zip(state.records.records, limits_s, strict=True)
        )
        records = state.records.model_copy(update={"records": sent})
        state = state.model_copy(update={"records": records})
    tags = tuple(row.request_id for row in state.records.records if row.request_id is not None)
    return FakeRunSeed(state, registry, tags)
