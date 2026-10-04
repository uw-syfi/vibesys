"""Exit-at-any-point properties of the dynamic loop's async shell and planner driver."""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.orchestration.dynamic.agent_loop import AgentLoop, DriverStep
from vibesys.orchestration.dynamic.control import (
    Accepted,
    HostCore,
    HostLimits,
    Refused,
    SearchEnd,
    StopReason,
    Withdrawal,
    WorkerOutcome,
    WorkItem,
)
from vibesys.orchestration.dynamic.planner_driver import PlannerDriver

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from vibesys.orchestration.dynamic.control import HostEvent

# How one worker attempt ends.
_ATTEMPTS = st.sampled_from(["ok", "retry", "exhausted", "fatal", "refunded"])


class _AttemptFailedError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _InjectedError(Exception):
    """A stop, driver crash or worker crash the test scheduled."""


@dataclass
class _FakeWorkers:
    """Workers whose attempts follow a script; every attempt records its exit."""

    attempts: dict[int, list[str]]
    core: HostCore[int] | None = None
    started: list[int] = field(default_factory=list)
    exited: list[int] = field(default_factory=list)
    started_after_stop: list[int] = field(default_factory=list)
    given_up: list[int] = field(default_factory=list)
    hang: asyncio.Event = field(default_factory=asyncio.Event)
    # Set once ``expected`` attempts have started.
    expected: int = 0
    all_started: asyncio.Event = field(default_factory=asyncio.Event)
    # Cluster jobs each plan's attempts submitted that nothing released yet.
    live_jobs: dict[int, int] = field(default_factory=dict)
    withdrawn: dict[int, Withdrawal] = field(default_factory=dict)
    kept_work: list[int] = field(default_factory=list)
    settled: list[tuple[int, Withdrawal]] = field(default_factory=list)
    started_after_withdrawal: list[int] = field(default_factory=list)

    async def _attempt(self, plan: int) -> None:
        try:
            script = self.attempts.setdefault(plan, [])
            kind = script.pop(0) if script else "ok"
            self.live_jobs[plan] = self.live_jobs.get(plan, 0) + 1
            if kind == "hang":
                await self.hang.wait()
            if kind not in {"ok", "refunded"}:
                raise _AttemptFailedError(kind)
        finally:
            if plan in self.withdrawn:
                self.kept_work.append(plan)
            self.exited.append(plan)

    def run_worker(self, plan: int) -> Coroutine[object, object, None]:
        assert self.core is not None
        if self.core.stopped is not None:
            self.started_after_stop.append(plan)
        if plan in self.withdrawn:
            self.started_after_withdrawal.append(plan)
        self.started.append(plan)
        if len(self.started) >= self.expected:
            self.all_started.set()
        return self._attempt(plan)

    async def classify(self, plan: int, error: BaseException | None) -> WorkerOutcome:
        del plan
        if error is None:
            return WorkerOutcome.COMPLETED
        assert isinstance(error, _AttemptFailedError)
        return {
            "retry": WorkerOutcome.RETRYABLE,
            "exhausted": WorkerOutcome.EXHAUSTED,
            "fatal": WorkerOutcome.FATAL,
        }[error.kind]

    async def give_up(self, plan: int) -> None:
        self.given_up.append(plan)

    def can_withdraw(self, worker_id: str) -> bool:
        del worker_id
        return True

    async def withdraw(self, plan: int, withdrawal: Withdrawal) -> None:
        self.withdrawn[plan] = withdrawal

    async def settle(self, plan: int, withdrawal: Withdrawal) -> None:
        self.settled.append((plan, withdrawal))
        # Release: the cluster cancels every job this plan's attempts left.
        self.live_jobs[plan] = 0


