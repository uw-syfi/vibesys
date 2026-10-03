"""Stateful properties of the dynamic loop's deterministic host core."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from vibesys.orchestration.dynamic.control import (
    Accepted,
    Attempt,
    Effect,
    EndSearch,
    FinishSearch,
    HostCore,
    HostLimits,
    RecordGiveUp,
    Recover,
    Refusal,
    Refused,
    SearchEnd,
    StartWorker,
    StopReason,
    StopRequested,
    Submit,
    TurnFaulted,
    WorkerFinished,
    WorkerOutcome,
    WorkItem,
)

_TERMINAL = frozenset(WorkerOutcome) - {WorkerOutcome.RETRYABLE}


@dataclass
class _Model:
    """What the effects told the observer, checked against the core's own view."""

    max_in_flight: int
    budget: int
    turn_attempts: int
    turn_faults: int = 0
    idle_turns: int = 0
    stop_reason: StopReason | None = None
    running: set[str] = field(default_factory=set)
    queue: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    settled: dict[str, int] = field(default_factory=dict)
    given_up: list[str] = field(default_factory=list)
    ends: list[EndSearch] = field(default_factory=list)
    charged: int = 0
    refunded: int = 0
    stopped: bool = False
    finishing: bool = False
    now: float = 0.0
    next_id: int = 0

    def fresh(self, count: int) -> tuple[WorkItem[int], ...]:
        items = tuple(WorkItem(f"w{self.next_id + n}", self.next_id + n) for n in range(count))
        self.next_id += count
        return items


