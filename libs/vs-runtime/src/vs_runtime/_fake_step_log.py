"""An in-memory ``StepLog``: the log of a real run over a run.log kept in memory.

It is ``RunLogStepLog``, the log production runs, over a writer that keeps its records in a
list, so it is never more forgiving than the real one: the same records are appended through
the confirmed log and folded with ``vs_core.api.recovery``, and a record the fold refuses is
never appended. Building one over the same ``FakeRunRecords`` is a restart: the next epoch's
header is appended, and the state is the fold of everything the earlier processes left.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Literal

import vs_core.api as core
from vs_core.api.recovery import Folded, fold_run
from vs_proto.api import EpochStarted, Record, Timestamp
from vs_runlog.api import ConfirmedLog
from vs_runtime._run_log_step_log import RunLogStepLog
from vs_sim.api.testing import Changes

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_core.api import CoreEvent, CoreState, OperationRegistry, RequestId
    from vs_sim.api import Clock, Threads


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


class FakeStepLog(RunLogStepLog):
    """One process's view of a run.log kept in memory: it opens a new epoch, then appends and folds.

    The run starts from ``seed``, given here and not recorded, as a test builds a run with open
    steps; ``RunLogStepLog.open`` is the way that records the start.
    """

    def __init__(
        self,
        records: FakeRunRecords,
        seed: CoreState,
        registry: OperationRegistry,
        clock: Clock,
        threads: Threads,
    ) -> None:
        """Fold ``records`` over ``seed`` and open the next epoch; ``threads`` locks its appends."""
        confirmed = ConfirmedLog(records, threads)
        epoch = (records.records[-1].epoch if records.records else 0) + 1
        confirmed.append(EpochStarted(epoch=epoch), epoch=epoch, at=Timestamp(), clock=clock.now())
        folded = fold_run(confirmed.records(), Folded(seed), registry=registry)
        super().__init__(confirmed, folded, registry, clock, epoch=epoch, search=None)
        self._operator = 0

    @property
    def epoch(self) -> int:
        """The epoch this process writes."""
        return self._epoch

    def push(self, event: CoreEvent) -> None:
        """Append an input that arrived from outside the loop, and wake ``next_input``."""
        self.input(event)

    def resume_run(self) -> None:
        """The operator resumes a parked run, in the epoch that follows its park."""
        self._operator += 1
        control = core.ControlInput(
            control_id=core.ControlId(root=f"operator-resume-{self.epoch}-{self._operator}"),
            action="resume",
            when="now",
        )
        self.push(core.RunControlEvent(control=control, now_at=self._clock.now()))


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