@dataclass
class _ScriptedPlanner:
    """Plans from a script of batch sizes; may fail at a scheduled checkpoint or turn."""

    batches: list[int]
    stop_at_checkpoint: int | None
    crash_turns: frozenset[int]
    checkpoints: int = 0
    longest_fault_run: int = 0
    _fault_run: int = 0
    turns: int = 0
    next_plan: int = 100

    async def land_stop(self) -> None:
        self.checkpoints += 1
        if self.checkpoints == self.stop_at_checkpoint:
            raise _InjectedError("stop")

    async def plan(self, capacity: int, in_flight: frozenset[str]) -> tuple[WorkItem[int], ...]:
        self.turns += 1
        assert capacity > 0
        if self.turns in self.crash_turns:
            self._fault_run += 1
            self.longest_fault_run = max(self.longest_fault_run, self._fault_run)
            raise _InjectedError("driver")
        self._fault_run = 0
        size = min(capacity, self.batches.pop(0) if self.batches else 0)
        items = tuple(WorkItem(f"p{self.next_plan + n}", self.next_plan + n) for n in range(size))
        self.next_plan += size
        assert not {item.worker_id for item in items} & in_flight
        return items


def _clock() -> float:
    return 0.0


@dataclass
class _QuiescenceCheck:
    """The planner driver, checked each time it chooses to wait for a worker."""

    inner: PlannerDriver[int]
    waits: int = 0

    async def checkpoint(self) -> None:
        await self.inner.checkpoint()

    def observe(self, event: HostEvent) -> None:
        self.inner.observe(event)

    def next_step(self, core: HostCore[int]) -> DriverStep:
        step = self.inner.next_step(core)
        if step is DriverStep.WAIT:
            # r19: a free slot with budget left idled 19.8 slot-minutes
            # waiting for a sibling; waiting is only right with no turn due.
            assert not core.wants_turn
            assert core.running
            self.waits += 1
        return step

    async def turn(self, core: HostCore[int]) -> tuple[WorkItem[int], ...]:
        return await self.inner.turn(core)


@dataclass(frozen=True)
class _Scenario:
    max_in_flight: int
    budget: int
    recovered: list[list[str]]
    planned: list[list[str]]
    batches: list[int]
    stop_at_checkpoint: int | None
    crash_turns: frozenset[int]
    turn_attempts: int


_SCENARIOS = st.builds(
    _Scenario,
    max_in_flight=st.integers(1, 3),
    budget=st.integers(0, 8),
    recovered=st.lists(st.lists(_ATTEMPTS, max_size=3), max_size=4),
    planned=st.lists(st.lists(_ATTEMPTS, max_size=3), max_size=8),
    batches=st.lists(st.integers(0, 3), max_size=6),
    stop_at_checkpoint=st.none() | st.integers(1, 5),
    crash_turns=st.frozensets(st.integers(1, 6), max_size=4),
    turn_attempts=st.integers(1, 3),
)


@given(scenario=_SCENARIOS)
def test_every_exit_settles_started_work_and_starts_none_after_a_stop(
    scenario: _Scenario,
) -> None:
    max_in_flight, budget = scenario.max_in_flight, scenario.budget
    recovered, planned = scenario.recovered, scenario.planned
    batches = list(scenario.batches)
    stop_at_checkpoint = scenario.stop_at_checkpoint
    attempts = {n: list(script) for n, script in enumerate(recovered)}
    attempts |= {100 + n: list(script) for n, script in enumerate(planned)}
    workers = _FakeWorkers(attempts)
    planner = _ScriptedPlanner(batches, stop_at_checkpoint, scenario.crash_turns)
    core: HostCore[int] = HostCore(
        HostLimits(
            max_in_flight=max_in_flight,
            start_budget=budget,
            turn_attempts=scenario.turn_attempts,
        )
    )
    workers.core = core
    driver = _QuiescenceCheck(PlannerDriver[int](plan=planner.plan, land_stop=planner.land_stop))
    loop = AgentLoop(core, driver, workers, clock=_clock)
    items = tuple(WorkItem(f"r{n}", n) for n in range(len(recovered)))

    async def run() -> SearchEnd | BaseException:
        try:
            return await loop.run(items)
        except (_InjectedError, _AttemptFailedError) as error:
            return error

    result = asyncio.run(run())

    # Every attempt that started also exited, exactly once.
    assert sorted(workers.exited) == sorted(workers.started)
    assert workers.started_after_stop == []
    assert core.ended
    assert not core.running
    match result:
        case SearchEnd.FINISHED:
            assert core.stopped is None
        case _InjectedError() | _AttemptFailedError():
            assert core.stopped is not None
        case _:
            pytest.fail(f"unexpected result {result!r}")
    if isinstance(result, _AttemptFailedError):
        assert core.stopped is StopReason.WORKER_FAILED
    # A faulted turn is retried; only a run of ``turn_attempts`` faults ends the search.
    exhausted = planner.longest_fault_run >= scenario.turn_attempts
    assert (core.stopped is StopReason.TURN_FAULTS_EXHAUSTED) == exhausted
    if exhausted:
        assert isinstance(result, _InjectedError)
        assert str(result) == "driver"


