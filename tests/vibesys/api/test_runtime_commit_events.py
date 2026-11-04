"""Confirmed core commits derive semantic projection events strategy-agnostically.

After each confirmed commit the run diffs the previous projected `RunView` against the new
one. It emits `ROUND_FINISHED` and `WORKSTREAM_FINISHED` for newly observed rounds,
`EXPERIMENTS_CHANGED` when the displayed experiment projection changes, and
`ITERATIONS_CHANGED` when the policy-owned navigation changes, and `INPUT_MEASUREMENT_FAILED`
when the input's trusted measurement is first seen to have failed. These tests use a synthetic
projector and no concrete strategy ID.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Literal

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from tests.support.real_git import NO_GIT, NO_GITHUB

from vibesys.api import IterationKind, IterationStatus, IterationSummary, IterationTree
from vibesys.candidates.api import InputFailureKind
from vibesys.events import (
    CoreEvent,
    CoreEventType,
    FrameworkWarningData,
    InputMeasurementFailureKind,
    IterationsChangedData,
    WorkstreamFailureKind,
)
from vibesys.run.contracts import (
    InputFailureSummary,
    IterationUnavailable,
    IterationUnavailableReason,
    RoundFailureSummary,
    RoundSummary,
    RunStatus,
    RunView,
)
from vibesys.run.integration import LocalRunIntegration
from vs_redaction.api import redact_diagnostic_tail
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api.testing import ManualClock, SimThreads, examples

if TYPE_CHECKING:
    import pytest

_FAILURE_TEXT_LIMIT = 4000


def _integration() -> LocalRunIntegration:
    """A run integration that runs no Git or GitHub command: these tests only observe commits."""
    return LocalRunIntegration(
        clock=ManualClock(),
        git=NO_GIT,
        github=NO_GITHUB,
        threads=SimThreads(),
        system=idle_environment_system(),
    )


class _FakeAgentState(BaseModel):
    """Minimal typed state carrying only what the projector needs."""

    round_numbers: tuple[int, ...] = ()
    active: int = 0
    revision: int = 0
    profile_skipped: bool = False
    round_status: Literal["completed", "failed"] = "completed"
    last_error: RoundFailureSummary | None = None
    repeated_error: bool = False
    unprojectable: bool = False
    """The policy's projection of this state fails validation."""
    input_failure: InputFailureSummary | None = None


class _FakeProjector:
    """Project `_FakeAgentState` into the product view a frontend consumes."""

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        if namespace != "commit-probe" or not isinstance(state, _FakeAgentState):
            return None
        if state.unprojectable:
            # As the product projector does when a policy's projection fails validation.
            return RunView(
                run_id=run_id,
                loop="commit-probe",
                status=RunStatus.ACTIVE,
                iterations=IterationUnavailable(
                    reason=IterationUnavailableReason.PROJECTION_INVALID
                ),
                experiment_revision=state.revision,
            )
        return _agent_view(state, run_id=run_id)


def _iterations(round_numbers: tuple[int, ...]) -> IterationTree:
    return IterationTree.from_nodes(
        tuple(
            IterationSummary(
                id=f"round:{number}",
                kind=IterationKind.ROUND,
                label=f"Round {number}",
                ordinal=number,
                status=IterationStatus.COMPLETED,
            )
            for number in round_numbers
        )
    )


def _agent_view(state: _FakeAgentState, *, run_id: str = "probe") -> RunView:
    return RunView(
        run_id=run_id,
        loop="commit-probe",
        status=RunStatus.ACTIVE,
        projection={"rounds": list(state.round_numbers), "active": state.active},
        iterations=_iterations(state.round_numbers),
        rounds=tuple(
            RoundSummary(
                number=number,
                status=state.round_status,
                attempts=1,
                judge_verdict="pass",
                profile_skipped=state.profile_skipped,
                last_error=state.last_error,
                repeated_error=state.repeated_error,
            )
            for number in state.round_numbers
        ),
        # A core run stamps every projection with its commit revision.
        experiment_revision=state.revision,
        input_failure=state.input_failure,
    )


