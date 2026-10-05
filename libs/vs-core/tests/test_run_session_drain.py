"""Run-owned sessions are closed once a stop commits, however controls and answers interleave.

A run-owned session (the planner's) belongs to no attempt, so attempt retirement never
closes it. The first committed Stop is the authority that does, but every stop, cancel
and deadline control reaches the same drain, and a session can become closable only
after a drain already ran: its cancelled turn ends, or its creation is answered, later.
A generator drives the public `step` with well-formed inputs in random orders (turn
requests, stop controls and decisions, clock ticks, executor answers in any order and
repeated). Core must never raise. Once the executor has answered everything that is
still pending, every session is closed and the run is terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .test_adoption import RUN_SCOPE, reload

REPLY = '{"commit": "reply"}'
ENDINGS = (
    core.ObservationStatus.SUCCEEDED,
    core.ObservationStatus.FAILED,
    core.ObservationStatus.CANCELLED,
)

type Action = tuple[
    Literal["turn", "stop_control", "stop_decision", "tick", "answer", "answer_twice"], int, int
]


def _turn(index: int, *, reuse: bool) -> core.TurnSpec:
    return core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root=f"session-{index}"),
            role_id=core.RoleId(root="planner"),
            policy="reuse" if reuse else "fresh",
            lifetime="ephemeral",
            access=core.Access.READ_ONLY,
        ),
        invocation_id=core.InvocationId(root=f"invocation-{index}"),
        workspace=RUN_SCOPE,
        prompts=(),
        output_schema=core.SchemaRef(name="plan", version=1),
        deadline_at=100.0,
        charge_class="free",
    )


@dataclass
class Executor:
    """The far side of the core: answers whatever requests the core has issued."""

    state: core.CoreState
    issued: list[core.Request] = field(default_factory=list)
    sequences: dict[str, int] = field(default_factory=dict)
    authorized: set[str] = field(default_factory=set)
    turns: int = 0
    stops: int = 0
    now: float = 1.0

    def feed(self, event: core.CoreEvent) -> None:
        transition = core.step(self.state, event)
        self.state = reload(transition.state)
        self.issued.extend(transition.requests)

    def pending(self) -> tuple[core.Request, ...]:
        return core.pending_requests(self.state.intents)

    def request_turn(self, *, reuse: bool) -> None:
        self.turns += 1
        self.feed(
            core.DecisionSubmitted(
                decision=core.RequestTurn(
                    decision_id=core.DecisionId(root=f"turn-{self.turns}"),
                    scope=RUN_SCOPE,
                    turn=_turn(self.turns, reuse=reuse),
                ),
                expected_revision=self.state.revision,
            )
        )

    def stop_control(self, variant: int) -> None:
        """An operator stop or the deadline stop: its own identity, its own result."""
        self.stops += 1
        self.now += 1
        self.feed(
            core.RunControlEvent(
                control=core.ControlInput(
                    control_id=core.ControlId(root=f"control-{self.stops}"), action="stop"
                ),
                now_at=self.now,
                result=core.RunResultProposal(outcome="cancelled", reason=f"stop {variant}"),
            )
        )

    def stop_decision(self, mode: Literal["drain", "cancel"]) -> None:
        self.stops += 1
        self.feed(
            core.DecisionSubmitted(
                decision=core.Stop(
                    decision_id=core.DecisionId(root=f"stop-{self.stops}"),
                    scope=RUN_SCOPE,
                    mode=mode,
                    result=core.RunResultProposal(outcome="cancelled", reason="requested"),
                ),
                expected_revision=self.state.revision,
            )
        )

    def tick(self, seconds: int) -> None:
        self.now += seconds
        self.feed(core.ClockAdvanced(now_at=self.now))

    def observation(
        self,
        request: core.Request,
        status: core.ObservationStatus,
        *,
        resource_id: core.ResourceId | None = None,
        released: bool = False,
        children_complete: bool = False,
    ) -> core.Observation:
        assert request.request_id is not None
        key = request.request_id.root
        self.sequences[key] = self.sequences.get(key, 0) + 1
        return core.Observation(
            event_id=core.EventId(root=f"{key}:{self.sequences[key]}"),
            request_id=request.request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=self.sequences[key],
            observed_at=self.now,
            status=status,
            accepted=True,
            terminal=True,
            resource_id=resource_id,
            released=released,
            children_complete=children_complete,
        )

    def answer(self, request: core.Request, status: core.ObservationStatus, *, twice: bool) -> None:
        assert request.request_id is not None
        if request.request_id.root not in self.authorized:
            self.authorized.add(request.request_id.root)
            self.feed(core.DispatchAuthorized(request_id=request.request_id))
        events = self.answers(request, status)
        for event in (*events, *events) if twice else events:
            self.feed(event)

    def answers(
        self, request: core.Request, status: core.ObservationStatus
    ) -> list[core.CoreEvent]:
        """What the executor reports for one request, owner events included."""
        if isinstance(request, core.EnsureSession):
            resource = core.ResourceId(root=f"conversation-{request.spec.session_id.root}")
            seen = self.observation(request, core.ObservationStatus.SUCCEEDED, resource_id=resource)
            return [core.RequestObserved(observation=seen)]
        if isinstance(request, core.DispatchTurn):
            seen = self.observation(
                request,
                status,
                resource_id=core.ResourceId(
                    root=f"conversation-{request.turn.session.session_id.root}"
                ),
                released=True,
                children_complete=True,
            )
            owner = core.TurnObserved(
                invocation=core.InvocationRef(
                    session_id=request.turn.session.session_id,
                    invocation_id=request.turn.invocation_id,
                    generation=request.scope.generation,
                ),
                observation=seen,
                output_schema=request.turn.output_schema,
                output_json=REPLY if status == core.ObservationStatus.SUCCEEDED else None,
            )
            return [core.RequestObserved(observation=seen), owner]
        if isinstance(request, core.CloseSession):
            seen = self.observation(
                request,
                core.ObservationStatus.SUCCEEDED,
                resource_id=request.resource_id,
                released=True,
                children_complete=True,
            )
            return [core.RequestObserved(observation=seen)]
        return [
            core.RequestObserved(
                observation=self.observation(request, core.ObservationStatus.SUCCEEDED)
            )
        ]


actions = st.one_of(
    st.tuples(st.just("turn"), st.integers(0, 1), st.just(0)),
    st.tuples(st.just("stop_control"), st.integers(0, 2), st.just(0)),
    st.tuples(st.just("stop_decision"), st.integers(0, 1), st.just(0)),
    st.tuples(st.just("tick"), st.integers(0, 3), st.just(0)),
    st.tuples(st.just("answer"), st.integers(0, 20), st.integers(0, len(ENDINGS) - 1)),
    st.tuples(st.just("answer_twice"), st.integers(0, 20), st.integers(0, len(ENDINGS) - 1)),
)


def _drive(executor: Executor, plan: list[Action]) -> None:
    for kind, index, other in plan:
        pending = executor.pending()
        if kind == "turn":
            executor.request_turn(reuse=bool(index))
        elif kind == "stop_control":
            executor.stop_control(index)
        elif kind == "stop_decision":
            executor.stop_decision("drain" if index else "cancel")
        elif kind == "tick":
            executor.tick(index)
        elif pending:
            executor.answer(
                pending[index % len(pending)], ENDINGS[other], twice=kind == "answer_twice"
            )


def _settle(executor: Executor) -> None:
    """The executor finishes everything still pending, then the clock moves on."""
    for _ in range(200):
        pending = executor.pending()
        if not pending:
            break
        executor.answer(pending[0], core.ObservationStatus.SUCCEEDED, twice=False)
    executor.tick(1)


@settings(max_examples=300, deadline=None)
@example(plan=[("turn", 0, 0)], final_stop=0)  # stop while the session is still being created
@given(plan=st.lists(actions, min_size=1, max_size=30), final_stop=st.integers(0, 2))
def test_every_run_owned_session_is_closed_and_the_run_ends_after_any_control_sequence(
    plan: list[Action], final_stop: int
) -> None:
    executor = Executor(core.initial_state())
    _drive(executor, plan)
    executor.stop_control(final_stop)
    _settle(executor)

    identities = [request.request_id for request in executor.issued]
    assert len(identities) == len(set(identities)), "a request was issued twice"
    assert executor.pending() == ()
    assert {row.phase for row in executor.state.sessions.sessions} <= {core.SessionPhase.TERMINAL}
    assert executor.state.run.status == core.RunStatus.TERMINAL