class HostCoreMachine(RuleBasedStateMachine):
    """Drive ``HostCore`` with arbitrary interleavings of actions and events."""

    def __init__(self) -> None:
        super().__init__()
        self.core: HostCore[int]
        self.model: _Model

    @initialize(
        max_in_flight=st.integers(1, 4),
        budget=st.integers(0, 12),
        recovered=st.integers(0, 6),
        turn_attempts=st.integers(1, 3),
    )
    def open_run(self, max_in_flight: int, budget: int, recovered: int, turn_attempts: int) -> None:
        self.core = HostCore(
            HostLimits(
                max_in_flight=max_in_flight, start_budget=budget, turn_attempts=turn_attempts
            )
        )
        self.model = _Model(max_in_flight, budget, turn_attempts)
        items = self.model.fresh(recovered)
        result, effects = self.core.on_action(Recover(items, self.model.now))
        assert isinstance(result, Accepted)
        self.model.queue.extend(result.queued)
        self._apply(effects, freed=False)

    def _tick(self, dt: float) -> float:
        self.model.now += dt
        return self.model.now

    def _apply(self, effects: tuple[Effect[int], ...], *, freed: bool) -> None:
        for effect in effects:
            match effect:
                case StartWorker(item=item, attempt=attempt):
                    assert not self.model.stopped, "work started after a stop"
                    if attempt is Attempt.RETRY:
                        assert item.worker_id in self.model.running
                        continue
                    if freed:
                        # A freed slot starts the queue head, in order.
                        assert self.model.queue, "started from an empty queue"
                        assert self.model.queue.pop(0) == item.worker_id
                    assert item.worker_id not in self.model.running
                    self.model.running.add(item.worker_id)
                    self.model.started.append(item.worker_id)
                case RecordGiveUp(item=item):
                    self.model.given_up.append(item.worker_id)
                case EndSearch():
                    expected = SearchEnd.STOPPED if self.model.stopped else SearchEnd.FINISHED
                    assert effect.end is expected
                    self.model.ends.append(effect)

    def _check_refusal(self, action: Recover[int] | Submit[int] | FinishSearch) -> None:
        before = (self.core.running, self.core.queued, self.core.remaining_budget)
        result, effects = self.core.on_action(action)
        assert isinstance(result, Refused)
        assert effects == ()
        assert (self.core.running, self.core.queued, self.core.remaining_budget) == before

    @rule(count=st.integers(0, 4), dt=st.floats(0, 600))
    def submit(self, count: int, dt: float) -> None:
        items = self.model.fresh(count)
        action = Submit(items, self._tick(dt))
        if self.model.stopped or self.model.finishing or count > self.core.remaining_budget:
            self._check_refusal(action)
            return
        result, effects = self.core.on_action(action)
        assert isinstance(result, Accepted)
        self.model.charged += count
        self.model.turn_faults = 0
        free = self._model_free()
        self.model.idle_turns = self.model.idle_turns + 1 if free > 0 else 0
        self.model.queue.extend(result.queued)
        self._apply(effects, freed=False)

    @precondition(lambda self: bool(self.model.running or self.model.queue))
    @rule(data=st.data(), dt=st.floats(0, 600))
    def submit_duplicate(self, data: st.DataObject, dt: float) -> None:
        taken = sorted(self.model.running | set(self.model.queue))
        worker_id = data.draw(st.sampled_from(taken))
        action = Submit((WorkItem(worker_id, -1),), self._tick(dt))
        if not (self.model.stopped or self.model.finishing):
            result, _ = self.core.on_action(action)
            assert result == Refused(Refusal.DUPLICATE, worker_id)
        self._check_refusal(action)

    @precondition(lambda self: bool(self.model.running))
    @rule(data=st.data(), outcome=st.sampled_from(WorkerOutcome), dt=st.floats(0, 600))
    def finish_worker(self, data: st.DataObject, outcome: WorkerOutcome, dt: float) -> None:
        worker_id = data.draw(st.sampled_from(sorted(self.model.running)))
        self.model.idle_turns = 0
        effects = self.core.on_event(WorkerFinished(worker_id, outcome, self._tick(dt)))
        restarted = any(
            isinstance(effect, StartWorker) and effect.attempt is Attempt.RETRY
            for effect in effects
        )
        assert restarted == (outcome is WorkerOutcome.RETRYABLE and not self.model.stopped)
        if not restarted:
            self.model.running.discard(worker_id)
            self.model.settled[worker_id] = self.model.settled.get(worker_id, 0) + 1
            if outcome is WorkerOutcome.REFUNDED:
                self.model.refunded += 1
            if outcome is WorkerOutcome.FATAL:
                self._stop(StopReason.WORKER_FAILED)
        self._apply(effects, freed=True)

    def _stop(self, reason: StopReason) -> None:
        if self.model.stop_reason is None:
            self.model.stop_reason = reason
        self.model.stopped = True
        self.model.queue.clear()

    @rule(reason=st.sampled_from(StopReason), dt=st.floats(0, 600))
    def stop(self, reason: StopReason, dt: float) -> None:
        self._stop(reason)
        self._apply(self.core.on_event(StopRequested(reason, self._tick(dt))), freed=False)

    @rule(dt=st.floats(0, 600))
    def turn_faulted(self, dt: float) -> None:
        self.model.turn_faults += 1
        self.model.idle_turns += 1
        exhausted = self.model.turn_faults >= self.model.turn_attempts
        if exhausted:
            self._stop(StopReason.TURN_FAULTS_EXHAUSTED)
        self._apply(self.core.on_event(TurnFaulted(self._tick(dt))), freed=False)

    @rule(dt=st.floats(0, 600))
    def finish_search(self, dt: float) -> None:
        action = FinishSearch(self._tick(dt))
        if self.model.stopped or self.model.finishing:
            self._check_refusal(action)
            return
        result, effects = self.core.on_action(action)
        assert isinstance(result, Accepted)
        self.model.finishing = True
        self._apply(effects, freed=False)

    @invariant()
    def only_a_spent_bound_or_a_stop_halts(self) -> None:
        assert self.core.stopped is self.model.stop_reason
        assert self.core.turn_faults == self.model.turn_faults

    def _model_free(self) -> int:
        if self.model.stopped or self.model.finishing:
            return 0
        free = self.model.max_in_flight - len(self.model.running) - len(self.model.queue)
        remaining = self.model.budget - self.model.charged + self.model.refunded
        return max(0, min(free, remaining))

    @invariant()
    def never_quiescent_with_a_free_slot_and_budget(self) -> None:
        """r19: a free slot with budget left and an empty queue asks for a turn at once.

        Only a spent idle-turn bound (turns in a row that faulted or left a
        slot free since the last finished worker) lets the slot wait.
        """
        free = self._model_free()
        assert self.core.free_capacity == free
        idle = free > 0 and not self.model.queue
        bound_spent = self.model.idle_turns >= self.model.turn_attempts
        assert self.core.wants_turn == (idle and not bound_spent)

    @invariant()
    def slots_never_exceed_the_limit(self) -> None:
        assert len(self.core.running) <= self.model.max_in_flight
        assert self.core.running == frozenset(self.model.running)

    @invariant()
    def queue_matches_and_waits_only_for_a_slot(self) -> None:
        assert list(self.core.queued) == self.model.queue
        if self.core.queued:
            assert len(self.core.running) == self.model.max_in_flight

    @invariant()
    def budget_is_never_exceeded(self) -> None:
        assert self.model.charged - self.model.refunded <= self.model.budget
        assert self.core.remaining_budget == (
            self.model.budget - self.model.charged + self.model.refunded
        )

    @invariant()
    def every_worker_settles_at_most_once(self) -> None:
        assert all(count == 1 for count in self.model.settled.values())
        assert len(self.model.started) == len(set(self.model.started))

    @invariant()
    def search_ends_once_when_drained(self) -> None:
        drained = not self.model.running and not self.model.queue
        should_end = drained and (self.model.stopped or self.model.finishing)
        assert len(self.model.ends) == (1 if should_end else 0)
        assert self.core.ended == should_end

    def teardown(self) -> None:
        """Drain: every started worker settles exactly once, and the search ends."""
        if not hasattr(self, "core"):
            return
        if not self.model.stopped:
            self.stop(StopReason.REQUESTED, 0.0)
        while self.model.running:
            self.finish_worker_now(sorted(self.model.running)[0])
        assert set(self.model.settled) == set(self.model.started)
        assert all(count == 1 for count in self.model.settled.values())
        assert len(self.model.ends) == 1
        assert self.core.ended

    def finish_worker_now(self, worker_id: str) -> None:
        effects = self.core.on_event(
            WorkerFinished(worker_id, WorkerOutcome.COMPLETED, self.model.now)
        )
        self.model.running.discard(worker_id)
        self.model.settled[worker_id] = self.model.settled.get(worker_id, 0) + 1
        self._apply(effects, freed=True)