@given(
    max_in_flight=st.integers(1, 3),
    workers_before_cancel=st.integers(1, 4),
)
def test_cancelling_the_loop_cancels_every_in_flight_worker_once(
    max_in_flight: int, workers_before_cancel: int
) -> None:
    attempts = {n: ["hang"] for n in range(8)}
    workers = _FakeWorkers(attempts, expected=min(max_in_flight, workers_before_cancel))
    core: HostCore[int] = HostCore(HostLimits(max_in_flight=max_in_flight, start_budget=0))
    workers.core = core
    planner = _ScriptedPlanner([], None, frozenset())
    driver = PlannerDriver[int](plan=planner.plan, land_stop=planner.land_stop)
    loop = AgentLoop(core, driver, workers, clock=_clock)
    items = tuple(WorkItem(f"r{n}", n) for n in range(workers_before_cancel))

    async def run() -> None:
        task = asyncio.create_task(loop.run(items))
        await workers.all_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())

    assert sorted(workers.exited) == sorted(workers.started)
    assert len(workers.started) == min(max_in_flight, workers_before_cancel)


@given(
    max_in_flight=st.integers(1, 3),
    budget=st.integers(0, 3),
    turn_attempts=st.integers(1, 3),
)
def test_a_planner_that_leaves_slots_free_is_asked_again_within_the_bound(
    max_in_flight: int, budget: int, turn_attempts: int
) -> None:
    """r19: a dropped plan idled a free slot until a sibling finished.

    A turn that leaves a slot free is followed by another turn at once, up to
    ``turn_attempts`` turns in a row; then the search ends without spinning.
    """
    core: HostCore[int] = HostCore(
        HostLimits(max_in_flight=max_in_flight, start_budget=budget, turn_attempts=turn_attempts)
    )
    capacities: list[int] = []

    async def no_plans(capacity: int, in_flight: frozenset[str]) -> tuple[WorkItem[int], ...]:
        del in_flight
        capacities.append(capacity)
        return ()

    async def no_stop() -> None:
        return None

    driver = PlannerDriver[int](plan=no_plans, land_stop=no_stop)
    workers = _FakeWorkers({})
    workers.core = core
    end = asyncio.run(AgentLoop(core, driver, workers, clock=_clock).run(()))

    assert end is SearchEnd.FINISHED
    assert capacities == ([min(max_in_flight, budget)] * turn_attempts if budget else [])
    assert driver.next_step(core) is DriverStep.FINISH


@dataclass
class _WithdrawingDriver:
    """The planner driver, plus turns that park or cancel an in-flight worker."""

    inner: PlannerDriver[int]
    script: list[tuple[int, Withdrawal]]
    loop: AgentLoop[int] | None = None
    results: dict[str, list[Accepted | Refused]] = field(default_factory=dict)
    turns: int = 0
    cancel_after: int | None = None
    cancel_point: asyncio.Event = field(default_factory=asyncio.Event)
    _withdrawing: bool = False

    async def checkpoint(self) -> None:
        await self.inner.checkpoint()

    def observe(self, event: HostEvent) -> None:
        self.inner.observe(event)

    def next_step(self, core: HostCore[int]) -> DriverStep:
        in_flight = core.running or core.queued
        self._withdrawing = bool(self.script and in_flight and core.stopped is None)
        if self._withdrawing:
            return DriverStep.TURN
        return self.inner.next_step(core)

    async def turn(self, core: HostCore[int]) -> tuple[WorkItem[int], ...]:
        self.turns += 1
        if self.turns == self.cancel_after:
            self.cancel_point.set()
        if not self._withdrawing:
            return await self.inner.turn(core)
        assert self.loop is not None
        pick, withdrawal = self.script.pop(0)
        targets = sorted({*core.running, *core.queued})
        worker_id = targets[pick % len(targets)]
        result = await self.loop.withdraw(worker_id, withdrawal)
        self.results.setdefault(worker_id, []).append(result)
        if pick % 2:
            # A second request for the same worker is refused and changes nothing.
            again = await self.loop.withdraw(worker_id, withdrawal)
            assert isinstance(again, Refused)
        return ()