def test_commit_projection_preserves_semantics_and_publication_order() -> None:
    integration = _integration()
    order: list[str] = []
    captured: list[CoreEvent] = []

    def capture_event(event: CoreEvent) -> None:
        order.append(event.type.value)
        captured.append(event)

    integration.add_committed_state_listener(
        lambda _namespace, _state, _changed: order.append("state_published")
    )
    integration.events.subscribe(capture_event)
    observer = integration.core_commit_observer("probe", _FakeProjector(), "commit-probe")

    state = _FakeAgentState(round_numbers=(1,), revision=1, profile_skipped=True)
    observer.committed(None, state)

    round_finished = next(event for event in captured if event.type is CoreEventType.ROUND_FINISHED)
    assert round_finished.data is not None
    assert round_finished.data.kind == "round_finished"
    assert round_finished.data.profile_skipped is True
    workstream_finished = next(
        event for event in captured if event.type is CoreEventType.WORKSTREAM_FINISHED
    )
    assert workstream_finished.data is not None
    assert workstream_finished.data.kind == "workstream_finished"
    assert workstream_finished.data.repeated_error is False
    assert workstream_finished.data.last_error is None
    assert workstream_finished.round_label == round_finished.round_label == "round-1"
    iterations_changed = next(
        event for event in captured if event.type is CoreEventType.ITERATIONS_CHANGED
    )
    assert iterations_changed.data == IterationsChangedData(
        revision=_iterations(state.round_numbers).revision
    )
    assert order[:4] == [
        "state_published",
        CoreEventType.ROUND_FINISHED.value,
        CoreEventType.WORKSTREAM_FINISHED.value,
        CoreEventType.ITERATIONS_CHANGED.value,
    ]


def test_workstream_finished_carries_a_typed_bounded_redacted_failure() -> None:
    """The commit producer preserves typed failures and sanitizes their bounded text."""
    integration = _integration()
    captured: list[CoreEvent] = []
    integration.events.subscribe(captured.append)
    observer = integration.core_commit_observer("probe", _FakeProjector(), "commit-probe")

    failure = RoundFailureSummary(
        kind=WorkstreamFailureKind.REPEATED_TURN.value,
        role="implementer",
        turns=3,
        limit=3,
        text="the implementer repeated an earlier turn",
    )
    state = _FakeAgentState(
        round_numbers=(1,),
        revision=1,
        round_status="failed",
        last_error=failure,
        repeated_error=True,
    )
    observer.committed(None, state)

    workstream_finished = next(
        event for event in captured if event.type is CoreEventType.WORKSTREAM_FINISHED
    )
    assert workstream_finished.status is not None
    assert workstream_finished.status.value == "failed"
    assert workstream_finished.data is not None
    assert workstream_finished.data.kind == "workstream_finished"
    assert workstream_finished.data.repeated_error is True
    assert workstream_finished.data.last_error is not None
    assert workstream_finished.data.last_error.kind is WorkstreamFailureKind.REPEATED_TURN
    assert workstream_finished.data.last_error.role == "implementer"
    assert workstream_finished.data.last_error.turns == 3
    assert workstream_finished.data.last_error.limit == 3
    assert workstream_finished.data.last_error.text == "the implementer repeated an earlier turn"

    credential = "API_TOKEN=workstream-secret"
    for round_number, split_at in enumerate(range(1, len("API_TOKEN")), start=2):
        suffix = "z" * (_FAILURE_TEXT_LIMIT + split_at - len(credential) - 1)
        observer.committed(
            None,
            _FakeAgentState(
                round_numbers=tuple(range(1, round_number + 1)),
                revision=round_number,
                round_status="failed",
                last_error=RoundFailureSummary(
                    kind=WorkstreamFailureKind.NO_TRUSTED_EVIDENCE.value,
                    text="prefix:" + credential + " " + suffix,
                    operator_text=redact_diagnostic_tail(
                        "prefix:" + credential + " " + suffix,
                        max_chars=_FAILURE_TEXT_LIMIT,
                    ),
                ),
            ),
        )

        finished = next(
            event for event in reversed(captured) if event.type is CoreEventType.WORKSTREAM_FINISHED
        )
        assert finished.data is not None
        assert finished.data.kind == "workstream_finished"
        assert finished.data.last_error is not None
        assert len(finished.data.last_error.text) <= _FAILURE_TEXT_LIMIT
        assert "workstream-secret" not in finished.data.last_error.text


