"""An in-memory ``StepLog``: core's own records, appended through the confirmed log and folded.

Nothing about the log is invented here: every method appends the record core defines for it
through ``vs_runlog.ConfirmedLog`` and folds it with ``vs_core.api.recovery.fold``, so the state
is what a real run.log would give. A record the fold refuses is never appended. Two things core
has no record for yet are stood in for and marked as such (see ``run_ended`` and ``resume_run``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

import vs_core.api as core
from vs_core.api import RequestPhase, RunStatus
from vs_core.api.recovery import (
    finished_message,
    fold,
    input_message,
    interrupted_message,
    started_message,
)
from vs_proto.api import (
    AttemptInterrupted,
    CoreInput,
    EpochStarted,
    Record,
    StepFinished,
    StepStarted,
    Timestamp,
)
from vs_runlog.api import Appended, ConfirmedLog
from vs_runtime._step_loop import OpenStep
from vs_sim.api.testing import Changes

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreEvent, CoreState, OperationRegistry, RequestId
    from vs_core.api.recovery import End, Interruption, Run, StepResult
    from vs_sim.api import Clock

_OPEN = (RequestPhase.PREPARED, RequestPhase.DISPATCHED, RequestPhase.RECONCILING)

type _Message = EpochStarted | CoreInput | StepStarted | StepFinished | AttemptInterrupted


class FakeLogUncertainError(Exception):
    """The in-memory log reported an append as uncertain, which its writer never does."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the in-memory run.log could not confirm an append")


class FakeRunRecords:
    """The durable side of a run.log, kept in memory: what survives the death of a host.

    ``ends`` and ``resumes`` are stand-ins for records core does not define yet. Both are
    durable, so a restarted log sees them as a real one would see a record.
    """

    def __init__(self) -> None:
        """Start with an empty log."""
        self.records: list[Record] = []
        self.ends: list[End] = []
        """Each end of the run a loop recorded; a faithful loop records one."""
        self.resumes = 0
        """How many times an operator resumed a parked run."""
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
        """The step ``tag`` as the state has it, while it has no result."""
        record = self._state.records.record(tag)
        if record is None or record.phase not in _OPEN:
            return None
        attempts = self._state.steps.of(tag)
        started = 0 if attempts is None else attempts.started
        return OpenStep(record.request, record.until.at, started)

    async def step_started(self, run: Run) -> None:
        """Append ``StepStarted``."""
        self._append(started_message(run))

    async def step_finished(self, tag: RequestId, attempt: int, result: StepResult) -> None:
        """Append ``StepFinished``."""
        self._append(finished_message(tag, attempt, result))

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Append ``AttemptInterrupted``."""
        self._append(interrupted_message(tag, attempt, why))

    async def run_ended(self, end: End) -> None:
        """Stand-in: core has no record of a run's end, so the end is kept beside the records.

        The state then reads ``TERMINAL``, which is what ``at_rest`` looks at.
        """
        self._records.ends.append(end)
        self._records.changes.notify()
        self._state = self._with_standins(self._state)

    async def next_input(self) -> None:
        """Return once ``push`` was called since this last returned."""
        await self._inputs.changed(self._inputs_seen)
        self._inputs_seen = self._inputs.version

    def resume_run(self) -> None:
        """Stand-in: an operator resumes a parked run (core reaches this through its recovery).

        The state is then running again (closing, if a stop was committed meanwhile), with the pause lifted.
        """
        self._records.resumes += 1
        self._state = self._with_standins(self._state)

    def _refolded(self) -> CoreState:
        folded = fold(self._records.read(), self._seed, registry=self._registry)
        return self._with_standins(folded)

    def _with_standins(self, state: CoreState) -> CoreState:
        run = state.run
        if self._records.resumes and run.status in (RunStatus.PARKED, RunStatus.DRAINING):
            resumed = RunStatus.CLOSING if run.result is not None else RunStatus.RUNNING
            run = run.model_copy(update={"status": resumed, "park": None})
        if self._records.ends:
            run = run.model_copy(update={"status": RunStatus.TERMINAL})
        return state.model_copy(update={"run": run})

    def _append(self, message: _Message) -> None:
        """Fold the next record first, so a record the fold refuses is never appended."""
        at, clock = Timestamp(), self._clock.now()
        scratch = ConfirmedLog(_Scratch(self._records.read()))
        built = scratch.append(message, epoch=self.epoch, at=at, clock=clock)
        if not isinstance(built, Appended):
            raise FakeLogUncertainError
        folded = fold([built.record], self._state, registry=self._registry)
        if not isinstance(self._log.extend(built.record), Appended):
            raise FakeLogUncertainError
        self._state = self._with_standins(folded)


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


def fake_run_seed(limits_s: Sequence[float], retries: int) -> FakeRunSeed:
    """A run with one open step per entry of ``limits_s``, each allowed that many seconds.

    ``retries`` is the bound on re-sends of a step after its first attempt (core's
    ``Limits.request_retries``): a step whose charged attempts pass it ends as
    ``ATTEMPTS_EXHAUSTED``.
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
    tags = tuple(row.request_id for row in state.records.records if row.request_id is not None)
    return FakeRunSeed(state, registry, tags)
