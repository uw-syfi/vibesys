"""A closing attempt reaches terminal once its measurement jobs are, whatever way they end.

Closing an attempt that still has a measurement job running records a release dependency on the
job. The attempt then needs the job's release to arrive as an event, however the job ends
(succeeded, failed, cancelled, without evidence) and in whatever order that release and the
other closing requests are answered. Hypothesis chooses the order in which the far side answers
what core asked, how long the job keeps running, and how it ends. Every example must leave the
attempt terminal with no dependency, so its settlement can complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import example, given, settings
from hypothesis import strategies as st

import vs_core.api as core

from .test_attempt_retirement import fixture, retirement
from .test_measurements import observation, plan

JOB = core.ResourceId(root="job")
ENDS = (
    core.ObservationStatus.SUCCEEDED,
    core.ObservationStatus.FAILED,
    core.ObservationStatus.CANCELLED,
)

type Action = tuple[str, int]


@dataclass
class Cluster:
    """The far side: answers the requests core issues, one at a time, in the order it is told."""

    state: core.CoreState
    submission: core.SubmitMeasurement
    ref: core.AttemptRef
    running_polls: int
    ending: core.ObservationStatus
    polls: int = 0
    sequence: int = 1
    authorized: set[str] = field(default_factory=set)
    now: float = 1.0

    def feed(self, event: core.CoreEvent) -> None:
        self.state = core.step(self.state, event).state

    def attempt(self) -> core.AttemptView:
        (owner,) = self.state.attempts.attempts
        return owner

    def pending(self) -> tuple[core.Request, ...]:
        # The submission stays open until its job ends; the job's own events close it.
        return tuple(
            request
            for request in core.pending_requests(self.state.intents)
            if not isinstance(request, core.SubmitMeasurement)
        )

    def job_view(self) -> core.Observation:
        """What the job looks like now: running for a while, then ended and released."""
        self.sequence += 1
        ended = self.polls >= self.running_polls
        self.polls += 1
        return observation(
            self.submission,
            self.sequence,
            status=self.ending if ended else core.ObservationStatus.PENDING,
            terminal=ended,
            released=ended,
            children_complete=ended,
            resource_id=JOB,
            observed_at=self.now + self.sequence,
        )

    def completed(self, request: core.Request) -> core.Observation:
        """The request's own answer: it ran and finished, and holds nothing."""
        assert request.request_id is not None
        return observation(
            self.submission,
            1,
            status=core.ObservationStatus.SUCCEEDED,
            terminal=True,
            released=True,
            children_complete=True,
            resource_id=None,
        ).model_copy(
            update={
                "event_id": core.EventId(root=f"done:{request.request_id.root}"),
                "request_id": request.request_id,
                "scope": request.scope,
                "admission_id": request.admission_id,
            }
        )

    def answer(self, request: core.Request) -> None:
        assert request.request_id is not None
        if request.request_id.root not in self.authorized:
            self.authorized.add(request.request_id.root)
            self.feed(core.DispatchAuthorized(request_id=request.request_id))
        self.feed(core.RequestObserved(observation=self.completed(request)))
        if isinstance(
            request,
            core.ObserveOwnedJob
            | core.InspectOwnedJob
            | core.CancelOwnedJob
            | core.CollectEvidence,
        ):
            # A query or a cancel completes by itself; the job's state is the job's own event.
            self.feed(core.JobObserved(resource_id=JOB, observation=self.job_view()))

    def tick(self) -> None:
        due = core.project(self.state).next_observe_at
        self.now = max(self.state.run.now_at + 1.0, due if due is not None else 0.0)
        self.feed(core.ClockAdvanced(now_at=self.now))


def started(running_polls: int, ending: core.ObservationStatus) -> Cluster:
    """An active attempt whose measurement job is running."""
    state, ref = fixture()
    scope = core.Scope(owner=ref.attempt_id, generation=ref.generation)
    submitted = core.step(
        state,
        core.DecisionSubmitted(
            decision=core.Measure(
                decision_id=core.DecisionId(root="measure"),
                scope=scope,
                plan=plan(purpose="local-validation", candidate=state.run.facts.baseline),
            ),
            expected_revision=state.revision,
        ),
    )
    (submission,) = submitted.requests
    assert isinstance(submission, core.SubmitMeasurement)
    cluster = Cluster(submitted.state, submission, ref, running_polls, ending)
    assert submission.request_id is not None
    cluster.feed(core.DispatchAuthorized(request_id=submission.request_id))
    cluster.authorized.add(submission.request_id.root)
    first = observation(submission, 1, resource_id=JOB)
    cluster.feed(core.RequestObserved(observation=first))
    cluster.feed(core.MeasurementSubmissionObserved(observation=first))
    cluster.feed(core.JobObserved(resource_id=JOB, observation=first))
    return cluster


actions = st.one_of(
    st.tuples(st.just("answer"), st.integers(0, 6)),
    st.tuples(st.just("tick"), st.just(0)),
)


def run_out(cluster: Cluster) -> None:
    """The far side answers everything outstanding, oldest first, and the clock reaches every poll."""
    for _ in range(100):
        pending = cluster.pending()
        if pending:
            cluster.answer(pending[0])
        elif core.project(cluster.state).next_observe_at is not None:
            cluster.tick()
        else:
            return


@settings(max_examples=300, deadline=None)
@example(plan=[("answer", 0)], running_polls=1, ending=core.ObservationStatus.FAILED)
@given(
    plan=st.lists(actions, max_size=12),
    running_polls=st.integers(0, 3),
    ending=st.sampled_from(ENDS),
)
def test_an_attempt_closed_over_a_running_job_ends_terminal_whenever_the_job_is_released(
    plan: list[Action], running_polls: int, ending: core.ObservationStatus
) -> None:
    cluster = started(running_polls, ending)
    cluster.feed(retirement(cluster.ref, "cancel"))
    assert cluster.attempt().phase == core.AttemptPhase.CLOSING
    assert (
        core.ReleaseDependency(kind="job", identity=JOB) in cluster.attempt().release_dependencies
    )
    for kind, index in plan:
        pending = cluster.pending()
        if kind == "answer" and pending:
            cluster.answer(pending[index % len(pending)])
        elif kind == "tick":
            cluster.tick()
    run_out(cluster)

    owner = cluster.attempt()
    job = cluster.state.evaluation.jobs[0]
    assert job.terminal
    assert job.released
    assert owner.release_dependencies == ()
    assert owner.phase == core.AttemptPhase.TERMINAL