def test_the_wire_input_failure_kinds_mirror_the_candidates_kinds() -> None:
    assert [kind.value for kind in InputMeasurementFailureKind] == [
        kind.value for kind in InputFailureKind
    ]


_INPUT_FAILURES = st.builds(
    InputFailureSummary,
    kind=st.sampled_from([kind.value for kind in InputFailureKind]),
    stage=st.none() | st.sampled_from(["correctness", "benchmark"]),
    output=st.sampled_from(["", "hung past 1200 s; API_TOKEN=input-secret", "z" * 3000]),
    attempts=st.integers(1, 2),
    compared=st.booleans(),
)


def _input_states(
    pending: int, failure: InputFailureSummary | None, after: int
) -> list[_FakeAgentState]:
    """A run's commits: the input pending for ``pending`` of them, then its result for ``after``.

    The input is measured once per run, so once its failure is committed every later commit
    holds the same failure.
    """
    states = [_FakeAgentState(revision=revision) for revision in range(pending + 1)]
    states += [
        _FakeAgentState(revision=revision, input_failure=failure)
        for revision in range(pending + 1, pending + 1 + after)
    ]
    return states


@given(
    run=st.tuples(st.integers(0, 3), st.none() | _INPUT_FAILURES, st.integers(1, 4)),
    restart=st.integers(0, 7),
    announcing_before=st.booleans(),
)
@settings(max_examples=examples(pr=20, nightly=200))
def test_a_failed_input_measurement_is_announced_once_per_run_across_a_restart(
    tmp_path_factory: pytest.TempPathFactory,
    run: tuple[int, InputFailureSummary | None, int],
    restart: int,
    *,
    announcing_before: bool,
) -> None:
    """S173: the input's failure is told once per run, with its stage and a safe tail.

    ``run`` is how many commits the input is pending, its result, and how many commits follow.
    The run restarts after commit ``restart``: a new process builds a new integration on the
    same log directory and a new observer, whose first commit's ``previous`` is the persisted
    record. Before the restart the run either announced as this build does, or ran a build that
    never announced (``announcing_before`` false, as a run checkpointed before S173 did): then
    the restarted process announces the failure it loads.
    """
    pending, failure, after = run
    states = _input_states(pending, failure, after)
    restart = min(restart, len(states) - 2)  # at least one commit after the restart
    log_dir = tmp_path_factory.mktemp("run") / "logs"
    first = _integration()
    first.attach(log_dir, run_id="probe")
    if announcing_before:
        observer = first.core_commit_observer("probe", _FakeProjector(), "commit-probe")
        for before, now in itertools.pairwise(states[: restart + 1]):
            observer.committed(before, now)
    restarted = _integration()
    restarted.attach(log_dir, run_id="probe")
    resumed = restarted.core_commit_observer("probe", _FakeProjector(), "commit-probe")
    for before, now in itertools.pairwise(states[restart:]):
        resumed.committed(before, now)

    told = [
        event
        for event in restarted.events.read()
        if event.type is CoreEventType.INPUT_MEASUREMENT_FAILED
    ]
    assert len(told) == (failure is not None)
    if failure is None:
        return
    data = told[0].data
    assert data is not None
    assert data.kind == "input_measurement_failed"
    assert data.failure.value == failure.kind
    assert data.stage == failure.stage
    assert (data.attempts, data.compared) == (failure.attempts, failure.compared)
    assert "input-secret" not in data.summary
    assert len(data.summary) <= 1000
    assert data.summary == redact_diagnostic_tail(failure.output, max_chars=1000)


