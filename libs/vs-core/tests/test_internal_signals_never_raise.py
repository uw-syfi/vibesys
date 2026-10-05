"""`step` raises a contract error only for a malformed external input.

A generator drives the public `step` with well-formed external inputs in random
orders: attempt starts in every workspace mode, winner proposals, executor
answers to whatever requests the core issued, clock ticks and a stop. Every
signal one leaf produces for another is internal, so none of these runs may fail
the step, however the leaves interleave. A bad input shows up as a Rejected
event, never as an exception. This is the class behind the adoption-fence crash:
Scheduling admitted an attempt that Attempts then refused by raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hypothesis import given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .test_adoption import GOOD, RUN_SCOPE, STATUSES, reload, world
from .test_scheduling import queued_root_behind_full_slot

MODES = tuple(core.WorkspaceMode)

type Action = tuple[Literal["start", "propose", "answer", "tick", "stop", "release"], int, int]


@dataclass
class Session:
    """One strategy and executor pair; every input is well formed by construction."""

    state: core.CoreState
    requests: list[core.Request] = field(default_factory=list)
    sequences: dict[str, int] = field(default_factory=dict)
    dispatched: set[str] = field(default_factory=set)
    finished: set[str] = field(default_factory=set)
    started: int = 0
    proposed: int = 0
    now: float = 1.0

    def feed(self, event: core.CoreEvent) -> None:
        transition = core.step(self.state, event)
        self.state = reload(transition.state)
        self.requests.extend(transition.requests)

    def decide(self, decision: core.Decision) -> None:
        self.feed(core.DecisionSubmitted(decision=decision, expected_revision=self.state.revision))

    def start(self, mode: core.WorkspaceMode) -> None:
        self.started += 1
        self.decide(
            core.StartAttempt(
                decision_id=core.DecisionId(root=f"start-{self.started}"),
                scope=RUN_SCOPE,
                attempt_id=core.AttemptId(root=f"attempt-{self.started}"),
                item_id=core.ItemId(root=f"item-{self.started}"),
                workspace=core.WorkspacePlan(mode=mode, base=self.state.run.facts.baseline),
                budget=core.AttemptBudget(),
            )
        )

    def propose(self, selection: core.Selection) -> None:
        self.proposed += 1
        self.decide(
            core.ProposeWinner(
                decision_id=core.DecisionId(root=f"winner-{self.proposed}"),
                scope=RUN_SCOPE,
                selection=selection,
            )
        )

    def answer(self, request: core.Request, status: core.ObservationStatus, revision: int) -> None:
        if request.request_id is None:
            return
        key = request.request_id.root
        if key in self.finished or not self.current(request):
            return  # a terminal request never changes its disposition
        if status != core.ObservationStatus.UNKNOWN:
            self.finished.add(key)
        if key not in self.dispatched:
            self.dispatched.add(key)
            self.feed(core.DispatchAuthorized(request_id=request.request_id))
        self.sequences[key] = self.sequences.get(key, 0) + 1
        observation = core.Observation(
            event_id=core.EventId(root=f"{key}:{self.sequences[key]}"),
            request_id=request.request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=self.sequences[key],
            observed_at=self.now,
            status=status,
            accepted=True,
            terminal=status != core.ObservationStatus.UNKNOWN,
        )
        if isinstance(request, core.AdoptRevision | core.VerifyAdoption):
            adopted = request.selection.revision if revision == 0 else None
            self.feed(core.AdoptionObserved(observation=observation, revision=adopted))
        else:
            self.feed(core.RequestObserved(observation=observation))

    def current(self, request: core.Request) -> bool:
        """Whether the request's admission episode is still live.

        An executor does not report on work whose episode ended; the core rightly
        refuses such a report as a malformed external input.
        """
        owner = request.scope.owner
        if not isinstance(owner, core.AttemptId):
            return True
        return any(
            attempt.attempt_id == owner
            and attempt.admission_id == request.admission_id
            and attempt.phase in (core.AttemptPhase.ACQUIRING, core.AttemptPhase.ACTIVE)
            for attempt in self.state.attempts.attempts
        )

    def release(self) -> None:
        """The occupant of the first slot ends, as the retirement leaves report it."""
        for slot in self.state.scheduling.slots:
            self.feed(core.SlotReleased(attempt=slot.attempt, admission_id=slot.admission_id))
            return

    def tick(self, seconds: int) -> None:
        self.now += seconds
        self.feed(core.ClockAdvanced(now_at=self.now))

    def stop(self, mode: Literal["drain", "cancel"]) -> None:
        self.decide(
            core.Stop(
                decision_id=core.DecisionId(root="stop"),
                scope=RUN_SCOPE,
                mode=mode,
                result=core.RunResultProposal(outcome="cancelled", reason="requested"),
            )
        )


actions = st.one_of(
    st.tuples(st.just("start"), st.integers(0, len(MODES) - 1), st.just(0)),
    st.tuples(st.just("propose"), st.integers(0, len(GOOD) - 1), st.just(0)),
    st.tuples(st.just("answer"), st.integers(0, 60), st.integers(0, len(STATUSES) - 1)),
    st.tuples(st.just("tick"), st.integers(0, 5), st.just(0)),
    st.tuples(st.just("stop"), st.integers(0, 1), st.just(0)),
    st.tuples(st.just("release"), st.just(0), st.just(0)),
)


def test_the_generator_reaches_a_queued_exclusive_root_behind_an_adoption() -> None:
    """The vocabulary is rich enough to build the state that crashed before the fix."""
    session = Session(world().model_copy(deep=True))
    session.state = session.state.model_copy(
        update={
            "run": session.state.run.model_copy(
                update={"limits": core.Limits(max_attempts=4, max_parallel=1)}
            )
        }
    )
    session.start(core.WorkspaceMode.ISOLATED_CHILD)
    session.start(core.WorkspaceMode.EXCLUSIVE_ROOT)
    assert len(session.state.scheduling.queue) == 1
    session.propose(GOOD[2])
    assert session.state.settlement.adoption is not None


def seeded(mode: core.WorkspaceMode | None, parallel: int) -> core.CoreState:
    """The adoption world, optionally with a workspace-mode head queued behind a closing slot."""
    state = world(max_retries=1)
    limits = core.Limits(max_attempts=6, max_parallel=parallel, max_retries=1)
    if mode is not None:
        queued, _ = queued_root_behind_full_slot(mode)
        state = queued.model_copy(
            update={
                "settlement": state.settlement,
                "evaluation": state.evaluation,
                "run": queued.run.model_copy(update={"limits": limits}),
            }
        )
    return state.model_copy(update={"run": state.run.model_copy(update={"limits": limits})})


@settings(max_examples=200, deadline=None)
@given(
    plan=st.lists(actions, min_size=1, max_size=40),
    parallel=st.integers(1, 2),
    seed=st.one_of(st.none(), st.sampled_from(MODES)),
)
def test_no_interleaving_of_well_formed_inputs_fails_the_step(
    plan: list[Action], parallel: int, seed: core.WorkspaceMode | None
) -> None:
    session = Session(seeded(seed, parallel))
    for kind, index, other in plan:
        if kind == "start":
            session.start(MODES[index])
        elif kind == "propose":
            session.propose(GOOD[index])
        elif kind == "answer":
            if session.requests:
                request = session.requests[index % len(session.requests)]
                session.answer(request, STATUSES[other], index % 2)
        elif kind == "tick":
            session.tick(index)
        elif kind == "release":
            session.release()
        else:
            session.stop("drain" if index else "cancel")
        _assert_root_exclusive(session.state)


def _assert_root_exclusive(state: core.CoreState) -> None:
    """At most one admitted attempt holds the root, and none is admitted under an adoption."""
    modes = {
        (attempt.attempt_id, attempt.generation): attempt.workspace.mode
        for attempt in state.attempts.attempts
    }
    holders = [
        slot
        for slot in state.scheduling.slots
        if modes.get((slot.attempt.attempt_id, slot.attempt.generation))
        == core.WorkspaceMode.EXCLUSIVE_ROOT
    ]
    assert len(holders) <= 1