_WITHDRAWALS = st.lists(st.tuples(st.integers(0, 7), st.sampled_from(Withdrawal)), max_size=6)


@given(
    scenario=_SCENARIOS,
    withdrawals=_WITHDRAWALS,
    cancel_after=st.none() | st.integers(1, 6),
)
def test_a_withdrawal_at_any_point_releases_jobs_once_and_settles_once(
    scenario: _Scenario,
    withdrawals: list[tuple[int, Withdrawal]],
    cancel_after: int | None,
) -> None:
    """Park or cancel at any point of a worker's life, then any exit of the loop.

    Every accepted withdrawal stops the worker (which keeps its work), never
    restarts it, frees its slot, and settles it exactly once, which releases
    its cluster jobs exactly once; a loop cancelled before the core settles
    it settles it on the way out.
    """
    attempts = {n: list(script) for n, script in enumerate(scenario.recovered)}
    attempts |= {100 + n: list(script) for n, script in enumerate(scenario.planned)}
    workers = _FakeWorkers(attempts)
    planner = _ScriptedPlanner(
        list(scenario.batches), scenario.stop_at_checkpoint, scenario.crash_turns
    )
    core: HostCore[int] = HostCore(
        HostLimits(
            max_in_flight=scenario.max_in_flight,
            start_budget=scenario.budget,
            turn_attempts=scenario.turn_attempts,
        )
    )
    workers.core = core
    driver = _WithdrawingDriver(
        PlannerDriver[int](plan=planner.plan, land_stop=planner.land_stop),
        list(withdrawals),
        cancel_after=cancel_after,
    )
    loop = AgentLoop(core, driver, workers, clock=_clock)
    driver.loop = loop
    items = tuple(WorkItem(f"r{n}", n) for n in range(len(scenario.recovered)))

    async def run() -> None:
        task = asyncio.create_task(loop.run(items))
        cancel = asyncio.create_task(driver.cancel_point.wait())
        await asyncio.wait({task, cancel}, return_when=asyncio.FIRST_COMPLETED)
        if not task.done():
            task.cancel()
        cancel.cancel()
        with contextlib.suppress(_InjectedError, _AttemptFailedError, asyncio.CancelledError):
            await task

    asyncio.run(run())

    accepted = {
        int(worker_id[1:])
        for worker_id, results in driver.results.items()
        if any(isinstance(result, Accepted) for result in results)
    }
    for results in driver.results.values():
        assert sum(isinstance(result, Accepted) for result in results) <= 1
    # Every accepted withdrawal settles exactly once, and only those do.
    assert sorted(plan for plan, _ in workers.settled) == sorted(accepted)
    for plan, withdrawal in workers.settled:
        assert workers.live_jobs.get(plan, 0) == 0, "jobs not released"
        assert workers.withdrawn.get(plan, withdrawal) is withdrawal
    # A stopped attempt keeps its work; a withdrawn worker never starts again.
    assert set(workers.kept_work) <= set(workers.withdrawn) <= accepted
    assert workers.started_after_withdrawal == []
    # Every attempt that ran exited once; one withdrawn before its first step never ran.
    unfinished = Counter(workers.started) - Counter(workers.exited)
    assert not Counter(workers.exited) - Counter(workers.started)
    assert set(unfinished) <= set(workers.withdrawn)
    assert all(count == 1 for count in unfinished.values())
    assert workers.started_after_stop == []