@given(
    steps=st.lists(
        st.tuples(st.integers(min_value=0, max_value=3), st.integers(min_value=0, max_value=2)),
        min_size=0,
        max_size=12,
    ),
    resume_at=st.integers(min_value=0, max_value=12),
)
@settings(max_examples=examples(pr=10, nightly=100))
def test_commit_events_property_no_double_emission_and_semantic_changes(
    steps: list[tuple[int, int]], resume_at: int
) -> None:
    """Rounds emit once, while experiment and iteration events follow their own projections."""
    round_numbers: list[int] = []
    active = 0
    states: list[_FakeAgentState] = [_FakeAgentState()]
    for revision, (new_rounds, active_delta) in enumerate(steps, start=1):
        next_round = (round_numbers[-1] + 1) if round_numbers else 1
        round_numbers.extend(range(next_round, next_round + new_rounds))
        active += active_delta
        states.append(
            _FakeAgentState(round_numbers=tuple(round_numbers), active=active, revision=revision)
        )

    integration = _integration()
    observer = integration.core_commit_observer("probe", _FakeProjector(), "commit-probe")
    captured: list[CoreEvent] = []
    integration.events.subscribe(captured.append)
    for index in range(1, len(states)):
        before = states[index - 1]
        if index == resume_at:
            before = _FakeAgentState.model_validate(before.model_dump())
        observer.committed(before, states[index])

    announced = [
        int((event.round_label or "").removeprefix("round-"))
        for event in captured
        if event.type is CoreEventType.ROUND_FINISHED
    ]
    assert announced == list(round_numbers)

    experiment_changes = sum(
        1 for event in captured if event.type is CoreEventType.EXPERIMENTS_CHANGED
    )
    expected_experiment_changes = sum(
        1
        for index in range(1, len(states))
        if (states[index - 1].round_numbers, states[index - 1].active)
        != (states[index].round_numbers, states[index].active)
    )
    assert experiment_changes == expected_experiment_changes

    iteration_changes = sum(
        1 for event in captured if event.type is CoreEventType.ITERATIONS_CHANGED
    )
    expected_iteration_changes = sum(
        1
        for index in range(1, len(states))
        if states[index - 1].round_numbers != states[index].round_numbers
    )
    assert iteration_changes == expected_iteration_changes


def test_a_commit_of_another_namespace_projects_nothing() -> None:
    integration = _integration()
    captured: list[CoreEvent] = []
    integration.events.subscribe(captured.append)
    observer = integration.core_commit_observer("probe", _FakeProjector(), "other-namespace")

    observer.committed(None, _FakeAgentState(round_numbers=(1,), revision=1))

    assert not [
        event
        for event in captured
        if event.type in {CoreEventType.ROUND_FINISHED, CoreEventType.ITERATIONS_CHANGED}
    ]


@given(unprojectable=st.lists(st.booleans(), max_size=10), held=st.integers(0, 3))
@settings(max_examples=examples(pr=20, nightly=200))
def test_a_commit_whose_view_cannot_be_built_warns_once_and_loses_no_round(
    unprojectable: list[bool], held: int
) -> None:
    """Each commit adds one round; some commits' views cannot be built.

    Regression: a projection that failed validation escaped the commit observer and ended a
    live run. Now each stretch of commits whose view cannot be built warns once when it starts,
    and the first commit after it whose view can be built announces the stretch's rounds, so
    every round of the last buildable view is announced exactly once, in order. The observer
    starts as a restarted process does: the record it held (``held`` rounds) was announced by the
    process before it, so none of those rounds is announced again, even when the first commit
    after the restart cannot be built.
    """
    integration = _integration()
    observer = integration.core_commit_observer("probe", _FakeProjector(), "commit-probe")
    captured: list[CoreEvent] = []
    integration.events.subscribe(captured.append)
    states = [_FakeAgentState(round_numbers=tuple(range(1, held + 1)))]
    for revision, broken in enumerate(unprojectable, start=1):
        states.append(
            _FakeAgentState(
                round_numbers=tuple(range(1, held + revision + 1)),
                revision=revision,
                unprojectable=broken,
            )
        )

    for before, after in itertools.pairwise(states):
        observer.committed(before, after)

    warnings = [
        event
        for event in captured
        if event.type is CoreEventType.FRAMEWORK_WARNING
        and isinstance(event.data, FrameworkWarningData)
        and event.data.source_label == "projection"
    ]
    stretches = sum(
        1
        for index, now in enumerate(unprojectable)
        if now and (index == 0 or not unprojectable[index - 1])
    )
    assert len(warnings) == stretches
    announced = [
        int((event.round_label or "").removeprefix("round-"))
        for event in captured
        if event.type is CoreEventType.ROUND_FINISHED
    ]
    shown = [state for state in states if not state.unprojectable]
    assert announced == [number for number in shown[-1].round_numbers if number > held]
