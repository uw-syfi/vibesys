"""`step` raises a contract error only for a malformed external input.

A generator drives the public `step` with well-formed external inputs in random
orders: attempt starts in every workspace mode, winner proposals, write turns and
measurements, executor answers to whatever requests the core issued, clock ticks and a
stop. An executor answers a turn with its observation and the owner's reply event, in
either order and repeated, and a measurement with a running view and then the job's end.
Every
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
from .test_measurements import plan
from .test_scheduling import queued_root_behind_full_slot

MODES = tuple(core.WorkspaceMode)
_ENDINGS = (
    core.ObservationStatus.SUCCEEDED,
    core.ObservationStatus.FAILED,
    core.ObservationStatus.CANCELLED,
)

type Action = tuple[
    Literal["start", "propose", "answer", "tick", "stop", "release", "turn", "measure"], int, int
]

REPLY = '{"commit": "reply"}'
JOB = core.ResourceId(root="job")


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
    turned: int = 0
    measured: int = 0
    now: float = 1.0
    results: list[core.TurnResult] = field(default_factory=list)

    def feed(self, event: core.CoreEvent) -> None:
        transition = core.step(self.state, event)
        self.state = reload(transition.state)
        self.requests.extend(transition.requests)
        self.results.extend(row for row in transition.events if isinstance(row, core.TurnResult))

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

    def turn(self, index: int) -> None:
        """A write turn on one of the attempts, whose reply only the owner event carries."""
        owners = self.state.attempts.attempts
        if not owners:
            return
        self.turned += 1
        attempt = Session.attempt_scope(owners[index % len(owners)])
        self.decide(
            core.RequestTurn(
                decision_id=core.DecisionId(root=f"turn-{self.turned}"),
                scope=attempt,
                turn=core.TurnSpec(
                    session=core.SessionSpec(
                        session_id=core.SessionId(root=f"session-{self.turned}"),
                        role_id=core.RoleId(root="implementer"),
                        policy="fresh",
                        lifetime="owner",
                        access=core.Access.WRITE_CANDIDATE,
                    ),
                    invocation_id=core.InvocationId(root=f"invocation-{self.turned}"),
                    workspace=core.WorkspaceRef(
                        scope=attempt,
                        revision=self.state.run.facts.baseline,
                        mode=core.WorkspaceMode.ISOLATED_CHILD,
                    ),
                    prompts=(),
                    output_schema=core.SchemaRef(name="implementation", version=1),
                    deadline_at=self.state.run.deadline_at,
                    charge_class="paid",
                ),
            )
        )

    @staticmethod
    def attempt_scope(owner: core.AttemptView) -> core.Scope:
        return core.Scope(owner=owner.attempt_id, generation=owner.generation)

    def measure(self) -> None:
        self.measured += 1
        self.decide(
            core.Measure(
                decision_id=core.DecisionId(root=f"measure-{self.measured}"),
                scope=RUN_SCOPE,
                plan=plan(evaluator_digest=f"evaluator-{self.measured}"),
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
        elif isinstance(request, core.DispatchTurn | core.ResumeSessionTurn):
            self.answer_turn(request, observation, observation_first=revision == 0)
        elif isinstance(request, core.SubmitMeasurement):
            self.answer_submit(observation)
        else:
            self.feed(core.RequestObserved(observation=observation))

    def answer_turn(
        self,
        request: core.DispatchTurn | core.ResumeSessionTurn,
        seen: core.Observation,
        *,
        observation_first: bool,
    ) -> None:
        """The executor's two answers to one turn, in either order, the owner's repeated."""
        owner = core.TurnObserved(
            invocation=core.InvocationRef(
                session_id=request.turn.session.session_id,
                invocation_id=request.turn.invocation_id,
                generation=request.scope.generation,
            ),
            observation=seen,
            output_schema=request.turn.output_schema,
            output_json=REPLY,
        )
        events: list[core.CoreEvent] = [core.RequestObserved(observation=seen), owner, owner]
        if not observation_first:
            events.insert(0, events.pop(1))
        for event in events:
            self.feed(event)

    def answer_submit(self, seen: core.Observation) -> None:
        """A job still running at the submit view, then its end as a job observation."""
        key = seen.request_id.root
        running = seen.model_copy(
            update={
                "status": core.ObservationStatus.PENDING,
                "terminal": False,
                "resource_id": JOB,
            }
        )
        if seen.status not in _ENDINGS:
            self.feed(core.RequestObserved(observation=running))
            return
        self.sequences[key] += 1
        ended = seen.model_copy(
            update={
                "sequence": self.sequences[key],
                "event_id": core.EventId(root=f"{key}:{self.sequences[key]}"),
                "resource_id": JOB,
                "released": True,
                "children_complete": True,
            }
        )
        running = running.model_copy(update={"sequence": self.sequences[key] - 1})
        self.feed(core.RequestObserved(observation=running))
        self.feed(core.JobObserved(resource_id=JOB, observation=ended))

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
    st.tuples(st.just("turn"), st.integers(0, 3), st.just(0)),
    st.tuples(st.just("measure"), st.just(0), st.just(0)),
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
        elif kind == "turn":
            session.turn(index)
        elif kind == "measure":
            session.measure()
        else:
            session.stop("drain" if index else "cancel")
        _assert_root_exclusive(session.state)
        _assert_owner_facts_reach_their_results(session)


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


def _assert_owner_facts_reach_their_results(session: Session) -> None:
    """The reply and the job's end reach the core's records, whichever answer came first.

    Only the owner's event carries a turn's reply, so a published success without it
    means the observation pre-empted the owner event. A job that ended completes the
    submit intent that created it.
    """
    for result in session.results:
        if result.observation.status == core.ObservationStatus.SUCCEEDED:
            assert result.output_json == REPLY
    for job in session.state.evaluation.jobs:
        if job.terminal and job.observation is not None:
            intent = next(
                row for row in session.state.intents.intents if row.request_id == job.submission_id
            )
            assert intent.phase == core.IntentPhase.COMPLETED