HostCoreMachine.TestCase.settings = settings(max_examples=150, stateful_step_count=40)
test_host_core_state_machine = HostCoreMachine.TestCase


@given(
    max_in_flight=st.integers(1, 4),
    plans=st.integers(0, 10),
    completions=st.lists(st.integers(0, 3), max_size=20),
)
def test_freed_slots_start_the_queue_head_in_submission_order(
    max_in_flight: int, plans: int, completions: list[int]
) -> None:
    core: HostCore[int] = HostCore(HostLimits(max_in_flight=max_in_flight, start_budget=plans))
    items = tuple(WorkItem(f"w{n}", n) for n in range(plans))
    result, effects = core.on_action(Submit(items, 0.0))
    assert isinstance(result, Accepted)
    order = [effect.item.worker_id for effect in effects if isinstance(effect, StartWorker)]
    for pick in completions:
        if not core.running:
            break
        worker_id = sorted(core.running)[pick % len(core.running)]
        effects = core.on_event(WorkerFinished(worker_id, WorkerOutcome.COMPLETED, 1.0))
        order.extend(effect.item.worker_id for effect in effects if isinstance(effect, StartWorker))
    assert order == [item.worker_id for item in items[: len(order)]]


def test_a_shell_bug_is_loud() -> None:
    core: HostCore[int] = HostCore(HostLimits(max_in_flight=1, start_budget=1))
    with pytest.raises(ValueError, match="holds no slot"):
        core.on_event(WorkerFinished("ghost", WorkerOutcome.COMPLETED, 0.0))
    core.on_action(Submit((WorkItem("a", 0),), 5.0))
    with pytest.raises(ValueError, match="time ran backwards"):
        core.on_event(WorkerFinished("a", WorkerOutcome.COMPLETED, 1.0))
